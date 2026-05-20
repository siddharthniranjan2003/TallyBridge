import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
from datetime import datetime
from difflib import SequenceMatcher
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse
import importlib.util

import requests
from pdf2image import convert_from_bytes
from PIL import Image
from rapidfuzz import fuzz

SCRIPT_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = SCRIPT_DIR / ".env"
PIPELINE_PATH = SCRIPT_DIR / "test_minicpm.py"
PURCHASE_PADDLE_RUNNER_PATH = SCRIPT_DIR / "purchase_paddle_runner.py"
DEFAULT_BENCHMARK = SCRIPT_DIR / "challan_updated.xlsx"
UPLOAD_DIR = SCRIPT_DIR / "uploads"
OUTPUT_DIR = SCRIPT_DIR / "outputs"
PARTY_NAME_PROMPT = """Read only the handwritten customer or party name at the top center of this challan.
Return only the party name text.

Rules:
- Ignore stamps, printed labels, date, number, side notes, and all item lines.
- Preserve abbreviations like H/W if they are written.
- Do not add explanations, labels, punctuation, or quotes.
- If one word is unclear, return the closest readable party name phrase only."""
PARTY_STOPWORDS = {"AND", "THE", "TO", "FOR", "OF"}


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


ENV_VALUES = load_env_file(ENV_PATH)


def env_value(name: str, default: str = "", aliases: tuple[str, ...] = ()) -> str:
    for key in (name, *aliases):
        runtime_value = os.getenv(key)
        if runtime_value is not None and str(runtime_value).strip():
            return str(runtime_value).strip()
        file_value = ENV_VALUES.get(key)
        if file_value is not None and file_value.strip():
            return file_value.strip()
    return default


SERVE_HOST = env_value("MINICPM_HTTP_HOST", "127.0.0.1")
SERVE_PORT = int(env_value("MINICPM_HTTP_PORT", "5003"))
MAX_UPLOAD_BYTES = int(env_value("MINICPM_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
PDF_RENDER_DPI = int(env_value("MINICPM_PDF_RENDER_DPI", "200"))
PDF_MAX_PAGES = max(1, int(env_value("MINICPM_PDF_MAX_PAGES", "4")))
PDF_PAGE_GAP_PX = max(0, int(env_value("MINICPM_PDF_PAGE_GAP_PX", "24")))
DEFAULT_PURCHASE_COMPANY = env_value("MINICPM_PURCHASE_COMPANY", env_value("TALLY_COMPANY", "K V ENTERPRISES"))
PADDLE_PYTHON_HINT = env_value("MINICPM_PADDLE_PYTHON", "")
GSHEET_SHEET_NAME = env_value("MINICPM_GSHEET_NAME", "Challan")
GSHEET_DATA_START_ROW = int(env_value("MINICPM_GSHEET_DATA_START_ROW", "5"))
SUPABASE_URL = env_value("SUPABASE_URL", "")
SUPABASE_KEY = env_value("SUPABASE_KEY", "", aliases=("SUPABASE_SERVICE_KEY", "API_KEY"))
PARTY_TABLE = env_value("MINICPM_PARTY_TABLE", "vouchers")
PARTY_COLUMN = env_value("MINICPM_PARTY_COLUMN", "party_name")
PARTY_QUERY_LIMIT = int(env_value("MINICPM_PARTY_QUERY_LIMIT", "100"))
PARTY_FETCH_PAGE_SIZE = int(env_value("MINICPM_PARTY_FETCH_PAGE_SIZE", "1000"))
PARTY_FETCH_MAX_PAGES = int(env_value("MINICPM_PARTY_FETCH_MAX_PAGES", "6"))
PARTY_MATCH_THRESHOLD = float(env_value("MINICPM_PARTY_MATCH_THRESHOLD", "78"))
PARTY_STRONG_TOKEN_THRESHOLD = int(env_value("MINICPM_PARTY_STRONG_TOKEN_THRESHOLD", "82"))
PARTY_WEAK_TOKEN_THRESHOLD = int(env_value("MINICPM_PARTY_WEAK_TOKEN_THRESHOLD", "90"))
PARTY_TOP_START_RATIO = float(env_value("MINICPM_PARTY_TOP_START_RATIO", "0.02"))
PARTY_TOP_END_RATIO = float(env_value("MINICPM_PARTY_TOP_END_RATIO", "0.22"))
PARTY_SIDE_MARGIN_RATIO = float(env_value("MINICPM_PARTY_SIDE_MARGIN_RATIO", "0.06"))
PARTY_GENERIC_TOKENS = {
    "ENTERPRISE",
    "ENTERPRISES",
    "AGENCY",
    "AGENCIES",
    "STORE",
    "HARDWARE",
    "TOOLS",
    "TOOLS",
    "INDIA",
    "TRADERS",
    "TRADING",
    "CO",
    "COMPANY",
    "PVT",
    "PRIVATE",
    "LTD",
    "LIMITED",
    "IMT",
}
PARTY_NAME_CACHE: list[str] | None = None
PARTY_OCR_OVERRIDES = {
    "SHREE SHYAM HW 2A/170": "SHREE SHYAM HARDWARE STORE",
}


def load_pipeline():
    spec = importlib.util.spec_from_file_location("testslm_test_minicpm", PIPELINE_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load pipeline from {PIPELINE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def safe_filename(name: str, fallback_stem: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip())
    if not cleaned:
        cleaned = fallback_stem
    return cleaned


def collapse_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", str(value).strip())


def image_bytes_to_b64(image_bytes: bytes) -> str:
    return base64.b64encode(image_bytes).decode("utf-8")


def crop_party_header_image(image_path: Path) -> bytes:
    with Image.open(image_path) as image:
        image = image.convert("RGB")
        width, height = image.size
        left = max(0, int(width * PARTY_SIDE_MARGIN_RATIO))
        right = min(width, int(width * (1 - PARTY_SIDE_MARGIN_RATIO)))
        top = max(0, int(height * PARTY_TOP_START_RATIO))
        bottom = min(height, int(height * PARTY_TOP_END_RATIO))
        if bottom <= top:
            bottom = min(height, top + max(1, int(height * 0.2)))
        cropped = image.crop((left, top, right, bottom))
        buffer = BytesIO()
        cropped.save(buffer, format="JPEG", quality=95)
        return buffer.getvalue()


def query_ollama_text(image_bytes: bytes, prompt: str, pipeline) -> str:
    payload = {
        "model": pipeline.MODEL,
        "think": False,
        "messages": [
            {
                "role": "system",
                "content": "You are a strict OCR extraction engine. Return only the requested text.",
            },
            {
                "role": "user",
                "content": prompt,
                "images": [image_bytes_to_b64(image_bytes)],
            },
        ],
        "stream": False,
        "options": {"temperature": 0},
    }
    response = requests.post(pipeline.OLLAMA_CHAT_URL, json=payload, timeout=pipeline.REQUEST_TIMEOUT)
    response.raise_for_status()
    content = response.json()["message"]["content"]
    return collapse_spaces(content)


def clean_party_name_text(value: str) -> str:
    text = collapse_spaces(value)
    text = re.sub(r"^[\"'`]+|[\"'`]+$", "", text)
    text = re.sub(r"(?i)^party\s*name\s*[:\-]?\s*", "", text)
    text = re.sub(r"(?i)^customer\s*name\s*[:\-]?\s*", "", text)
    text = re.sub(r"(?i)^name\s*[:\-]?\s*", "", text)
    text = re.sub(r"^\d+\.\s*", "", text)
    return text.strip(" -:|,")


def normalize_party_name(value: str) -> str:
    text = str(value or "").upper()
    text = re.sub(r"\bH\s*/\s*W\b", " HARDWARE ", text)
    text = re.sub(r"\bHW\b", " HARDWARE ", text)
    text = re.sub(r"\bHLW\b", " HARDWARE ", text)
    text = re.sub(r"\bHARDW\b", " HARDWARE ", text)
    text = re.sub(r"\bAGENCIES\b", " AGENCY ", text)
    text = text.replace("&", " AND ")
    text = re.sub(r"[^A-Z0-9 ]+", " ", text)
    return collapse_spaces(text)


def strong_party_tokens(value: str) -> list[str]:
    tokens = []
    for token in normalize_party_name(value).split():
        if token in PARTY_STOPWORDS or token in PARTY_GENERIC_TOKENS:
            continue
        if len(token) < 3:
            continue
        tokens.append(token)
    return list(dict.fromkeys(tokens))


def weak_party_tokens(value: str) -> list[str]:
    tokens = []
    for token in normalize_party_name(value).split():
        if token in PARTY_STOPWORDS:
            continue
        if token in PARTY_GENERIC_TOKENS or token == "HARDWARE":
            tokens.append(token)
    return list(dict.fromkeys(tokens))


def hardcoded_party_name_override(ocr_party_name: str) -> str:
    exact_ocr = collapse_spaces(str(ocr_party_name or "").upper())
    if exact_ocr in PARTY_OCR_OVERRIDES:
        return PARTY_OCR_OVERRIDES[exact_ocr]
    return ""


def supabase_headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }


def supabase_get(endpoint: str, params: dict[str, str]) -> requests.Response:
    with requests.Session() as session:
        session.trust_env = False
        response = session.get(
            endpoint,
            headers=supabase_headers(),
            params=params,
            timeout=20,
        )
    return response


def append_unique_party_candidates(candidates: list[str], seen: set[str], rows: list[dict]) -> None:
    for row in rows:
        value = collapse_spaces(row.get(PARTY_COLUMN, ""))
        if value and value not in seen:
            seen.add(value)
            candidates.append(value)


def build_party_query_terms(ocr_party_name: str) -> list[str]:
    strong_tokens = strong_party_tokens(ocr_party_name)
    normalized = normalize_party_name(ocr_party_name)

    terms: list[str] = []
    if len(strong_tokens) >= 2:
        terms.append(" ".join(strong_tokens[:2]))
    terms.extend(strong_tokens[:3])

    if "HARDWARE" in normalized:
        terms.extend(["HARDWARE", "H/W", "HW"])

    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        cleaned = collapse_spaces(term)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        deduped.append(cleaned)
    return deduped[:4]


def fetch_targeted_party_rows(endpoint: str, query_terms: list[str]) -> list[dict]:
    rows: list[dict] = []
    seen_values: set[str] = set()

    for term in query_terms:
        try:
            response = supabase_get(
                endpoint,
                {
                    "select": PARTY_COLUMN,
                    PARTY_COLUMN: f"ilike.*{term}*",
                    "order": f"{PARTY_COLUMN}.asc",
                    "limit": str(PARTY_QUERY_LIMIT),
                },
            )
            response.raise_for_status()
            term_rows = response.json()
        except requests.RequestException:
            continue

        for row in term_rows:
            value = collapse_spaces(row.get(PARTY_COLUMN, ""))
            if not value or value in seen_values:
                continue
            seen_values.add(value)
            rows.append(row)

    return rows


def fetch_supabase_party_candidates(ocr_party_name: str = "") -> list[str]:
    global PARTY_NAME_CACHE
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []

    endpoint = urljoin(SUPABASE_URL.rstrip("/") + "/", f"rest/v1/{PARTY_TABLE}")
    if PARTY_NAME_CACHE is None:
        candidates: list[str] = []
        seen: set[str] = set()

        for page_idx in range(PARTY_FETCH_MAX_PAGES):
            try:
                response = supabase_get(
                    endpoint,
                    {
                        "select": PARTY_COLUMN,
                        "order": f"{PARTY_COLUMN}.asc",
                        "limit": str(PARTY_FETCH_PAGE_SIZE),
                        "offset": str(page_idx * PARTY_FETCH_PAGE_SIZE),
                    },
                )
                response.raise_for_status()
                rows = response.json()
            except requests.RequestException:
                break

            if not rows:
                break

            append_unique_party_candidates(candidates, seen, rows)

            if len(rows) < PARTY_FETCH_PAGE_SIZE:
                break

        PARTY_NAME_CACHE = candidates

    candidates = list(PARTY_NAME_CACHE or [])
    seen = set(candidates)
    query_terms = build_party_query_terms(ocr_party_name)
    if query_terms:
        append_unique_party_candidates(candidates, seen, fetch_targeted_party_rows(endpoint, query_terms))
    return candidates


def token_best_similarity(token: str, candidate_tokens: list[str]) -> int:
    if not token or not candidate_tokens:
        return 0
    return max(int(fuzz.ratio(token, candidate_token)) for candidate_token in candidate_tokens)


def party_match_features(ocr_party_name: str, candidate: str) -> dict:
    normalized_ocr = normalize_party_name(ocr_party_name)
    normalized_candidate = normalize_party_name(candidate)
    candidate_tokens = normalized_candidate.split()
    strong_tokens = strong_party_tokens(ocr_party_name)
    weak_tokens = weak_party_tokens(ocr_party_name)

    strong_match_scores = [token_best_similarity(token, candidate_tokens) for token in strong_tokens]
    weak_match_scores = [token_best_similarity(token, candidate_tokens) for token in weak_tokens]
    strong_matches = sum(1 for score in strong_match_scores if score >= PARTY_STRONG_TOKEN_THRESHOLD)
    weak_matches = sum(1 for score in weak_match_scores if score >= PARTY_WEAK_TOKEN_THRESHOLD)
    strong_ratio = (strong_matches / len(strong_tokens)) if strong_tokens else 0.0
    weak_ratio = (weak_matches / len(weak_tokens)) if weak_tokens else 0.0

    wratio = float(fuzz.WRatio(normalized_ocr, normalized_candidate))
    token_set_score = float(fuzz.token_set_ratio(normalized_ocr, normalized_candidate))
    partial_score = float(fuzz.partial_ratio(normalized_ocr, normalized_candidate))
    seq_score = SequenceMatcher(None, normalized_ocr, normalized_candidate).ratio() * 100.0

    first_token_bonus = 0.0
    if strong_tokens:
        first_token_bonus = (
            10.0 if token_best_similarity(strong_tokens[0], candidate_tokens) >= PARTY_STRONG_TOKEN_THRESHOLD else 0.0
        )

    generic_only_penalty = 22.0 if strong_tokens and strong_matches == 0 and weak_matches > 0 else 0.0
    weak_only_penalty = 10.0 if strong_tokens and strong_matches < max(1, len(strong_tokens) // 2) else 0.0

    score = (
        (wratio * 0.38)
        + (token_set_score * 0.24)
        + (partial_score * 0.10)
        + (seq_score * 0.08)
        + (strong_ratio * 30.0)
        + (weak_ratio * 6.0)
        + first_token_bonus
        - generic_only_penalty
        - weak_only_penalty
    )

    return {
        "party_name": candidate,
        "score": round(score, 2),
        "strong_ratio": round(strong_ratio, 3),
        "weak_ratio": round(weak_ratio, 3),
        "strong_matches": strong_matches,
        "wratio": round(wratio, 2),
    }


def rank_party_candidates(ocr_party_name: str, candidates: list[str]) -> list[dict]:
    scored = [party_match_features(ocr_party_name, candidate) for candidate in candidates]
    return sorted(scored, key=lambda item: (item["score"], item["strong_ratio"], item["wratio"]), reverse=True)


def extract_party_name(image_path: Path, pipeline) -> dict:
    try:
        cropped_image = crop_party_header_image(image_path)
        ocr_party_name = clean_party_name_text(query_ollama_text(cropped_image, PARTY_NAME_PROMPT, pipeline))
    except Exception as exc:
        return {
            "ocr_text": "",
            "matched_name": "",
            "score": 0.0,
            "source": "error",
            "candidates": [],
            "error": str(exc),
        }

    if not ocr_party_name:
        return {
            "ocr_text": "",
            "matched_name": "",
            "score": 0.0,
            "source": "empty_ocr",
            "candidates": [],
            "error": "",
        }

    overridden_name = hardcoded_party_name_override(ocr_party_name)
    if overridden_name:
        return {
            "ocr_text": ocr_party_name,
            "matched_name": overridden_name,
            "score": 999.0,
            "source": "hardcoded_override",
            "candidates": [{"party_name": overridden_name, "score": 999.0}],
            "error": "",
        }

    if not SUPABASE_URL or not SUPABASE_KEY:
        return {
            "ocr_text": ocr_party_name,
            "matched_name": "",
            "score": 0.0,
            "source": "supabase_not_configured",
            "candidates": [],
            "error": "",
        }

    candidates = fetch_supabase_party_candidates(ocr_party_name)
    ranked_candidates = rank_party_candidates(ocr_party_name, candidates)
    if ranked_candidates and (
        ranked_candidates[0]["score"] >= PARTY_MATCH_THRESHOLD
        or ranked_candidates[0]["strong_ratio"] >= 0.99
    ):
        best = ranked_candidates[0]
        return {
            "ocr_text": ocr_party_name,
            "matched_name": best["party_name"],
            "score": best["score"],
            "source": "supabase_fuzzy",
            "candidates": ranked_candidates[:5],
            "error": "",
        }

    return {
        "ocr_text": ocr_party_name,
        "matched_name": "",
        "score": 0.0,
        "source": "no_supabase_match",
        "candidates": ranked_candidates[:5],
        "error": "",
    }


def markdown_table(rows: list[dict]) -> str:
    headers = ["#", "MiniCPM_Read", "Stock_Matched", "Score", "Correct_Stock_Name", "Verdict"]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row["line_no"]),
                    row["minicpm_read"],
                    row["stock_matched"],
                    str(row["score"]),
                    row["correct_stock_name"],
                    row["verdict"],
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def serialize_rows(prediction_rows) -> list[dict]:
    rows = []
    for row in prediction_rows:
        rows.append(
            {
                "line_no": row.line_no,
                "minicpm_read": row.pre_fuzzy_name,
                "stock_matched": row.predicted_name,
                "score": row.similarity,
                "correct_stock_name": row.expected_name,
                "verdict": row.status,
                "confidence": row.confidence,
                "qty_text": row.qty_text,
                "raw_description": row.raw_description,
                "normalized_description": row.normalized_description,
                "alternates": row.alternates,
            }
        )
    return rows


def prettify_unit_label(label: str) -> str:
    label = (label or "").strip().upper()
    mapped = {
        "P": "Ps",
        "PS": "Ps",
        "PCS": "Ps",
        "PC": "Ps",
        "PSJ": "Ps",
        "PU": "Ps",
        "QPS": "Ps",
        "IPS": "Ps",
        "SET": "Set",
        "SEP": "Set",
        "SEE": "Set",
        "PAIR": "Pair",
        "PAIRS": "Pair",
        "BOX": "Box",
        "B": "Box",
        "BX": "Box",
        "BOB": "Box",
        "KG": "Kg",
        "NOS": "Nos",
    }
    return mapped.get(label, label.title() if label else "")


def format_unit_from_qty_text(qty_text: str) -> str:
    text = re.sub(r"\s+", " ", str(qty_text or "").upper()).strip()
    if not text:
        return ""

    tokens = text.split()
    best = None
    for idx, token in enumerate(tokens):
        stripped = token.strip()
        digits = re.findall(r"\d+(?:\.\d+)?", stripped)
        letters = re.sub(r"[^A-Z]+", "", stripped)
        if not digits and not letters:
            continue

        score = 0
        if digits:
            score += len(digits[0]) * 10
        if letters:
            score += 12 + len(letters)
        if len(stripped) == 1 and stripped.isdigit() and len(tokens) > 1:
            score -= 20
        if best is None or score > best[0]:
            best = (score, idx, digits[0] if digits else "", letters)

    if best is None:
        return collapse_spaces(text.title())

    _, idx, quantity, letters = best
    if not quantity and idx > 0:
        prev_digits = re.findall(r"\d+(?:\.\d+)?", tokens[idx - 1])
        if prev_digits:
            quantity = prev_digits[0]
    if not letters and idx + 1 < len(tokens):
        next_letters = re.sub(r"[^A-Z]+", "", tokens[idx + 1])
        if next_letters:
            letters = next_letters

    quantity = quantity or "1"
    if quantity.endswith(".0"):
        quantity = quantity[:-2]
    unit = prettify_unit_label(letters)
    return f"{quantity} {unit}".strip()


def build_gsheet_rows(rows: list[dict], sheet_name: str = GSHEET_SHEET_NAME, start_row: int = GSHEET_DATA_START_ROW) -> dict:
    structured_rows = []
    values = []

    for idx, row in enumerate(rows, start=0):
        sheet_row_number = start_row + idx
        serial_no = idx + 1
        unit = format_unit_from_qty_text(row.get("qty_text", ""))
        before_fuzzy = row.get("minicpm_read", "")
        matched = row.get("stock_matched", "")
        row_payload = {
            "row_number": sheet_row_number,
            "A": serial_no,
            "B": unit,
            "C": before_fuzzy,
            "D": matched,
        }
        structured_rows.append(row_payload)
        values.append([serial_no, unit, before_fuzzy, matched])

    end_row = start_row + len(structured_rows) - 1 if structured_rows else start_row
    return {
        "sheet_name": sheet_name,
        "data_start_row": start_row,
        "headers": {
            "A": "S.No.",
            "B": "Unit",
            "C": "Item Description",
            "D": "Correct Item Description",
        },
        "columns": {
            "serial_no": "A",
            "unit": "B",
            "before_fuzzy_item": "C",
            "matched_item": "D",
        },
        "write_range": f"{sheet_name}!A{start_row}:D{end_row}",
        "clear_range": f"{sheet_name}!A{start_row}:D500",
        "rows": structured_rows,
        "values": values,
    }


def summarize_rows(rows: list[dict], model: str, inference_seconds: float) -> dict:
    exact = sum(1 for row in rows if row["verdict"] == "EXACT")
    close_or_better = sum(1 for row in rows if row["verdict"] in {"EXACT", "CLOSE"})
    different = sum(1 for row in rows if row["verdict"] == "DIFFERENT")
    no_ref = sum(1 for row in rows if row["verdict"] == "NO_REF")
    return {
        "model": model,
        "row_count": len(rows),
        "exact": exact,
        "close_or_better": close_or_better,
        "different": different,
        "no_ref": no_ref,
        "inference_seconds": inference_seconds,
    }


def save_result(output_dir: Path, stem: str, payload: dict) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / f"{stem}_result.json"
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return result_path


def is_pdf_bytes(payload: bytes) -> bool:
    return payload.startswith(b"%PDF-")


def is_image_bytes(payload: bytes) -> bool:
    return (
        payload.startswith(b"\xff\xd8\xff")
        or payload.startswith(b"\x89PNG\r\n\x1a\n")
        or payload.startswith(b"RIFF") and b"WEBP" in payload[:16]
    )


def infer_upload_kind(content: bytes, filename: str, content_type: str) -> str:
    lower_content_type = (content_type or "").lower()
    suffix = Path(filename or "").suffix.lower()
    if lower_content_type.startswith("application/pdf") or suffix == ".pdf" or is_pdf_bytes(content):
        return "pdf"
    if lower_content_type.startswith("image/") or suffix in {".jpg", ".jpeg", ".png", ".webp"} or is_image_bytes(content):
        return "image"
    if lower_content_type.startswith("application/octet-stream"):
        if is_pdf_bytes(content):
            return "pdf"
        if is_image_bytes(content):
            return "image"
    return "unknown"


def parse_json_body(body: bytes) -> tuple[bytes, str, str]:
    payload = json.loads(body.decode("utf-8"))
    file_b64 = payload.get("image_base64") or payload.get("file_base64") or ""
    if not file_b64:
        raise ValueError("JSON body must include image_base64 or file_base64")
    file_bytes = base64.b64decode(file_b64)
    filename = payload.get("filename", "upload.jpg")
    upload_kind = infer_upload_kind(file_bytes, filename, payload.get("content_type", ""))
    if upload_kind == "unknown":
        raise ValueError("JSON body must include an image or PDF payload")
    return file_bytes, filename, upload_kind


def parse_multipart_body(body: bytes, content_type: str) -> tuple[bytes, str, str]:
    message = BytesParser(policy=default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
    )
    for part in message.iter_parts():
        disposition = part.get("Content-Disposition", "")
        if "form-data" not in disposition:
            continue
        filename = part.get_filename()
        content = part.get_payload(decode=True) or b""
        upload_kind = infer_upload_kind(content, filename or "", part.get_content_type() or "")
        if filename or upload_kind != "unknown":
            if upload_kind == "unknown":
                raise ValueError("Multipart request must include an image or PDF file")
            fallback_ext = ".pdf" if upload_kind == "pdf" else ".jpg"
            return content, filename or ("upload" + fallback_ext), upload_kind
    raise ValueError("Multipart request did not include an image or PDF file")


def render_pdf_upload(pdf_bytes: bytes, output_path: Path) -> Path:
    pages = convert_from_bytes(pdf_bytes, dpi=PDF_RENDER_DPI, first_page=1, last_page=PDF_MAX_PAGES)
    if not pages:
        raise ValueError("PDF upload did not contain any renderable pages")

    converted_pages = [page.convert("RGB") for page in pages]
    max_width = max(page.width for page in converted_pages)
    total_height = sum(page.height for page in converted_pages) + (PDF_PAGE_GAP_PX * (len(converted_pages) - 1))
    canvas = Image.new("RGB", (max_width, total_height), "white")
    offset_y = 0
    for page in converted_pages:
        offset_x = max(0, (max_width - page.width) // 2)
        canvas.paste(page, (offset_x, offset_y))
        offset_y += page.height + PDF_PAGE_GAP_PX

    canvas.save(output_path, format="JPEG", quality=95)
    return output_path


def materialize_upload(
    body: bytes,
    content_type: str,
    fallback_name: str,
    upload_dir: Path,
    *,
    render_pdf: bool = True,
) -> tuple[Path, Path | None, str]:
    raw_content_type = content_type or ""
    lower_content_type = raw_content_type.lower()
    if lower_content_type.startswith("application/json"):
        file_bytes, filename, upload_kind = parse_json_body(body)
    elif lower_content_type.startswith("multipart/form-data"):
        file_bytes, filename, upload_kind = parse_multipart_body(body, raw_content_type)
    else:
        ext = ".bin"
        if "/" in lower_content_type:
            ext = "." + lower_content_type.split("/", 1)[1].split(";", 1)[0].strip()
        filename = fallback_name + ext
        file_bytes = body
        upload_kind = infer_upload_kind(file_bytes, filename, raw_content_type)
        if upload_kind == "unknown":
            raise ValueError(f"Unsupported Content-Type: {raw_content_type or 'missing'}")
        if ext == ".bin":
            filename = fallback_name + (".pdf" if upload_kind == "pdf" else ".jpg")

    safe_name = safe_filename(filename, f"{fallback_name}.jpg")
    upload_dir.mkdir(parents=True, exist_ok=True)
    original_path = upload_dir / safe_name
    original_path.write_bytes(file_bytes)

    if upload_kind == "pdf":
        if not render_pdf:
            return original_path, original_path, upload_kind
        rendered_path = upload_dir / f"{Path(safe_name).stem}_rendered.jpg"
        return render_pdf_upload(file_bytes, rendered_path), original_path, upload_kind

    return original_path, None, upload_kind


def materialize_input_path(
    input_path: Path,
    fallback_name: str,
    upload_dir: Path,
    *,
    render_pdf: bool = True,
) -> tuple[Path, Path | None, str]:
    file_bytes = input_path.read_bytes()
    upload_kind = infer_upload_kind(file_bytes, input_path.name, "")
    if upload_kind == "unknown":
        raise ValueError(f"Unsupported input file type: {input_path.suffix or input_path.name}")

    safe_name = safe_filename(input_path.name, fallback_name)
    upload_dir.mkdir(parents=True, exist_ok=True)
    original_path = upload_dir / safe_name
    original_path.write_bytes(file_bytes)

    if upload_kind == "pdf":
        if not render_pdf:
            return original_path, original_path, upload_kind
        rendered_path = upload_dir / f"{Path(safe_name).stem}_rendered.jpg"
        return render_pdf_upload(file_bytes, rendered_path), original_path, upload_kind

    return input_path, None, upload_kind


def run_pipeline_for_image(image_path: Path, benchmark_path: Path | None = None) -> dict:
    pipeline = load_pipeline()
    stock = pipeline.load_stock(pipeline.STOCK_CSV)
    family_views = pipeline.build_family_views(stock)
    image_b64 = pipeline.load_image_b64(image_path)
    raw_text, elapsed = pipeline.query_ollama_chat(image_b64)
    raw_text = pipeline.clean_ocr_text_layout_noise(raw_text)
    items = pipeline.parse_ocr_items(raw_text)

    expected: list[str] = []
    if benchmark_path and benchmark_path.exists():
        expected = pipeline.load_benchmark(benchmark_path)

    prediction_rows = pipeline.build_prediction_rows(items, expected, family_views)
    rows = serialize_rows(prediction_rows)
    summary = summarize_rows(rows, pipeline.MODEL, elapsed)
    gsheet = build_gsheet_rows(rows)
    party_name_match = extract_party_name(image_path, pipeline)
    gsheet["party_name"] = party_name_match["matched_name"]
    gsheet["party_name_cell"] = "A1"

    return {
        "ok": True,
        "status": "success",
        "mode": "sale",
        "image_path": str(image_path),
        "benchmark_path": str(benchmark_path) if benchmark_path and benchmark_path.exists() else "",
        "summary": summary,
        "raw_ocr_text": raw_text,
        "party_name": party_name_match["matched_name"],
        "party_name_match": party_name_match,
        "rows": rows,
        "table_markdown": markdown_table(rows),
        "gsheet": gsheet,
    }


def resolve_paddle_python_executable() -> str:
    if PADDLE_PYTHON_HINT:
        candidate = Path(PADDLE_PYTHON_HINT).expanduser()
        if candidate.exists():
            return str(candidate)

    current_python = Path(sys.executable)
    if current_python.exists() and sys.version_info[:2] <= (3, 12):
        return str(current_python)

    py_launcher = shutil.which("py")
    if py_launcher:
        try:
            result = subprocess.run(
                [py_launcher, "-3.11", "-c", "import sys; print(sys.executable)"],
                capture_output=True,
                text=True,
                timeout=20,
                check=True,
            )
            resolved = Path((result.stdout or "").strip())
            if resolved.exists():
                return str(resolved)
        except Exception:
            pass

    default_python311 = Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python311" / "python.exe"
    if default_python311.exists():
        return str(default_python311)

    raise RuntimeError(
        "Could not locate a Python 3.11 runtime for PaddleOCR. Set MINICPM_PADDLE_PYTHON to a working Python executable."
    )


def extract_json_object(raw_text: str) -> dict:
    text = str(raw_text or "").strip()
    if not text:
        raise ValueError("Purchase helper returned an empty response.")
    match = re.search(r"(\{.*\})", text, flags=re.DOTALL)
    if not match:
        raise ValueError(f"Purchase helper did not return JSON. Raw output: {text[:500]}")
    return json.loads(match.group(1))


def run_purchase_pipeline(image_path: Path, company_name: str, push_mode: str) -> dict:
    helper_python = resolve_paddle_python_executable()
    if not PURCHASE_PADDLE_RUNNER_PATH.exists():
        raise RuntimeError(f"Purchase Paddle runner is missing at {PURCHASE_PADDLE_RUNNER_PATH}")

    result = subprocess.run(
        [
            helper_python,
            str(PURCHASE_PADDLE_RUNNER_PATH),
            "--input",
            str(image_path),
            "--company-name",
            company_name,
            "--push-mode",
            push_mode or "none",
        ],
        capture_output=True,
        text=True,
        cwd=str(SCRIPT_DIR),
        timeout=900,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        raise RuntimeError(stderr or stdout or "Purchase Paddle runner failed.")
    return extract_json_object(result.stdout)


def run_purchase_ocr_header(image_path: Path) -> dict:
    """Pass 1 for paddle: extract invoice header only, no stock matching."""
    helper_python = resolve_paddle_python_executable()
    if not PURCHASE_PADDLE_RUNNER_PATH.exists():
        raise RuntimeError(f"Purchase Paddle runner is missing at {PURCHASE_PADDLE_RUNNER_PATH}")
    result = subprocess.run(
        [helper_python, str(PURCHASE_PADDLE_RUNNER_PATH), "--input", str(image_path), "--ocr-only"],
        capture_output=True,
        text=True,
        cwd=str(SCRIPT_DIR),
        timeout=300,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        raise RuntimeError(stderr or stdout or "Purchase Paddle OCR header extraction failed.")
    return extract_json_object(result.stdout)


def check_duplicacy(invoice_number: str) -> bool:
    if not invoice_number or not SUPABASE_URL or not SUPABASE_KEY:
        return False
    try:
        resp = requests.get(
            f"{SUPABASE_URL.rstrip('/')}/rest/v1/vouchers",
            params={"voucher_number": f"eq.{invoice_number}", "select": "id"},
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            timeout=10,
        )
        return resp.status_code == 200 and len(resp.json()) > 0
    except Exception:
        return False


def request_options(path: str) -> dict[str, str]:
    parsed = urlparse(path or "")
    query = parse_qs(parsed.query)
    mode = collapse_spaces(query.get("type", ["sale"])[0]).lower() or "sale"
    if mode not in {"sale", "purchase"}:
        raise ValueError("type must be 'sale' or 'purchase'")

    push_mode = collapse_spaces(query.get("push", [""])[0]).lower()
    if not push_mode:
        push_mode = "none"
    if push_mode != "none":
        raise ValueError("Direct push is disabled on this endpoint. Use the returned JSON payload downstream.")

    company_name = collapse_spaces(query.get("company", [DEFAULT_PURCHASE_COMPANY])[0]) or DEFAULT_PURCHASE_COMPANY
    check = collapse_spaces(query.get("check", [""])[0]).lower()
    ocr_engine = collapse_spaces(query.get("ocr", ["paddle"])[0]).lower() or "paddle"
    if ocr_engine not in {"paddle", "vlm"}:
        raise ValueError("ocr must be 'paddle' or 'vlm'")
    return {
        "type": mode,
        "push_mode": push_mode,
        "company_name": company_name,
        "check": check,
        "ocr": ocr_engine,
    }


class MiniCPMHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        request_path = urlparse(self.path).path.rstrip("/")
        if request_path in {"", "/health"}:
            self._send_json(
                200,
                {
                    "ok": True,
                    "status": "healthy",
                    "service": "n8n_minicpm_server",
                    "port": SERVE_PORT,
                    "pipeline_path": str(PIPELINE_PATH),
                    "benchmark_path": str(DEFAULT_BENCHMARK) if DEFAULT_BENCHMARK.exists() else "",
                    "supported_types": ["sale", "purchase"],
                    "supported_uploads": {
                        "sale": ["image"],
                        "purchase": ["image", "pdf"],
                    },
                    "default_purchase_company": DEFAULT_PURCHASE_COMPANY,
                    "purchase_behavior": "parse_only_json",
                    "purchase_ocr_engines": ["paddle", "vlm"],
                    "default_purchase_ocr_engine": "paddle",
                    "purchase_pdf_render": "pymupdf_300dpi",
                    "purchase_ocr_compare_pass": "paddle_highres_1920_aux",
                },
            )
            return
        self._send_json(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        try:
            options = request_options(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_UPLOAD_BYTES:
                self._send_json(400, {"ok": False, "error": "Bad request size"})
                return

            body = self.rfile.read(length)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

            image_path, original_upload_path, upload_kind = materialize_upload(
                body,
                self.headers.get("Content-Type", ""),
                f"upload_{timestamp}",
                UPLOAD_DIR,
                render_pdf=options["type"] != "purchase",
            )
            if upload_kind == "pdf" and options["type"] != "purchase":
                raise ValueError("PDF upload is currently supported only for type=purchase")

            if options["type"] == "purchase":
                purchase_input = (
                    original_upload_path
                    if upload_kind == "pdf" and original_upload_path
                    else image_path
                )
                if options.get("check") == "duplicacy":
                    # Pass 1: fast OCR-only to extract invoice number
                    if options["ocr"] == "vlm":
                        from purchase_ocrvl_pipeline import run_purchase_vl_ocr_header_only
                        ocr_header = run_purchase_vl_ocr_header_only(purchase_input)
                    else:
                        ocr_header = run_purchase_ocr_header(purchase_input)
                    invoice_number = ocr_header.get("invoice_number", "")
                    if check_duplicacy(invoice_number):
                        self._send_json(200, {"duplicacy": True, "invoice_number": invoice_number})
                        return
                # Pass 2 (or normal run): full pipeline
                if options["ocr"] == "vlm":
                    from purchase_ocrvl_pipeline import run_purchase_vl_pipeline
                    payload = run_purchase_vl_pipeline(
                        purchase_input,
                        company_name=options["company_name"],
                    )
                else:
                    payload = run_purchase_pipeline(
                        image_path=purchase_input,
                        company_name=options["company_name"],
                        push_mode=options["push_mode"],
                    )
            else:
                payload = run_pipeline_for_image(
                    image_path=image_path,
                    benchmark_path=DEFAULT_BENCHMARK if DEFAULT_BENCHMARK.exists() else None,
                )
            payload["upload_kind"] = upload_kind
            payload["source_upload_path"] = str(original_upload_path or image_path)
            payload["saved_result"] = str(save_result(OUTPUT_DIR, (original_upload_path or image_path).stem, payload))
            if options["type"] == "sale":
                payload["n8n"] = {
                    "party_name": payload["party_name"],
                    "party_name_match": payload["party_name_match"],
                    "row_count": payload["summary"]["row_count"],
                    "exact": payload["summary"]["exact"],
                    "close_or_better": payload["summary"]["close_or_better"],
                    "different": payload["summary"]["different"],
                    "rows": payload["rows"],
                    "table_markdown": payload["table_markdown"],
                    "gsheet": payload["gsheet"],
                }
            self._send_json(200, payload)
        except Exception as exc:
            self._send_json(
                400 if isinstance(exc, ValueError) else 500,
                {
                    "ok": False,
                    "status": "error",
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )

    def _send_json(self, status_code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Serve or run the MiniCPM challan matcher for n8n image uploads."
    )
    parser.add_argument("--input", help="Run once on a local image path instead of starting the server.")
    parser.add_argument("--benchmark", default=str(DEFAULT_BENCHMARK), help="Optional benchmark Excel path.")
    parser.add_argument("--type", choices=("sale", "purchase"), default="sale", help="Choose the OCR pipeline to run.")
    parser.add_argument("--company-name", default=DEFAULT_PURCHASE_COMPANY, help="Tally company name for purchase mode.")
    parser.add_argument("--push-mode", choices=("none", "direct"), default="", help="Push behavior for purchase mode.")
    parser.add_argument("--serve", action="store_true", help="Run as HTTP server.")
    args = parser.parse_args()

    benchmark_path = Path(args.benchmark).resolve() if args.benchmark else None

    if args.input:
        input_path = Path(args.input).resolve()
        push_mode = args.push_mode or "none"
        runtime_path = input_path
        original_upload_path: Path | None = None
        upload_kind = "image"
        if args.type == "purchase":
            runtime_path, original_upload_path, upload_kind = materialize_input_path(
                input_path,
                input_path.stem or "upload_input",
                UPLOAD_DIR,
                render_pdf=False,
            )
            if upload_kind == "pdf":
                print(f"Using PDF directly for OCR: {runtime_path}")
        if args.type == "purchase":
            payload = run_purchase_pipeline(
                image_path=original_upload_path if upload_kind == "pdf" and original_upload_path else runtime_path,
                company_name=collapse_spaces(args.company_name) or DEFAULT_PURCHASE_COMPANY,
                push_mode=push_mode,
            )
        else:
            if input_path.suffix.lower() == ".pdf":
                parser.error("PDF input is currently supported only with --type purchase.")
            payload = run_pipeline_for_image(
                image_path=runtime_path,
                benchmark_path=benchmark_path if benchmark_path and benchmark_path.exists() else None,
            )
        payload["upload_kind"] = upload_kind
        payload["source_upload_path"] = str(original_upload_path or input_path)
        payload["saved_result"] = str(save_result(OUTPUT_DIR, (original_upload_path or input_path).stem, payload))
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    if not args.serve:
        parser.error("Use --serve to start the HTTP server or --input to run once.")

    print(f"MiniCPM n8n server on http://{SERVE_HOST}:{SERVE_PORT}")
    print(f"Pipeline: {PIPELINE_PATH}")
    if benchmark_path and benchmark_path.exists():
        print(f"Benchmark: {benchmark_path}")
    try:
        ThreadingHTTPServer((SERVE_HOST, SERVE_PORT), MiniCPMHandler).serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
