import json
import os
import sys
import time
from datetime import date, datetime, timedelta

from cloud_pusher import fetch_remote_alter_ids, push
from definition_extractor import fetch_structured_section
from odbc_bridge import OdbcBridge, compare_section_rows
from tally_client import (
    TallyConnectionError,
    TallyTimeoutError,
    detect_tally_product,
    get_balance_sheet,
    get_company_alter_ids,
    get_company_info,
    get_groups,
    get_ledgers,
    get_outstanding_payables,
    get_outstanding_receivables,
    get_profit_and_loss,
    get_stock_items,
    get_stock_summary_report,
    get_trial_balance,
    get_vouchers,
)
from xml_parser import (
    parse_alter_ids,
    parse_balance_sheet,
    parse_company_info,
    parse_groups,
    parse_ledgers,
    parse_outstanding,
    parse_profit_and_loss,
    parse_stock,
    parse_trial_balance,
    parse_vouchers,
)

COMPANY = os.environ.get("TALLY_COMPANY", "")
COMPANY_GUID = os.environ.get("TALLY_COMPANY_GUID", "").strip()
COMPANY_CACHE_KEY = COMPANY_GUID or COMPANY
FORCE_FULL_SYNC = os.environ.get("TB_FORCE_FULL_SYNC", "").strip().lower() in {
    "1", "true", "yes", "on",
}
READ_MODE = (os.environ.get("TB_READ_MODE", "auto") or "auto").strip().lower()
if READ_MODE not in {"auto", "xml-only", "hybrid", "shadow"}:
    READ_MODE = "auto"
ENABLE_INCREMENTAL_VOUCHER_SYNC = os.environ.get(
    "TB_ENABLE_INCREMENTAL_VOUCHER_SYNC",
    "",
).strip().lower() in {"1", "true", "yes", "on"}
SYNC_FROM_DATE_OVERRIDE_RAW = os.environ.get("TB_SYNC_FROM_DATE", "").strip()
SYNC_TO_DATE_OVERRIDE_RAW = os.environ.get("TB_SYNC_TO_DATE", "").strip()
VOUCHER_OVERLAP_DAYS = 7


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


def load_cached_ids() -> tuple[dict, bool]:
    if not os.path.exists(CACHE_FILE):
        return {}, False

    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as handle:
            cache = json.load(handle)

        company_cache = cache.get(COMPANY_CACHE_KEY)
        if company_cache is None and COMPANY_GUID:
            company_cache = cache.get(COMPANY)
        if company_cache is None:
            return {}, False
        if isinstance(company_cache, dict):
            return company_cache, False
        raise TypeError("company cache entry must be an object")
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        print(f"[Cache] Could not load alter IDs: {error}")
        return {}, True


def save_cached_ids(ids: dict) -> bool:
    try:
        cache = {}
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r", encoding="utf-8") as handle:
                cache = json.load(handle)
        cache[COMPANY_CACHE_KEY] = ids
        temp_path = f"{CACHE_FILE}.{os.getpid()}.tmp"
        last_error = None

        for attempt in range(5):
            try:
                with open(temp_path, "w", encoding="utf-8") as handle:
                    json.dump(cache, handle)
                os.replace(temp_path, CACHE_FILE)
                return True
            except PermissionError as error:
                last_error = error
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                time.sleep(0.2 * (attempt + 1))
            except OSError as error:
                last_error = error
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                break

        if last_error:
            with open(CACHE_FILE, "w", encoding="utf-8") as handle:
                json.dump(cache, handle)
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            return True
    except (OSError, json.JSONDecodeError, TypeError) as error:
        print(f"[Cache] Could not save alter IDs: {error}")
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


def format_tally_compact(value: date) -> str:
    return value.strftime("%Y%m%d")


def parse_date_override(raw_value: str) -> str | None:
    value = (raw_value or "").strip()
    if not value:
        return None

    if len(value) == 8 and value.isdigit():
        parsed = parse_tally_compact_date(value)
        if not parsed:
            raise ValueError(f"Invalid compact date override: {value}")
        return value

    parsed_iso = parse_iso_date(value)
    if parsed_iso:
        return format_tally_compact(parsed_iso)

    raise ValueError(
        f"Invalid date override '{value}'. Use YYYY-MM-DD or YYYYMMDD."
    )


def resolve_effective_date_range(default_from_date: str, default_to_date: str) -> tuple[str, str, str]:
    source = "company_fy"
    from_date = default_from_date
    to_date = default_to_date

    override_from = parse_date_override(SYNC_FROM_DATE_OVERRIDE_RAW)
    override_to = parse_date_override(SYNC_TO_DATE_OVERRIDE_RAW)
    if override_from or override_to:
        source = "override"
        if override_from:
            from_date = override_from
        if override_to:
            to_date = override_to

    from_date_obj = parse_tally_compact_date(from_date)
    to_date_obj = parse_tally_compact_date(to_date)
    if not from_date_obj or not to_date_obj:
        raise ValueError(
            f"Invalid effective date range {from_date}..{to_date}. "
            "Expected compact Tally dates (YYYYMMDD)."
        )

    if from_date_obj > to_date_obj:
        raise ValueError(
            f"Invalid date range: from_date {from_date} is after to_date {to_date}."
        )

    return from_date, to_date, source


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


def get_fy_dates_fallback() -> tuple[str, str]:
    today = date.today()
    fy_year = today.year - 1 if today.month < 4 else today.year
    return f"{fy_year}0401", today.strftime("%Y%m%d")


def fy_date_from_iso(iso_str: str) -> str:
    return iso_str.replace("-", "") if iso_str else ""


def build_month_windows(from_date: str, to_date: str) -> list[tuple[str, str]]:
    start = parse_tally_compact_date(from_date)
    end = parse_tally_compact_date(to_date)
    if not start or not end or start > end:
        return [(from_date, to_date)]

    windows: list[tuple[str, str]] = []
    cursor = start
    while cursor <= end:
        next_month = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        window_end = min(next_month - timedelta(days=1), end)
        windows.append((format_tally_compact(cursor), format_tally_compact(window_end)))
        cursor = window_end + timedelta(days=1)
    return windows


def split_window(from_date: str, to_date: str) -> list[tuple[str, str]]:
    start = parse_tally_compact_date(from_date)
    end = parse_tally_compact_date(to_date)
    if not start or not end or start >= end:
        return [(from_date, to_date)]

    midpoint = start + timedelta(days=(end - start).days // 2)
    right_start = midpoint + timedelta(days=1)
    if right_start > end:
        return [(from_date, to_date)]

    return [
        (format_tally_compact(start), format_tally_compact(midpoint)),
        (format_tally_compact(right_start), format_tally_compact(end)),
    ]


def dedupe_vouchers(vouchers: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen_guids: set[str] = set()

    for voucher in sorted(
        vouchers,
        key=lambda row: (
            row.get("date") or "",
            row.get("voucher_number") or "",
            row.get("tally_guid") or "",
        ),
    ):
        tally_guid = (voucher.get("tally_guid") or "").strip()
        if tally_guid:
            if tally_guid in seen_guids:
                continue
            seen_guids.add(tally_guid)
        deduped.append(voucher)

    return deduped


def is_retryable_voucher_error(error: Exception) -> bool:
    if isinstance(error, (TallyTimeoutError, TallyConnectionError, TimeoutError)):
        return True
    message = str(error).lower()
    return "timed out" in message or "unresponsive" in message or "connection" in message


def fetch_voucher_window(from_date: str, to_date: str, prefer_day_book: bool = False) -> list[dict]:
    if prefer_day_book:
        print("[Tally] Using Day Book export for this voucher window")
        return parse_vouchers(get_vouchers(from_date, to_date))

    try:
        vouchers = fetch_structured_section(
            "vouchers",
            {"from_date": from_date, "to_date": to_date},
        )
        print("[Tally] Vouchers loaded via definition-driven collection")
        return vouchers
    except Exception as structured_error:
        print(
            f"[Tally] Structured voucher fetch failed for {from_date}..{to_date} "
            f"({structured_error}) - falling back to Day Book parser"
        )
        return parse_vouchers(get_vouchers(from_date, to_date))


def fetch_vouchers_with_batches(
    from_date: str,
    to_date: str,
    mode: str,
    prefer_day_book: bool = False,
) -> list[dict]:
    initial_windows = build_month_windows(from_date, to_date)
    all_vouchers: list[dict] = []

    def fetch_recursive(window_from: str, window_to: str, depth: int = 0) -> list[dict]:
        indent = "  " * depth
        print(f"[Tally] Voucher window {window_from} to {window_to}")
        try:
            rows = fetch_voucher_window(window_from, window_to, prefer_day_book=prefer_day_book)
            print(f"{indent}[Tally] Voucher window succeeded with {len(rows)} rows")
            return rows
        except Exception as error:
            if not is_retryable_voucher_error(error):
                raise

            nested_windows = split_window(window_from, window_to)
            if len(nested_windows) == 1 and nested_windows[0] == (window_from, window_to):
                raise RuntimeError(
                    f"Tally XML server became unresponsive while exporting vouchers for "
                    f"{window_from}..{window_to}: {error}"
                ) from error

            print(
                f"{indent}[Tally] Voucher window {window_from}..{window_to} timed out "
                "or dropped the connection - retrying with smaller windows"
            )
            split_rows: list[dict] = []
            for child_from, child_to in nested_windows:
                split_rows.extend(fetch_recursive(child_from, child_to, depth + 1))
            return split_rows

    print(
        f"[Tally] Fetching vouchers ({from_date} to {to_date}) using "
        f"{'batched full-year' if mode == 'full' else 'batched incremental'} XML export..."
    )
    for window_from, window_to in initial_windows:
        all_vouchers.extend(fetch_recursive(window_from, window_to))

    deduped = dedupe_vouchers(all_vouchers)
    duplicates_removed = len(all_vouchers) - len(deduped)
    if duplicates_removed > 0:
        print(f"[Tally] Removed {duplicates_removed} duplicate vouchers after batch merge")
    return deduped


def fetch_groups_xml() -> list[dict]:
    try:
        groups = fetch_structured_section("groups")
        if not groups:
            raise ValueError("structured groups collection returned no rows")
        print("[Tally] Groups loaded via definition-driven collection")
        return groups
    except Exception as structured_error:
        print(f"[Tally] Structured groups fetch failed ({structured_error}) - falling back to legacy parser")
        return parse_groups(get_groups())


def fetch_ledgers_xml() -> list[dict]:
    try:
        ledgers = fetch_structured_section("ledgers")
        if not ledgers:
            raise ValueError("structured ledger collection returned no rows")
        print("[Tally] Ledgers loaded via definition-driven collection")
        return ledgers
    except Exception as structured_error:
        print(f"[Tally] Structured ledgers fetch failed ({structured_error}) - falling back to legacy parser")
        return parse_ledgers(get_ledgers())


def fetch_stock_xml() -> list[dict]:
    try:
        try:
            stock = fetch_structured_section("stock_items")
            print("[Tally] Stock items loaded via definition-driven collection")
        except Exception as structured_error:
            print(
                f"[Tally] Structured stock collection failed ({structured_error}) - "
                "falling back to legacy parser"
            )
            stock = parse_stock(get_stock_items())

        metrics_detected = any(item.get("closing_value") or item.get("rate") for item in stock)
        if stock and metrics_detected:
            print(f"[Tally] Got {len(stock)} stock items via structured collection")
            return stock

        print("[Tally] Structured stock export was sparse - falling back to Stock Summary report")
        stock = parse_stock(get_stock_summary_report())
        print(f"[Tally] Got {len(stock)} stock items via Stock Summary report")
        return stock
    except Exception as error:
        print(f"[Tally] Structured stock export failed ({error}) - falling back to Stock Summary report")
        stock = parse_stock(get_stock_summary_report())
        print(f"[Tally] Got {len(stock)} stock items via Stock Summary report")
        return stock


def maybe_fetch_section_via_odbc(
    bridge: OdbcBridge | None,
    section_name: str,
) -> tuple[list[dict] | None, dict | None]:
    if not bridge:
        return None, None

    try:
        rows, result = bridge.fetch_section(section_name)
        if result.get("state") == "ok" and rows:
            dsn = result.get("dsn")
            print(f"[ODBC] {section_name} loaded via {dsn or 'Tally ODBC'}")
            return rows, result
        if result.get("state") == "empty":
            print(f"[ODBC] {section_name} returned no rows - falling back to XML")
            return None, result
        print(
            f"[ODBC] {section_name} unavailable ({result.get('message') or result.get('state')}) "
            "- falling back to XML"
        )
        return None, result
    except Exception as error:
        print(f"[ODBC] {section_name} failed ({error}) - falling back to XML")
        return None, {"state": "error", "message": str(error)}


def merge_odbc_status(base_status: dict | None, result: dict | None) -> dict | None:
    if not result:
        return base_status
    merged = dict(base_status or {})
    merged["state"] = result.get("state", merged.get("state"))
    if result.get("dsn"):
        merged["dsn"] = result.get("dsn")
    if result.get("message"):
        merged["message"] = result.get("message")
    if result.get("supported_sections") is not None:
        merged["supported_sections"] = result.get("supported_sections")
    return merged


def fetch_company_info_with_fallback() -> tuple[dict, str, str]:
    from_date, to_date = get_fy_dates_fallback()
    company_info: dict = {}

    try:
        print("[Tally] Fetching company info...")
        try:
            company_info = fetch_structured_section("company_info")
            if company_info:
                print("[Tally] Company info loaded via definition-driven collection")
        except Exception as structured_error:
            print(
                f"[Tally] Structured company info fetch failed ({structured_error}) - "
                "falling back to legacy parser"
            )
            company_info = parse_company_info(get_company_info())

        if company_info:
            books_from = company_info.get("books_from")
            books_to = company_info.get("books_to")
            if books_from and not books_to:
                books_from_date = parse_iso_date(books_from)
                if books_from_date:
                    try:
                        books_to = (
                            books_from_date.replace(year=books_from_date.year + 1)
                            - timedelta(days=1)
                        ).isoformat()
                        company_info["books_to"] = books_to
                    except ValueError:
                        books_to = None

            if books_from:
                from_date = fy_date_from_iso(books_from)
                effective_to = min(parse_iso_date(books_to) or date.today(), date.today())
                to_date = format_tally_compact(effective_to)
                if books_to:
                    print(f"[Tally] Company FY: {books_from} to {books_to}")
                else:
                    print(
                        f"[Tally] Company FY starts {books_from}; end date not exposed by Tally, "
                        f"syncing through {effective_to.isoformat()}"
                    )
            else:
                print("[Tally] Could not read FY dates - using default April-March")

            if company_info.get("gstin"):
                print(f"[Tally] GSTIN: {company_info['gstin']}")
        else:
            print("[Tally] Could not read company info - using default FY dates")
    except Exception as error:
        print(f"[Tally] Company info fetch failed ({error}) - using default FY dates")

    return company_info, from_date, to_date


def main() -> int:
    print(f"[TallyBridge] Starting sync: {COMPANY}")
    product_info = detect_tally_product()
    if product_info.get("product_name"):
        version_suffix = f" {product_info['product_version']}" if product_info.get("product_version") else ""
        print(f"[Tally] Connected to {product_info['product_name']}{version_suffix}")

    transport_mode = "hybrid" if READ_MODE == "shadow" else READ_MODE
    shadow_mode = READ_MODE == "shadow"
    section_sources: dict[str, str] = {}
    odbc_bridge = None
    odbc_status = {
        "state": "disabled" if transport_mode == "xml-only" else "not_configured",
        "dsn": None,
        "supported_sections": [],
        "message": None,
    }

    try:
        if transport_mode != "xml-only":
            odbc_bridge = OdbcBridge(os.environ.get("TB_ODBC_DSN_OVERRIDE", "").strip() or None)
            try:
                probe = odbc_bridge.probe(["groups", "ledgers", "stock_items"])
            except Exception as error:
                probe = {
                    "state": "error",
                    "dsn": None,
                    "supported_sections": [],
                    "message": str(error),
                }
            odbc_status = merge_odbc_status(odbc_status, probe) or odbc_status
            if probe.get("state") == "ok":
                supported = ", ".join(probe.get("supported_sections") or [])
                print(f"[ODBC] Probe succeeded via {probe.get('dsn')}. Supported sections: {supported or 'none'}")
            else:
                print(f"[ODBC] Probe status: {probe.get('state')} ({probe.get('message') or 'no DSN detected'})")

        company_info, default_from_date, default_to_date = fetch_company_info_with_fallback()
        from_date, to_date, date_range_source = resolve_effective_date_range(
            default_from_date,
            default_to_date,
        )
        manual_range_override = date_range_source == "override"
        if manual_range_override:
            print(
                "[TallyBridge] Manual date range override active "
                f"({from_date} to {to_date})."
            )
        current_ids = {}
        print(f"[Tally] Effective date range: {from_date} to {to_date} ({date_range_source})")

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
            current_ids = parse_alter_ids(get_company_alter_ids())
            force_full_sync = FORCE_FULL_SYNC or manual_range_override

            if force_full_sync:
                if manual_range_override:
                    print("[TallyBridge] Forcing full sync because a manual date range was configured.")
                else:
                    print("[TallyBridge] Forcing full sync because this company has not synced from this connector yet.")

            remote_alter_ids, remote_lookup_status = fetch_remote_alter_ids()
            if remote_lookup_status == "company_not_found":
                force_full_sync = True
                print("[Cloud] Company not found in backend - forcing full sync.")
            elif remote_lookup_status == "ok":
                remote_has_change_markers = any(
                    remote_alter_ids.get(key)
                    for key in ("alter_id", "alt_vch_id", "alt_mst_id")
                ) if remote_alter_ids else False
                if not remote_has_change_markers:
                    force_full_sync = True
                    print("[Cloud] Backend company has no alter IDs yet - forcing full sync.")

            sync_plan = build_sync_plan(current_ids, from_date, to_date, force_full_sync=force_full_sync)
            if current_ids:
                print(
                    f"[Tally] AlterID={current_ids.get('alter_id')}, "
                    f"VchID={current_ids.get('alt_vch_id')}, "
                    f"MstID={current_ids.get('alt_mst_id')}"
                )
                if not sync_plan.get("has_changes"):
                    print("[TallyBridge] No changes detected - skipping sync.")
                    print(json.dumps({
                        "status": "skipped",
                        "reason": "no_changes",
                        "records": {},
                    }))
                    return 0

                print(
                    f"[TallyBridge] Changes detected - proceeding with "
                    f"{sync_plan.get('voucher_sync_mode', 'full')} sync plan."
                )
            else:
                print("[TallyBridge] Could not read alter IDs - proceeding with sync anyway.")
        except Exception as error:
            print(f"[TallyBridge] Change detection failed ({error}) - proceeding with sync.")

        groups = None
        ledgers = None
        vouchers = None
        stock = None
        outstanding = None
        profit_loss = None
        balance_sheet = None
        trial_balance = None
        record_updates: dict[str, int] = {}

        if sync_plan.get("need_groups"):
            print("[Tally] Fetching groups...")
            used_odbc = False
            if transport_mode in {"auto", "hybrid"} and odbc_bridge:
                odbc_groups, odbc_result = maybe_fetch_section_via_odbc(odbc_bridge, "groups")
                odbc_status = merge_odbc_status(odbc_status, odbc_result) or odbc_status
                if odbc_groups is not None:
                    used_odbc = True
                    if shadow_mode:
                        groups = fetch_groups_xml()
                        mismatch = compare_section_rows("groups", groups, odbc_groups)
                        if mismatch:
                            print(f"[ODBC][Shadow] {mismatch}")
                        section_sources["groups"] = "xml"
                    else:
                        groups = odbc_groups
                        section_sources["groups"] = "odbc"
            if groups is None:
                groups = fetch_groups_xml()
                section_sources["groups"] = "xml"
            if used_odbc and shadow_mode:
                print("[ODBC][Shadow] Groups compared against XML; XML remains authoritative.")
            record_updates["groups"] = len(groups)
            print(f"[Tally] Got {len(groups)} groups")

        if sync_plan.get("need_ledgers"):
            print("[Tally] Fetching ledgers...")
            used_odbc = False
            if transport_mode in {"auto", "hybrid"} and odbc_bridge:
                odbc_ledgers, odbc_result = maybe_fetch_section_via_odbc(odbc_bridge, "ledgers")
                odbc_status = merge_odbc_status(odbc_status, odbc_result) or odbc_status
                if odbc_ledgers is not None:
                    used_odbc = True
                    if shadow_mode:
                        ledgers = fetch_ledgers_xml()
                        mismatch = compare_section_rows("ledgers", ledgers, odbc_ledgers)
                        if mismatch:
                            print(f"[ODBC][Shadow] {mismatch}")
                        section_sources["ledgers"] = "xml"
                    else:
                        ledgers = odbc_ledgers
                        section_sources["ledgers"] = "odbc"
            if ledgers is None:
                ledgers = fetch_ledgers_xml()
                section_sources["ledgers"] = "xml"
            if used_odbc and shadow_mode:
                print("[ODBC][Shadow] Ledgers compared against XML; XML remains authoritative.")
            record_updates["ledgers"] = len(ledgers)
            print(f"[Tally] Got {len(ledgers)} ledgers")

        if sync_plan.get("need_vouchers"):
            voucher_from_date = sync_plan.get("voucher_from_date", from_date)
            voucher_to_date = sync_plan.get("voucher_to_date", to_date)
            prefer_day_book_vouchers = product_info.get("product_name") == "Tally.ERP 9"
            vouchers = fetch_vouchers_with_batches(
                voucher_from_date,
                voucher_to_date,
                sync_plan.get("voucher_sync_mode", "full"),
                prefer_day_book=prefer_day_book_vouchers,
            )
            section_sources["vouchers"] = "xml"
            record_updates["vouchers"] = len(vouchers)
            print(f"[Tally] Got {len(vouchers)} vouchers")

        if sync_plan.get("need_stock"):
            print("[Tally] Fetching stock items...")
            used_odbc = False
            if transport_mode in {"auto", "hybrid"} and odbc_bridge:
                odbc_stock, odbc_result = maybe_fetch_section_via_odbc(odbc_bridge, "stock_items")
                odbc_status = merge_odbc_status(odbc_status, odbc_result) or odbc_status
                if odbc_stock is not None:
                    used_odbc = True
                    if shadow_mode:
                        stock = fetch_stock_xml()
                        mismatch = compare_section_rows("stock_items", stock, odbc_stock)
                        if mismatch:
                            print(f"[ODBC][Shadow] {mismatch}")
                        section_sources["stock_items"] = "xml"
                    else:
                        stock = odbc_stock
                        section_sources["stock_items"] = "odbc"
            if stock is None:
                stock = fetch_stock_xml()
                section_sources["stock_items"] = "xml"
            if used_odbc and shadow_mode:
                print("[ODBC][Shadow] Stock compared against XML; XML remains authoritative.")
            record_updates["stock"] = len(stock)

        if sync_plan.get("need_outstanding"):
            print("[Tally] Fetching outstanding...")
            try:
                outstanding = (
                    fetch_structured_section("outstanding_receivables")
                    + fetch_structured_section("outstanding_payables")
                )
                print("[Tally] Outstanding loaded via definition-driven report parsing")
            except Exception as structured_error:
                print(
                    f"[Tally] Structured outstanding fetch failed ({structured_error}) - "
                    "falling back to legacy parser"
                )
                outstanding = (
                    parse_outstanding(get_outstanding_receivables(), "receivable")
                    + parse_outstanding(get_outstanding_payables(), "payable")
                )
            section_sources["outstanding"] = "xml"
            record_updates["outstanding"] = len(outstanding)
            print(f"[Tally] Got {len(outstanding)} outstanding entries")

        if sync_plan.get("need_reports"):
            print("[Tally] Fetching Profit & Loss...")
            try:
                profit_loss = fetch_structured_section(
                    "profit_loss",
                    {"from_date": from_date, "to_date": to_date},
                )
                if not profit_loss:
                    raise ValueError("structured profit and loss report returned no rows")
                print("[Tally] Profit & Loss loaded via definition-driven report parsing")
            except Exception as structured_error:
                print(
                    f"[Tally] Structured Profit & Loss fetch failed ({structured_error}) - "
                    "falling back to legacy parser"
                )
                profit_loss = parse_profit_and_loss(get_profit_and_loss(from_date, to_date))
            section_sources["profit_loss"] = "xml"
            record_updates["profit_loss"] = len(profit_loss)
            print(f"[Tally] Got {len(profit_loss)} P&L line items")

            print("[Tally] Fetching Balance Sheet...")
            try:
                balance_sheet = fetch_structured_section(
                    "balance_sheet",
                    {"from_date": from_date, "to_date": to_date},
                )
                if not balance_sheet:
                    raise ValueError("structured balance sheet report returned no rows")
                print("[Tally] Balance Sheet loaded via definition-driven report parsing")
            except Exception as structured_error:
                print(
                    f"[Tally] Structured Balance Sheet fetch failed ({structured_error}) - "
                    "falling back to legacy parser"
                )
                balance_sheet = parse_balance_sheet(get_balance_sheet(from_date, to_date))
            section_sources["balance_sheet"] = "xml"
            record_updates["balance_sheet"] = len(balance_sheet)
            print(f"[Tally] Got {len(balance_sheet)} Balance Sheet items")

            print("[Tally] Fetching Trial Balance...")
            try:
                trial_balance = fetch_structured_section(
                    "trial_balance",
                    {"from_date": from_date, "to_date": to_date},
                )
                if not trial_balance:
                    raise ValueError("structured trial balance report returned no rows")
                print("[Tally] Trial Balance loaded via definition-driven report parsing")
            except Exception as structured_error:
                print(
                    f"[Tally] Structured Trial Balance fetch failed ({structured_error}) - "
                    "falling back to legacy parser"
                )
                trial_balance = parse_trial_balance(get_trial_balance(from_date, to_date))
            section_sources["trial_balance"] = "xml"
            record_updates["trial_balance"] = len(trial_balance)
            print(f"[Tally] Got {len(trial_balance)} Trial Balance items")

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
                "effective_from_date": from_date,
                "effective_to_date": to_date,
                "date_range_source": date_range_source,
                "master_changed": sync_plan.get("master_changed", True),
                "voucher_changed": sync_plan.get("voucher_changed", True),
                "section_sources": section_sources,
                "product_name": product_info.get("product_name"),
                "product_version": product_info.get("product_version"),
                "odbc_status": odbc_status,
            },
        }

        if not push(payload):
            raise RuntimeError("Cloud push failed")

        if current_ids:
            if save_cached_ids(current_ids):
                print("[TallyBridge] Cached alter IDs for next change detection.")
            else:
                print("[TallyBridge] Alter ID cache could not be updated; next sync may re-fetch more data.")

        print(json.dumps({
            "status": "success",
            "records": record_updates,
            "voucher_sync_mode": sync_plan.get("voucher_sync_mode", "full"),
            "sync_meta": payload["sync_meta"],
        }))
        return 0
    except Exception as error:
        print(f"[Error] Sync failed: {error}", file=sys.stderr)
        return 1
    finally:
        if odbc_bridge:
            odbc_bridge.close()


if __name__ == "__main__":
    sys.exit(main())
