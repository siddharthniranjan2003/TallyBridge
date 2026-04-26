import html
import os
import re
from decimal import Decimal, InvalidOperation

from tally_client import TALLY_COMPANY, _post, _xml_escape

PUSH_AMOUNT_TOLERANCE = Decimal("0.05")
DEFAULT_ALLOWED_PUSH_TYPES = [
    "Sales",
    "Purchase",
    "GST SALE",
    "GST PURCHASE",
]


def _normalize_voucher_type_key(value: str) -> str:
    return (value or "").strip().upper()


def _get_allowed_push_type_keys() -> set[str]:
    raw = (os.environ.get("TB_PUSH_ALLOWED_TYPES", "") or "").strip()
    source = [
        entry.strip()
        for entry in raw.split(",")
        if entry.strip()
    ] if raw else DEFAULT_ALLOWED_PUSH_TYPES
    return {
        _normalize_voucher_type_key(entry)
        for entry in source
        if entry.strip()
    }


def _is_supported_voucher_type(value: str) -> bool:
    key = _normalize_voucher_type_key(value)
    return bool(key) and key in _get_allowed_push_type_keys()


def _voucher_kind(voucher_type: str) -> str:
    normalized = _normalize_voucher_type_key(voucher_type)
    if "PURCHASE" in normalized:
        return "purchase"
    if "SALE" in normalized:
        return "sales"
    raise ValueError(f"Unsupported voucher type for push: {voucher_type}")


def _decimal_from_value(value, label: str) -> Decimal:
    try:
        text = str(value).strip()
        if not text:
            return Decimal("0")
        return Decimal(text)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc


def _format_amount(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _format_quantity(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _signed_tally_amount(amount, is_deemed_positive: bool) -> Decimal:
    absolute_amount = abs(_decimal_from_value(amount, "amount"))
    return -absolute_amount if is_deemed_positive else absolute_amount


def _signed_inventory_amount(amount, voucher_kind: str) -> Decimal:
    absolute_amount = abs(_decimal_from_value(amount, "item amount"))
    return -absolute_amount if voucher_kind == "purchase" else absolute_amount


def _iso_to_tally_date(iso_value: str) -> str:
    raw = (iso_value or "").strip()
    if re.fullmatch(r"\d{8}", raw):
        return raw
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw.replace("-", "")
    raise ValueError("voucher date must be in YYYY-MM-DD or YYYYMMDD format")


def _is_tax_like_ledger(name: str) -> bool:
    normalized = (name or "").strip().upper()
    if "SALE" in normalized or "PURCHASE" in normalized:
        return False
    return any(
        marker in normalized
        for marker in ("CGST", "SGST", "IGST", "GST", "VAT", "TAX", "CESS")
    )


def _looks_like_primary_inventory_ledger(name: str, voucher_kind: str) -> bool:
    normalized = (name or "").strip().upper()
    if voucher_kind == "sales":
        return "SALE" in normalized
    return "PURCHASE" in normalized


def _normalize_ledger_entries(voucher: dict, party_name: str) -> list[dict]:
    ledger_entries = voucher.get("ledger_entries")
    if not isinstance(ledger_entries, list) or not ledger_entries:
        raise ValueError("voucher ledger_entries must be a non-empty array")

    normalized_entries: list[dict] = []
    normalized_party_name = party_name.casefold()
    for index, entry in enumerate(ledger_entries):
        if not isinstance(entry, dict):
            raise ValueError(f"ledger entry #{index + 1} must be an object")

        ledger_name = str(entry.get("ledger_name") or "").strip()
        if not ledger_name:
            raise ValueError(f"ledger entry #{index + 1} is missing ledger_name")

        is_deemed_positive = bool(entry.get("is_deemed_positive"))
        signed_amount = _signed_tally_amount(entry.get("amount", 0), is_deemed_positive)
        is_party_ledger = bool(entry.get("is_party_ledger")) or ledger_name.casefold() == normalized_party_name

        normalized_entries.append({
            "ledger_name": ledger_name,
            "signed_amount": signed_amount,
            "is_deemed_positive": is_deemed_positive,
            "is_party_ledger": is_party_ledger,
        })

    return normalized_entries


def _normalize_items(voucher: dict, voucher_kind: str) -> list[dict]:
    items = voucher.get("items")
    if items is None:
        return []
    if not isinstance(items, list):
        raise ValueError("voucher items must be an array when provided")

    normalized_items: list[dict] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"item #{index + 1} must be an object")

        stock_item_name = str(item.get("stock_item_name") or "").strip()
        if not stock_item_name:
            raise ValueError(f"item #{index + 1} is missing stock_item_name")

        unit = str(item.get("unit") or "").strip()
        if not unit:
            raise ValueError(f"item #{index + 1} is missing unit")

        quantity = _decimal_from_value(item.get("quantity", 0), f"item #{index + 1} quantity")
        if quantity <= 0:
            raise ValueError(f"item #{index + 1} quantity must be greater than zero")

        rate = _decimal_from_value(item.get("rate", 0), f"item #{index + 1} rate")
        raw_amount = item.get("amount")
        computed_amount = quantity * rate
        signed_amount = _signed_inventory_amount(
            raw_amount if raw_amount not in (None, "") else computed_amount,
            voucher_kind,
        )

        normalized_items.append({
            "stock_item_name": stock_item_name,
            "quantity": quantity,
            "unit": unit,
            "rate": rate,
            "signed_amount": signed_amount,
            "godown_name": str(item.get("godown_name") or "").strip(),
            "batch_name": str(item.get("batch_name") or "").strip(),
            "destination_godown_name": str(item.get("destination_godown_name") or "").strip(),
        })

    return normalized_items


def _find_party_entry(ledger_entries: list[dict], party_name: str) -> dict:
    normalized_party_name = party_name.casefold()
    matches = [
        entry for entry in ledger_entries
        if entry["is_party_ledger"] or entry["ledger_name"].casefold() == normalized_party_name
    ]
    if not matches:
        raise ValueError(
            "voucher ledger_entries must include the party ledger entry "
            f"for '{party_name}'"
        )

    exact_matches = [
        entry for entry in matches
        if entry["ledger_name"].casefold() == normalized_party_name
    ]
    return exact_matches[0] if exact_matches else matches[0]


def _find_inventory_ledger_entry(
    voucher: dict,
    ledger_entries: list[dict],
    party_entry: dict,
    item_total: Decimal,
    voucher_kind: str,
) -> dict:
    explicit_name = str(
        voucher.get("inventory_ledger_name")
        or voucher.get("stock_ledger_name")
        or ""
    ).strip()
    candidates = [
        entry for entry in ledger_entries
        if entry is not party_entry and not entry["is_party_ledger"] and not _is_tax_like_ledger(entry["ledger_name"])
    ]

    if explicit_name:
        explicit_matches = [
            entry for entry in candidates
            if entry["ledger_name"].casefold() == explicit_name.casefold()
        ]
        if not explicit_matches:
            raise ValueError(
                f"inventory_ledger_name '{explicit_name}' was not found in ledger_entries"
            )
        return explicit_matches[0]

    if not candidates:
        raise ValueError(
            "inventory Sales/Purchase voucher needs a non-tax Sales/Purchase ledger entry "
            "in ledger_entries"
        )

    sorted_candidates = sorted(
        candidates,
        key=lambda entry: (
            0 if _looks_like_primary_inventory_ledger(entry["ledger_name"], voucher_kind) else 1,
            0 if abs(abs(entry["signed_amount"]) - item_total) <= PUSH_AMOUNT_TOLERANCE else 1,
            abs(abs(entry["signed_amount"]) - item_total),
            -abs(entry["signed_amount"]),
        ),
    )
    selected = sorted_candidates[0]
    if abs(abs(selected["signed_amount"]) - item_total) > PUSH_AMOUNT_TOLERANCE:
        raise ValueError(
            "inventory ledger amount does not match the total item amount for this voucher"
        )
    return selected


def _build_party_ledger_entry_xml(entry: dict) -> str:
    is_deemed = "Yes" if entry["is_deemed_positive"] else "No"
    return (
        "<LEDGERENTRIES.LIST>"
        f"<LEDGERNAME>{_xml_escape(entry['ledger_name'])}</LEDGERNAME>"
        f"<ISDEEMEDPOSITIVE>{is_deemed}</ISDEEMEDPOSITIVE>"
        f"<ISLASTDEEMEDPOSITIVE>{is_deemed}</ISLASTDEEMEDPOSITIVE>"
        f"<AMOUNT>{_format_amount(entry['signed_amount'])}</AMOUNT>"
        # PUSH PHASE 1: Keep party bill allocations empty unless we have
        # explicit bill-wise metadata. A partial BILLALLOCATIONS block can
        # trigger silent import exceptions in GST invoice mode.
        "<BILLALLOCATIONS.LIST></BILLALLOCATIONS.LIST>"
        "<COSTTRACKALLOCATIONS.LIST></COSTTRACKALLOCATIONS.LIST>"
        "</LEDGERENTRIES.LIST>"
    )


def _build_ledger_entry_xml(entry: dict) -> str:
    is_deemed = "Yes" if entry["is_deemed_positive"] else "No"
    return (
        "<LEDGERENTRIES.LIST>"
        f"<LEDGERNAME>{_xml_escape(entry['ledger_name'])}</LEDGERNAME>"
        f"<ISDEEMEDPOSITIVE>{is_deemed}</ISDEEMEDPOSITIVE>"
        f"<ISLASTDEEMEDPOSITIVE>{is_deemed}</ISLASTDEEMEDPOSITIVE>"
        f"<AMOUNT>{_format_amount(entry['signed_amount'])}</AMOUNT>"
        "<BILLALLOCATIONS.LIST></BILLALLOCATIONS.LIST>"
        "<COSTTRACKALLOCATIONS.LIST></COSTTRACKALLOCATIONS.LIST>"
        "</LEDGERENTRIES.LIST>"
    )


def _build_batch_allocations_xml(item: dict) -> str:
    godown_name = item.get("godown_name") or ""
    batch_name = item.get("batch_name") or "Primary Batch"
    destination_godown_name = item.get("destination_godown_name") or ""

    batch_parts = [
        "<BATCHALLOCATIONS.LIST>",
    ]
    if godown_name:
        batch_parts.append(f"<GODOWNNAME>{_xml_escape(godown_name)}</GODOWNNAME>")
    if batch_name:
        batch_parts.append(f"<BATCHNAME>{_xml_escape(batch_name)}</BATCHNAME>")
    if destination_godown_name:
        batch_parts.append(
            f"<DESTINATIONGODOWNNAME>{_xml_escape(destination_godown_name)}</DESTINATIONGODOWNNAME>"
        )
    batch_parts.extend([
        "<INDENTNO>&#4; Not Applicable</INDENTNO>",
        "<ORDERNO>&#4; Not Applicable</ORDERNO>",
        "<TRACKINGNUMBER>&#4; Not Applicable</TRACKINGNUMBER>",
        # PUSH PHASE 1: Match the shape of exported GST SALE vouchers more
        # closely so Tally gets the same inventory allocation structure.
        "<ADDLAMOUNT></ADDLAMOUNT>",
        "<BATCHDISCOUNT>0</BATCHDISCOUNT>",
        f"<AMOUNT>{_format_amount(item['signed_amount'])}</AMOUNT>",
        f"<ACTUALQTY>{_format_quantity(item['quantity'])} {_xml_escape(item['unit'])}</ACTUALQTY>",
        f"<BILLEDQTY>{_format_quantity(item['quantity'])} {_xml_escape(item['unit'])}</BILLEDQTY>",
        f"<BATCHRATE>{_format_amount(item['rate'])}/{_xml_escape(item['unit'])}</BATCHRATE>",
        "</BATCHALLOCATIONS.LIST>",
    ])
    return "".join(batch_parts)


def _build_inventory_entry_xml(item: dict, inventory_ledger_entry: dict, voucher_kind: str) -> str:
    inventory_is_deemed_positive = "Yes" if voucher_kind == "purchase" else "No"
    batch_allocations = _build_batch_allocations_xml(item)
    return (
        "<ALLINVENTORYENTRIES.LIST>"
        f"<STOCKITEMNAME>{_xml_escape(item['stock_item_name'])}</STOCKITEMNAME>"
        "<ADDLAMOUNT></ADDLAMOUNT>"
        f"<ISDEEMEDPOSITIVE>{inventory_is_deemed_positive}</ISDEEMEDPOSITIVE>"
        f"<ISLASTDEEMEDPOSITIVE>{inventory_is_deemed_positive}</ISLASTDEEMEDPOSITIVE>"
        f"<RATE>{_format_amount(item['rate'])}/{_xml_escape(item['unit'])}</RATE>"
        "<DISCOUNT>0</DISCOUNT>"
        f"<AMOUNT>{_format_amount(item['signed_amount'])}</AMOUNT>"
        f"<ACTUALQTY>{_format_quantity(item['quantity'])} {_xml_escape(item['unit'])}</ACTUALQTY>"
        f"<BILLEDQTY>{_format_quantity(item['quantity'])} {_xml_escape(item['unit'])}</BILLEDQTY>"
        f"{batch_allocations}"
        "<ACCOUNTINGALLOCATIONS.LIST>"
        f"<LEDGERNAME>{_xml_escape(inventory_ledger_entry['ledger_name'])}</LEDGERNAME>"
        f"<ISDEEMEDPOSITIVE>{inventory_is_deemed_positive}</ISDEEMEDPOSITIVE>"
        f"<ISLASTDEEMEDPOSITIVE>{inventory_is_deemed_positive}</ISLASTDEEMEDPOSITIVE>"
        f"<AMOUNT>{_format_amount(item['signed_amount'])}</AMOUNT>"
        "<BILLALLOCATIONS.LIST></BILLALLOCATIONS.LIST>"
        "<TAXOBJECTALLOCATIONS.LIST></TAXOBJECTALLOCATIONS.LIST>"
        "<COSTTRACKALLOCATIONS.LIST></COSTTRACKALLOCATIONS.LIST>"
        "</ACCOUNTINGALLOCATIONS.LIST>"
        "</ALLINVENTORYENTRIES.LIST>"
    )


def _build_voucher_xml_block(voucher: dict) -> str:
    voucher_type = str(voucher.get("voucher_type") or "").strip()
    if not _is_supported_voucher_type(voucher_type):
        raise ValueError(f"voucher_type '{voucher_type}' is not enabled for push")

    voucher_kind = _voucher_kind(voucher_type)
    party_name = str(voucher.get("party_name") or "").strip()
    if not party_name:
        raise ValueError("voucher party_name is required")

    voucher_date = _iso_to_tally_date(str(voucher.get("date") or ""))
    ledger_entries = _normalize_ledger_entries(voucher, party_name)
    party_entry = _find_party_entry(ledger_entries, party_name)
    items = _normalize_items(voucher, voucher_kind)
    item_total = sum((abs(item["signed_amount"]) for item in items), Decimal("0"))

    inventory_ledger_entry = None
    rendered_ledger_entries = []
    if items:
        inventory_ledger_entry = _find_inventory_ledger_entry(
            voucher,
            ledger_entries,
            party_entry,
            item_total,
            voucher_kind,
        )
        rendered_ledger_entries = [
            entry for entry in ledger_entries
            if entry is not party_entry and entry is not inventory_ledger_entry
        ]
    else:
        rendered_ledger_entries = [
            entry for entry in ledger_entries
            if entry is not party_entry
        ]

    voucher_number = str(voucher.get("voucher_number") or "").strip()
    narration = str(voucher.get("narration") or "").strip()
    reference = str(voucher.get("reference") or "").strip()
    invoice_mode = bool(items)
    voucher_is_deemed_positive = "Yes" if party_entry["is_deemed_positive"] else "No"
    voucher_amount = _format_amount(party_entry["signed_amount"])
    voucher_attributes = f' VCHTYPE="{_xml_escape(voucher_type)}" ACTION="Create"'
    if invoice_mode:
        voucher_attributes += ' OBJVIEW="Invoice Voucher View"'

    parts = [
        f"<VOUCHER{voucher_attributes}>",
        f"<DATE>{voucher_date}</DATE>",
        f"<VOUCHERTYPENAME>{_xml_escape(voucher_type)}</VOUCHERTYPENAME>",
    ]
    if voucher_number:
        parts.append(f"<VOUCHERNUMBER>{_xml_escape(voucher_number)}</VOUCHERNUMBER>")
    if reference:
        parts.append(f"<REFERENCE>{_xml_escape(reference)}</REFERENCE>")
    if narration:
        parts.append(f"<NARRATION>{_xml_escape(narration)}</NARRATION>")
    # PUSH PHASE 1: Mirror the metadata that Tally exports for GST invoice
    # vouchers so import shape stays closer to a real live voucher.
    parts.extend([
        "<REQUESTORRULE/>",
        "<SERIALMASTER></SERIALMASTER>",
        "<ARESERIALMASTER></ARESERIALMASTER>",
    ])
    parts.append(f"<PARTYLEDGERNAME>{_xml_escape(party_name)}</PARTYLEDGERNAME>")
    parts.append(f"<ISDEEMEDPOSITIVE>{voucher_is_deemed_positive}</ISDEEMEDPOSITIVE>")
    parts.append("<ISOPTIONAL>No</ISOPTIONAL>")
    parts.append(f"<EFFECTIVEDATE>{voucher_date}</EFFECTIVEDATE>")
    parts.append("<ISCANCELLED>No</ISCANCELLED>")
    parts.append(f"<AMOUNT>{voucher_amount}</AMOUNT>")
    if invoice_mode:
        parts.extend([
            "<PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>",
            "<ISINVOICE>Yes</ISINVOICE>",
            "<OBJVIEW>Invoice Voucher View</OBJVIEW>",
        ])
    else:
        parts.extend([
            "<PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>",
            "<ISINVOICE>No</ISINVOICE>",
        ])

    parts.append(_build_party_ledger_entry_xml(party_entry))
    for entry in rendered_ledger_entries:
        parts.append(_build_ledger_entry_xml(entry))

    if items:
        if inventory_ledger_entry is None:
            raise ValueError("inventory ledger entry could not be resolved")
        for item in items:
            parts.append(
                _build_inventory_entry_xml(item, inventory_ledger_entry, voucher_kind)
            )

    parts.append("</VOUCHER>")
    return "".join(parts)


def _build_import_envelope(vouchers: list[dict], company: str) -> str:
    if not vouchers:
        raise ValueError("push_vouchers received no vouchers")

    tally_messages = "".join(
        f"<TALLYMESSAGE>{_build_voucher_xml_block(voucher)}</TALLYMESSAGE>"
        for voucher in vouchers
    )
    static_variables = (
        f"<STATICVARIABLES><SVCURRENTCOMPANY>{_xml_escape(company)}</SVCURRENTCOMPANY></STATICVARIABLES>"
        if (company or "").strip()
        else ""
    )
    return (
        "<ENVELOPE>"
        "<HEADER>"
        "<VERSION>1</VERSION>"
        "<TALLYREQUEST>Import</TALLYREQUEST>"
        "<TYPE>Data</TYPE>"
        "<ID>Vouchers</ID>"
        "</HEADER>"
        "<BODY>"
        "<DESC>"
        f"{static_variables}"
        "</DESC>"
        "<DATA>"
        f"{tally_messages}"
        "</DATA>"
        "</BODY>"
        "</ENVELOPE>"
    )


def parse_push_response(xml_text: str) -> dict:
    created = 0
    altered = 0
    errors = 0
    exceptions = 0

    for match in re.findall(r"<CREATED>\s*([0-9]+)\s*</CREATED>", xml_text, re.IGNORECASE):
        created += int(match)
    for match in re.findall(r"<ALTERED>\s*([0-9]+)\s*</ALTERED>", xml_text, re.IGNORECASE):
        altered += int(match)
    for match in re.findall(r"<ERRORS>\s*([0-9]+)\s*</ERRORS>", xml_text, re.IGNORECASE):
        errors += int(match)
    for match in re.findall(r"<EXCEPTIONS>\s*([0-9]+)\s*</EXCEPTIONS>", xml_text, re.IGNORECASE):
        exceptions += int(match)

    line_errors = [
        html.unescape(re.sub(r"<[^>]+>", " ", line).strip())
        for line in re.findall(r"<LINEERROR>\s*(.*?)\s*</LINEERROR>", xml_text, re.IGNORECASE | re.DOTALL)
        if line.strip()
    ]

    if line_errors and errors == 0:
        errors = len(line_errors)
    if exceptions and errors == 0:
        errors = exceptions
    if exceptions and not line_errors:
        line_errors.append(
            "Tally reported an import exception without a LINEERROR message. "
            "Inspect the raw response for more detail."
        )

    return {
        "created": created,
        "altered": altered,
        "errors": errors,
        "exceptions": exceptions,
        "line_errors": line_errors,
        "raw": xml_text,
    }


def push_vouchers(vouchers: list[dict], company: str = TALLY_COMPANY) -> dict:
    envelope = _build_import_envelope(vouchers, company)
    response_xml = _post(envelope)
    return parse_push_response(response_xml)
