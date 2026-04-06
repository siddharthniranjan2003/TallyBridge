import os

import requests

BACKEND_URL = os.environ.get("BACKEND_URL", "")
API_KEY = os.environ.get("API_KEY", "")
TALLY_COMPANY = os.environ.get("TALLY_COMPANY", "").strip()
TALLY_COMPANY_GUID = os.environ.get("TALLY_COMPANY_GUID", "").strip()


def get_backend_timeout_seconds() -> int:
    try:
        return max(60, int(os.environ.get("BACKEND_TIMEOUT_SECONDS", "120")))
    except ValueError:
        return 120


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

def push(payload: dict) -> bool:
    if not BACKEND_URL:
        print("[Cloud] No backend URL configured — skipping push")
        return True

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
                print(f"[Cloud] Push failed: {data}")
                return False

            records = data.get("records", {})
            print(f"[Cloud] Sync successful!")
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
        else:
            print(f"[Cloud] Push failed: HTTP {response.status_code}")
            print(f"[Cloud] Response: {response.text[:300]}")
            return False

    except requests.exceptions.ConnectionError:
        print(f"[Cloud] Cannot reach backend at {BACKEND_URL}")
        print(f"[Cloud] Is the backend running?")
        return False
    except Exception as e:
        print(f"[Cloud] Push error: {e}")
        return False
