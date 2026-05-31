"""
Ollama/VLM-based purchase pipeline (not used by the server; kept for reference).
Re-exports purchase package symbols for backward compatibility.
"""
from __future__ import annotations

import base64
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests

from lib.env import make_env_loader
from lib.numeric import decimal_value, normalize_decimal_token, parse_date_to_iso, pretty_number, round2
from lib.text import normalize_space
from purchase.company_context import resolve_supabase_company_context
from purchase.models import PurchaseRawItem, StockMatch
from purchase.vendor import detect_vendor
from purchase.voucher_builder import build_purchase_queue_payload, build_voucher_payload, combine_ocr_items

_env = make_env_loader(Path(__file__).resolve().parent / ".env")
DEFAULT_COMPANY_NAME: str = _env("MINICPM_PURCHASE_COMPANY", "") or _env("TALLY_COMPANY", "") or "K V ENTERPRISES"
DEFAULT_MATCH_THRESHOLD: float = float(_env("MINICPM_PURCHASE_MIN_MATCH_SCORE", "56") or "56")

HEADER_PROMPT = """Read this purchase tax invoice image and return only valid JSON.
Schema: {"vendor_name":"","invoice_number":"","invoice_date":"DD-MM-YYYY","customer_name":"","discount_percent":0,"discount_amount":0,"invoice_total":0,"tax_entries":[{"ledger_name":"IGST|CGST|SGST","amount":0}],"round_off":0}
Rules: Read only visible values. Keep numbers as numbers. Output JSON only."""

DESCRIPTION_PROMPT = """Read only the item-code and description columns from this purchase invoice image.
Return only valid JSON: {"items":[{"item_code":"","raw_description":""}]}
Rules: One object per visible item row. Ignore qty/rate/amount/GST. Output JSON only."""

NUMERIC_PROMPT = """Read only the quantity, unit, rate, and taxable value columns from this purchase invoice image.
Return only valid JSON: {"rows":[{"quantity":0,"unit":"","rate":0,"amount":0}]}
Rules: One object per visible item row. Quantity/rate/amount must be numbers. Output JSON only."""


def image_bytes_to_b64(image_bytes: bytes) -> str:
    return base64.b64encode(image_bytes).decode("utf-8")


def extract_json_payload(raw_text: str) -> dict[str, Any]:
    text = re.sub(r"^\s*</think>\s*", "", str(raw_text or "").strip(), flags=re.IGNORECASE)
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.IGNORECASE | re.DOTALL) or re.search(r"(\{.*\})", text, flags=re.DOTALL)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_repair_json(text))


def _repair_json(text: str) -> str:
    repaired: list[str] = []
    in_string = escaped = False
    for i, char in enumerate(text):
        if char == "\\" and in_string and not escaped:
            repaired.append(char)
            escaped = True
            continue
        if char == '"' and not escaped:
            if not in_string:
                in_string = True
                repaired.append(char)
                continue
            lookahead = i + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            next_char = text[lookahead] if lookahead < len(text) else ""
            if next_char in {",", "}", "]", ":"}:
                in_string = False
                repaired.append(char)
            else:
                repaired.append('\\"')
            continue
        repaired.append(char)
        escaped = False
    return "".join(repaired)


def query_ollama_json(image_path: Path, prompt: str, pipeline) -> dict[str, Any]:
    payload = {"model": pipeline.MODEL, "think": False, "messages": [{"role": "system", "content": "You are a strict OCR extraction engine. Return only valid JSON."}, {"role": "user", "content": prompt, "images": [image_bytes_to_b64(image_path.read_bytes())]}], "stream": False, "options": {"temperature": 0}}
    resp = requests.post(pipeline.OLLAMA_CHAT_URL, json=payload, timeout=pipeline.REQUEST_TIMEOUT)
    resp.raise_for_status()
    return extract_json_payload(resp.json()["message"]["content"])


def run_purchase_pipeline_for_image(image_path: Path, pipeline, *, company_name: str = DEFAULT_COMPANY_NAME, push_mode: str = "direct", min_match_score: float = DEFAULT_MATCH_THRESHOLD) -> dict[str, Any]:
    started_at = time.time()
    header_data = query_ollama_json(image_path, HEADER_PROMPT, pipeline)
    description_payload = query_ollama_json(image_path, DESCRIPTION_PROMPT, pipeline)
    numeric_payload = query_ollama_json(image_path, NUMERIC_PROMPT, pipeline)

    vendor = detect_vendor(header_data.get("vendor_name", ""))
    raw_items, item_warnings = combine_ocr_items(vendor, description_payload.get("items", []), numeric_payload.get("rows", []))
    if not raw_items:
        raise ValueError("Purchase OCR did not yield any item rows")

    context = resolve_supabase_company_context(company_name)
    build_result = build_voucher_payload(company_name, vendor, header_data, raw_items, context["stock_items"], context["ledgers"], min_match_score)

    push_result: dict[str, Any] | None = None
    if push_mode == "direct":
        src_python_dir = Path(__file__).resolve().parents[1] / "src" / "python"
        if str(src_python_dir) not in sys.path:
            sys.path.insert(0, str(src_python_dir))
        from tally_pusher import push_vouchers
        push_result = push_vouchers([build_result["voucher_payload"]], company_name)
        if push_result.get("errors"):
            raise RuntimeError("; ".join(push_result.get("line_errors", [])) or "Tally rejected the purchase voucher")

    elapsed = round(time.time() - started_at, 2)
    return {
        "ok": True, "status": "success", "mode": "purchase", "image_path": str(image_path), "company_name": company_name, "master_source": context.get("source", "supabase"), "vendor": vendor,
        "summary": {"row_count": len(raw_items), "weak_match_count": len(build_result["weak_matches"]), "inference_seconds": elapsed, "push_mode": push_mode, "master_source": context.get("source", "supabase")},
        "ocr": {"header": header_data, "description_rows": description_payload.get("items", []), "numeric_rows": numeric_payload.get("rows", []), "warnings": item_warnings},
        "raw_items": [{"item_code": i.item_code, "raw_description": i.raw_description, "quantity": float(i.quantity), "rate": float(round2(i.rate)), "amount": float(round2(i.amount)), "unit": i.unit} for i in raw_items],
        "matched_items": build_result["matched_items"], "weak_matches": build_result["weak_matches"],
        "party_name": build_result["party_name"], "voucher_payload": build_result["voucher_payload"],
        "push_queue_payload": build_purchase_queue_payload(company_name, build_result["voucher_payload"]),
        "tally_push": push_result,
    }
