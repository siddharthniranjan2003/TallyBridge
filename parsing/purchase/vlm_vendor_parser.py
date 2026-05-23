from __future__ import annotations

from dataclasses import dataclass
from html import unescape
import re
from typing import Any

from lib.text import normalize_space
from purchase.vendor import detect_vendor

LINE_Y_TOLERANCE = 2.2  # kept for parity with the PDF parser design
ROW_RE = re.compile(
    r"^(?P<row_no>\d+)\s+"
    r"(?:(?P<item_code>[A-Z0-9-]{4,})\s+)?"
    r"(?P<description>.+?)\s+"
    r"(?:(?P<hsn>\d{6,12})\s+)?"
    r"(?P<qty>[0-9][0-9,]*(?:\.\d+)?)\s+"
    r"(?P<unit>[A-Z]{1,10})\s+"
    r"(?P<rate>[0-9][0-9,]*(?:\.\d{1,2})?)\s+"
    r"(?P<amount>[0-9][0-9,]*(?:\.\d{1,2})?)$"
)
AMOUNT_RE = re.compile(r"-?[0-9][0-9,]*\.\d{1,2}")
DATE_RE = re.compile(r"\b\d{1,2}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{2,4}\b")
FOOTER_MARKERS = (
    "TOTAL :",
    "TOTAL TAXABLE",
    "NET BILL VALUE",
    "TOTAL INVOICE VALUE",
    "DISCOUNT",
    "IGST",
    "CGST",
    "SGST",
    "R.OFF",
    "ROUND OFF",
)
ADDISON_ROW_RE = re.compile(
    r"^(?P<row_no>\d+)\s+"
    r"(?P<hsn>\d{6,12})\s+"
    r"(?P<code1>[A-Z0-9/-]+)\s+"
    r"(?P<code2>[A-Z0-9/-]+)\s+"
    r"(?P<qty>[0-9][0-9,]*(?:\.\d+)?)\s+"
    r"(?P<unit>[A-Z]{1,10})\s+"
    r"(?P<rate>[0-9][0-9,]*(?:\.\d{1,2})?)\s+"
    r"(?P<amount>[0-9][0-9,]*(?:\.\d{1,2})?)$"
)
ADDISON_ROW_PARTIAL_RE = re.compile(
    r"^(?P<row_no>\d+)\s+"
    r"(?P<hsn>\d{6,12})\s+"
    r"(?P<code1>[A-Z0-9/-]+)\s+"
    r"(?P<code2>[A-Z0-9/-]+)\s+"
    r"(?P<qty>[0-9][0-9,]*(?:\.\d+)?)\s+"
    r"(?P<unit>[A-Z]{1,10})\s+"
    r"(?P<rate>[0-9][0-9,]*(?:\.\d{1,2})?)"
    r"(?:\s*GST@\d+(?:\.\d+)?%)?"
    r"(?:\s+(?P<amount>[0-9][0-9,]*(?:\.\d{1,2})?))?"
    r"(?:\s+(?P<tax>[0-9][0-9,]*(?:\.\d{1,2})?))?$"
)
ADDISON_MERGED_ROW_RE = re.compile(
    r"^(?P<row_no>\d+)\s+"
    r"(?P<hsn>\d{6,12})\s+"
    r"(?P<code1>[A-Z0-9/-]+)\s*(?P<code2>0000)\s*"
    r"(?P<description>.+?)\s+"
    r"(?P<qty>[0-9][0-9,]*(?:\.\d+)?)\s+"
    r"(?P<unit>[A-Z]{1,10})\s+"
    r"(?P<rate>[0-9][0-9,]*(?:\.\d{1,2})?)"
    r"(?:\s*GST@\d+(?:\.\d+)?%)?\s+"
    r"(?P<amount>[0-9][0-9,]*(?:\.\d{1,2})?)"
    r"(?:\s+(?P<tax>[0-9][0-9,]*(?:\.\d{1,2})?))?$"
)
CP_ROW_RE = re.compile(
    r"^(?P<row_no>\d+)\s+"
    r"(?P<description>.+?)\s+"
    r"(?P<hsn>\d{6,12})\s+"
    r"(?P<qty>[0-9][0-9,]*)\s+"
    r"(?P<unit>[A-Z]{2,10})\s+"
    r"(?P<rate>[0-9][0-9,]*(?:\.\d{1,2})?)\s+"
    r"(?P<rate_unit>[A-Z]{2,10})\s+"
    r"(?P<discount>[0-9]+(?:\.\d+)?)\s*%\s+"
    r"(?P<amount>[0-9][0-9,]*(?:\.\d{1,2})?)$"
)
GNL_ROW_RE = re.compile(
    r"^(?P<row_no>\d{4})\s+"
    r"(?P<item_code>\d{8,15})\s+"
    r"(?P<description>.+?)\s+"
    r"(?P<qty>[0-9][0-9,]*)\s+"
    r"(?P<unit>[A-Z]{1,4})\s*/\s*"
    r"(?P<packages>[0-9]+)\s+"
    r"(?P<rate>[0-9][0-9,]*(?:\.\d{1,2})?)\s*/\s*"
    r"(?P<rate_unit>[A-Z]{1,4})\s+"
    r"(?P<amount>[0-9][0-9,]*(?:\.\d{1,2})?)$"
)
RR_ROW_RE = re.compile(
    r"^(?P<row_no>\d+)\s+"
    r"(?P<description>.+?)\s+"
    r"(?P<hsn>\d{6,12})\s+"
    r"(?P<qty>[0-9][0-9,]*)\s+"
    r"(?P<unit>[A-Za-z]{1,10})\s+"
    r"(?P<rate>[0-9][0-9,]*(?:\.\d{1,2})?)\s+"
    r"(?P<rate_unit>[A-Za-z]{1,10})\s+"
    r"(?P<discount>[0-9]+(?:\.\d+)?)\s*%\s+"
    r"(?P<amount>[0-9][0-9,]*(?:\.\d{1,2})?)$"
)
RR_SIMPLE_ROW_RE = re.compile(
    r"^(?P<row_no>\d+)\s+"
    r"(?P<description>.+?)\s+"
    r"(?P<hsn>\d{6,12})\s+"
    r"(?P<qty>[0-9][0-9,]*)\s+"
    r"(?P<unit>[A-Za-z]{1,10})\s+"
    r"(?P<rate>[0-9][0-9,]*(?:\.\d{1,2})?)\s+"
    r"(?P<rate_unit>[A-Za-z]{1,10})\s+"
    r"(?P<amount>[0-9][0-9,]*(?:\.\d{1,2})?)$"
)
STANLEY_ROW_RE = re.compile(
    r"^(?P<row_no>\d+)\s+"
    r"(?P<item_code>[A-Z0-9]+)\s+"
    r"(?P<hsn>\d{6,12})\s+"
    r"(?P<rate>[0-9][0-9,]*(?:\.\d{1,2})?)\s+"
    r"(?P<qty>[0-9][0-9,]*)\s+"
    r"(?P<unit>[A-Z]{1,10})\s+"
    r"(?P<amount>[0-9][0-9,]*(?:\.\d{1,2})?)$"
)
TOTEM_ROW_RE = re.compile(
    r"^(?P<row_no>\d+)\s+"
    r"(?P<item_code>[A-Z0-9]+)\s+"
    r"(?P<description>.+?)\s+"
    r"(?P<hsn>\d{6,12})\s+"
    r"(?P<qty>[0-9][0-9,]*)\s+"
    r"(?P<rate>[0-9][0-9,]*(?:\.\d{1,2})?)\s+Per\s+\d+\s+"
    r"(?P<unit>[A-Z]{1,10})\s+"
    r"(?P<amount>[0-9][0-9,]*(?:\.\d{1,2})?)$"
)
WIKUS_CODE_RE = re.compile(r"^\d{5}-\d{4}$")


@dataclass
class OcrLine:
    page_index: int
    text: str


def normalize_ocr_text(value: str) -> str:
    text = unescape(str(value or ""))
    text = (
        text.replace("â€”", "-")
        .replace("â€“", "-")
        .replace("â€œ", '"')
        .replace("â€", '"')
        .replace("â€™", "'")
        .replace("â€˜", "'")
        .replace("â‚¹", "")
        .replace("\xa0", " ")
    )
    return normalize_space(text)


def strip_html_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text)


def _float_from_token(value: str) -> float:
    return float(str(value).replace(",", "").strip())


def _last_amount_from_text(text: str) -> float | None:
    matches = AMOUNT_RE.findall(text or "")
    if not matches:
        return None
    return _float_from_token(matches[-1])


def _first_date_from_text(text: str) -> str:
    match = DATE_RE.search(text or "")
    if not match:
        return ""
    return re.sub(r"\s*([./-])\s*", r"\1", match.group(0))


def _joined_text(lines: list[OcrLine]) -> str:
    return "\n".join(line.text for line in lines)


def _extract_markdown_tables(markdown: str) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for raw in markdown.splitlines():
        line = raw.strip()
        if line.startswith("|") and line.count("|") >= 2:
            cells = [normalize_ocr_text(cell) for cell in line.strip("|").split("|")]
            if all(re.fullmatch(r":?-+:?", cell or "-") for cell in cells):
                continue
            current.append(cells)
        else:
            if len(current) >= 1:
                tables.append(current)
            current = []
    if len(current) >= 1:
        tables.append(current)
    return tables


def _extract_html_tables(markdown: str) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    for table_html in re.findall(r"<table[^>]*>(.*?)</table>", markdown, flags=re.DOTALL | re.IGNORECASE):
        rows: list[list[str]] = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, flags=re.DOTALL | re.IGNORECASE):
            cells = [
                normalize_ocr_text(strip_html_tags(td))
                for td in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, flags=re.DOTALL | re.IGNORECASE)
            ]
            if any(cells):
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def _table_rows_to_lines(page_index: int, tables: list[list[list[str]]]) -> list[OcrLine]:
    lines: list[OcrLine] = []
    for table in tables:
        for row in table:
            text = normalize_ocr_text(" ".join(cell for cell in row if cell))
            if text:
                lines.append(OcrLine(page_index=page_index, text=text))
    return lines


def _extract_plain_markdown_lines(page_index: int, markdown: str) -> list[OcrLine]:
    cleaned = re.sub(r"<table[^>]*>.*?</table>", " ", markdown, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<img[^>]*>", " ", cleaned, flags=re.IGNORECASE)
    lines: list[OcrLine] = []
    for raw in cleaned.splitlines():
        text = normalize_ocr_text(strip_html_tags(raw).replace("|", " "))
        if text:
            lines.append(OcrLine(page_index=page_index, text=text))
    return lines


def _extract_layout_lines(page_index: int, layout_page: dict[str, Any]) -> list[OcrLine]:
    lines: list[OcrLine] = []
    for block in layout_page.get("parsing_res_list", []) or []:
        content = str(block.get("block_content", "") or "")
        if not content:
            continue
        label = str(block.get("block_label", "") or "").lower()
        if label == "table":
            lines.extend(_table_rows_to_lines(page_index, _extract_html_tables(content)))
            continue
        text = normalize_ocr_text(strip_html_tags(content))
        if text:
            lines.append(OcrLine(page_index=page_index, text=text))
    return lines


def build_ocr_lines(vlm_result: dict[str, Any]) -> list[OcrLine]:
    page_markdown = vlm_result.get("page_markdown")
    if not isinstance(page_markdown, list) or not page_markdown:
        page_markdown = [vlm_result.get("markdown", "") or ""]
    layout = vlm_result.get("layout")
    layout_pages = layout if isinstance(layout, list) else []

    deduped: list[OcrLine] = []
    seen: set[tuple[int, str]] = set()
    for page_index, markdown in enumerate(page_markdown):
        page_lines: list[OcrLine] = []
        markdown = str(markdown or "")
        page_lines.extend(_extract_plain_markdown_lines(page_index, markdown))
        page_lines.extend(_table_rows_to_lines(page_index, _extract_markdown_tables(markdown)))
        page_lines.extend(_table_rows_to_lines(page_index, _extract_html_tables(markdown)))
        if page_index < len(layout_pages) and isinstance(layout_pages[page_index], dict):
            page_lines.extend(_extract_layout_lines(page_index, layout_pages[page_index]))
        for line in page_lines:
            key = (line.page_index, line.text)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(line)
    return deduped


def _trim_duplicate_copy_lines(lines: list[OcrLine]) -> list[OcrLine]:
    for index, line in enumerate(lines):
        upper = line.text.upper()
        if "DUPLICATE FOR TRANSPORTER" in upper or "DUPLICATE FOR RECIPIENT" in upper:
            return lines[:index]
    return lines


def _find_table_header_index(lines: list[OcrLine]) -> int:
    for index, line in enumerate(lines):
        upper = line.text.upper()
        if "DESCRIPTION" in upper and "QTY" in upper and "RATE" in upper and "TAXABLE" in upper:
            return index
    raise ValueError("Could not locate the purchase item table in the OCR output.")


def _find_table_end_index(lines: list[OcrLine], header_index: int) -> int:
    header_page = lines[header_index].page_index
    for index in range(header_index + 1, len(lines)):
        line = lines[index]
        upper = line.text.upper()
        if line.page_index != header_page:
            return index
        if any(marker in upper for marker in FOOTER_MARKERS):
            return index
    return len(lines)


def _parse_item_lines(lines: list[OcrLine], header_index: int, end_index: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    description_rows: list[dict[str, Any]] = []
    numeric_rows: list[dict[str, Any]] = []
    for line in lines[header_index + 1 : end_index]:
        text = line.text
        if not text or not text[:1].isdigit():
            continue
        match = ROW_RE.match(text)
        if not match:
            continue
        groups = match.groupdict()
        description_rows.append(
            {
                "item_code": normalize_space(groups.get("item_code", "")),
                "raw_description": normalize_space(groups.get("description", "")),
                "hsn_code": normalize_space(groups.get("hsn", "")),
                "row_no": int(groups["row_no"]),
            }
        )
        numeric_rows.append(
            {
                "quantity": _float_from_token(groups["qty"]),
                "unit": normalize_space(groups["unit"]),
                "rate": _float_from_token(groups["rate"]),
                "amount": _float_from_token(groups["amount"]),
            }
        )
    if not description_rows:
        raise ValueError("No purchase item rows could be parsed from the OCR text table.")
    return description_rows, numeric_rows


def _parse_addison_items(lines: list[OcrLine]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    description_rows: list[dict[str, Any]] = []
    numeric_rows: list[dict[str, Any]] = []
    index = 0

    def _normalize_addison_line(text: str) -> str:
        cleaned = normalize_ocr_text(text)
        cleaned = re.sub(r"(0000)(?=[A-Z])", r"\1 ", cleaned)
        cleaned = re.sub(r"(?<=\d)(GST@\d+(?:\.\d+)?%)", r" \1", cleaned)
        cleaned = re.sub(r"(GST@\d+(?:\.\d+)?%)(?=\d)", r"\1 ", cleaned)
        cleaned = re.sub(
            r"(\d{1,3}(?:,\d{3})*\.\d{2})(?=\d{1,3}(?:,\d{3})*\.\d{2}$)",
            r"\1 ",
            cleaned,
        )
        return normalize_space(cleaned)

    def _is_addison_row_start(text: str) -> bool:
        normalized = _normalize_addison_line(text)
        return bool(
            ADDISON_ROW_RE.match(normalized)
            or ADDISON_ROW_PARTIAL_RE.match(normalized)
            or ADDISON_MERGED_ROW_RE.match(normalized)
        )

    def _clean_addison_description(text: str) -> str:
        cleaned = re.sub(r"\bGST@\d+(?:\.\d+)?%.*$", "", text, flags=re.IGNORECASE)
        cleaned = re.sub(r"(?<=[A-Za-z])(?=\d+(?:\.\d+)?mm)", " ", cleaned, flags=re.IGNORECASE)
        return normalize_space(cleaned.strip(" -:"))

    def _append_row(
        row_no: str,
        hsn: str,
        code1: str,
        code2: str,
        qty: str,
        unit: str,
        rate: str,
        amount: str | None,
        description: str,
    ) -> None:
        resolved_amount = _float_from_token(amount) if amount else round(_float_from_token(qty) * _float_from_token(rate), 2)
        description_rows.append(
            {
                "item_code": normalize_space(f"{code1} {code2}"),
                "raw_description": _clean_addison_description(description) or normalize_space(f"{code1} {code2}"),
                "hsn_code": normalize_space(hsn),
                "row_no": int(row_no),
            }
        )
        numeric_rows.append(
            {
                "quantity": _float_from_token(qty),
                "unit": normalize_space(unit),
                "rate": _float_from_token(rate),
                "amount": resolved_amount,
            }
        )

    while index < len(lines):
        normalized = _normalize_addison_line(lines[index].text)
        if not _is_addison_row_start(normalized):
            index += 1
            continue

        block_lines = [normalized]
        look_ahead = index + 1
        while look_ahead < len(lines):
            next_text = _normalize_addison_line(lines[look_ahead].text)
            upper = next_text.upper()
            if _is_addison_row_start(next_text) or upper.startswith("TOTAL") or upper.startswith("PAGE "):
                break
            if upper.startswith("YOUR ITEM NO"):
                look_ahead += 1
                continue
            block_lines.append(next_text)
            look_ahead += 1

        first_line = block_lines[0]
        split_match = ADDISON_ROW_RE.match(first_line) or ADDISON_ROW_PARTIAL_RE.match(first_line)
        if split_match:
            groups = split_match.groupdict()
            description = " ".join(block_lines[1:]) if len(block_lines) > 1 else ""
            _append_row(
                groups["row_no"],
                groups["hsn"],
                groups["code1"],
                groups["code2"],
                groups["qty"],
                groups["unit"],
                groups["rate"],
                groups.get("amount"),
                description,
            )
            index = look_ahead
            continue

        merged_text = " ".join(block_lines)
        merged_match = ADDISON_MERGED_ROW_RE.match(merged_text)
        if merged_match:
            groups = merged_match.groupdict()
            _append_row(
                groups["row_no"],
                groups["hsn"],
                groups["code1"],
                groups["code2"],
                groups["qty"],
                groups["unit"],
                groups["rate"],
                groups.get("amount"),
                groups.get("description", ""),
            )
            index = look_ahead
            continue

        index = look_ahead

    if not description_rows:
        raise ValueError("No Addison item rows could be parsed from the OCR output.")
    return description_rows, numeric_rows


def _parse_cp_items(lines: list[OcrLine]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    description_rows: list[dict[str, Any]] = []
    numeric_rows: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        match = CP_ROW_RE.match(line.text)
        if not match:
            index += 1
            continue
        groups = match.groupdict()
        description_parts = [groups["description"]]
        look_ahead = index + 1
        while look_ahead < len(lines):
            next_text = lines[look_ahead].text
            upper = next_text.upper()
            if CP_ROW_RE.match(next_text) or upper.startswith("COURIER CHARGES") or upper.startswith("ROUNDING OFF") or upper.startswith("TOTAL"):
                break
            if "COMPUTER GENERATED INVOICE" in upper or "CONTINUED TO PAGE" in upper:
                look_ahead += 1
                continue
            description_parts.append(next_text)
            look_ahead += 1
        raw_description = normalize_space(" ".join(description_parts))
        description_rows.append(
            {
                "item_code": raw_description,
                "raw_description": raw_description,
                "hsn_code": normalize_space(groups.get("hsn", "")),
                "row_no": int(groups["row_no"]),
            }
        )
        numeric_rows.append(
            {
                "quantity": _float_from_token(groups["qty"]),
                "unit": normalize_space(groups["unit"]),
                "rate": _float_from_token(groups["rate"]),
                "amount": _float_from_token(groups["amount"]),
                "discount_pct": _float_from_token(groups["discount"]),
            }
        )
        index = look_ahead
    if not description_rows:
        raise ValueError("No CP Grat-Ex item rows could be parsed from the OCR output.")
    return description_rows, numeric_rows


def _strip_gnl_trailing_price(text: str) -> str:
    return normalize_space(
        re.sub(r"\s+[0-9][0-9,]*\.\d{1,2}\s*/\s*[A-Z]{1,4}\s+IGST.*$", "", text, flags=re.IGNORECASE)
    )


def _parse_gnl_items(lines: list[OcrLine]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    description_rows: list[dict[str, Any]] = []
    numeric_rows: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        match = GNL_ROW_RE.match(line.text)
        if not match:
            index += 1
            continue
        groups = match.groupdict()
        description_parts = [groups["description"]]
        look_ahead = index + 1
        while look_ahead < len(lines):
            next_text = lines[look_ahead].text
            upper = next_text.upper()
            if GNL_ROW_RE.match(next_text) or upper.startswith("GROSS VALUE") or upper.startswith("TOTAL DISCOUNT") or upper.startswith("INVOICE TOTAL"):
                break
            if upper.startswith("SPD") or upper.startswith("EB ") or upper.startswith("TD "):
                look_ahead += 1
                continue
            if upper.startswith("HSN/SAC CODE"):
                look_ahead += 1
                continue
            description_parts.append(_strip_gnl_trailing_price(next_text))
            look_ahead += 1
        description_rows.append(
            {
                "item_code": groups["item_code"],
                "raw_description": normalize_space(" ".join(part for part in description_parts if part)),
                "hsn_code": "",
                "row_no": int(groups["row_no"]),
            }
        )
        numeric_rows.append(
            {
                "quantity": _float_from_token(groups["qty"]),
                "unit": normalize_space(groups["unit"]),
                "rate": _float_from_token(groups["rate"]),
                "amount": _float_from_token(groups["amount"]),
            }
        )
        index = look_ahead
    if not description_rows:
        raise ValueError("No Grindwell Norton item rows could be parsed from the OCR output.")
    return description_rows, numeric_rows


def _parse_rr_items(lines: list[OcrLine]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    description_rows: list[dict[str, Any]] = []
    numeric_rows: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        match = RR_ROW_RE.match(line.text) or RR_SIMPLE_ROW_RE.match(line.text)
        if not match:
            index += 1
            continue
        groups = match.groupdict()
        description_parts = [groups["description"]]
        look_ahead = index + 1
        while look_ahead < len(lines):
            next_text = lines[look_ahead].text
            upper = next_text.upper()
            if RR_ROW_RE.match(next_text) or RR_SIMPLE_ROW_RE.match(next_text) or upper.startswith("TOTAL") or upper.startswith("CGST") or upper.startswith("IGST") or upper.startswith("SGST"):
                break
            if upper.startswith("INVOICE(PAGE") or upper.startswith("INVOICE (PAGE") or bool(re.fullmatch(r"[0-9][0-9,]*\.\d{1,2}", next_text)):
                break
            if "COMPUTER GENERATED INVOICE" in upper or "CONTINUED TO PAGE" in upper:
                look_ahead += 1
                continue
            description_parts.append(next_text)
            look_ahead += 1
        raw_description = normalize_space(" ".join(description_parts))
        code_tokens = re.findall(r"\b[A-Z0-9]{6,}\b", raw_description)
        code_match = [token for token in code_tokens if re.search(r"[A-Z]", token) and re.search(r"\d", token)]
        description_rows.append(
            {
                "item_code": code_match[-1] if code_match else "",
                "raw_description": raw_description,
                "hsn_code": normalize_space(groups.get("hsn", "")),
                "row_no": int(groups["row_no"]),
            }
        )
        numeric_rows.append(
            {
                "quantity": _float_from_token(groups["qty"]),
                "unit": normalize_space(groups["unit"]),
                "rate": _float_from_token(groups["rate"]),
                "amount": _float_from_token(groups["amount"]),
                "discount_pct": _float_from_token(groups.get("discount", "0") or "0"),
            }
        )
        index = look_ahead
    if not description_rows:
        raise ValueError("No R R Tools item rows could be parsed from the OCR output.")
    return description_rows, numeric_rows


def _parse_stanley_items(lines: list[OcrLine]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    description_rows: list[dict[str, Any]] = []
    numeric_rows: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        match = STANLEY_ROW_RE.match(line.text)
        if not match:
            index += 1
            continue
        groups = match.groupdict()
        description_parts: list[str] = []
        look_ahead = index + 1
        while look_ahead < len(lines):
            next_text = lines[look_ahead].text
            upper = next_text.upper()
            if STANLEY_ROW_RE.match(next_text) or upper.startswith("LESS DISCOUNT") or upper.startswith("TOTAL QUAN") or upper.startswith("STANLEY BLACK"):
                break
            description_parts.append(next_text)
            look_ahead += 1
        description_rows.append(
            {
                "item_code": groups["item_code"],
                "raw_description": normalize_space(" ".join(description_parts)) or groups["item_code"],
                "hsn_code": normalize_space(groups.get("hsn", "")),
                "row_no": int(groups["row_no"]),
            }
        )
        numeric_rows.append(
            {
                "quantity": _float_from_token(groups["qty"]),
                "unit": normalize_space(groups["unit"]),
                "rate": _float_from_token(groups["rate"]),
                "amount": _float_from_token(groups["amount"]),
            }
        )
        index = look_ahead
    if not description_rows:
        raise ValueError("No Stanley item rows could be parsed from the OCR output.")
    return description_rows, numeric_rows


def _parse_wikus_items(lines: list[OcrLine]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    description_rows: list[dict[str, Any]] = []
    numeric_rows: list[dict[str, Any]] = []
    pending_code = ""
    index = 0
    while index < len(lines):
        text = lines[index].text
        upper = text.upper()
        if WIKUS_CODE_RE.fullmatch(text):
            pending_code = text
            index += 1
            continue
        row_match = re.match(
            r"^(?P<row_no>\d+)\s+"
            r"(?P<series>(?:ECOFLEX|PRIMAR|NOVOFLEX)\s+M42)\s+"
            r"8202\s+2000\s+"
            r"(?P<qty>[0-9][0-9,]*)\s+"
            r"(?P<rate>[0-9][0-9,]*(?:\.\d{1,2})?)\s+"
            r"(?P<amount>[0-9][0-9,]*(?:\.\d{1,2})?)\b",
            text,
            re.IGNORECASE,
        )
        if not row_match:
            index += 1
            continue
        groups = row_match.groupdict()
        dims = ""
        blade_length = ""
        look_ahead = index + 1
        while look_ahead < len(lines):
            next_text = lines[look_ahead].text
            upper_next = next_text.upper()
            if WIKUS_CODE_RE.fullmatch(next_text) or upper_next.startswith("TOTAL ") or "DUPLICATE FOR TRANSPORTER" in upper_next:
                break
            if " BLADE LENGTH " in f" {upper_next} ":
                blade_length = next_text
            elif "TPI" in upper_next and "MM" in upper_next:
                dims = next_text
            look_ahead += 1
        description_rows.append(
            {
                "item_code": pending_code,
                "raw_description": normalize_space(f"{groups['series']} {dims} {blade_length}"),
                "hsn_code": "82022000",
                "row_no": int(groups["row_no"]),
            }
        )
        numeric_rows.append(
            {
                "quantity": _float_from_token(groups["qty"]),
                "unit": "NOS",
                "rate": _float_from_token(groups["rate"]),
                "amount": _float_from_token(groups["amount"]),
            }
        )
        pending_code = ""
        index = look_ahead
    if not description_rows:
        raise ValueError("No WIKUS item rows could be parsed from the OCR output.")
    return description_rows, numeric_rows


def _parse_totem_items(lines: list[OcrLine]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    description_rows: list[dict[str, Any]] = []
    numeric_rows: list[dict[str, Any]] = []
    for line in lines:
        match = TOTEM_ROW_RE.match(line.text)
        if not match:
            continue
        groups = match.groupdict()
        description_rows.append(
            {
                "item_code": groups["item_code"],
                "raw_description": normalize_space(groups["description"]),
                "hsn_code": normalize_space(groups.get("hsn", "")),
                "row_no": int(groups["row_no"]),
            }
        )
        numeric_rows.append(
            {
                "quantity": _float_from_token(groups["qty"]),
                "unit": normalize_space(groups["unit"]),
                "rate": _float_from_token(groups["rate"]),
                "amount": _float_from_token(groups["amount"]),
            }
        )
    if not description_rows:
        raise ValueError("No Forbes/TOTEM item rows could be parsed from the OCR output.")
    return description_rows, numeric_rows


def _parse_pidilite_items(lines: list[OcrLine]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    description_rows: list[dict[str, Any]] = []
    numeric_rows: list[dict[str, Any]] = []
    item_code = ""
    raw_description = ""
    quantity = 0.0
    rate = 0.0
    amount = 0.0
    for index, line in enumerate(lines):
        text = line.text
        if re.fullmatch(r"[A-Z0-9]{10,}", text):
            item_code = text
        if "STEELGRIP" in text.upper():
            desc_parts = [text]
            if index + 1 < len(lines) and "]" not in text:
                desc_parts.append(lines[index + 1].text)
            raw_description = normalize_space(" ".join(desc_parts))
        if not quantity:
            qty_match = re.search(r"(?P<cases>\d+)\s*CASE\s*/\s*(?P<units>\d+)\s*EA", text, re.IGNORECASE)
            if qty_match:
                quantity = float(qty_match.group("units"))
        if not rate or not amount:
            row_match = re.search(r"(?P<cases>\d+)Case\s+(?P<rate>[0-9][0-9,]*(?:\.\d{1,2})?)\s+(?P<amount>[0-9][0-9,]*(?:\.\d{1,2})?)", text, re.IGNORECASE)
            if row_match:
                rate = _float_from_token(row_match.group("rate"))
                amount = _float_from_token(row_match.group("amount"))
    if item_code and raw_description and quantity and rate and amount:
        description_rows.append(
            {
                "item_code": item_code,
                "raw_description": raw_description,
                "hsn_code": "85469090",
                "row_no": 1,
            }
        )
        numeric_rows.append(
            {
                "quantity": quantity,
                "unit": "NOS",
                "rate": rate,
                "amount": amount,
            }
        )
    if not description_rows:
        raise ValueError("No Pidilite item rows could be parsed from the OCR output.")
    return description_rows, numeric_rows


def _parse_vendor_items(vendor: str, lines: list[OcrLine]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if vendor == "ET":
        header_index = _find_table_header_index(lines)
        end_index = _find_table_end_index(lines, header_index)
        return _parse_item_lines(lines, header_index, end_index)
    if vendor == "ADDISON":
        return _parse_addison_items(lines)
    if vendor == "CP":
        return _parse_cp_items(lines)
    if vendor == "GNL":
        return _parse_gnl_items(lines)
    if vendor == "RR":
        return _parse_rr_items(lines)
    if vendor == "STANLEY":
        return _parse_stanley_items(lines)
    if vendor == "WIKUS":
        return _parse_wikus_items(lines)
    if vendor == "TOTEM":
        return _parse_totem_items(lines)
    if vendor == "PIDILITE":
        return _parse_pidilite_items(lines)
    raise ValueError(f"Unsupported purchase OCR vendor parser: {vendor}")


def _extract_vendor_name(lines: list[OcrLine]) -> str:
    for line in lines:
        upper = line.text.upper()
        if upper.startswith("FOR "):
            candidate = normalize_space(line.text[4:])
            candidate_upper = candidate.upper()
            if any(
                token in candidate_upper
                for token in (
                    "LIMITED",
                    "LTD",
                    "PRIVATE",
                    "EQUIPMENTS",
                    "CP GRAT-EX",
                    "GRINDWELL",
                    "ADDISON",
                    "EMKAY",
                    "R R TOOLS",
                    "FORBES",
                    "PIDILITE",
                    "STANLEY",
                    "WIKUS",
                )
            ):
                return candidate
    for line in lines[:20]:
        upper = line.text.upper()
        if not upper or "TAX INVOICE" in upper:
            continue
        if any(
            token in upper
            for token in (
                "CP GRAT-EX",
                "GRINDWELL NORTON",
                "ADDISON",
                "EMKAY TOOLS",
                "R R TOOLS",
                "FORBES PRECISION",
                "PIDILITE",
                "STANLEY BLACK",
                "WIKUS",
            )
        ):
            if "ADDISON" in upper:
                return "Addison and Company Limited"
            return normalize_space(line.text)
        if any(token in upper for token in ("LIMITED", "LTD", "PRIVATE", "EQUIPMENTS")):
            if "ADDISON" in upper:
                return "Addison and Company Limited"
            return normalize_space(line.text)
    raise ValueError("Could not determine supplier name from the OCR header.")


def _extract_invoice_number(lines: list[OcrLine], vendor: str) -> str:
    joined = _joined_text(lines)
    patterns = [
        r"(?:SERIAL\s+NO\.?\s+INVOICE|INVOICE\s+NO(?:\s*/\s*DATE)?)\s*:?\s*([A-Z0-9][A-Z0-9\-/]+)",
        r"GST\s+INVOICE\s+NO\s*:\s*([A-Z0-9][A-Z0-9\-/]+)",
        r"INVOICE\s+NUMBER\s*:\s*([A-Z0-9][A-Z0-9\-/]+)",
        r"DOCUMENT\s+NO\s*:\s*([A-Z0-9][A-Z0-9\-/]+)",
        r"\b(RR/\d{2}-\d{2}/\d+)\b",
        r"\b(\d{1,4}/\d{4}-\d{2})\b",
    ]
    if vendor == "ADDISON":
        patterns.insert(0, r"\b(GRTW-\d{5,6}-\d{4})\b")
    if vendor == "TOTEM":
        patterns.insert(0, r"INVOICE\s+NO\.\s*:\s*([A-Z0-9][A-Z0-9\-/]+)")
    for pattern in patterns:
        regex = re.compile(pattern, re.IGNORECASE)
        match = regex.search(joined)
        if match:
            candidate = normalize_space(match.group(1))
            if candidate.upper() == "IRN":
                continue
            return candidate
    return ""


def _extract_invoice_date(lines: list[OcrLine], vendor: str) -> str:
    joined = _joined_text(lines)
    patterns = [
        r"DATE OF INVOICE\s*:\s*([0-9][0-9\s./-]+)",
        r"INVOICE DATE\s*:\s*([0-9][0-9\s./-]+)",
        r"DOCUMENT DATE\s*:\s*([0-9][0-9\s./-]+)",
        r"ACK DATE\s*:\s*([0-9A-Z][0-9A-Z\s./-]+)",
        r"DATED\s*([0-9]{1,2}[-/.][A-Z]{3}[-/.][0-9]{2,4})",
    ]
    if vendor == "CP":
        patterns.insert(0, r"\b\d{1,4}/\d{4}-\d{2}\s+(?:DATED\s*)?([0-9]{1,2}-[A-Z]{3}-[0-9]{2,4})\b")
    if vendor == "RR":
        patterns.insert(0, r"\bRR/\d{2}-\d{2}/\d+\s+([0-9]{1,2}-[A-Z]{3}-[0-9]{2})\b")
    for pattern in patterns:
        regex = re.compile(pattern, re.IGNORECASE)
        match = regex.search(joined)
        if match:
            date_token = _first_date_from_text(match.group(1))
            if date_token:
                return date_token
            return normalize_space(match.group(1))
    for line in lines:
        upper = line.text.upper()
        if "DATE OF INVOICE" in upper or "INVOICE DATE" in upper or "DATED" in upper:
            date_token = _first_date_from_text(line.text)
            if date_token:
                return date_token
    return ""


def _extract_customer_name(lines: list[OcrLine], vendor: str) -> str:
    stop_tokens = (
        "LR NO.",
        "GSTIN",
        "STATE",
        "TRANSPORTER NAME",
        "CONTACT DETAIL",
        "CONTACT NO",
        "PLACE OF SUPPLY",
        "DATE OF SUPPLY",
        "YOUR PO",
        "PO NO.",
        "INVOICE NO",
        "IRN",
    )
    if vendor == "ADDISON":
        candidates: list[str] = []
        for line in lines:
            upper = line.text.upper()
            if "ENTERPRISES" in upper and ("K.V" in upper or "K V" in upper or "KV" in upper):
                candidate = line.text
                for token in ("2A/18-B", "GRTW-", "TRANSPORTATION MODE", "OUR ORDER NO", "INVOICE NO / DATE"):
                    marker = candidate.upper().find(token)
                    if marker >= 0:
                        candidate = candidate[:marker]
                candidate = normalize_space(candidate.strip(" :-"))
                if candidate:
                    candidates.append(candidate)
        if candidates:
            return min(candidates, key=len)
    for index, line in enumerate(lines):
        upper = line.text.upper()
        if "DETAILS OF RECIPIENT (BILLED TO)" not in upper and "BILLED TO" not in upper and "BILL TO" not in upper:
            continue
        candidate = re.split(r"(?i)\b(?:DETAILS OF RECIPIENT \(BILLED TO\)|BILLED TO|BILL TO)\b", line.text, maxsplit=1)
        if len(candidate) > 1:
            inline = normalize_space(re.sub(r"^[^A-Z0-9]+", "", candidate[1]).strip(" :-"))
            inline_upper = inline.upper()
            for token in stop_tokens:
                if token in inline_upper:
                    inline = inline[: inline_upper.index(token)]
                    break
            inline = normalize_space(inline)
            if inline and re.search(r"[A-Z]", inline.upper()) and not re.fullmatch(r"[A-Z0-9-]{4,}", inline):
                return inline
        for next_line in lines[index + 1 : index + 5]:
            if next_line.page_index != line.page_index:
                break
            candidate = next_line.text
            upper_candidate = candidate.upper()
            for token in stop_tokens:
                if token in upper_candidate:
                    candidate = candidate[: upper_candidate.index(token)]
                    break
            candidate = normalize_space(candidate.strip(" :"))
            if candidate and not DATE_RE.fullmatch(candidate) and not re.fullmatch(r"[A-Z0-9-]{4,}", candidate):
                return candidate
    return ""


def _extract_tax_entries(lines: list[OcrLine], vendor: str) -> list[dict[str, Any]]:
    joined = _joined_text(lines)
    summary_patterns = {
        "IGST": [
            r"TOTAL\s+IGST(?:@\d+(?:\.\d+)?\s*%?)?\s+([0-9][0-9,]*\.\d{1,2})",
            r"IGST\s+AMOUNT\s+RS\.?\s*([0-9][0-9,]*\.\d{1,2})",
            r"IGST\s+([0-9][0-9,]*\.\d{1,2})",
        ],
        "CGST": [
            r"TOTAL\s+CGST(?:@\d+(?:\.\d+)?\s*%?)?\s+([0-9][0-9,]*\.\d{1,2})",
        ],
        "SGST": [
            r"TOTAL\s+SGST(?:@\d+(?:\.\d+)?\s*%?)?\s+([0-9][0-9,]*\.\d{1,2})",
        ],
    }
    summary_entries: list[dict[str, Any]] = []
    for ledger_name, patterns in summary_patterns.items():
        for pattern in patterns:
            match = re.search(pattern, joined, re.IGNORECASE)
            if match:
                summary_entries.append({"ledger_name": ledger_name, "amount": _float_from_token(match.group(1))})
                break
    if summary_entries and vendor != "PIDILITE":
        return summary_entries
    totals: dict[str, float] = {}
    seen: set[tuple[str, float]] = set()
    for line in lines:
        upper = line.text.upper()
        for ledger_name in ("IGST", "CGST", "SGST"):
            if ledger_name not in upper:
                continue
            amount = _last_amount_from_text(line.text)
            if amount is None:
                continue
            key = (ledger_name, round(amount, 2))
            if key in seen:
                continue
            seen.add(key)
            totals[ledger_name] = round(totals.get(ledger_name, 0.0) + amount, 2)
    return [{"ledger_name": ledger_name, "amount": amount} for ledger_name, amount in totals.items()]


def _extract_discount_fields(lines: list[OcrLine]) -> tuple[float, float]:
    for line in lines:
        upper = line.text.upper()
        if "DISCOUNT" not in upper:
            continue
        percent_match = re.search(r"DISCOUNT\s+([0-9]+(?:\.[0-9]+)?)\s*%", line.text, re.IGNORECASE)
        amount = _last_amount_from_text(line.text)
        return float(percent_match.group(1)) if percent_match else 0.0, amount or 0.0
    return 0.0, 0.0


def _extract_round_off(lines: list[OcrLine]) -> float:
    for line in lines:
        upper = line.text.upper()
        if "R.OFF" not in upper and "ROUND OFF" not in upper:
            continue
        return _last_amount_from_text(line.text) or 0.0
    return 0.0


def _extract_invoice_total(lines: list[OcrLine], vendor: str) -> float:
    joined = _joined_text(lines)
    patterns = (
        r"TOTAL INVOICE VALUE[:\s]+([0-9][0-9,]*\.\d{1,2})",
        r"INVOICE TOTAL[:\s]+([0-9][0-9,]*\.\d{1,2})",
        r"NET BILL VALUE[:\s]+([0-9][0-9,]*\.\d{1,2})",
        r"TOTAL VALUE[:\s]+([0-9][0-9,]*\.\d{1,2})",
        r"GRAND TOTAL\*?[:\s]+([0-9][0-9,]*\.\d{1,2})",
        r"TOTAL \?\s+([0-9][0-9,]*\.\d{1,2})",
    )
    for pattern in patterns:
        match = re.search(pattern, joined, re.IGNORECASE)
        if match:
            return _float_from_token(match.group(1))
    for line in reversed(lines):
        upper = line.text.upper()
        if "TOTAL INVOICE VALUE" in upper and "WORD" not in upper:
            amount = _last_amount_from_text(line.text)
            if amount is not None:
                return amount
    for line in reversed(lines):
        upper = line.text.upper()
        if "NET BILL VALUE" in upper:
            amount = _last_amount_from_text(line.text)
            if amount is not None:
                return amount
    return 0.0


def parse_vlm_invoice(vlm_result: dict[str, Any]) -> dict[str, Any]:
    lines = _trim_duplicate_copy_lines(build_ocr_lines(vlm_result))
    if not lines:
        raise ValueError("PaddleOCR-VL did not return any usable OCR lines.")

    warnings: list[str] = []
    vendor_name = _extract_vendor_name(lines)
    vendor = detect_vendor(vendor_name)
    description_rows: list[dict[str, Any]] = []
    numeric_rows: list[dict[str, Any]] = []
    try:
        description_rows, numeric_rows = _parse_vendor_items(vendor, lines)
    except Exception as exc:
        warnings.append(str(exc))

    discount_percent, discount_amount = _extract_discount_fields(lines)
    header_data = {
        "vendor_name": vendor_name,
        "invoice_number": _extract_invoice_number(lines, vendor),
        "invoice_date": _extract_invoice_date(lines, vendor),
        "customer_name": _extract_customer_name(lines, vendor),
        "discount_percent": discount_percent,
        "discount_amount": discount_amount,
        "invoice_total": _extract_invoice_total(lines, vendor),
        "tax_entries": _extract_tax_entries(lines, vendor),
        "round_off": _extract_round_off(lines),
    }
    return {
        "vendor": vendor,
        "header_data": header_data,
        "description_rows": description_rows,
        "numeric_rows": numeric_rows,
        "warnings": warnings,
        "line_count": len(lines),
        "page_count": int(vlm_result.get("page_count", 1) or 1),
        "raw_markdown": str(vlm_result.get("markdown", "") or ""),
        "page_markdown": list(vlm_result.get("page_markdown", []) or []),
        "layout": vlm_result.get("layout", []),
        "lines": [line.text for line in lines],
    }


__all__ = ["build_ocr_lines", "parse_vlm_invoice"]
