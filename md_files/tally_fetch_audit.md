# TallyBridge - Tally Data Fetching Audit

> Audit date: 2026-04-09
> Branch: `codex/erp9-sync` (commit `f39d76a`)
> Scope: Python worker fetching pipeline, backend upsert, cloud push

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Bugs](#2-bugs)
3. [Missing Failsafes](#3-missing-failsafes)
4. [Inefficiencies](#4-inefficiencies)
5. [Data Integrity Risks](#5-data-integrity-risks)
6. [Recommendations (Priority Order)](#6-recommendations-priority-order)

---

## 1. Architecture Overview

```
Tally ERP 9 / TallyPrime
       |
       |--- XML/HTTP (port 9000) ---> tally_client.py ---> sync_main.py
       |--- ODBC (PowerShell)    ---> odbc_bridge.py  --->     |
                                                               v
                                                       cloud_pusher.py
                                                               |
                                                        HTTP POST /api/sync
                                                               |
                                                    backend/routes/sync.ts
                                                               |
                                                          Supabase DB
```

**ERP 9 Two-Pass Voucher Pipeline:**
```
Pass 1: Header-only TDL collection (no .LIST fields)
        -> DateRangeFilter with ##SVFromDate/##SVToDate
        -> Returns: GUID, MasterID, AlterID, Date, Amount, etc.
        -> ~lightweight, avoids ERP 9 XML server hang

Pass 2: Batched detail fetch by MasterID (25 per batch)
        -> Full voucher data with LEDGERENTRIES.LIST, ALLINVENTORYENTRIES.LIST, BILLALLOCATIONS.LIST
        -> Filtered by MasterIdFilter formula

Merge: GUID-based join of headers + details
```

---

## 2. Bugs

### BUG-1: No retry on ERP 9 detail batch failure (HIGH)
**File:** `sync_main.py:702-712`

If any batch of 25 MasterIDs times out during Pass 2, the entire `fetch_erp9_two_pass_vouchers()` function raises an exception. All prior successful batches are discarded. There is no per-batch retry.

```python
# Line 707-712: Exception on batch 5/10 loses batches 1-4
for batch_index, batch_master_ids in enumerate(batches, start=1):
    detail_rows.extend(
        parse_structured_section(
            "vouchers",
            get_voucher_details_erp9_batch(batch_master_ids),  # <-- can raise
        )
    )
```

**Impact:** A single slow batch kills the entire voucher sync, even if 90% of batches succeeded.

---

### BUG-2: Day Book fallback returns out-of-window vouchers (HIGH)
**File:** `sync_main.py:594-596`

If both Day Book request shapes return rows outside the requested date window, the first attempt's rows are returned anyway as a "best-effort" fallback.

```python
# Line 594-596
if fallback_rows is not None:
    print("[Tally] No Day Book request shape honored the requested window; using best-effort result.")
    return fallback_rows  # <-- these rows DON'T match the requested window
```

**Impact:** Out-of-window vouchers get merged with other monthly windows, causing duplicates or incorrect date associations.

---

### BUG-3: Unbounded recursion on persistent timeouts (HIGH)
**File:** `sync_main.py:766-801`

`fetch_recursive()` splits windows in half on timeout but has no depth limit. On a persistent Tally slowdown, it will recurse until reaching a single-day window (max ~31 levels for a monthly window), potentially deeper for multi-year ranges.

```python
# Line 766: depth parameter is tracked but never checked against a max
def fetch_recursive(window_from, window_to, depth=0):
    ...
    split_rows.extend(fetch_recursive(child_from, child_to, depth + 1))
```

**Impact:** Stack overflow on persistent Tally unresponsiveness; Python default recursion limit is 1000.

---

### BUG-4: `parse_voucher_headers` requires REMOTEID attribute (MEDIUM)
**File:** `xml_parser.py:385-389`

The header parser uses `re.findall` with pattern `<VOUCHER\b[^>]*REMOTEID=`. If ERP 9 returns `<VOUCHER>` tags without the `REMOTEID` attribute, those vouchers are silently dropped.

```python
voucher_blocks = re.findall(
    r"(<VOUCHER\b[^>]*REMOTEID=.*?</VOUCHER>)",  # <-- requires REMOTEID in tag
    cleaned, re.IGNORECASE | re.DOTALL,
)
```

**Impact:** Silent data loss if Tally omits `REMOTEID` attribute on some vouchers.

---

### BUG-5: Header values blindly overwrite detail values in merge (MEDIUM)
**File:** `sync_main.py:657-666`

During GUID-based merge, header fields always overwrite detail fields using `or` fallback. But `amount = 0` is falsy in Python, so a legitimate zero amount from the header would be replaced by the detail amount.

```python
# Line 664: if header amount is 0 (falsy), detail amount wins
merged_row["amount"] = abs(header.get("amount") or merged_row.get("amount", 0) or 0)
```

**Impact:** Amount discrepancies between header and detail views could produce incorrect values.

---

### BUG-6: Duplicate detail GUIDs silently dropped (MEDIUM)
**File:** `sync_main.py:624-631`

When building the detail lookup map, if Tally returns the same GUID in multiple detail rows (e.g., a voucher modified between batch fetches), only the first occurrence is kept.

```python
if tally_guid in detail_by_guid:
    duplicate_detail_guids += 1
    continue  # <-- second occurrence silently dropped
detail_by_guid[tally_guid] = detail
```

**Impact:** If the second occurrence is the correct/latest version, stale data is used.

---

### BUG-7: `is_retryable_voucher_error()` uses fragile string matching (LOW)
**File:** `sync_main.py:539-543`

Retryable error detection checks for substrings like `"timed out"`, `"connection"` in the error message. This is fragile and locale-dependent.

```python
return "timed out" in message or "unresponsive" in message or "connection" in message
```

**Impact:** Errors from different Tally versions or Python versions might not match, causing non-retryable treatment of transient errors.

---

## 3. Missing Failsafes

### FS-1: No retry on cloud push failure (CRITICAL)
**File:** `cloud_pusher.py:58-106`, `sync_main.py:1326-1327`

After spending 20+ minutes fetching data from Tally, a single transient network error to the backend causes the entire sync to fail with no retry.

```python
# cloud_pusher.py:100-106
except requests.exceptions.ConnectionError:
    print(f"[Cloud] Cannot reach backend at {BACKEND_URL}")
    return False  # <-- no retry

# sync_main.py:1326-1327
if not push(payload):
    raise RuntimeError("Cloud push failed")  # <-- entire sync fails
```

**Fix needed:** Retry with exponential backoff (e.g., 3 attempts: 2s, 5s, 15s).

---

### FS-2: No transaction isolation on backend upsert (HIGH)
**File:** `backend/src/routes/sync.ts`

Voucher items and ledger entries are inserted in separate database calls with no wrapping transaction. If ledger entry insert fails, voucher items are already committed.

**Fix needed:** Wrap voucher graph upsert (voucher + items + ledger entries) in a database transaction.

---

### FS-3: No progress saving on multi-section sync (HIGH)
**File:** `sync_main.py:987-1355`

If groups, ledgers, and vouchers fetch successfully but stock fetch fails, all fetched data is lost. The sync is all-or-nothing.

**Fix needed:** Push each section incrementally, or cache fetched data to disk so a retry can skip completed sections.

---

### FS-4: No validation that detail batch is complete (MEDIUM)
**File:** `sync_main.py:713-716`

After Pass 2, the code prints the detail row count but never checks whether the number of detail rows matches the number of unique MasterIDs requested. Missing details are silently filled with empty placeholder data.

```python
# Line 713: detail count could be less than master_id count -- no check
print(f"[Tally] ERP 9 detail pass returned {len(detail_rows)} voucher row(s)")
```

**Fix needed:** Warn or fail if detail_rows count differs significantly from master_ids count.

---

### FS-5: No timeout on ODBC subprocess (MEDIUM)
**File:** `odbc_bridge.py`

The PowerShell ODBC helper has no subprocess timeout. If it hangs, the sync blocks indefinitely.

**Fix needed:** Add `subprocess.communicate(timeout=...)` or similar watchdog.

---

### FS-6: Alter ID cache race condition (LOW)
**File:** `sync_main.py:113-157`

Multiple sync processes running simultaneously can corrupt the `.alter_ids_cache.json` file. The temp-file + rename approach helps but doesn't use file locking.

**Fix needed:** Use `fcntl.flock()` (Unix) or `msvcrt.locking()` (Windows) for file-level locks.

---

### FS-7: No max age check on cached alter IDs (LOW)
**File:** `sync_main.py:92-111`

Cached alter IDs are used regardless of age. A cache from weeks ago would still be treated as valid, potentially skipping a needed full sync.

---

## 4. Inefficiencies

### EFF-1: 96 monthly windows for a single-year company (HIGH)
**File:** `sync_main.py`

When `TB_SYNC_FROM_DATE=2018-04-01` and `TB_SYNC_TO_DATE=2026-04-08`, the pipeline creates 96 monthly windows even though K.V. ENTERPRISES only has data for FY 2018-19 (12 months). The remaining 84 months return empty results from Tally but each incurs a full HTTP round-trip (up to 75s timeout).

**Fix:** Use `books_from`/`books_to` from company info to clamp the date range before building windows. The `clamp_range_to_company_books()` function exists (line 232) but only clamps when override dates exceed company range -- it doesn't override a wide manual range.

---

### EFF-2: Sequential detail batch fetching (MEDIUM)
**File:** `sync_main.py:702-712`

Detail batches (25 MasterIDs each) are fetched sequentially. For 20,000 vouchers, this means ~800 sequential HTTP requests.

**Fix:** Could use `concurrent.futures.ThreadPoolExecutor` to fetch 2-3 batches in parallel (limited to avoid overloading Tally's XML server).

---

### EFF-3: Full sort for deduplication (LOW)
**File:** `sync_main.py:424-443`

`dedupe_vouchers()` sorts the entire voucher list O(n log n) before deduping, when a simple dict-based approach would be O(n).

```python
# Current: O(n log n) sort + O(n) scan
for voucher in sorted(vouchers, key=lambda row: (...)):

# Better: O(n) dict
seen = {}
for v in vouchers:
    guid = v.get("tally_guid")
    if guid not in seen:
        seen[guid] = v
```

---

### EFF-4: Double XML parsing in structured fallback chain (LOW)
**File:** `sync_main.py:863-888`

If the structured stock collection succeeds but returns sparse data (no closing values), the code falls through to `get_stock_summary_report()` which fetches from Tally again. The first successful fetch's data is discarded entirely.

---

## 5. Data Integrity Risks

### INT-1: Missing vouchers with empty line items silently accepted
When Pass 2 fails to return details for a header, the merge creates a stub voucher with empty `items` and `ledger_entries` arrays. These stub vouchers are pushed to Supabase as if they are complete -- there is no flag indicating they are header-only.

**Risk:** Downstream reports will show vouchers with correct totals but no line-item breakdowns.

---

### INT-2: No idempotency guarantee across sync reruns
If a sync crashes after cloud push succeeds but before alter IDs are cached, the next run will re-push all data. The backend upsert is keyed on `tally_guid` (for vouchers) which makes it safe for vouchers, but snapshot tables (P&L, Balance Sheet, Trial Balance) may get duplicate rows.

---

### INT-3: ERP 9 MasterID=0 vouchers silently excluded
**File:** `sync_main.py:690`

```python
if not master_id or master_id in seen_master_ids:
    continue
```

If a voucher has `MasterID=0` (which is falsy), it's silently skipped from the detail fetch. The header row still exists, so it becomes a stub voucher with empty line items.

---

### INT-4: No checksum or count verification after push
After pushing data to the backend, the response includes record counts but these aren't verified against what was sent. If the backend silently drops rows, the sync reports success.

---

## 6. Recommendations (Priority Order)

| Priority | Issue | Effort | Description |
|----------|-------|--------|-------------|
| P0 | FS-1 | Small | Add retry with backoff to `cloud_pusher.push()` |
| P0 | BUG-1 | Small | Add per-batch retry (2 attempts) in `fetch_erp9_two_pass_vouchers()` |
| P1 | BUG-3 | Small | Add `MAX_SPLIT_DEPTH = 10` guard in `fetch_recursive()` |
| P1 | FS-3 | Medium | Cache fetched sections to disk; push incrementally |
| P1 | EFF-1 | Small | Clamp date range to company books before building monthly windows |
| P2 | BUG-2 | Small | Filter out-of-window rows before returning from Day Book fallback |
| P2 | FS-2 | Medium | Wrap backend upsert in Supabase transaction |
| P2 | FS-4 | Small | Add detail-vs-header count check with warning threshold |
| P2 | INT-3 | Small | Handle MasterID=0 explicitly (log warning, include in detail fetch) |
| P3 | BUG-5 | Small | Use explicit None check instead of `or` for amount merge |
| P3 | EFF-2 | Medium | Parallelize detail batch fetches (2-3 concurrent) |
| P3 | FS-5 | Small | Add subprocess timeout to ODBC bridge |
| P3 | BUG-4 | Small | Broaden header regex to not require REMOTEID attribute |

---

## Files Referenced

| File | Role |
|------|------|
| `src/python/sync_main.py` | Main orchestrator, pipeline logic, retry/fallback |
| `src/python/tally_client.py` | HTTP transport to Tally XML server |
| `src/python/xml_parser.py` | XML response parsing (vouchers, headers, etc.) |
| `src/python/cloud_pusher.py` | HTTP push to backend |
| `src/python/odbc_bridge.py` | ODBC/PowerShell data fetch |
| `src/python/definition_extractor.py` | Structured section extraction |
| `backend/src/routes/sync.ts` | Backend upsert to Supabase |
