#!/usr/bin/env python3
"""
Standalone tester for the Audit_Trail_Purchase read path.

Runs ONLY the pure-Python lookup + source-tagging logic from voucher_builder.py --
no Supabase, no OCR, no GPU. Covers the two things that are easy to get wrong:

  1. The audit key is CASE-INSENSITIVE. raw_description is upper-cased on the
     recognized-vendor path but case-preserved on the alien path, so a trail taught
     by one path must still be found by the other.
  2. The audit trail is checked BEFORE Purchase_Matching, and an audit hit is tagged
     source="Audit_Trail" in source_payload.items (not "Matching_Algorithem").

Usage:
    python purchase/test_audit_trail.py
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

# Make the top-level parsing/ modules importable when run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.text import audit_trail_key  # noqa: E402
from purchase.models import PurchaseRawItem  # noqa: E402
from purchase.voucher_builder import (  # noqa: E402
    AUDIT_TRAIL_REASON,
    alien_source_label,
    build_source_payload_items,
    match_item_via_audit_trail,
    match_item_via_purchase_matching,
    source_label_for_reason,
)

FAILURES: list[str] = []


def check(label: str, actual: object, expected: object) -> None:
    if actual == expected:
        print(f"  PASS  {label}")
        return
    FAILURES.append(label)
    print(f"  FAIL  {label}\n          expected: {expected!r}\n          actual:   {actual!r}")


def raw_item(description: str) -> PurchaseRawItem:
    return PurchaseRawItem(
        item_code="",
        raw_description=description,
        quantity=Decimal("1"),
        amount=Decimal("100"),
        rate=Decimal("100"),
        unit="NOS",
    )


STOCK_ROWS = [{"name": "HSS DRILL 5.1 ADDISON", "unit": "Nos", "group_name": "TOOLS", "rate": "150"}]

# A trail taught once, keyed exactly as the parser will key it on re-scan.
AUDIT_MAP = {audit_trail_key("M2 HSS PSTD 5.1mm"): "HSS DRILL 5.1 ADDISON"}


print("audit_trail_key canonicalization")
check("collapses whitespace + casefolds", audit_trail_key("  M2  HSS   Pstd 5.1mm "), "m2 hss pstd 5.1mm")
check("None-safe", audit_trail_key(""), "")


print("\nmatch_item_via_audit_trail -- the loop must close across case regimes")
# The vendor path uppercases; the alien path preserves case. Both must hit the
# same trail row. This is the regression the whole feature depends on.
vendor_path = match_item_via_audit_trail(raw_item("M2 HSS PSTD 5.1MM"), STOCK_ROWS, AUDIT_MAP)
alien_path = match_item_via_audit_trail(raw_item("M2 HSS Pstd 5.1mm"), STOCK_ROWS, AUDIT_MAP)
check("vendor path (UPPERCASED) hits", vendor_path.stock_item_name if vendor_path else None, "HSS DRILL 5.1 ADDISON")
check("alien path (case preserved) hits", alien_path.stock_item_name if alien_path else None, "HSS DRILL 5.1 ADDISON")
check("hit carries the audit reason", vendor_path.trace["reason"] if vendor_path else None, AUDIT_TRAIL_REASON)
check("hit scores 100", vendor_path.score if vendor_path else None, 100.0)
# clean_numeric_unit normalizes the catalog unit ("Nos" -> "NOS"), as it does for
# every other matcher.
check("hit adopts the catalog unit", vendor_path.unit if vendor_path else None, "NOS")

check("miss returns None", match_item_via_audit_trail(raw_item("SOMETHING ELSE"), STOCK_ROWS, AUDIT_MAP), None)
check("blank description returns None", match_item_via_audit_trail(raw_item("   "), STOCK_ROWS, AUDIT_MAP), None)
check("empty map returns None", match_item_via_audit_trail(raw_item("M2 HSS PSTD 5.1mm"), STOCK_ROWS, {}), None)
check("None map returns None", match_item_via_audit_trail(raw_item("M2 HSS PSTD 5.1mm"), STOCK_ROWS, None), None)


print("\nprecedence -- the audit trail outranks Purchase_Matching")
# Same OCR text present in BOTH tables, pointing at different items. Whichever the
# builder consults first wins; the audit trail (a human's committed correction) must.
contested = "M2 HSS PSTD 5.1mm"
pm_map = {contested: "WRONG CURATED ITEM"}
audit_hit = match_item_via_audit_trail(raw_item(contested), STOCK_ROWS, AUDIT_MAP)
pm_hit = match_item_via_purchase_matching(raw_item(contested), STOCK_ROWS, pm_map)
check("audit trail resolves its own item", audit_hit.stock_item_name if audit_hit else None, "HSS DRILL 5.1 ADDISON")
check("Purchase_Matching would have won otherwise", pm_hit.stock_item_name if pm_hit else None, "WRONG CURATED ITEM")


print("\nsource tagging")
check("audit reason -> Audit_Trail", source_label_for_reason(AUDIT_TRAIL_REASON), "Audit_Trail")
check("purchase_matching reason -> Purchase_Matching", source_label_for_reason("purchase_matching_exact_lookup"), "Purchase_Matching")
check("anything else -> Matching_Algorithem", source_label_for_reason("fuzzy"), "Matching_Algorithem")
check("alien audit hit -> Audit_Trail", alien_source_label({"matched": True, "match_trace": {"reason": AUDIT_TRAIL_REASON}}), "Audit_Trail")
check("alien fuzzy hit -> Alien_Matched", alien_source_label({"matched": True, "match_trace": {"reason": "fuzzy"}}), "Alien_Matched")
check("alien miss -> Alien_Passthrough", alien_source_label({"matched": False, "match_trace": None}), "Alien_Passthrough")


print("\nbuild_source_payload_items -- raw_ocr must reach the app")
voucher_items = [{"stock_item_name": "HSS DRILL 5.1 ADDISON", "quantity": 1.0, "rate": 100.0}]
matched_items = [
    {
        "raw_description": "M2 HSS PSTD 5.1mm",
        "match_score": 100.0,
        "match_trace": {"reason": AUDIT_TRAIL_REASON},
    }
]
source_items = build_source_payload_items(voucher_items, matched_items)
check("carries raw_ocr from matched_item", source_items[0].get("raw_ocr"), "M2 HSS PSTD 5.1mm")
check("tags the audit source", source_items[0].get("source"), "Audit_Trail")
check("keeps the resolved item name", source_items[0].get("stock_item_name"), "HSS DRILL 5.1 ADDISON")

# A line the audit trail never saw still carries its raw OCR, so the user's edit can teach it.
fuzzy_source = build_source_payload_items(
    [{"stock_item_name": "SOME GUESS"}],
    [{"raw_description": "UNSEEN OCR LINE", "match_score": 61.0, "match_trace": {"reason": "fuzzy"}}],
)
check("unmatched line still carries raw_ocr", fuzzy_source[0].get("raw_ocr"), "UNSEEN OCR LINE")
check("unmatched line tagged Matching_Algorithem", fuzzy_source[0].get("source"), "Matching_Algorithem")

# A matched_item with no raw_description must not blow up or emit None.
degenerate = build_source_payload_items([{"stock_item_name": "X"}], [{}])
check("missing raw_description -> empty string", degenerate[0].get("raw_ocr"), "")


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("all audit-trail checks passed")
