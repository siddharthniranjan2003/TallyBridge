# Final Merged Plan: Hybrid XML + ODBC Support for Tally ERP 9 6.6.3 and Latest TallyPrime

## Summary

Support both Tally ERP 9 6.6.3 and latest TallyPrime with a version-aware hybrid read pipeline:

- Keep XML as the canonical transport
- Add ODBC as an optional read accelerator for selected ERP 9 sections
- Use capability detection + fallback, never hard dependency
- Fix ERP 9 reliability first with XML voucher batching, because that is the current production blocker

This merged plan keeps the strong product direction from `Plan.md` and adopts the key engineering refinements from `plan1.md`.

## Implementation Changes

### 1. Read strategy and fallback order

Implement ODBC as a prefix to the existing XML fallback chain, not as a parallel replacement.

Final section fetch order for ODBC-capable sections:

1. If `readMode` allows ODBC and ODBC probe passes for the section, try ODBC
2. If ODBC fails or returns an explicit unsupported/error result, try definition-driven XML
3. If structured XML fails, try the legacy XML parser
4. If the section already has a report-style fallback, keep using it as the final tier

This preserves the current architecture in `main.py` and `definition_extractor.py` rather than introducing a separate new router.

### 2. Section ownership by transport

Use this exact v1 ownership:

- Always XML
  - company discovery
  - company info
  - alter IDs / change detection
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

Reason:
- vouchers remain the most complex normalized graph and already rely on XML-specific parsing plus backend reconciliation
- report-style sections are not good ODBC v1 candidates
- this minimizes regression risk for latest TallyPrime

ODBC reads must still respect the existing sync plan:
- if `need_groups` is false, do not fetch groups through ODBC
- same for `need_ledgers` and `need_stock`

### 3. ERP 9 voucher reliability via XML batching

Do not move vouchers to ODBC in v1.

Instead, wrap the current voucher fetch flow with batching:

- keep existing `voucher_sync_mode` behavior
- if mode is `full`, split the full range into monthly windows
- if mode is `incremental`, batch within the already narrowed range
- on timeout, recursively split the failing window until success or a 1-day minimum window
- merge all batch results client-side
- dedupe by `tally_guid` before push
- preserve one final combined payload to the backend so current reconciliation logic remains valid

Also add:
- separate connect timeout and read timeout
- clear log lines showing active voucher date window
- clean failure message when ERP 9 XML becomes unresponsive after heavy reads

### 4. ODBC helper process

Do not add ODBC directly to the current 64-bit Python sync engine.

Add a separate Windows x86 ODBC helper process with JSON stdin/stdout protocol.

Helper requirements:

- Commands
  - `probe`
  - `query`
- `probe` must return:
  - driver/DSN resolution result
  - product info if discoverable
  - supported sections
  - explicit unsupported vs unavailable vs error states
- `query` must return:
  - `ok`
  - `rows`
  - `empty`
  - `unsupported`
  - `error`
- Must define:
  - per-query timeout
  - UTF-8 normalized stdout output
  - codepage handling for ERP 9 ODBC results
  - single helper lifetime per sync run, not per query, so connection setup is reused

DSN resolution order:

1. explicit `odbcDsnOverride`
2. `TallyODBC64_<port>`
3. `TallyODBC_<port>`

If DSN/driver is missing, the helper must return a typed “not configured” result, not crash.

### 5. Desktop configuration and diagnostics

Extend app config with:

- `readMode: "auto" | "xml-only" | "hybrid"`
- `odbcDsnOverride?: string`

Behavior:

- `auto`
  - prefer ODBC only for supported sections when probe passes
  - otherwise XML
- `xml-only`
  - never call ODBC helper
- `hybrid`
  - always probe ODBC and use it for supported sections, else XML fallback

Add a new IPC method:

- `check-tally-capabilities`

It should return:
- XML connectivity
- discovered company/product info
- ODBC status
- DSN resolution result
- section transport plan

UI scope for v1:
- add `readMode` and optional DSN override to stored config
- add capability diagnostics in Settings or as a minimal diagnostic panel
- surface ODBC/XML decisions through the existing sync log stream
- do not add auto-registration of ODBC drivers in this release

### 6. Backend contract and metadata

Keep all business payload shapes unchanged.

Extend `sync_meta` in the sync payload with:

- `section_sources`
- `product_name`
- `product_version`
- `odbc_status`

Backend changes:
- accept and preserve the extra `sync_meta` fields
- do not change company/group/ledger/voucher table schemas for this feature
- if audit persistence is required, store the metadata inside existing JSON-based sync logging rather than inventing a new normalized schema in v1
- do not require a core business-table migration for hybrid transport support

### 7. ODBC mapping configuration

Do not duplicate all section definitions in ad hoc code.

Reuse the existing section-driven pattern by adding optional ODBC mapping metadata per ODBC-capable section, either:
- inside `structured_sections.json`, or
- in a sibling config file with the same section names

Each ODBC-capable section definition must specify:
- query template
- expected columns
- normalized field mapping
- required field
- section-specific post-processing if needed

This keeps XML and ODBC outputs aligned to the same normalized payload shape.

### 8. Delivery order

Implement in this order:

1. XML voucher batching and timeout hardening
2. ODBC helper process with probe/query contract
3. ODBC-first section dispatch for groups/ledgers/stock with XML fallback
4. Desktop capability diagnostics and config surface
5. Backend `sync_meta` passthrough/audit support

## Public Interfaces / Type Changes

Add or extend these interfaces:

- `AppConfig`
  - `readMode: "auto" | "xml-only" | "hybrid"`
  - `odbcDsnOverride?: string`
- New IPC
  - `check-tally-capabilities`
- Sync payload metadata
  - `sync_meta.section_sources`
  - `sync_meta.product_name`
  - `sync_meta.product_version`
  - `sync_meta.odbc_status`

No changes to normalized section row shapes are allowed in v1.

## Test Plan

### ERP 9 6.6.3

1. XML-only mode, no ODBC DSN
- company discovery works
- company info works
- groups/ledgers/stock sync via XML
- vouchers sync via batched XML without hanging ERP 9
- reports sync via XML
- Supabase upload completes

2. Hybrid mode, ODBC available
- probe succeeds
- groups/ledgers/stock come from ODBC
- vouchers/reports still come from XML
- sync log shows per-section source
- normalized counts match XML-only baseline

3. Hybrid mode, ODBC partially fails
- one ODBC section errors or is unsupported
- that section falls back to XML in the same sync
- sync still completes successfully

4. Heavy voucher load
- full-FY sync batches correctly
- timeout window splitting works
- duplicate vouchers are removed by `tally_guid`
- final backend payload is one merged voucher set

### Latest TallyPrime

5. Auto mode
- current XML behavior remains valid
- no regressions for company discovery, masters, vouchers, stock, outstanding, P&L, balance sheet, trial balance

6. ODBC unavailable
- app stays fully functional through XML
- no false “ODBC connected” state in UI/logs

### Backend / Data Integrity

7. Transport parity
- ODBC and XML produce the same normalized row shapes
- GUID-based company identity remains intact
- voucher reconciliation behavior remains unchanged
- `sync_meta.section_sources` correctly reflects actual transport used

## Assumptions and Defaults

- This feature is for reading from Tally; Tally write-back remains out of scope
- Windows is the only ODBC target platform
- ODBC setup is detected and guided, not auto-executed
- ODBC is optional and never required for latest TallyPrime
- ERP 9 6.6.3 is the primary compatibility target for ODBC
- Voucher extraction stays XML in v1 hybrid
- The x86 ODBC helper is a product requirement; whether it is built from Python or another Windows-friendly runtime is an implementation detail, but it must ship as a reliable bundled helper and must not depend on the customer manually installing a matching 32-bit interpreter
