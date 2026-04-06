import os
import sys
import json
import time
from datetime import date, datetime, timedelta

from tally_client import (
    get_company_info, get_company_alter_ids,
    get_groups,
    get_ledgers, get_vouchers, get_stock_items, get_stock_summary_report,
    get_outstanding_receivables, get_outstanding_payables,
    get_profit_and_loss, get_balance_sheet, get_trial_balance,
)
from xml_parser import (
    parse_company_info, parse_alter_ids,
    parse_groups,
    parse_ledgers, parse_vouchers, parse_stock, parse_outstanding,
    parse_profit_and_loss, parse_balance_sheet, parse_trial_balance,
)
from cloud_pusher import fetch_remote_alter_ids, push
from definition_extractor import fetch_structured_section

COMPANY = os.environ.get("TALLY_COMPANY", "")
COMPANY_GUID = os.environ.get("TALLY_COMPANY_GUID", "").strip()
COMPANY_CACHE_KEY = COMPANY_GUID or COMPANY
FORCE_FULL_SYNC = os.environ.get("TB_FORCE_FULL_SYNC", "").strip().lower() in {
    "1", "true", "yes", "on"
}

# ── Change detection ─────────────────────────────────────────────
# Store last-known alter IDs in a local file so we can skip
# full sync when nothing has changed in TallyPrime.

def resolve_cache_file() -> str:
    user_data_dir = os.environ.get("TB_USER_DATA_DIR")
    if user_data_dir:
        cache_dir = user_data_dir
    elif os.name == "nt" and os.environ.get("APPDATA"):
        cache_dir = os.path.join(os.environ["APPDATA"], "TallyBridge")
    else:
        cache_dir = os.path.dirname(__file__)

    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, ".alter_ids_cache.json")


CACHE_FILE = resolve_cache_file()
VOUCHER_OVERLAP_DAYS = 7
ENABLE_INCREMENTAL_VOUCHER_SYNC = os.environ.get("TB_ENABLE_INCREMENTAL_VOUCHER_SYNC", "").strip().lower() in {
    "1", "true", "yes", "on"
}

def load_cached_ids() -> tuple[dict, bool]:
    """Load previously saved alter IDs from disk.

    Returns:
        (cached_ids, had_load_error)
    """
    if not os.path.exists(CACHE_FILE):
        return {}, False

    try:
        with open(CACHE_FILE, "r") as f:
            cache = json.load(f)

        company_cache = cache.get(COMPANY_CACHE_KEY)
        if company_cache is None and COMPANY_GUID:
            company_cache = cache.get(COMPANY)
        if company_cache is None:
            return {}, False
        if isinstance(company_cache, dict):
            return company_cache, False

        raise TypeError("company cache entry must be an object")
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"[Cache] Could not load alter IDs: {e}")
        return {}, True

def save_cached_ids(ids: dict) -> bool:
    """Save current alter IDs to disk using an atomic replace with retries."""
    try:
        cache = {}
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r") as f:
                cache = json.load(f)
        cache[COMPANY_CACHE_KEY] = ids
        temp_path = f"{CACHE_FILE}.{os.getpid()}.tmp"
        last_error = None

        for attempt in range(5):
            try:
                with open(temp_path, "w") as f:
                    json.dump(cache, f)
                os.replace(temp_path, CACHE_FILE)
                return True
            except PermissionError as e:
                last_error = e
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                time.sleep(0.2 * (attempt + 1))
            except OSError as e:
                last_error = e
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                break

        if last_error:
            try:
                with open(CACHE_FILE, "w") as f:
                    json.dump(cache, f)
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                return True
            except OSError:
                raise last_error
    except (OSError, json.JSONDecodeError, TypeError) as e:
        print(f"[Cache] Could not save alter IDs: {e}")
    return False

def parse_iso_date(iso_str: str):
    if not iso_str:
        return None
    try:
        return datetime.strptime(iso_str, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_tally_compact_date(compact_str: str):
    if not compact_str or len(compact_str) != 8 or not compact_str.isdigit():
        return None
    try:
        return date(int(compact_str[:4]), int(compact_str[4:6]), int(compact_str[6:8]))
    except ValueError:
        return None


def format_tally_compact(d: date) -> str:
    return d.strftime("%Y%m%d")


def build_full_sync_plan(reason: str, fy_from: str, fy_to: str) -> dict:
    return {
        "has_changes": True,
        "master_changed": True,
        "voucher_changed": True,
        "need_groups": True,
        "need_ledgers": True,
        "need_vouchers": True,
        "need_stock": True,
        "need_outstanding": True,
        "need_reports": True,
        "voucher_from_date": fy_from,
        "voucher_to_date": fy_to,
        "voucher_sync_mode": "full",
        "reason": reason,
    }


def build_sync_plan(current_ids: dict, fy_from: str, fy_to: str, force_full_sync: bool = False) -> dict:
    """Figure out which sections need refreshing and whether vouchers can be incremental."""
    cached_ids, cache_load_failed = load_cached_ids()

    if force_full_sync:
        return build_full_sync_plan("forced_full_sync", fy_from, fy_to)

    if not current_ids:
        return build_full_sync_plan("missing_change_markers", fy_from, fy_to)

    if cache_load_failed:
        return build_full_sync_plan("cache_load_failed", fy_from, fy_to)

    if not cached_ids:
        return build_full_sync_plan("first_successful_sync", fy_from, fy_to)

    company_changed = current_ids.get("alter_id", "0") != cached_ids.get("alter_id", "0")
    voucher_changed = (
        current_ids.get("alt_vch_id", "0") != cached_ids.get("alt_vch_id", "0")
        or current_ids.get("vch_id", "0") != cached_ids.get("vch_id", "0")
    )
    master_changed = (
        current_ids.get("alt_mst_id", "0") != cached_ids.get("alt_mst_id", "0")
        or (company_changed and not voucher_changed)
    )

    has_changes = company_changed or voucher_changed or master_changed
    if not has_changes:
        return {
            "has_changes": False,
            "reason": "no_changes",
        }

    voucher_from_date = fy_from
    voucher_sync_mode = "none"

    if voucher_changed:
        voucher_sync_mode = "full"
        fy_from_date = parse_tally_compact_date(fy_from)
        cached_last = parse_iso_date(cached_ids.get("last_voucher_date"))
        current_last = parse_iso_date(current_ids.get("last_voucher_date"))

        if (
            ENABLE_INCREMENTAL_VOUCHER_SYNC
            and fy_from_date
            and cached_last
            and current_last
            and current_last > cached_last
        ):
            start_date = max(fy_from_date, cached_last - timedelta(days=VOUCHER_OVERLAP_DAYS))
            voucher_from_date = format_tally_compact(start_date)
            voucher_sync_mode = "incremental"

    return {
        "has_changes": True,
        "master_changed": master_changed,
        "voucher_changed": voucher_changed,
        "need_groups": master_changed,
        "need_ledgers": master_changed,
        "need_vouchers": voucher_changed,
        "need_stock": master_changed or voucher_changed,
        "need_outstanding": voucher_changed,
        "need_reports": voucher_changed,
        "voucher_from_date": voucher_from_date,
        "voucher_to_date": fy_to,
        "voucher_sync_mode": voucher_sync_mode,
        "reason": "changes_detected",
    }


# ── Financial year helpers ───────────────────────────────────────

def get_fy_dates_fallback() -> tuple:
    """Hardcoded April-March FY as fallback."""
    today = date.today()
    fy_year = today.year - 1 if today.month < 4 else today.year
    from_date = f"{fy_year}0401"
    to_date = today.strftime("%Y%m%d")
    return from_date, to_date

def fy_date_from_iso(iso_str: str) -> str:
    """Convert ISO date '2025-04-01' → Tally format '20250401'."""
    if not iso_str:
        return ""
    return iso_str.replace("-", "")


# ── Main sync ────────────────────────────────────────────────────

def main():
    print(f"[TallyBridge] Starting sync: {COMPANY}")

    # ── Step 0: Fetch company info (FY dates) ────────────────
    from_date, to_date = get_fy_dates_fallback()
    company_info = {}
    current_ids = {}
    try:
        print("[Tally] Fetching company info...")
        try:
            company_info = fetch_structured_section("company_info")
            if company_info:
                print("[Tally] Company info loaded via definition-driven collection")
        except Exception as structured_error:
            print(f"[Tally] Structured company info fetch failed ({structured_error}) — falling back to legacy parser")
            raw_info = get_company_info()
            company_info = parse_company_info(raw_info)
        if company_info:
            books_from = company_info.get("books_from")
            books_to = company_info.get("books_to")
            if books_from and not books_to:
                books_from_date = parse_iso_date(books_from)
                if books_from_date:
                    try:
                        derived_books_to = books_from_date.replace(year=books_from_date.year + 1) - timedelta(days=1)
                    except ValueError:
                        derived_books_to = None
                    if derived_books_to:
                        books_to = derived_books_to.isoformat()
                        company_info["books_to"] = books_to
            if books_from:
                from_date = fy_date_from_iso(books_from)
                effective_to = min(
                    parse_iso_date(books_to) or date.today(),
                    date.today(),
                )
                to_date = format_tally_compact(effective_to)
                if books_to:
                    print(f"[Tally] Company FY: {books_from} to {books_to}")
                else:
                    print(f"[Tally] Company FY starts {books_from}; end date not exposed by Tally, syncing through {effective_to.isoformat()}")
            else:
                print("[Tally] Could not read FY dates — using default April-March")
            if company_info.get("gstin"):
                print(f"[Tally] GSTIN: {company_info['gstin']}")
        else:
            print("[Tally] Could not read company info — using default FY dates")
    except Exception as e:
        print(f"[Tally] Company info fetch failed ({e}) — using default FY dates")

    print(f"[Tally] Date range: {from_date} to {to_date}")

    # ── Step 1: Change detection ─────────────────────────────
    sync_plan = {
        "has_changes": True,
        "master_changed": True,
        "voucher_changed": True,
        "need_groups": True,
        "need_ledgers": True,
        "need_vouchers": True,
        "need_stock": True,
        "need_outstanding": True,
        "need_reports": True,
        "voucher_from_date": from_date,
        "voucher_to_date": to_date,
        "voucher_sync_mode": "full",
        "reason": "pre_change_detection",
    }
    try:
        print("[Tally] Checking for changes...")
        raw_ids = get_company_alter_ids()
        current_ids = parse_alter_ids(raw_ids)
        force_full_sync = FORCE_FULL_SYNC

        if force_full_sync:
            print("[TallyBridge] Forcing full sync because this company has not synced from this connector yet.")

        remote_alter_ids, remote_lookup_status = fetch_remote_alter_ids()
        if remote_lookup_status == "company_not_found":
            force_full_sync = True
            print("[Cloud] Company not found in backend — forcing full sync.")
        elif remote_lookup_status == "ok":
            remote_has_change_markers = any(
                remote_alter_ids.get(key)
                for key in ("alter_id", "alt_vch_id", "alt_mst_id")
            ) if remote_alter_ids else False
            if not remote_has_change_markers:
                force_full_sync = True
                print("[Cloud] Backend company has no alter IDs yet — forcing full sync.")

        sync_plan = build_sync_plan(current_ids, from_date, to_date, force_full_sync=force_full_sync)

        if current_ids:
            print(f"[Tally] AlterID={current_ids.get('alter_id')}, "
                  f"VchID={current_ids.get('alt_vch_id')}, "
                  f"MstID={current_ids.get('alt_mst_id')}")

            if not sync_plan.get("has_changes"):
                print("[TallyBridge] No changes detected — skipping sync.")
                print(json.dumps({
                    "status": "skipped",
                    "reason": "no_changes",
                    "records": {}
                }))
                sys.exit(0)
            else:
                print(f"[TallyBridge] Changes detected — proceeding with {sync_plan.get('voucher_sync_mode', 'full')} sync plan.")
        else:
            print("[TallyBridge] Could not read alter IDs — proceeding with sync anyway.")
    except Exception as e:
        print(f"[TallyBridge] Change detection failed ({e}) — proceeding with sync.")

    # ── Step 2: Fetch all data ───────────────────────────────
    try:
        groups = None
        ledgers = None
        vouchers = None
        stock = None
        outstanding = None
        profit_loss = None
        balance_sheet = None
        trial_balance = None
        record_updates = {}

        if sync_plan.get("need_groups"):
            print("[Tally] Fetching groups...")
            try:
                groups = fetch_structured_section("groups")
                if not groups:
                    raise ValueError("structured groups collection returned no rows")
                print("[Tally] Groups loaded via definition-driven collection")
            except Exception as structured_error:
                print(f"[Tally] Structured groups fetch failed ({structured_error}) — falling back to legacy parser")
                groups = parse_groups(get_groups())
            record_updates["groups"] = len(groups)
            print(f"[Tally] Got {len(groups)} groups")

        if sync_plan.get("need_ledgers"):
            print("[Tally] Fetching ledgers...")
            try:
                ledgers = fetch_structured_section("ledgers")
                if not ledgers:
                    raise ValueError("structured ledger collection returned no rows")
                print("[Tally] Ledgers loaded via definition-driven collection")
            except Exception as structured_error:
                print(f"[Tally] Structured ledgers fetch failed ({structured_error}) — falling back to legacy parser")
                ledgers = parse_ledgers(get_ledgers())
            record_updates["ledgers"] = len(ledgers)
            print(f"[Tally] Got {len(ledgers)} ledgers")

        if sync_plan.get("need_vouchers"):
            voucher_from_date = sync_plan.get("voucher_from_date", from_date)
            voucher_to_date = sync_plan.get("voucher_to_date", to_date)
            print(f"[Tally] Fetching vouchers ({voucher_from_date} to {voucher_to_date})...")
            try:
                vouchers = fetch_structured_section("vouchers", {
                    "from_date": voucher_from_date,
                    "to_date": voucher_to_date,
                })
                print("[Tally] Vouchers loaded via definition-driven collection")
            except Exception as structured_error:
                print(f"[Tally] Structured voucher fetch failed ({structured_error}) — falling back to Day Book parser")
                vouchers = parse_vouchers(get_vouchers(voucher_from_date, voucher_to_date))
            record_updates["vouchers"] = len(vouchers)
            print(f"[Tally] Got {len(vouchers)} vouchers")

        if sync_plan.get("need_stock"):
            print("[Tally] Fetching stock items...")
            try:
                try:
                    stock = fetch_structured_section("stock_items")
                    print("[Tally] Stock items loaded via definition-driven collection")
                except Exception as structured_error:
                    print(f"[Tally] Structured stock collection failed ({structured_error}) — falling back to legacy parser")
                    stock = parse_stock(get_stock_items())
                metrics_detected = any(
                    item.get("closing_value") or item.get("rate")
                    for item in stock
                )
                if stock and metrics_detected:
                    print(f"[Tally] Got {len(stock)} stock items via structured collection")
                else:
                    print("[Tally] Structured stock export was sparse — falling back to Stock Summary report")
                    stock = parse_stock(get_stock_summary_report())
                    print(f"[Tally] Got {len(stock)} stock items via Stock Summary report")
            except Exception as e:
                print(f"[Tally] Structured stock export failed ({e}) — falling back to Stock Summary report")
                stock = parse_stock(get_stock_summary_report())
                print(f"[Tally] Got {len(stock)} stock items via Stock Summary report")
            record_updates["stock"] = len(stock)

        if sync_plan.get("need_outstanding"):
            print("[Tally] Fetching outstanding...")
            try:
                outstanding = (
                    fetch_structured_section("outstanding_receivables") +
                    fetch_structured_section("outstanding_payables")
                )
                print("[Tally] Outstanding loaded via definition-driven report parsing")
            except Exception as structured_error:
                print(f"[Tally] Structured outstanding fetch failed ({structured_error}) — falling back to legacy parser")
                outstanding = (
                    parse_outstanding(get_outstanding_receivables(), "receivable") +
                    parse_outstanding(get_outstanding_payables(), "payable")
                )
            record_updates["outstanding"] = len(outstanding)
            print(f"[Tally] Got {len(outstanding)} outstanding entries")

        if sync_plan.get("need_reports"):
            # ── Financial reports ────────────────────────────────
            print("[Tally] Fetching Profit & Loss...")
            try:
                profit_loss = fetch_structured_section("profit_loss", {
                    "from_date": from_date,
                    "to_date": to_date,
                })
                if not profit_loss:
                    raise ValueError("structured profit and loss report returned no rows")
                print("[Tally] Profit & Loss loaded via definition-driven report parsing")
            except Exception as structured_error:
                print(f"[Tally] Structured Profit & Loss fetch failed ({structured_error}) — falling back to legacy parser")
                profit_loss = parse_profit_and_loss(get_profit_and_loss(from_date, to_date))
            print(f"[Tally] Got {len(profit_loss)} P&L line items")
            record_updates["profit_loss"] = len(profit_loss)

            print("[Tally] Fetching Balance Sheet...")
            try:
                balance_sheet = fetch_structured_section("balance_sheet", {
                    "from_date": from_date,
                    "to_date": to_date,
                })
                if not balance_sheet:
                    raise ValueError("structured balance sheet report returned no rows")
                print("[Tally] Balance Sheet loaded via definition-driven report parsing")
            except Exception as structured_error:
                print(f"[Tally] Structured Balance Sheet fetch failed ({structured_error}) — falling back to legacy parser")
                balance_sheet = parse_balance_sheet(get_balance_sheet(from_date, to_date))
            print(f"[Tally] Got {len(balance_sheet)} Balance Sheet items")
            record_updates["balance_sheet"] = len(balance_sheet)

            print("[Tally] Fetching Trial Balance...")
            try:
                trial_balance = fetch_structured_section("trial_balance", {
                    "from_date": from_date,
                    "to_date": to_date,
                })
                if not trial_balance:
                    raise ValueError("structured trial balance report returned no rows")
                print("[Tally] Trial Balance loaded via definition-driven report parsing")
            except Exception as structured_error:
                print(f"[Tally] Structured Trial Balance fetch failed ({structured_error}) — falling back to legacy parser")
                trial_balance = parse_trial_balance(get_trial_balance(from_date, to_date))
            print(f"[Tally] Got {len(trial_balance)} Trial Balance items")
            record_updates["trial_balance"] = len(trial_balance)

        # ── Step 3: Push to cloud ────────────────────────────
        print("[Cloud] Pushing to backend...")
        payload = {
            "company_name": COMPANY,
            "company_guid": COMPANY_GUID or None,
            "company_info": company_info,
            "alter_ids": current_ids,
            "groups": groups,
            "ledgers": ledgers,
            "vouchers": vouchers,
            "stock_items": stock,
            "outstanding": outstanding,
            "profit_loss": profit_loss,
            "balance_sheet": balance_sheet,
            "trial_balance": trial_balance,
            "sync_meta": {
                "voucher_sync_mode": sync_plan.get("voucher_sync_mode", "full"),
                "voucher_from_date": sync_plan.get("voucher_from_date"),
                "voucher_to_date": sync_plan.get("voucher_to_date"),
                "master_changed": sync_plan.get("master_changed", True),
                "voucher_changed": sync_plan.get("voucher_changed", True),
            },
        }

        if not push(payload):
            raise RuntimeError("Cloud push failed")

        # ── Step 4: Cache alter IDs ──────────────────────────
        if current_ids:
            if save_cached_ids(current_ids):
                print("[TallyBridge] Cached alter IDs for next change detection.")
            else:
                print("[TallyBridge] Alter ID cache could not be updated; next sync may re-fetch more data.")

        # Print JSON summary as last line — Electron reads this
        print(json.dumps({
            "status": "success",
            "records": record_updates,
            "voucher_sync_mode": sync_plan.get("voucher_sync_mode", "full"),
        }))
        sys.exit(0)

    except Exception as e:
        print(f"[Error] Sync failed: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
