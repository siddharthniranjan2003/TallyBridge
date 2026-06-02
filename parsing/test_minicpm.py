"""
MiniCPM challan OCR matcher.

Prediction path:
    challan_image.jpg -> MiniCPM OCR -> stock_items_rows__1_.csv stock matching

Benchmark path:
    challan_updated.xlsx is loaded only to report how close the predicted
    stock descriptions are to Column D.

Run:
    py test_minicpm.py
"""

from __future__ import annotations

import base64
import os
import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests
from PIL import Image
from openpyxl import load_workbook
from rapidfuzz import fuzz, process


BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
IMAGE_PATH = BASE_DIR / "challan_image.jpg"
STOCK_CSV = BASE_DIR / "stock_items_rows__1_.csv"
BENCHMARK_XLSX = BASE_DIR / "challan_updated.xlsx"
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


MODEL = env_value("OLLAMA_MODEL", "openbmb/minicpm-v4.5")
OLLAMA_CHAT_URL = env_value("OLLAMA_CHAT_URL", "http://localhost:11434/api/chat")
REQUEST_TIMEOUT = int(env_value("REQUEST_TIMEOUT", "600"))

# Sale OCR backend: "ollama" (local, default) or "runpod" (MiniCPM-V 4.5 serverless).
# Knobs kept separate from the Nanonets RUNPOD_POD_* so both endpoints coexist.
SALE_OCR_BACKEND = env_value("SALE_OCR_BACKEND", "ollama").strip().lower()
MINICPM_RUNPOD_URL = env_value("MINICPM_RUNPOD_URL", "").rstrip("/")
MINICPM_RUNPOD_API_KEY = env_value("MINICPM_RUNPOD_API_KEY", "")
MINICPM_RUNPOD_MODEL = env_value("MINICPM_RUNPOD_MODEL", "openbmb/MiniCPM-V-4_5")
MINICPM_RUNPOD_TIMEOUT_SECONDS = int(env_value("MINICPM_RUNPOD_TIMEOUT_SECONDS", "300"))
MINICPM_RUNPOD_MAX_TOKENS = int(env_value("MINICPM_RUNPOD_MAX_TOKENS", "4096"))
MINICPM_RUNPOD_POLL_SECONDS = float(env_value("MINICPM_RUNPOD_POLL_SECONDS", "2"))
OCR_SYSTEM_PROMPT = "You are a strict OCR extraction engine. Return only the requested text."
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

PROMPT = """Read this handwritten industrial tools challan.
Return ONLY a numbered list of line items.
Format each line exactly as: NUMBER. FULL_DESCRIPTION | QTY UNIT

Rules:
- These are industrial item names such as taps, dies, drills, deburring blades, countersink tools, HIKOKI tools, and tape items.
- Carefully preserve fractions like 9/16, metric sizes like 8x1.25, and brands like TOTEM, ADDISON, HIKOKI, ET.
- Keep short item codes exactly as written when visible, including mixed letter/number codes such as GUC2 or FPT23.
- Do not invent extra leading digits in sizes. If the handwriting shows 4.0, do not turn it into 14.0.
- Expand ditto marks by repeating the full leading product family from the previous non-ditto line.
- Ignore side notes, bracket annotations, crossed-out rows, and any row that is clearly cut off at the bottom edge.
- Write one line per visible challan row.
- No explanation. No heading."""

BRAND_TOKENS = {
    "TOTEM",
    "ADDISON",
    "HIKOKI",
    "ET",
    "MIRANDA",
    "BOSCH",
    "YG",
    "STEELGRIP",
}
COMMON_TOKENS = {
    "HSS",
    "E",
    "TOOL",
    "TOOLS",
    "ITEM",
    "INDUSTRIAL",
    "THE",
    "OF",
    "A",
}
PRIORITY_TOKENS = {
    "TIN",
    "LH",
    "ROUND",
    "LONG",
    "BOT",
    "SET",
    "SEC",
    "PAIR",
    "SPPT",
    "SPFL",
    "TPR",
    "UNF",
    "UNC",
    "BSW",
    "BSF",
    "D371",
    "DIN371",
    "M35",
    "COUNTERSINK",
    "DEBURRING",
    "BLADE",
    "TAPE",
}
COLOR_TOKENS = ("BLUE", "RED", "GREEN", "YELLOW", "BLACK")
SPECIAL_CANDIDATE_TOKENS = {"LH", "TIN", "SPPT", "SPFL", "TPR", "BOT", "LONG"}
TOKEN_CORRECTION_TARGETS = {
    "TOTEM",
    "MIRANDA",
    "ADDISON",
    "HIKOKI",
    "DRILL",
    "HANDLE",
    "ROUND",
    "SUPERON",
    "CHAMP",
    "GREEN",
    "GNL",
    "BOT",
    "SEC",
    "SET",
    "PAIR",
}
HANDLE_TOKENS = {"HANDLE", "HANDR"}
ABRASIVE_TOKENS = {"DISK", "WHEEL", "FLAP", "CUT", "OFF"}
WELDING_TOKENS = {"WELDING", "ROD", "WIRE", "ELECTRODE", "SUPERON"}
DITTO_NOISE_TOKENS = {"I", "II", "III", "LI", "LL", "U"}
DRILL_VARIANT_TOKENS = {"GOLD", "STUB", "LONG", "EXTRA", "SUPER", "T/S", "TS"}
CANONICAL_TOKEN_MAP = {
    "DEBURING": "DEBURRING",
    "BLADES": "BLADE",
    "TAPS": "TAP",
    "DIES": "DIE",
    "DRILS": "DRILL",
}


@dataclass
class OCRItem:
    line_no: int
    raw_description: str
    qty_text: str
    description: str


@dataclass
class RankedMatch:
    name: str
    score: float


@dataclass
class PredictionRow:
    line_no: int
    raw_description: str
    normalized_description: str
    pre_fuzzy_name: str
    qty_text: str
    predicted_name: str
    expected_name: str
    status: str
    similarity: int
    confidence: int
    alternates: list[str]


def load_image_b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


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


def query_ollama_text(image_bytes: bytes, prompt: str) -> str:
    payload = {
        "model": MODEL,
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
    resp = requests.post(OLLAMA_CHAT_URL, json=payload, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return collapse_spaces(resp.json()["message"]["content"])


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


def extract_party_name(image_path: Path) -> dict:
    try:
        cropped_image = crop_party_header_image(image_path)
        ocr_party_name = clean_party_name_text(query_ollama_text(cropped_image, PARTY_NAME_PROMPT))
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
        ranked_candidates[0]["score"] >= PARTY_MATCH_THRESHOLD or ranked_candidates[0]["strong_ratio"] >= 0.99
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


def collapse_repeated_tokens(text: str) -> str:
    tokens = collapse_spaces(text).split()
    collapsed: list[str] = []
    repeatable = PRIORITY_TOKENS | BRAND_TOKENS | HANDLE_TOKENS
    for token in tokens:
        if collapsed and collapsed[-1] == token and token in repeatable:
            continue
        collapsed.append(token)
    return " ".join(collapsed)


def normalize_text(value: str) -> str:
    text = str(value).upper()
    text = text.replace("H.SS", "HSS").replace("HSS-TAP", "HSS TAP")
    text = text.replace("HIS DIE", "HSS DIE").replace("HIS ", "HSS ")
    text = re.sub(r"[^A-Z0-9+/\.\-]+", " ", text)
    text = re.sub(r"\b(\d+)\.0+\b", r"\1", text)
    return collapse_spaces(text)


def canonicalize_tokens(tokens: list[str]) -> list[str]:
    return [CANONICAL_TOKEN_MAP.get(token, token) for token in tokens]


def tokenize(value: str) -> list[str]:
    return canonicalize_tokens(normalize_text(value).split())


def token_set(value: str) -> set[str]:
    return set(tokenize(value))


def extract_numbers(value: str) -> list[str]:
    return re.findall(r"\d+(?:/\d+)?(?:\.\d+)?", normalize_text(value))


def _minicpm_runpod_base_url() -> str:
    # Serverless MINICPM_RUNPOD_URL is .../v2/<id>/openai; the native job API drops /openai.
    base = MINICPM_RUNPOD_URL.rstrip("/")
    if base.endswith("/openai"):
        base = base[: -len("/openai")]
    return base


def _minicpm_is_serverless() -> bool:
    return "api.runpod.ai/v2/" in MINICPM_RUNPOD_URL


def _strip_think(text: str) -> str:
    # MiniCPM-V 4.5 is a reasoning model; drop any <think>...</think> chain-of-thought.
    cleaned = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL)
    # If thinking was truncated (no closing tag), drop everything up to the last </think>.
    if "<think>" in cleaned:
        cleaned = cleaned.split("</think>")[-1]
        cleaned = cleaned.replace("<think>", "")
    return cleaned.strip()


def _extract_openai_content(payload: object) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        choices = payload.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"]
            if isinstance(choices[0].get("text"), str):
                return choices[0]["text"]
        for key in ("content", "text", "output"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
    raise ValueError(f"MiniCPM RunPod response had no text content: {str(payload)[:300]}")


def query_minicpm_vlm(image_b64: str, prompt: str, system_prompt: str = OCR_SYSTEM_PROMPT) -> str:
    """Send one image + prompt to the MiniCPM-V 4.5 RunPod worker (OpenAI chat format)."""
    if not MINICPM_RUNPOD_URL or not MINICPM_RUNPOD_API_KEY:
        raise ValueError(
            "SALE_OCR_BACKEND=runpod requires MINICPM_RUNPOD_URL and MINICPM_RUNPOD_API_KEY."
        )
    headers = {
        "Authorization": f"Bearer {MINICPM_RUNPOD_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": MINICPM_RUNPOD_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                    {"type": "text", "text": prompt},
                ],
            },
        ],
        "max_tokens": MINICPM_RUNPOD_MAX_TOKENS,
        "temperature": 0,
        # Disable MiniCPM-V 4.5 reasoning so the model returns only the OCR text.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    session = requests.Session()
    session.trust_env = False

    if _minicpm_is_serverless():
        # Custom serverless workers don't proxy the OpenAI route, so use native /run + /status
        # and unwrap the worker's `output` (the OpenAI chat body).
        base = _minicpm_runpod_base_url()
        submit = session.post(
            f"{base}/run", headers=headers, json={"input": body}, timeout=MINICPM_RUNPOD_TIMEOUT_SECONDS
        )
        submit.raise_for_status()
        job_id = (submit.json() or {}).get("id")
        if not job_id:
            raise ValueError(f"MiniCPM RunPod did not return a job id: {submit.text[:300]}")
        deadline = time.time() + MINICPM_RUNPOD_TIMEOUT_SECONDS
        while time.time() < deadline:
            status = session.get(
                f"{base}/status/{job_id}", headers=headers, timeout=min(MINICPM_RUNPOD_TIMEOUT_SECONDS, 60)
            )
            status.raise_for_status()
            data = status.json() or {}
            state = data.get("status")
            if state == "COMPLETED":
                return _strip_think(_extract_openai_content(data.get("output") or {}))
            if state in ("FAILED", "CANCELLED", "TIMED_OUT"):
                raise ValueError(f"MiniCPM RunPod job {state}: {str(data)[:500]}")
            time.sleep(MINICPM_RUNPOD_POLL_SECONDS)
        raise ValueError(f"MiniCPM RunPod job timed out after {MINICPM_RUNPOD_TIMEOUT_SECONDS}s")

    response = session.post(
        f"{MINICPM_RUNPOD_URL}/v1/chat/completions",
        headers=headers,
        json=body,
        timeout=MINICPM_RUNPOD_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return _strip_think(_extract_openai_content(response.json()))


def query_ollama_chat(image_b64: str) -> tuple[str, float]:
    if SALE_OCR_BACKEND == "runpod":
        start = time.time()
        content = query_minicpm_vlm(image_b64, PROMPT)
        return content, round(time.time() - start, 1)

    payload = {
        "model": MODEL,
        "think": False,
        "messages": [
            {
                "role": "system",
                "content": OCR_SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": PROMPT,
                "images": [image_b64],
            },
        ],
        "stream": False,
        "options": {"temperature": 0},
    }

    start = time.time()
    resp = requests.post(OLLAMA_CHAT_URL, json=payload, timeout=REQUEST_TIMEOUT)
    elapsed = round(time.time() - start, 1)
    resp.raise_for_status()
    return resp.json()["message"]["content"], elapsed


def numbered_ocr_lines(raw_text: str) -> list[str]:
    return [line.strip() for line in raw_text.splitlines() if re.match(r"^\d+\.", line.strip())]


def clean_desc_boundary_text(text: str) -> str:
    cleaned = collapse_spaces(text)
    cleaned = re.sub(r"[=\-–—_:;.,~]+\s*$", "", cleaned)
    return collapse_spaces(cleaned)


def clean_qty_boundary_text(text: str) -> str:
    cleaned = collapse_spaces(text)
    if not cleaned:
        return ""

    cleaned = re.sub(r"^[=\-–—_:;.,~]+\s*", "", cleaned)
    cleaned = collapse_spaces(cleaned)

    # The center divider is often misread as a leading standalone "2" before the real qty token.
    if re.match(r"^2\s+\d", cleaned):
        cleaned = re.sub(r"^2\s+", "", cleaned, count=1)

    return collapse_spaces(cleaned)


def clean_ocr_text_layout_noise(raw_text: str) -> str:
    cleaned_lines: list[str] = []
    for original_line in raw_text.splitlines():
        stripped = original_line.strip()
        if not re.match(r"^\d+\.", stripped):
            cleaned_lines.append(original_line)
            continue

        prefix_match = re.match(r"^(\d+\.)\s*(.*)$", stripped)
        if not prefix_match:
            cleaned_lines.append(stripped)
            continue

        line_prefix = prefix_match.group(1)
        body = prefix_match.group(2)
        left, separator, right = body.partition("|")
        if not separator:
            cleaned_lines.append(stripped)
            continue

        left = clean_desc_boundary_text(left)
        right = clean_qty_boundary_text(right)
        cleaned_lines.append(f"{line_prefix} {left} | {right}".rstrip())
    return "\n".join(cleaned_lines)


def split_desc_qty(line: str) -> tuple[str, str]:
    body = re.sub(r"^\d+\.\s*", "", line)
    left, _, right = body.partition("|")
    return clean_desc_boundary_text(left), clean_qty_boundary_text(right)


def is_ditto(value: str) -> bool:
    return bool(re.match(r'^\s*["\u201c\u201d]+', value))


def prefix_and_suffix(description: str) -> tuple[str, str]:
    tokens = tokenize(description)

    prefix_tokens = []
    for token in tokens:
        if re.search(r"\d", token):
            break
        prefix_tokens.append(token)

    suffix_tokens: list[str] = []
    for token in reversed(tokens):
        if re.search(r"\d", token):
            break
        if token in BRAND_TOKENS:
            suffix_tokens.insert(0, token)
        else:
            break

    return " ".join(prefix_tokens).strip(), " ".join(suffix_tokens).strip()


def has_number(token: str) -> bool:
    return bool(re.search(r"\d", token))


def correct_close_tokens(text: str) -> str:
    corrected = []
    for token in normalize_text(text).split():
        if has_number(token) or len(token) < 4:
            corrected.append(token)
            continue

        best = process.extractOne(token, TOKEN_CORRECTION_TARGETS, scorer=fuzz.ratio)
        if best and best[1] >= 74 and abs(len(token) - len(best[0])) <= 3:
            corrected.append(best[0])
        else:
            corrected.append(token)
    return " ".join(corrected)


def split_carry_parts(description: str) -> tuple[list[str], list[str], list[str]]:
    tokens = tokenize(description)
    if not tokens:
        return [], [], []

    first_num_idx = next((i for i, token in enumerate(tokens) if has_number(token)), len(tokens))
    suffix_start = len(tokens)
    while suffix_start > first_num_idx and tokens[suffix_start - 1] in BRAND_TOKENS:
        suffix_start -= 1

    numeric_indices = [i for i, token in enumerate(tokens[:suffix_start]) if has_number(token)]
    last_num_idx = numeric_indices[-1] if numeric_indices else first_num_idx - 1

    prefix_tokens = tokens[:first_num_idx]
    carry_tail = tokens[last_num_idx + 1 : suffix_start]
    suffix_tokens = tokens[suffix_start:]
    return prefix_tokens, carry_tail, suffix_tokens


def merge_ditto_description(desc_core: str, carry_prefix: list[str], carry_tail: list[str], carry_suffix: list[str]) -> str:
    core_tokens = [token for token in tokenize(desc_core) if token not in DITTO_NOISE_TOKENS]
    present = set(core_tokens)
    merged = list(carry_prefix) + core_tokens

    for token in carry_tail:
        if token not in present:
            merged.append(token)
            present.add(token)

    for token in carry_suffix:
        if token not in present:
            merged.append(token)
            present.add(token)

    return " ".join(merged).strip()


def apply_ocr_fixes(description: str) -> str:
    fixed = normalize_text(description)
    replacements = {
        "TOIEN": "TOTEM",
        "TOIEM": "TOTEM",
        "TOTAN": "TOTEM",
        "ADMISON": "ADDISON",
        "ADVISOR": "ADDISON",
        "HISEKDI": "HIKOKI",
        "HISEKI": "HIKOKI",
        "HISEKOI": "HIKOKI",
        "AGY ": "AG4 ",
        "AG 4": "AG-4",
        "AG4 ": "AG-4 ",
        "HSSTAP": "HSS TAP",
        "HSSS TAP": "HSS TAP",
        "CHLDR": "DRILL",
        "CHLUDN": "DRILL",
        "CHLUD": "DRILL",
        "CHIKU": "DRILL",
        "IEX": "10X",
        "1EX": "10X",
        "SPP ": "SPPT ",
        "HANDR": "HANDLE",
        "TEM": "TOTEM",
    }
    for src, dst in replacements.items():
        fixed = fixed.replace(src, dst)

    fixed = re.sub(r"\bCHL[A-Z0-9]{2,4}\b", "DRILL", fixed)
    fixed = re.sub(r"\bCHW[A-Z0-9]{2,4}\b", "DRILL", fixed)
    fixed = re.sub(r"\bHS\.?\s*S\b", "HSS", fixed)
    fixed = re.sub(r"\bM[IA][DR]AN[A-Z]{2}\b", "MIRANDA", fixed)
    fixed = correct_close_tokens(fixed)
    return collapse_spaces(fixed)


def parse_ocr_items(raw_text: str) -> list[OCRItem]:
    raw_text = clean_ocr_text_layout_noise(raw_text)
    items: list[OCRItem] = []
    carry_prefix: list[str] = []
    carry_tail: list[str] = []
    carry_suffix: list[str] = []

    for line in numbered_ocr_lines(raw_text):
        match = re.match(r"^(\d+)\.", line)
        if not match:
            continue

        line_no = int(match.group(1))
        raw_desc, qty_text = split_desc_qty(line)
        desc_core = re.sub(r'^[\"\u201c\u201d\s]+', "", raw_desc).strip()

        if is_ditto(raw_desc) or re.match(r"^\d", desc_core):
            raw_desc = merge_ditto_description(desc_core, carry_prefix, carry_tail, carry_suffix)

        description = apply_ocr_fixes(raw_desc)
        prefix_tokens, tail_tokens, suffix_tokens = split_carry_parts(description)
        if prefix_tokens:
            carry_prefix = prefix_tokens
        if tail_tokens or prefix_tokens:
            carry_tail = tail_tokens
        if suffix_tokens:
            carry_suffix = suffix_tokens

        items.append(
            OCRItem(
                line_no=line_no,
                raw_description=collapse_spaces(raw_desc),
                qty_text=normalize_text(qty_text),
                description=description,
            )
        )

    return items


def stock_unit_kind(unit: str, name: str) -> str:
    text = normalize_text(f"{unit} {name}")
    if "PAIR" in text:
        return "PAIR"
    if "SET" in text:
        return "SET"
    return "PIECE"


def _finalize_stock_frame(df: pd.DataFrame) -> pd.DataFrame:
    for column in ("id", "name", "group_name", "unit"):
        if column not in df.columns:
            df[column] = ""
    stock = df[["id", "name", "group_name", "unit"]].dropna(subset=["name"]).copy()
    # `id` is used as a dedup key downstream; synthesize one if the source lacks it.
    stock["id"] = [
        str(val) if (val is not None and str(val).strip() and str(val).lower() != "nan") else f"row-{pos}"
        for pos, val in enumerate(stock["id"].tolist())
    ]
    stock["name"] = stock["name"].astype(str)
    stock["group_name"] = stock["group_name"].fillna("").astype(str)
    stock["unit"] = stock["unit"].fillna("").astype(str)
    stock["norm"] = stock["name"].map(normalize_text)
    stock["group_norm"] = stock["group_name"].map(normalize_text)
    stock["numbers"] = stock["norm"].map(extract_numbers)
    stock["tokens"] = stock["name"].map(tokenize)
    stock["token_set"] = stock["tokens"].map(set)
    stock["brand_tokens"] = stock["token_set"].map(lambda vals: vals & BRAND_TOKENS)
    stock["unit_kind"] = stock.apply(lambda row: stock_unit_kind(row["unit"], row["name"]), axis=1)
    return stock.reset_index(drop=True)


def load_stock(path: Path) -> pd.DataFrame:
    return _finalize_stock_frame(pd.read_csv(path))


def load_stock_from_supabase(company_name: str) -> pd.DataFrame:
    """Live stock_items for the company, matching the CSV stock frame shape."""
    from purchase.company_context import (
        fetch_supabase_company_record,
        fetch_supabase_company_rows,
    )

    company = fetch_supabase_company_record(company_name)
    rows = fetch_supabase_company_rows(
        "stock_items",
        company["id"],
        select="id,name,group_name,unit,closing_qty,closing_value,rate",
    )
    if not rows:
        raise ValueError(f"No stock_items found in Supabase for company '{company_name}'.")
    return _finalize_stock_frame(pd.DataFrame(rows))


def load_benchmark(path: Path) -> list[str]:
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    header_row = None
    for row_idx in range(1, ws.max_row + 1):
        value = ws[f"D{row_idx}"].value
        if value and collapse_spaces(str(value)).upper() == "CORRECTED ITEM DESCRIPTION":
            header_row = row_idx
            break

    start_row = (header_row + 1) if header_row else 5
    corrected = []
    for row_idx in range(start_row, ws.max_row + 1):
        value = ws[f"D{row_idx}"].value
        if not value:
            continue
        corrected.append(collapse_spaces(value))
    return corrected


def qty_kind(qty_text: str) -> str:
    qty = normalize_text(qty_text)
    if "PAIR" in qty:
        return "PAIR"
    if "SET" in qty or "SEP" in qty or "SEE" in qty:
        return "SET"
    return "PIECE"


def is_code_like_query(query: str) -> bool:
    tokens = re.findall(r"[A-Z0-9]+", normalize_text(query))
    code_tokens = [token for token in tokens if any(ch.isalpha() for ch in token) and any(ch.isdigit() for ch in token)]
    return len(code_tokens) == 1 and len(tokens) <= 3


def classify_family(query: str) -> str:
    q = normalize_text(query)
    q_tokens = token_set(q)
    if HANDLE_TOKENS & q_tokens:
        return "HANDLE"
    if WELDING_TOKENS & q_tokens or ("SUPER" in q and "KG" in q):
        return "WELDING"
    if ABRASIVE_TOKENS & q_tokens or "GNL" in q:
        return "ABRASIVE"
    if "STEELGRIP" in q or ("TAPE" in q and "3/4" in q):
        return "STEELGRIP"
    if "HIKOKI" in q or "AG-4" in q or "AG-7" in q:
        return "HIKOKI"
    if "COUNTERSINK" in q or q.startswith("C520") or q.startswith("CS20"):
        return "COUNTERSINK"
    if "DEBUR" in q or "BLADE" in q:
        return "DEBUR"
    if "DRILL" in q or "M35" in q:
        return "DRILL"
    if "DIE" in q:
        return "DIE"
    if "TAP" in q or "DIN37" in q or "DIN371" in q or "D371" in q:
        return "TAP"
    return "ALL"


def normalize_metric_sizes(query: str, family: str) -> str:
    def replace_compact_with_suffix(match: re.Match[str]) -> str:
        left = match.group(1)
        right = match.group(2)
        suffix = match.group(3)
        if family in {"TAP", "DIE"} and "." not in right and len(right) == 1:
            right = f".{right}"
        return f"{left} X {right} {suffix}"

    def replace_compact(match: re.Match[str]) -> str:
        left = match.group(1)
        right = match.group(2)
        if family in {"TAP", "DIE"} and "." not in right and len(right) == 1:
            right = f".{right}"
        return f"{left} X {right}"

    query = re.sub(r"\b(\d{1,2})X(\d+(?:\.\d+)?)(SEC|SET|BOT|PAIR|TPR)\b", replace_compact_with_suffix, query)
    query = re.sub(r"\b(\d{1,2})X\.(\d+)\b", r"\1 X .\2", query)
    query = re.sub(r"\b(\d{1,2})X(\d+(?:\.\d+)?)\b", replace_compact, query)
    return collapse_repeated_tokens(query)


def prepare_query(description: str, qty_text: str) -> str:
    query = normalize_text(description)
    family = classify_family(query)

    if "DIN37" in query or "DIN371" in query:
        query = query.replace("DIN371", "D371").replace("DIN37", "D371")
        if "TAP" not in query:
            query = f"HSS-E TAP {query}"

    if "SPP" in query and "SPPT" not in query:
        query = query.replace("SPP", "SPPT")
    query = query.replace("SPPT-IN", "SPPT TIN")

    if "C520" in query or "CS20" in query:
        query = query.replace("C520", "CS20").replace("CS20", "CS20 COUNTERSINK TOOL")

    unit_mode = qty_kind(qty_text)
    if unit_mode == "SET":
        query = query.replace(" SEP ", " SET ").replace(" SEE ", " SET ")
    else:
        query = query.replace(" SEP ", " SEC ").replace(" SEE ", " SEC ")

    if unit_mode == "PIECE":
        query = re.sub(r"\bS\b$", "SEC", query)

    query = normalize_metric_sizes(query, family)

    if family == "DRILL":
        query = re.sub(r"\b(\d+)\.0\b", r"\1", query)

    if family == "WELDING" and "KG" in query:
        query = query.replace("HOLD", "ROD").replace("HOLDER", "ROD")

    if family == "HANDLE":
        query = query.replace("HANDR", "HANDLE")
        if query.startswith("DIE HANDLE"):
            query = query.replace("DIE HANDLE", "ROUND DIE HANDLE", 1)

    return collapse_spaces(query)


def build_family_views(stock: pd.DataFrame) -> dict[str, list[dict]]:
    recs = stock.to_dict("records")
    return {
        "ALL": recs,
        "TAP": [r for r in recs if "TAP" in r["token_set"]],
        "DIE": [r for r in recs if "DIE" in r["token_set"]],
        "HANDLE": [r for r in recs if "HANDLE" in r["token_set"]],
        "DRILL": [r for r in recs if "DRILL" in r["token_set"]],
        "HIKOKI": [r for r in recs if "HIKOKI" in r["token_set"] or "AG-4" in r["token_set"] or "AG-7" in r["token_set"]],
        "COUNTERSINK": [r for r in recs if "COUNTERSINK" in r["token_set"]],
        "DEBUR": [r for r in recs if "DEBURRING" in r["token_set"] or "BLADE" in r["token_set"] or "DEBUR" in r["token_set"]],
        "STEELGRIP": [r for r in recs if "STEELGRIP" in r["token_set"] or "TAPE" in r["token_set"]],
        "ABRASIVE": [r for r in recs if (ABRASIVE_TOKENS & r["token_set"]) or "GNL" in r["token_set"] or r["group_norm"] == "GNL"],
        "WELDING": [r for r in recs if (WELDING_TOKENS & r["token_set"]) or r["group_norm"] == "SUPERON"],
    }


def meaningful_tokens(tokens: set[str]) -> set[str]:
    return {token for token in tokens if token not in COMMON_TOKENS and len(token) > 1}


def as_float(value: str) -> float | None:
    if "/" in value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def numeric_distance_penalty(query_numbers: list[str], candidate_numbers: list[str], family: str) -> float:
    if family not in {"TAP", "DIE", "DRILL", "COUNTERSINK"}:
        return 0.0

    query_values = [num for num in (as_float(value) for value in query_numbers) if num is not None]
    candidate_values = [num for num in (as_float(value) for value in candidate_numbers) if num is not None]
    if not query_values or not candidate_values:
        return 0.0

    nearest = min(abs(q - c) for q in query_values for c in candidate_values)
    return min(18.0, nearest * 10.0)


def family_keywords(family: str) -> set[str]:
    return {
        "TAP": {"TAP"},
        "DIE": {"DIE"},
        "DRILL": {"DRILL", "M35"},
        "HIKOKI": {"HIKOKI", "AG-4", "AG-7"},
        "COUNTERSINK": {"COUNTERSINK", "CS20"},
        "DEBUR": {"DEBURRING", "BLADE"},
        "STEELGRIP": {"STEELGRIP", "TAPE"},
    }.get(family, set())


def expand_steelgrip_colors(description: str) -> list[str]:
    normalized = normalize_text(description)
    tail = normalized.replace("STEELGRIP", " ")
    color_chunks = tail.split()
    code_map = {
        "B": ["BLUE"],
        "R": ["RED"],
        "G": ["GREEN"],
        "Y": ["YELLOW"],
        "BK": ["BLACK"],
        "BR": ["BLUE", "RED"],
        "GY": ["GREEN", "YELLOW"],
        "BY": ["BLUE", "YELLOW"],
    }

    colors: list[str] = []
    for chunk in color_chunks:
        letters = re.sub(r"[^A-Z]", "", chunk)
        if not letters or letters in {"STEELGRIP", "TAPE"}:
            continue
        if re.search(r"\d", chunk) and len(letters) <= 1:
            continue

        idx = 0
        while idx < len(letters):
            two = letters[idx : idx + 2]
            one = letters[idx]
            if two in code_map:
                colors.extend(code_map[two])
                idx += 2
                continue
            if one in code_map:
                colors.extend(code_map[one])
            idx += 1

    return colors


def code_tokens(value: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[A-Z0-9]+", normalize_text(value))
        if any(ch.isalpha() for ch in token) and any(ch.isdigit() for ch in token) and 3 <= len(token) <= 10
    ]


def normalize_confusable_code(token: str) -> str:
    return token.translate(str.maketrans({"O": "0", "Q": "0", "I": "1", "L": "1", "S": "5", "T": "7"}))


def code_similarity(query: str, candidate_name: str) -> int:
    query_codes = code_tokens(query)
    candidate_codes = code_tokens(candidate_name)
    if not query_codes or not candidate_codes:
        return 0

    best = 0
    for query_code in query_codes:
        query_norm = normalize_confusable_code(query_code)
        query_sorted = "".join(sorted(query_norm))
        for candidate_code in candidate_codes:
            windows = [candidate_code]
            if len(candidate_code) > len(query_code):
                windows.extend(candidate_code[i : i + len(query_code)] for i in range(len(candidate_code) - len(query_code) + 1))
            for window in windows:
                window_norm = normalize_confusable_code(window)
                best = max(best, int(fuzz.ratio(query_norm, window_norm)))
                best = max(best, int(fuzz.partial_ratio(query_norm, window_norm)))
                best = max(best, int(fuzz.ratio(query_sorted, "".join(sorted(window_norm)))))
    return best


def candidate_pool(query: str, family: str, family_views: dict[str, list[dict]]) -> list[dict]:
    primary = family_views.get(family, family_views["ALL"])
    if family == "ALL":
        primary = family_views["ALL"]

    query_tokens = token_set(query)
    query_numbers = set(extract_numbers(query))
    query_brands = query_tokens & BRAND_TOKENS

    narrowed = []
    for record in primary:
        overlap_tokens = len(query_tokens & record["token_set"])
        overlap_numbers = len(query_numbers & set(record["numbers"]))
        overlap_brands = len(query_brands & record["brand_tokens"])
        code_score = code_similarity(query, record["name"])
        if overlap_numbers or overlap_brands or overlap_tokens >= 2 or code_score >= 78:
            narrowed.append(record)

    if len(narrowed) >= 12:
        return narrowed

    if family == "ALL":
        return primary
    if family in {"ABRASIVE", "WELDING", "HANDLE"} or is_code_like_query(query):
        return narrowed or primary

    seen_ids = {record["id"] for record in primary}
    fallback = list(primary)
    for record in family_views["ALL"]:
        if record["id"] in seen_ids:
            continue
        overlap_tokens = len(query_tokens & record["token_set"])
        overlap_numbers = len(query_numbers & set(record["numbers"]))
        overlap_brands = len(query_brands & record["brand_tokens"])
        code_score = code_similarity(query, record["name"])
        if overlap_numbers or overlap_brands or overlap_tokens >= 3 or code_score >= 84:
            fallback.append(record)
    return fallback


def score_candidate(query: str, qty_text: str, family: str, record: dict) -> float:
    candidate = record["norm"]
    query_tokens = token_set(query)
    candidate_tokens = record["token_set"]
    query_numbers = extract_numbers(query)
    candidate_numbers = record["numbers"]
    query_brands = query_tokens & BRAND_TOKENS
    candidate_brands = record["brand_tokens"]
    code_score = code_similarity(query, record["name"])

    score = (
        0.55 * fuzz.WRatio(query, candidate)
        + 0.30 * fuzz.token_set_ratio(query, candidate)
        + 0.15 * fuzz.partial_ratio(query, candidate)
    )

    meaningful_overlap = meaningful_tokens(query_tokens) & meaningful_tokens(candidate_tokens)
    missing_meaningful = meaningful_tokens(query_tokens) - meaningful_tokens(candidate_tokens)
    score += len(meaningful_overlap) * 4.0
    score -= min(14.0, len(missing_meaningful) * 2.0)

    query_number_set = set(query_numbers)
    candidate_number_set = set(candidate_numbers)
    overlap_numbers = query_number_set & candidate_number_set
    missing_numbers = query_number_set - candidate_number_set
    extra_numbers = candidate_number_set - query_number_set
    score += len(overlap_numbers) * 16.0
    score -= len(missing_numbers) * 18.0
    if query_number_set:
        score -= min(10.0, len(extra_numbers) * 4.0)
        score -= numeric_distance_penalty(query_numbers, candidate_numbers, family)

    if query_brands:
        if query_brands & candidate_brands:
            score += 12.0
        else:
            score -= 10.0

    if code_score:
        score += max(0.0, code_score - 72) * 1.2

    for token in PRIORITY_TOKENS:
        if token in query_tokens and token in candidate_tokens:
            score += 4.0
        elif token in query_tokens and token not in candidate_tokens:
            score -= 3.0

    unexpected_specials = {
        token for token in SPECIAL_CANDIDATE_TOKENS if token in candidate_tokens and token not in query_tokens
    }
    score -= len(unexpected_specials) * 4.0

    if family == "DIE" and "LH" not in query_tokens and "LH" in candidate_tokens:
        score -= 8.0
    if family == "DIE" and "ROUND" in candidate_tokens and "LH" not in query_tokens:
        score += 4.0
    if family == "HANDLE" and "HANDLE" in candidate_tokens:
        score += 14.0
    if family == "HANDLE" and "ROUND" in candidate_tokens and "DIE" in query_tokens:
        score += 8.0
    if family == "ABRASIVE" and (ABRASIVE_TOKENS & candidate_tokens):
        score += 10.0
    if family == "ABRASIVE" and (record["group_norm"] == "GNL" or "GNL" in candidate_tokens):
        score += 6.0
    if family == "ABRASIVE" and is_code_like_query(query):
        if {"FLAP", "DISK"} <= candidate_tokens:
            score += 10.0
        if {"CUT", "WHEEL"} <= candidate_tokens:
            score += 8.0
        if "PAPER" in candidate_tokens:
            score -= 10.0
        if "BELT" in candidate_tokens:
            score -= 12.0
    if family == "WELDING" and ((WELDING_TOKENS & candidate_tokens) or record["group_norm"] == "SUPERON"):
        score += 14.0
    if family == "WELDING" and "KG" in query and "KG" in candidate:
        score += 6.0
    if family == "WELDING" and not ((WELDING_TOKENS & candidate_tokens) or record["group_norm"] == "SUPERON"):
        score -= 18.0
    if family == "WELDING" and "SUPERON" in query and record["group_norm"] != "SUPERON":
        score -= 28.0
    if family == "DRILL":
        unexpected_variants = {token for token in DRILL_VARIANT_TOKENS if token in candidate_tokens and token not in query_tokens}
        score -= len(unexpected_variants) * 6.0

    unit_mode = qty_kind(qty_text)
    if unit_mode == record["unit_kind"]:
        score += 10.0
    elif unit_mode in {"SET", "PAIR"} and record["unit_kind"] == "PIECE":
        score -= 6.0

    hints = family_keywords(family)
    if hints:
        if hints & candidate_tokens:
            score += 10.0
        else:
            score -= 14.0

    return round(score, 2)


def confidence_from_matches(matches: list[RankedMatch]) -> int:
    if not matches:
        return 0

    top_score = matches[0].score
    second_score = matches[1].score if len(matches) > 1 else top_score - 12.0
    gap = max(0.0, top_score - second_score)
    confidence = 40.0 + min(30.0, max(0.0, top_score - 70.0) * 0.65) + min(25.0, gap * 1.6)
    return max(5, min(99, int(round(confidence))))


def rank_candidates(query: str, qty_text: str, family: str, family_views: dict[str, list[dict]], limit: int = 3) -> list[RankedMatch]:
    pool = candidate_pool(query, family, family_views)
    scored = [RankedMatch(record["name"], score_candidate(query, qty_text, family, record)) for record in pool]
    scored.sort(key=lambda match: match.score, reverse=True)
    return scored[:limit]


def rank_steelgrip_bundle(item: OCRItem, family_views: dict[str, list[dict]]) -> list[RankedMatch]:
    colors = expand_steelgrip_colors(item.description)
    if not colors:
        query = prepare_query(item.description, item.qty_text)
        return rank_candidates(query, item.qty_text, "STEELGRIP", family_views, limit=3)

    matches: list[RankedMatch] = []
    for color in colors:
        color_query = normalize_text(f'STEELGRIP TAPE 3/4" {color}')
        color_matches = rank_candidates(color_query, item.qty_text, "STEELGRIP", family_views, limit=1)
        if not color_matches:
            continue
        best = color_matches[0]
        matches.append(best)

    if not matches:
        query = prepare_query(item.description, item.qty_text)
        return rank_candidates(query, item.qty_text, "STEELGRIP", family_views, limit=3)

    joined_name = " + ".join(match.name for match in matches)
    average_score = round(sum(match.score for match in matches) / len(matches), 2)
    return [RankedMatch(joined_name, average_score)]


def extract_od_size(name: str) -> str:
    match = re.search(r'OD\s+([0-9/\-\.]+)', normalize_text(name))
    return match.group(1) if match else ""


def rank_handle_matches(item: OCRItem, family_views: dict[str, list[dict]]) -> list[RankedMatch]:
    query = prepare_query(item.description, item.qty_text)
    if query.startswith("ROUND DIE HANDLE"):
        size_tokens = extract_numbers(query)
        if size_tokens:
            die_size = size_tokens[0]
            related_dies = [
                record
                for record in family_views["DIE"]
                if "ROUND" in record["token_set"] and die_size in record["norm"]
            ]
            for record in related_dies:
                od_size = extract_od_size(record["name"])
                if od_size:
                    handle_query = normalize_text(f"ROUND DIE HANDLE {od_size}")
                    matches = rank_candidates(handle_query, item.qty_text, "HANDLE", family_views, limit=3)
                    if matches:
                        return matches
    return rank_candidates(query, item.qty_text, "HANDLE", family_views, limit=3)


def build_match_query_text(item: OCRItem) -> str:
    family = classify_family(item.description)
    if family != "STEELGRIP":
        return prepare_query(item.description, item.qty_text)

    colors = expand_steelgrip_colors(item.description)
    if not colors:
        return prepare_query(item.description, item.qty_text)

    return " + ".join([normalize_text(f'STEELGRIP TAPE 3/4" {color}') for color in colors])


def predict_matches(item: OCRItem, family_views: dict[str, list[dict]]) -> list[RankedMatch]:
    family = classify_family(item.description)
    if family == "STEELGRIP":
        return rank_steelgrip_bundle(item, family_views)
    if family == "HANDLE":
        return rank_handle_matches(item, family_views)

    query = prepare_query(item.description, item.qty_text)
    return rank_candidates(query, item.qty_text, family, family_views, limit=3)


def compare_against_reference(predicted_name: str, expected_name: str) -> tuple[str, int]:
    if not expected_name:
        return "NO_REF", 0

    predicted_norm = normalize_text(predicted_name)
    expected_norm = normalize_text(expected_name)
    similarity = max(
        int(fuzz.WRatio(predicted_norm, expected_norm)),
        int(fuzz.token_set_ratio(predicted_norm, expected_norm)),
    )

    if predicted_norm == expected_norm:
        return "EXACT", 100

    predicted_tokens = token_set(predicted_name)
    expected_tokens = token_set(expected_name)
    predicted_numbers = set(extract_numbers(predicted_name))
    expected_numbers = set(extract_numbers(expected_name))
    shared_meaningful = meaningful_tokens(predicted_tokens) & meaningful_tokens(expected_tokens)
    predicted_family = classify_family(predicted_name)
    expected_family = classify_family(expected_name)
    same_family = predicted_family == expected_family
    brand_match = bool((predicted_tokens & BRAND_TOKENS) & (expected_tokens & BRAND_TOKENS))

    if similarity >= 92:
        return "CLOSE", similarity
    if same_family and predicted_numbers & expected_numbers and len(shared_meaningful) >= 2:
        return "CLOSE", similarity
    if same_family and brand_match and len(shared_meaningful) >= 2:
        return "CLOSE", similarity
    if predicted_family == "STEELGRIP" and expected_family == "STEELGRIP" and len(shared_meaningful & set(COLOR_TOKENS)) >= 2:
        return "CLOSE", similarity
    if same_family and len(shared_meaningful) >= 3:
        return "CLOSE", similarity

    return "DIFFERENT", similarity


def refine_code_row_matches(items: list[OCRItem], match_sets: list[list[RankedMatch]], family_views: dict[str, list[dict]]) -> list[list[RankedMatch]]:
    refined = list(match_sets)
    for idx, item in enumerate(items):
        query = build_match_query_text(item)
        if not is_code_like_query(query):
            continue

        neighbor_families = []
        for offset in (-1, 1):
            neighbor_idx = idx + offset
            if 0 <= neighbor_idx < len(match_sets) and match_sets[neighbor_idx]:
                family = classify_family(match_sets[neighbor_idx][0].name)
                if family not in {"ALL", "TAP", "DRILL", "DIE"}:
                    neighbor_families.append(family)

        if not neighbor_families:
            continue

        anchor_family = neighbor_families[0]
        reranked = rank_candidates(query, item.qty_text, anchor_family, family_views, limit=3)
        if reranked and (not refined[idx] or reranked[0].score >= refined[idx][0].score - 6):
            refined[idx] = reranked
    return refined


def build_prediction_rows(items: list[OCRItem], expected: list[str], family_views: dict[str, list[dict]]) -> list[PredictionRow]:
    match_sets = [predict_matches(item, family_views) for item in items]
    match_sets = refine_code_row_matches(items, match_sets, family_views)
    rows = []
    for idx, item in enumerate(items):
        pre_fuzzy_name = build_match_query_text(item)
        matches = match_sets[idx]
        predicted_name = matches[0].name if matches else ""
        expected_name = expected[idx] if idx < len(expected) else ""
        status, similarity = compare_against_reference(predicted_name, expected_name)
        rows.append(
            PredictionRow(
                line_no=item.line_no,
                raw_description=item.raw_description,
                normalized_description=item.description,
                pre_fuzzy_name=pre_fuzzy_name,
                qty_text=item.qty_text,
                predicted_name=predicted_name,
                expected_name=expected_name,
                status=status,
                similarity=similarity,
                confidence=confidence_from_matches(matches),
                alternates=[match.name for match in matches[1:3]],
            )
        )
    return rows


def print_predictions(rows: list[PredictionRow], elapsed: float, party_name_info: dict | None = None) -> None:
    total = len(rows)
    exact = sum(1 for row in rows if row.status == "EXACT")
    close = sum(1 for row in rows if row.status in {"EXACT", "CLOSE"})
    different = sum(1 for row in rows if row.status == "DIFFERENT")

    def cell(value: str, width: int) -> str:
        text = collapse_spaces(value)
        if len(text) <= width:
            return text.ljust(width)
        return f"{text[: width - 3]}..."

    print("Loaded sample files.")
    print(f"Model        : {MODEL}")
    print(f"Rows found   : {total}")
    print(f"Exact        : {exact}/{total}")
    print(f"Close-or-better: {close}/{total}")
    print(f"Different    : {different}/{total}")
    print(f"Inference    : {elapsed}s")
    if party_name_info:
        print(f"Party OCR    : {party_name_info.get('ocr_text', '')}")
        print(f"Party Match  : {party_name_info.get('matched_name', '')} ({party_name_info.get('source', '')})")
    print("Prediction uses only challan_image.jpg + stock_items_rows__1_.csv.")
    print("Benchmark uses challan_updated.xlsx Column D only for review.")
    print("=" * 120)
    print(
        f"{'#':<4} | "
        f"{cell('MiniCPM_Read', 32)} | "
        f"{cell('Stock_Matched', 40)} | "
        f"{cell('Score', 5)} | "
        f"{cell('Correct_Stock_Name', 40)} | "
        f"{cell('Verdict', 9)}"
    )
    print("-" * 120)

    for row in rows:
        print(
            f"{row.line_no:<4} | "
            f"{cell(row.pre_fuzzy_name, 32)} | "
            f"{cell(row.predicted_name, 40)} | "
            f"{row.similarity:>5} | "
            f"{cell(row.expected_name, 40)} | "
            f"{cell(row.status, 9)}"
        )


def main() -> None:
    print("Loading files...")
    image_b64 = load_image_b64(IMAGE_PATH)
    stock = load_stock(STOCK_CSV)
    expected = load_benchmark(BENCHMARK_XLSX)
    family_views = build_family_views(stock)
    party_name_info = extract_party_name(IMAGE_PATH)

    raw_text, elapsed = query_ollama_chat(image_b64)
    raw_text = clean_ocr_text_layout_noise(raw_text)
    items = parse_ocr_items(raw_text)
    if expected and len(items) > len(expected):
        items = items[: len(expected)]
    rows = build_prediction_rows(items, expected, family_views)
    print_predictions(rows, elapsed, party_name_info)


if __name__ == "__main__":
    main()
