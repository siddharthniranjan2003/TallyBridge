#!/usr/bin/env python3
"""
Standalone tester for sale pricing: global-latest rate + party-scoped discount.

Runs ONLY the pure-Python branch in handler.py -- Supabase reads are stubbed, no
OCR, no GPU, no network. Covers the rule change of 2026-07-31:

  rate     = latest GST SALE of the item to ANYONE   (party irrelevant)
  discount = latest GST SALE of the item to THIS party (no history -> 0%)

so rate and discount routinely come from different vouchers. The waterfall this
replaced had no automated tests at all.

The case worth staring at is E8 vs E2: both put 0.0 in discount_pct, but one is a
real "this party's last sale carried no discount" and the other is "never sold to
this party". Only `discount_source` tells them apart, which is why the merge keys
off `name in discounts` rather than the value's truthiness.

Usage:
    python server/test_sale_pricing.py
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
    def __init__(self, rows: object, ok: bool = True) -> None:
        self._rows = rows
        self.ok = ok
        self.text = "" if rows in ([], None) else "[...]"

    def json(self) -> object:
        return self._rows


class FakeSession:
    """Captures the RPC calls the fetchers make and replays canned responses."""

    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url: str, headers: dict, json: dict, timeout: float) -> FakeResponse:
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt if isinstance(nxt, FakeResponse) else FakeResponse(nxt)


handler.SUPABASE_URL = "https://stub.supabase.co"
handler.SUPABASE_KEY = "stub-key"

# Not an assertion -- the effective value comes from MINICPM_PUSH_QUEUE_TIMEOUT_SECONDS
# in parsing/.env (or the Cloud Run service env), which overrides the code default and is
# per-machine. But a low value is worth shouting about: the batch rate RPC scans
# voucher_items (~216k rows, no index on stock_item_name), and a timeout there prices the
# entire invoice at 0. parsing/.env shipped 8s, which is nowhere near enough.
if handler.PUSH_QUEUE_TIMEOUT_SECONDS < 180:
    print(
        f"  WARN  MINICPM_PUSH_QUEUE_TIMEOUT_SECONDS is {handler.PUSH_QUEUE_TIMEOUT_SECONDS}s "
        "in this environment; the rate RPC needs 180s. Fix the env before deploying.\n"
    )


def with_session(responses: list[object]) -> FakeSession:
    session = FakeSession(responses)
    handler._supabase_session = lambda: session
    return session


# ── The two RPC fetchers ───────────────────────────────────────────────────────
print("fetch_latest_sale_rates -- request shape")
session = with_session([[{"stock_item_name": "DRILL 5.1", "rate": "1200.5", "party_name": "ISH TRADING"}]])
rates = handler.fetch_latest_sale_rates(["DRILL 5.1", "TAP 1 BSW"])
call = session.calls[-1]
check("hits the batch rate RPC", call["url"].endswith("/rpc/get_latest_sale_rates_for_items"), True)
check("sends the whole item array", call["json"], {"p_item_names": ["DRILL 5.1", "TAP 1 BSW"]})
check("uses the shared timeout constant", call["timeout"], handler.PUSH_QUEUE_TIMEOUT_SECONDS)
check("parses rate + party", rates, {"DRILL 5.1": {"rate": 1200.5, "party_name": "ISH TRADING"}})

print()
print("fetch_latest_party_discounts -- request shape")
session = with_session([[{"stock_item_name": "DRILL 5.1", "discount_pct": "45"}]])
discounts = handler.fetch_latest_party_discounts("HARYANA STEEL", ["DRILL 5.1"])
call = session.calls[-1]
check("hits the batch discount RPC", call["url"].endswith("/rpc/get_latest_party_discounts_for_items"), True)
check("sends party + items", call["json"], {"p_party_name": "HARYANA STEEL", "p_item_names": ["DRILL 5.1"]})
check("parses discount", discounts, {"DRILL 5.1": 45.0})

print()
print("fetcher degradation (E14 / E14b)")
with_session([RuntimeError("connection reset")])
check("rate RPC raising -> {}", handler.fetch_latest_sale_rates(["DRILL 5.1"]), {})
with_session([FakeResponse([], ok=False)])
check("rate RPC non-2xx -> {}", handler.fetch_latest_sale_rates(["DRILL 5.1"]), {})
with_session([RuntimeError("connection reset")])
check("discount RPC raising -> {}", handler.fetch_latest_party_discounts("P", ["DRILL 5.1"]), {})
check("no items -> no call at all", handler.fetch_latest_sale_rates([]), {})
check("no party -> no discount call", handler.fetch_latest_party_discounts("", ["DRILL 5.1"]), {})


# ── The merge: build_sale_rate_map_global ──────────────────────────────────────
def rate_map(rate_rows: list[dict], discount_rows: list[dict], party: str = "HARYANA STEEL") -> dict:
    handler.fetch_latest_sale_rates = lambda item_names: {
        r["stock_item_name"]: {"rate": float(r["rate"]), "party_name": r["party_name"]} for r in rate_rows
    }
    handler.fetch_latest_party_discounts = lambda party_name, item_names: {
        r["stock_item_name"]: float(r["discount_pct"]) for r in discount_rows
    }
    return handler.build_sale_rate_map_global(party, ["DRILL 5.1"])


print()
print("rate/discount merge")
check(
    "E1  newer sale elsewhere wins the rate, this party still supplies the discount",
    rate_map(
        [{"stock_item_name": "DRILL 5.1", "rate": 12606, "party_name": "ISH TRADING"}],
        [{"stock_item_name": "DRILL 5.1", "discount_pct": 45}],
    ),
    {"DRILL 5.1": {"rate": 12606.0, "discount_pct": 45.0, "source": "different_party", "discount_source": "same_party"}},
)
check(
    "E2  never sold to this party -> global rate, 0%, discount_source none",
    rate_map([{"stock_item_name": "DRILL 5.1", "rate": 12606, "party_name": "ISH TRADING"}], []),
    {"DRILL 5.1": {"rate": 12606.0, "discount_pct": 0.0, "source": "different_party", "discount_source": "none"}},
)
check(
    "E3  never sold to anyone -> item absent from the map entirely",
    rate_map([], []),
    {},
)
check(
    "E8  this party's last sale carried 0% -> 0.0 but discount_source same_party",
    rate_map(
        [{"stock_item_name": "DRILL 5.1", "rate": 12606, "party_name": "ISH TRADING"}],
        [{"stock_item_name": "DRILL 5.1", "discount_pct": 0}],
    ),
    {"DRILL 5.1": {"rate": 12606.0, "discount_pct": 0.0, "source": "different_party", "discount_source": "same_party"}},
)
check(
    "rate row happens to be this party -> source same_party",
    rate_map(
        [{"stock_item_name": "DRILL 5.1", "rate": 900, "party_name": "haryana steel"}],
        [{"stock_item_name": "DRILL 5.1", "discount_pct": 10}],
    ),
    {"DRILL 5.1": {"rate": 900.0, "discount_pct": 10.0, "source": "same_party", "discount_source": "same_party"}},
)
check(
    "E14 rate lookup down -> empty map even though discounts came back",
    rate_map([], [{"stock_item_name": "DRILL 5.1", "discount_pct": 45}]),
    {},
)
check(
    "E14b discount lookup down -> priced, but discount_source none",
    rate_map([{"stock_item_name": "DRILL 5.1", "rate": 500, "party_name": "ISH TRADING"}], []),
    {"DRILL 5.1": {"rate": 500.0, "discount_pct": 0.0, "source": "different_party", "discount_source": "none"}},
)
check("E16/E17 no usable item names -> no lookup", handler.build_sale_rate_map_global("P", ["", None]), {})


# ── The payload ───────────────────────────────────────────────────────────────
handler.fetch_party_state = lambda party_name: "Haryana"  # intra-state -> CGST+SGST
handler.build_sale_rate_map_global = lambda party_name, item_names: {
    "DRILL 5.1": {"rate": 1000.0, "discount_pct": 10.0, "source": "different_party", "discount_source": "same_party"},
    "TAP 1 BSW": {"rate": 500.0, "discount_pct": 0.0, "source": "same_party", "discount_source": "none"},
}

PAYLOAD_ROWS = [
    {"stock_matched": "DRILL 5.1", "qty_text": "5", "unit": "NOS", "match_score": 100.0},
    {"stock_matched": "TAP 1 BSW", "qty_text": "2 NOS", "unit": "NOS", "match_score": 88.0},
    {"stock_matched": "NO MATCH", "qty_text": "9", "unit": "NOS", "match_score": 0.0},
]

request, priced, source = handler.build_sale_voucher_payload("K V ENTERPRISES", "HARYANA STEEL", PAYLOAD_ROWS)
voucher = request["voucher_payload"]

print()
print("built voucher")
check("E17 NO MATCH row is dropped", len(voucher["items"]), 2)
check(
    "net amount deducts the line's own discount",
    [item["amount"] for item in voucher["items"]],
    [4500.0, 1000.0],
)
check("rate stays gross", [item["rate"] for item in voucher["items"]], [1000.0, 500.0])
check("discount_total is the rupee sum", voucher["discount_total"], 500.0)

ledgers = {entry["ledger_name"]: entry["amount"] for entry in voucher["ledger_entries"]}
check("GST on the NET subtotal", {"CGST": ledgers["CGST"], "SGST": ledgers["SGST"]}, {"CGST": 495.0, "SGST": 495.0})
check("party is debited the gross total", ledgers["HARYANA STEEL"], 6490.0)
check(
    "inventory ledger equals the sum of item amounts (tally_pusher invariant)",
    ledgers["GST SALE"],
    sum(item["amount"] for item in voucher["items"]),
)

print()
print("provenance")
check(
    "pushed voucher carries NO provenance keys",
    sorted({k for item in voucher["items"] for k in item}),
    ["amount", "discount_pct", "godown_name", "quantity", "rate", "stock_item_name", "unit"],
)
check(
    "source_payload records both sources",
    [(item["rate_source"], item["discount_source"]) for item in source["items"]],
    [("different_party", "same_party"), ("same_party", "none")],
)
check("priced_items keep provenance for the HTTP response", "discount_source" in priced[0], True)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("all sale pricing checks passed")
