from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from lib.env import make_env_loader
from lib.numeric import round2
from purchase.vlm_vendor_parser import build_ocr_lines, parse_vlm_invoice
from purchase.voucher_builder import (
    build_alien_voucher_payload,
    build_purchase_queue_payload,
    combine_ocr_items,
)
from purchase.company_context import resolve_supabase_company_context, SUPABASE_URL, SUPABASE_KEY
from purchase.voucher_builder import build_voucher_payload

_env = make_env_loader(Path(__file__).resolve().parent / ".env")

DEFAULT_COMPANY_NAME = (
    _env("MINICPM_PURCHASE_COMPANY", "")
    or _env("TALLY_COMPANY", "")
    or "K V ENTERPRISES"
)
DEFAULT_MATCH_THRESHOLD = float(_env("MINICPM_PURCHASE_MIN_MATCH_SCORE", "56") or "56")
# Vendor-neutral (alien) matching uses a text-forward scorer on a different scale than
# the vendor rule engine, so it gets its own, more conservative threshold: only a
# confident text match replaces the plain OCR description (the user edits the rest).
DEFAULT_ALIEN_MATCH_THRESHOLD = float(_env("MINICPM_ALIEN_MIN_MATCH_SCORE", "72") or "72")


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


def _infer_page_count(markdown: str) -> int:
    totals = [int(match.group(1)) for match in re.finditer(r"\bPage\s+\d+\s+of\s+(\d+)\b", markdown, re.IGNORECASE)]
    if totals:
        return max(totals)
    page_mentions = re.findall(r"\bPage\s+\d+\b", markdown, re.IGNORECASE)
    return max(1, len(page_mentions) or 1)


def _vlm_result_from_docstrange(markdown: str) -> dict[str, Any]:
    return {
        "ok": True,
        "markdown": markdown,
        "page_markdown": [],
        "page_count": _infer_page_count(markdown),
        "layout": [],
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


def _queue_request_payload(company_name: str, build_result: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tally_payload": {
            "company_name": company_name,
            "voucher_payload": build_result["voucher_payload"],
        }
    }
    if isinstance(build_result.get("source_payload"), dict):
        payload["source_payload"] = build_result["source_payload"]
    return payload


def run_docstrange_purchase_all_pipeline(
    *,
    input_path: Path,
    markdown: str,
    html: str,
    company_name: str = DEFAULT_COMPANY_NAME,
    min_match_score: float = DEFAULT_MATCH_THRESHOLD,
) -> dict[str, Any]:
    started_at = time.time()
    warnings: list[str] = []
    vlm_result = _vlm_result_from_docstrange(markdown)

    try:
        parsed = parse_vlm_invoice(vlm_result)
    except Exception as exc:  # noqa: BLE001 - return the markdown for diagnosis instead of failing hard
        lines = [line.text for line in build_ocr_lines(vlm_result)]
        parsed = {
            "vendor": "",
            "header_data": _default_header_data(),
            "description_rows": [],
            "numeric_rows": [],
            "warnings": [str(exc)],
            "line_count": len(lines),
            "page_count": vlm_result["page_count"],
            "raw_markdown": markdown,
            "page_markdown": [],
            "layout": [],
            "lines": lines,
        }

    warnings.extend(parsed.get("warnings", []))
    vendor = str(parsed.get("vendor", "") or "")
    header_data = dict(parsed.get("header_data", {}) or _default_header_data())
    description_rows = list(parsed.get("description_rows", []) or [])
    numeric_rows = list(parsed.get("numeric_rows", []) or [])

    is_alien = not vendor
    raw_items: list[PurchaseRawItem] = []
    if vendor:
        raw_items, item_warnings = combine_ocr_items(
            vendor,
            description_rows,
            numeric_rows,
            repair_descriptions=False,
        )
        warnings.extend(item_warnings)
    else:
        # Alien invoice: keep the plain OCR description as-is (no repair, no matching).
        raw_items, item_warnings = combine_ocr_items(
            "",
            description_rows,
            numeric_rows,
            repair_descriptions=False,
            preserve_raw=True,
        )
        warnings.extend(item_warnings)

    base_payload: dict[str, Any] = {
        "ok": True,
        "status": "partial_success",
        "mode": "docstrange_purchase_all",
        "parser": "docstrange_markdown_vendor_patterns",
        "input_path": str(input_path),
        "company_name": company_name,
        "master_source": "none",
        "vendor": vendor,
        "alien": is_alien,
        "description_repair_enabled": False,
        "summary": {
            "row_count": len(raw_items) or max(len(description_rows), len(numeric_rows)),
            "weak_match_count": 0,
            "inference_seconds": round(time.time() - started_at, 2),
            "master_source": "supabase_live_stock",
            "matching_mode": "live_supabase_stock_match",
            "parser": "docstrange_markdown_vendor_patterns",
        },
        "parsed": {
            "header": header_data,
            "description_rows": description_rows,
            "numeric_rows": numeric_rows,
            "warnings": warnings,
            "line_count": int(parsed.get("line_count", 0) or 0),
            "page_count": int(parsed.get("page_count", 1) or 1),
            "raw_markdown": markdown,
            "page_markdown": list(parsed.get("page_markdown", []) or []),
            "layout": parsed.get("layout", []),
            "lines": parsed.get("lines", []),
        },
        "raw_items": _serialize_raw_items(raw_items),
        "matched_items": [],
        "fuzzy_items": [],
        "weak_matches": [],
        "party_name": "",
        "voucher_payload": None,
        "push_queue_payload": None,
        "push_queue_request_payload": None,
        "source_payload": {"items": []},
        "markdown": markdown,
        "html": html,
    }

    if is_alien:
        # Unrecognized supplier: no vendor rule engine. Extract items generically, then
        # match each plain OCR description VENDOR-NEUTRALLY against this company's live
        # stock catalog (when available); unmatched items keep their raw text. The
        # voucher is flagged alien:true so the user edits it before activation/push.
        if not raw_items:
            warnings.append("Alien invoice: no usable purchase item rows could be parsed.")
            return base_payload
        stock_rows: list[dict[str, Any]] = []
        exact_map = None
        audit_trail_map = None
        if SUPABASE_URL and SUPABASE_KEY:
            try:
                context = resolve_supabase_company_context(company_name)
                stock_rows = context["stock_items"]
                exact_map = context.get("purchase_matching_exact_map")
                audit_trail_map = context.get("audit_trail_map")
            except Exception as exc:  # noqa: BLE001 - matching is best-effort for aliens
                warnings.append(f"Alien matching skipped: could not load live Supabase catalog: {exc}")
        else:
            warnings.append("Alien matching skipped: Supabase not configured; items kept as plain OCR text.")
        build_result = build_alien_voucher_payload(
            company_name,
            header_data,
            raw_items,
            stock_rows=stock_rows,
            min_score=DEFAULT_ALIEN_MATCH_THRESHOLD,
            purchase_matching_exact_map=exact_map,
            audit_trail_map=audit_trail_map,
        )
        push_queue_payload = build_purchase_queue_payload(
            company_name,
            build_result["voucher_payload"],
            build_result.get("source_payload"),
        )
        request_payload = _queue_request_payload(company_name, build_result)
        matching_mode = "alien_generic_stock_match" if stock_rows else "alien_passthrough"
        return {
            **base_payload,
            "status": "success",
            "master_source": matching_mode,
            "summary": {
                **base_payload["summary"],
                "weak_match_count": len(build_result["weak_matches"]),
                "inference_seconds": round(time.time() - started_at, 2),
                "master_source": matching_mode,
                "matching_mode": matching_mode,
            },
            "party_name": build_result["party_name"],
            "matched_items": build_result["matched_items"],
            "fuzzy_items": build_result["matched_items"],
            "weak_matches": build_result["weak_matches"],
            "voucher_payload": build_result["voucher_payload"],
            "push_queue_payload": push_queue_payload,
            "push_queue_request_payload": request_payload,
            "source_payload": build_result.get("source_payload", {"items": []}),
            "n8n": {
                "type": "purchase",
                "alien": True,
                "company_name": company_name,
                "vendor": "",
                "party_name": build_result["party_name"],
                "voucher_payload": build_result["voucher_payload"],
                "push_queue_payload": push_queue_payload,
                "push_queue_request_payload": request_payload,
                "source_payload": build_result.get("source_payload", {"items": []}),
                "matched_items": build_result["matched_items"],
                "weak_matches": build_result["weak_matches"],
                "parsed_header": header_data,
            },
        }
    if not raw_items:
        warnings.append("Matching skipped: vendor parser did not yield usable purchase item rows.")
        return base_payload

    if not (SUPABASE_URL and SUPABASE_KEY):
        warnings.append("Matching skipped: Supabase is not configured (SUPABASE_URL/SUPABASE_KEY).")
        return base_payload

    try:
        context = resolve_supabase_company_context(company_name)
    except Exception as exc:  # noqa: BLE001 - surface the reason instead of crashing the request
        warnings.append(f"Matching skipped: could not load live Supabase catalog: {exc}")
        return base_payload

    build_result = build_voucher_payload(
        company_name,
        vendor,
        header_data,
        raw_items,
        context["stock_items"],
        context["ledgers"],
        min_match_score,
        context.get("purchase_matching_exact_map"),
        context.get("audit_trail_map"),
    )

    push_queue_payload = build_purchase_queue_payload(
        company_name,
        build_result["voucher_payload"],
        build_result.get("source_payload"),
    )
    request_payload = _queue_request_payload(company_name, build_result)

    matched_items = build_result.get("matched_items", [])
    return {
        **base_payload,
        "status": "success",
        "master_source": "supabase_live_stock",
        "summary": {
            **base_payload["summary"],
            "weak_match_count": len(build_result["weak_matches"]),
            "inference_seconds": round(time.time() - started_at, 2),
            "master_source": "supabase_live_stock",
            "matching_mode": "live_supabase_stock_match",
        },
        "matched_items": matched_items,
        "fuzzy_items": matched_items,
        "weak_matches": build_result["weak_matches"],
        "party_name": build_result["party_name"],
        "voucher_payload": build_result["voucher_payload"],
        "push_queue_payload": push_queue_payload,
        "push_queue_request_payload": request_payload,
        "source_payload": build_result.get("source_payload", {"items": []}),
        "n8n": {
            "type": "purchase",
            "company_name": company_name,
            "vendor": vendor,
            "party_name": build_result["party_name"],
            "voucher_payload": build_result["voucher_payload"],
            "push_queue_payload": push_queue_payload,
            "push_queue_request_payload": request_payload,
            "source_payload": build_result.get("source_payload", {"items": []}),
            "matched_items": matched_items,
            "weak_matches": build_result["weak_matches"],
            "parsed_header": header_data,
        },
    }
