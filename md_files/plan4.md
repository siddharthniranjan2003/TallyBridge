# Final Plan: Hybrid XML + ODBC Support for Tally ERP 9 6.6.3 and Latest TallyPrime

## Summary

Support both Tally ERP 9 6.6.3 and latest TallyPrime with a version-aware hybrid read pipeline:

- Keep XML as the canonical transport
- Add ODBC as an optional ERP 9 read accelerator for selected sections
- Use capability detection and fallback, never a hard dependency
- Keep vouchers on XML in v1
- Fix ERP 9 reliability first with XML voucher batching, because that is the immediate blocker

This plan reflects the codebase as it exists today and incorporates the important corrections from the later reviews.

## Implementation Changes

### 1. Transport model and fallback order

Use this exact transport strategy:

- Always XML
  - company discovery
  - company info
  - alter IDs and change detection
  - vouchers
  - outstanding
  - profit & loss
  - balance sheet
  - trial balance
  - any future write-back into Tally
- ODBC candidates in v1
  - groups
  - ledgers
  - stock items

ODBC must be a prefix to the existing XML fallback chain:

1. If `readMode` allows ODBC and ODBC probe passes for the section, try ODBC
2. If ODBC fails or returns unsupported, try definition-driven XML
3. If structured XML fails, try legacy XML
4. If the section already has a report fallback, keep that as the final tier

ODBC reads must respect the existing sync plan:

- only fetch groups when `need_groups` is true
- only fetch ledgers when `need_ledgers` is true
- only fetch stock when `need_stock` is true

### 2. ERP 9 voucher batching and failure policy

Do not move vouchers to ODBC in v1.

Implement voucher batching as a wrapper around the current voucher fetch flow:

- preserve the current `voucher_sync_mode`
- if mode is `full`, split the date range into monthly windows
- if mode is `incremental`, batch inside the already narrowed date range
- on timeout, recursively split the failing window until success or a 1-day minimum window
- merge all successful windows
- dedupe by `tally_guid` before push

Critical failure rule:

- if any voucher window still fails after all retries, abort the entire sync
- do not push a partial voucher set
- do not run voucher reconciliation on incomplete data

This avoids silent data loss through the current voucher reconciliation logic.

Also add:

- separate connect timeout and read timeout for XML
- progress log lines for each voucher window
- a clean error when ERP 9 becomes unresponsive after heavy reads

### 3. ODBC helper process

Do not add ODBC directly to the current 64-bit Python sync engine.

Add a separate Windows x86 ODBC helper process with newline-delimited JSON over stdin/stdout.

Helper commands:

- `probe`
- `query`
- `quit`

Helper response states:

- `ok`
- `empty`
- `unsupported`
- `not_configured`
- `error`

Helper requirements:

- resolve DSN in this order:
  1. explicit override
  2. `TallyODBC64_<port>`
  3. `TallyODBC_<port>`
- normalize stdout to UTF-8
- handle ERP 9 codepage conversion explicitly
- enforce per-query timeout
- stay alive for the full sync run so connections can be reused
- if the helper exits unexpectedly, fall back to XML for the remaining ODBC-capable sections in that sync

### 4. ODBC mapping configuration

Do not mix ODBC metadata into the existing XML section definitions.

Add a dedicated sibling config such as `odbc_sections.json`, keyed by the same section names.

Each ODBC-capable section definition must include:

- query template
- expected columns
- normalized field mapping
- required field
- any section-specific post-processing rules

This keeps XML and ODBC concerns separated while preserving the same normalized output shape.

### 5. Desktop config, env plumbing, and diagnostics

Extend local app config with:

- `readMode: "auto" | "xml-only" | "hybrid"`
- `odbcDsnOverride?: string`

Config behavior:

- `auto`
  - prefer ODBC only for supported sections when probe passes
  - otherwise XML
- `xml-only`
  - never call the ODBC helper
- `hybrid`
  - always probe ODBC and use it where available, else XML fallback

Add these Python env vars in the sync-engine spawn:

- `TB_READ_MODE`
- `TB_ODBC_DSN_OVERRIDE`
- idle timeout env
- hard max timeout env

Add a new IPC method:

- `check-tally-capabilities`

It should return:

- XML connectivity
- company and product info if available
- ODBC status
- DSN resolution result
- section transport plan

UI scope for v1:

- add `readMode` and optional DSN override to stored config
- add minimal capability diagnostics in Settings
- surface XML vs ODBC decisions through the existing sync log stream
- do not auto-register ODBC drivers in this release

### 6. Long-running sync timeout model

The current process timeout model is too small for full-FY batched ERP 9 syncs.

Replace the simple fixed kill behavior with:

- idle timeout
  - resets whenever the sync process emits progress output
- hard max timeout
  - higher default suitable for full-FY batched syncs

Rules:

- do not kill a sync that is still emitting progress
- do kill a sync that has gone silent past the idle threshold
- keep the hard max as a final safety cap

### 7. Backend metadata and audit

Keep all normalized business payload shapes unchanged.

Extend `sync_meta` in the sync payload with:

- `section_sources`
- `product_name`
- `product_version`
- `odbc_status`

Backend changes:

- extend the backend `SyncMeta` type
- update `normalizeSyncMeta()` so the new fields are not silently dropped
- add a small migration to `sync_log`:
  - `sync_meta JSONB NULL`
- store normalized sync metadata in `sync_log.sync_meta`

Do not overload `records_synced` with metadata.
Do not add new business-table columns for this feature.

### 8. Shadow mode for validation

Add a developer/testing-only mode:

- `TB_READ_MODE=shadow`

Behavior:

- for ODBC-capable sections, fetch both ODBC and XML
- compare normalized results
- log mismatches clearly
- use XML as the authoritative result
- do not expose this as a normal end-user UI mode in v1

### 9. Delivery order

Implement in this order:

1. XML voucher batching and timeout hardening
2. ODBC helper with probe/query/quit contract
3. ODBC-first dispatch for groups/ledgers/stock with XML fallback
4. Desktop capability diagnostics and config plumbing
5. Backend `sync_meta` support and `sync_log` migration
6. Shadow-mode comparison support for rollout validation

## Public Interfaces / Type Changes

Add or extend these interfaces:

- `AppConfig`
  - `readMode: "auto" | "xml-only" | "hybrid"`
  - `odbcDsnOverride?: string`
- New IPC
  - `check-tally-capabilities`
- Python env inputs
  - `TB_READ_MODE`
  - `TB_ODBC_DSN_OVERRIDE`
  - idle timeout env
  - hard timeout env
- Sync payload metadata
  - `sync_meta.section_sources`
  - `sync_meta.product_name`
  - `sync_meta.product_version`
  - `sync_meta.odbc_status`
- Database
  - add `sync_log.sync_meta JSONB`

No normalized business row shapes change in v1.

## Test Plan

### ERP 9 6.6.3

1. XML-only mode, no ODBC DSN
- company discovery works
- company info works
- groups, ledgers, and stock sync via XML
- vouchers sync through batched XML
- reports sync through XML
- Supabase upload completes

2. Hybrid mode, ODBC available
- groups, ledgers, and stock come from ODBC
- vouchers and reports stay on XML
- sync log shows actual section source
- normalized output matches XML-only baseline

3. Voucher batch failure
- at least one voucher window fails after all retries
- full sync aborts
- no partial voucher reconciliation occurs
- existing vouchers remain intact in Supabase

4. Long-running full-FY sync
- sync exceeds the old 5-minute threshold
- idle timeout does not fire while progress is still emitted
- hard max timeout is not hit during normal batched operation

### Latest TallyPrime

5. Auto mode
- current XML behavior remains valid
- no regressions for discovery, masters, vouchers, stock, outstanding, P&L, balance sheet, trial balance

6. ODBC unavailable or unsupported
- app falls back cleanly to XML
- no false ODBC-ready state appears in the UI or logs

### Validation and Audit

7. Shadow mode
- ODBC and XML both run for ODBC-capable sections
- mismatches are logged clearly
- XML remains authoritative

8. Backend audit
- new `sync_meta` fields survive normalization
- `sync_log.sync_meta` contains transport metadata for the run

## Assumptions and Defaults

- This feature is for reading from Tally only
- Windows is the only ODBC target platform
- ODBC remains optional and ERP 9-focused in v1
- vouchers remain XML in v1 hybrid
- a small `sync_log` migration is acceptable
- a dedicated `odbc_sections.json` file is preferred over mixing ODBC metadata into XML definitions
- shadow mode is developer/testing-only
- the x86 helper must ship as a bundled, reliable component and must not depend on the customer manually installing a matching 32-bit interpreter
