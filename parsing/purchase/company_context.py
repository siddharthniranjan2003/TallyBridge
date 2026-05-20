from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import requests

from lib.env import make_env_loader

_env = make_env_loader(Path(__file__).resolve().parents[1] / ".env")
SUPABASE_URL = _env("SUPABASE_URL", "")
SUPABASE_KEY = _env("SUPABASE_KEY", "", aliases=("SUPABASE_SERVICE_KEY",))
CONTEXT_CACHE_TTL_SECONDS = max(
    30,
    int(_env("MINICPM_SUPABASE_CACHE_SECONDS", _env("MINICPM_TALLY_CACHE_SECONDS", "300")) or "300"),
)
SUPABASE_PAGE_SIZE = max(100, int(_env("MINICPM_SUPABASE_PAGE_SIZE", "1000") or "1000"))
SUPABASE_MAX_PAGES = max(1, int(_env("MINICPM_SUPABASE_MAX_PAGES", "10") or "10"))
SUPABASE_TIMEOUT_SECONDS = max(5, int(_env("MINICPM_SUPABASE_TIMEOUT_SECONDS", "20") or "20"))
_CONTEXT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


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


def resolve_supabase_company_context(company_name: str) -> dict[str, Any]:
    cached = _CONTEXT_CACHE.get(company_name)
    now = time.time()
    if cached and now - cached[0] < CONTEXT_CACHE_TTL_SECONDS:
        return cached[1]

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
    }
    _CONTEXT_CACHE[company_name] = (now, context)
    return context
