# Plan Review: Hybrid XML + ODBC Plan Against Codebase

## What the plan gets right

The plan is architecturally sound on the big decisions:

1. **XML as canonical, ODBC as optional accelerator** — correct. The entire pipeline (tally_client, definition_extractor, xml_parser) is XML-native.
2. **x86 helper process** — correct. ERP 9's 32-bit ODBC driver vs 64-bit Python runtime demands a subprocess.
3. **Vouchers staying on XML in v1** — correct. The voucher graph (parent + items + ledger entries + bill allocations + reconciliation) is too complex to reimplement over ODBC.
4. **Capability detection over version hardcoding** — correct. Matches the existing fallback pattern in definition_extractor.py.
5. **Delivery order** — batching first, ODBC second. The batching delivers immediate ERP 9 reliability with zero ODBC dependency.

---

## Issues

### 1. `normalizeSyncMeta()` will silently drop the new fields

Plan Section 6 says "accept and preserve the extra sync_meta fields." But `sync.ts:50-63` is a **whitelist**:

```typescript
function normalizeSyncMeta(value: unknown): SyncMeta {
  return {
    voucher_sync_mode,
    voucher_from_date: ...,
    voucher_to_date: ...,
    master_changed: ...,
    voucher_changed: ...,
  };
}
```

The proposed `section_sources`, `product_name`, `product_version`, `odbc_status` will be stripped on arrival. The plan says "no schema migration required" but the `SyncMeta` type and `normalizeSyncMeta()` both need updating. This is a code change, not a schema migration — the plan should be explicit about it.

### 2. `sync_log` has no place to store the metadata

The plan says "store the metadata inside existing JSON-based sync logging." But the actual sync_log insert (`sync.ts:900-904`) is:

```typescript
await supabase.from("sync_log").insert({
  company_id: companyId,
  status: "success",
  records_synced: records,  // Record<string, number>
});
```

There's no `sync_meta` column. `records_synced` is a count map (`{ groups: 5, ledgers: 42 }`), not a generic JSON bag. Either:
- Add a `sync_meta` JSONB column to `sync_log` (a small migration), or
- Stuff metadata into `records_synced` (semantically wrong)

The plan needs to pick one.

### 3. Voucher batching + failed windows = data-loss risk via reconciliation

`reconcileVoucherScope()` (`sync.ts:365-382`) loads all existing vouchers for the company/date-range, then **deletes any whose tally_guid is NOT in the incoming set**.

If one monthly batch window fails completely after all retries, the merged voucher list will be missing those vouchers. Reconciliation will then delete them from the database — a silent data loss.

The plan says "merge and dedupe by tally_guid before push" but doesn't specify what happens when a window fails permanently. Options:
- **Abort the entire sync** if any window fails after retries (safest)
- **Skip reconciliation** when the fetch was incomplete (set `voucher_sync_mode: "none"` for that run)
- **Track which windows succeeded** and only reconcile within those date ranges

This needs to be specified. Recommendation: abort the sync — a partial voucher push without reconciliation leaves stale data, and partial reconciliation is complex.

### 4. Process timeout will be exceeded by batching

`sync-engine.ts:136` has `maxDurationMs` defaulting to 5 minutes (or `TB_SYNC_PROCESS_TIMEOUT_MS`). A full-FY voucher sync with monthly batching = 12 HTTP requests minimum. If each window takes the current 30s timeout ceiling, that's 6 minutes for vouchers alone — already over the process limit, before groups/ledgers/stock/reports.

With split-on-timeout retries, a single problematic month could generate 4-8 sub-requests. The plan says "configurable timeout per window" but doesn't address `maxDurationMs`. This timeout needs to either:
- Scale with the estimated number of batch windows, or
- Be increased to a higher default (15-20 minutes), or
- Be removed in favor of heartbeat-based liveness detection (the sync engine already reads stdout lines)

### 5. Config-to-Python plumbing is unspecified

The plan adds `readMode` and `odbcDsnOverride` to `AppConfig` but doesn't say how they reach the Python process. Currently `sync-engine.ts` passes config via specific env vars:

```typescript
env: { TALLY_URL, TALLY_COMPANY, TALLY_COMPANY_GUID, BACKEND_URL, API_KEY, TB_USER_DATA_DIR }
```

The plan needs to specify: add `READ_MODE` and `ODBC_DSN_OVERRIDE` to this env block, and `main.py` needs to read them via `os.environ.get()`.

Similarly, `save-settings` in `ipc-handlers.ts` only persists 5 specific fields. The new config fields need to be added to that handler explicitly.

### 6. ODBC helper IPC framing is unspecified

The plan says "stdin JSON request / stdout JSON response" with "single helper lifetime per sync run." But multiple request/response exchanges over a single process need a framing protocol:
- **How does the orchestrator know when one response ends?** Newline-delimited JSON (one JSON object per line) is the simplest option.
- **How does the orchestrator shut down the helper?** Close stdin? Send a `quit` command?
- **What if the helper crashes mid-sync?** The orchestrator needs to detect process exit and fall back to XML for all remaining sections.

### 7. No shadow/comparison mode for validating ODBC parity

Test plan item 7 says "ODBC and XML produce the same normalized row shapes." But there's no mechanism to verify this in production. During initial rollout, a shadow mode — fetch via both ODBC and XML, compare results, log discrepancies, use the XML result — would catch field mapping bugs before they corrupt data. This doesn't need to ship in v1 UI, but the Python orchestrator should support a `READ_MODE=shadow` option for development/testing.

### 8. The ODBC mapping config decision is left too open

Section 7 says the ODBC mapping can go "inside structured_sections.json, or in a sibling config file." These are very different approaches. `structured_sections.json` defines XML-specific things (`collection_type`, `fetch_list`, `response_tag`). ODBC needs completely different metadata (SQL template, column names, type mappings). Mixing them in one file creates coupling between two transports.

A sibling file (e.g., `odbc_sections.json`) keyed by the same section names is cleaner — the ODBC helper can load just its own config without parsing XML metadata. The plan should commit to one approach.

---

## Summary

The plan is well-structured. The three things that need resolution before implementation starts:

1. **Data-loss risk**: What happens when a voucher batch window fails permanently? (Recommend: abort the sync.)
2. **Backend metadata gap**: `normalizeSyncMeta()` and `sync_log` both need small changes — acknowledge them.
3. **Process timeout**: Batching will exceed the 5-minute `maxDurationMs` on full-FY syncs.

The rest (IPC framing, config plumbing, shadow mode, ODBC config file) are implementation details that can be resolved during Phase 2, but they should be noted as open decisions.
