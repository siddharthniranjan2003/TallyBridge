import os
import time

import requests

BACKEND_URL = os.environ.get("BACKEND_URL", "")
API_KEY = os.environ.get("API_KEY", "")
TALLY_COMPANY = os.environ.get("TALLY_COMPANY", "").strip()
TALLY_COMPANY_GUID = os.environ.get("TALLY_COMPANY_GUID", "").strip()
LAST_PUSH_ERROR = ""


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


def get_last_push_error() -> str:
    return LAST_PUSH_ERROR


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


def fetch_remote_alter_ids() -> tuple[dict | None, str]:
    if not BACKEND_URL:
        return None, "backend_unconfigured"

    params: dict[str, str] = {}
    if TALLY_COMPANY_GUID:
        params["company_guid"] = TALLY_COMPANY_GUID
    elif TALLY_COMPANY:
        params["company_name"] = TALLY_COMPANY
    else:
        return None, "company_identity_missing"

    try:
        response = requests.get(
            f"{BACKEND_URL}/api/sync/alter-ids",
            params=params,
            headers={"x-api-key": API_KEY},
            timeout=min(get_backend_timeout_seconds(), 60),
        )

        if response.status_code == 404:
            return None, "company_not_found"

        if response.status_code == 409:
            return None, "company_not_ready"

        if not response.ok:
            print(f"[Cloud] Alter-id lookup failed: HTTP {response.status_code}")
            print(f"[Cloud] Response: {response.text[:300]}")
            return None, "lookup_failed"

        data = response.json() if response.content else {}
        return data if isinstance(data, dict) else {}, "ok"
    except requests.exceptions.ConnectionError:
        print(f"[Cloud] Cannot reach backend alter-id endpoint at {BACKEND_URL}")
        return None, "backend_unreachable"
    except Exception as e:
        print(f"[Cloud] Alter-id lookup error: {e}")
        return None, "lookup_failed"


def verify_remote_sync_completion(
    expected_alter_ids: dict | None,
    previous_last_synced_at: str | None = None,
) -> bool:
    deadline = time.time() + get_backend_post_verify_seconds()
    poll_seconds = get_backend_post_verify_poll_seconds()
    print("[Cloud] Backend response timed out - checking whether the sync completed anyway...")

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
                    "[Cloud] Backend completed the sync after the client timed out; "
                    "treating this push as successful."
                )
                return True
        time.sleep(poll_seconds)

    print("[Cloud] Backend did not confirm sync completion after the timeout window.")
    return False


def push(payload: dict) -> bool:
    _set_last_push_error("")

    if not BACKEND_URL:
        print("[Cloud] No backend URL configured - skipping push")
        return True

    previous_remote_state, previous_remote_status = fetch_remote_alter_ids()
    previous_last_synced_at = (
        _string_marker(previous_remote_state.get("last_synced_at"))
        if previous_remote_status == "ok" and previous_remote_state
        else None
    )

    try:
        response = requests.post(
            f"{BACKEND_URL}/api/sync",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": API_KEY,
            },
            timeout=get_backend_timeout_seconds(),
        )

        if response.ok:
            data = response.json()
            if data.get("success") is False:
                message = str(data.get("error") or data)
                _set_last_push_error(message)
                print(f"[Cloud] Push failed: {message}")
                return False

            records = data.get("records", {})
            print("[Cloud] Sync successful!")
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
                    print(f"[Cloud] {label}: {records.get(key, 0)}")
            return True

        error_message = _extract_backend_error(response)
        _set_last_push_error(error_message)
        print(f"[Cloud] Push failed: HTTP {response.status_code}")
        print(f"[Cloud] Response: {error_message}")
        return False

    except requests.exceptions.ConnectionError:
        print(f"[Cloud] Cannot reach backend at {BACKEND_URL}")
        print("[Cloud] Is the backend running?")
        _set_last_push_error(f"Cannot reach backend at {BACKEND_URL}")
        return False
    except requests.exceptions.ReadTimeout:
        expected_alter_ids = payload.get("alter_ids") if isinstance(payload.get("alter_ids"), dict) else None
        if verify_remote_sync_completion(expected_alter_ids, previous_last_synced_at):
            return True
        timeout_message = (
            f"Backend at {BACKEND_URL} did not respond within "
            f"{get_backend_timeout_seconds()}s."
        )
        _set_last_push_error(timeout_message)
        print(f"[Cloud] {timeout_message}")
        return False
    except Exception as e:
        print(f"[Cloud] Push error: {e}")
        _set_last_push_error(str(e))
        return False
