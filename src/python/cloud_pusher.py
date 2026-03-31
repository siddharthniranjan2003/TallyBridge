import requests
import os
import json

BACKEND_URL = os.environ.get("BACKEND_URL", "")
API_KEY = os.environ.get("API_KEY", "")

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
            timeout=60,
        )

        if response.status_code == 200:
            data = response.json()
            records = data.get("records", {})
            print(f"[Cloud] Sync successful!")
            print(f"[Cloud] Groups: {records.get('groups', 0)}")
            print(f"[Cloud] Ledgers: {records.get('ledgers', 0)}")
            print(f"[Cloud] Vouchers: {records.get('vouchers', 0)}")
            print(f"[Cloud] Stock: {records.get('stock_items', 0)}")
            print(f"[Cloud] Outstanding: {records.get('outstanding', 0)}")
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