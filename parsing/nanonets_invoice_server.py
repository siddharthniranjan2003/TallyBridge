"""Local Nanonets/DocStrange invoice server.

This keeps the DocStrange extractor loaded in memory so repeated invoice tests do
not pay the model startup cost every time.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


MONEY_RE = re.compile(r"^-?\d[\d,]*(?:\.\d+)?$")


class _InvoiceTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: List[List[str]] = []
        self._current_row: Optional[List[str]] = None
        self._current_cell: Optional[List[str]] = None
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag == "tr":
            self._current_row = []
        elif tag in {"td", "th"} and self._current_row is not None:
            self._current_cell = []
            self._in_cell = True
        elif tag == "br" and self._in_cell and self._current_cell is not None:
            self._current_cell.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._current_row is not None and self._current_cell is not None:
            text = normalize_space("".join(self._current_cell).replace("\n", " | "))
            self._current_row.append(text)
            self._current_cell = None
            self._in_cell = False
        elif tag == "tr" and self._current_row is not None:
            if any(cell for cell in self._current_row):
                self.rows.append(self._current_row)
            self._current_row = None

    def handle_data(self, data: str) -> None:
        if self._in_cell and self._current_cell is not None:
            self._current_cell.append(data)


def normalize_text(text: str) -> str:
    return (
        text.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\u00a0", " ")
        .replace("Â°", "*")
        .replace("â˜", "☐")
        .replace("â˜‘", "☑")
    )


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_number(value: str) -> Optional[float]:
    cleaned = normalize_space(value).replace(",", "")
    if not cleaned or not re.match(r"^-?\d+(?:\.\d+)?$", cleaned):
        return None
    number = float(cleaned)
    return int(number) if number.is_integer() else number


def first_match(pattern: str, text: str, flags: int = re.IGNORECASE | re.MULTILINE) -> Optional[str]:
    match = re.search(pattern, text, flags)
    return normalize_space(match.group(1)) if match else None


def extract_block(lines: List[str], start_marker: str, stop_markers: Iterable[str]) -> List[str]:
    start = None
    for index, line in enumerate(lines):
        if line.lower().startswith(start_marker.lower()):
            start = index
            break
    if start is None:
        return []

    stop_set = tuple(marker.lower() for marker in stop_markers)
    block: List[str] = []
    for line in lines[start + 1 :]:
        lowered = line.lower()
        if any(lowered.startswith(marker) for marker in stop_set):
            break
        block.append(line)
    return [line for line in block if line]


def parse_party_block(lines: List[str]) -> Dict[str, Any]:
    code = None
    if lines and re.fullmatch(r"\d+[A-Z]?", lines[0]):
        code = lines.pop(0)
    name = lines[0] if lines else None
    gstin = None
    state = None
    state_code = None
    address_parts: List[str] = []

    for line in lines[1:]:
        gst_match = re.search(r"\b([0-9]{2}[A-Z0-9]{13})\b", line)
        if gst_match:
            gstin = gst_match.group(1)
            continue
        state_match = re.search(r"\bState\s+(\d{2})\s+(.+)$", line, re.IGNORECASE)
        if state_match:
            state_code = state_match.group(1)
            state = normalize_space(state_match.group(2))
            continue
        address_parts.append(line)

    return {
        "Code": code,
        "Name": name,
        "Address": normalize_space(" ".join(address_parts)) or None,
        "GSTIN": gstin,
        "State": state,
        "StateCode": state_code,
    }


def extract_tables(markdown: str) -> List[List[List[str]]]:
    tables = []
    for table_match in re.finditer(r"<table\b.*?</table>", markdown, flags=re.IGNORECASE | re.DOTALL):
        parser = _InvoiceTableParser()
        parser.feed(table_match.group(0))
        tables.append(parser.rows)
    return tables


def split_item_description(value: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    value = normalize_space(value.replace('"IS"', "IS").replace(" IS :", " IS:"))
    match = re.match(r"^(\d{6,10})\s+([A-Z0-9]+(?:\s+\d{4})?)\s+\|\s+(.+)$", value)
    if not match:
        match = re.match(r"^(\d{6,10})\s+([A-Z0-9]+(?:\s+\d{4})?)\s+(.+)$", value)
    if not match:
        return None, None, value or None
    return match.group(1), normalize_space(match.group(2)), normalize_space(match.group(3))


def split_qty_uom(value: str) -> Tuple[Optional[float], Optional[str]]:
    match = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*([A-Za-z]+)", value or "")
    if not match:
        return None, None
    return parse_number(match.group(1)), match.group(2).upper()


def split_rate_tax(value: str) -> Tuple[Optional[float], Optional[str]]:
    rate = first_match(r"(\d[\d,]*(?:\.\d+)?)", value or "")
    gst = first_match(r"GST\s*@\s*(\d+(?:\.\d+)?%)", value or "")
    return parse_number(rate or ""), gst


def split_taxable_tax(value: str) -> Tuple[Optional[float], Optional[float]]:
    numbers = re.findall(r"\d[\d,]*(?:\.\d+)?", value or "")
    taxable = parse_number(numbers[0]) if numbers else None
    tax_amount = parse_number(numbers[1]) if len(numbers) > 1 else None
    return taxable, tax_amount


def parse_line_items(markdown: str) -> List[Dict[str, Any]]:
    tables = extract_tables(markdown)
    rows = tables[0] if tables else []
    items: List[Dict[str, Any]] = []

    for row in rows:
        if len(row) < 5 or not re.fullmatch(r"\d+", row[0]):
            continue
        hsn, product_no, description = split_item_description(row[1])
        qty, uom = split_qty_uom(row[2])
        rate, gst_rate = split_rate_tax(row[3])
        taxable, gst_amount = split_taxable_tax(row[4])
        items.append(
            {
                "SL_NO": int(row[0]),
                "HSN_SAC": hsn,
                "ProductNo": product_no,
                "Description": description,
                "Qty": qty,
                "UOM": uom,
                "Rate": rate,
                "TaxableValue": taxable,
                "GST_Rate": gst_rate,
                "GST_Amount": gst_amount,
                "YourItemNo": None,
            }
        )
    return items


def parse_addison_like_invoice(markdown: str) -> Dict[str, Any]:
    text = normalize_text(markdown)
    lines = [normalize_space(line) for line in text.splitlines() if normalize_space(line)]
    flat = "\n".join(lines)

    seller_name = first_match(r"Tax Invoice\s+(?:☐ Original\s+☐ Duplicate\s+)?(?:<img>.*?</img>\s+)?(.+?Limited)", flat, re.DOTALL)
    if not seller_name:
        seller_name = first_match(r"^(.+?(?:Limited|Company|Corporation|Industries|Enterprises))$", flat)
    seller_address = first_match(rf"{re.escape(seller_name or '')}\s+(.+?)\s+(?:TOLL FREE|State Code)", flat, re.DOTALL) if seller_name else None

    invoice_no = first_match(r"Invoice No\s*/\s*Date\s+([A-Z0-9/-]+)", flat)
    invoice_date = first_match(r"Invoice No\s*/\s*Date\s+[A-Z0-9/-]+\s+(\d{1,2}/\d{1,2}/\d{4})", flat)
    billed_block = extract_block(lines, "Billed To", ("Invoice No", "Date of Supply", "Shipped To", "<table>"))
    if lines:
        for idx, line in enumerate(lines):
            if line.lower().startswith("billed to"):
                code = normalize_space(line[len("Billed To") :])
                if code:
                    billed_block.insert(0, code)
                break
    shipped_block = extract_block(lines, "Shipped To", ("GSTIN", "<table>"))
    if lines:
        for idx, line in enumerate(lines):
            if line.lower().startswith("shipped to"):
                code = normalize_space(line[len("Shipped To") :])
                if code:
                    shipped_block.insert(0, code)
                tail = lines[idx + 1 :]
                stop = next((offset for offset, value in enumerate(tail) if value.startswith("<table>")), len(tail))
                shipped_block = [line for line in shipped_block + tail[:stop] if line]
                break

    line_items = parse_line_items(text)
    total_qty = sum((item["Qty"] or 0) for item in line_items)
    total_taxable = first_match(r"<td[^>]*>\s*Total\s*</td>.*?<td[^>]*>(\d[\d,]*(?:\.\d+)?)</td>\s*</tr>", text, re.IGNORECASE | re.DOTALL)

    return {
        "InvoiceType": "Tax Invoice" if re.search(r"\bTax Invoice\b", text, re.IGNORECASE) else None,
        "CopyType": first_match(r"☑\s*(Original|Duplicate)", text),
        "SellerDetails": {
            "Name": seller_name,
            "Address": seller_address,
            "StateCode": parse_number(first_match(r"State Code\s*\|?\s*(\d+)", flat) or ""),
            "TollFree": first_match(r"TOLL FREE\s*:?\s*([0-9 ]+)", flat),
            "Email": first_match(r"EMAIL\s+([^\s]+@[^\s]+)", flat),
            "Website": first_match(r"Website\s+([^\s]+)", flat),
            "GSTIN": first_match(r"GSTIN\s*:?\s*([0-9]{2}[A-Z0-9]{13})", flat),
            "CIN": first_match(r"CIN\s*:?\s*([A-Z0-9]+)", flat),
            "PAN": first_match(r"PAN\s*:?\s*([A-Z0-9]+)", flat),
            "MDS_DC_Company_Range": first_match(r"MDS/DC Company Range-\s*([A-Z]+ \(\d+\))", flat),
        },
        "InvoiceDetails": {
            "InvoiceNo": invoice_no,
            "Date": invoice_date,
            "IRN": normalize_space((first_match(r"IRN[\":\s]+(.+?)(?:Eway bill|Billed To)", flat, re.IGNORECASE | re.DOTALL) or "").replace(" ", "")) or None,
            "EwayBillNo": first_match(r"Eway bill\s+No[\":\s]+([A-Z0-9/-]+)", flat),
            "DateOfSupply": first_match(r"Date of Supply\s+(\d{1,2}/\d{1,2}/\d{4})", flat),
            "PlaceOfSupply": first_match(r"Place of Supply\s+([A-Za-z ]+)", flat),
            "TransportationMode": first_match(r"Transportation Mode\s+([A-Z-]+)", flat),
            "OurOrderNo": None,
            "YourPoNoAndDate": None,
            "PaymentTerms": first_match(r"Payment Terms\s+([A-Z ]+)", flat),
            "ContactNo": first_match(r"Contact\s+\"?No\"?:\s*([0-9]+)", flat),
        },
        "BilledTo": parse_party_block(billed_block),
        "ShippedTo": parse_party_block(shipped_block),
        "LineItems": line_items,
        "Totals": {
            "TotalQty": total_qty,
            "TotalTaxableValue": parse_number(total_taxable or "") or sum((item["TaxableValue"] or 0) for item in line_items),
        },
    }


def invoice_to_csv(invoice: Dict[str, Any]) -> str:
    output = io.StringIO()
    fieldnames = [
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
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for item in invoice.get("LineItems", []):
        writer.writerow({key: item.get(key) for key in fieldnames})
    return output.getvalue()


def load_markdown_from_docstrange_json(path: str) -> str:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    document = payload.get("document", {})
    return document.get("raw_text") or payload.get("raw_text") or ""


_extractor = None


def get_extractor():
    global _extractor
    if _extractor is None:
        from docstrange import DocumentExtractor

        _extractor = DocumentExtractor(gpu=True)
    return _extractor


def extract_markdown_from_file(path: str) -> str:
    result = get_extractor().extract(path)
    return result.extract_markdown()


def create_app():
    from flask import Flask, Response, jsonify, request

    app = Flask(__name__)

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "model_loaded": _extractor is not None})

    @app.post("/invoice")
    def invoice():
        upload = request.files.get("file")
        input_path = request.form.get("path")
        output_format = (request.form.get("format") or "json").lower()
        tmp_path = None

        try:
            if upload:
                suffix = Path(upload.filename or "invoice.jpeg").suffix or ".jpeg"
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
                tmp_path = tmp.name
                tmp.close()
                upload.save(tmp_path)
                source_path = tmp_path
            elif input_path:
                source_path = input_path
            else:
                return jsonify({"ok": False, "error": "Send multipart file=... or form path=..."}), 400

            markdown = extract_markdown_from_file(source_path)
            parsed = parse_addison_like_invoice(markdown)

            if output_format == "csv":
                return Response(invoice_to_csv(parsed), mimetype="text/csv")
            return jsonify({"ok": True, "invoice": parsed, "markdown": markdown})
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Nanonets/DocStrange invoice JSON server")
    parser.add_argument("--serve", action="store_true", help="Start HTTP server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--from-json", help="Parse an existing DocStrange JSON file")
    parser.add_argument("--image", help="Run OCR on an image/PDF and parse it")
    parser.add_argument("--out", help="Write JSON output to this path")
    parser.add_argument("--csv-out", help="Write line-item CSV output to this path")
    args = parser.parse_args()

    if args.serve:
        app = create_app()
        app.run(host=args.host, port=args.port)
        return 0

    if args.from_json:
        markdown = load_markdown_from_docstrange_json(args.from_json)
    elif args.image:
        markdown = extract_markdown_from_file(args.image)
    else:
        parser.error("Use --serve, --from-json, or --image")

    invoice = parse_addison_like_invoice(markdown)
    if args.out:
        Path(args.out).write_text(json.dumps(invoice, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        print(json.dumps(invoice, ensure_ascii=False, indent=2))
    if args.csv_out:
        Path(args.csv_out).write_text(invoice_to_csv(invoice), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
