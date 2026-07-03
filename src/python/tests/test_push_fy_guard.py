"""Unit tests for the push financial-year guard.

A cloud->Tally push targets the company by NAME only. When a client keeps a
previous-year company with the SAME name, opening it makes it the active company
and a push would land in the wrong year's books. The guard reads the LOADED
company's financial-year period (STARTINGFROM/ENDINGAT, via SVCURRENTCOMPANY) and
refuses any voucher whose date falls outside it. Self-contained: no cloud/backend.

Run: python tests/test_push_fy_guard.py   (or: pytest tests/test_push_fy_guard.py)
"""

import os
import sys
from datetime import date
from unittest.mock import MagicMock

sys.modules.setdefault("requests", MagicMock())
sys.modules.setdefault("xmltodict", MagicMock())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sync_main  # noqa: E402
from sync_main import PushCompanyMismatch  # noqa: E402

# Company A = FY 2023-24 (closed single year); Company B = current, multi-year.
FY_A = (date(2023, 4, 1), date(2024, 3, 31))
FY_B = (date(2024, 4, 1), date(2026, 7, 17))


def _assert_blocks(voucher, period, message):
    try:
        sync_main.assert_voucher_in_loaded_fy(voucher, period)
    except PushCompanyMismatch:
        return
    raise AssertionError(message)


def test_voucher_inside_period_allows():
    sync_main.assert_voucher_in_loaded_fy({"date": "2023-06-01"}, FY_A)
    sync_main.assert_voucher_in_loaded_fy({"date": "2026-03-17"}, FY_B)


def test_current_voucher_into_old_company_blocks():
    # The exact failure seen live: a 2026 voucher while the FY2023-24 company is open.
    _assert_blocks({"date": "2026-03-17"}, FY_A, "expected block: 2026 voucher into FY2023-24 company")


def test_old_voucher_into_newer_company_blocks():
    _assert_blocks({"date": "2023-06-01"}, FY_B, "expected block: 2023 voucher into a company that starts 2024-04-01")


def test_current_fy_headroom_allows_post_last_entry():
    # Voucher dated after the last recorded entry but within the same FY must pass
    # (upper bound is the end of the FY containing ENDINGAT, not ENDINGAT itself).
    sync_main.assert_voucher_in_loaded_fy({"date": "2026-08-01"}, FY_B)  # FY end = 2027-03-31


def test_compact_date_format_supported():
    sync_main.assert_voucher_in_loaded_fy({"date": "20230601"}, FY_A)
    _assert_blocks({"date": "20260317"}, FY_A, "expected block: compact 2026 date into FY2023-24")


def test_missing_or_bad_date_blocks():
    _assert_blocks({}, FY_B, "expected block: voucher with no date")
    _assert_blocks({"date": "not-a-date"}, FY_B, "expected block: voucher with unparseable date")


def test_read_period_returns_none_when_unreadable():
    sync_main.get_company_period = lambda: "<xml/>"
    sync_main.parse_company_period = lambda _x=None: {}  # no fy_start/fy_end
    assert sync_main.read_loaded_company_period() is None


def test_read_period_parses_dates():
    sync_main.get_company_period = lambda: "<xml/>"
    sync_main.parse_company_period = lambda _x=None: {"fy_start": "2024-04-01", "fy_end": "2026-07-17"}
    assert sync_main.read_loaded_company_period() == (date(2024, 4, 1), date(2026, 7, 17))


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\nPASS: {len(fns)} push-fy-guard tests")


if __name__ == "__main__":
    try:
        _run_all()
    except AssertionError as exc:
        print(f"\nFAIL: {exc}")
        sys.exit(1)
