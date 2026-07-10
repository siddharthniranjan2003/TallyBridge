"""Unit tests for the stock-item master push (create item in Tally).

A create-stock-item job rides the voucher push_queue with a payload-embedded
discriminator (kind == "stock_item") and imports via an 'All Masters' envelope.
Live-verified behaviors these tests lock in: Tally treats master Create as an
upsert (repeat Create -> ALTERED), unknown PARENT/BASEUNITS produce LINEERRORs,
and the GST rate splits CGST/SGST = rate/2 with IGST = rate. Self-contained: no
Tally/cloud/backend.

Run: python tests/test_push_stock_items.py
"""

import os
import sys
from unittest.mock import MagicMock

sys.modules.setdefault("requests", MagicMock())
sys.modules.setdefault("xmltodict", MagicMock())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tally_pusher  # noqa: E402
from tally_pusher import (  # noqa: E402
    _build_masters_import_envelope,
    _build_stock_item_xml_block,
    is_stock_item_payload,
)


def test_kind_discriminator():
    assert is_stock_item_payload({"kind": "stock_item", "name": "X"})
    assert is_stock_item_payload({"kind": " Stock_Item "})
    assert not is_stock_item_payload({"voucher_type": "GST SALE"})
    assert not is_stock_item_payload({"kind": "voucher"})
    assert not is_stock_item_payload(None)
    assert not is_stock_item_payload("stock_item")


def test_full_item_block():
    xml = _build_stock_item_xml_block({
        "kind": "stock_item",
        "name": "TEST ITEM",
        "stock_group": "BOSCH",
        "unit": "NOS",
        "gst_applicable": True,
        "gst_rate": 18,
        "hsn_code": "82055990",
        "opening_qty": 0,
    })
    assert '<STOCKITEM NAME="TEST ITEM" ACTION="Create">' in xml
    assert "<PARENT>BOSCH</PARENT>" in xml
    assert "<BASEUNITS>NOS</BASEUNITS>" in xml
    assert "<GSTAPPLICABLE>&#4; Applicable</GSTAPPLICABLE>" in xml
    assert "<HSNCODE>82055990</HSNCODE>" in xml
    # 18% splits into CGST 9 / SGST 9 / IGST 18.
    assert xml.count("<GSTRATE>9</GSTRATE>") == 2
    assert xml.count("<GSTRATE>18</GSTRATE>") == 1
    assert "<HSNDETAILS.LIST>" in xml
    assert "<OPENINGBALANCE>0</OPENINGBALANCE>" in xml


def test_minimal_item_defaults():
    xml = _build_stock_item_xml_block({"kind": "stock_item", "name": "MINIMAL"})
    assert "<PARENT/>" in xml  # empty -> Primary group
    assert "<BASEUNITS>NOS</BASEUNITS>" in xml  # default unit
    # GST defaults to applicable @ 18.
    assert xml.count("<GSTRATE>18</GSTRATE>") == 1


def test_gst_not_applicable_has_no_details():
    xml = _build_stock_item_xml_block({
        "kind": "stock_item",
        "name": "EXEMPT ITEM",
        "gst_applicable": False,
    })
    assert "<GSTAPPLICABLE>&#4; Not Applicable</GSTAPPLICABLE>" in xml
    assert "GSTDETAILS" not in xml
    assert "HSNDETAILS" not in xml


def test_odd_gst_rate_splits_without_truncation():
    xml = _build_stock_item_xml_block({
        "kind": "stock_item",
        "name": "FIVE PCT",
        "gst_rate": 5,
    })
    assert xml.count("<GSTRATE>2.5</GSTRATE>") == 2
    assert xml.count("<GSTRATE>5</GSTRATE>") == 1


def test_opening_qty_rendered_with_unit():
    xml = _build_stock_item_xml_block({
        "kind": "stock_item",
        "name": "OPENING",
        "unit": "KG",
        "opening_qty": 2.5,
    })
    assert "<OPENINGBALANCE>2.5 KG</OPENINGBALANCE>" in xml


def test_name_is_xml_escaped():
    xml = _build_stock_item_xml_block({
        "kind": "stock_item",
        "name": 'BOLT 1/2" <M8> & NUT',
    })
    # Attribute context must escape the quote too, or the NAME="..." attribute
    # would terminate early and corrupt the envelope.
    assert 'NAME="BOLT 1/2&quot; &lt;M8&gt; &amp; NUT"' in xml
    # Element text needs &lt;/&amp; but a literal quote is fine there.
    assert '<NAME>BOLT 1/2" &lt;M8&gt; &amp; NUT</NAME>' in xml
    assert "<M8>" not in xml


def _assert_value_error(item, message):
    try:
        _build_stock_item_xml_block(item)
    except ValueError:
        return
    raise AssertionError(message)


def test_validation_rejections():
    _assert_value_error({"kind": "stock_item"}, "expected reject: missing name")
    _assert_value_error({"kind": "stock_item", "name": "   "}, "expected reject: blank name")
    _assert_value_error(
        {"kind": "stock_item", "name": "X", "gst_rate": 17},
        "expected reject: gst_rate outside 5/12/18/28",
    )
    _assert_value_error(
        {"kind": "stock_item", "name": "X", "gst_rate": "abc"},
        "expected reject: non-numeric gst_rate",
    )
    _assert_value_error(
        {"kind": "stock_item", "name": "X", "opening_qty": -1},
        "expected reject: negative opening_qty",
    )


def test_masters_envelope_shape():
    envelope = _build_masters_import_envelope(
        [{"kind": "stock_item", "name": "A"}, {"kind": "stock_item", "name": "B"}],
        "K V ENTERPRISES",
    )
    assert "<ID>All Masters</ID>" in envelope
    assert "<TALLYREQUEST>Import</TALLYREQUEST>" in envelope
    assert "<SVCURRENTCOMPANY>K V ENTERPRISES</SVCURRENTCOMPANY>" in envelope
    assert envelope.count("<TALLYMESSAGE") == 2

    try:
        _build_masters_import_envelope([], "K V ENTERPRISES")
    except ValueError:
        pass
    else:
        raise AssertionError("expected reject: empty items list")


def test_gst_rate_not_gated_by_voucher_allowlist():
    # The voucher-type allowlist must not apply to masters: a stock-item block
    # builds fine even though 'stock_item' is not an allowed voucher type.
    assert not tally_pusher._is_supported_voucher_type("stock_item")
    _build_stock_item_xml_block({"kind": "stock_item", "name": "NO VCH TYPE"})


if __name__ == "__main__":
    failures = 0
    tests = [
        (name, fn)
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    for name, fn in tests:
        try:
            fn()
            print(f"  ok: {name}")
        except Exception as error:  # noqa: BLE001
            failures += 1
            print(f"  FAIL: {name}: {error}")
    print()
    if failures:
        print(f"FAIL: {failures} of {len(tests)} stock-item push tests failed")
        sys.exit(1)
    print(f"PASS: {len(tests)} stock-item push tests")
