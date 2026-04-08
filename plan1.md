# Plan Review: Version-Aware Hybrid Tally Read Support

## Review of Plan.md against the actual codebase

After reading every source file in the project, here is my assessment of the plan's assumptions, gaps, risks, and suggested changes.

---

## What the plan gets right

**1. XML must stay canonical.** The entire sync pipeline (`tally_client.py`, `definition_extractor.py`, `xml_parser.py`) is XML-native. Every section already has both a legacy XML parser and a definition-driven XML parser with automatic fallback. This is well-designed and stable. ODBC should layer on top, not replace it.

**2. Vouchers should not move to ODBC in v1.** The voucher pipeline is the most complex part of the system: parent voucher + inventory items + ledger entries + bill allocations, with GUID-based identity, incremental sync by date window, and a reconciliation step on the backend that deletes stale voucher graphs (`deleteVoucherGraph`). Moving this to ODBC would require reimplementing all of that relational extraction. The plan correctly defers this.

**3. The x86 ODBC helper process is the right architecture.** The current Python runtime is 64-bit (`py -3` on Windows). ERP 9 6.6.3 ships `regodbc32.exe`, which registers a 32-bit ODBC driver. A separate x86 subprocess avoids bitness mismatch and keeps the main sync engine clean.

**4. Capability-based detection over version hardcoding.** The plan's `auto`/`xml-only`/`hybrid` readMode with runtime probing is sound. The codebase already demonstrates this pattern with the `definition_extractor.py` fallback chain.

---

## Issues and gaps

### Issue 1: The plan underestimates the existing fallback infrastructure

The codebase already has a sophisticated three-tier fallback system that the plan doesn't acknowledge:

1. **Definition-driven collection** (`fetch_structured_section`) via `structured_sections.json`
2. **Legacy XML parser** (e.g., `parse_groups(get_groups())`)
3. **Alternative report format** (e.g., Stock Summary fallback when StockItem collection is sparse)

Adding ODBC creates a **fourth tier**. The plan should specify exactly where ODBC fits in the existing fallback chain. My recommendation:

```
ODBC probe passes? -> Try ODBC
  ODBC fails?      -> Try definition-driven XML (existing tier 1)
    XML fails?     -> Try legacy XML parser (existing tier 2)
      Fails?       -> Try alternative report (existing tier 3, stock only)
```

This means ODBC is a prefix to the existing chain, not a replacement. The `main.py` section-fetching code (lines 382-511) already has this try/except structure; ODBC would wrap around it.

### Issue 2: The plan doesn't address the definition_extractor.py architecture

The plan says "Introduce a section-level read router in the Python sync path." But there's already a read router: `definition_extractor.py` uses `structured_sections.json` to drive XML requests declaratively. The ODBC helper is analogous to an alternative transport for the same section definitions.

**Recommendation:** Don't create a separate "read router." Instead, extend the existing section-level dispatch in `main.py` with an ODBC-first path for eligible sections. The `fetch_structured_section()` function could gain a sibling `fetch_odbc_section()`, and the dispatch logic in `main.py` would try ODBC first when the capability probe says it's available.

### Issue 3: The ODBC helper IPC contract needs more specifics

The plan says "stdin JSON request / stdout JSON response." This is fine but needs:

- **Error protocol:** How does the helper report partial failures vs total failures? A section that returns 0 rows might be an empty result or a query error. The existing XML path distinguishes these (empty collection vs exception).
- **Timeout handling:** The current `tally_client.py` uses `timeout=30` on HTTP requests. The ODBC helper needs its own timeout. ERP 9 ODBC queries can hang on large datasets just like XML can.
- **Encoding:** Tally ODBC returns data in the system's ANSI codepage on ERP 9. The helper must handle codepage-to-UTF-8 conversion, which the plan doesn't mention. This matters for Indian company names with special characters.
- **Connection pooling:** Should the helper stay alive between sections in a single sync run, or be spawned per-query? Staying alive avoids DSN resolution overhead on every query. I'd recommend a single helper process per sync run with multiple request/response exchanges over stdin/stdout.

### Issue 4: Date-window voucher batching is more complex than described

The plan says "default full-FY sync is split into monthly windows." But the current code already supports incremental voucher sync with overlap (`VOUCHER_OVERLAP_DAYS = 7`, lines 48-49 of `main.py`) and has a sync plan builder that determines `voucher_from_date`/`voucher_to_date`. The batching needs to work within this framework:

- When `voucher_sync_mode` is "incremental", the date range is already narrowed. Batching should still apply within that range.
- When `voucher_sync_mode` is "full", batch the full FY into months.
- The `from_date`/`to_date` passed to `get_vouchers()` and `fetch_structured_section("vouchers", {...})` need to be iterated over windows.
- Deduplication by `tally_guid` must happen client-side before push, since overlapping windows can return the same voucher.
- The backend's `reconcileVoucherScope` already handles stale-row cleanup, but it operates on the full incoming set. The batched results must be merged into a single list before push.

**Recommendation:** Implement batching as a wrapper around the existing voucher fetch, not inside `tally_client.py` or `definition_extractor.py`. Something like:

```python
def fetch_vouchers_batched(from_date, to_date, window_months=1):
    all_vouchers = {}
    for window_start, window_end in generate_windows(from_date, to_date, window_months):
        try:
            batch = fetch_vouchers(window_start, window_end, timeout=per_window_timeout)
            for v in batch:
                all_vouchers[v["tally_guid"]] = v  # dedup by GUID
        except TimeoutError:
            # split this window in half and retry
            ...
    return list(all_vouchers.values())
```

### Issue 5: The sync_meta extensions need coordination with the backend

The plan adds `section_sources`, `product_name`, `product_version`, `odbc_status` to `sync_meta`. But the backend's `normalizeSyncMeta()` function (sync.ts:50-63) currently only accepts `voucher_sync_mode`, `voucher_from_date`, `voucher_to_date`, `master_changed`, `voucher_changed`. 

These new fields would be silently dropped unless the backend is updated. The plan says "no schema migration required," but:
- The `sync_log` table stores `records_synced` as a JSON column. `sync_meta` metadata could go there.
- If you want to query "which syncs used ODBC for groups" later, you need to persist it somewhere.

**Recommendation:** Add the new `sync_meta` fields as passthrough JSON in the sync_log insert, not as new database columns. This avoids migration while preserving audit trail:

```typescript
await supabase.from("sync_log").insert({
  company_id: companyId,
  status: "success",
  records_synced: records,
  sync_meta: normalizedSyncMeta,  // add this
});
```

### Issue 6: Missing error-surface design for the Electron UI

The plan says "Add ODBC diagnostics to desktop settings and sync logs." But the current UI has no settings page for ODBC and the sync log is just a streaming text view (`sync-log` IPC events from `sync-engine.ts`). The plan should specify:

- Does the `check-tally-capabilities` IPC handler go in `ipc-handlers.ts` alongside the existing `check-tally` handler?
- What does the Settings page actually show? A read-only diagnostic panel, or editable readMode/DSN fields?
- How do log lines from the ODBC helper appear in the sync log? Currently, the sync engine forwards all stdout lines to the renderer. ODBC helper output would need to be captured and forwarded similarly.

**Recommendation:** Start minimal:
- Add `readMode` to the existing `AppConfig` in `store.ts` (default: `"auto"`).
- Surface ODBC status as additional sync-log lines (the existing mechanism handles this for free).
- Add `check-tally-capabilities` as a new IPC handler that runs the ODBC probe and returns JSON.
- Defer a dedicated Settings UI panel to v2.

### Issue 7: The plan doesn't account for the two XML request patterns

The codebase has two distinct XML request patterns:
1. **Collection+FETCH** (TDL-based): Used for groups, ledgers, stock items, vouchers, company info. These go through `definition_extractor.py`.
2. **Data export** (report-based): Used for outstanding, P&L, balance sheet, trial balance, Day Book. These use regex-based or indexed-row parsing.

ODBC can only replace pattern 1 sections, because pattern 2 sections return Tally's own report layout (with display columns like `DSPDISPNAME`, `BSMAINAMT`), not raw object data. The plan's section split aligns with this, but it should be explicit about *why* reports can't come from ODBC.

### Issue 8: Incremental sync interaction with ODBC

The change-detection system (`alter_ids`) drives the sync plan. If ODBC is used for groups/ledgers/stock, the `master_changed` flag should still gate those fetches. The plan doesn't discuss whether ODBC fetches should respect the sync plan or always run.

**Recommendation:** ODBC fetches should respect the existing sync plan. If `need_groups` is false, don't query ODBC for groups. This avoids unnecessary load on ERP 9 and keeps behavior consistent regardless of transport.

---

## Implementation priority order

Based on the codebase state and the plan's goals, I'd sequence the work as:

### Phase 1: XML voucher batching (highest value, zero ODBC dependency)
This is the plan's Section 4. It unblocks ERP 9 reliability immediately.
- Add `fetch_vouchers_batched()` wrapper in `main.py`
- Add configurable timeout per window in `tally_client.py` (currently hardcoded at 30s)
- Split-and-retry on timeout
- Deduplicate by `tally_guid` before push
- Test with ERP 9 6.6.3

### Phase 2: ODBC helper process (foundation for hybrid)
This is the plan's Section 2.
- Build a standalone Python x86 script (`odbc_helper.py`) that reads JSON from stdin and writes JSON to stdout
- Implement `probe` and `query` commands
- Handle DSN resolution, codepage conversion, connection timeouts
- Test independently against ERP 9 ODBC

### Phase 3: Transport routing in main.py (wiring it together)
This is the plan's Sections 1 and 5.
- Add `readMode` to `AppConfig` in `store.ts`
- Pass `readMode` as env var to the Python process
- In `main.py`, add ODBC-first dispatch for groups/ledgers/stock when probe passes
- Fall back to existing XML path on any ODBC failure
- Log transport decisions as sync-log lines

### Phase 4: Diagnostics and UI (polish)
This is the plan's Sections 3 and 6.
- Add `check-tally-capabilities` IPC handler
- Extend `sync_meta` with `section_sources` and pass through to backend sync_log
- Surface ODBC status in the Settings page

---

## Risks to watch

1. **ERP 9 ODBC driver stability.** Tally ERP 9's ODBC driver is notoriously fragile. It may crash on large result sets, return truncated data, or fail silently. The probe must be conservative, and fallback must be immediate.

2. **32-bit Python availability.** The plan assumes a 32-bit Python runtime is available or bundled. In production, TallyBridge uses a bundled `tallybridge-engine.exe` (see `sync-engine.ts:112`). A separate x86 exe would need its own build pipeline.

3. **Sync atomicity.** Currently, the entire sync is one payload push to the backend. If ODBC provides groups but XML provides ledgers, the data is still consistent because it's all sent in one `push()` call. This is fine. But if ODBC adds per-section latency (probe + query), the total sync duration may increase, which interacts with the process timeout (`maxDurationMs` in `sync-engine.ts:136`).

4. **The `structured_sections.json` pattern.** This file defines both the request XML and response parsing. ODBC doesn't use either of those. The ODBC path needs its own field-mapping configuration (ODBC column names to normalized output keys). Consider extending `structured_sections.json` with an optional `odbc` block per section.

---

## Summary

The plan is architecturally sound. The main corrections are:
- ODBC is a fourth fallback tier, not a replacement for the existing three-tier XML fallback
- Voucher batching should wrap the existing fetch, not restructure it
- The ODBC helper needs explicit error, timeout, and encoding contracts
- Backend `sync_meta` handling needs a small update to persist the new fields
- Implementation should be phased: XML batching first (immediate value), ODBC second (incremental value)
- Respect the existing sync plan (change detection) even when using ODBC
