from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import requests

from lib.env import make_env_loader
from lib.text import audit_trail_key, normalize_space

_env = make_env_loader(Path(__file__).resolve().parents[1] / ".env")
SUPABASE_URL = _env("SUPABASE_URL", "")
SUPABASE_KEY = _env("SUPABASE_KEY", "", aliases=("SUPABASE_SERVICE_KEY",))
CONTEXT_CACHE_TTL_SECONDS = max(
    30,
    int(_env("MINICPM_SUPABASE_CACHE_SECONDS", _env("MINICPM_TALLY_CACHE_SECONDS", "300")) or "300"),
)
# The audit trail is a learning loop: a correction a user just pushed must be
# visible on the very next scan, so it refreshes far more often than the heavy,
# slow-changing stock/ledger catalog (which keeps the longer TTL above).
AUDIT_TRAIL_CACHE_TTL_SECONDS = max(5, int(_env("AUDIT_TRAIL_CACHE_SECONDS", "30") or "30"))
SUPABASE_PAGE_SIZE = max(100, int(_env("MINICPM_SUPABASE_PAGE_SIZE", "1000") or "1000"))
SUPABASE_MAX_PAGES = max(1, int(_env("MINICPM_SUPABASE_MAX_PAGES", "20") or "20"))
SUPABASE_TIMEOUT_SECONDS = max(5, int(_env("MINICPM_SUPABASE_TIMEOUT_SECONDS", "20") or "20"))
_CONTEXT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_PURCHASE_MATCHING_EXACT_CACHE: tuple[float, dict[str, str]] | None = None
_AUDIT_TRAIL_CACHE: tuple[float, dict[str, str]] | None = None


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
            timeout=SUPABASE_TIMEOUT_SECONDS,
        )
    return response


def supabase_post_json(endpoint: str, payload: dict[str, Any]) -> requests.Response:
    with requests.Session() as session:
        session.trust_env = False
        response = session.post(
            endpoint,
            headers={
                **supabase_headers(),
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=SUPABASE_TIMEOUT_SECONDS,
        )
    return response


def supabase_endpoint(table_name: str) -> str:
    return f"{SUPABASE_URL.rstrip('/')}/rest/v1/{table_name}"


def ensure_supabase_configured() -> None:
    if SUPABASE_URL and SUPABASE_KEY:
        return
    raise ValueError(
        "Supabase master lookup is not configured. Set SUPABASE_URL and SUPABASE_SERVICE_KEY in parsing/.env."
    )


def fetch_supabase_company_record(company_name: str) -> dict[str, Any]:
    ensure_supabase_configured()
    try:
        response = supabase_get(
            supabase_endpoint("companies"),
            {
                "select": "id,name,last_synced_at",
                "name": f"eq.{company_name}",
                "limit": "2",
            },
        )
        response.raise_for_status()
        companies = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(
            "Could not reach Supabase while loading company master data for purchase matching."
        ) from exc

    if not companies:
        raise ValueError(f"Company '{company_name}' was not found in Supabase.")
    if len(companies) > 1:
        raise ValueError(
            f"Multiple Supabase companies share the name '{company_name}'. Use a unique company name."
        )

    company = companies[0]
    if not company.get("last_synced_at"):
        raise ValueError(
            f"Company '{company_name}' has not completed a successful sync in Supabase yet."
        )
    return company


def fetch_supabase_company_rows(
    table_name: str,
    company_id: str,
    *,
    select: str,
    order: str = "name.asc",
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    endpoint = supabase_endpoint(table_name)
    for page_index in range(SUPABASE_MAX_PAGES):
        params = {
            "select": select,
            "company_id": f"eq.{company_id}",
            "order": order,
            "limit": str(SUPABASE_PAGE_SIZE),
            "offset": str(page_index * SUPABASE_PAGE_SIZE),
        }
        try:
            response = supabase_get(endpoint, params)
            response.raise_for_status()
            batch = response.json()
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Could not reach Supabase while loading {table_name} master data for purchase matching."
            ) from exc

        rows.extend(batch)
        if len(batch) < SUPABASE_PAGE_SIZE:
            break
    return rows


def fetch_supabase_table_rows(table_name: str, *, select: str = "*") -> list[dict[str, Any]]:
    ensure_supabase_configured()

    rows: list[dict[str, Any]] = []
    endpoint = supabase_endpoint(table_name)
    for page_index in range(SUPABASE_MAX_PAGES):
        params = {
            "select": select,
            "order": "id.asc",
            "limit": str(SUPABASE_PAGE_SIZE),
            "offset": str(page_index * SUPABASE_PAGE_SIZE),
        }
        try:
            response = supabase_get(endpoint, params)
            response.raise_for_status()
            batch = response.json()
        except requests.RequestException as exc:
            raise RuntimeError(f"Could not reach Supabase while loading {table_name} lookup data.") from exc

        rows.extend(batch)
        if len(batch) < SUPABASE_PAGE_SIZE:
            break
    return rows


def fetch_purchase_matching_rows() -> list[dict[str, Any]]:
    try:
        return fetch_supabase_table_rows("purchase_matching_api", select="*")
    except RuntimeError:
        pass

    try:
        return fetch_supabase_table_rows("Purchase_Matching", select="*")
    except RuntimeError:
        pass

    ensure_supabase_configured()
    sql = 'select "Invoice Item Description", "Tally Item Description" from public."Purchase_Matching";'
    candidate_endpoints = [
        f"{SUPABASE_URL.rstrip('/')}/pg/v1/query",
        f"{SUPABASE_URL.rstrip('/')}/pg-meta/default/query",
    ]
    last_error: Exception | None = None
    for endpoint in candidate_endpoints:
        try:
            response = supabase_post_json(endpoint, {"query": sql})
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, list):
                return [row for row in payload if isinstance(row, dict)]
            if isinstance(payload, dict):
                rows = payload.get("rows") or payload.get("result") or payload.get("data") or []
                if isinstance(rows, list):
                    return [row for row in rows if isinstance(row, dict)]
        except requests.RequestException as exc:
            last_error = exc
            continue

    raise RuntimeError("Could not reach Supabase while loading Purchase_Matching lookup data.") from last_error


def resolve_purchase_matching_exact_map() -> dict[str, str]:
    global _PURCHASE_MATCHING_EXACT_CACHE

    now = time.time()
    if _PURCHASE_MATCHING_EXACT_CACHE and now - _PURCHASE_MATCHING_EXACT_CACHE[0] < CONTEXT_CACHE_TTL_SECONDS:
        return _PURCHASE_MATCHING_EXACT_CACHE[1]

    try:
        rows = fetch_purchase_matching_rows()
    except (RuntimeError, ValueError):
        _PURCHASE_MATCHING_EXACT_CACHE = (now, {})
        return {}

    description_to_tally_names: dict[str, set[str]] = {}
    for row in rows:
        invoice_description = normalize_space(row.get("Invoice Item Description", ""))
        tally_description = normalize_space(row.get("Tally Item Description", ""))
        if not invoice_description or not tally_description:
            continue
        description_to_tally_names.setdefault(invoice_description, set()).add(tally_description)

    exact_map = {
        invoice_description: next(iter(tally_names))
        for invoice_description, tally_names in description_to_tally_names.items()
        if len(tally_names) == 1
    }
    _PURCHASE_MATCHING_EXACT_CACHE = (now, exact_map)
    return exact_map


def fetch_audit_trail_rows() -> list[dict[str, Any]]:
    return fetch_supabase_table_rows("Audit_Trail_Purchase", select="raw_ocr,actual_item")


def resolve_audit_trail_map() -> dict[str, str]:
    """raw OCR text -> the stock item a human actually committed to Tally.

    Rows arrive ordered by id ascending, so a later correction of the same OCR text
    simply overwrites the earlier one: newest row wins, while every row is kept in
    the table as the audit history. An unreachable or absent table yields an empty
    map, which makes the audit lookup a no-op and leaves existing matching untouched.
    """
    global _AUDIT_TRAIL_CACHE

    now = time.time()
    if _AUDIT_TRAIL_CACHE and now - _AUDIT_TRAIL_CACHE[0] < AUDIT_TRAIL_CACHE_TTL_SECONDS:
        return _AUDIT_TRAIL_CACHE[1]

    try:
        rows = fetch_audit_trail_rows()
    except (RuntimeError, ValueError):
        _AUDIT_TRAIL_CACHE = (now, {})
        return {}

    audit_map: dict[str, str] = {}
    for row in rows:
        lookup_key = audit_trail_key(row.get("raw_ocr", ""))
        actual_item = normalize_space(row.get("actual_item", ""))
        if not lookup_key or not actual_item:
            continue
        audit_map[lookup_key] = actual_item

    _AUDIT_TRAIL_CACHE = (now, audit_map)
    return audit_map


def resolve_supabase_company_context(company_name: str) -> dict[str, Any]:
    cached = _CONTEXT_CACHE.get(company_name)
    now = time.time()
    if cached and now - cached[0] < CONTEXT_CACHE_TTL_SECONDS:
        context = cached[1]
        # Refresh only the audit map on a catalog-cache hit, on its own short TTL, so a
        # just-taught correction is picked up within seconds rather than minutes.
        context["audit_trail_map"] = resolve_audit_trail_map()
        return context

    company = fetch_supabase_company_record(company_name)
    ledgers = fetch_supabase_company_rows(
        "ledgers",
        company["id"],
        select="name,group_name,opening_balance,closing_balance,master_id,email,phone,mobile,pincode,gstin,state,country,credit_period,credit_limit,bank_account,ifsc_code,pan,mailing_name,guid",
    )
    stock_items = fetch_supabase_company_rows(
        "stock_items",
        company["id"],
        select="name,group_name,unit,closing_qty,closing_value,rate",
    )
    context = {
        "company_id": company["id"],
        "company_name": company.get("name", company_name),
        "source": "supabase",
        "ledgers": ledgers,
        "stock_items": stock_items,
        "purchase_matching_exact_map": resolve_purchase_matching_exact_map(),
        "audit_trail_map": resolve_audit_trail_map(),
    }
    _CONTEXT_CACHE[company_name] = (now, context)
    return context
