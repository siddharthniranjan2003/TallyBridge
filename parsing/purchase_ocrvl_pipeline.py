"""
Vendor-specific PaddleOCR-VL purchase pipeline (the `ocr=vlm` engine).

The VLM server is used only for OCR/layout extraction. Its output is then parsed
through the same vendor-specific style as the Gmail purchase project before any
duplicacy, matching, or voucher building happens.
"""
from __future__ import annotations

import base64
import re
import time
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from lib.env import make_env_loader
from lib.numeric import decimal_value, parse_date_to_iso, round2
from lib.text import normalize_space
from purchase.company_context import resolve_supabase_company_context
from purchase.models import PurchaseRawItem
from purchase.vlm_vendor_parser import build_ocr_lines, parse_vlm_invoice
from purchase.voucher_builder import (
    best_effort_party_ledger_name,
    best_effort_purchase_ledger_name,
    build_voucher_payload,
    combine_ocr_items,
    match_item_to_live_stock,
    normalize_tax_entries,
)

_env = make_env_loader(Path(__file__).resolve().parent / ".env")

VLM_SERVER_URL = _env("MINICPM_VLM_SERVER_URL", "http://127.0.0.1:5006/parse")
VLM_REQUEST_TIMEOUT = int(_env("MINICPM_VLM_REQUEST_TIMEOUT", "600") or "600")
DEFAULT_COMPANY_NAME = (
    _env("MINICPM_PURCHASE_COMPANY", "")
    or _env("TALLY_COMPANY", "")
    or "K V ENTERPRISES"
)
DEFAULT_MATCH_THRESHOLD = float(_env("MINICPM_PURCHASE_MIN_MATCH_SCORE", "56") or "56")
VENDOR_SAFE_ENDPOINTS: dict[str, dict[str, str]] = {
    "purchase_addison": {
        "expected_vendor": "ADDISON",
        "mode": "purchase_addison_safe",
        "parser": "vlm_vendor_addison_safe",
    },
    "purchase_cp": {
        "expected_vendor": "CP",
        "mode": "purchase_cp_safe",
        "parser": "vlm_vendor_cp_safe",
    },
    "purchase_emkay": {
        "expected_vendor": "ET",
        "mode": "purchase_emkay_safe",
        "parser": "vlm_vendor_emkay_safe",
    },
    "purchase_forbes": {
        "expected_vendor": "TOTEM",
        "mode": "purchase_forbes_safe",
        "parser": "vlm_vendor_forbes_safe",
    },
    "purchase_grindwell": {
        "expected_vendor": "GNL",
        "mode": "purchase_grindwell_safe",
        "parser": "vlm_vendor_grindwell_safe",
    },
    "purchase_pidilite": {
        "expected_vendor": "PIDILITE",
        "mode": "purchase_pidilite_safe",
        "parser": "vlm_vendor_pidilite_safe",
    },
    "purchase_rr": {
        "expected_vendor": "RR",
        "mode": "purchase_rr_safe",
        "parser": "vlm_vendor_rr_safe",
    },
    "purchase_stanley": {
        "expected_vendor": "STANLEY",
        "mode": "purchase_stanley_safe",
        "parser": "vlm_vendor_stanley_safe",
    },
    "purchase_wikus": {
        "expected_vendor": "WIKUS",
        "mode": "purchase_wikus_safe",
        "parser": "vlm_vendor_wikus_safe",
    },
}
VENDOR_SAFE_ENDPOINTS_BY_VENDOR: dict[str, tuple[str, dict[str, str]]] = {
    config["expected_vendor"]: (invoice_mode, config)
    for invoice_mode, config in VENDOR_SAFE_ENDPOINTS.items()
}


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


def _build_preview_raw_items(
    description_rows: list[dict[str, Any]],
    numeric_rows: list[dict[str, Any]],
) -> tuple[list[PurchaseRawItem], list[str]]:
    warnings: list[str] = []
    if len(description_rows) != len(numeric_rows):
        warnings.append(
            f"Description row count ({len(description_rows)}) did not match numeric row count ({len(numeric_rows)})."
        )

    row_count = min(len(description_rows), len(numeric_rows))
    items: list[PurchaseRawItem] = []
    for index in range(row_count):
        description_row = description_rows[index] or {}
        numeric_row = numeric_rows[index] or {}

        raw_description = str(description_row.get("raw_description", "") or "").strip()
        item_code = str(description_row.get("item_code", "") or "").strip()
        quantity = decimal_value(numeric_row.get("quantity", 0))
        rate = decimal_value(numeric_row.get("rate", 0))
        amount = decimal_value(numeric_row.get("amount", 0))
        unit = str(numeric_row.get("unit", "") or "").strip()

        if not raw_description and not item_code:
            continue
        if quantity <= 0 and rate <= 0 and amount <= 0:
            continue

        items.append(
            PurchaseRawItem(
                item_code=item_code,
                raw_description=raw_description,
                quantity=quantity,
                amount=amount,
                rate=rate,
                unit=unit,
            )
        )
    return items, warnings


def _normalize_vendor_preview_items(
    vendor: str,
    items: list[PurchaseRawItem],
) -> tuple[list[PurchaseRawItem], list[str]]:
    warnings: list[str] = []
    if vendor != "CP":
        return items, warnings

    normalized_items: list[PurchaseRawItem] = []
    for item in items:
        description = normalize_space(item.raw_description)
        unit = normalize_space(item.unit).upper()
        quantity = item.quantity
        rate = item.rate

        pack_match = re.search(r"\(\s*PACK\s+OF\s+(\d+(?:\.\d+)?)\s*\)", description, re.IGNORECASE)
        if pack_match:
            pack_size = decimal_value(pack_match.group(1))
            if pack_size > 0:
                quantity = round2(quantity * pack_size)
                if rate > 0:
                    rate = round2(rate / pack_size)
                unit = "NOS"
                warnings.append(
                    f"Normalized CP pack item `{description}` using PACK OF {pack_size}: quantity/rate converted to per-piece."
                )

        elif unit in {"PCS", "PIECES"}:
            unit = "NOS"

        normalized_items.append(
            PurchaseRawItem(
                item_code=item.item_code,
                raw_description=item.raw_description,
                quantity=quantity,
                amount=item.amount,
                rate=rate,
                unit=unit or item.unit,
            )
        )

    return normalized_items, warnings


def _cp_header_is_usable(header_data: dict[str, Any]) -> bool:
    invoice_number = normalize_space(header_data.get("invoice_number", ""))
    invoice_date = normalize_space(header_data.get("invoice_date", ""))
    if not invoice_number or not invoice_date:
        return False
    if "THIS IS A COMPUTER GENERATED INVOICE" in invoice_date.upper():
        return False
    try:
        parse_date_to_iso(invoice_date)
    except ValueError:
        return False
    return True


def _cp_tax_entries_are_usable(header_data: dict[str, Any], raw_items: list[PurchaseRawItem]) -> bool:
    tax_entries = normalize_tax_entries(header_data.get("tax_entries", []))
    if not tax_entries:
        return False
    subtotal = round2(sum((item.amount for item in raw_items), Decimal("0")))
    tax_total = round2(sum((decimal_value(entry.get("amount", 0)) for entry in tax_entries), Decimal("0")))
    if subtotal > 0 and tax_total >= round2(subtotal * Decimal("0.50")):
        return False
    return True


def _merge_cp_header_fallback(
    input_path: Path,
    parsed: dict[str, Any],
    raw_items: list[PurchaseRawItem],
) -> dict[str, Any]:
    if str(parsed.get("vendor", "") or "") != "CP":
        return parsed

    header_data = dict(parsed.get("header_data", {}) or {})
    need_header = not _cp_header_is_usable(header_data)
    need_tax = not _cp_tax_entries_are_usable(header_data, raw_items)
    if not need_header and not need_tax:
        return parsed

    try:
        fallback_vlm = call_vlm_server(input_path, use_layout_detection=False)
        fallback_parsed = parse_vlm_server_result(fallback_vlm)
    except Exception as exc:  # noqa: BLE001 - keep preview route soft-failing
        warnings = list(parsed.get("warnings", []))
        warnings.append(f"CP secondary OCR pass failed: {exc}")
        parsed = dict(parsed)
        parsed["warnings"] = warnings
        return parsed

    fallback_header = dict(fallback_parsed.get("header_data", {}) or {})
    warnings = list(parsed.get("warnings", []))
    used_fallback = False

    if need_header and _cp_header_is_usable(fallback_header):
        header_data["invoice_number"] = normalize_space(fallback_header.get("invoice_number", ""))
        header_data["invoice_date"] = normalize_space(fallback_header.get("invoice_date", ""))

        fallback_vendor_name = normalize_space(fallback_header.get("vendor_name", ""))
        if fallback_vendor_name and "CP GRAT-EX" in fallback_vendor_name.upper():
            header_data["vendor_name"] = fallback_vendor_name

        fallback_customer = normalize_space(fallback_header.get("customer_name", ""))
        if fallback_customer and fallback_customer.upper() not in {"SI NO.", "SI NO"}:
            header_data["customer_name"] = fallback_customer

        warnings.append("Recovered CP invoice header fields using a secondary OCR pass without layout detection.")
        used_fallback = True

    if need_tax and _cp_tax_entries_are_usable(fallback_header, raw_items):
        header_data["tax_entries"] = list(fallback_header.get("tax_entries", []) or [])
        warnings.append("Recovered CP tax entries using a secondary OCR pass without layout detection.")
        used_fallback = True

    if not used_fallback:
        return parsed

    parsed = dict(parsed)
    parsed["header_data"] = header_data
    parsed["warnings"] = warnings
    return parsed


def _build_match_preview(
    raw_items: list[PurchaseRawItem],
    vendor: str,
    context: dict[str, Any],
    min_match_score: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    stock_rows = context.get("stock_items", [])
    matched_items: list[dict[str, Any]] = []
    weak_matches: list[dict[str, Any]] = []

    for raw_item in raw_items:
        stock_match = match_item_to_live_stock(raw_item, vendor, stock_rows)

        match_trace = stock_match.trace or {}
        preview_row = {
            "item_code": raw_item.item_code,
            "raw_description": raw_item.raw_description,
            "canonical_query": stock_match.canonical_query,
            "stock_item_name": stock_match.stock_item_name,
            "quantity": float(raw_item.quantity),
            "rate": float(round2(raw_item.rate)),
            "amount": float(round2(raw_item.amount)),
            "unit": stock_match.unit or raw_item.unit or "NOS",
            "match_score": stock_match.score,
            "match_group": stock_match.group_name,
            "match_source": "Matching_Algorithem",
            "match_trace": match_trace,
        }
        matched_items.append(preview_row)
        if stock_match.score < min_match_score:
            weak_matches.append(preview_row)

    return matched_items, weak_matches


def _build_vendor_tally_payload(
    *,
    company_name: str,
    vendor: str,
    header_data: dict[str, Any],
    matched_items: list[dict[str, Any]],
    ledgers: list[dict[str, Any]],
) -> dict[str, Any]:
    party_ledger_name = best_effort_party_ledger_name(vendor, header_data)
    purchase_ledger_name = best_effort_purchase_ledger_name()
    tax_entries = normalize_tax_entries(header_data.get("tax_entries", []))
    subtotal = round2(sum((decimal_value(item.get("amount", 0)) for item in matched_items), Decimal("0")))
    tax_total = round2(sum((decimal_value(entry.get("amount", 0)) for entry in tax_entries), Decimal("0")))
    invoice_total = round2(decimal_value(header_data.get("invoice_total", 0)))
    if invoice_total <= 0:
        invoice_total = round2(subtotal + tax_total)

    invoice_number = normalize_space(header_data.get("invoice_number", ""))
    invoice_date = normalize_space(header_data.get("invoice_date", ""))
    try:
        voucher_date = parse_date_to_iso(invoice_date) if invoice_date else ""
    except ValueError:
        voucher_date = ""
    narration = f"Purchase invoice {invoice_number}" if invoice_number else "Purchase invoice"

    return {
        "company_name": company_name,
        "voucher_payload": {
            "voucher_type": "Purchase",
            "date": voucher_date,
            "party_ledger_name": party_ledger_name,
            "narration": narration,
            "items": [
                {
                    "stock_item_name": item.get("stock_item_name", ""),
                    "qty": float(item.get("quantity", 0) or 0),
                    "rate": float(item.get("rate", 0) or 0),
                    "amount": float(item.get("amount", 0) or 0),
                }
                for item in matched_items
            ],
            "ledger_entries": [
                {
                    "ledger_name": party_ledger_name,
                    "amount": float(invoice_total),
                },
                {
                    "ledger_name": purchase_ledger_name,
                    "amount": float(subtotal),
                },
                *[
                    {
                        "ledger_name": entry.get("ledger_name", ""),
                        "amount": float(round2(decimal_value(entry.get("amount", 0)))),
                    }
                    for entry in tax_entries
                ],
            ],
        },
    }


def _build_vendor_source_payload(matched_items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "fetched_from": "email",
        "items": [
            {
                "source": normalize_space(
                    " ".join(
                        part
                        for part in (
                            str(item.get("item_code", "") or "").strip(),
                            str(item.get("raw_description", "") or "").strip(),
                        )
                        if part
                    )
                ),
                "score": float(item.get("match_score", 0) or 0),
            }
            for item in matched_items
        ],
    }


def _build_vendor_header_echo(header_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "vendor_name": normalize_space(header_data.get("vendor_name", "")),
        "invoice_number": normalize_space(header_data.get("invoice_number", "")),
        "invoice_date": normalize_space(header_data.get("invoice_date", "")),
        "customer_name": normalize_space(header_data.get("customer_name", "")),
    }


def _unsupported_vendor_response(
    *,
    input_path: Path,
    company_name: str,
    vendor: str,
    header_data: dict[str, Any],
    parsed: dict[str, Any],
    warnings: list[str],
    elapsed: float,
    parser_label: str,
    response_mode: str,
    failure_message: str,
) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "unsupported_vendor",
        "mode": response_mode,
        "parser": parser_label,
        "input_path": str(input_path),
        "company_name": company_name,
        "vendor": vendor,
        **_build_vendor_header_echo(header_data),
        "summary": {
            "row_count": max(len(parsed.get("description_rows", []) or []), len(parsed.get("numeric_rows", []) or [])),
            "matched_count": 0,
            "weak_match_count": 0,
            "inference_seconds": elapsed,
            "safe_to_post": False,
            "parser": parser_label,
        },
        "tally_payload": None,
        "source_payload": {"fetched_from": "email", "items": []},
        "validations": {
            "safe_to_post": False,
            "failures": [failure_message],
            "warnings": warnings,
        },
        "parsed": {
            "header": header_data,
            "description_rows": parsed.get("description_rows", []),
            "numeric_rows": parsed.get("numeric_rows", []),
            "warnings": warnings,
            "line_count": int(parsed.get("line_count", 0) or 0),
            "page_count": int(parsed.get("page_count", 1) or 1),
            "raw_markdown": parsed.get("raw_markdown", ""),
            "page_markdown": parsed.get("page_markdown", []),
            "layout": parsed.get("layout", []),
            "lines": parsed.get("lines", []),
        },
        "raw_items": [],
        "matched_items": [],
        "weak_matches": [],
    }


def _validate_vendor_safe_extraction(
    *,
    vendor: str,
    header_data: dict[str, Any],
    raw_items: list[PurchaseRawItem],
    matched_items: list[dict[str, Any]],
    weak_matches: list[dict[str, Any]],
    company_context: dict[str, Any],
    min_match_score: float,
) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    warnings: list[str] = []

    if not raw_items:
        failures.append(f"No {vendor} item rows were extracted from the VLM output.")
    if len(matched_items) != len(raw_items):
        failures.append(
            f"Matched item count ({len(matched_items)}) did not match extracted item count ({len(raw_items)})."
        )

    invoice_date = normalize_space(header_data.get("invoice_date", ""))
    if not invoice_date:
        failures.append("Invoice date could not be extracted.")
    else:
        try:
            parse_date_to_iso(invoice_date)
        except ValueError:
            failures.append(f"Invoice date could not be normalized safely: {invoice_date}")

    _ = company_context

    for item in raw_items:
        if item.quantity <= 0:
            failures.append(f"Item `{item.item_code or item.raw_description}` has non-positive quantity.")
        if item.rate <= 0:
            failures.append(f"Item `{item.item_code or item.raw_description}` has non-positive rate.")
        if item.amount <= 0:
            failures.append(f"Item `{item.item_code or item.raw_description}` has non-positive amount.")

    for item in matched_items:
        if not normalize_space(item.get("stock_item_name", "")):
            failures.append(f"Matched stock name missing for `{item.get('raw_description', '')}`.")
        if float(item.get("match_score", 0) or 0) < min_match_score:
            failures.append(
                f"Match score {float(item.get('match_score', 0) or 0):.2f} is below the safe threshold for `{item.get('raw_description', '')}`."
            )

    if weak_matches:
        warnings.append(
            f"{len(weak_matches)} {vendor} items are below the safe match threshold of {min_match_score:.0f}."
        )

    return failures, warnings


def _run_vendor_vl_safe_extract_from_vlm_result(
    *,
    input_path: Path,
    vlm_result: dict[str, Any],
    expected_vendor: str,
    response_mode: str,
    parser_label: str,
    company_name: str = DEFAULT_COMPANY_NAME,
    min_match_score: float = DEFAULT_MATCH_THRESHOLD,
) -> dict[str, Any]:
    started_at = time.time()
    parsed = parse_vlm_server_result(vlm_result)
    description_rows = parsed["description_rows"]
    numeric_rows = parsed["numeric_rows"]
    raw_items, item_warnings = _build_preview_raw_items(description_rows, numeric_rows)
    vendor = str(parsed.get("vendor", "") or "")
    warnings = list(parsed.get("warnings", []))
    warnings.extend(item_warnings)
    raw_items, normalization_warnings = _normalize_vendor_preview_items(vendor, raw_items)
    warnings.extend(normalization_warnings)
    parsed["warnings"] = warnings
    parsed = _merge_cp_header_fallback(input_path, parsed, raw_items)
    header_data = parsed["header_data"]
    warnings = list(parsed.get("warnings", []))
    elapsed = round(time.time() - started_at, 2)

    base_payload: dict[str, Any] = {
        "ok": True,
        "status": "partial_success",
        "mode": response_mode,
        "parser": parser_label,
        "input_path": str(input_path),
        "company_name": company_name,
        "vendor": vendor,
        **_build_vendor_header_echo(header_data),
        "summary": {
            "row_count": len(raw_items) or max(len(description_rows), len(numeric_rows)),
            "matched_count": 0,
            "weak_match_count": 0,
            "inference_seconds": elapsed,
            "safe_to_post": False,
            "parser": parser_label,
        },
        "tally_payload": None,
        "source_payload": {"fetched_from": "email", "items": []},
        "validations": {
            "safe_to_post": False,
            "failures": [],
            "warnings": warnings,
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
    }

    if vendor != expected_vendor:
        return _unsupported_vendor_response(
            input_path=input_path,
            company_name=company_name,
            vendor=vendor,
            header_data=header_data,
            parsed=parsed,
            warnings=warnings,
            elapsed=elapsed,
            parser_label=parser_label,
            response_mode=response_mode,
            failure_message=f"Expected vendor {expected_vendor} for this endpoint, but parsed `{vendor or 'UNKNOWN'}`.",
        )

    try:
        context = resolve_supabase_company_context(company_name)
        matched_items, weak_matches = _build_match_preview(raw_items, vendor, context, min_match_score)
    except Exception as exc:  # noqa: BLE001 - keep route soft-failing
        base_payload["ok"] = False
        base_payload["status"] = "matching_failed"
        base_payload["validations"]["failures"] = [f"{expected_vendor} matching failed: {exc}"]
        return base_payload

    failures, validation_warnings = _validate_vendor_safe_extraction(
        vendor=expected_vendor,
        header_data=header_data,
        raw_items=raw_items,
        matched_items=matched_items,
        weak_matches=weak_matches,
        company_context=context,
        min_match_score=min_match_score,
    )
    warnings.extend(validation_warnings)

    source_payload = _build_vendor_source_payload(matched_items)
    safe_to_post = not failures

    response = {
        **base_payload,
        "ok": safe_to_post,
        "status": "success" if safe_to_post else "unsafe",
        "summary": {
            **base_payload["summary"],
            "matched_count": len(matched_items),
            "weak_match_count": len(weak_matches),
            "safe_to_post": safe_to_post,
        },
        "source_payload": source_payload,
        "validations": {
            "safe_to_post": safe_to_post,
            "failures": failures,
            "warnings": warnings,
        },
        "matched_items": matched_items,
        "weak_matches": weak_matches,
    }

    try:
        response["tally_payload"] = _build_vendor_tally_payload(
            company_name=company_name,
            vendor=expected_vendor,
            header_data=header_data,
            matched_items=matched_items,
            ledgers=context.get("ledgers", []),
        )
    except Exception as exc:  # noqa: BLE001 - keep route soft-failing
        response["ok"] = False
        response["status"] = "unsafe"
        response["summary"]["safe_to_post"] = False
        response["validations"]["safe_to_post"] = False
        response["validations"]["failures"] = response["validations"].get("failures", []) + [
            f"Could not build {expected_vendor} tally payload safely: {exc}"
        ]
        response["tally_payload"] = None

    return response


def _vlm_request_url(
    *,
    document_unwarping: bool | None = None,
    use_layout_detection: bool | None = None,
    prompt_label: str | None = None,
) -> str:
    if document_unwarping is None and use_layout_detection is None and not prompt_label:
        return VLM_SERVER_URL
    parts = urlsplit(VLM_SERVER_URL)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if document_unwarping is not None:
        query["document_unwarping"] = "on" if document_unwarping else "off"
    if use_layout_detection is not None:
        query["use_layout_detection"] = "on" if use_layout_detection else "off"
    if prompt_label:
        query["prompt_label"] = str(prompt_label).strip()
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def call_vlm_server(
    file_path: Path,
    *,
    document_unwarping: bool | None = None,
    use_layout_detection: bool | None = None,
    prompt_label: str | None = None,
) -> dict[str, Any]:
    payload = {
        "file_base64": base64.b64encode(file_path.read_bytes()).decode("utf-8"),
        "filename": file_path.name,
    }
    try:
        response = requests.post(
            _vlm_request_url(
                document_unwarping=document_unwarping,
                use_layout_detection=use_layout_detection,
                prompt_label=prompt_label,
            ),
            json=payload,
            timeout=VLM_REQUEST_TIMEOUT,
        )
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
            context.get("purchase_matching_exact_map"),
        )
    except Exception as exc:  # noqa: BLE001 - mirror the soft-failure behavior expected by the endpoint
        warnings.append(f"Matching skipped: {exc}")
        return base_payload

    queue_payload = _project_gmail_queue_payload(company_name, build_result["voucher_payload"])
    if isinstance(build_result.get("source_payload"), dict):
        queue_payload["source_payload"] = build_result["source_payload"]
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


def run_purchase_vl_matching_preview(
    input_path: Path,
    *,
    company_name: str = DEFAULT_COMPANY_NAME,
    min_match_score: float = DEFAULT_MATCH_THRESHOLD,
    document_unwarping: bool | None = None,
    use_layout_detection: bool | None = None,
    prompt_label: str | None = None,
) -> dict[str, Any]:
    started_at = time.time()
    vlm_result = call_vlm_server(
        Path(input_path),
        document_unwarping=document_unwarping,
        use_layout_detection=use_layout_detection,
        prompt_label=prompt_label,
    )
    parsed = parse_vlm_server_result(vlm_result)
    header_data = parsed["header_data"]
    description_rows = parsed["description_rows"]
    numeric_rows = parsed["numeric_rows"]
    warnings = list(parsed.get("warnings", []))
    vendor = str(parsed.get("vendor", "") or "")
    raw_items, item_warnings = _build_preview_raw_items(description_rows, numeric_rows)
    warnings.extend(item_warnings)

    elapsed = round(time.time() - started_at, 2)
    payload: dict[str, Any] = {
        "ok": True,
        "status": "partial_success",
        "mode": "purchase_matching_preview",
        "parser": "vlm_vendor_raw_match",
        "input_path": str(input_path),
        "company_name": company_name,
        "master_source": "supabase",
        "vendor": vendor,
        "summary": {
            "row_count": len(raw_items) or max(len(description_rows), len(numeric_rows)),
            "matched_count": 0,
            "weak_match_count": 0,
            "inference_seconds": elapsed,
            "master_source": "supabase",
            "parser": "vlm_vendor_raw_match",
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
        "voucher_payload": None,
        "push_queue_payload": None,
        "tally_push": None,
        "n8n": {
            "type": "purchase_matching_preview",
            "company_name": company_name,
            "vendor": vendor,
            "parsed_header": header_data,
            "matched_items": [],
            "weak_matches": [],
        },
    }

    if not vendor:
        warnings.append("Matching skipped: vendor could not be determined from the VLM OCR output.")
        return payload
    if not raw_items:
        warnings.append("Matching skipped: vendor parser did not yield usable purchase item rows.")
        return payload

    try:
        context = resolve_supabase_company_context(company_name)
        matched_items, weak_matches = _build_match_preview(raw_items, vendor, context, min_match_score)
    except Exception as exc:  # noqa: BLE001 - keep route soft-failing
        warnings.append(f"Matching skipped: {exc}")
        return payload

    return {
        **payload,
        "status": "success",
        "master_source": context.get("source", "supabase"),
        "summary": {
            **payload["summary"],
            "matched_count": len(matched_items),
            "weak_match_count": len(weak_matches),
            "master_source": context.get("source", "supabase"),
        },
        "matched_items": matched_items,
        "weak_matches": weak_matches,
        "n8n": {
            "type": "purchase_matching_preview",
            "company_name": company_name,
            "vendor": vendor,
            "parsed_header": header_data,
            "matched_items": matched_items,
            "weak_matches": weak_matches,
        },
    }


def run_vendor_vl_safe_extract(
    input_path: Path,
    *,
    expected_vendor: str,
    response_mode: str,
    parser_label: str,
    company_name: str = DEFAULT_COMPANY_NAME,
    min_match_score: float = DEFAULT_MATCH_THRESHOLD,
    document_unwarping: bool | None = None,
    use_layout_detection: bool | None = None,
    prompt_label: str | None = None,
) -> dict[str, Any]:
    vlm_result = call_vlm_server(
        Path(input_path),
        document_unwarping=document_unwarping,
        use_layout_detection=use_layout_detection,
        prompt_label=prompt_label,
    )
    return _run_vendor_vl_safe_extract_from_vlm_result(
        input_path=Path(input_path),
        vlm_result=vlm_result,
        expected_vendor=expected_vendor,
        response_mode=response_mode,
        parser_label=parser_label,
        company_name=company_name,
        min_match_score=min_match_score,
    )


def run_addison_vl_safe_extract(
    input_path: Path,
    *,
    company_name: str = DEFAULT_COMPANY_NAME,
    min_match_score: float = DEFAULT_MATCH_THRESHOLD,
    document_unwarping: bool | None = None,
    use_layout_detection: bool | None = None,
    prompt_label: str | None = None,
) -> dict[str, Any]:
    config = VENDOR_SAFE_ENDPOINTS["purchase_addison"]
    return run_vendor_vl_safe_extract(
        input_path,
        expected_vendor=config["expected_vendor"],
        response_mode=config["mode"],
        parser_label=config["parser"],
        company_name=company_name,
        min_match_score=min_match_score,
        document_unwarping=document_unwarping,
        use_layout_detection=use_layout_detection,
        prompt_label=prompt_label,
    )


def run_all_vendors_vl_safe_extract(
    input_path: Path,
    *,
    company_name: str = DEFAULT_COMPANY_NAME,
    min_match_score: float = DEFAULT_MATCH_THRESHOLD,
    document_unwarping: bool | None = None,
    use_layout_detection: bool | None = None,
    prompt_label: str | None = None,
) -> dict[str, Any]:
    vlm_result = call_vlm_server(
        Path(input_path),
        document_unwarping=document_unwarping,
        use_layout_detection=use_layout_detection,
        prompt_label=prompt_label,
    )
    parsed = parse_vlm_server_result(vlm_result)
    detected_vendor = str(parsed.get("vendor", "") or "")
    route = VENDOR_SAFE_ENDPOINTS_BY_VENDOR.get(detected_vendor)
    if not route:
        header_data = parsed.get("header_data", _default_header_data())
        warnings = list(parsed.get("warnings", []))
        response = _unsupported_vendor_response(
            input_path=Path(input_path),
            company_name=company_name,
            vendor=detected_vendor,
            header_data=header_data,
            parsed=parsed,
            warnings=warnings,
            elapsed=0.0,
            parser_label="vlm_vendor_all_router",
            response_mode="purchase_all_vendors_safe",
            failure_message=f"Detected vendor `{detected_vendor or 'UNKNOWN'}` is not mapped to an all_vendors pipeline.",
        )
        response["requested_invoice_mode"] = "all_vendors"
        response["routed_invoice_mode"] = ""
        response["routed_pipeline"] = ""
        return response

    routed_invoice_mode, vendor_config = route
    response = _run_vendor_vl_safe_extract_from_vlm_result(
        input_path=Path(input_path),
        vlm_result=vlm_result,
        expected_vendor=vendor_config["expected_vendor"],
        response_mode=vendor_config["mode"],
        parser_label=vendor_config["parser"],
        company_name=company_name,
        min_match_score=min_match_score,
    )
    response["requested_invoice_mode"] = "all_vendors"
    response["routed_invoice_mode"] = routed_invoice_mode
    response["routed_pipeline"] = vendor_config["mode"]
    return response


__all__ = [
    "DEFAULT_COMPANY_NAME",
    "DEFAULT_MATCH_THRESHOLD",
    "VENDOR_SAFE_ENDPOINTS",
    "VLM_REQUEST_TIMEOUT",
    "VLM_SERVER_URL",
    "call_vlm_server",
    "parse_vlm_server_result",
    "run_addison_vl_safe_extract",
    "run_all_vendors_vl_safe_extract",
    "run_purchase_vl_ocr_header_only",
    "run_purchase_vl_matching_preview",
    "run_purchase_vl_pipeline",
    "run_vendor_vl_safe_extract",
]
