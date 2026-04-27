# =============================================================================
# CHANGES — 2026-04-21 (branch: tallybridge-tallyprime-fix-attempt)
# Revert by undoing the 4 marked blocks below.
#
# CHANGE 1 (line ~1031): should_skip_voucher_family()
#   OLD: returned True for TallyPrime, skipping all voucher-family data
#   NEW: always returns False — TallyPrime now goes through the Day Book path
#
# CHANGE 2 (line ~723): fetch_voucher_window()
#   OLD: signature had no is_tallyprime param — tried TDL collection first (crashes TallyPrime)
#   NEW: added is_tallyprime=False — if True, skips TDL and goes directly to Day Book
#
# CHANGE 3 (line ~759): fetch_vouchers_with_batches()
#   OLD: signature had no is_tallyprime param
#   NEW: added is_tallyprime=False — passed through to fetch_voucher_window()
#
# CHANGE 4 (line ~1247): main() voucher fetch block
#   OLD: allow_day_book_fallback only True for ERP9; no is_tallyprime passed
#   NEW: allow_day_book_fallback also True for TallyPrime; is_tallyprime passed in
# =============================================================================
import json
import os
import sys
import threading
import time
from datetime import date, datetime, timedelta

from cloud_pusher import (
    fetch_pending_push_vouchers,
    fetch_remote_alter_ids,
    get_last_push_error,
    get_last_push_stats,
    mark_push_results,
    push,
)
from definition_extractor import fetch_structured_section, parse_structured_section
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
    get_voucher_details_erp9_batch,
    get_voucher_headers_erp9,
    get_vouchers,
    get_vouchers_collection_tdl,
    get_vouchers_legacy_data_request,
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
    parse_voucher_headers,
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
SYNC_TRIGGER = (os.environ.get("TB_SYNC_TRIGGER", "manual") or "manual").strip().lower()
if SYNC_TRIGGER not in {"startup", "manual", "heartbeat"}:
    SYNC_TRIGGER = "manual"
MANUAL_BACKFILL_PENDING = os.environ.get(
    "TB_MANUAL_BACKFILL_PENDING",
    "",
).strip().lower() in {"1", "true", "yes", "on"}
try:
    SYNC_HEARTBEAT_SECONDS = max(
        0,
        int(os.environ.get("TB_SYNC_HEARTBEAT_SECONDS", "20") or "20"),
    )
except ValueError:
    SYNC_HEARTBEAT_SECONDS = 20
ALLOW_DAYBOOK_FALLBACK = os.environ.get(
    "TB_ALLOW_DAYBOOK_FALLBACK",
    "",
).strip().lower() in {"1", "true", "yes", "on"}
ALLOW_TALLYPRIME_VOUCHER_XML = os.environ.get(
    "TB_ALLOW_TALLYPRIME_VOUCHER_XML",
    "",
).strip().lower() in {"1", "true", "yes", "on"}
ENABLE_PUSH = os.environ.get(
    "TB_ENABLE_PUSH",
    "",
).strip().lower() in {"1", "true", "yes", "on"}
COMMAND = (os.environ.get("TB_COMMAND", "sync") or "sync").strip().lower()
if COMMAND not in {"sync", "push_voucher", "poll_push_queue"}:
    COMMAND = "sync"
CONTROL_PLANE_URL = (
    os.environ.get("CONTROL_PLANE_URL", "").strip()
    or os.environ.get("BACKEND_URL", "").strip()
)
SYNC_INGEST_MODE = (os.environ.get("SYNC_INGEST_MODE", "render") or "render").strip().lower()
if SYNC_INGEST_MODE not in {"render", "hybrid", "direct"}:
    SYNC_INGEST_MODE = "render"
SYNC_INGEST_URL = (os.environ.get("SYNC_INGEST_URL", "") or "").strip()
try:
    SYNC_CONTRACT_VERSION = max(1, int(os.environ.get("SYNC_CONTRACT_VERSION", "1") or "1"))
except ValueError:
    SYNC_CONTRACT_VERSION = 1
VOUCHER_OVERLAP_DAYS = 7
ERP9_DETAIL_BATCH_SIZE = 25


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


def clamp_range_to_company_books(
    from_date: str,
    to_date: str,
    company_info: dict,
) -> tuple[str, str, bool]:
    books_from_iso = (company_info or {}).get("books_from")
    books_to_iso = (company_info or {}).get("books_to")
    books_from = fy_date_from_iso(books_from_iso) if books_from_iso else ""
    books_to = fy_date_from_iso(books_to_iso) if books_to_iso else ""

    from_obj = parse_tally_compact_date(from_date)
    to_obj = parse_tally_compact_date(to_date)
    if not from_obj or not to_obj:
        return from_date, to_date, False

    was_clamped = False
    if books_from:
        books_from_obj = parse_tally_compact_date(books_from)
        if books_from_obj and from_obj < books_from_obj:
            print(
                f"[TallyBridge] Clamping from_date {from_date} to company books start {books_from}."
            )
            from_date = books_from
            from_obj = books_from_obj
            was_clamped = True

    if books_to:
        books_to_obj = parse_tally_compact_date(books_to)
        if books_to_obj and to_obj > books_to_obj:
            print(
                f"[TallyBridge] Clamping to_date {to_date} to company books end {books_to}."
            )
            to_date = books_to
            to_obj = books_to_obj
            was_clamped = True

    if from_obj > to_obj:
        raise ValueError(
            f"Effective range after company bounds is invalid: {from_date}..{to_date}."
        )

    return from_date, to_date, was_clamped


def start_sync_heartbeat() -> tuple[threading.Event | None, threading.Thread | None]:
    if SYNC_HEARTBEAT_SECONDS <= 0:
        return None, None

    stop_event = threading.Event()

    def heartbeat_loop():
        while not stop_event.wait(SYNC_HEARTBEAT_SECONDS):
            print(
                f"[TallyBridge] Heartbeat: sync still running ({SYNC_HEARTBEAT_SECONDS}s interval).",
                flush=True,
            )

    thread = threading.Thread(target=heartbeat_loop, daemon=True)
    thread.start()
    return stop_event, thread


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


def validate_voucher_batch(
    vouchers: list[dict],
    from_date: str,
    to_date: str,
    voucher_source: str = "xml_collection",
) -> None:
    if not vouchers:
        print("[Tally] Voucher batch returned no rows")
        return

    total = len(vouchers)
    guid_counts: dict[str, int] = {}
    missing_guids = 0
    missing_dates = 0
    voucher_dates: list[date] = []

    for voucher in vouchers:
        tally_guid = (voucher.get("tally_guid") or "").strip()
        if tally_guid:
            guid_counts[tally_guid] = guid_counts.get(tally_guid, 0) + 1
        else:
            missing_guids += 1

        voucher_date = parse_iso_date(voucher.get("date") or "")
        if voucher_date:
            voucher_dates.append(voucher_date)
        else:
            missing_dates += 1

    if missing_guids:
        raise ValueError(
            f"Voucher batch is missing tally_guid on {missing_guids} row(s); "
            "aborting to avoid ambiguous voucher identity."
        )

    if missing_dates:
        raise ValueError(
            f"Voucher batch is missing date on {missing_dates} row(s); "
            "aborting to avoid corrupted date history."
        )

    duplicate_guid_count = sum(count - 1 for count in guid_counts.values() if count > 1)
    if duplicate_guid_count:
        duplicate_ratio = duplicate_guid_count / total
        if duplicate_guid_count > 3 or duplicate_ratio > 0.01:
            raise ValueError(
                f"Voucher batch returned {duplicate_guid_count} duplicate GUID row(s) "
                f"out of {total}; refusing sync because the voucher identity is unstable."
            )
        print(
            f"[Tally] Warning: voucher batch contained {duplicate_guid_count} duplicate "
            "GUID row(s) before merge."
        )

    if not voucher_dates:
        raise ValueError("Voucher batch did not contain any parseable voucher dates.")

    min_date = min(voucher_dates)
    max_date = max(voucher_dates)
    unique_date_count = len(set(voucher_dates))
    requested_start = parse_tally_compact_date(from_date)
    requested_end = parse_tally_compact_date(to_date)
    requested_span_days = (
        (requested_end - requested_start).days
        if requested_start and requested_end
        else 0
    )

    print(
        f"[Tally] Voucher date coverage: {min_date.isoformat()} to {max_date.isoformat()} "
        f"across {unique_date_count} distinct date(s)"
    )

    if (
        requested_span_days >= 30
        and total >= 25
        and unique_date_count == 1
    ):
        if voucher_source == "xml_daybook":
            print(
                "[Tally] Warning: Day Book export collapsed to a single voucher date "
                f"({min_date.isoformat()}) across a {requested_span_days + 1}-day request "
                "window. This ERP 9 build appears to ignore Day Book date filters; "
                "proceeding with the returned full voucher set."
            )
            return
        raise ValueError(
            "Voucher batch collapsed to a single voucher date "
            f"({min_date.isoformat()}) across a {requested_span_days + 1}-day request window; "
            "this usually means a report-style export was parsed as raw vouchers."
        )


def is_retryable_voucher_error(error: Exception) -> bool:
    if isinstance(error, (TallyTimeoutError, TallyConnectionError, TimeoutError)):
        return True
    message = str(error).lower()
    return "timed out" in message or "unresponsive" in message or "connection" in message


def fetch_day_book_voucher_window(from_date: str, to_date: str) -> list[dict]:
    print("[Tally] Using Day Book XML fallback for this voucher window")
    requested_start = parse_tally_compact_date(from_date)
    requested_end = parse_tally_compact_date(to_date)

    def rows_match_requested_window(rows: list[dict]) -> bool:
        if not rows or not requested_start or not requested_end:
            return True

        parsed_dates = [
            parse_iso_date(row.get("date") or "")
            for row in rows
            if row.get("date")
        ]
        if not parsed_dates:
            return False
        return all(
            requested_start <= voucher_date <= requested_end
            for voucher_date in parsed_dates
            if voucher_date
        )

    attempts = [
        ("report-style request", get_vouchers),
        ("legacy data request", get_vouchers_legacy_data_request),
    ]
    fallback_rows: list[dict] | None = None
    last_error: Exception | None = None

    for label, fetcher in attempts:
        try:
            print(f"[Tally] Day Book request shape: {label}")
            rows = parse_vouchers(fetcher(from_date, to_date))
            if not rows:
                print(f"[Tally] {label} returned no rows")
                return rows
            if rows_match_requested_window(rows):
                return rows
            print(
                f"[Tally] {label} ignored the requested date window "
                f"{from_date}..{to_date}; trying alternate request shape."
            )
            if fallback_rows is None:
                fallback_rows = rows
        except Exception as error:
            last_error = error
            print(f"[Tally] {label} failed ({error})")

    if fallback_rows is not None:
        print("[Tally] No Day Book request shape honored the requested window; using best-effort result.")
        return fallback_rows

    if last_error:
        raise last_error

    print("[Tally] Day Book fallback returned no rows")
    return []


def fetch_tdl_collection_voucher_window(from_date: str, to_date: str) -> list[dict]:
    print("[Tally] Using inline TDL voucher collection fallback for this voucher window")
    rows = parse_structured_section(
        "vouchers",
        get_vouchers_collection_tdl(from_date, to_date),
    )
    if rows:
        print("[Tally] Vouchers loaded via inline TDL voucher collection")
    else:
        print("[Tally] Inline TDL voucher collection returned no rows")
    return rows


def merge_voucher_headers_with_details(
    headers: list[dict],
    detail_rows: list[dict],
) -> list[dict]:
    detail_by_guid: dict[str, dict] = {}
    duplicate_detail_guids = 0
    for detail in detail_rows:
        tally_guid = (detail.get("tally_guid") or "").strip()
        if not tally_guid:
            continue
        if tally_guid in detail_by_guid:
            duplicate_detail_guids += 1
            continue
        detail_by_guid[tally_guid] = detail

    if duplicate_detail_guids:
        print(
            f"[Tally] Warning: ignored {duplicate_detail_guids} duplicate Day Book detail row(s) "
            "while building the ERP 9 voucher detail map."
        )

    merged: list[dict] = []
    missing_detail_count = 0
    for header in headers:
        tally_guid = (header.get("tally_guid") or "").strip()
        detail = detail_by_guid.get(tally_guid)
        if detail:
            merged_row = dict(detail)
        else:
            missing_detail_count += 1
            merged_row = {
                "tally_guid": tally_guid,
                "items": [],
                "ledger_entries": [],
                "narration": "",
                "is_invoice": False,
                "view": "",
            }

        merged_row["master_id"] = header.get("master_id", 0)
        merged_row["alter_id"] = header.get("alter_id", 0)
        merged_row["voucher_number"] = header.get("voucher_number") or merged_row.get("voucher_number", "")
        merged_row["voucher_type"] = header.get("voucher_type") or merged_row.get("voucher_type", "")
        merged_row["date"] = header.get("date") or merged_row.get("date")
        merged_row["party_name"] = header.get("party_name") or merged_row.get("party_name", "")
        merged_row["reference"] = header.get("reference") or merged_row.get("reference", "")
        merged_row["amount"] = abs(header.get("amount") or merged_row.get("amount", 0) or 0)
        merged_row["is_cancelled"] = bool(header.get("is_cancelled", merged_row.get("is_cancelled", False)))
        merged_row["is_optional"] = bool(header.get("is_optional", merged_row.get("is_optional", False)))
        merged_row.setdefault("items", [])
        merged_row.setdefault("ledger_entries", [])
        merged.append(merged_row)

    if missing_detail_count:
        print(
            f"[Tally] Warning: {missing_detail_count} ERP 9 voucher header row(s) had no matching "
            "Day Book detail row by GUID."
        )

    return merged


def fetch_erp9_two_pass_vouchers(from_date: str, to_date: str) -> list[dict]:
    print("[Tally] Using ERP 9 two-pass voucher sync (header collection + master-id detail batches)")
    header_rows = parse_voucher_headers(get_voucher_headers_erp9(from_date, to_date))
    print(f"[Tally] ERP 9 header pass returned {len(header_rows)} voucher row(s)")
    validate_voucher_batch(header_rows, from_date, to_date, voucher_source="erp9_headers")
    detail_rows: list[dict] = []
    master_ids: list[int] = []
    seen_master_ids: set[int] = set()
    for header in header_rows:
        master_id = header.get("master_id")
        if not master_id or master_id in seen_master_ids:
            continue
        seen_master_ids.add(master_id)
        master_ids.append(master_id)
    batches = [
        master_ids[index:index + ERP9_DETAIL_BATCH_SIZE]
        for index in range(0, len(master_ids), ERP9_DETAIL_BATCH_SIZE)
    ]
    print(
        f"[Tally] ERP 9 detail pass will fetch {len(master_ids)} voucher(s) "
        f"in {len(batches)} batch(es) of up to {ERP9_DETAIL_BATCH_SIZE}."
    )
    for batch_index, batch_master_ids in enumerate(batches, start=1):
        print(
            f"[Tally] ERP 9 detail batch {batch_index}/{len(batches)} "
            f"({len(batch_master_ids)} voucher id(s))"
        )
        detail_rows.extend(
            parse_structured_section(
                "vouchers",
                get_voucher_details_erp9_batch(batch_master_ids),
            )
        )
    print(f"[Tally] ERP 9 detail pass returned {len(detail_rows)} voucher row(s)")
    merged_rows = merge_voucher_headers_with_details(header_rows, detail_rows)
    print(f"[Tally] ERP 9 two-pass merge produced {len(merged_rows)} voucher row(s)")
    return merged_rows


def fetch_voucher_window(
    from_date: str,
    to_date: str,
    prefer_day_book: bool = False,
    allow_day_book_fallback: bool = False,
    is_tallyprime: bool = False,  # CHANGE 2 — new param
) -> tuple[list[dict], str]:
    if is_tallyprime:
        print(
            f"[Tally] TallyPrime detected — using voucher collection path without "
            f"SVCURRENTCOMPANY for {from_date}..{to_date}"
        )

    try:
        vouchers = fetch_structured_section(
            "vouchers",
            {"from_date": from_date, "to_date": to_date},
        )
        print("[Tally] Vouchers loaded via definition-driven collection")
        return vouchers, "xml_collection"
    except Exception as structured_error:
        print(
            f"[Tally] Structured voucher fetch failed for {from_date}..{to_date} "
            f"({structured_error}) - retrying with inline TDL voucher collection"
        )
        try:
            return fetch_tdl_collection_voucher_window(from_date, to_date), "xml_collection_tdl"
        except Exception as tdl_error:
            if allow_day_book_fallback:
                print(
                    f"[Tally] Inline TDL voucher collection failed for {from_date}..{to_date} "
                    f"({tdl_error}) - falling back to Day Book XML"
                )
                return fetch_day_book_voucher_window(from_date, to_date), "xml_daybook"

            raise RuntimeError(
                f"Structured voucher fetch failed for {from_date}..{to_date}: "
                f"{structured_error}. Inline TDL voucher collection also failed: "
                f"{tdl_error}. Day Book fallback is disabled because it can "
                "produce corrupted voucher dates and IDs."
            ) from tdl_error


def fetch_vouchers_with_batches(
    from_date: str,
    to_date: str,
    mode: str,
    prefer_day_book: bool = False,
    allow_day_book_fallback: bool = False,
    is_tallyprime: bool = False,  # CHANGE 3 — new param, passed through to fetch_voucher_window
) -> tuple[list[dict], str]:
    initial_windows = build_month_windows(from_date, to_date)
    all_vouchers: list[dict] = []
    transport_sources: set[str] = set()

    def fetch_recursive(window_from: str, window_to: str, depth: int = 0) -> list[dict]:
        indent = "  " * depth
        print(f"[Tally] Voucher window {window_from} to {window_to}")
        try:
            if prefer_day_book:
                rows = fetch_erp9_two_pass_vouchers(window_from, window_to)
                source = "erp9_two_pass"
            else:
                rows, source = fetch_voucher_window(
                    window_from,
                    window_to,
                    prefer_day_book=prefer_day_book,
                    allow_day_book_fallback=allow_day_book_fallback,
                    is_tallyprime=is_tallyprime,  # CHANGE 3 — pass through
                )
            transport_sources.add(source)
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
    print(
        f"[Tally] Voucher strategy: "
        f"{'ERP 9 two-pass: header collection -> master-id detail batches' if prefer_day_book else 'definition-driven collection -> inline TDL collection'}"
    )
    if prefer_day_book:
        print(
            "[Tally] Month-window batching is enabled on ERP 9 so header fetches stay light "
            "while detail rows are loaded by voucher master id."
        )
    for window_from, window_to in initial_windows:
        all_vouchers.extend(fetch_recursive(window_from, window_to))

    if transport_sources == {"xml_collection"}:
        voucher_source = "xml_collection"
    elif transport_sources == {"xml_collection_tdl"}:
        voucher_source = "xml_collection_tdl"
    elif transport_sources == {"erp9_two_pass"}:
        voucher_source = "erp9_two_pass"
    elif transport_sources == {"xml_daybook"}:
        voucher_source = "xml_daybook"
    elif transport_sources:
        voucher_source = "xml_mixed"
    else:
        voucher_source = "xml_collection"
    validate_voucher_batch(all_vouchers, from_date, to_date, voucher_source=voucher_source)
    deduped = dedupe_vouchers(all_vouchers)
    duplicates_removed = len(all_vouchers) - len(deduped)
    if duplicates_removed > 0:
        print(f"[Tally] Removed {duplicates_removed} duplicate vouchers after batch merge")
    return deduped, voucher_source


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
            books_from_date = parse_iso_date(books_from) if books_from else None
            books_to_date = parse_iso_date(books_to) if books_to else None
            last_voucher_date = None

            if books_from_date:
                try:
                    live_alter_ids = parse_alter_ids(get_company_alter_ids())
                    last_voucher_date = parse_iso_date(
                        live_alter_ids.get("last_voucher_date", "")
                    )
                except Exception as alter_error:
                    print(
                        f"[Tally] Could not read last voucher date for range detection "
                        f"({alter_error})"
                    )

                inferred_books_to = None
                if not books_to_date:
                    try:
                        inferred_books_to = (
                            books_from_date.replace(year=books_from_date.year + 1)
                            - timedelta(days=1)
                        )
                    except ValueError:
                        inferred_books_to = None

                candidate_end_dates = [
                    candidate
                    for candidate in (
                        books_to_date,
                        inferred_books_to,
                        last_voucher_date,
                    )
                    if candidate and candidate >= books_from_date
                ]
                effective_to = min(
                    max(candidate_end_dates) if candidate_end_dates else date.today(),
                    date.today(),
                )

                from_date = format_tally_compact(books_from_date)
                to_date = format_tally_compact(effective_to)

                if not books_to_date and last_voucher_date and last_voucher_date > (inferred_books_to or books_from_date):
                    company_info["books_to"] = effective_to.isoformat()
                    print(
                        f"[Tally] Company books end was not exposed; using last voucher date "
                        f"{effective_to.isoformat()} for full sync coverage."
                    )
                elif books_to_date and last_voucher_date and last_voucher_date > books_to_date:
                    company_info["books_to"] = effective_to.isoformat()
                    print(
                        f"[Tally] Company books end {books_to} lagged behind the latest "
                        f"voucher date {last_voucher_date.isoformat()}; syncing through "
                        f"{effective_to.isoformat()}."
                    )
                elif books_to_date:
                    print(f"[Tally] Company FY: {books_from} to {books_to}")
                else:
                    print(
                        f"[Tally] Company FY starts {books_from}; syncing through "
                        f"{effective_to.isoformat()}."
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


def should_skip_voucher_family(product_name: str | None) -> tuple[bool, str | None]:
    # CHANGE 1 — commented out TallyPrime skip; Day Book path now handles vouchers safely
    # if product_name == "TallyPrime" and not ALLOW_TALLYPRIME_VOUCHER_XML:
    #     return (
    #         True,
    #         "Skipping voucher, outstanding, and financial report XML export on TallyPrime "
    #         "to avoid the server crash seen on port 9000. Stable masters and stock sync "
    #         "will continue.",
    #     )

    return False, None


def run_pending_push_cycle(
    warnings: list[str],
    quiet_no_jobs: bool = False,
) -> None:
    # PUSH PHASE 1: outbound Tally import is optional and must never break the
    # already-stable inbound sync path.
    from tally_pusher import push_vouchers

    if not quiet_no_jobs:
        print("[Push] Checking backend queue for pending Sales/Purchase vouchers...")
    pending_jobs, status = fetch_pending_push_vouchers()
    if status != "ok":
        if quiet_no_jobs and status in {
            "backend_unconfigured",
            "company_identity_missing",
            "company_not_found",
            "company_not_ready",
        }:
            return
        warning = (
            "Outbound push queue check was skipped because the backend queue could not "
            f"be read ({status})."
        )
        warnings.append(warning)
        print(f"[Push] {warning}")
        return

    if not pending_jobs:
        if not quiet_no_jobs:
            print("[Push] No pending push jobs found.")
        return

    print(f"[Push] Found {len(pending_jobs)} pending job(s).")
    job_results: list[dict] = []
    for job in pending_jobs:
        job_id = str(job.get("id") or "").strip()
        voucher_payload = job.get("voucher_payload")
        if not job_id or not isinstance(voucher_payload, dict):
            print("[Push] Skipping malformed push job from backend queue.")
            continue

        voucher_label = (
            f"{voucher_payload.get('voucher_type', 'Voucher')} "
            f"{voucher_payload.get('voucher_number', '').strip()}".strip()
        )
        print(f"[Push] Importing {voucher_label or 'voucher'}...")

        try:
            result = push_vouchers([voucher_payload], company=COMPANY)
            status_value = "failed" if result.get("errors") else "pushed"
            error_message = "; ".join(result.get("line_errors") or []) or None
            if status_value == "failed":
                warning = (
                    f"Voucher push failed for job {job_id}: "
                    f"{error_message or 'unknown Tally import error'}"
                )
                warnings.append(warning)
                print(f"[Push] {warning}")
            else:
                print(
                    f"[Push] Imported successfully (created={result.get('created', 0)}, "
                    f"altered={result.get('altered', 0)})."
                )

            job_results.append({
                "id": job_id,
                "status": status_value,
                "error_message": error_message,
                "tally_response": result,
            })
        except Exception as error:
            warning = f"Voucher push failed for job {job_id}: {error}"
            warnings.append(warning)
            print(f"[Push] {warning}")
            job_results.append({
                "id": job_id,
                "status": "failed",
                "error_message": str(error),
                "tally_response": {
                    "created": 0,
                    "altered": 0,
                    "errors": 1,
                    "line_errors": [str(error)],
                },
            })

    if job_results and not mark_push_results(job_results):
        warning = "Outbound push results could not be stored in the backend queue."
        warnings.append(warning)
        print(f"[Push] {warning}")


def run_poll_push_queue_command() -> int:
    try:
        warnings: list[str] = []
        run_pending_push_cycle(warnings, quiet_no_jobs=True)
        return 0
    except Exception as error:
        print(f"[Push] Queue poll failed: {error}", file=sys.stderr)
        return 1


def run_single_push_command() -> int:
    # PUSH LOCAL API: reuse the existing engine entrypoint for one direct
    # voucher push so the local backend can hand work to TallyBridge cleanly.
    from tally_pusher import push_vouchers

    try:
        raw_payload = sys.stdin.read().strip()
        if not raw_payload:
            raise ValueError("No voucher payload was provided on stdin")

        payload = json.loads(raw_payload)
        if not isinstance(payload, dict):
            raise ValueError("Voucher payload must be a JSON object")

        company_name = str(
            payload.get("company_name")
            or payload.get("company")
            or COMPANY
            or ""
        ).strip()
        if not company_name:
            raise ValueError("company_name is required for direct push mode")

        result = push_vouchers([payload], company_name)
        ok = bool(result.get("created") or result.get("altered")) and not result.get("errors")
        print(json.dumps({
            "ok": ok,
            "company_name": company_name,
            **result,
        }))
        return 0 if ok else 1
    except Exception as error:
        print(json.dumps({
            "ok": False,
            "error": str(error),
        }))
        return 1


def estimate_payload_bytes(value) -> int:
    if value is None:
        return 0
    try:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
    except Exception:
        return 0


def build_section_metric(
    payload_value,
    started_at: float,
    source: str | None = None,
    status: str = "fetched",
    notes: str | None = None,
) -> dict[str, object]:
    row_count = len(payload_value) if isinstance(payload_value, list) else 0
    return {
        "status": status,
        "rows": row_count,
        "bytes": estimate_payload_bytes(payload_value),
        "fetch_duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
        "source": source or "",
        "notes": notes or "",
    }


def build_skipped_section_metric(reason: str) -> dict[str, object]:
    return {
        "status": "skipped",
        "rows": 0,
        "bytes": 0,
        "fetch_duration_ms": 0,
        "source": "",
        "notes": reason,
    }


def log_section_metric(section_name: str, metric: dict[str, object]) -> None:
    print(
        "[Metrics] "
        f"{section_name}: status={metric.get('status')} "
        f"rows={metric.get('rows', 0)} "
        f"bytes={metric.get('bytes', 0)} "
        f"fetch_ms={metric.get('fetch_duration_ms', 0)} "
        f"source={metric.get('source') or 'n/a'} "
        f"notes={metric.get('notes') or 'n/a'}"
    )


def build_payload_section_sizes(payload: dict) -> dict[str, int]:
    section_sizes: dict[str, int] = {}
    for section_name in (
        "company_info",
        "alter_ids",
        "groups",
        "ledgers",
        "vouchers",
        "stock_items",
        "outstanding",
        "profit_loss",
        "balance_sheet",
        "trial_balance",
    ):
        section_sizes[section_name] = estimate_payload_bytes(payload.get(section_name))
    return section_sizes


def main() -> int:
    if COMMAND == "push_voucher":
        return run_single_push_command()
    if COMMAND == "poll_push_queue":
        return run_poll_push_queue_command()

    total_sync_started_at = time.perf_counter()
    heartbeat_stop, heartbeat_thread = start_sync_heartbeat()
    print(f"[TallyBridge] Starting sync: {COMPANY}")
    print(f"[TallyBridge] Sync trigger: {SYNC_TRIGGER}")
    print(
        f"[TallyBridge] Control plane: {CONTROL_PLANE_URL or 'not configured'} | "
        f"Ingest mode: {SYNC_INGEST_MODE} | Contract v{SYNC_CONTRACT_VERSION}"
    )
    if SYNC_INGEST_MODE in {"hybrid", "direct"}:
        print(f"[TallyBridge] Direct ingest URL: {SYNC_INGEST_URL or 'not configured'}")
    product_info = detect_tally_product()
    if product_info.get("product_name"):
        version_suffix = f" {product_info['product_version']}" if product_info.get("product_version") else ""
        print(f"[Tally] Connected to {product_info['product_name']}{version_suffix}")

    transport_mode = "hybrid" if READ_MODE == "shadow" else READ_MODE
    shadow_mode = READ_MODE == "shadow"
    section_sources: dict[str, str] = {}
    section_metrics: dict[str, dict[str, object]] = {}
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
        from_date, to_date, was_clamped = clamp_range_to_company_books(
            from_date,
            to_date,
            company_info,
        )
        manual_range_override = date_range_source == "override"
        if manual_range_override:
            print(
                "[TallyBridge] Manual date range override active "
                f"({from_date} to {to_date})."
            )
        if was_clamped:
            print(
                "[TallyBridge] Date range was clamped to the selected company's books range."
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
            force_full_sync = FORCE_FULL_SYNC

            if force_full_sync:
                print("[TallyBridge] Forcing full sync because this company has not synced from this connector yet.")
            elif manual_range_override:
                print(
                    "[TallyBridge] Manual date range override will scope this run, "
                    "but it will not force heartbeat runs into full sync mode."
                )
            elif SYNC_TRIGGER == "heartbeat":
                print("[TallyBridge] Heartbeat mode: checking TallyPrime for changes before syncing.")

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
                    if SYNC_TRIGGER == "heartbeat":
                        print("[TallyBridge] Heartbeat found no changes - skipping sync.")
                    else:
                        print("[TallyBridge] No changes detected - skipping sync.")
                    print(json.dumps({
                        "status": "skipped",
                        "reason": "no_changes",
                        "records": {},
                        "sync_meta": {
                            "change_detection_mode": SYNC_TRIGGER,
                            "manual_backfill_pending": MANUAL_BACKFILL_PENDING,
                            "observability": {
                                "transport": {
                                    "control_plane_url": CONTROL_PLANE_URL,
                                    "ingest_mode": SYNC_INGEST_MODE,
                                    "ingest_url": SYNC_INGEST_URL if SYNC_INGEST_MODE in {"hybrid", "direct"} else "",
                                    "contract_version": SYNC_CONTRACT_VERSION,
                                },
                                "total_sync_ms": round((time.perf_counter() - total_sync_started_at) * 1000, 2),
                            },
                        },
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
        warnings: list[str] = []
        voucher_family_skipped = False
        skip_voucher_family, skip_voucher_reason = should_skip_voucher_family(
            product_info.get("product_name")
        )

        if skip_voucher_family and sync_plan.get("voucher_changed"):
            voucher_family_skipped = True
            warnings.append(skip_voucher_reason or "Voucher family skipped.")
            print(f"[TallyBridge] {skip_voucher_reason}")
            for section_name in (
                "vouchers",
                "outstanding",
                "profit_loss",
                "balance_sheet",
                "trial_balance",
            ):
                section_sources[section_name] = "skipped_tallyprime_safe_mode"
                section_metrics[section_name] = build_skipped_section_metric(
                    skip_voucher_reason or "voucher_family_skipped"
                )
                log_section_metric(section_name, section_metrics[section_name])

        if sync_plan.get("need_groups"):
            groups_started_at = time.perf_counter()
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
            section_metrics["groups"] = build_section_metric(
                groups,
                groups_started_at,
                source=section_sources.get("groups"),
            )
            log_section_metric("groups", section_metrics["groups"])
            print(f"[Tally] Got {len(groups)} groups")

        if sync_plan.get("need_ledgers"):
            ledgers_started_at = time.perf_counter()
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
            section_metrics["ledgers"] = build_section_metric(
                ledgers,
                ledgers_started_at,
                source=section_sources.get("ledgers"),
            )
            log_section_metric("ledgers", section_metrics["ledgers"])
            print(f"[Tally] Got {len(ledgers)} ledgers")

        if sync_plan.get("need_vouchers") and not voucher_family_skipped:
            vouchers_started_at = time.perf_counter()
            voucher_from_date = sync_plan.get("voucher_from_date", from_date)
            voucher_to_date = sync_plan.get("voucher_to_date", to_date)
            # CHANGE 4 — detect TallyPrime, enable Day Book fallback for it too
            is_tallyprime = product_info.get("product_name") == "TallyPrime"
            allow_day_book_fallback = (
                ALLOW_DAYBOOK_FALLBACK
                or product_info.get("product_name") == "Tally.ERP 9"
                or is_tallyprime  # CHANGE 4
            )
            prefer_day_book = product_info.get("product_name") == "Tally.ERP 9"
            try:
                vouchers, voucher_source = fetch_vouchers_with_batches(
                    voucher_from_date,
                    voucher_to_date,
                    sync_plan.get("voucher_sync_mode", "full"),
                    prefer_day_book=prefer_day_book,
                    allow_day_book_fallback=allow_day_book_fallback,
                    is_tallyprime=is_tallyprime,  # CHANGE 4
                )
                section_sources["vouchers"] = voucher_source
                record_updates["vouchers"] = len(vouchers)
                section_metrics["vouchers"] = build_section_metric(
                    vouchers,
                    vouchers_started_at,
                    source=section_sources.get("vouchers"),
                )
                log_section_metric("vouchers", section_metrics["vouchers"])
                print(f"[Tally] Got {len(vouchers)} vouchers")
            except Exception as voucher_error:
                voucher_family_skipped = True
                warning = (
                    "Voucher XML export became unstable and was skipped so the rest of the "
                    f"sync could finish safely: {voucher_error}"
                )
                warnings.append(warning)
                print(f"[TallyBridge] {warning}")
                section_sources["vouchers"] = "skipped_after_voucher_error"
                section_metrics["vouchers"] = build_skipped_section_metric(str(voucher_error))
                log_section_metric("vouchers", section_metrics["vouchers"])
                for section_name in ("outstanding", "profit_loss", "balance_sheet", "trial_balance"):
                    section_sources[section_name] = "skipped_after_voucher_error"
                    section_metrics[section_name] = build_skipped_section_metric("skipped_after_voucher_error")
                    log_section_metric(section_name, section_metrics[section_name])
                vouchers = None

        if sync_plan.get("need_stock"):
            stock_started_at = time.perf_counter()
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
            section_metrics["stock_items"] = build_section_metric(
                stock,
                stock_started_at,
                source=section_sources.get("stock_items"),
            )
            log_section_metric("stock_items", section_metrics["stock_items"])

        if sync_plan.get("need_outstanding") and not voucher_family_skipped:
            outstanding_started_at = time.perf_counter()
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
            section_metrics["outstanding"] = build_section_metric(
                outstanding,
                outstanding_started_at,
                source=section_sources.get("outstanding"),
            )
            log_section_metric("outstanding", section_metrics["outstanding"])
            print(f"[Tally] Got {len(outstanding)} outstanding entries")

        if sync_plan.get("need_reports") and not voucher_family_skipped:
            profit_loss_started_at = time.perf_counter()
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
            section_metrics["profit_loss"] = build_section_metric(
                profit_loss,
                profit_loss_started_at,
                source=section_sources.get("profit_loss"),
            )
            log_section_metric("profit_loss", section_metrics["profit_loss"])
            print(f"[Tally] Got {len(profit_loss)} P&L line items")

            balance_sheet_started_at = time.perf_counter()
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
            section_metrics["balance_sheet"] = build_section_metric(
                balance_sheet,
                balance_sheet_started_at,
                source=section_sources.get("balance_sheet"),
            )
            log_section_metric("balance_sheet", section_metrics["balance_sheet"])
            print(f"[Tally] Got {len(balance_sheet)} Balance Sheet items")

            trial_balance_started_at = time.perf_counter()
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
            section_metrics["trial_balance"] = build_section_metric(
                trial_balance,
                trial_balance_started_at,
                source=section_sources.get("trial_balance"),
            )
            log_section_metric("trial_balance", section_metrics["trial_balance"])
            print(f"[Tally] Got {len(trial_balance)} Trial Balance items")

        print("[Ingest] Preparing sync payload...")
        effective_voucher_sync_mode = (
            "none" if voucher_family_skipped else sync_plan.get("voucher_sync_mode", "full")
        )
        effective_voucher_changed = False if voucher_family_skipped else sync_plan.get(
            "voucher_changed", True
        )
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
                "voucher_sync_mode": effective_voucher_sync_mode,
                "voucher_from_date": sync_plan.get("voucher_from_date"),
                "voucher_to_date": sync_plan.get("voucher_to_date"),
                "effective_from_date": from_date,
                "effective_to_date": to_date,
                "date_range_source": date_range_source,
                "date_range_clamped": was_clamped,
                "change_detection_mode": SYNC_TRIGGER,
                "manual_backfill_pending": MANUAL_BACKFILL_PENDING,
                "master_changed": sync_plan.get("master_changed", True),
                "voucher_changed": effective_voucher_changed,
                "section_sources": section_sources,
                "product_name": product_info.get("product_name"),
                "product_version": product_info.get("product_version"),
                "odbc_status": odbc_status,
            },
        }
        payload_section_bytes = build_payload_section_sizes(payload)
        payload_total_bytes = estimate_payload_bytes(payload)
        total_extract_ms = round((time.perf_counter() - total_sync_started_at) * 1000, 2)
        payload["sync_meta"]["observability"] = {
            "transport": {
                "control_plane_url": CONTROL_PLANE_URL,
                "ingest_mode": SYNC_INGEST_MODE,
                "ingest_url": SYNC_INGEST_URL if SYNC_INGEST_MODE in {"hybrid", "direct"} else "",
                "contract_version": SYNC_CONTRACT_VERSION,
            },
            "sections": section_metrics,
            "payload_section_bytes": payload_section_bytes,
            "payload_total_bytes": payload_total_bytes,
            "extract_duration_ms": total_extract_ms,
        }
        print(
            "[Metrics] Payload total_bytes="
            f"{payload_total_bytes} "
            f"extract_ms={total_extract_ms} "
            f"sections={payload_section_bytes}"
        )

        if not push(payload):
            raise RuntimeError(get_last_push_error() or "Cloud push failed")
        upload_stats = get_last_push_stats()
        print(f"[Metrics] Upload stats: {upload_stats}")

        if current_ids and not voucher_family_skipped:
            if save_cached_ids(current_ids):
                print("[TallyBridge] Cached alter IDs for next change detection.")
            else:
                print("[TallyBridge] Alter ID cache could not be updated; next sync may re-fetch more data.")
        elif current_ids and voucher_family_skipped:
            print(
                "[TallyBridge] Skipped updating alter ID cache because voucher-family sync "
                "did not complete."
            )

        if ENABLE_PUSH:
            run_pending_push_cycle(warnings)

        total_sync_ms = round((time.perf_counter() - total_sync_started_at) * 1000, 2)
        print(json.dumps({
            "status": "success",
            "records": record_updates,
            "voucher_sync_mode": effective_voucher_sync_mode,
            "warnings": warnings,
            "sync_meta": payload["sync_meta"],
            "observability": {
                "sections": section_metrics,
                "payload_section_bytes": payload_section_bytes,
                "payload_total_bytes": payload_total_bytes,
                "upload": upload_stats,
                "total_sync_ms": total_sync_ms,
            },
        }))
        return 0
    except Exception as error:
        print(f"[Error] Sync failed: {error}", file=sys.stderr)
        return 1
    finally:
        if heartbeat_stop:
            heartbeat_stop.set()
        if heartbeat_thread:
            heartbeat_thread.join(timeout=1)
        if odbc_bridge:
            odbc_bridge.close()


if __name__ == "__main__":
    sys.exit(main())
