#!/usr/bin/env python3
"""
Standalone tester for party candidate retrieval. No network, no OCR, no GPU --
every Supabase call is stubbed.

Covers the two-stage bug that made MUNDHRA AGENCIES a garbage invoice:

  1. The old path paged the `vouchers` table, which holds one row per INVOICE.
     6000 rows bought only 211 distinct customers, reaching '3S DESIGN'..'Cash'.
     1165 of 1376 customers were never candidates at all.
  2. get_distinct_party_names() fixed the ratio -- but PostgREST still caps a
     response at its own max-rows, so the unpaged RPC answered
     Content-Range 0-999/1376 and dropped 'S LAL TOOLS'..'ZODIAC ENGINEERS'.
     Same bug, higher ceiling, equally silent.

The case worth staring at is `stops on the exact total, not on a short page`:
PostgREST caps a page at max-rows regardless of the limit asked for, so a page
shorter than PARTY_RPC_PAGE_SIZE means "capped", NOT "no more data". Trusting a
short page is precisely how a paging loop silently truncates.

And `Range header is ignored on an RPC POST` is the one that cost real time: a
Range-based loop returns 200 with page one every single time, so it reads as
working while it re-fetches rows 0-999 forever.

Usage:
    python server/test_party_candidates.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the top-level parsing/ modules importable when run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import handler  # noqa: E402

FAILURES: list[str] = []


def check(label: str, actual: object, expected: object) -> None:
    if actual == expected:
        print(f"  PASS  {label}")
        return
    FAILURES.append(label)
    print(f"  FAIL  {label}\n          expected: {expected!r}\n          actual:   {actual!r}")


class FakeResponse:
    def __init__(self, rows: object, ok: bool = True, content_range: str = "") -> None:
        self._rows = rows
        self.ok = ok
        self.text = "" if rows in ([], None) else "[...]"
        self.headers = {"content-range": content_range} if content_range else {}

    def json(self) -> object:
        return self._rows


class FakeSession:
    """Captures the RPC calls the fetcher makes and replays canned responses."""

    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"unexpected extra request: {kwargs.get('params')}")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt if isinstance(nxt, FakeResponse) else FakeResponse(nxt)


handler.SUPABASE_URL = "https://stub.supabase.co"
handler.SUPABASE_KEY = "stub-key"


def with_session(responses: list[object]) -> FakeSession:
    session = FakeSession(responses)
    handler._supabase_session = lambda: session
    handler.PARTY_NAME_CACHE = None
    return session


def rows(*names: str) -> list[dict]:
    return [{"party_name": name} for name in names]


def bulk(prefix: str, count: int, start: int = 0) -> list[dict]:
    return [{"party_name": f"{prefix}{i:04d}"} for i in range(start, start + count)]


# ── Content-Range parsing ──────────────────────────────────────────────────────
print("_content_range_total")
check("reads the total", handler._content_range_total("0-999/1376"), 1376)
check("reads a count-only range", handler._content_range_total("*/1376"), 1376)
check("unknown total -> None", handler._content_range_total("0-999/*"), None)
check("absent header -> None", handler._content_range_total(""), None)
check("malformed -> None", handler._content_range_total("garbage"), None)


# ── Paging the RPC ─────────────────────────────────────────────────────────────
print()
print("fetch_distinct_party_names -- request shape")
session = with_session([FakeResponse(rows("A TOOLS", "B TOOLS"), content_range="0-1/2")])
names = handler.fetch_distinct_party_names()
call = session.calls[-1]
check("hits the distinct-names RPC", call["url"].endswith("/rpc/get_distinct_party_names"), True)
check("pages with limit/offset query params", call["params"], {"limit": str(handler.PARTY_RPC_PAGE_SIZE), "offset": "0"})
check(
    "Range header is ignored on an RPC POST, so it must not be sent",
    [k for k in call["headers"] if k.lower().startswith("range")],
    [],
)
check("asks for an exact count on the first page", call["headers"].get("Prefer"), "count=exact")
check("uses the shared timeout constant", call["timeout"], handler.PUSH_QUEUE_TIMEOUT_SECONDS)
check("returns the names", names, ["A TOOLS", "B TOOLS"])

print()
print("paging to the end of the alphabet (the 1000/1376 truncation)")
session = with_session(
    [
        FakeResponse(bulk("P", 1000), content_range="0-999/1376"),
        FakeResponse(bulk("Q", 376), content_range="1000-1375/1376"),
    ]
)
names = handler.fetch_distinct_party_names()
check("collects every page", len(names), 1376)
check("stops as soon as the total is consumed", len(session.calls), 2)
check("second page offsets past the first", session.calls[1]["params"], {"limit": "1000", "offset": "1000"})
check("count=exact is asked for once, not per page", session.calls[1]["headers"].get("Prefer"), None)

print()
print("stops on the exact total, not on a short page")
# PostgREST caps a page at its own max-rows: ask for 2000, get 1000. A short page
# here means "capped", not "done" -- stopping on it truncates at exactly the bug
# this function exists to fix.
original_page_size = handler.PARTY_RPC_PAGE_SIZE
handler.PARTY_RPC_PAGE_SIZE = 2000
session = with_session(
    [
        FakeResponse(bulk("P", 1000), content_range="0-999/1376"),
        FakeResponse(bulk("Q", 376), content_range="1000-1375/1376"),
    ]
)
names = handler.fetch_distinct_party_names()
check("a server-capped page does not end the loop", len(names), 1376)
handler.PARTY_RPC_PAGE_SIZE = original_page_size

print()
print("stops on a short page when the total is unknown")
session = with_session([FakeResponse(bulk("P", 12), content_range="0-11/*")])
check("no total to steer by -> short page ends it", len(handler.fetch_distinct_party_names()), 12)
check("and asks for nothing more", len(session.calls), 1)

print()
print("page budget is a guard, not a data limit")
original_max = handler.PARTY_RPC_MAX_PAGES
handler.PARTY_RPC_MAX_PAGES = 2
session = with_session(
    [
        FakeResponse(bulk("P", 1000, 0), content_range="0-999/5000"),
        FakeResponse(bulk("P", 1000, 1000), content_range="1000-1999/5000"),
    ]
)
check("stops at the cap rather than looping forever", len(handler.fetch_distinct_party_names()), 2000)
handler.PARTY_RPC_MAX_PAGES = original_max


# ── Degradation: None means "fall back", [] would mean "match against nobody" ──
print()
print("degradation -- None vs []")
with_session([FakeResponse([], ok=False)])
check("RPC missing (PGRST202) -> None", handler.fetch_distinct_party_names(), None)
with_session([RuntimeError("connection reset")])
check("transport error on page 1 -> None", handler.fetch_distinct_party_names(), None)
with_session([FakeResponse([], content_range="*/0")])
check("genuinely empty -> None, never []", handler.fetch_distinct_party_names(), None)
with_session([FakeResponse({"code": "PGRST202"}, content_range="")])
check("error object instead of a list -> None", handler.fetch_distinct_party_names(), None)
session = with_session(
    [
        FakeResponse(bulk("P", 1000), content_range="0-999/1376"),
        FakeResponse([], ok=False),
    ]
)
check(
    "page 2 failing keeps page 1 rather than falling back to 211",
    len(handler.fetch_distinct_party_names() or []),
    1000,
)

print()
print("a renamed PARTY_COLUMN cannot silently yield an empty list")
# The SQL function hardcodes its output column as party_name. If PARTY_COLUMN was
# overridden, every row parses to "" and the matcher would score against nobody.
original_column = handler.PARTY_COLUMN
handler.PARTY_COLUMN = "ledger_name"
with_session([FakeResponse(rows("A TOOLS", "B TOOLS"), content_range="0-1/2")])
check("rows in but no names out -> None", handler.fetch_distinct_party_names(), None)
handler.PARTY_COLUMN = original_column


# ── The caller: RPC first, paging fallback second ──────────────────────────────
print()
print("fetch_supabase_party_candidates -- route selection")
with_session([FakeResponse(rows("MUNDHARA AGENCIES", "S LAL TOOLS"), content_range="0-1/2")])
candidates = handler.fetch_supabase_party_candidates("MUNDHRA AGENCIES")
check("uses the RPC when it exists", candidates, ["MUNDHARA AGENCIES", "S LAL TOOLS"])

# The fallback is load-bearing: a project without the migration must still match.
paged: list[list[dict]] = [rows("BALAJI H/W AGENCIES", "AMBEY TOOLS"), []]


class FakePagedResponse:
    def __init__(self, payload: list[dict]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> list[dict]:
        return self._payload


original_supabase_get = handler.supabase_get
with_session([FakeResponse([], ok=False)])
handler.supabase_get = lambda endpoint, params: FakePagedResponse(paged.pop(0) if paged else [])
candidates = handler.fetch_supabase_party_candidates("MUNDHRA AGENCIES")
check("falls back to paging when the RPC is absent", candidates, ["BALAJI H/W AGENCIES", "AMBEY TOOLS"])
handler.supabase_get = original_supabase_get


# ── Candidate strings must be identical whichever route produced them ──────────
print()
print("route parity")
with_session([FakeResponse([{"party_name": "  MUNDHARA   AGENCIES  "}], content_range="0-0/1")])
check("RPC route collapses whitespace", handler.fetch_distinct_party_names(), ["MUNDHARA AGENCIES"])
with_session([FakeResponse(rows("A TOOLS", "A TOOLS", "B TOOLS"), content_range="0-2/3")])
check("RPC route de-dups", handler.fetch_distinct_party_names(), ["A TOOLS", "B TOOLS"])


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("all party candidate checks passed")
