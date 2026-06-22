import os
import time
from datetime import datetime, timezone

import requests

LEGACY_BACKEND_URL = os.environ.get("BACKEND_URL", "").strip()
LEGACY_API_KEY = os.environ.get("API_KEY", "").strip()
CONTROL_PLANE_URL = (
    os.environ.get("CONTROL_PLANE_URL", "").strip()
    or LEGACY_BACKEND_URL
).rstrip("/")
CONTROL_PLANE_API_KEY = (
    os.environ.get("CONTROL_PLANE_API_KEY", "").strip()
    or LEGACY_API_KEY
)
SYNC_INGEST_MODE = (os.environ.get("SYNC_INGEST_MODE", "render") or "render").strip().lower()
if SYNC_INGEST_MODE not in {"render", "hybrid", "direct"}:
    SYNC_INGEST_MODE = "render"
SYNC_INGEST_URL = (os.environ.get("SYNC_INGEST_URL", "") or "").strip()
SYNC_INGEST_KEY = (os.environ.get("SYNC_INGEST_KEY", "") or "").strip()
try:
    SYNC_CONTRACT_VERSION = max(1, int(os.environ.get("SYNC_CONTRACT_VERSION", "1") or "1"))
except ValueError:
    SYNC_CONTRACT_VERSION = 1
TALLY_COMPANY = os.environ.get("TALLY_COMPANY", "").strip()
TALLY_COMPANY_GUID = os.environ.get("TALLY_COMPANY_GUID", "").strip()
LAST_PUSH_ERROR = ""
LAST_PUSH_STATS: dict[str, object] = {}
# Cached per process: the resolved Supabase company id for the direct (PostgREST)
# transport, so we resolve/upsert the company once per sync run, not per request.
_DIRECT_COMPANY_ID: str | None = None
DIRECT_INGEST_SECTION_KEYS = (
    "groups",
    "ledgers",
    "stock_items",
    "outstanding",
    "profit_loss",
    "balance_sheet",
    "trial_balance",
)
DIRECT_VOUCHER_CHUNK_SIZE = 2000


def get_backend_timeout_seconds() -> int:
    try:
        return max(120, int(os.environ.get("BACKEND_TIMEOUT_SECONDS", "900")))
    except ValueError:
        return 900


def get_backend_post_verify_seconds() -> int:
    try:
        return max(30, int(os.environ.get("BACKEND_POST_VERIFY_SECONDS", "180")))
    except ValueError:
        return 180


def get_backend_post_verify_poll_seconds() -> int:
    try:
        return max(5, int(os.environ.get("BACKEND_POST_VERIFY_POLL_SECONDS", "10")))
    except ValueError:
        return 10


def _string_marker(value) -> str:
    return str(value).strip() if value is not None else ""


def _set_last_push_error(message: str) -> None:
    global LAST_PUSH_ERROR
    LAST_PUSH_ERROR = (message or "").strip()


def _set_last_push_stats(stats: dict[str, object]) -> None:
    global LAST_PUSH_STATS
    LAST_PUSH_STATS = dict(stats)


def get_last_push_error() -> str:
    return LAST_PUSH_ERROR


def get_last_push_stats() -> dict[str, object]:
    return dict(LAST_PUSH_STATS)


def _extract_backend_error(response: requests.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            error_value = data.get("error")
            if error_value:
                return str(error_value)
            if data.get("success") is False:
                return str(data)
    except Exception:
        pass

    text = (response.text or "").strip()
    if text:
        return text[:500]

    return f"HTTP {response.status_code}"


def _alter_ids_match(expected: dict | None, remote: dict | None) -> bool:
    if not expected or not remote:
        return False

    return all(
        _string_marker(remote.get(key)) == _string_marker(expected.get(key))
        for key in ("alter_id", "alt_vch_id", "alt_mst_id")
    )


def _company_identity_params() -> tuple[dict[str, str], str]:
    params: dict[str, str] = {}
    if TALLY_COMPANY_GUID:
        params["company_guid"] = TALLY_COMPANY_GUID
        return params, "ok"
    if TALLY_COMPANY:
        params["company_name"] = TALLY_COMPANY
        return params, "ok"
    return params, "company_identity_missing"


def _control_plane_url(path: str) -> str:
    if not CONTROL_PLANE_URL:
        return ""
    return f"{CONTROL_PLANE_URL}{path}"


def _build_control_headers() -> dict[str, str]:
    return {"x-api-key": CONTROL_PLANE_API_KEY}


def _build_ingest_target(transport: str) -> tuple[str, dict[str, str], str | None]:
    if transport == "direct":
        if not SYNC_INGEST_URL:
            return "", {}, "Direct ingest mode is enabled but SYNC_INGEST_URL is not configured."
        if not SYNC_INGEST_KEY:
            return "", {}, "Direct ingest mode is enabled but SYNC_INGEST_KEY is not configured."
        return (
            SYNC_INGEST_URL,
            {
                "Content-Type": "application/json",
                "x-sync-key": SYNC_INGEST_KEY,
                "x-sync-contract-version": str(SYNC_CONTRACT_VERSION),
            },
            None,
        )

    if not CONTROL_PLANE_URL:
        return "", {}, "Render ingest mode is enabled but the control plane URL is not configured."
    if not CONTROL_PLANE_API_KEY:
        return "", {}, "Render ingest mode is enabled but the control plane API key is not configured."
    return (
        _control_plane_url("/api/sync"),
        {
            "Content-Type": "application/json",
            "x-api-key": CONTROL_PLANE_API_KEY,
            "x-sync-contract-version": str(SYNC_CONTRACT_VERSION),
        },
        None,
    )


# ── Direct ingest via PostgREST ──────────────────────────────────
# The Supabase Edge Function endpoint (functions/v1/ingest-sync) intermittently
# stalls on POST regardless of body size (~37% even at 160 KB), causing HTTP 503.
# PostgREST (rest/v1) is reliable, so the direct transport calls the ingest RPCs
# there using the configured publishable key. This replicates what the edge
# function did (resolve the company, then call the masters / voucher RPCs).

_COMPANY_INFO_FIELDS = (
    "books_from", "books_to", "books_from_raw", "books_to_raw", "gstin",
    "address", "guid", "state", "country", "pincode", "email", "phone",
    "gst_type", "pan",
)
_COMPANY_ALTER_FIELDS = ("alter_id", "alt_vch_id", "alt_mst_id", "last_voucher_date")


def _postgrest_base() -> str:
    if SYNC_INGEST_URL and "/functions/" in SYNC_INGEST_URL:
        return SYNC_INGEST_URL.split("/functions/", 1)[0] + "/rest/v1"
    return ""


def _postgrest_headers() -> dict[str, str]:
    return {
        "apikey": SYNC_INGEST_KEY,
        "Authorization": f"Bearer {SYNC_INGEST_KEY}",
        "Content-Type": "application/json",
    }


def _build_company_update(company_guid: str | None, company_info, alter_ids) -> dict:
    """Mirror the edge function's company upsert payload (metadata only)."""
    update: dict[str, object] = {}
    if isinstance(company_info, dict):
        for key in _COMPANY_INFO_FIELDS:
            value = company_info.get(key)
            if value in (None, ""):
                continue
            if key == "guid" and company_guid:
                continue
            update[key] = value
        master_id = company_info.get("master_id")
        if isinstance(master_id, (int, float)) and not isinstance(master_id, bool):
            update["master_id"] = int(master_id)
    if isinstance(alter_ids, dict):
        for key in _COMPANY_ALTER_FIELDS:
            value = alter_ids.get(key)
            if value not in (None, ""):
                update[key] = value
    return update


def _resolve_direct_company_id(rest: str, headers: dict, payload: dict) -> str:
    """Find (or insert) the company by GUID/name and apply any metadata in this
    payload — same resolution order as the edge function's upsertCompanyRecord."""
    global _DIRECT_COMPANY_ID
    name = (payload.get("company_name") or "").strip()
    guid = (payload.get("company_guid") or "").strip() or None
    update = _build_company_update(guid, payload.get("company_info"), payload.get("alter_ids"))
    timeout = get_backend_timeout_seconds()

    if _DIRECT_COMPANY_ID is None:
        if guid:
            rows = requests.get(f"{rest}/companies", headers=headers,
                                params={"guid": f"eq.{guid}", "select": "id"}, timeout=timeout)
            rows.raise_for_status()
            data = rows.json()
            if data:
                _DIRECT_COMPANY_ID = data[0]["id"]
        if _DIRECT_COMPANY_ID is None and name:
            rows = requests.get(f"{rest}/companies", headers=headers,
                                params={"name": f"eq.{name}", "select": "id,guid", "limit": "2"},
                                timeout=timeout)
            rows.raise_for_status()
            data = rows.json()
            if len(data) > 1:
                raise RuntimeError("Multiple companies share this name; re-add with a Tally GUID.")
            if data and (not data[0].get("guid") or data[0].get("guid") == guid):
                _DIRECT_COMPANY_ID = data[0]["id"]
        if _DIRECT_COMPANY_ID is None:
            insert = {"name": name}
            if guid:
                insert["guid"] = guid
            insert.update(update)
            created = requests.post(f"{rest}/companies", headers={**headers, "Prefer": "return=representation"},
                                    params={"select": "id"}, json=[insert], timeout=timeout)
            created.raise_for_status()
            _DIRECT_COMPANY_ID = created.json()[0]["id"]
            return _DIRECT_COMPANY_ID  # insert already carried the metadata

    if update:
        patched = requests.patch(f"{rest}/companies", headers=headers,
                                 params={"id": f"eq.{_DIRECT_COMPANY_ID}"}, json=update, timeout=timeout)
        patched.raise_for_status()
    return _DIRECT_COMPANY_ID


def _direct_error_result(step_label, target_url, status, error, *, http_status=None, duration_ms=None):
    stats: dict[str, object] = {
        "step": step_label, "transport": "direct", "target_url": target_url, "status": status,
    }
    if http_status is not None:
        stats["http_status"] = http_status
    if duration_ms is not None:
        stats["request_duration_ms"] = duration_ms
    return {"ok": False, "error": error, "records": {}, "stats": stats}


def _post_direct_via_postgrest(payload: dict, step_label: str) -> dict[str, object]:
    rest = _postgrest_base()
    if not rest:
        msg = "Direct ingest needs SYNC_INGEST_URL to derive the Supabase REST endpoint."
        print(f"[Ingest][{step_label}] {msg}")
        return _direct_error_result(step_label, rest, "not_configured", msg)
    if not SYNC_INGEST_KEY:
        msg = "Direct ingest mode is enabled but SYNC_INGEST_KEY is not configured."
        print(f"[Ingest][{step_label}] {msg}")
        return _direct_error_result(step_label, rest, "not_configured", msg)

    headers = _postgrest_headers()
    synced_at = payload.get("sync_run_synced_at") or datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    print(f"[Ingest][{step_label}] Uploading via PostgREST to {rest}")
    try:
        company_id = _resolve_direct_company_id(rest, headers, payload)

        if payload.get("vouchers") is not None:
            meta = payload.get("sync_meta") if isinstance(payload.get("sync_meta"), dict) else {}
            rpc = "tb_ingest_vouchers"
            body = {
                "p_company_id": company_id,
                "p_synced_at": synced_at,
                "p_vouchers": payload.get("vouchers") or [],
                "p_sync_meta": meta,
                "p_alter_ids": payload.get("alter_ids") or {},
                "p_is_final_chunk": bool(meta.get("is_final_chunk")),
                "p_record_counts": meta.get("record_counts") or {},
            }
        else:
            rpc = "tb_ingest_phase3_hybrid"
            body = {
                "p_company_id": company_id,
                "p_synced_at": synced_at,
                "p_groups": payload.get("groups"),
                "p_ledgers": payload.get("ledgers"),
                "p_stock_items": payload.get("stock_items"),
                "p_outstanding": payload.get("outstanding"),
                "p_profit_loss": payload.get("profit_loss"),
                "p_balance_sheet": payload.get("balance_sheet"),
                "p_trial_balance": payload.get("trial_balance"),
            }

        response = requests.post(f"{rest}/rpc/{rpc}", headers=headers, json=body,
                                 timeout=get_backend_timeout_seconds())
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        if not response.ok:
            message = _extract_backend_error(response)
            print(f"[Ingest][{step_label}] Upload failed: HTTP {response.status_code}")
            print(f"[Ingest][{step_label}] Response: {message}")
            return _direct_error_result(step_label, rest, "http_error", message,
                                        http_status=response.status_code, duration_ms=duration_ms)

        records = response.json() if response.content else {}
        if not isinstance(records, dict):
            records = {}
        print(f"[Ingest][{step_label}] Upload successful in {duration_ms} ms")
        return {
            "ok": True,
            "error": "",
            "records": records,
            "stats": {
                "step": step_label,
                "transport": "direct",
                "target_url": rest,
                "status": "ok",
                "http_status": response.status_code,
                "request_duration_ms": duration_ms,
            },
        }
    except requests.exceptions.RequestException as error:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        message = f"PostgREST ingest request failed: {error}"
        print(f"[Ingest][{step_label}] {message}")
        return _direct_error_result(step_label, rest, "exception", message, duration_ms=duration_ms)
    except Exception as error:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        print(f"[Ingest][{step_label}] Upload error: {error}")
        return _direct_error_result(step_label, rest, "exception", str(error), duration_ms=duration_ms)


def _clone_sync_meta(payload: dict, ingest_role: str) -> dict:
    sync_meta = payload.get("sync_meta")
    next_meta = dict(sync_meta) if isinstance(sync_meta, dict) else {}
    next_meta["ingest_role"] = ingest_role
    return next_meta


def _count_rows(value) -> int:
    return len(value) if isinstance(value, list) else 0


def _nested_row_count(rows, field_name: str) -> int:
    total = 0
    if not isinstance(rows, list):
        return 0

    for row in rows:
        if not isinstance(row, dict):
            continue
        nested_rows = row.get(field_name)
        if isinstance(nested_rows, list):
            total += len(nested_rows)
    return total


def _is_purchase_voucher_type(value) -> bool:
    if not isinstance(value, str):
        return False

    normalized = value.strip().lower()
    if normalized in {"purchase", "local purchase", "import purchase"}:
        return True
    return "purchase" in normalized and "order" not in normalized


def _build_record_counts(payload: dict) -> dict[str, int]:
    vouchers = payload.get("vouchers") if isinstance(payload.get("vouchers"), list) else []
    return {
        "groups": _count_rows(payload.get("groups")),
        "ledgers": _count_rows(payload.get("ledgers")),
        "vouchers": len(vouchers),
        "stock": _count_rows(payload.get("stock_items")),
        "outstanding": _count_rows(payload.get("outstanding")),
        "profit_loss": _count_rows(payload.get("profit_loss")),
        "balance_sheet": _count_rows(payload.get("balance_sheet")),
        "trial_balance": _count_rows(payload.get("trial_balance")),
        "purchases": sum(
            1
            for voucher in vouchers
            if isinstance(voucher, dict) and _is_purchase_voucher_type(voucher.get("voucher_type"))
        ),
        "voucher_items": _nested_row_count(vouchers, "items"),
        "voucher_ledger_entries": _nested_row_count(vouchers, "ledger_entries"),
    }


def _chunk_rows(rows: list[dict], chunk_size: int) -> list[list[dict]]:
    if not rows:
        return [[]]
    return [
        rows[index:index + chunk_size]
        for index in range(0, len(rows), chunk_size)
    ]


def _build_direct_payload(
    payload: dict,
    sync_run_synced_at: str,
) -> dict | None:
    if not any(payload.get(key) is not None for key in DIRECT_INGEST_SECTION_KEYS):
        return None

    return {
        "company_name": payload.get("company_name"),
        "company_guid": payload.get("company_guid"),
        "company_info": payload.get("company_info"),
        "groups": payload.get("groups"),
        "ledgers": payload.get("ledgers"),
        "stock_items": payload.get("stock_items"),
        "outstanding": payload.get("outstanding"),
        "profit_loss": payload.get("profit_loss"),
        "balance_sheet": payload.get("balance_sheet"),
        "trial_balance": payload.get("trial_balance"),
        "sync_meta": _clone_sync_meta(payload, "direct_masters_snapshots"),
        "sync_contract_version": SYNC_CONTRACT_VERSION,
        "sync_run_synced_at": sync_run_synced_at,
    }


def _build_render_payload(payload: dict) -> dict:
    return {
        "company_name": payload.get("company_name"),
        "company_guid": payload.get("company_guid"),
        "company_info": payload.get("company_info"),
        "alter_ids": payload.get("alter_ids"),
        "vouchers": payload.get("vouchers"),
        "sync_meta": _clone_sync_meta(payload, "render_voucher_control_plane"),
        "sync_contract_version": SYNC_CONTRACT_VERSION,
    }


def _build_direct_voucher_payloads(
    payload: dict,
    sync_run_synced_at: str,
    record_counts: dict[str, int],
) -> list[dict]:
    vouchers = payload.get("vouchers") if isinstance(payload.get("vouchers"), list) else []
    voucher_chunks = _chunk_rows(vouchers, DIRECT_VOUCHER_CHUNK_SIZE)
    chunk_count = len(voucher_chunks)
    direct_payloads: list[dict] = []

    for chunk_index, voucher_chunk in enumerate(voucher_chunks, start=1):
        sync_meta = _clone_sync_meta(payload, "direct_vouchers")
        sync_meta.update({
            "chunk_index": chunk_index,
            "chunk_count": chunk_count,
            "voucher_chunk_index": chunk_index,
            "voucher_chunk_count": chunk_count,
            "voucher_chunk_size": len(voucher_chunk),
            "is_final_chunk": chunk_index == chunk_count,
            "record_counts": record_counts,
        })

        direct_payloads.append({
            "company_name": payload.get("company_name"),
            "company_guid": payload.get("company_guid"),
            "company_info": payload.get("company_info") if chunk_index == 1 else None,
            "alter_ids": payload.get("alter_ids") if chunk_index == chunk_count else None,
            "vouchers": voucher_chunk,
            "sync_meta": sync_meta,
            "sync_contract_version": SYNC_CONTRACT_VERSION,
            "sync_run_synced_at": sync_run_synced_at,
        })

    return direct_payloads


def fetch_remote_alter_ids() -> tuple[dict | None, str]:
    if not CONTROL_PLANE_URL:
        return None, "backend_unconfigured"

    params, company_status = _company_identity_params()
    if company_status != "ok":
        return None, "company_identity_missing"

    try:
        response = requests.get(
            _control_plane_url("/api/sync/alter-ids"),
            params=params,
            headers=_build_control_headers(),
            timeout=min(get_backend_timeout_seconds(), 60),
        )

        if response.status_code == 404:
            return None, "company_not_found"

        if response.status_code == 409:
            return None, "company_not_ready"

        if not response.ok:
            print(f"[Control] Alter-id lookup failed: HTTP {response.status_code}")
            print(f"[Control] Response: {response.text[:300]}")
            return None, "lookup_failed"

        data = response.json() if response.content else {}
        return data if isinstance(data, dict) else {}, "ok"
    except requests.exceptions.ConnectionError:
        print(f"[Control] Cannot reach alter-id endpoint at {CONTROL_PLANE_URL}")
        return None, "backend_unreachable"
    except Exception as error:
        print(f"[Control] Alter-id lookup error: {error}")
        return None, "lookup_failed"


def fetch_pending_push_vouchers(limit: int = 10) -> tuple[list[dict], str]:
    if not CONTROL_PLANE_URL:
        return [], "backend_unconfigured"

    params, company_status = _company_identity_params()
    if company_status != "ok":
        return [], company_status

    params["limit"] = str(max(1, min(limit, 50)))

    try:
        response = requests.get(
            _control_plane_url("/api/sync/push-queue"),
            params=params,
            headers=_build_control_headers(),
            timeout=min(get_backend_timeout_seconds(), 60),
        )

        if response.status_code == 404:
            return [], "company_not_found"

        if response.status_code == 409:
            return [], "company_not_ready"

        if not response.ok:
            print(f"[Control][Push] Queue fetch failed: HTTP {response.status_code}")
            print(f"[Control][Push] Response: {response.text[:300]}")
            return [], "lookup_failed"

        data = response.json() if response.content else {}
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            return [], "malformed_response"

        normalized_jobs = [
            job for job in jobs
            if isinstance(job, dict)
            and job.get("id")
            and isinstance(job.get("voucher_payload"), dict)
        ]
        return normalized_jobs, "ok"
    except requests.exceptions.ConnectionError:
        print(f"[Control][Push] Cannot reach push queue endpoint at {CONTROL_PLANE_URL}")
        return [], "backend_unreachable"
    except Exception as error:
        print(f"[Control][Push] Queue fetch error: {error}")
        return [], "lookup_failed"


def mark_push_results(job_results: list[dict]) -> bool:
    if not CONTROL_PLANE_URL:
        print("[Control][Push] No control plane URL configured; cannot store push results")
        return False

    if not job_results:
        return True

    try:
        response = requests.post(
            _control_plane_url("/api/sync/push-results"),
            json={"results": job_results},
            headers={
                "Content-Type": "application/json",
                "x-api-key": CONTROL_PLANE_API_KEY,
            },
            timeout=min(get_backend_timeout_seconds(), 60),
        )

        if response.ok:
            return True

        print(f"[Control][Push] Result update failed: HTTP {response.status_code}")
        print(f"[Control][Push] Response: {_extract_backend_error(response)}")
        return False
    except requests.exceptions.ConnectionError:
        print(f"[Control][Push] Cannot reach {CONTROL_PLANE_URL} to store push results")
        return False
    except Exception as error:
        print(f"[Control][Push] Result update error: {error}")
        return False


def verify_remote_sync_completion(
    expected_alter_ids: dict | None,
    previous_last_synced_at: str | None = None,
) -> bool:
    deadline = time.time() + get_backend_post_verify_seconds()
    poll_seconds = get_backend_post_verify_poll_seconds()
    print("[Control] Upload timed out; checking whether the sync completed anyway...")

    while time.time() < deadline:
        remote_state, remote_status = fetch_remote_alter_ids()
        if remote_status == "ok" and remote_state:
            remote_last_synced_at = _string_marker(remote_state.get("last_synced_at"))
            has_newer_sync = (
                bool(remote_last_synced_at)
                and remote_last_synced_at != _string_marker(previous_last_synced_at)
            )
            if has_newer_sync and _alter_ids_match(expected_alter_ids, remote_state):
                print(
                    "[Control] Control plane confirmed the sync after the client timed out; "
                    "treating this upload as successful."
                )
                return True
        time.sleep(poll_seconds)

    print("[Control] Control plane did not confirm sync completion after the timeout window.")
    return False


def _post_sync_payload(
    transport: str,
    payload: dict,
    step_label: str,
    *,
    verify_after_timeout: bool = False,
) -> dict[str, object]:
    if transport == "direct":
        return _post_direct_via_postgrest(payload, step_label)

    target_url, headers, config_error = _build_ingest_target(transport)
    if config_error:
        print(f"[Ingest][{step_label}] {config_error}")
        return {
            "ok": False,
            "error": config_error,
            "records": {},
            "stats": {
                "step": step_label,
                "transport": transport,
                "target_url": target_url,
                "status": "not_configured",
                "timeout_ms": get_backend_timeout_seconds() * 1000,
            },
        }

    previous_last_synced_at = None
    expected_alter_ids = None
    if verify_after_timeout:
        previous_remote_state, previous_remote_status = fetch_remote_alter_ids()
        previous_last_synced_at = (
            _string_marker(previous_remote_state.get("last_synced_at"))
            if previous_remote_status == "ok" and previous_remote_state
            else None
        )
        expected_alter_ids = payload.get("alter_ids") if isinstance(payload.get("alter_ids"), dict) else None

    print(f"[Ingest][{step_label}] Uploading via {transport} to {target_url}")
    request_started_at = time.perf_counter()

    try:
        response = requests.post(
            target_url,
            json=payload,
            headers=headers,
            timeout=get_backend_timeout_seconds(),
        )
        request_duration_ms = round((time.perf_counter() - request_started_at) * 1000, 2)

        if response.ok:
            data = response.json() if response.content else {}
            if isinstance(data, dict) and data.get("success") is False:
                message = str(data.get("error") or data)
                print(f"[Ingest][{step_label}] Upload failed: {message}")
                return {
                    "ok": False,
                    "error": message,
                    "records": data.get("records", {}) if isinstance(data, dict) else {},
                    "stats": {
                        "step": step_label,
                        "transport": transport,
                        "target_url": target_url,
                        "status": "application_error",
                        "http_status": response.status_code,
                        "request_duration_ms": request_duration_ms,
                    },
                }

            records = data.get("records", {}) if isinstance(data, dict) else {}
            print(f"[Ingest][{step_label}] Upload successful in {request_duration_ms} ms")
            for key, label in (
                ("groups", "Groups"),
                ("ledgers", "Ledgers"),
                ("vouchers", "Vouchers"),
                ("stock", "Stock"),
                ("outstanding", "Outstanding"),
                ("profit_loss", "Profit & Loss"),
                ("balance_sheet", "Balance Sheet"),
                ("trial_balance", "Trial Balance"),
            ):
                if key in records:
                    print(f"[Ingest][{step_label}] {label}: {records.get(key, 0)}")

            return {
                "ok": True,
                "error": "",
                "records": records,
                "stats": {
                    "step": step_label,
                    "transport": transport,
                    "target_url": target_url,
                    "status": "ok",
                    "http_status": response.status_code,
                    "request_duration_ms": request_duration_ms,
                    "dry_run": bool(isinstance(data, dict) and data.get("dry_run")),
                },
            }

        error_message = _extract_backend_error(response)
        print(f"[Ingest][{step_label}] Upload failed: HTTP {response.status_code}")
        print(f"[Ingest][{step_label}] Response: {error_message}")
        return {
            "ok": False,
            "error": error_message,
            "records": {},
            "stats": {
                "step": step_label,
                "transport": transport,
                "target_url": target_url,
                "status": "http_error",
                "http_status": response.status_code,
                "request_duration_ms": request_duration_ms,
            },
        }

    except requests.exceptions.ConnectionError:
        message = f"Cannot reach ingest endpoint at {target_url}"
        print(f"[Ingest][{step_label}] {message}")
        return {
            "ok": False,
            "error": message,
            "records": {},
            "stats": {
                "step": step_label,
                "transport": transport,
                "target_url": target_url,
                "status": "connection_error",
                "timeout_ms": get_backend_timeout_seconds() * 1000,
            },
        }
    except requests.exceptions.ReadTimeout:
        request_duration_ms = round((time.perf_counter() - request_started_at) * 1000, 2)
        verified_after_timeout = False
        if verify_after_timeout:
            verified_after_timeout = verify_remote_sync_completion(
                expected_alter_ids,
                previous_last_synced_at,
            )
            if verified_after_timeout:
                return {
                    "ok": True,
                    "error": "",
                    "records": {},
                    "stats": {
                        "step": step_label,
                        "transport": transport,
                        "target_url": target_url,
                        "status": "timeout_verified",
                        "request_duration_ms": request_duration_ms,
                        "verified_after_timeout": True,
                    },
                }

        timeout_message = (
            f"Ingest endpoint at {target_url} did not respond within "
            f"{get_backend_timeout_seconds()}s."
        )
        print(f"[Ingest][{step_label}] {timeout_message}")
        return {
            "ok": False,
            "error": timeout_message,
            "records": {},
            "stats": {
                "step": step_label,
                "transport": transport,
                "target_url": target_url,
                "status": "timeout",
                "request_duration_ms": request_duration_ms,
                "verified_after_timeout": False,
            },
        }
    except Exception as error:
        request_duration_ms = round((time.perf_counter() - request_started_at) * 1000, 2)
        print(f"[Ingest][{step_label}] Upload error: {error}")
        return {
            "ok": False,
            "error": str(error),
            "records": {},
            "stats": {
                "step": step_label,
                "transport": transport,
                "target_url": target_url,
                "status": "exception",
                "request_duration_ms": request_duration_ms,
            },
        }


def push(payload: dict) -> bool:
    global _DIRECT_COMPANY_ID
    _DIRECT_COMPANY_ID = None
    _set_last_push_error("")
    _set_last_push_stats({})
    request_payload = dict(payload)
    request_payload["sync_contract_version"] = SYNC_CONTRACT_VERSION

    if SYNC_INGEST_MODE == "render":
        render_result = _post_sync_payload(
            "render",
            request_payload,
            "render-full",
            verify_after_timeout=True,
        )
        _set_last_push_stats(render_result["stats"])
        if render_result["ok"]:
            return True
        _set_last_push_error(str(render_result["error"] or "Cloud push failed"))
        return False

    sync_run_synced_at = datetime.now(timezone.utc).isoformat()
    direct_payload = _build_direct_payload(request_payload, sync_run_synced_at)
    record_counts = _build_record_counts(request_payload)
    voucher_payloads = _build_direct_voucher_payloads(
        request_payload,
        sync_run_synced_at,
        record_counts,
    )
    aggregate_stats: dict[str, object] = {
        "mode": SYNC_INGEST_MODE,
        "status": "in_progress",
        "steps": {},
    }

    if direct_payload is not None:
        direct_result = _post_sync_payload(
            "direct",
            direct_payload,
            "direct-masters-snapshots",
        )
        aggregate_stats["steps"] = {
            **(aggregate_stats.get("steps") or {}),
            "direct": direct_result["stats"],
        }
        if not direct_result["ok"]:
            aggregate_stats["status"] = "direct_failed"
            _set_last_push_stats(aggregate_stats)
            _set_last_push_error(str(direct_result["error"] or "Direct ingest failed"))
            return False
    else:
        aggregate_stats["steps"] = {
            **(aggregate_stats.get("steps") or {}),
            "direct": {
                "step": "direct-masters-snapshots",
                "transport": "direct",
                "status": "skipped",
                "reason": "no_direct_sections",
            },
        }

    voucher_chunk_stats: list[dict[str, object]] = []
    for chunk_index, voucher_payload in enumerate(voucher_payloads, start=1):
        voucher_result = _post_sync_payload(
            "direct",
            voucher_payload,
            f"direct-voucher-chunk-{chunk_index}",
        )
        voucher_chunk_stats.append(voucher_result["stats"])
        if not voucher_result["ok"]:
            aggregate_stats["steps"] = {
                **(aggregate_stats.get("steps") or {}),
                "voucher_chunks": voucher_chunk_stats,
            }
            aggregate_stats["status"] = "voucher_direct_failed"
            _set_last_push_stats(aggregate_stats)
            _set_last_push_error(str(voucher_result["error"] or "Direct voucher ingest failed"))
            return False

    aggregate_stats["steps"] = {
        **(aggregate_stats.get("steps") or {}),
        "voucher_chunks": voucher_chunk_stats,
    }

    total_request_ms = 0.0
    for step_stats in (aggregate_stats.get("steps") or {}).values():
        if isinstance(step_stats, dict):
            duration = step_stats.get("request_duration_ms")
            if isinstance(duration, (int, float)):
                total_request_ms += float(duration)
            continue

        if isinstance(step_stats, list):
            for nested_stats in step_stats:
                if not isinstance(nested_stats, dict):
                    continue
                duration = nested_stats.get("request_duration_ms")
                if isinstance(duration, (int, float)):
                    total_request_ms += float(duration)

    aggregate_stats["status"] = "ok"
    aggregate_stats["request_duration_ms"] = round(total_request_ms, 2)
    _set_last_push_stats(aggregate_stats)
    return True
