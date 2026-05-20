"""
PaddleOCR-VL purchase pipeline (the `ocr=vlm` engine).

Runs in the main n8n server's environment. It calls the persistent PaddleOCR-VL
server (paddleocr_vl_server.py) over HTTP, parses the returned markdown invoice
into header + line-item rows, then reuses the shared voucher-building logic so the
output payload is shape-compatible with purchase_paddle_runner.py.
"""
from __future__ import annotations

import base64
import re
import time
from pathlib import Path
from typing import Any

import requests

from lib.env import make_env_loader
from lib.numeric import round2
from lib.text import normalize_space
from purchase.company_context import resolve_supabase_company_context
from purchase.vendor import detect_vendor
from purchase.voucher_builder import (
    build_purchase_queue_payload,
    build_voucher_payload,
    combine_ocr_items,
)

_env = make_env_loader(Path(__file__).resolve().parent / ".env")

VLM_SERVER_URL = _env("MINICPM_VLM_SERVER_URL", "http://127.0.0.1:5006/parse")
VLM_REQUEST_TIMEOUT = int(_env("MINICPM_VLM_REQUEST_TIMEOUT", "600") or "600")
DEFAULT_COMPANY_NAME = (
    _env("MINICPM_PURCHASE_COMPANY", "")
    or _env("TALLY_COMPANY", "")
    or "K V ENTERPRISES"
)
DEFAULT_MATCH_THRESHOLD = float(_env("MINICPM_PURCHASE_MIN_MATCH_SCORE", "56") or "56")

VENDOR_DISPLAY_NAMES = {
    "ADDISON": "ADDISON & COMPANY LTD",
    "ET": "EMKAY TOOLS LIMITED",
    "GNL": "GRINDWELL NORTON LIMITED",
    "RR": "R.R.TOOLS & EQUIPMENTS",
    "TOTEM": "FORBES PRECISION TOOLS AND MACHINE PARTS LTD",
    "CP": "CP GRAT-EX MANUFACTURING CO. LTD.",
    "PIDILITE": "PIDILITE INDUSTRIES LIMITED",
    "STANLEY": "STANLEY BLACK & DECKER INDIA PRIVATE LIMITED",
    "WIKUS": "WIKUS INDIA PRIVATE LIMITED",
}

STOP_ROW_MARKERS = (
    "TOTAL",
    "DISCOUNT",
    "IGST",
    "CGST",
    "SGST",
    "R.OFF",
    "ROUND OFF",
    "DECLARATION",
    "AUTHORISED SIGNATORY",
    "SUBJECT TO",
)

GSTIN_PATTERN = r"(\d{2}[A-Z]{5}\d{4}[A-Z]\d[Z][A-Z0-9])"
PAN_PATTERN = r"\b([A-Z]{5}\d{4}[A-Z])\b"


# --------------------------------------------------------------------------- #
# VLM server client
# --------------------------------------------------------------------------- #
def call_vlm_server(file_path: Path) -> dict[str, Any]:
    payload = {
        "file_base64": base64.b64encode(file_path.read_bytes()).decode("utf-8"),
        "filename": file_path.name,
    }
    try:
        response = requests.post(VLM_SERVER_URL, json=payload, timeout=VLM_REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Could not reach the PaddleOCR-VL server at {VLM_SERVER_URL}. "
            f"Start it with `python paddleocr_vl_server.py`. ({exc})"
        ) from exc
    if response.status_code != 200:
        raise RuntimeError(
            f"PaddleOCR-VL server returned {response.status_code}: {response.text[:300]}"
        )
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"PaddleOCR-VL server failed: {data.get('error', 'unknown error')}")
    return data


# --------------------------------------------------------------------------- #
# Markdown parsing helpers
# --------------------------------------------------------------------------- #
def normalize_ocr_text(value: str) -> str:
    text = str(value or "")
    text = text.replace("—", "-").replace("–", "-").replace("“", '"').replace("”", '"')
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("₹", "")
    return normalize_space(text)


def compact_letters(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", normalize_ocr_text(value).upper())


def parse_number(value: str) -> float:
    text = normalize_ocr_text(value).replace(",", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else 0.0


def numeric_string(value: float) -> float | int:
    if abs(value - round(value)) < 0.00001:
        return int(round(value))
    return round(value, 2)


def markdown_text_lines(markdown: str) -> list[str]:
    """Flatten markdown to plain text lines (table pipes become spaces)."""
    lines: list[str] = []
    for raw in markdown.splitlines():
        text = normalize_ocr_text(raw.replace("|", " "))
        if text:
            lines.append(text)
    return lines


def extract_markdown_tables(markdown: str) -> list[list[list[str]]]:
    """Return every GitHub-flavored markdown table as a list of cell rows (no separators)."""
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for raw in markdown.splitlines():
        line = raw.strip()
        if line.startswith("|") and line.count("|") >= 2:
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if all(re.fullmatch(r":?-+:?", cell or "-") for cell in cells):
                continue  # separator row
            current.append(cells)
        else:
            if len(current) >= 2:
                tables.append(current)
            current = []
    if len(current) >= 2:
        tables.append(current)
    return tables


_COLUMN_KEYWORDS = {
    "item_code": ("ITEMCODE", "CODE", "PARTNO", "PARTCODE"),
    "description": ("DESCRIPTION", "PARTICULARS", "DESCRIPTIONOFGOODS", "ITEMNAME", "GOODS"),
    "hsn": ("HSN", "HSNSAC", "SAC"),
    "qty": ("QTY", "QUANTITY"),
    "unit": ("UNIT", "UOM"),
    "rate": ("RATE", "PRICE"),
    "amount": ("TAXABLEVALUE", "TAXABLE", "AMOUNT", "VALUE"),
}


def map_table_columns(header_row: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    compact_cells = [compact_letters(cell) for cell in header_row]
    for field, keywords in _COLUMN_KEYWORDS.items():
        for index, cell in enumerate(compact_cells):
            if index in mapping.values():
                continue
            if any(keyword in cell for keyword in keywords):
                mapping[field] = index
                break
    return mapping


def select_item_table(tables: list[list[list[str]]]) -> tuple[list[list[str]], dict[str, int]]:
    """Pick the table that is the line-item table; prefer a usable column mapping."""
    best: tuple[list[list[str]], dict[str, int]] | None = None
    for table in tables:
        mapping = map_table_columns(table[0])
        has_money = ("rate" in mapping or "amount" in mapping)
        has_desc = "description" in mapping
        score = len(mapping) + len(table)
        if has_desc and has_money:
            if best is None or score > (len(best[1]) + len(best[0])):
                best = (table, mapping)
    if best is not None:
        return best
    if tables:
        widest = max(tables, key=len)
        return widest, map_table_columns(widest[0])
    return [], {}


def cell(row: list[str], mapping: dict[str, int], field: str) -> str:
    index = mapping.get(field)
    if index is None or index >= len(row):
        return ""
    return normalize_ocr_text(row[index])


def parse_item_table(markdown: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    table, mapping = select_item_table(extract_markdown_tables(markdown))
    if not table:
        warnings.append("PaddleOCR-VL did not return a recognizable invoice item table.")
        return [], [], warnings
    if "description" not in mapping:
        warnings.append("Could not identify the description column in the PaddleOCR-VL table.")

    description_rows: list[dict[str, Any]] = []
    numeric_rows: list[dict[str, Any]] = []
    for row in table[1:]:
        joined = normalize_ocr_text(" ".join(row)).upper()
        if any(marker in joined for marker in STOP_ROW_MARKERS):
            break
        description = cell(row, mapping, "description")
        item_code = cell(row, mapping, "item_code")
        quantity = parse_number(cell(row, mapping, "qty"))
        rate = parse_number(cell(row, mapping, "rate"))
        amount = parse_number(cell(row, mapping, "amount"))
        unit = cell(row, mapping, "unit")
        if not description and quantity <= 0 and rate <= 0 and amount <= 0:
            continue
        if not description:
            warnings.append(f"Skipped a table row with no description: {joined[:80]}")
            continue
        description_rows.append({"item_code": item_code, "raw_description": description})
        numeric_rows.append(
            {
                "quantity": numeric_string(quantity),
                "unit": unit,
                "rate": round(rate, 2),
                "amount": round(amount, 2),
            }
        )
    if not description_rows:
        warnings.append("PaddleOCR-VL table contained no usable purchase item rows.")
    return description_rows, numeric_rows, warnings


# --------------------------------------------------------------------------- #
# Header field extraction
# --------------------------------------------------------------------------- #
def _first_match(lines: list[str], patterns: tuple[str, ...], scope: int) -> str:
    text = "\n".join(lines[:scope])
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return normalize_ocr_text(match.group(1)).strip(":- ")
    return ""


def extract_invoice_number(lines: list[str]) -> str:
    return _first_match(
        lines,
        (
            r"INVOICE\s*NO\.?\s*[:\-]?\s*([A-Z0-9/\-]+)",
            r"SUPPLIER\s*INVOICE\s*NO\.?\s*[:\-]?\s*([A-Z0-9/\-]+)",
            r"BILL\s*NO\.?\s*[:\-]?\s*([A-Z0-9/\-]+)",
        ),
        scope=40,
    )


def extract_invoice_date(lines: list[str]) -> str:
    return _first_match(
        lines,
        (
            r"DATE\s*OF\s*INVOICE\s*[:\-]?\s*(\d{1,2}[-/.][A-Z0-9]{2,3}[-/.]\d{2,4})",
            r"INVOICE\s*DATE\s*[:\-]?\s*(\d{1,2}[-/.][A-Z0-9]{2,3}[-/.]\d{2,4})",
            r"DATE\s*[:\-]?\s*(\d{1,2}[-/.][A-Z0-9]{2,3}[-/.]\d{2,4})",
        ),
        scope=50,
    )


def extract_all_matches(lines: list[str], pattern: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    compiled = re.compile(pattern, flags=re.IGNORECASE)
    for line in lines:
        for match in compiled.findall(line):
            value = normalize_ocr_text(match).upper()
            if value and value not in seen:
                seen.add(value)
                values.append(value)
    return values


def _billed_index(lines: list[str]) -> int:
    for index, line in enumerate(lines):
        upper = line.upper()
        if "BILLED TO" in upper or "DETAILS OF RECIPIENT" in upper or "BILL TO" in upper:
            return index
    return 0


def extract_customer_name(lines: list[str]) -> str:
    start = _billed_index(lines)
    for candidate in lines[start + 1 : start + 6]:
        text = normalize_ocr_text(candidate)
        text = re.split(
            r"\b(?:LR\s*NO\.?|PO\s*NO\.?|DATE|TRANSPORTER\s*NAME)\b",
            text,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        text = normalize_ocr_text(text)
        upper = text.upper()
        if not text or any(
            marker in upper for marker in ("STATE", "GSTIN", "PAN", "CONTACT", "EMAIL", "DATE")
        ):
            continue
        if re.search(r"[A-Z]", upper):
            return text
    return ""


def extract_tax_entries(lines: list[str]) -> list[dict[str, Any]]:
    found: dict[str, float] = {}
    for line in lines:
        text = normalize_ocr_text(line).upper()
        for ledger_name in ("IGST", "CGST", "SGST"):
            if ledger_name not in text:
                continue
            numbers = re.findall(r"-?\d[\d,]*(?:\.\d+)?", text)
            amount = abs(parse_number(numbers[-1])) if numbers else 0.0
            if amount > 0:
                found[ledger_name] = round(amount, 2)
    return [{"ledger_name": name, "amount": amount} for name, amount in found.items()]


def extract_round_off(lines: list[str]) -> float:
    text = "\n".join(lines)
    match = re.search(
        r"(?:R\.?\s*OFF|ROUND\s*OFF)\s+(-?\d[\d,]*(?:\.\d+)?)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return round(parse_number(match.group(1)), 2) if match else 0.0


def extract_invoice_total(lines: list[str]) -> float:
    text = "\n".join(lines)
    match = re.search(
        r"TOTAL\s*INVOICE\s*VALUE(?:\s*\(.*?\))?\s*[:\-]?\s+(-?\d[\d,]*(?:\.\d+)?)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return round(abs(parse_number(match.group(1))), 2) if match else 0.0


def extract_discount(lines: list[str]) -> tuple[float, float]:
    text = "\n".join(lines)
    match = re.search(
        r"DISCOUNT\s+(\d+(?:\.\d+)?)\s*(?:%|OF)?\s+(-?\d[\d,]*(?:\.\d+)?)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        return round(parse_number(match.group(1)), 2), round(abs(parse_number(match.group(2))), 2)
    return 0.0, 0.0


def infer_vendor_display_name(lines: list[str]) -> str:
    top_text = "\n".join(lines[:18])
    try:
        vendor = detect_vendor(top_text)
    except ValueError:
        return normalize_ocr_text(lines[0]) if lines else ""
    return VENDOR_DISPLAY_NAMES.get(vendor, normalize_ocr_text(lines[0]) if lines else "")


def build_header_data(markdown: str) -> dict[str, Any]:
    lines = markdown_text_lines(markdown)
    supplier_gstins = extract_all_matches(lines[:18], GSTIN_PATTERN)
    customer_gstins = extract_all_matches(
        lines[_billed_index(lines) : _billed_index(lines) + 18], GSTIN_PATTERN
    )
    supplier_pans = extract_all_matches(lines[:18], PAN_PATTERN)
    customer_pans = extract_all_matches(
        lines[_billed_index(lines) : _billed_index(lines) + 18], PAN_PATTERN
    )
    discount_percent, discount_amount = extract_discount(lines)
    return {
        "vendor_name": infer_vendor_display_name(lines),
        "invoice_number": extract_invoice_number(lines),
        "invoice_date": extract_invoice_date(lines),
        "customer_name": extract_customer_name(lines),
        "supplier_gstin": supplier_gstins[0] if supplier_gstins else "",
        "customer_gstin": customer_gstins[0] if customer_gstins else "",
        "supplier_pan": supplier_pans[0] if supplier_pans else "",
        "customer_pan": customer_pans[0] if customer_pans else "",
        "discount_percent": discount_percent,
        "discount_amount": discount_amount,
        "invoice_total": extract_invoice_total(lines),
        "tax_entries": extract_tax_entries(lines),
        "round_off": extract_round_off(lines),
    }


def parse_vlm_invoice(
    markdown: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    header_data = build_header_data(markdown)
    description_rows, numeric_rows, warnings = parse_item_table(markdown)
    if not header_data["invoice_total"]:
        item_total = sum(row.get("amount", 0) or 0 for row in numeric_rows)
        tax_total = sum(entry.get("amount", 0) or 0 for entry in header_data["tax_entries"])
        header_data["invoice_total"] = round(item_total + tax_total, 2)
    return header_data, description_rows, numeric_rows, warnings


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def run_purchase_vl_pipeline(
    image_path: Path,
    *,
    company_name: str = DEFAULT_COMPANY_NAME,
    min_match_score: float = DEFAULT_MATCH_THRESHOLD,
) -> dict[str, Any]:
    started_at = time.time()
    vlm_result = call_vlm_server(Path(image_path))
    markdown = vlm_result.get("markdown", "") or ""
    header_data, description_rows, numeric_rows, warnings = parse_vlm_invoice(markdown)
    elapsed = round(time.time() - started_at, 2)

    warnings_out = list(warnings)
    vendor = ""
    try:
        vendor = detect_vendor(header_data.get("vendor_name", ""))
    except ValueError as exc:
        warnings_out.append(str(exc))

    raw_items = []
    if vendor:
        raw_items, item_warnings = combine_ocr_items(vendor, description_rows, numeric_rows)
        warnings_out.extend(item_warnings)

    ocr_block = {
        "header": header_data,
        "description_rows": description_rows,
        "numeric_rows": numeric_rows,
        "warnings": warnings_out,
        "raw_markdown": markdown,
        "page_count": vlm_result.get("page_count", 1),
    }
    base_payload: dict[str, Any] = {
        "ok": True,
        "status": "success",
        "mode": "purchase",
        "ocr_engine": "vlm",
        "image_path": str(image_path),
        "company_name": company_name,
        "master_source": "supabase",
        "vendor": vendor,
        "summary": {
            "row_count": len(raw_items) or max(len(description_rows), len(numeric_rows)),
            "weak_match_count": 0,
            "inference_seconds": elapsed,
            "push_mode": "none",
            "master_source": "supabase",
            "ocr_engine": "vlm",
            "matching_ready": bool(raw_items),
        },
        "ocr": ocr_block,
        "raw_items": [
            {
                "item_code": item.item_code,
                "raw_description": item.raw_description,
                "quantity": float(item.quantity),
                "rate": float(round2(item.rate)),
                "amount": float(round2(item.amount)),
                "unit": item.unit,
            }
            for item in raw_items
        ],
        "matched_items": [],
        "weak_matches": [],
        "party_name": "",
        "voucher_payload": None,
        "push_queue_payload": None,
        "tally_push": None,
        "n8n": {
            "type": "purchase",
            "ocr_engine": "vlm",
            "company_name": company_name,
            "vendor": vendor,
            "party_name": "",
            "voucher_payload": None,
            "push_queue_payload": None,
            "matched_items": [],
            "weak_matches": [],
            "ocr": {"header": header_data, "warnings": warnings_out},
        },
    }

    if not vendor or not raw_items:
        if not raw_items:
            warnings_out.append("Matching skipped: PaddleOCR-VL did not yield purchase item rows.")
        base_payload["status"] = "partial_success"
        return base_payload

    try:
        context = resolve_supabase_company_context(company_name)
        build_result = build_voucher_payload(
            company_name,
            vendor,
            header_data,
            raw_items,
            context["stock_items"],
            context["ledgers"],
            min_match_score,
        )
    except Exception as exc:  # noqa: BLE001 - surface as a soft failure, like the paddle runner
        warnings_out.append(f"Matching skipped: {exc}")
        base_payload["status"] = "partial_success"
        return base_payload

    queue_payload = build_purchase_queue_payload(company_name, build_result["voucher_payload"])
    return {
        **base_payload,
        "status": "success",
        "master_source": context.get("source", "supabase"),
        "matched_items": build_result["matched_items"],
        "weak_matches": build_result["weak_matches"],
        "party_name": build_result["party_name"],
        "voucher_payload": build_result["voucher_payload"],
        "push_queue_payload": queue_payload,
        "summary": {
            **base_payload["summary"],
            "weak_match_count": len(build_result["weak_matches"]),
            "master_source": context.get("source", "supabase"),
            "matching_ready": True,
        },
        "n8n": {
            "type": "purchase",
            "ocr_engine": "vlm",
            "company_name": company_name,
            "vendor": vendor,
            "party_name": build_result["party_name"],
            "voucher_payload": build_result["voucher_payload"],
            "push_queue_payload": queue_payload,
            "matched_items": build_result["matched_items"],
            "weak_matches": build_result["weak_matches"],
            "ocr": {"header": header_data, "warnings": warnings_out},
        },
    }
