import os

import requests

BACKEND_URL = os.environ.get("BACKEND_URL", "")
API_KEY = os.environ.get("API_KEY", "")


def get_backend_timeout_seconds() -> int:
    try:
        return max(60, int(os.environ.get("BACKEND_TIMEOUT_SECONDS", "120")))
    except ValueError:
        return 120

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
