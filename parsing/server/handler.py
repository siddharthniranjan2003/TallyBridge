import argparse
import base64
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime
from difflib import SequenceMatcher
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit
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
BACKEND_ENV_VALUES = load_env_file(SCRIPT_DIR.parent / "backend" / ".env")


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
# RunPod/Nanonets path renders at higher DPI: dense headers (e.g. Pidilite's
# multi-column "Document No" block) get dropped at 200 but survive at 300.
RUNPOD_PDF_RENDER_DPI = int(env_value("MINICPM_RUNPOD_PDF_DPI", "300"))
RUNPOD_PDF_RETRY_DPI = int(env_value("MINICPM_RUNPOD_PDF_RETRY_DPI", "400"))
PDF_MAX_PAGES = max(1, int(env_value("MINICPM_PDF_MAX_PAGES", "4")))
PDF_PAGE_GAP_PX = max(0, int(env_value("MINICPM_PDF_PAGE_GAP_PX", "24")))
DEFAULT_PURCHASE_COMPANY = env_value("MINICPM_PURCHASE_COMPANY", env_value("TALLY_COMPANY", "K V ENTERPRISES"))
PADDLE_PYTHON_HINT = env_value("MINICPM_PADDLE_PYTHON", "")
DOCSTRANGE_INVOICE_URL = env_value("DOCSTRANGE_INVOICE_URL", "http://127.0.0.1:8010/extract")
DOCSTRANGE_TIMEOUT_SECONDS = int(env_value("DOCSTRANGE_TIMEOUT_SECONDS", "1800"))
PUSH_QUEUE_URL = (
    env_value("MINICPM_PUSH_QUEUE_URL", "")
    or BACKEND_ENV_VALUES.get("PUSH_QUEUE_URL", "")
    or "http://localhost:3001/api/sync/push-queue?company_name=K+V+ENTERPRISES"
)
PUSH_QUEUE_API_KEY = (
    env_value("MINICPM_PUSH_QUEUE_API_KEY", "")
    or BACKEND_ENV_VALUES.get("API_KEY", "")
)
PUSH_QUEUE_TIMEOUT_SECONDS = int(env_value("MINICPM_PUSH_QUEUE_TIMEOUT_SECONDS", "30"))
RUNPOD_POD_URL = env_value("RUNPOD_POD_URL", "https://e1a2h1u5vujgun-8000.proxy.runpod.net").rstrip("/")
RUNPOD_POD_API_KEY = env_value("RUNPOD_POD_API_KEY", "sk-e1a2h1u5vujgun")
RUNPOD_MODEL = env_value("RUNPOD_MODEL", "nanonets/Nanonets-OCR2-3B")
RUNPOD_TIMEOUT_SECONDS = int(env_value("RUNPOD_TIMEOUT_SECONDS", "180"))
RUNPOD_SERVERLESS_POLL_SECONDS = float(env_value("RUNPOD_SERVERLESS_POLL_SECONDS", "3") or "3")
RUNPOD_MAX_TOKENS = int(env_value("RUNPOD_MAX_TOKENS", "4096"))
RUNPOD_TEMPERATURE = float(env_value("RUNPOD_TEMPERATURE", "0") or "0")
RUNPOD_DEBUG_LOG = env_value("RUNPOD_DEBUG_LOG", str(OUTPUT_DIR / "runpod_debug.log"))
RUNPOD_MARKDOWN_PROMPT = env_value(
    "RUNPOD_MARKDOWN_PROMPT",
    "Extract the text from the above document as if you were reading it naturally. "
    "Return the tables in markdown table format. "
    "Return page numbers as: <page_number>1</page_number>",
)
GSHEET_SHEET_NAME = env_value("MINICPM_GSHEET_NAME", "Challan")
GSHEET_DATA_START_ROW = int(env_value("MINICPM_GSHEET_DATA_START_ROW", "5"))
SUPABASE_URL = env_value("SUPABASE_URL", "")
SUPABASE_KEY = env_value("SUPABASE_KEY", "", aliases=("SUPABASE_SERVICE_KEY", "API_KEY"))
# Sale voucher knobs. Mirrors backend push-invoice.ts (GST SALE, 9% CGST + 9% SGST).
SALE_GST_RATE = float(env_value("SALE_GST_RATE", "0.09"))
SALE_IGST_RATE = float(env_value("SALE_IGST_RATE", "0.18"))
SALE_VOUCHER_TYPE = env_value("SALE_VOUCHER_TYPE", "GST SALE")
SALE_LEDGER_NAME = env_value("SALE_LEDGER_NAME", "GST SALE")
SALE_DEFAULT_UNIT = env_value("SALE_DEFAULT_UNIT", "NOS")
# Intra-state (CGST+SGST) vs inter-state (IGST) is decided by the debtor's home
# state on the Supabase ledgers row. SALE_HOME_STATE = the company's own state.
SALE_HOME_STATE = env_value("SALE_HOME_STATE", "Haryana")
SALE_LEDGER_TABLE = env_value("MINICPM_SALE_LEDGER_TABLE", "ledgers")
SALE_DEBTOR_GROUP = env_value("MINICPM_SALE_DEBTOR_GROUP", "Sundry Debtors")
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
    if getattr(pipeline, "SALE_OCR_BACKEND", "ollama") == "runpod":
        return collapse_spaces(pipeline.query_minicpm_vlm(image_bytes_to_b64(image_bytes), prompt))

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


def load_full_image_jpeg(image_path: Path) -> bytes:
    with Image.open(image_path) as image:
        buffer = BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=95)
        return buffer.getvalue()


def _match_party_from_image(image_bytes: bytes, pipeline) -> tuple[dict, bool]:
    """OCR the party name from one image and match it against Supabase.

    Returns (result, confident) where confident=True means a usable match
    (override or a Supabase hit above threshold) — i.e. no fallback needed.
    """
    ocr_party_name = clean_party_name_text(query_ollama_text(image_bytes, PARTY_NAME_PROMPT, pipeline))

    if not ocr_party_name:
        return {"ocr_text": "", "matched_name": "", "score": 0.0, "source": "empty_ocr", "candidates": [], "error": ""}, False

    overridden_name = hardcoded_party_name_override(ocr_party_name)
    if overridden_name:
        return {
            "ocr_text": ocr_party_name,
            "matched_name": overridden_name,
            "score": 999.0,
            "source": "hardcoded_override",
            "candidates": [{"party_name": overridden_name, "score": 999.0}],
            "error": "",
        }, True

    if not SUPABASE_URL or not SUPABASE_KEY:
        return {"ocr_text": ocr_party_name, "matched_name": "", "score": 0.0, "source": "supabase_not_configured", "candidates": [], "error": ""}, False

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
        }, True

    return {"ocr_text": ocr_party_name, "matched_name": "", "score": 0.0, "source": "no_supabase_match", "candidates": ranked_candidates[:5], "error": ""}, False


def extract_party_name(image_path: Path, pipeline) -> dict:
    try:
        result, confident = _match_party_from_image(crop_party_header_image(image_path), pipeline)
    except Exception as exc:
        return {"ocr_text": "", "matched_name": "", "score": 0.0, "source": "error", "candidates": [], "error": str(exc)}

    if confident:
        return result

    # The narrow header crop can misread (e.g. cursive "Balaji" -> "Bain"); the full
    # image gives the model more context. Retry once and keep the better result.
    try:
        full_result, full_confident = _match_party_from_image(load_full_image_jpeg(image_path), pipeline)
        if full_confident or full_result.get("ocr_text"):
            full_result["source"] = f"{full_result['source']}_fullimg"
            return full_result
    except Exception:
        pass

    return result


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
                "match_score": row.match_score,
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


def call_docstrange_invoice_server(file_bytes: bytes, filename: str, upload_kind: str) -> dict:
    content_type = "application/pdf" if upload_kind == "pdf" else "image/jpeg"
    response = requests.post(
        DOCSTRANGE_INVOICE_URL,
        files={"file": (filename, file_bytes, content_type)},
        timeout=DOCSTRANGE_TIMEOUT_SECONDS,
    )
    if response.status_code >= 400:
        detail = response.text
        try:
            detail = json.dumps(response.json(), ensure_ascii=False)
        except Exception:
            pass
        raise ValueError(f"DocStrange server failed with HTTP {response.status_code}: {detail}")
    payload = response.json()
    if not payload.get("ok"):
        raise ValueError(payload.get("error") or "DocStrange extraction failed")
    return payload


def render_pdf_bytes_to_jpeg(pdf_bytes: bytes) -> bytes:
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
    buffer = BytesIO()
    canvas.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def render_pdf_bytes_to_jpeg_pages(pdf_bytes: bytes, dpi: int | None = None) -> list[bytes]:
    """Render each PDF page to its own full-resolution JPEG (no stitching)."""
    pages = convert_from_bytes(pdf_bytes, dpi=dpi or PDF_RENDER_DPI, first_page=1, last_page=PDF_MAX_PAGES)
    if not pages:
        raise ValueError("PDF upload did not contain any renderable pages")
    result: list[bytes] = []
    for page in pages:
        buffer = BytesIO()
        page.convert("RGB").save(buffer, format="JPEG", quality=95)
        result.append(buffer.getvalue())
    return result


# DPI for the per-page JPEGs stored against the push_queue row (shown in the app's
# queue/history Image tab). Lower than the OCR render DPI to keep the forwarded
# payload small; still legible on a phone.
INVOICE_IMAGE_DPI = int(env_value("INVOICE_IMAGE_DPI", "150"))


def scanned_pages_b64(file_bytes: bytes) -> list[str]:
    """Render an uploaded document to per-page JPEG base64 strings for storage.

    A PDF becomes one JPEG per page; a single uploaded image becomes one entry.
    Best-effort: any failure yields an empty list so it never blocks the push to
    the backend queue (the voucher, not the image, is the source of truth)."""
    try:
        if is_pdf_bytes(file_bytes):
            pages = render_pdf_bytes_to_jpeg_pages(file_bytes, dpi=INVOICE_IMAGE_DPI)
        elif is_image_bytes(file_bytes):
            pages = [file_bytes]
        else:
            return []
        return [base64.b64encode(page).decode("ascii") for page in pages]
    except Exception as exc:
        print(f"[scanned_pages_b64] render failed, skipping images: {exc}")
        return []


def markdown_to_basic_html(markdown: str) -> str:
    lines = str(markdown or "").splitlines()
    parts: list[str] = [
        "<!DOCTYPE html>",
        "<html lang=\"en\">",
        "<head>",
        "<meta charset=\"UTF-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">",
        "<title>RunPod OCR Markdown</title>",
        "<style>body{font-family:Arial,sans-serif;line-height:1.5;padding:24px;max-width:1100px;margin:auto}"
        "table{border-collapse:collapse;width:100%;margin:16px 0}"
        "th,td{border:1px solid #ccc;padding:6px;text-align:left;vertical-align:top}"
        "th{background:#f3f3f3}pre{white-space:pre-wrap}</style>",
        "</head>",
        "<body>",
    ]
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip().startswith("|"):
            table_rows: list[list[str]] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                if cells and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells):
                    index += 1
                    continue
                table_rows.append(cells)
                index += 1
            if table_rows:
                parts.append("<table>")
                for row_index, cells in enumerate(table_rows):
                    tag = "th" if row_index == 0 else "td"
                    parts.append("<tr>" + "".join(f"<{tag}>{html.escape(cell)}</{tag}>" for cell in cells) + "</tr>")
                parts.append("</table>")
            continue
        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        if stripped.startswith("#"):
            level = min(6, len(stripped) - len(stripped.lstrip("#")))
            text = stripped[level:].strip()
            parts.append(f"<h{level}>{html.escape(text)}</h{level}>")
        else:
            parts.append(f"<p>{html.escape(stripped)}</p>")
        index += 1
    parts.extend(["</body>", "</html>"])
    return "\n".join(parts)


def extract_runpod_markdown(output: object) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        choices = output.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict) and isinstance(message.get("content"), str):
                    return message["content"]
                if isinstance(first.get("text"), str):
                    return first["text"]
        for key in ("content", "markdown", "text", "output"):
            value = output.get(key)
            if isinstance(value, str):
                return value
    return json.dumps(output, ensure_ascii=False, indent=2)


def log_runpod_debug(event: str, payload: dict[str, object]) -> None:
    try:
        log_path = Path(RUNPOD_DEBUG_LOG)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            **payload,
        }
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def push_queue_url_with_company(company_name: str) -> str:
    parts = urlsplit(PUSH_QUEUE_URL)
    query_pairs = dict(parse_qsl(parts.query, keep_blank_values=True))
    query_pairs["company_name"] = company_name or DEFAULT_PURCHASE_COMPANY
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_pairs), parts.fragment))


def post_to_push_queue(payload: dict, company_name: str) -> dict:
    if not PUSH_QUEUE_API_KEY:
        raise ValueError("Push queue API key is not configured. Set MINICPM_PUSH_QUEUE_API_KEY or backend/.env API_KEY.")
    request_url = push_queue_url_with_company(company_name)
    session = requests.Session()
    session.trust_env = False
    response = session.post(
        request_url,
        headers={
            "Content-Type": "application/json",
            "x-api-key": PUSH_QUEUE_API_KEY,
        },
        json=payload,
        timeout=PUSH_QUEUE_TIMEOUT_SECONDS,
    )
    try:
        body = response.json() if response.text else {}
    except ValueError:
        body = {"raw": response.text}
    if not response.ok:
        raise ValueError(f"Push queue request failed with HTTP {response.status_code}: {json.dumps(body, ensure_ascii=False)[:1000]}")
    return {
        "ok": True,
        "status_code": response.status_code,
        "request_url": request_url,
        "request_payload": payload,
        "body": body,
    }


# ── Sale rate lookup (ported from the Challan Apps Script) ──────────────────────
def _supabase_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    return session


def delete_scan_job(job_id: str | None) -> None:
    """Drain the shared scan_jobs row for this scan so the app's "Processing…"
    badge clears on every client — the realtime DELETE reaches the scanning phone
    AND any open web session. Called right after the push_queue invoice row is
    inserted (its arrival is what the badge was waiting on). Best-effort: on
    failure, clients drop the row at their 300s safety TTL. No-op when the request
    carried no job_id (older app build, or a direct/manual call). job_id is
    validated as a UUID before it goes into the PostgREST filter."""
    if not (job_id and SUPABASE_URL and SUPABASE_KEY):
        return
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", job_id):
        return
    try:
        url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/scan_jobs?id=eq.{job_id}"
        _supabase_session().delete(
            url,
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            timeout=PUSH_QUEUE_TIMEOUT_SECONDS,
        )
    except Exception:
        pass


def fail_scan_job(job_id: str | None, reason: str, page_count: int = 0) -> None:
    """Mark this scan's shared scan_jobs row as failed (status='failed') instead
    of deleting it, so the app surfaces a "Garbage invoice" row the user can see
    and dismiss — rather than the scan silently expiring at the 300s badge TTL.
    Best-effort; the same UUID guard + Supabase auth as delete_scan_job. No-op
    when the request carried no job_id (older app build, or a direct/manual call).
    Pair with post_scan_image so the failed row's scanned pages are viewable."""
    if not (job_id and SUPABASE_URL and SUPABASE_KEY):
        return
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", job_id):
        return
    try:
        url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/scan_jobs?id=eq.{job_id}"
        _supabase_session().patch(
            url,
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            json={"status": "failed", "reason": (reason or "")[:500], "page_count": page_count},
            timeout=PUSH_QUEUE_TIMEOUT_SECONDS,
        )
    except Exception:
        pass


def scan_image_url(job_id: str) -> str:
    """Backend endpoint that stores a failed scan's page images, derived from
    PUSH_QUEUE_URL (same host + base path) and keyed by the scan_jobs row id.
    The app reads them back via GET /push-queue/{id}/image/{page}."""
    parts = urlsplit(PUSH_QUEUE_URL)
    base_path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, f"{base_path}/scan-image/{job_id}", "", ""))


def post_scan_image(job_id: str | None, pages_b64: list[str]) -> None:
    """Forward a failed scan's rendered page images to the backend (GCS, keyed by
    the scan_jobs id) so the app's garbage-invoice sheet can show them. Best-effort;
    never blocks the response. No-op without a job_id, images, or push-queue key."""
    if not (job_id and pages_b64 and PUSH_QUEUE_API_KEY):
        return
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", job_id):
        return
    try:
        session = requests.Session()
        session.trust_env = False
        session.post(
            scan_image_url(job_id),
            headers={"Content-Type": "application/json", "x-api-key": PUSH_QUEUE_API_KEY},
            json={"images_b64": pages_b64},
            timeout=PUSH_QUEUE_TIMEOUT_SECONDS,
        )
    except Exception:
        pass


def report_failed_scan(job_id: str | None, reason: str, image_bytes: bytes | None) -> None:
    """Surface a scan that produced no voucher as a "Garbage invoice" in the app:
    store its scanned pages (if any) then mark the scan_jobs row failed. Image
    storage runs first so the pages exist by the time the failed-status realtime
    event reaches the app. Entirely best-effort — a no-op job_id just falls back
    to today's badge-TTL behavior."""
    if not job_id:
        return
    pages_b64 = scanned_pages_b64(image_bytes) if image_bytes else []
    post_scan_image(job_id, pages_b64)
    fail_scan_job(job_id, reason, len(pages_b64))


def fetch_latest_rates_for_party(party_name: str) -> dict[str, dict]:
    """RPC get_latest_rates_for_party -> {stock_item_name: {rate, source}} (same party)."""
    if not SUPABASE_URL or not SUPABASE_KEY or not party_name:
        return {}
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/rpc/get_latest_rates_for_party"
    try:
        resp = _supabase_session().post(
            url,
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"},
            json={"p_party_name": party_name},
            timeout=PUSH_QUEUE_TIMEOUT_SECONDS,
        )
        rows = resp.json() if resp.ok and resp.text else []
    except Exception:
        return {}
    rate_map: dict[str, dict] = {}
    if isinstance(rows, list):
        for row in rows:
            name = str(row.get("stock_item_name", "") or "").strip()
            if name:
                rate_map[name] = {
                    "rate": float(row.get("rate") or 0),
                    "discount_pct": float(row.get("discount_pct") or 0),
                    "source": "same_party",
                }
    return rate_map


def fetch_party_state(party_name: str) -> str:
    """Look up the debtor's state from the Supabase ledgers row.

    Filters ledgers by name + group_name (Sundry Debtors) and returns the `state`
    column. Used to choose intra-state (CGST+SGST) vs inter-state (IGST) GST.
    Returns "" when not configured / not found so callers can fall back to default.
    """
    if not SUPABASE_URL or not SUPABASE_KEY or not party_name:
        return ""
    endpoint = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{SALE_LEDGER_TABLE}"
    try:
        resp = supabase_get(
            endpoint,
            {
                "select": "state",
                "name": f"eq.{party_name}",
                "group_name": f"eq.{SALE_DEBTOR_GROUP}",
                "limit": "1",
            },
        )
        rows = resp.json() if resp.ok and resp.text else []
    except Exception:
        return ""
    if isinstance(rows, list) and rows:
        return str(rows[0].get("state", "") or "").strip()
    return ""


def fetch_fallback_rate_for_item(party_name: str, item_name: str) -> dict | None:
    """Latest GST SALE rate for this item from a DIFFERENT party.

    Sale-scoped: this feeds build_sale_rate_map, so an unfiltered lookup can
    return a purchase line and stamp a supplier cost price onto a sale invoice.
    voucher_type/date are read off the parent voucher; the copies on
    voucher_items are unmaintained and NULL on rows synced since 2026-07-20.
    Order by the voucher (invoice) date, NOT created_at — created_at is the
    sync-insertion time, so a late-synced old invoice would otherwise outrank the
    genuine latest sale. Carry the source sale's discount instead of zeroing it,
    so the borrowed rate reflects the price the item actually sold at.
    """
    if not SUPABASE_URL or not SUPABASE_KEY or not item_name:
        return None
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/voucher_items"
    params = {
        "select": "stock_item_name,rate,discount_pct,created_at,vouchers!inner(party_name,date)",
        "stock_item_name": f"eq.{item_name}",
        "vouchers.party_name": f"neq.{party_name}",
        "vouchers.voucher_type": "eq.GST SALE",
        "order": "vouchers(date).desc.nullslast",
        "limit": "1",
    }
    try:
        resp = _supabase_session().get(
            url,
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            params=params,
            timeout=PUSH_QUEUE_TIMEOUT_SECONDS,
        )
        rows = resp.json() if resp.ok and resp.text else []
    except Exception:
        return None
    first = rows[0] if isinstance(rows, list) and rows else None
    if not first or first.get("rate") is None:
        return None
    return {
        "rate": float(first.get("rate") or 0),
        "discount_pct": float(first.get("discount_pct") or 0),
        "source": "different_party",
    }


def build_sale_rate_map(party_name: str, item_names: list[str]) -> dict[str, dict]:
    rate_map = fetch_latest_rates_for_party(party_name)
    for name in {n for n in item_names if n}:
        if name in rate_map:
            continue
        fallback = fetch_fallback_rate_for_item(party_name, name)
        if fallback:
            rate_map[name] = fallback
    return rate_map


def parse_sale_quantity(qty_text: str) -> float:
    match = re.match(r"^\s*(\d+(?:\.\d+)?)", str(qty_text or ""))
    return float(match.group(1)) if match else 0.0


def format_sale_match_score(score: float) -> str:
    """Match-score percent string, identical formatting to the purchase path."""
    rounded = round(float(score or 0), 2)
    if abs(rounded - round(rounded)) < 0.001:
        return f"{int(round(rounded))}%"
    return f"{rounded:.2f}%"


def build_sale_voucher_payload(company_name: str, party_name: str, rows: list[dict]) -> tuple[dict, list[dict], dict]:
    """Build a GST SALE voucher (same envelope as purchase) from matched sale rows.

    Returns (queue_request_payload, priced_items, source_payload). The source_payload
    mirrors the purchase path: {"items": [<voucher item> + provenance]} so the
    push_queue row records how each line was matched and where its rate came from.
    """
    item_names = [
        str(r.get("stock_matched", "") or "").strip()
        for r in rows
        if str(r.get("stock_matched", "") or "").strip() and str(r.get("stock_matched", "") or "").strip() != "NO MATCH"
    ]
    rate_map = build_sale_rate_map(party_name, item_names)

    priced_items: list[dict] = []
    source_items: list[dict] = []
    for row in rows:
        name = str(row.get("stock_matched", "") or "").strip()
        if not name or name == "NO MATCH":
            continue
        quantity = parse_sale_quantity(row.get("qty_text"))
        if quantity <= 0:
            quantity = 1.0
        info = rate_map.get(name)
        rate = round(float(info["rate"]), 2) if info else 0.0
        discount_pct = round(float(info.get("discount_pct", 0) or 0), 2) if info else 0.0
        # Amount is NET: deduct this line's own discount % from the gross (qty x rate).
        gross = round(quantity * rate, 2)
        amount = round(gross * (1 - discount_pct / 100.0), 2)
        rate_source = info["source"] if info else "none"
        voucher_item = {
            "stock_item_name": name,
            "quantity": quantity,
            "rate": rate,
            "amount": amount,
            "discount_pct": discount_pct,
            "unit": str(row.get("unit") or "").strip() or SALE_DEFAULT_UNIT,
            "godown_name": "Main Location",
        }
        priced_items.append({**voucher_item, "rate_source": rate_source})
        source_items.append(
            {
                **voucher_item,
                "source": "Matching_Algorithem",
                "score": format_sale_match_score(row.get("match_score")),
                "rate_source": rate_source,
            }
        )

    # Net subtotal (each item amount is already after its discount).
    subtotal = round(sum(item["amount"] for item in priced_items), 2)
    # discount_total = sum of per-item rupee discounts (gross - net).
    discount_total = round(
        sum(
            round(item["quantity"] * item["rate"], 2) - item["amount"]
            for item in priced_items
        ),
        2,
    )

    # GST is charged on the NET (discounted) subtotal. Intra-state (party in the
    # company's home state) books CGST+SGST; otherwise inter-state IGST.
    party_state = fetch_party_state(party_name)
    is_intra_state = party_state.strip().casefold() == SALE_HOME_STATE.strip().casefold()
    tax_entries: list[dict] = []
    if is_intra_state:
        cgst = round(subtotal * SALE_GST_RATE, 2)
        sgst = round(subtotal * SALE_GST_RATE, 2)
        if cgst > 0:
            tax_entries.append({"ledger_name": "CGST", "amount": cgst, "is_deemed_positive": False})
        if sgst > 0:
            tax_entries.append({"ledger_name": "SGST", "amount": sgst, "is_deemed_positive": False})
    else:
        igst = round(subtotal * SALE_IGST_RATE, 2)
        if igst > 0:
            tax_entries.append({"ledger_name": "IGST", "amount": igst, "is_deemed_positive": False})
    total = round(subtotal + sum(entry["amount"] for entry in tax_entries), 2)
    voucher_number = f"SALE-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    ledger_entries = [
        {"ledger_name": party_name, "amount": total, "is_deemed_positive": True},
        {"ledger_name": SALE_LEDGER_NAME, "amount": subtotal, "is_deemed_positive": False},
    ]
    ledger_entries.extend(tax_entries)

    voucher_payload = {
        "party_name": party_name,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "voucher_number": voucher_number,
        "reference": voucher_number,
        "narration": f"Sales challan {voucher_number}",
        "discount_total": discount_total,
        "voucher_type": SALE_VOUCHER_TYPE,
        "inventory_ledger_name": SALE_LEDGER_NAME,
        "ledger_entries": ledger_entries,
        "items": [{k: v for k, v in item.items() if k != "rate_source"} for item in priced_items],
    }
    source_payload = {"items": source_items}
    return (
        {"company_name": company_name, "voucher_payload": voucher_payload, "source_payload": source_payload},
        priced_items,
        source_payload,
    )


def _runpod_is_serverless() -> bool:
    return "api.runpod.ai/v2/" in RUNPOD_POD_URL


def _runpod_serverless_base_url() -> str:
    # Serverless RUNPOD_POD_URL is .../v2/<id>/openai; the native job API drops /openai.
    base = RUNPOD_POD_URL.rstrip("/")
    if base.endswith("/openai"):
        base = base[: -len("/openai")]
    return base


def _runpod_serverless_completion(session: requests.Session, headers: dict, payload: dict) -> dict:
    # Custom serverless workers don't transparently proxy the OpenAI route, so use
    # the native /run + /status API and unwrap the worker's `output` (OpenAI body).
    base = _runpod_serverless_base_url()
    submit = session.post(f"{base}/run", headers=headers, json={"input": payload}, timeout=RUNPOD_TIMEOUT_SECONDS)
    try:
        submit.raise_for_status()
    except requests.HTTPError as exc:
        log_runpod_debug("error", {"pod_url": base, "status_code": submit.status_code, "body": submit.text[:1000]})
        raise ValueError(f"RunPod serverless submit failed with HTTP {submit.status_code}: {submit.text[:1000]}") from exc

    job_id = (submit.json() or {}).get("id")
    if not job_id:
        raise ValueError(f"RunPod serverless did not return a job id: {submit.text[:300]}")

    deadline = time.time() + RUNPOD_TIMEOUT_SECONDS
    while time.time() < deadline:
        status = session.get(f"{base}/status/{job_id}", headers=headers, timeout=min(RUNPOD_TIMEOUT_SECONDS, 60))
        status.raise_for_status()
        data = status.json() or {}
        state = data.get("status")
        if state == "COMPLETED":
            return data.get("output") or {}
        if state in ("FAILED", "CANCELLED", "TIMED_OUT"):
            raise ValueError(f"RunPod serverless job {state}: {json.dumps(data)[:500]}")
        time.sleep(RUNPOD_SERVERLESS_POLL_SECONDS)
    raise ValueError(f"RunPod serverless job timed out after {RUNPOD_TIMEOUT_SECONDS}s")


def _runpod_markdown_for_image(image_bytes: bytes, filename: str, upload_kind: str) -> str:
    b64_image = base64.b64encode(image_bytes).decode("ascii")
    headers = {
        "Authorization": f"Bearer {RUNPOD_POD_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": RUNPOD_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"}},
                    {"type": "text", "text": RUNPOD_MARKDOWN_PROMPT},
                ],
            }
        ],
        "max_tokens": RUNPOD_MAX_TOKENS,
        "temperature": RUNPOD_TEMPERATURE,
    }
    session = requests.Session()
    session.trust_env = False
    log_runpod_debug(
        "submitted",
        {
            "pod_url": RUNPOD_POD_URL,
            "model": RUNPOD_MODEL,
            "filename": filename,
            "upload_kind": upload_kind,
            "image_bytes": len(image_bytes),
            "max_tokens": RUNPOD_MAX_TOKENS,
        },
    )
    if _runpod_is_serverless():
        response_payload = _runpod_serverless_completion(session, headers, payload)
        return extract_runpod_markdown(response_payload)

    response = session.post(
        f"{RUNPOD_POD_URL}/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=RUNPOD_TIMEOUT_SECONDS,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        log_runpod_debug(
            "error",
            {
                "pod_url": RUNPOD_POD_URL,
                "status_code": response.status_code,
                "body": response.text[:1000],
            },
        )
        raise ValueError(f"RunPod pod request failed with HTTP {response.status_code}: {response.text[:1000]}") from exc

    return extract_runpod_markdown(response.json())


def call_runpod_markdown_ocr(file_bytes: bytes, filename: str, upload_kind: str, dpi: int | None = None) -> dict:
    if not RUNPOD_POD_URL or not RUNPOD_POD_API_KEY:
        raise ValueError("RunPod OCR source requires RUNPOD_POD_URL and RUNPOD_POD_API_KEY in environment or parsing/.env")

    started_at = time.time()
    # Multi-page PDFs are OCR'd one page at a time so each page keeps full
    # resolution. Stitching pages into one tall JPEG made scanned multi-page
    # invoices (e.g. 3-page scans) downscale until the model read almost nothing.
    if upload_kind == "pdf":
        page_images = render_pdf_bytes_to_jpeg_pages(file_bytes, dpi=dpi or RUNPOD_PDF_RENDER_DPI)
    else:
        page_images = [file_bytes]

    markdown_parts: list[str] = []
    for image_bytes in page_images:
        page_markdown = _runpod_markdown_for_image(image_bytes, filename, upload_kind)
        if page_markdown.strip():
            markdown_parts.append(page_markdown.strip())
    markdown = "\n\n---\n\n".join(markdown_parts)

    log_runpod_debug(
        "completed",
        {
            "pod_url": RUNPOD_POD_URL,
            "model": RUNPOD_MODEL,
            "seconds": round(time.time() - started_at, 2),
            "pages": len(page_images),
            "markdown_chars": len(markdown),
        },
    )
    return {
        "ok": True,
        "status": "success",
        "source": "runpod",
        "markdown": markdown,
        "html": markdown_to_basic_html(markdown),
        "runpod": {
            "transport": "pod_openai_chat_completions",
            "pod_url": RUNPOD_POD_URL,
            "model": RUNPOD_MODEL,
            "seconds": round(time.time() - started_at, 2),
            "upload_kind": upload_kind,
            "filename": filename,
            "pages": len(page_images),
        },
    }


def runpod_targeted_field(file_bytes: bytes, upload_kind: str, question: str) -> str:
    """Ask the VLM one specific question about the document and return its raw
    text answer. Used as a fallback when the generic markdown OCR drops a field
    (e.g. Pidilite's "Document No" on a dense header). Renders the first page at
    the higher retry DPI for the best chance the label is legible."""
    if upload_kind == "pdf":
        pages = render_pdf_bytes_to_jpeg_pages(file_bytes, dpi=RUNPOD_PDF_RETRY_DPI)
        image_bytes = pages[0] if pages else file_bytes
    else:
        image_bytes = file_bytes
    b64_image = base64.b64encode(image_bytes).decode("ascii")
    headers = {"Authorization": f"Bearer {RUNPOD_POD_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": RUNPOD_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"}},
                    {"type": "text", "text": question},
                ],
            }
        ],
        "max_tokens": 64,
        "temperature": 0,
    }
    session = requests.Session()
    session.trust_env = False
    if _runpod_is_serverless():
        result = _runpod_serverless_completion(session, headers, payload)
        return extract_runpod_markdown(result)
    resp = session.post(f"{RUNPOD_POD_URL}/v1/chat/completions", headers=headers, json=payload, timeout=RUNPOD_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return extract_runpod_markdown(resp.json())


def normalize_voucher_number(voucher_payload) -> str:
    if isinstance(voucher_payload, dict):
        return str(voucher_payload.get("voucher_number") or "").strip()
    return ""


# Vendor-specific re-ask prompts. The model is shown the invoice image and asked
# for exactly one field, which is far more reliable than hoping a label survives
# the generic markdown transcription.
_INVOICE_NUMBER_QUESTIONS = {
    "PIDILITE": "What is the 'Document No' printed on this invoice? Reply with ONLY the number, no words.",
}
_DEFAULT_INVOICE_NUMBER_QUESTION = (
    "What is the invoice number (or document number) on this invoice? "
    "Reply with ONLY the number/code, no words."
)


def recover_invoice_number(file_bytes: bytes, upload_kind: str, vendor: str) -> str:
    question = _INVOICE_NUMBER_QUESTIONS.get(vendor, _DEFAULT_INVOICE_NUMBER_QUESTION)
    answer = runpod_targeted_field(file_bytes, upload_kind, question)
    # Pull the first plausible invoice token from the model's reply.
    match = re.search(r"[A-Z0-9][A-Z0-9\-/]{3,}", str(answer or "").upper())
    candidate = match.group(0) if match else ""
    if candidate in {"INVOICE", "DOCUMENT", "NUMBER", "NONE", "N/A", "NA"}:
        return ""
    return candidate


def apply_recovered_invoice_number(payload: dict, number: str) -> None:
    number = number.strip()
    if not number:
        return
    parsed_header = payload.setdefault("parsed", {}).setdefault("header", {})
    parsed_header["invoice_number"] = number
    narration = f"Purchase invoice {number}"
    for key in ("voucher_payload",):
        vp = payload.get(key)
        if isinstance(vp, dict):
            vp["voucher_number"] = number
            vp["reference"] = number
            vp["narration"] = narration
    # Mirror into the queue request body that actually gets pushed.
    req = payload.get("push_queue_request_payload")
    if isinstance(req, dict):
        tp = req.get("tally_payload")
        if isinstance(tp, dict) and isinstance(tp.get("voucher_payload"), dict):
            tp["voucher_payload"]["voucher_number"] = number
            tp["voucher_payload"]["reference"] = number
            tp["voucher_payload"]["narration"] = narration
    payload["invoice_number_source"] = "vlm_targeted_reask"


def load_sale_stock(pipeline, company_name: str | None):
    """Live Supabase stock_items for the company; CSV fallback for local/CLI use."""
    if company_name:
        try:
            return pipeline.load_stock_from_supabase(company_name)
        except Exception as exc:
            log_runpod_debug("sale_stock_supabase_fallback", {"company": company_name, "error": str(exc)[:300]})
    return pipeline.load_stock(pipeline.STOCK_CSV)


def build_stock_unit_map(stock) -> dict[str, str]:
    """name(casefold) -> unit, from the loaded stock catalog DataFrame.

    Lets each matched sale line carry its real Tally unit (stock_items.unit) so
    the pushed voucher (and the Tally XML built from it) uses the same unit as
    the stock master, instead of the SALE_DEFAULT_UNIT fallback."""
    unit_map: dict[str, str] = {}
    try:
        records = list(stock[["name", "unit"]].itertuples(index=False, name=None))
    except Exception:
        return unit_map
    for name, unit in records:
        key = collapse_spaces(str(name)).casefold()
        value = collapse_spaces(str(unit))
        if key and value:
            unit_map.setdefault(key, value)
    return unit_map


def apply_stock_units(rows: list[dict], stock) -> None:
    """Stamp each serialized row's matched-stock unit (best-effort).

    Rows without a stock match (or whose stock has no unit) are left untouched so
    build_sale_voucher_payload falls back to SALE_DEFAULT_UNIT for them."""
    unit_map = build_stock_unit_map(stock)
    if not unit_map:
        return
    for row in rows:
        matched = collapse_spaces(str(row.get("stock_matched", "") or "")).casefold()
        unit = unit_map.get(matched)
        if unit:
            row["unit"] = unit


def run_pipeline_for_image(
    image_path: Path, benchmark_path: Path | None = None, company_name: str | None = None
) -> dict:
    pipeline = load_pipeline()
    stock = load_sale_stock(pipeline, company_name)
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
    apply_stock_units(rows, stock)
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
    if push_mode not in {"none", "queue"}:
        raise ValueError("push must be 'none' or 'queue'.")

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


def normalized_request_path(path: str) -> str:
    return urlparse(path or "").path.rstrip("/") or "/"


def optional_query_bool(path: str, name: str) -> bool | None:
    values = parse_qs(urlparse(path or "").query).get(name, [])
    if not values:
        return None
    value = collapse_spaces(values[0]).lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be one of: on, off, true, false, 1, 0")


def optional_query_string(path: str, name: str) -> str | None:
    values = parse_qs(urlparse(path or "").query).get(name, [])
    if not values:
        return None
    value = collapse_spaces(values[0])
    return value or None


class MiniCPMHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        current_path = normalized_request_path(self.path)
        if current_path in {"", "/health"}:
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
                    "supported_endpoints": {
                        "purchase_and_sale": "/?type=sale|purchase...",
                        "raw_vlm": "/raw-vlm",
                        "docstrange": "/docstrange",
                    },
                    "docstrange": {
                        "url": DOCSTRANGE_INVOICE_URL,
                        "sources": ["local", "runpod"],
                        "runpod_configured": bool(RUNPOD_POD_URL and RUNPOD_POD_API_KEY),
                        "runpod_transport": "pod_openai_chat_completions",
                        "returns": ["markdown", "html"],
                        "purchase_all": "/docstrange?purchase=all",
                        "purchase_all_returns": [
                            "markdown",
                            "html",
                            "vendor",
                            "parsed",
                            "fuzzy_items",
                            "push_queue_payload",
                            "invoice_exists",
                        ],
                    },
                    "raw_vlm_query_overrides": {
                        "invoice": [
                            "purchase",
                            "all_vendors",
                            "purchase_addison",
                            "purchase_cp",
                            "purchase_emkay",
                            "purchase_forbes",
                            "purchase_grindwell",
                            "purchase_pidilite",
                            "purchase_rr",
                            "purchase_stanley",
                            "purchase_wikus",
                        ],
                        "company": ["K V ENTERPRISES"],
                        "document_unwarping": ["on", "off"],
                        "use_layout_detection": ["on", "off"],
                        "prompt_label": ["table", "ocr", "formula", "chart"],
                    },
                },
            )
            return
        self._send_json(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        # Pre-init so the generic except below can safely reference them even if
        # the request blows up before they are assigned in the try.
        job_id = None
        body = b""
        try:
            current_path = normalized_request_path(self.path)
            # The app tags each scan upload with ?job_id=<scan_jobs row id> so we
            # can delete that exact row once the invoice lands, draining the
            # "Processing…" badge on every client. Absent for non-app callers.
            job_id = optional_query_string(self.path, "job_id")
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_UPLOAD_BYTES:
                self._send_json(400, {"ok": False, "error": "Bad request size"})
                return

            body = self.rfile.read(length)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

            if current_path == "/raw-vlm":
                invoice_mode = (optional_query_string(self.path, "invoice") or "").lower()
                document_unwarping = optional_query_bool(self.path, "document_unwarping")
                use_layout_detection = optional_query_bool(self.path, "use_layout_detection")
                prompt_label = optional_query_string(self.path, "prompt_label")
                upload_path, original_upload_path, upload_kind = materialize_upload(
                    body,
                    self.headers.get("Content-Type", ""),
                    f"upload_{timestamp}",
                    UPLOAD_DIR,
                    render_pdf=False,
                )
                source_upload = original_upload_path or upload_path

                if invoice_mode == "all_vendors":
                    from purchase_ocrvl_pipeline import run_all_vendors_vl_safe_extract

                    company_name = (
                        optional_query_string(self.path, "company") or DEFAULT_PURCHASE_COMPANY
                    )
                    payload = run_all_vendors_vl_safe_extract(
                        source_upload,
                        company_name=company_name,
                        document_unwarping=document_unwarping,
                        use_layout_detection=use_layout_detection,
                        prompt_label=prompt_label,
                    )
                elif invoice_mode == "purchase_addison":
                    from purchase_ocrvl_pipeline import run_addison_vl_safe_extract

                    company_name = (
                        optional_query_string(self.path, "company") or DEFAULT_PURCHASE_COMPANY
                    )
                    payload = run_addison_vl_safe_extract(
                        source_upload,
                        company_name=company_name,
                        document_unwarping=document_unwarping,
                        use_layout_detection=use_layout_detection,
                        prompt_label=prompt_label,
                    )
                elif invoice_mode in {
                    "purchase_cp",
                    "purchase_emkay",
                    "purchase_forbes",
                    "purchase_grindwell",
                    "purchase_pidilite",
                    "purchase_rr",
                    "purchase_stanley",
                    "purchase_wikus",
                }:
                    from purchase_ocrvl_pipeline import VENDOR_SAFE_ENDPOINTS, run_vendor_vl_safe_extract

                    company_name = (
                        optional_query_string(self.path, "company") or DEFAULT_PURCHASE_COMPANY
                    )
                    vendor_config = VENDOR_SAFE_ENDPOINTS[invoice_mode]
                    payload = run_vendor_vl_safe_extract(
                        source_upload,
                        expected_vendor=vendor_config["expected_vendor"],
                        response_mode=vendor_config["mode"],
                        parser_label=vendor_config["parser"],
                        company_name=company_name,
                        document_unwarping=document_unwarping,
                        use_layout_detection=use_layout_detection,
                        prompt_label=prompt_label,
                    )
                elif invoice_mode == "purchase":
                    from purchase_ocrvl_pipeline import run_purchase_vl_matching_preview

                    company_name = (
                        optional_query_string(self.path, "company") or DEFAULT_PURCHASE_COMPANY
                    )
                    payload = run_purchase_vl_matching_preview(
                        source_upload,
                        company_name=company_name,
                        document_unwarping=document_unwarping,
                        use_layout_detection=use_layout_detection,
                        prompt_label=prompt_label,
                    )
                    duplicate_invoice_number = (
                        payload.get("parsed", {}).get("header", {}).get("invoice_number", "")
                    )
                    payload["duplicacy"] = {
                        "checked": True,
                        "is_duplicate": bool(check_duplicacy(duplicate_invoice_number)),
                        "invoice_number": duplicate_invoice_number,
                    }
                else:
                    from purchase_ocrvl_pipeline import call_vlm_server

                    payload = call_vlm_server(
                        source_upload,
                        document_unwarping=document_unwarping,
                        use_layout_detection=use_layout_detection,
                        prompt_label=prompt_label,
                    )

                payload["upload_kind"] = upload_kind
                payload["source_upload_path"] = str(source_upload)
                payload["saved_result"] = str(save_result(OUTPUT_DIR, source_upload.stem, payload))
                self._send_json(200, payload)
                return

            if current_path == "/docstrange":
                purchase_mode = (optional_query_string(self.path, "purchase") or "").lower()
                source_mode = (optional_query_string(self.path, "source") or "local").lower()
                if source_mode not in {"local", "docstrange", "runpod"}:
                    raise ValueError("Unsupported /docstrange source. Use source=local or source=runpod.")
                raw_content_type = self.headers.get("Content-Type", "")
                lower_content_type = raw_content_type.lower()
                if lower_content_type.startswith("application/json"):
                    file_bytes, filename, upload_kind = parse_json_body(body)
                elif lower_content_type.startswith("multipart/form-data"):
                    file_bytes, filename, upload_kind = parse_multipart_body(body, raw_content_type)
                else:
                    ext = ".bin"
                    if "/" in lower_content_type:
                        ext = "." + lower_content_type.split("/", 1)[1].split(";", 1)[0].strip()
                    filename = f"upload_{timestamp}{ext}"
                    file_bytes = body
                    upload_kind = infer_upload_kind(file_bytes, filename, raw_content_type)
                    if upload_kind == "unknown":
                        raise ValueError(f"Unsupported Content-Type for /docstrange: {raw_content_type or 'missing'}")

                safe_name = safe_filename(filename, f"upload_{timestamp}.jpg")
                UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                source_upload_path = UPLOAD_DIR / safe_name
                source_upload_path.write_bytes(file_bytes)

                if source_mode == "runpod":
                    docstrange_payload = call_runpod_markdown_ocr(file_bytes, safe_name, upload_kind)
                else:
                    docstrange_payload = call_docstrange_invoice_server(file_bytes, safe_name, upload_kind)
                if purchase_mode == "all":
                    from purchase_docstrange_pipeline import run_docstrange_purchase_all_pipeline

                    company_name = (
                        optional_query_string(self.path, "company") or DEFAULT_PURCHASE_COMPANY
                    )
                    payload = run_docstrange_purchase_all_pipeline(
                        input_path=source_upload_path,
                        markdown=docstrange_payload.get("markdown") or "",
                        html=docstrange_payload.get("html") or "",
                        company_name=company_name,
                    )
                    # B (DISABLED): field-targeted VLM re-ask fallback for a missing
                    # invoice number. Commented out because the extra OCR call adds
                    # latency; 300 DPI (option A) makes the primary parse reliable.
                    # Re-enable by uncommenting this block.
                    # if (
                    #     source_mode == "runpod"
                    #     and not normalize_voucher_number(payload.get("voucher_payload"))
                    # ):
                    #     try:
                    #         recovered = recover_invoice_number(
                    #             file_bytes, upload_kind, payload.get("vendor", "")
                    #         )
                    #     except Exception as exc:  # noqa: BLE001 - fallback must never break the push
                    #         recovered = ""
                    #         print(f"[Runpod][Fallback] invoice-number re-ask failed: {exc}")
                    #     if recovered:
                    #         apply_recovered_invoice_number(payload, recovered)
                    invoice_number = (
                        payload.get("parsed", {}).get("header", {}).get("invoice_number", "")
                    )
                    invoice_exists = bool(check_duplicacy(invoice_number))
                    payload["invoice_exists"] = invoice_exists
                    payload["duplicacy"] = {
                        "checked": True,
                        "invoice_number": invoice_number,
                        "invoice_exists": invoice_exists,
                    }
                    if source_mode == "runpod":
                        queue_request_payload = payload.get("push_queue_request_payload")
                        if not isinstance(queue_request_payload, dict):
                            # Surface why the voucher could not be built instead of
                            # raising a bare error that hides vendor/warning context.
                            parse_warnings = payload.get("parsed", {}).get("warnings", []) or []
                            reason = "push_queue_request_payload was not produced; no voucher to enqueue."
                            payload["queue_response"] = {
                                "ok": False,
                                "skipped": True,
                                "reason": reason,
                                "vendor": payload.get("vendor", ""),
                                "warnings": parse_warnings,
                            }
                            # No voucher was enqueued — surface the scan as a
                            # "Garbage invoice" in the app instead of letting its
                            # badge silently expire.
                            report_failed_scan(job_id, reason, file_bytes)
                        else:
                            # Still push the voucher even if no invoice/document number
                            # was detected, but flag it clearly in the response.
                            if not invoice_number:
                                payload["invoice"] = "not detected"
                            # Persist the duplicacy result on the enqueued voucher so it
                            # is visible on the push_queue row's voucher_payload.
                            tally_payload = queue_request_payload.get("tally_payload")
                            if isinstance(tally_payload, dict) and isinstance(
                                tally_payload.get("voucher_payload"), dict
                            ):
                                tally_payload["voucher_payload"]["invoice_exists"] = invoice_exists
                            # Forward the scanned page images so the backend can store
                            # them in GCS mapped to the new push_queue row id.
                            queue_request_payload["scanned_images_b64"] = scanned_pages_b64(file_bytes)
                            queue_response = post_to_push_queue(queue_request_payload, company_name)
                            payload["queue_response"] = queue_response
                            delete_scan_job(job_id)
                            payload["saved_queue_response"] = str(
                                save_result(
                                    OUTPUT_DIR,
                                    f"{source_upload_path.stem}_queue_response",
                                    queue_response,
                                )
                            )
                elif purchase_mode:
                    raise ValueError("Unsupported /docstrange purchase mode. Use purchase=all.")
                else:
                    payload = {
                        "markdown": docstrange_payload.get("markdown") or "",
                        "html": docstrange_payload.get("html") or "",
                    }
                payload["upload_kind"] = upload_kind
                payload["ocr_source"] = "runpod" if source_mode == "runpod" else "local_docstrange"
                if isinstance(docstrange_payload.get("runpod"), dict):
                    payload["runpod"] = docstrange_payload["runpod"]
                payload["source_upload_path"] = str(source_upload_path)
                payload["saved_result"] = str(save_result(OUTPUT_DIR, source_upload_path.stem, payload))
                self._send_json(200, payload)
                return

            options = request_options(self.path)
            image_path, original_upload_path, upload_kind = materialize_upload(
                body,
                self.headers.get("Content-Type", ""),
                f"upload_{timestamp}",
                UPLOAD_DIR,
                render_pdf=options["type"] != "purchase",
            )
            if upload_kind == "pdf" and options["type"] not in {"purchase", "sale"}:
                raise ValueError("PDF upload is currently supported only for type=purchase or type=sale")

            if options["type"] == "purchase":
                purchase_input = (
                    original_upload_path
                    if upload_kind == "pdf" and original_upload_path
                    else image_path
                )
                vlm_result: dict | None = None
                if options.get("check") == "duplicacy":
                    duplicate_invoice_number = ""
                    is_duplicate = False
                    if options["ocr"] == "vlm":
                        from purchase_ocrvl_pipeline import call_vlm_server, parse_vlm_server_result

                        vlm_result = call_vlm_server(purchase_input)
                        parsed_preview = parse_vlm_server_result(vlm_result)
                        duplicate_invoice_number = parsed_preview.get("header_data", {}).get("invoice_number", "")
                    else:
                        duplicate_invoice_number = run_purchase_ocr_header(purchase_input).get("invoice_number", "")
                    is_duplicate = check_duplicacy(duplicate_invoice_number)
                else:
                    duplicate_invoice_number = ""
                    is_duplicate = False

                if options["ocr"] == "vlm":
                    from purchase_ocrvl_pipeline import run_purchase_vl_pipeline

                    payload = run_purchase_vl_pipeline(
                        purchase_input,
                        company_name=options["company_name"],
                        _preloaded_vlm_result=vlm_result,
                    )
                else:
                    payload = run_purchase_pipeline(
                        image_path=purchase_input,
                        company_name=options["company_name"],
                        push_mode=options["push_mode"],
                    )
                if options.get("check") == "duplicacy":
                    payload["duplicacy"] = {
                        "checked": True,
                        "is_duplicate": bool(is_duplicate),
                        "invoice_number": duplicate_invoice_number,
                    }
            else:
                payload = run_pipeline_for_image(
                    image_path=image_path,
                    benchmark_path=DEFAULT_BENCHMARK if DEFAULT_BENCHMARK.exists() else None,
                    company_name=options["company_name"],
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
                if options["push_mode"] == "queue":
                    party_name = (payload.get("party_name") or "").strip()
                    if not party_name:
                        reason = "No party_name matched in Supabase; cannot build a Sales voucher."
                        payload["queue_response"] = {
                            "ok": False,
                            "skipped": True,
                            "reason": reason,
                        }
                        report_failed_scan(job_id, reason, body)
                    else:
                        queue_request_payload, sale_items, sale_source_payload = build_sale_voucher_payload(
                            options["company_name"], party_name, payload.get("rows", [])
                        )
                        if not sale_items:
                            reason = "No matched stock items to enqueue."
                            payload["queue_response"] = {
                                "ok": False,
                                "skipped": True,
                                "reason": reason,
                            }
                            report_failed_scan(job_id, reason, body)
                        else:
                            payload["sale_voucher_payload"] = queue_request_payload["voucher_payload"]
                            payload["sale_rate_items"] = sale_items
                            payload["sale_source_payload"] = sale_source_payload
                            # Forward the scanned page images (rendered per-page from
                            # the original upload) so the backend stores them in GCS
                            # mapped to the new push_queue row id.
                            queue_request_payload["scanned_images_b64"] = scanned_pages_b64(body)
                            payload["queue_response"] = post_to_push_queue(
                                queue_request_payload, options["company_name"]
                            )
                            delete_scan_job(job_id)
            self._send_json(200, payload)
        except Exception as exc:
            # A scan that carried a job_id but failed here (e.g. backend ingest
            # down / 5xx, SCAN-27) still produced no voucher — surface it as a
            # "Garbage invoice" instead of a silent drop.
            #
            # EXCEPTION: a ReadTimeout is ambiguous. The backend commits the
            # push_queue row BEFORE its synchronous multi-page GCS image upload
            # and only then responds; a slow upload can exceed our read timeout
            # after the voucher already enqueued. Marking the (still-present)
            # scan_jobs row failed there would show a bogus garbage row next to
            # the real voucher. So on a read timeout we do nothing and let the
            # badge fall through to its 300s TTL (pre-feature behavior). Connect
            # failures / non-2xx are unambiguous (nothing enqueued) → surface.
            if not isinstance(exc, requests.exceptions.ReadTimeout):
                report_failed_scan(job_id, str(exc), body or None)
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
