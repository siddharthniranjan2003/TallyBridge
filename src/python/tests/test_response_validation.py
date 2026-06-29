"""Tests for Tally response validation (review finding tally-dep-02 / H2).

A degraded TallyPrime (busy, no company loaded, license/activation dialog) can
answer port 9000 with HTTP 200 whose body is an HTML page or empty — not XML
data. These must be rejected loudly instead of being parsed to 0 rows that look
like a genuinely empty company.

Run: python3 tests/test_response_validation.py  (or: pytest tests/test_response_validation.py)
"""

import os
import sys
from unittest.mock import MagicMock

sys.modules.setdefault("requests", MagicMock())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tally_client  # noqa: E402


def _raises(text: str) -> bool:
    try:
        tally_client._check_response(text)
        return False
    except RuntimeError:
        return True


def test_empty_body_rejected():
    assert _raises("")
    assert _raises("   \n\t ")
    # BOM-only / BOM+whitespace from a UTF-16 degraded Tally is still "empty".
    assert _raises("﻿")
    assert _raises("﻿   \n")


def test_assert_data_response_used_by_push_path():
    # push_vouchers routes its response through the same guard; a valid import
    # response passes through unchanged, HTML/empty raises.
    ok = "<ENVELOPE><BODY><DATA><CREATED>1</CREATED></DATA></BODY></ENVELOPE>"
    assert tally_client._assert_data_response(ok) == ok
    for bad in ("", "﻿", "<!DOCTYPE html><html></html>", "<html><body>x</body></html>"):
        try:
            tally_client._assert_data_response(bad)
            assert False, f"expected rejection for {bad!r}"
        except RuntimeError:
            pass


def test_html_license_page_rejected():
    assert _raises("<!DOCTYPE html><html><body>License expired</body></html>")
    assert _raises("<html><head><title>Tally</title></head></html>")


def test_status_zero_still_rejected():
    assert _raises("<ENVELOPE><STATUS>0</STATUS><DATA>Could not find company</DATA></ENVELOPE>")


def test_valid_data_passes_through():
    body = "<ENVELOPE><BODY><DATA><COLLECTION><LEDGER>x</LEDGER></COLLECTION></DATA></BODY></ENVELOPE>"
    assert tally_client._check_response(body) == body


def test_empty_envelope_left_to_wipe_guards():
    # A near-empty envelope (no company / 0 rows) is NOT rejected here on
    # purpose — the master/voucher wipe guards decide, to avoid false failures.
    body = "<ENVELOPE></ENVELOPE>"
    assert tally_client._check_response(body) == body


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\nPASS: {len(fns)} response-validation tests")


if __name__ == "__main__":
    try:
        _run_all()
    except AssertionError as exc:
        print(f"\nFAIL: {exc}")
        sys.exit(1)
