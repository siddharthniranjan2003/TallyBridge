from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore")

import cv2
import fitz
import numpy as np
from paddleocr import PaddleOCR

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from purchase_image_pipeline import (
    DEFAULT_COMPANY_NAME,
    DEFAULT_MATCH_THRESHOLD,
    build_purchase_queue_payload,
    build_voucher_payload,
    combine_ocr_items,
    decimal_value,
    detect_vendor,
    normalize_space,
    pretty_number,
    resolve_supabase_company_context,
    round2,
)


PADDLE_LANG = os.getenv("MINICPM_PADDLE_LANG", "en") or "en"
PADDLE_USE_ANGLE = (os.getenv("MINICPM_PADDLE_USE_ANGLE", "1") or "1").strip() not in {"0", "false", "False"}
PADDLE_MIN_CONFIDENCE = float(os.getenv("MINICPM_PADDLE_MIN_CONFIDENCE", "0.15") or "0.15")
PADDLE_REC_BATCH_NUM = max(1, int(os.getenv("MINICPM_PADDLE_REC_BATCH_NUM", "6") or "6"))
PADDLE_PDF_DPI = max(72, int(os.getenv("MINICPM_PADDLE_PDF_DPI", "300") or "300"))
PADDLE_PDF_MAX_PAGES = max(1, int(os.getenv("MINICPM_PADDLE_PDF_MAX_PAGES", "4") or "4"))
PADDLE_PAGE_GAP_PX = max(0, int(os.getenv("MINICPM_PADDLE_PAGE_GAP_PX", "24") or "24"))
PADDLE_HIGHRES_DET_LIMIT = max(960, int(os.getenv("MINICPM_PADDLE_HIGHRES_DET_LIMIT", "1920") or "1920"))
PADDLE_PREPROCESS = (os.getenv("MINICPM_PADDLE_PREPROCESS", "1") or "1").strip() not in {"0", "false", "False"}
PADDLE_PREPROCESS_DESKEW = (os.getenv("MINICPM_PADDLE_DESKEW", "1") or "1").strip() not in {"0", "false", "False"}
PADDLE_PREPROCESS_DENOISE = (os.getenv("MINICPM_PADDLE_DENOISE", "1") or "1").strip() not in {"0", "false", "False"}
PADDLE_PREPROCESS_SHARPEN = (os.getenv("MINICPM_PADDLE_SHARPEN", "1") or "1").strip() not in {"0", "false", "False"}
PADDLE_DESKEW_MAX_ANGLE = float(os.getenv("MINICPM_PADDLE_DESKEW_MAX_ANGLE", "10") or "10")

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
    "TOTAL TAX VALUE",
    "TOTAL GST VALUE",
    "TOTAL INVOICE VALUE",
    "DECLARATION",
    "AUTHORISED SIGNATORY",
)

_OCR_ENGINES: dict[str, PaddleOCR] = {}


@dataclass
class OCRToken:
    text: str
    confidence: float
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def height(self) -> float:
        return max(1.0, self.y1 - self.y0)


@dataclass
class OCRLine:
    tokens: list[OCRToken]

    @property
    def x0(self) -> float:
        return min(token.x0 for token in self.tokens)

    @property
    def y0(self) -> float:
        return min(token.y0 for token in self.tokens)

    @property
    def x1(self) -> float:
        return max(token.x1 for token in self.tokens)

    @property
    def y1(self) -> float:
        return max(token.y1 for token in self.tokens)

    @property
    def cy(self) -> float:
        return statistics.mean(token.cy for token in self.tokens)

    @property
    def text(self) -> str:
        ordered = sorted(self.tokens, key=lambda token: token.x0)
        return normalize_space(" ".join(token.text for token in ordered))


@dataclass
class TableBoundaries:
    code_start: float
    desc_start: float
    hsn_start: float
    qty_start: float
    unit_start: float
    rate_start: float
    amount_start: float
    width: float


def get_ocr_engine(mode: str = "raw") -> PaddleOCR:
    engine = _OCR_ENGINES.get(mode)
    if engine is not None:
        return engine

    kwargs: dict[str, Any] = {
        "use_angle_cls": PADDLE_USE_ANGLE,
        "lang": PADDLE_LANG,
        "rec_batch_num": PADDLE_REC_BATCH_NUM,
        "show_log": False,
    }
    if mode == "highres":
        kwargs["det_limit_side_len"] = PADDLE_HIGHRES_DET_LIMIT
    engine = PaddleOCR(**kwargs)
    _OCR_ENGINES[mode] = engine
    return engine


def decode_image_bytes(image_bytes: bytes) -> np.ndarray:
    image_array = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Could not decode purchase image bytes for PaddleOCR.")
    return image


def render_pdf_to_image(pdf_path: Path, dpi: int = PADDLE_PDF_DPI) -> tuple[np.ndarray, dict[str, Any]]:
    document = fitz.open(pdf_path)
    try:
        page_images: list[np.ndarray] = []
        for page_index in range(min(len(document), PADDLE_PDF_MAX_PAGES)):
            page = document[page_index]
            matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            page_images.append(decode_image_bytes(pixmap.tobytes("png")))
    finally:
        document.close()

    if not page_images:
        raise ValueError("Purchase PDF did not contain any renderable pages.")

    if len(page_images) == 1:
        return page_images[0], {"source_kind": "pdf", "page_count": 1, "render_dpi": dpi}

    max_width = max(image.shape[1] for image in page_images)
    total_height = sum(image.shape[0] for image in page_images) + (len(page_images) - 1) * PADDLE_PAGE_GAP_PX
    canvas = np.full((total_height, max_width, 3), 255, dtype=np.uint8)
    offset_y = 0
    for image in page_images:
        height, width = image.shape[:2]
        offset_x = max(0, (max_width - width) // 2)
        canvas[offset_y : offset_y + height, offset_x : offset_x + width] = image
        offset_y += height + PADDLE_PAGE_GAP_PX
    return canvas, {"source_kind": "pdf", "page_count": len(page_images), "render_dpi": dpi}


def load_source_image(source_path: Path) -> tuple[np.ndarray, dict[str, Any]]:
    if source_path.suffix.lower() == ".pdf":
        return render_pdf_to_image(source_path)
    return decode_image_bytes(source_path.read_bytes()), {"source_kind": "image", "page_count": 1, "render_dpi": 0}


def _deskew(gray: np.ndarray) -> np.ndarray:
    thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    coords = np.column_stack(np.where(thresh > 0))
    if len(coords) < 100:
        return gray
    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = 90.0 + angle
    if abs(angle) < 0.3 or abs(angle) > PADDLE_DESKEW_MAX_ANGLE:
        return gray
    h, w = gray.shape
    center = (w // 2, h // 2)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    return cv2.warpAffine(gray, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def preprocess_for_ocr(image: np.ndarray) -> np.ndarray:
    if not PADDLE_PREPROCESS:
        return image

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    if PADDLE_PREPROCESS_DESKEW:
        gray = _deskew(gray)

    if PADDLE_PREPROCESS_DENOISE:
        gray = cv2.fastNlMeansDenoising(gray, h=4, templateWindowSize=7, searchWindowSize=21)

    if PADDLE_PREPROCESS_SHARPEN:
        blur = cv2.GaussianBlur(gray, (0, 0), sigmaX=1)
        gray = cv2.addWeighted(gray, 1.3, blur, -0.3, 0)

    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def normalize_ocr_text(value: str) -> str:
    text = str(value or "")
    text = text.replace("—", "-").replace("–", "-").replace("“", '"').replace("”", '"')
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("|", " ")
    text = text.replace("₹", "")
    return normalize_space(text)


def compact_letters(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", normalize_ocr_text(value).upper())


def parse_numeric_token(value: str) -> float:
    text = normalize_ocr_text(value).upper()
    if not text:
        return 0.0
    text = text.replace("O", "0").replace("S", "5").replace("L", "1").replace("I", "1").replace("B", "8")
    text = text.replace(",", "")
    text = text.replace("..", ".")
    text = text.rstrip(".")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return 0.0
    return float(match.group(0))


def numeric_string(value: float) -> float | int:
    if abs(value - round(value)) < 0.00001:
        return int(round(value))
    return round(value, 2)


def sanitize_numeric_row(quantity: float, rate: float, amount: float) -> tuple[float, float, float]:
    """
    Cross-validate qty × rate ≈ amount and recover from OCR errors.

    - If qty=0 but rate>0 and amount>0: infer qty = round(amount / rate)
    - If rate has a lost decimal point (e.g. 47736 instead of 477.36):
      try shifting decimal left until qty × rate ≈ amount
    - All corrections require amount > 0 as the anchor (largest column, most reliably read)
    """
    if amount <= 0:
        return quantity, rate, amount

    # Infer qty from amount / rate when OCR misread it as 0
    if quantity <= 0 and rate > 0:
        inferred = round(amount / rate)
        if inferred > 0 and abs(inferred * rate - amount) / amount < 0.02:
            quantity = inferred

    # Recover lost decimal point in rate by trying /10, /100, /1000
    if quantity > 0 and rate > 0:
        expected = amount / quantity
        if abs(rate - expected) / max(expected, 1e-9) > 0.02:
            for divisor in (10.0, 100.0, 1000.0, 0.1):
                candidate = rate / divisor
                if candidate > 0 and abs(candidate - expected) / max(expected, 1e-9) < 0.02:
                    rate = round(candidate, 2)
                    break

    return quantity, rate, amount


def extract_number_from_line(text: str) -> float:
    numbers = re.findall(r"-?\d[\d,]*(?:\.\d+)?", normalize_ocr_text(text))
    if not numbers:
        return 0.0
    return parse_numeric_token(numbers[-1])


def infer_vendor_display_name(lines: list[OCRLine]) -> str:
    top_text = "\n".join(line.text for line in lines[:18])
    vendor = detect_vendor(top_text)
    return VENDOR_DISPLAY_NAMES.get(vendor, normalize_ocr_text(lines[0].text if lines else ""))


def extract_invoice_number(lines: list[OCRLine]) -> str:
    text = "\n".join(line.text for line in lines[:40])
    patterns = (
        r"SERIAL\s*NO\.?\s*INVOICE\s*[:\-]?\s*([A-Z0-9\/-]+)",
        r"INVOICE\s*NO\.?\s*[:\-]?\s*([A-Z0-9\/-]+)",
        r"SUPPLIER\s*INVOICE\s*NO\.?\s*[:\-]?\s*([A-Z0-9\/-]+)",
        r"BILL\s*NO\.?\s*[:\-]?\s*([A-Z0-9\/-]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return normalize_ocr_text(match.group(1)).strip(":- ")
    return ""


def extract_invoice_date(lines: list[OCRLine]) -> str:
    text = "\n".join(line.text for line in lines[:50])
    patterns = (
        r"DATE\s*OF\s*INVOICE\s*[:\-]?\s*(\d{1,2}[-/.][A-Z0-9]{2,3}[-/.]\d{2,4})",
        r"INVOICE\s*DATE\s*[:\-]?\s*(\d{1,2}[-/.][A-Z0-9]{2,3}[-/.]\d{2,4})",
        r"DATE\s*[:\-]?\s*(\d{1,2}[-/.][A-Z0-9]{2,3}[-/.]\d{2,4})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return normalize_ocr_text(match.group(1))
    return ""


def extract_customer_name(lines: list[OCRLine]) -> str:
    markers = ("DETAILS OF RECIPIENT", "BILLED TO", "BILL TO")
    for index, line in enumerate(lines):
        upper = normalize_ocr_text(line.text).upper()
        if any(marker in upper for marker in markers):
            for candidate in lines[index + 1 : index + 5]:
                text = normalize_ocr_text(candidate.text)
                upper_text = text.upper()
                if not text:
                    continue
                text = re.split(r"\b(?:LR\s*NO\.?|PO\s*NO\.?|DATE|TRANSPORTER\s*NAME)\b", text, maxsplit=1, flags=re.IGNORECASE)[0]
                text = normalize_ocr_text(text)
                upper_text = text.upper()
                if not text:
                    continue
                if any(marker in upper_text for marker in ("STATE", "GSTIN", "PAN", "CONTACT", "EMAIL", "DATE", "PO NO", "LR NO")):
                    continue
                if re.search(r"[A-Z]", upper_text):
                    return text
    return ""


def extract_discount(lines: list[OCRLine]) -> tuple[float, float]:
    footer_text = "\n".join(line.text for line in lines[-20:])
    match = re.search(
        r"DISCOUNT\s+(\d+(?:\.\d+)?)\s*(?:%|OF)?\s+(-?\d[\d,]*(?:\.\d+)?)",
        footer_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        return round(parse_numeric_token(match.group(1)), 2), round(abs(parse_numeric_token(match.group(2))), 2)
    return 0.0, 0.0


def extract_all_matches(lines: list[OCRLine], pattern: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    compiled = re.compile(pattern, flags=re.IGNORECASE)
    for line in lines:
        for match in compiled.findall(line.text):
            value = normalize_ocr_text(match).upper()
            if value and value not in seen:
                seen.add(value)
                values.append(value)
    return values


def extract_supplier_gstin(lines: list[OCRLine]) -> str:
    gstins = extract_all_matches(lines[:18], r"(\d{2}[A-Z]{5}\d{4}[A-Z]\d[Z][A-Z0-9])")
    return gstins[0] if gstins else ""


def extract_customer_gstin(lines: list[OCRLine]) -> str:
    billed_index = 0
    for index, line in enumerate(lines):
        if "BILLED TO" in line.text.upper() or "DETAILS OF RECIPIENT" in line.text.upper():
            billed_index = index
            break
    gstins = extract_all_matches(lines[billed_index : billed_index + 18], r"(\d{2}[A-Z]{5}\d{4}[A-Z]\d[Z][A-Z0-9])")
    return gstins[0] if gstins else ""


def extract_supplier_pan(lines: list[OCRLine]) -> str:
    pans = extract_all_matches(lines[:18], r"\b([A-Z]{5}\d{4}[A-Z])\b")
    return pans[0] if pans else ""


def extract_customer_pan(lines: list[OCRLine]) -> str:
    billed_index = 0
    for index, line in enumerate(lines):
        if "BILLED TO" in line.text.upper() or "DETAILS OF RECIPIENT" in line.text.upper():
            billed_index = index
            break
    pans = extract_all_matches(lines[billed_index : billed_index + 18], r"\b([A-Z]{5}\d{4}[A-Z])\b")
    return pans[0] if pans else ""


def extract_tax_entries(lines: list[OCRLine]) -> list[dict[str, Any]]:
    footer_lines = lines[-24:]
    found: dict[str, float] = {}
    for line in footer_lines:
        text = normalize_ocr_text(line.text).upper()
        for ledger_name in ("IGST", "CGST", "SGST"):
            if ledger_name not in text:
                continue
            amount = abs(extract_number_from_line(text))
            if amount > 0:
                found[ledger_name] = round(amount, 2)
    return [{"ledger_name": name, "amount": amount} for name, amount in found.items()]


def extract_round_off(lines: list[OCRLine]) -> float:
    footer_text = "\n".join(line.text for line in lines[-20:])
    match = re.search(r"(?:R\.?\s*OFF|ROUND\s*OFF)\s+(-?\d[\d,]*(?:\.\d+)?)", footer_text, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return round(parse_numeric_token(match.group(1)), 2)
    return 0.0


def extract_invoice_total(lines: list[OCRLine]) -> float:
    footer_text = "\n".join(line.text for line in lines[-24:])
    patterns = (
        r"TOTAL\s*INVOICE\s*VALUE(?:\s*\(.*?\))?\s+(-?\d[\d,]*(?:\.\d+)?)",
        r"TOTAL\s*INVOICE\s*VALUE\s*[:\-]?\s+(-?\d[\d,]*(?:\.\d+)?)",
    )
    for pattern in patterns:
        match = re.search(pattern, footer_text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return round(abs(parse_numeric_token(match.group(1))), 2)
    return 0.0


def extract_ocr_tokens(image: np.ndarray, mode: str = "raw") -> list[OCRToken]:
    result = get_ocr_engine(mode).ocr(image, cls=True)
    tokens: list[OCRToken] = []
    for page in result or []:
        for entry in page or []:
            if not entry or len(entry) < 2:
                continue
            box, payload = entry
            if not payload or len(payload) < 2:
                continue
            text, confidence = payload
            cleaned = normalize_ocr_text(text)
            if not cleaned:
                continue
            xs = [point[0] for point in box]
            ys = [point[1] for point in box]
            token = OCRToken(
                text=cleaned,
                confidence=float(confidence or 0.0),
                x0=float(min(xs)),
                y0=float(min(ys)),
                x1=float(max(xs)),
                y1=float(max(ys)),
            )
            if token.confidence >= PADDLE_MIN_CONFIDENCE:
                tokens.append(token)
    tokens.sort(key=lambda token: (token.cy, token.x0))
    return tokens


def build_ocr_lines(tokens: list[OCRToken]) -> list[OCRLine]:
    if not tokens:
        return []
    median_height = statistics.median(token.height for token in tokens)
    row_threshold = max(8.0, median_height * 0.4)

    rows: list[list[OCRToken]] = []
    current: list[OCRToken] = []
    current_center = 0.0
    for token in tokens:
        if not current:
            current = [token]
            current_center = token.cy
            continue
        if abs(token.cy - current_center) <= row_threshold:
            current.append(token)
            current_center = statistics.mean(item.cy for item in current)
            continue
        rows.append(sorted(current, key=lambda item: item.x0))
        current = [token]
        current_center = token.cy
    if current:
        rows.append(sorted(current, key=lambda item: item.x0))
    return [OCRLine(tokens=row) for row in rows]


def locate_table_header(lines: list[OCRLine]) -> tuple[int, int]:
    header_indexes: list[int] = []
    for index, line in enumerate(lines):
        compact = compact_letters(line.text)
        if (
            "DESCRIPTIONOFGOODS" in compact
            or "ITEMCODE" in compact
            or ("QTY" in compact and "RATE" in compact)
            or "HSNSAC" in compact
        ):
            header_indexes.append(index)
    if not header_indexes:
        raise ValueError("Could not locate the purchase item table in the OCR output.")
    header_start = min(header_indexes)
    header_end = min(len(lines) - 1, max(header_indexes) + 1)
    return header_start, header_end


def find_column_token(tokens: list[OCRToken], *keywords: str) -> OCRToken | None:
    for token in tokens:
        compact = compact_letters(token.text)
        if all(keyword in compact for keyword in keywords):
            return token
    return None


def infer_table_boundaries(header_tokens: list[OCRToken], page_width: float) -> TableBoundaries:
    item_code_token = find_column_token(header_tokens, "ITEM", "CODE")
    desc_token = find_column_token(header_tokens, "DESCRIPTION")
    hsn_token = find_column_token(header_tokens, "HSN")
    qty_token = find_column_token(header_tokens, "QTY")
    unit_token = find_column_token(header_tokens, "UNIT")
    rate_token = find_column_token(header_tokens, "RATE")
    amount_token = find_column_token(header_tokens, "TAXABLE") or find_column_token(header_tokens, "AMOUNT")

    desc_start = desc_token.x0 if desc_token else page_width * 0.20
    hsn_start = hsn_token.x0 if hsn_token else page_width * 0.58
    qty_start = qty_token.x0 if qty_token else page_width * 0.68
    unit_start = unit_token.x0 if unit_token else page_width * 0.75
    rate_start = rate_token.x0 if rate_token else page_width * 0.81
    amount_start = amount_token.x0 if amount_token else page_width * 0.91

    code_start = item_code_token.x0 if item_code_token else page_width * 0.08
    return TableBoundaries(
        code_start=code_start,
        desc_start=desc_start,
        hsn_start=hsn_start,
        qty_start=qty_start,
        unit_start=unit_start,
        rate_start=rate_start,
        amount_start=amount_start,
        width=page_width,
    )


def line_is_stop_marker(line: OCRLine) -> bool:
    text = normalize_ocr_text(line.text).upper()
    return any(marker in text for marker in STOP_ROW_MARKERS)


def parse_table_rows(lines: list[OCRLine], header_end: int, boundaries: TableBoundaries) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    description_rows: list[dict[str, Any]] = []
    numeric_rows: list[dict[str, Any]] = []
    warnings: list[str] = []

    current_description = ""
    current_item_code = ""
    current_numeric: dict[str, Any] | None = None

    for line in lines[header_end + 1 :]:
        if line_is_stop_marker(line):
            break

        buckets = {
            "serial": [],
            "item_code": [],
            "description": [],
            "hsn": [],
            "qty": [],
            "unit": [],
            "rate": [],
            "amount": [],
        }
        for token in sorted(line.tokens, key=lambda item: item.x0):
            if token.cx < boundaries.code_start:
                buckets["serial"].append(token.text)
            elif token.cx < boundaries.desc_start:
                buckets["item_code"].append(token.text)
            elif token.cx < boundaries.hsn_start:
                buckets["description"].append(token.text)
            elif token.cx < boundaries.qty_start:
                buckets["hsn"].append(token.text)
            elif token.cx < boundaries.unit_start:
                buckets["qty"].append(token.text)
            elif token.cx < boundaries.rate_start:
                buckets["unit"].append(token.text)
            elif token.cx < boundaries.amount_start:
                buckets["rate"].append(token.text)
            else:
                buckets["amount"].append(token.text)

        description_text = normalize_ocr_text(" ".join(buckets["description"]))
        item_code = normalize_ocr_text(" ".join(buckets["item_code"]))
        quantity = parse_numeric_token(" ".join(buckets["qty"]))
        unit = normalize_ocr_text(" ".join(buckets["unit"]))
        rate = parse_numeric_token(" ".join(buckets["rate"]))
        amount = parse_numeric_token(" ".join(buckets["amount"]))
        quantity, rate, amount = sanitize_numeric_row(quantity, rate, amount)

        has_numeric_values = quantity > 0 or rate > 0 or amount > 0
        if not description_text and not has_numeric_values and not item_code:
            continue

        if not has_numeric_values and description_text and current_numeric is not None:
            current_description = normalize_ocr_text(f"{current_description} {description_text}")
            continue

        if current_numeric is not None:
            description_rows.append(
                {
                    "item_code": current_item_code,
                    "raw_description": current_description,
                }
            )
            numeric_rows.append(current_numeric)

        current_item_code = item_code
        current_description = description_text
        current_numeric = {
            "quantity": numeric_string(quantity),
            "unit": unit,
            "rate": round(rate, 2),
            "amount": round(amount, 2),
        }

    if current_numeric is not None and current_description:
        description_rows.append(
            {
                "item_code": current_item_code,
                "raw_description": current_description,
            }
        )
        numeric_rows.append(current_numeric)

    if not description_rows:
        warnings.append("PaddleOCR could not extract any purchase item rows from the invoice table.")
    return description_rows, numeric_rows, warnings


def build_header_data(lines: list[OCRLine]) -> dict[str, Any]:
    vendor_name = infer_vendor_display_name(lines)
    header_data = {
        "vendor_name": vendor_name,
        "invoice_number": extract_invoice_number(lines),
        "invoice_date": extract_invoice_date(lines),
        "customer_name": extract_customer_name(lines),
        "supplier_gstin": extract_supplier_gstin(lines),
        "customer_gstin": extract_customer_gstin(lines),
        "supplier_pan": extract_supplier_pan(lines),
        "customer_pan": extract_customer_pan(lines),
        "discount_percent": 0,
        "discount_amount": 0,
        "invoice_total": extract_invoice_total(lines),
        "tax_entries": extract_tax_entries(lines),
        "round_off": extract_round_off(lines),
    }
    discount_percent, discount_amount = extract_discount(lines)
    header_data["discount_percent"] = discount_percent
    header_data["discount_amount"] = discount_amount
    return header_data


def build_table_payload(tokens: list[OCRToken], lines: list[OCRLine], header_data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    header_start, header_end = locate_table_header(lines)
    header_tokens: list[OCRToken] = []
    for line in lines[header_start : header_end + 1]:
        header_tokens.extend(line.tokens)
    page_width = max(token.x1 for token in tokens)
    boundaries = infer_table_boundaries(header_tokens, page_width)
    description_rows, numeric_rows, warnings = parse_table_rows(lines, header_end, boundaries)

    if not header_data["invoice_total"]:
        item_total = sum(row.get("amount", 0) or 0 for row in numeric_rows)
        tax_total = sum(entry.get("amount", 0) or 0 for entry in header_data["tax_entries"])
        header_data["invoice_total"] = round(item_total + tax_total, 2)

    return {"items": description_rows}, {"rows": numeric_rows}, warnings


def normalize_tax_signature(entries: list[dict[str, Any]]) -> dict[str, float]:
    signature: dict[str, float] = {}
    for entry in entries or []:
        name = normalize_ocr_text(entry.get("ledger_name", "")).upper()
        if not name:
            continue
        signature[name] = round(float(entry.get("amount", 0) or 0), 2)
    return signature


def compare_critical_headers(raw_header: dict[str, Any], high_header: dict[str, Any]) -> list[str]:
    disagreements: list[str] = []
    critical_fields = (
        "invoice_number",
        "invoice_date",
        "invoice_total",
        "round_off",
        "supplier_gstin",
        "customer_gstin",
        "supplier_pan",
        "customer_pan",
    )
    for field in critical_fields:
        raw_value = raw_header.get(field, "")
        high_value = high_header.get(field, "")
        if isinstance(raw_value, float):
            raw_value = round(raw_value, 2)
        if isinstance(high_value, float):
            high_value = round(high_value, 2)
        if normalize_ocr_text(str(raw_value)) != normalize_ocr_text(str(high_value)):
            disagreements.append(
                f"Aux OCR disagreement on {field}: raw={raw_value!r}, highres={high_value!r}. Using raw."
            )
    if normalize_tax_signature(raw_header.get("tax_entries", [])) != normalize_tax_signature(high_header.get("tax_entries", [])):
        disagreements.append(
            f"Aux OCR disagreement on tax_entries: raw={raw_header.get('tax_entries', [])!r}, highres={high_header.get('tax_entries', [])!r}. Using raw."
        )
    return disagreements


def build_purchase_ocr_payload(source_path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str], list[str], dict[str, Any]]:
    source_image, source_meta = load_source_image(source_path)
    processed_image = preprocess_for_ocr(source_image)
    tokens = extract_ocr_tokens(processed_image, mode="raw")
    if not tokens:
        raise ValueError("PaddleOCR did not detect any readable text in the purchase input.")
    lines = build_ocr_lines(tokens)
    header_data = build_header_data(lines)
    description_payload, numeric_payload, warnings = build_table_payload(tokens, lines, header_data)

    comparison: dict[str, Any] = {
        "strategy": "raw_primary",
        "raw_header": header_data,
        "highres_header": None,
        "critical_disagreements": [],
    }
    try:
        high_tokens = extract_ocr_tokens(source_image, mode="highres")
        if high_tokens:
            high_lines = build_ocr_lines(high_tokens)
            high_header = build_header_data(high_lines)
            comparison["highres_header"] = high_header
            comparison["critical_disagreements"] = compare_critical_headers(header_data, high_header)
            warnings.extend(comparison["critical_disagreements"])
    except Exception as exc:
        warnings.append(f"Aux highres PaddleOCR pass failed: {exc}")

    return (
        header_data,
        description_payload,
        numeric_payload,
        warnings,
        [line.text for line in lines],
        comparison | source_meta,
    )


def run_purchase_paddle_pipeline(
    input_path: Path,
    *,
    company_name: str = DEFAULT_COMPANY_NAME,
    push_mode: str = "none",
    min_match_score: float = DEFAULT_MATCH_THRESHOLD,
) -> dict[str, Any]:
    if push_mode != "none":
        raise ValueError("Direct push is disabled for the Paddle purchase pipeline. JSON output only.")

    started_at = time.time()
    header_data, description_payload, numeric_payload, paddle_warnings, raw_lines, comparison_info = build_purchase_ocr_payload(input_path)
    elapsed = round(time.time() - started_at, 2)
    warnings_out = list(paddle_warnings)
    vendor = ""
    try:
        vendor = detect_vendor(header_data.get("vendor_name", ""))
    except Exception as exc:
        warnings_out.append(str(exc))

    raw_items = []
    item_warnings: list[str] = []
    if vendor:
        raw_items, item_warnings = combine_ocr_items(
            vendor,
            description_payload.get("items", []),
            numeric_payload.get("rows", []),
        )
        warnings_out.extend(item_warnings)

    base_payload = {
        "ok": True,
        "status": "success",
        "mode": "purchase",
        "ocr_engine": "paddle",
        "image_path": str(input_path),
        "company_name": company_name,
        "master_source": "supabase",
        "vendor": vendor,
        "summary": {
            "row_count": len(raw_items) or max(len(description_payload.get("items", [])), len(numeric_payload.get("rows", []))),
            "weak_match_count": 0,
            "inference_seconds": elapsed,
            "push_mode": "none",
            "master_source": "supabase",
            "ocr_engine": "paddle",
            "matching_ready": bool(raw_items),
            "source_kind": comparison_info.get("source_kind", "image"),
        },
        "ocr": {
            "header": header_data,
            "description_rows": description_payload.get("items", []),
            "numeric_rows": numeric_payload.get("rows", []),
            "warnings": warnings_out,
            "raw_lines": raw_lines,
            "comparison": comparison_info,
        },
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
            "ocr_engine": "paddle",
            "company_name": company_name,
            "vendor": vendor,
            "party_name": "",
            "voucher_payload": None,
            "push_queue_payload": None,
            "matched_items": [],
            "weak_matches": [],
            "ocr": {
                "header": header_data,
                "warnings": warnings_out,
            },
        },
    }

    if not vendor or not raw_items:
        if not raw_items:
            warnings_out.append("Matching skipped because PaddleOCR did not yield structured purchase item rows.")
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
    except Exception as exc:
        warnings_out.append(f"Matching skipped: {exc}")
        base_payload["status"] = "partial_success"
        return base_payload

    return {
        **base_payload,
        "status": "success",
        "master_source": context.get("source", "supabase"),
        "summary": {
            "row_count": len(raw_items),
            "weak_match_count": len(build_result["weak_matches"]),
            "inference_seconds": elapsed,
            "push_mode": "none",
            "master_source": context.get("source", "supabase"),
            "ocr_engine": "paddle",
            "matching_ready": True,
            "source_kind": comparison_info.get("source_kind", "image"),
        },
        "ocr": {
            "header": header_data,
            "description_rows": description_payload.get("items", []),
            "numeric_rows": numeric_payload.get("rows", []),
            "warnings": warnings_out,
            "raw_lines": raw_lines,
            "comparison": comparison_info,
        },
        "matched_items": build_result["matched_items"],
        "weak_matches": build_result["weak_matches"],
        "party_name": build_result["party_name"],
        "voucher_payload": build_result["voucher_payload"],
        "push_queue_payload": build_purchase_queue_payload(company_name, build_result["voucher_payload"]),
        "tally_push": None,
        "n8n": {
            "type": "purchase",
            "ocr_engine": "paddle",
            "company_name": company_name,
            "vendor": vendor,
            "party_name": build_result["party_name"],
            "voucher_payload": build_result["voucher_payload"],
            "push_queue_payload": build_purchase_queue_payload(company_name, build_result["voucher_payload"]),
            "matched_items": build_result["matched_items"],
            "weak_matches": build_result["weak_matches"],
            "ocr": {
                "header": header_data,
                "warnings": warnings_out,
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the purchase OCR pipeline with PaddleOCR and return JSON.")
    parser.add_argument("--input", help="Path to the purchase invoice PDF or image.")
    parser.add_argument("--image", help="Backward-compatible alias for --input.")
    parser.add_argument("--company-name", default=DEFAULT_COMPANY_NAME, help="Company name used for Supabase master lookup.")
    parser.add_argument("--push-mode", default="none", help="Must remain 'none'; direct push is disabled.")
    parser.add_argument("--min-match-score", type=float, default=DEFAULT_MATCH_THRESHOLD, help="Weak-match threshold.")
    args = parser.parse_args()
    input_value = args.input or args.image
    if not input_value:
        parser.error("Provide --input with a purchase invoice PDF or image path.")

    payload = run_purchase_paddle_pipeline(
        Path(input_value).resolve(),
        company_name=normalize_space(args.company_name) or DEFAULT_COMPANY_NAME,
        push_mode=normalize_space(args.push_mode).lower() or "none",
        min_match_score=float(args.min_match_score),
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
