"""Always-on DocStrange/Nanonets invoice extraction server.

Run this from WSL with the DocStrange virtualenv active. The model is loaded
once and reused for every request, which avoids the slow startup cost per file.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import traceback
from io import StringIO
from pathlib import Path
from typing import Any


DEFAULT_MODEL_REPO = "nanonets/Nanonets-OCR2-3B"
DEFAULT_DOCSTRANGE_REPO = "/mnt/d/Desktop/docstrange"
INVOICE_SCHEMA: dict[str, Any] = {
    "invoice_type": "string",
    "vendor_name": "string",
    "vendor_gstin": "string",
    "invoice_number": "string",
    "invoice_date": "string",
    "buyer_name": "string",
    "buyer_gstin": "string",
    "ship_to_name": "string",
    "ship_to_gstin": "string",
    "place_of_supply": "string",
    "line_items": [
        {
            "sr_no": "number",
            "item_code": "string",
            "description": "string",
            "hsn_sac": "string",
            "quantity": "number",
            "unit": "string",
            "rate": "number",
            "taxable_value": "number",
        }
    ],
    "taxes": [
        {
            "name": "string",
            "rate_percent": "number",
            "amount": "number",
        }
    ],
    "discount": {
        "rate_percent": "number",
        "amount": "number",
    },
    "round_off": "number",
    "total_taxable_value": "number",
    "total_tax_value": "number",
    "total_invoice_value": "number",
}

_EXTRACTOR = None
_CACHE: dict[str, dict[str, Any]] = {}


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = str(value)
    text = text.replace("&amp;", "&")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("**", "")
    text = _clean_mojibake(text)
    text = text.replace('"IS"', "IS").replace('"No"', "No").replace('"No":', "No:")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"\n\s+", "\n", text)
    return text.strip()


def _clean_mojibake(text: str) -> str:
    replacements = {
        "Â°": "°",
        "â˜": "☐",
        "â˜‘": "☑",
        "â€“": "-",
        "â€”": "-",
        "â€˜": "'",
        "â€™": "'",
        "â€œ": '"',
        "â€�": '"',
        "Â ": " ",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    return text


def _deep_clean(value: Any) -> Any:
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, list):
        return [_deep_clean(item) for item in value]
    if isinstance(value, dict):
        return {key: _deep_clean(item) for key, item in value.items()}
    return value


def _lines(markdown: str) -> list[str]:
    return [_clean_text(line) for line in markdown.splitlines() if _clean_text(line)]


def _number(value: str | None) -> float | int | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else float(value)
    cleaned = re.sub(r"[^\d.\-]", "", value)
    if not cleaned:
        return None
    number = float(cleaned)
    return int(number) if number.is_integer() else number


def _first_match(pattern: str, text: str, flags: int = re.I) -> str | None:
    match = re.search(pattern, text, flags)
    return _clean_text(match.group(1)) if match else None


def _extract_after_label(label: str, lines: list[str]) -> str | None:
    pattern = re.compile(rf"^{re.escape(label)}\s*:?\s*(.+)$", re.I)
    for line in lines:
        match = pattern.search(line)
        if match:
            value = _clean_text(match.group(1)).strip(": _")
            return value or None
    return None


def _line_index(lines: list[str], pattern: str) -> int | None:
    regex = re.compile(pattern, re.I)
    for index, line in enumerate(lines):
        if regex.search(line):
            return index
    return None


def _extract_table_rows(markdown: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for row_html in re.findall(r"<tr\b[^>]*>(.*?)</tr>", markdown, flags=re.I | re.S):
        cells = re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row_html, flags=re.I | re.S)
        if cells:
            rows.append([_clean_text(cell) for cell in cells])
    return rows


def _extract_html_tables(markdown: str) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    for table_index, table_html in enumerate(re.findall(r"<table\b[^>]*>(.*?)</table>", markdown, flags=re.I | re.S), start=1):
        parsed_rows: list[dict[str, Any]] = []
        headers: list[str] = []
        raw_rows: list[list[str]] = []

        for row_html in re.findall(r"<tr\b[^>]*>(.*?)</tr>", table_html, flags=re.I | re.S):
            header_cells = re.findall(r"<th\b[^>]*>(.*?)</th>", row_html, flags=re.I | re.S)
            data_cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row_html, flags=re.I | re.S)
            if header_cells and not headers:
                headers = [_clean_text(cell) for cell in header_cells]
                raw_rows.append(headers)
                continue
            cells = [_clean_text(cell) for cell in data_cells]
            if cells:
                raw_rows.append(cells)
                if headers:
                    parsed_rows.append(_row_to_dict(headers, cells))

        tables.append(
            {
                "table_index": table_index,
                "headers": headers,
                "rows": parsed_rows,
                "raw_rows": raw_rows,
            }
        )
    return tables


def _extract_markdown_pipe_tables(markdown: str) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        next_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
        if _is_pipe_row(line) and re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?", next_line):
            headers = [_clean_text(cell) for cell in _split_pipe_row(line)]
            raw_rows = [headers]
            rows = []
            index += 2
            while index < len(lines) and _is_pipe_row(lines[index].strip()):
                cells = [_clean_text(cell) for cell in _split_pipe_row(lines[index].strip())]
                raw_rows.append(cells)
                rows.append(_row_to_dict(headers, cells))
                index += 1
            tables.append(
                {
                    "table_index": len(tables) + 1,
                    "headers": headers,
                    "rows": rows,
                    "raw_rows": raw_rows,
                }
            )
            continue
        index += 1
    return tables


def _is_pipe_row(line: str) -> bool:
    return line.startswith("|") and line.endswith("|") and line.count("|") >= 2


def _split_pipe_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _row_to_dict(headers: list[str], cells: list[str]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for index, header in enumerate(headers):
        key = _normalize_json_key(header) or f"column_{index + 1}"
        value = cells[index] if index < len(cells) else None
        row[key] = _typed_value(value)
    if len(cells) > len(headers):
        row["extra_columns"] = cells[len(headers) :]
    return row


def _normalize_json_key(value: str) -> str:
    text = _clean_text(value).lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text


def _typed_value(value: str | None) -> Any:
    if value is None:
        return None
    text = _clean_text(value)
    if text == "":
        return None
    numeric = re.sub(r"[, ]+", "", text)
    if re.fullmatch(r"-?\d+(?:\.\d+)?", numeric):
        number = float(numeric)
        return int(number) if number.is_integer() else number
    return text


def _strip_tables(markdown: str) -> str:
    text = re.sub(r"<table\b[^>]*>.*?</table>", "\n", markdown, flags=re.I | re.S)
    lines = text.splitlines()
    kept: list[str] = []
    skip_pipe_table = False
    for line in lines:
        stripped = line.strip()
        if _is_pipe_row(stripped):
            skip_pipe_table = True
            continue
        if skip_pipe_table and not stripped:
            skip_pipe_table = False
            continue
        if not skip_pipe_table:
            kept.append(line)
    return "\n".join(kept)


def _extract_key_values(text: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for line in _lines(text):
        clean_line = _clean_text(line.replace("**", ""))
        if ":" not in clean_line:
            continue
        label, value = clean_line.split(":", 1)
        key = _normalize_json_key(label)
        if not key:
            continue
        parsed_value = _typed_value(value.strip())
        if parsed_value is None:
            continue
        if key in fields:
            existing = fields[key]
            if not isinstance(existing, list):
                fields[key] = [existing]
            fields[key].append(parsed_value)
        else:
            fields[key] = parsed_value
    return fields


def docstrange_markdown_to_json(markdown: str, path: str) -> dict[str, Any]:
    html_tables = _extract_html_tables(markdown)
    pipe_tables = _extract_markdown_pipe_tables(markdown)
    tables = html_tables or pipe_tables
    text_without_tables = _strip_tables(markdown)
    text_blocks = [_clean_text(line.replace("**", "")) for line in text_without_tables.splitlines() if _clean_text(line)]
    return {
        "document": {
            "text_blocks": text_blocks,
            "fields": _extract_key_values(text_without_tables),
            "tables": tables,
        },
        "format": "docstrange_markdown_structured_json",
        "gpu_processing_info": {
            "ocr_provider": "nanonets",
            "processing_mode": "gpu",
            "file_path": path,
            "json_extraction_method": "markdown_table_parser",
        },
    }


def _parse_item_description(cell: str) -> tuple[str | None, str | None, str]:
    text = _clean_text(cell).replace("\n", " ")
    match = re.match(r"^(\d{6,8})\s+([A-Z0-9]{6,12})\s+([A-Z0-9]{4})\s+(.+)$", text, re.I)
    if match:
        return match.group(1), f"{match.group(2)} {match.group(3)}", _clean_text(match.group(4))
    match = re.match(r"^(\d{6,8})\s+(.+)$", text, re.I)
    if match:
        return match.group(1), None, _clean_text(match.group(2))
    return None, None, text


def _parse_line_items(markdown: str) -> tuple[list[dict[str, Any]], float | int | None]:
    items: list[dict[str, Any]] = []
    total_taxable: float | int | None = None

    for cells in _extract_table_rows(markdown):
        if len(cells) >= 5 and re.fullmatch(r"\d+", cells[0] or ""):
            hsn, product_no, description = _parse_item_description(cells[1])
            qty_match = re.search(r"([\d,]+(?:\.\d+)?)\s*([A-Za-z]+)", cells[2])
            rate = _first_match(r"([\d,]+(?:\.\d+)?)", cells[3])
            gst_rate = _first_match(r"GST\s*@\s*([\d.]+%)", cells[3])
            amounts = re.findall(r"[\d,]+(?:\.\d+)?", cells[4])

            items.append(
                {
                    "SL_NO": int(cells[0]),
                    "HSN_SAC": hsn,
                    "ProductNo": product_no,
                    "Description": description,
                    "Qty": _number(qty_match.group(1)) if qty_match else None,
                    "UOM": qty_match.group(2).upper() if qty_match else None,
                    "Rate": _number(rate),
                    "TaxableValue": _number(amounts[0]) if amounts else None,
                    "GST_Rate": gst_rate,
                    "GST_Amount": _number(amounts[1]) if len(amounts) > 1 else None,
                    "YourItemNo": None,
                }
            )
            continue

        row_text = " ".join(cells)
        if re.search(r"\bTotal\b", row_text, re.I):
            amounts = re.findall(r"[\d,]+(?:\.\d+)?", row_text)
            if amounts:
                total_taxable = _number(amounts[-1])

    return items, total_taxable


def _party_from_block(block_lines: list[str], gstin: str | None, state_line: str | None) -> dict[str, Any]:
    code = None
    name = None
    address_parts: list[str] = []

    if block_lines:
        code_match = re.search(r"\b([0-9]{4,}[A-Z]?)\b", block_lines[0], re.I)
        code = code_match.group(1) if code_match else None

    for line in block_lines[1:]:
        if re.search(r"^(GSTIN|State)\b", line, re.I):
            break
        if name is None and re.search(r"ENTERPRISE|LIMITED|LTD|COMPANY|PRIVATE|PVT|TOOLS", line, re.I):
            name = line
        elif name is None:
            name = line
        else:
            address_parts.append(line)

    state_code = None
    state = None
    if state_line:
        state_match = re.search(r"State\s+([0-9]{1,2})\s+(.+)$", state_line, re.I)
        if state_match:
            state_code = state_match.group(1).zfill(2)
            state = _clean_text(state_match.group(2))

    return {
        "Code": code,
        "Name": name,
        "Address": _clean_text(" ".join(address_parts)) or None,
        "GSTIN": gstin,
        "State": state,
        "StateCode": state_code,
    }


def extract_invoice_json(markdown: str) -> dict[str, Any]:
    """Normalize DocStrange/Nanonets markdown into invoice JSON."""
    text = _clean_text(markdown)
    lines = _lines(text)

    seller_gstin = _first_match(r"GSTIN\s*:?\s*([0-9A-Z]{15})", text)
    all_gstins = re.findall(r"\b[0-9]{2}[A-Z0-9]{13}\b", text)
    customer_gstins = [gstin for gstin in all_gstins if gstin != seller_gstin]
    state_lines = [line for line in lines if re.match(r"^State\s+[0-9]{1,2}\s+", line, re.I)]

    seller_name = None
    seller_index = _line_index(lines, r"Addison and Company|Tax Invoice")
    if seller_index is not None:
        for line in lines[seller_index + 1 : seller_index + 6]:
            if re.search(r"Company|Limited|LTD|PVT|PRIVATE", line, re.I):
                seller_name = line
                break

    if not seller_name:
        seller_name = _first_match(r"Tax Invoice\s+(.+?)(?:\n|$)", text, re.I | re.S)

    address_parts: list[str] = []
    if seller_name and seller_name in lines:
        start = lines.index(seller_name) + 1
        for line in lines[start:]:
            if re.search(r"^(State Code|TOLL FREE|EMAIL|Website|GSTIN)\b", line, re.I):
                break
            address_parts.append(line)

    invoice_no = None
    invoice_date = None
    invoice_index = _line_index(lines, r"Invoice\s+No\s*/\s*Date")
    if invoice_index is not None:
        for line in lines[invoice_index + 1 : invoice_index + 5]:
            if not invoice_no and re.search(r"[A-Z]{2,}[-/][A-Z0-9/-]+", line, re.I):
                invoice_no = line
                continue
            if not invoice_date and re.search(r"\b\d{1,2}/\d{1,2}/\d{4}\b", line):
                invoice_date = _first_match(r"(\d{1,2}/\d{1,2}/\d{4})", line)
                break

    billed_idx = _line_index(lines, r"^Billed\s+To\b")
    shipped_idx = _line_index(lines, r"^Shipped\s+To\b")
    billed_block = lines[billed_idx : invoice_index or shipped_idx or len(lines)] if billed_idx is not None else []
    shipped_block = lines[shipped_idx : len(lines)] if shipped_idx is not None else []

    line_items, total_taxable = _parse_line_items(markdown)
    if total_taxable is None:
        total_taxable = _number(sum((item.get("TaxableValue") or 0) for item in line_items))

    invoice = {
        "InvoiceType": "Tax Invoice" if re.search(r"Tax Invoice", text, re.I) else None,
        "CopyType": None,
        "SellerDetails": {
            "Name": seller_name,
            "Address": _clean_text(" ".join(address_parts)) or None,
            "StateCode": _number(_first_match(r"State\s*Code\s*\|?\s*(\d+)", text)),
            "TollFree": _extract_after_label("TOLL FREE", lines),
            "Email": _extract_after_label("EMAIL", lines),
            "Website": _extract_after_label("Website", lines),
            "GSTIN": seller_gstin,
            "CIN": _extract_after_label("CIN", lines),
            "PAN": _extract_after_label("PAN", lines),
            "MDS_DC_Company_Range": _first_match(r"MDS/DC\s+Company\s+Range-?\s*(.+)", text),
        },
        "InvoiceDetails": {
            "InvoiceNo": invoice_no,
            "Date": invoice_date,
            "IRN": None,
            "EwayBillNo": _extract_after_label("Eway bill No", lines),
            "DateOfSupply": _first_match(r"Date\s+of\s+Supply\s+(\d{1,2}/\d{1,2}/\d{4})", text),
            "PlaceOfSupply": _first_match(r"Place\s+of\s+Supply\s+([A-Za-z ]+)", text),
            "TransportationMode": _first_match(r"Transportation\s+Mode\s+([A-Za-z0-9-]+)", text),
            "OurOrderNo": None,
            "YourPoNoAndDate": None,
            "PaymentTerms": _first_match(r"Payment\s+Terms\s+(.+)", text),
            "ContactNo": _first_match(r"Contact\s+No\s*:?\s*([0-9]+)", text),
        },
        "BilledTo": _party_from_block(
            billed_block,
            customer_gstins[0] if customer_gstins else None,
            state_lines[0] if state_lines else None,
        ),
        "ShippedTo": _party_from_block(
            shipped_block,
            customer_gstins[1] if len(customer_gstins) > 1 else (customer_gstins[0] if customer_gstins else None),
            state_lines[1] if len(state_lines) > 1 else (state_lines[0] if state_lines else None),
        ),
        "LineItems": line_items,
        "Totals": {
            "TotalQty": _number(sum((item.get("Qty") or 0) for item in line_items)),
            "TotalTaxableValue": total_taxable,
        },
    }
    return _deep_clean(invoice)


def invoice_json_to_csv(invoice: dict[str, Any]) -> str:
    output = StringIO()
    columns = [
        "InvoiceNo",
        "InvoiceDate",
        "VendorName",
        "BuyerName",
        "BuyerGSTIN",
        "SL_NO",
        "HSN_SAC",
        "ProductNo",
        "Description",
        "Qty",
        "UOM",
        "Rate",
        "TaxableValue",
        "GST_Rate",
        "GST_Amount",
        "YourItemNo",
    ]
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    invoice_details = invoice.get("InvoiceDetails") or {}
    seller = invoice.get("SellerDetails") or {}
    buyer = invoice.get("BilledTo") or {}
    base_row = {
        "InvoiceNo": invoice_details.get("InvoiceNo"),
        "InvoiceDate": invoice_details.get("Date"),
        "VendorName": seller.get("Name"),
        "BuyerName": buyer.get("Name"),
        "BuyerGSTIN": buyer.get("GSTIN"),
    }
    for item in invoice.get("LineItems") or []:
        row = dict(base_row)
        row.update(item)
        writer.writerow(row)
    return output.getvalue()


def _file_cache_key(path: str) -> str:
    stat = Path(path).stat()
    return f"path:{path}:{stat.st_mtime_ns}:{stat.st_size}"


def _bytes_cache_key(data: bytes) -> str:
    return "bytes:" + hashlib.sha256(data).hexdigest()


def get_extractor():
    global _EXTRACTOR
    if _EXTRACTOR is None:
        os.environ.setdefault("DOCSTRANGE_NANONETS_MODEL_REPO", DEFAULT_MODEL_REPO)
        os.environ.setdefault("document_extractor_PREFER_HF", "true")
        docstrange_repo = os.environ.get("DOCSTRANGE_REPO", DEFAULT_DOCSTRANGE_REPO)
        if docstrange_repo and Path(docstrange_repo).exists():
            sys.path.insert(0, docstrange_repo)
        from docstrange import DocumentExtractor

        _EXTRACTOR = DocumentExtractor(gpu=True)
    return _EXTRACTOR


def extract_file(path: str) -> dict[str, Any]:
    start = time.time()
    result = get_extractor().extract(path)
    markdown = result.extract_markdown()
    html = result.extract_html()
    payload = {
        "ok": True,
        "cached": False,
        "input_path": path,
        "seconds": round(time.time() - start, 2),
        "markdown": markdown,
        "html": html,
    }
    return payload


def create_app():
    from flask import Flask, Response, jsonify, request

    app = Flask(__name__)

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "model_loaded": _EXTRACTOR is not None, "result_cache_enabled": False})

    @app.post("/extract")
    def extract():
        output_format = (request.args.get("format") or "json").lower()
        temp_path = None
        try:
            if "file" in request.files:
                uploaded = request.files["file"]
                suffix = Path(uploaded.filename or "invoice.jpeg").suffix or ".jpeg"
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
                    data = uploaded.read()
                    temp_file.write(data)
                    temp_path = temp_file.name
                payload = extract_file(temp_path)
            else:
                body = request.get_json(silent=True) or {}
                path = body.get("path") or request.form.get("path")
                if not path:
                    return jsonify({"ok": False, "error": "Send multipart file=<image> or JSON {\"path\": \"...\"}"}), 400
                payload = extract_file(path)

            if output_format == "html":
                return Response(payload["html"], mimetype="text/html")
            if output_format == "markdown":
                return Response(payload["markdown"], mimetype="text/markdown")
            return jsonify(payload)
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"ok": False, "error": str(exc), "traceback": traceback.format_exc()}), 500
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    return app


if __name__ == "__main__":
    port = int(os.environ.get("DOCSTRANGE_INVOICE_PORT", "8010"))
    warm_on_start = os.environ.get("DOCSTRANGE_WARM_ON_START", "1").strip().lower() not in {"0", "false", "no"}
    if warm_on_start:
        print("Loading DocStrange/Nanonets model before starting server...", flush=True)
        get_extractor()
        print("Model loaded. Starting invoice server.", flush=True)
    else:
        print("Lazy model loading is enabled; first /extract request will load the model.", flush=True)
    create_app().run(host="127.0.0.1", port=port, threaded=False)
