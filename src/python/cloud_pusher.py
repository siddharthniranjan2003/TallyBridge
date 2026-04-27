import os
import time

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
if SYNC_INGEST_MODE not in {"render", "direct"}:
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


def _build_ingest_target() -> tuple[str, str, dict[str, str]]:
    if SYNC_INGEST_MODE == "direct":
        headers = {
            "Content-Type": "application/json",
            "x-sync-key": SYNC_INGEST_KEY,
            "x-sync-contract-version": str(SYNC_CONTRACT_VERSION),
        }
        return "direct", SYNC_INGEST_URL, headers

    return (
        "render",
        _control_plane_url("/api/sync"),
        {
            "Content-Type": "application/json",
            "x-api-key": CONTROL_PLANE_API_KEY,
            "x-sync-contract-version": str(SYNC_CONTRACT_VERSION),
        },
    )


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


def push(payload: dict) -> bool:
    _set_last_push_error("")
    _set_last_push_stats({})

    transport, target_url, headers = _build_ingest_target()
    if not target_url:
        if transport == "direct":
            message = "Direct ingest mode is enabled but SYNC_INGEST_URL is not configured."
        else:
            message = "Render ingest mode is enabled but the control plane URL is not configured."
        print(f"[Ingest] {message}")
        _set_last_push_error(message)
        _set_last_push_stats({
            "transport": transport,
            "target_url": target_url,
            "status": "not_configured",
            "timeout_ms": get_backend_timeout_seconds() * 1000,
        })
        return False

    if transport == "direct" and not SYNC_INGEST_KEY:
        message = "Direct ingest mode is enabled but SYNC_INGEST_KEY is not configured."
        print(f"[Ingest] {message}")
        _set_last_push_error(message)
        _set_last_push_stats({
            "transport": transport,
            "target_url": target_url,
            "status": "not_configured",
            "timeout_ms": get_backend_timeout_seconds() * 1000,
        })
        return False

    request_payload = dict(payload)
    request_payload["sync_contract_version"] = SYNC_CONTRACT_VERSION

    previous_remote_state, previous_remote_status = fetch_remote_alter_ids()
    previous_last_synced_at = (
        _string_marker(previous_remote_state.get("last_synced_at"))
        if previous_remote_status == "ok" and previous_remote_state
        else None
    )

    print(f"[Ingest] Uploading via {transport} to {target_url}")
    request_started_at = time.perf_counter()
    try:
        response = requests.post(
            target_url,
            json=request_payload,
            headers=headers,
            timeout=get_backend_timeout_seconds(),
        )
        request_duration_ms = round((time.perf_counter() - request_started_at) * 1000, 2)

        if response.ok:
            data = response.json() if response.content else {}
            if isinstance(data, dict) and data.get("success") is False:
                message = str(data.get("error") or data)
                _set_last_push_error(message)
                _set_last_push_stats({
                    "transport": transport,
                    "target_url": target_url,
                    "status": "application_error",
                    "http_status": response.status_code,
                    "request_duration_ms": request_duration_ms,
                })
                print(f"[Ingest] Upload failed: {message}")
                return False

            records = data.get("records", {}) if isinstance(data, dict) else {}
            _set_last_push_stats({
                "transport": transport,
                "target_url": target_url,
                "status": "ok",
                "http_status": response.status_code,
                "request_duration_ms": request_duration_ms,
                "dry_run": bool(isinstance(data, dict) and data.get("dry_run")),
            })
            print(f"[Ingest] Upload successful in {request_duration_ms} ms")
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
                    print(f"[Ingest] {label}: {records.get(key, 0)}")
            return True

        error_message = _extract_backend_error(response)
        _set_last_push_error(error_message)
        _set_last_push_stats({
            "transport": transport,
            "target_url": target_url,
            "status": "http_error",
            "http_status": response.status_code,
            "request_duration_ms": request_duration_ms,
        })
        print(f"[Ingest] Upload failed: HTTP {response.status_code}")
        print(f"[Ingest] Response: {error_message}")
        return False

    except requests.exceptions.ConnectionError:
        message = f"Cannot reach ingest endpoint at {target_url}"
        print(f"[Ingest] {message}")
        _set_last_push_error(message)
        _set_last_push_stats({
            "transport": transport,
            "target_url": target_url,
            "status": "connection_error",
            "timeout_ms": get_backend_timeout_seconds() * 1000,
        })
        return False
    except requests.exceptions.ReadTimeout:
        expected_alter_ids = payload.get("alter_ids") if isinstance(payload.get("alter_ids"), dict) else None
        verified_after_timeout = verify_remote_sync_completion(expected_alter_ids, previous_last_synced_at)
        request_duration_ms = round((time.perf_counter() - request_started_at) * 1000, 2)
        _set_last_push_stats({
            "transport": transport,
            "target_url": target_url,
            "status": "timeout_verified" if verified_after_timeout else "timeout",
            "request_duration_ms": request_duration_ms,
            "verified_after_timeout": verified_after_timeout,
        })
        if verified_after_timeout:
            return True

        timeout_message = (
            f"Ingest endpoint at {target_url} did not respond within "
            f"{get_backend_timeout_seconds()}s."
        )
        _set_last_push_error(timeout_message)
        print(f"[Ingest] {timeout_message}")
        return False
    except Exception as error:
        request_duration_ms = round((time.perf_counter() - request_started_at) * 1000, 2)
        print(f"[Ingest] Upload error: {error}")
        _set_last_push_error(str(error))
        _set_last_push_stats({
            "transport": transport,
            "target_url": target_url,
            "status": "exception",
            "request_duration_ms": request_duration_ms,
        })
        return False
