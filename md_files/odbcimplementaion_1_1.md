# ODBC / Hybrid Transport Implementation Log 1.1

## Scope

This file records what has been implemented so far for hybrid XML + ODBC support in TallyBridge, with the main target being:

- Tally.ERP 9 Release 6.6.3
- latest TallyPrime

The goal was:

- keep XML as the canonical transport
- add ODBC as an optional read accelerator
- use ODBC first for selected master sections
- keep vouchers and report-style sections on XML
- preserve backend/Supabase compatibility

## Final Implemented Transport Model

### XML remains authoritative for

- company discovery
- company info
- alter IDs / change detection
- vouchers
- outstanding
- profit & loss
- balance sheet
- trial balance

### ODBC is used first for

- groups
- ledgers
- stock items

### Fallback behavior

For ODBC-capable sections:

1. try ODBC first
2. if ODBC is unavailable or unsupported, fall back to structured XML
3. if structured XML fails, fall back to legacy XML parsing

## Core Files Added / Changed

### Python sync path

- [src/python/sync_main.py](/D:/Desktop/TallyBridge/src/python/sync_main.py)
- [src/python/main.py](/D:/Desktop/TallyBridge/src/python/main.py)
- [src/python/odbc_bridge.py](/D:/Desktop/TallyBridge/src/python/odbc_bridge.py)
- [src/python/tally_odbc_helper.ps1](/D:/Desktop/TallyBridge/src/python/tally_odbc_helper.ps1)
- [src/python/definitions/odbc_sections.json](/D:/Desktop/TallyBridge/src/python/definitions/odbc_sections.json)
- [src/python/tally_client.py](/D:/Desktop/TallyBridge/src/python/tally_client.py)

### Electron / desktop app

- [src/main/sync-engine.ts](/D:/Desktop/TallyBridge/src/main/sync-engine.ts)
- [src/main/ipc-handlers.ts](/D:/Desktop/TallyBridge/src/main/ipc-handlers.ts)
- [src/main/store.ts](/D:/Desktop/TallyBridge/src/main/store.ts)
- [src/main/preload.ts](/D:/Desktop/TallyBridge/src/main/preload.ts)
- [src/renderer/electron.d.ts](/D:/Desktop/TallyBridge/src/renderer/electron.d.ts)
- [src/renderer/pages/Settings.tsx](/D:/Desktop/TallyBridge/src/renderer/pages/Settings.tsx)

### Backend

- [backend/src/routes/sync.ts](/D:/Desktop/TallyBridge/backend/src/routes/sync.ts)
- [backend/full_schema.sql](/D:/Desktop/TallyBridge/backend/full_schema.sql)
- [backend/supabase_schema_v3.sql](/D:/Desktop/TallyBridge/backend/supabase_schema_v3.sql)
- [backend/supabase_schema_v4.sql](/D:/Desktop/TallyBridge/backend/supabase_schema_v4.sql)

## What Was Implemented

### 1. New sync entrypoint

The live sync flow now runs through:

- [src/python/sync_main.py](/D:/Desktop/TallyBridge/src/python/sync_main.py)

This file handles:

- product detection
- company info fetch
- alter-id based change detection
- ODBC-first section selection
- XML voucher batching
- XML report fetches
- sync metadata generation
- push to backend

`main.py` was reduced to a thin entry wrapper that calls `sync_main.main()`.

### 2. ODBC bridge and helper process

Implemented a separate ODBC bridge layer:

- Python side: [src/python/odbc_bridge.py](/D:/Desktop/TallyBridge/src/python/odbc_bridge.py)
- helper process: [src/python/tally_odbc_helper.ps1](/D:/Desktop/TallyBridge/src/python/tally_odbc_helper.ps1)

The helper uses newline-delimited JSON over stdin/stdout with:

- `probe`
- `query`
- `quit`

Returned states include:

- `ok`
- `empty`
- `unsupported`
- `not_configured`
- `error`

### 3. ODBC section mapping

Added:

- [src/python/definitions/odbc_sections.json](/D:/Desktop/TallyBridge/src/python/definitions/odbc_sections.json)

Mapped sections:

- `groups`
- `ledgers`
- `stock_items`

Important correction made during implementation:

- `groups` needed to query `Groups`, not `Group`

### 4. PowerShell / ODBC bitness fix

This was one of the main live issues during implementation.

Originally the ODBC helper used the x86 PowerShell path, which could not see the registered DSN:

- `TallyODBC64_9000`

This caused the app to incorrectly report:

- ODBC not configured

Fixed in:

- [src/python/odbc_bridge.py](/D:/Desktop/TallyBridge/src/python/odbc_bridge.py)

The bridge now tries multiple PowerShell/ODBC views so it can detect whichever DSN is actually registered on the machine.

### 5. Live ODBC detection behavior

Current DSN resolution order:

1. explicit override from config
2. `TallyODBC64_<port>`
3. `TallyODBC_<port>`

On the live ERP 9 machine, the working DSN is:

- `TallyODBC64_9000`

### 6. Desktop config and diagnostics

Added app config fields:

- `readMode`
- `odbcDsnOverride`

Supported modes:

- `auto`
- `xml-only`
- `hybrid`
- `shadow` (developer/testing mode)

Also added capability diagnostics through the desktop app so the UI can report:

- XML connectivity
- ODBC status
- DSN used
- section transport plan

### 7. ERP 9 voucher batching and timeouts

Implemented batched XML voucher export in:

- [src/python/sync_main.py](/D:/Desktop/TallyBridge/src/python/sync_main.py)

Behavior:

- full voucher sync splits into monthly windows
- incremental mode batches inside the reduced date range
- on timeout, windows are recursively split smaller
- results are merged and deduped by `tally_guid`

Important ERP 9 change:

- for ERP 9, voucher export now prefers direct Day Book XML instead of the structured collection path

This is what made the ERP 9 full-year sync reliable enough to complete in live testing.

### 8. Timeout model improvements

The sync launcher no longer relies only on one short total timeout.

Implemented in:

- [src/main/sync-engine.ts](/D:/Desktop/TallyBridge/src/main/sync-engine.ts)

Added:

- idle timeout behavior
- hard max timeout behavior
- long-running sync support as long as progress is still being emitted

### 9. Backend sync metadata

Extended backend sync metadata in:

- [backend/src/routes/sync.ts](/D:/Desktop/TallyBridge/backend/src/routes/sync.ts)

Added fields:

- `section_sources`
- `product_name`
- `product_version`
- `odbc_status`

Also added:

- `sync_log.sync_meta`

Migration file:

- [backend/supabase_schema_v3.sql](/D:/Desktop/TallyBridge/backend/supabase_schema_v3.sql)

Fallback behavior:

- if `sync_meta` column is missing in a live database, sync logging falls back to the old insert shape instead of failing the sync

### 10. Backend pagination fixes

The backend GET APIs had a hidden 1000-row cap from Supabase.

Fixed in:

- [backend/src/routes/sync.ts](/D:/Desktop/TallyBridge/backend/src/routes/sync.ts)

Added:

- `fetchAllPages(...)`

Improvements made after review:

- query errors now throw instead of silently truncating results
- paginated endpoints now use deterministic ordering with `id` tie-breakers

Affected routes:

- `/vouchers`
- `/outstanding`
- `/stock`
- `/parties`

### 11. Purchase section

Added a dedicated backend `purchases` section/table.

Files:

- [backend/full_schema.sql](/D:/Desktop/TallyBridge/backend/full_schema.sql)
- [backend/supabase_schema_v4.sql](/D:/Desktop/TallyBridge/backend/supabase_schema_v4.sql)
- [backend/src/routes/sync.ts](/D:/Desktop/TallyBridge/backend/src/routes/sync.ts)
- [purchaseheaderadd.md](/D:/Desktop/TallyBridge/purchaseheaderadd.md)

Implementation approach:

- purchases are derived from synced `vouchers`
- no desktop payload change was required
- purchase rows are created from vouchers whose `voucher_type` contains `"purchase"`
- added `/api/sync/purchases`

## Live Testing Results

### ERP 9 live test status

Verified live against:

- `http://localhost:9000`
- company: `K.V.ENTERPRISES 18-19`

Successful hybrid run result:

- ODBC probe succeeded via `TallyODBC64_9000`
- `groups` via ODBC: `37`
- `ledgers` via ODBC: `1078`
- `stock_items` via ODBC: `6891`
- `vouchers` via XML: `332`
- `outstanding` via XML: `4921`
- `profit_loss` via XML: `9`
- `balance_sheet` via XML: `40`
- `trial_balance` via XML: `11`
- backend upload to `localhost:3001` succeeded

`sync_meta.section_sources` from the successful run showed:

- `groups: odbc`
- `ledgers: odbc`
- `stock_items: odbc`
- `vouchers: xml`
- `outstanding: xml`
- `profit_loss: xml`
- `balance_sheet: xml`
- `trial_balance: xml`

### Change detection re-test

Immediate second run correctly returned:

- `No changes detected`

So alter-id cache and remote alter-id comparison are working in the current live path.

## Main Problems Encountered During Implementation

### 1. ERP 9 XML voucher timeout / server hang

Originally:

- full-year voucher export timed out
- ERP 9 sometimes stopped responding after the heavy request

Mitigation implemented:

- monthly batching
- recursive window splitting
- Day Book direct XML for ERP 9

### 2. ODBC false negative due to bitness mismatch

Originally:

- helper reported ODBC `not_configured`
- actual DSN existed

Cause:

- helper process was using the wrong PowerShell / ODBC registry view

Fix:

- bridge now probes across available PowerShell/ODBC views

### 3. Older PowerShell compatibility

Some helper syntax was initially too modern for the machine’s PowerShell version.

Fixes included removing unsupported constructs from:

- [src/python/tally_odbc_helper.ps1](/D:/Desktop/TallyBridge/src/python/tally_odbc_helper.ps1)

### 4. Backend readback truncation

Large GET endpoints originally returned only 1000 rows.

Fix:

- pagination helper
- deterministic ordering
- proper error propagation

## What Still Depends On Supabase Changes

To fully match the implemented backend code, the database needs:

### Already needed

- [backend/supabase_schema_v3.sql](/D:/Desktop/TallyBridge/backend/supabase_schema_v3.sql)
  - adds `sync_log.sync_meta`

### Newly needed

- [backend/supabase_schema_v4.sql](/D:/Desktop/TallyBridge/backend/supabase_schema_v4.sql)
  - adds `purchases`

## Current Status

### Implemented and working

- hybrid XML + ODBC sync architecture
- ERP 9 live hybrid sync
- latest TallyPrime-safe XML fallback behavior
- ODBC diagnostics and config plumbing
- voucher batching for ERP 9
- backend sync metadata
- backend pagination fixes
- derived purchases section

### Current practical requirement

For the `purchases` feature to work in Supabase, run:

- [backend/supabase_schema_v4.sql](/D:/Desktop/TallyBridge/backend/supabase_schema_v4.sql)

## Notes

- ODBC is optional, not mandatory
- XML remains the canonical fallback
- current purchase classification is based on voucher type containing `"purchase"`
- packaged desktop builds still need to be rebuilt/released to include the latest runtime fixes if the user is not running directly from this workspace
