#!/usr/bin/env python3
"""
Standalone tester for the sale GST head decision (CGST+SGST vs IGST).

Runs ONLY the pure-Python branch in handler.py -- Supabase reads are stubbed, no
OCR, no GPU. Covers the thing that is easy to get wrong:

  The party matcher draws candidates from `vouchers.party_name` (any party, any
  group), but the state lookup used to filter `ledgers` by group_name='Sundry
  Debtors'. A real customer filed under any other group therefore resolved to
  state "" and silently fell into the inter-state branch -- IGST on a local sale.
  `ledgers.name` is unique, so the state lookup matches on name alone.

Usage:
    python server/test_sale_gst_state.py
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
    """Minimal stand-in for the requests.Response bits fetch_party_state reads."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.ok = True
        self.text = "[]" if not rows else "[...]"

    def json(self) -> list[dict]:
        return self._rows


# Every ledger row the stub knows about, keyed by name. Mirrors the real table:
# names are unique across groups, and plenty of genuine customers are filed
# outside "Sundry Debtors".
LEDGER_ROWS = {
    "BALAJI H/W AGENCIES": {"group_name": "Sundry Debtors", "state": "Haryana"},
    "GUPTA BROTHERS SALES PVT LTD": {"group_name": "Sundry Debtors", "state": "Delhi"},
    "SHREE SHYAM ENTERPRISES": {"group_name": "TRADERS", "state": "Haryana"},
    "BAID LALITA & CO.": {"group_name": "Unregistered", "state": "Haryana"},
    "CONCEPT TOOLING": {"group_name": "Sundry Debtors", "state": ""},
}

CAPTURED_PARAMS: list[dict] = []


def fake_supabase_get(endpoint: str, params: dict) -> FakeResponse:
    CAPTURED_PARAMS.append(dict(params))
    name = str(params.get("name", "")).removeprefix("eq.")
    row = LEDGER_ROWS.get(name)
    if row is None:
        return FakeResponse([])
    # Honour a group_name filter if the caller still sends one, so the test can
    # prove the filter is gone rather than merely that the happy path works.
    wanted_group = params.get("group_name")
    if wanted_group is not None and row["group_name"] != str(wanted_group).removeprefix("eq."):
        return FakeResponse([])
    return FakeResponse([{"state": row["state"]}])


handler.supabase_get = fake_supabase_get
handler.SUPABASE_URL = "https://stub.supabase.co"
handler.SUPABASE_KEY = "stub-key"
# Keep the payload builder off the network; the GST branch is what's under test.
handler.build_sale_rate_map = lambda party_name, item_names: {
    "HSS DRILL 5.1": {"rate": 100.0, "discount_pct": 0.0, "source": "same_party"}
}

SALE_ROWS = [{"stock_matched": "HSS DRILL 5.1", "qty_text": "10", "unit": "NOS", "match_score": 100.0}]


def tax_heads(party_name: str) -> dict[str, float]:
    """Build a sale voucher for this party and return its {tax ledger: amount}."""
    payload, _items, _source = handler.build_sale_voucher_payload("K V ENTERPRISES", party_name, SALE_ROWS)
    return {
        entry["ledger_name"]: entry["amount"]
        for entry in payload["voucher_payload"]["ledger_entries"]
        if entry["ledger_name"] in ("CGST", "SGST", "IGST")
    }


print("fetch_party_state query shape")
CAPTURED_PARAMS.clear()
handler.fetch_party_state("SHREE SHYAM ENTERPRISES")
sent = CAPTURED_PARAMS[-1]
check("looks the party up by name", sent.get("name"), "eq.SHREE SHYAM ENTERPRISES")
check("selects the state column", sent.get("select"), "state")
check("sends NO group_name filter", "group_name" in sent, False)

print()
print("state resolution")
check(
    "customer outside Sundry Debtors still resolves",
    handler.fetch_party_state("SHREE SHYAM ENTERPRISES"),
    "Haryana",
)
check("Sundry Debtors customer resolves", handler.fetch_party_state("BALAJI H/W AGENCIES"), "Haryana")
check("out-of-state customer resolves", handler.fetch_party_state("GUPTA BROTHERS SALES PVT LTD"), "Delhi")
check("unknown party stays empty", handler.fetch_party_state("NOT A REAL LEDGER"), "")

print()
print("GST head on a 1000.00 sale")
# The regression: a Haryana customer filed under TRADERS was charged IGST 180.
check(
    "Haryana customer outside Sundry Debtors -> CGST+SGST",
    tax_heads("SHREE SHYAM ENTERPRISES"),
    {"CGST": 90.0, "SGST": 90.0},
)
check(
    "Haryana customer inside Sundry Debtors -> CGST+SGST",
    tax_heads("BALAJI H/W AGENCIES"),
    {"CGST": 90.0, "SGST": 90.0},
)
check(
    "genuinely out-of-state customer -> IGST",
    tax_heads("GUPTA BROTHERS SALES PVT LTD"),
    {"IGST": 180.0},
)
check(
    "Haryana customer filed under Unregistered -> CGST+SGST",
    tax_heads("BAID LALITA & CO."),
    {"CGST": 90.0, "SGST": 90.0},
)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("all sale GST state checks passed")
