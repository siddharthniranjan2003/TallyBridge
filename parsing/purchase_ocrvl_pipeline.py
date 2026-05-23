"""
Vendor-specific PaddleOCR-VL purchase pipeline (the `ocr=vlm` engine).

The VLM server is used only for OCR/layout extraction. Its output is then parsed
through the same vendor-specific style as the Gmail purchase project before any
duplicacy, matching, or voucher building happens.
"""
from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

import requests

from lib.env import make_env_loader
from lib.numeric import round2
from purchase.company_context import resolve_supabase_company_context
from purchase.models import PurchaseRawItem
from purchase.vlm_vendor_parser import build_ocr_lines, parse_vlm_invoice
from purchase.voucher_builder import build_voucher_payload, combine_ocr_items

_env = make_env_loader(Path(__file__).resolve().parent / ".env")

VLM_SERVER_URL = _env("MINICPM_VLM_SERVER_URL", "http://127.0.0.1:5006/parse")
VLM_REQUEST_TIMEOUT = int(_env("MINICPM_VLM_REQUEST_TIMEOUT", "600") or "600")
DEFAULT_COMPANY_NAME = (
    _env("MINICPM_PURCHASE_COMPANY", "")
    or _env("TALLY_COMPANY", "")
    or "K V ENTERPRISES"
)
DEFAULT_MATCH_THRESHOLD = float(_env("MINICPM_PURCHASE_MIN_MATCH_SCORE", "56") or "56")


def _default_header_data() -> dict[str, Any]:
    return {
        "vendor_name": "",
        "invoice_number": "",
        "invoice_date": "",
        "customer_name": "",
        "discount_percent": 0.0,
        "discount_amount": 0.0,
        "invoice_total": 0.0,
        "tax_entries": [],
        "round_off": 0.0,
    }


def _project_gmail_queue_payload(company_name: str, voucher_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "company_name": company_name,
        "voucher_payload": voucher_payload,
    }


def _serialize_raw_items(items: list[PurchaseRawItem]) -> list[dict[str, Any]]:
    return [
        {
            "item_code": item.item_code,
            "raw_description": item.raw_description,
            "quantity": float(item.quantity),
            "rate": float(round2(item.rate)),
            "amount": float(round2(item.amount)),
            "unit": item.unit,
        }
        for item in items
    ]


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


def parse_vlm_server_result(vlm_result: dict[str, Any]) -> dict[str, Any]:
    try:
        return parse_vlm_invoice(vlm_result)
    except Exception as exc:  # noqa: BLE001 - keep the purchase route soft-failing
        lines = [line.text for line in build_ocr_lines(vlm_result)]
        return {
            "vendor": "",
            "header_data": _default_header_data(),
            "description_rows": [],
            "numeric_rows": [],
            "warnings": [str(exc)],
            "line_count": len(lines),
            "page_count": int(vlm_result.get("page_count", 1) or 1),
            "raw_markdown": str(vlm_result.get("markdown", "") or ""),
            "page_markdown": list(vlm_result.get("page_markdown", []) or []),
            "layout": vlm_result.get("layout", []),
            "lines": lines,
        }


def run_purchase_vl_ocr_header_only(input_path: Path) -> dict[str, Any]:
    vlm_result = call_vlm_server(Path(input_path))
    parsed = parse_vlm_server_result(vlm_result)
    header_data = parsed["header_data"]
    return {
        "ok": True,
        "invoice_number": header_data.get("invoice_number", ""),
        "invoice_date": header_data.get("invoice_date", ""),
        "vendor_name": header_data.get("vendor_name", ""),
        "warnings": list(parsed.get("warnings", [])),
    }


def run_purchase_vl_pipeline(
    input_path: Path,
    *,
    company_name: str = DEFAULT_COMPANY_NAME,
    min_match_score: float = DEFAULT_MATCH_THRESHOLD,
    _preloaded_vlm_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started_at = time.time()
    vlm_result = _preloaded_vlm_result or call_vlm_server(Path(input_path))
    parsed = parse_vlm_server_result(vlm_result)
    header_data = parsed["header_data"]
    description_rows = parsed["description_rows"]
    numeric_rows = parsed["numeric_rows"]
    warnings = list(parsed.get("warnings", []))
    vendor = str(parsed.get("vendor", "") or "")

    raw_items: list[PurchaseRawItem] = []
    if vendor:
        raw_items, item_warnings = combine_ocr_items(vendor, description_rows, numeric_rows)
        warnings.extend(item_warnings)

    elapsed = round(time.time() - started_at, 2)
    base_payload: dict[str, Any] = {
        "ok": True,
        "status": "partial_success",
        "mode": "purchase",
        "parser": "vlm_vendor",
        "input_path": str(input_path),
        "company_name": company_name,
        "master_source": "supabase",
        "vendor": vendor,
        "summary": {
            "row_count": len(raw_items) or max(len(description_rows), len(numeric_rows)),
            "weak_match_count": 0,
            "inference_seconds": elapsed,
            "push_mode": "none",
            "master_source": "supabase",
            "parser": "vlm_vendor",
        },
        "parsed": {
            "header": header_data,
            "description_rows": description_rows,
            "numeric_rows": numeric_rows,
            "warnings": warnings,
            "line_count": int(parsed.get("line_count", 0) or 0),
            "page_count": int(parsed.get("page_count", 1) or 1),
            "raw_markdown": parsed.get("raw_markdown", ""),
            "page_markdown": parsed.get("page_markdown", []),
            "layout": parsed.get("layout", []),
            "lines": parsed.get("lines", []),
        },
        "raw_items": _serialize_raw_items(raw_items),
        "matched_items": [],
        "weak_matches": [],
        "party_name": "",
        "voucher_payload": None,
        "push_queue_payload": None,
        "tally_push": None,
        "n8n": {
            "type": "purchase",
            "company_name": company_name,
            "vendor": vendor,
            "party_name": "",
            "voucher_payload": None,
            "push_queue_payload": None,
            "matched_items": [],
            "weak_matches": [],
            "parsed_header": header_data,
        },
    }

    if not vendor:
        warnings.append("Matching skipped: vendor could not be determined from the VLM OCR output.")
        return base_payload
    if not raw_items:
        warnings.append("Matching skipped: vendor parser did not yield usable purchase item rows.")
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
    except Exception as exc:  # noqa: BLE001 - mirror the soft-failure behavior expected by the endpoint
        warnings.append(f"Matching skipped: {exc}")
        return base_payload

    queue_payload = _project_gmail_queue_payload(company_name, build_result["voucher_payload"])
    return {
        **base_payload,
        "status": "success",
        "master_source": context.get("source", "supabase"),
        "summary": {
            **base_payload["summary"],
            "weak_match_count": len(build_result["weak_matches"]),
            "master_source": context.get("source", "supabase"),
        },
        "matched_items": build_result["matched_items"],
        "weak_matches": build_result["weak_matches"],
        "party_name": build_result["party_name"],
        "voucher_payload": build_result["voucher_payload"],
        "push_queue_payload": queue_payload,
        "n8n": {
            "type": "purchase",
            "company_name": company_name,
            "vendor": vendor,
            "party_name": build_result["party_name"],
            "voucher_payload": build_result["voucher_payload"],
            "push_queue_payload": queue_payload,
            "matched_items": build_result["matched_items"],
            "weak_matches": build_result["weak_matches"],
            "parsed_header": header_data,
        },
    }


__all__ = [
    "DEFAULT_COMPANY_NAME",
    "DEFAULT_MATCH_THRESHOLD",
    "VLM_REQUEST_TIMEOUT",
    "VLM_SERVER_URL",
    "call_vlm_server",
    "parse_vlm_server_result",
    "run_purchase_vl_ocr_header_only",
    "run_purchase_vl_pipeline",
]
