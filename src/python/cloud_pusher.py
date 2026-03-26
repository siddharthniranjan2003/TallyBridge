import requests
import os

BACKEND_URL = os.environ.get("BACKEND_URL", "")
API_KEY = os.environ.get("API_KEY", "")

def push(payload: dict) -> bool:
    if not BACKEND_URL:
        print("[Cloud] No backend URL configured — skipping push")
        return True  # don't fail sync just because backend not set up yet

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
            print(f"[Cloud] Push successful")
            return True
        else:
            print(f"[Cloud] Push failed: {response.status_code} {response.text[:200]}")
            return False
    except Exception as e:
        print(f"[Cloud] Push error: {e}")
        return False