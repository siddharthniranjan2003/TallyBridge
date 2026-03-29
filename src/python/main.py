import os
import sys
import json
from datetime import date

from tally_client import (
    get_company_alter_ids,
    get_ledgers, get_vouchers, get_stock_items,
    get_outstanding_receivables, get_outstanding_payables,
    get_profit_and_loss, get_balance_sheet, get_trial_balance,
)
from xml_parser import (
    parse_alter_ids,
    parse_ledgers, parse_vouchers, parse_stock, parse_outstanding,
    parse_profit_and_loss, parse_balance_sheet, parse_trial_balance,
)
from cloud_pusher import push

COMPANY = os.environ.get("TALLY_COMPANY", "")

# ── Change detection ─────────────────────────────────────────────
# Store last-known alter IDs in a local file so we can skip
# full sync when nothing has changed in TallyPrime.

CACHE_FILE = os.path.join(os.path.dirname(__file__), ".alter_ids_cache.json")

def load_cached_ids() -> dict:
    """Load previously saved alter IDs from disk."""
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r") as f:
                cache = json.load(f)
                return cache.get(COMPANY, {})
    except:
        pass
    return {}

def save_cached_ids(ids: dict):
    """Save current alter IDs to disk."""
    try:
        cache = {}
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r") as f:
                cache = json.load(f)
        cache[COMPANY] = ids
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except Exception as e:
        print(f"[Cache] Could not save alter IDs: {e}")

def has_data_changed(current_ids: dict) -> bool:
    """Compare current alter IDs with cached ones. Returns True if data changed."""
    if not current_ids:
        return True  # can't detect → assume changed

    cached = load_cached_ids()
    if not cached:
        return True  # first run → must sync

    # Compare the key counters
    for key in ("alter_id", "alt_vch_id", "alt_mst_id", "vch_id"):
        if current_ids.get(key, 0) != cached.get(key, 0):
            return True

    return False


# ── Main sync ────────────────────────────────────────────────────

def main():
    print(f"[TallyBridge] Starting sync: {COMPANY}")

    # Financial year date range
    today = date.today()
    fy_year = today.year - 1 if today.month < 4 else today.year
    from_date = f"{fy_year}0401"
    to_date = today.strftime("%Y%m%d")

    # ── Step 1: Change detection ─────────────────────────────
    try:
        print("[Tally] Checking for changes...")
        raw_ids = get_company_alter_ids()
        current_ids = parse_alter_ids(raw_ids)

        if current_ids:
            print(f"[Tally] AlterID={current_ids.get('alter_id')}, "
                  f"VchID={current_ids.get('alt_vch_id')}, "
                  f"MstID={current_ids.get('alt_mst_id')}")

            if not has_data_changed(current_ids):
                print("[TallyBridge] No changes detected — skipping sync.")
                print(json.dumps({
                    "status": "skipped",
                    "reason": "no_changes",
                    "records": {"ledgers": 0, "vouchers": 0, "stock": 0, "outstanding": 0}
                }))
                sys.exit(0)
            else:
                print("[TallyBridge] Changes detected — proceeding with full sync.")
        else:
            print("[TallyBridge] Could not read alter IDs — proceeding with sync anyway.")
    except Exception as e:
        print(f"[TallyBridge] Change detection failed ({e}) — proceeding with sync.")

    # ── Step 2: Fetch all data ───────────────────────────────
    try:
        print("[Tally] Fetching ledgers...")
        ledgers = parse_ledgers(get_ledgers())
        print(f"[Tally] Got {len(ledgers)} ledgers")

        print("[Tally] Fetching vouchers...")
        vouchers = parse_vouchers(get_vouchers(from_date, to_date))
        print(f"[Tally] Got {len(vouchers)} vouchers")

        print("[Tally] Fetching stock items...")
        stock = parse_stock(get_stock_items())
        print(f"[Tally] Got {len(stock)} stock items")

        print("[Tally] Fetching outstanding...")
        outstanding = (
            parse_outstanding(get_outstanding_receivables(), "receivable") +
            parse_outstanding(get_outstanding_payables(), "payable")
        )
        print(f"[Tally] Got {len(outstanding)} outstanding entries")

        # ── NEW: Financial reports ───────────────────────────
        print("[Tally] Fetching Profit & Loss...")
        profit_loss = parse_profit_and_loss(get_profit_and_loss(from_date, to_date))
        print(f"[Tally] Got {len(profit_loss)} P&L line items")

        print("[Tally] Fetching Balance Sheet...")
        balance_sheet = parse_balance_sheet(get_balance_sheet(from_date, to_date))
        print(f"[Tally] Got {len(balance_sheet)} Balance Sheet items")

        print("[Tally] Fetching Trial Balance...")
        trial_balance = parse_trial_balance(get_trial_balance(from_date, to_date))
        print(f"[Tally] Got {len(trial_balance)} Trial Balance items")

        # ── Step 3: Push to cloud ────────────────────────────
        print("[Cloud] Pushing to backend...")
        push({
            "company_name": COMPANY,
            "ledgers": ledgers,
            "vouchers": vouchers,
            "stock_items": stock,
            "outstanding": outstanding,
            "profit_loss": profit_loss,
            "balance_sheet": balance_sheet,
            "trial_balance": trial_balance,
        })

        # ── Step 4: Cache alter IDs ──────────────────────────
        if current_ids:
            save_cached_ids(current_ids)
            print("[TallyBridge] Cached alter IDs for next change detection.")

        # Print JSON summary as last line — Electron reads this
        print(json.dumps({
            "status": "success",
            "records": {
                "ledgers": len(ledgers),
                "vouchers": len(vouchers),
                "stock": len(stock),
                "outstanding": len(outstanding),
                "profit_loss": len(profit_loss),
                "balance_sheet": len(balance_sheet),
                "trial_balance": len(trial_balance),
            }
        }))
        sys.exit(0)

    except Exception as e:
        print(f"[Error] Sync failed: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()