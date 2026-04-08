# ODBC / ERP 9 Implementation Run 1.0

Date: 2026-04-07
Workspace: `D:\Desktop\TallyBridge`
Target: Tally.ERP 9 on `http://localhost:9000`
Company: `K.V.ENTERPRISES 18-19`
GUID: `f3e9df46-fc4f-4b85-930a-abe1e427e900`

## What Worked

- Electron main build passed.
- Renderer build passed.
- Backend TypeScript build passed.
- New Python files passed syntax checks.
- ERP 9 XML connectivity worked initially.
- Company discovery worked.
- Company info fetch worked.
- Groups fetch worked and returned `37`.
- Ledgers fetch worked and returned `1078`.
- The ODBC helper now runs on the machine's older Windows PowerShell and returns a typed result instead of crashing.

## Issues Encountered

### 1. Older Windows PowerShell compatibility broke the first ODBC helper run

Initial helper implementation used syntax not supported by the Windows PowerShell version on this machine:

- `??`
- `ConvertFrom-Json -AsHashtable`

Symptoms:

- ODBC probe failed before any real DSN check.
- Sync aborted too early on the first live run.

Fix applied during this run:

- Removed use of `??`.
- Replaced `ConvertFrom-Json -AsHashtable` with plain `ConvertFrom-Json`.
- Made ODBC probe failure non-fatal so XML fallback still proceeds.

### 2. No Tally ODBC DSN is registered on this machine

After fixing the helper itself, the live ODBC probe returned:

- `state = not_configured`
- `TallyODBC64_9000` not found
- `TallyODBC_9000` not found

Impact:

- Hybrid read path is implemented in code, but ODBC is inactive on this machine right now.
- Groups, ledgers, and stock fall back to XML.

Implication:

- To test real ODBC reads, the Tally ODBC DSN must be registered first.

### 3. ERP 9 voucher export is still the main blocker

Live sync behavior:

- Company info succeeded.
- Change detection succeeded.
- Groups succeeded.
- Ledgers succeeded.
- Voucher export started as batched XML.

Observed failure:

- First monthly voucher window timed out.
- Recursive splitting kept reducing the window size.
- Even a single-day voucher window still timed out.
- Sync aborted intentionally once the smallest window failed.

Impact:

- No partial voucher set was pushed.
- No backend reconciliation was allowed to run on incomplete voucher data.
- End-to-end sync did not complete.

This is a product-correct failure mode, but still a functional blocker for ERP 9.

### 4. ERP 9 HTTP server became unresponsive after voucher stress

After the voucher stress test:

- Even `GET http://localhost:9000/` timed out.
- Direct manual one-day Day Book requests also timed out.
- Direct structured one-day voucher requests also timed out.

Impact:

- Could not immediately re-run the improved ERP-9-specific Day Book-only voucher path.
- Tally ERP 9 now needs a manual restart before the next live attempt.

### 5. First voucher strategy on ERP 9 was too aggressive

During the live run, the voucher path still tried the structured voucher collection before falling back to Day Book.

Likely effect:

- The structured voucher request appears to be the more dangerous path for ERP 9.
- Once it times out, the server may already be in a degraded state before the fallback executes.

Fix applied during this run:

- Updated ERP 9 voucher batching code to prefer Day Book XML directly for voucher windows.

Status:

- Code changed.
- Not yet re-tested because Tally hung and needed restart.

### 6. Dedicated backend launch on port 3002 failed

Attempted to start a separate backend instance for isolation.

Observed problems:

- `Start-Process` failed with Windows dictionary/key collision:
  - duplicate `Path` / `PATH`
- A later background launch attempt to `3002` did not come up.

Impact:

- Realtime testing used the already-running backend on `3001`.
- Could not fully prove the live backend process was the newly launched isolated instance.

### 7. Packaged production runtime is not fully validated yet

Realtime testing used the new Python script entrypoint:

- `src/python/sync_main.py`

But the packaged production path still references the existing bundled executable:

- `tallybridge-engine.exe`

Impact:

- Dev/test path is updated.
- Installed packaged app behavior is not fully validated from this run alone.
- Production packaging/runtime may still need a corresponding rebuild/update step.

### 8. The old `src/python/main.py` entrypoint became stale during refactor

While implementing the new sync path, the legacy `main.py` was no longer the real runtime target.

Risk:

- Keeping an outdated or half-edited entrypoint would make the repo confusing and unsafe.

Fix applied during this run:

- Replaced `src/python/main.py` with a thin wrapper that delegates to `sync_main.py`.

## Current Code-Level Outcome

- XML voucher batching is implemented.
- Abort-on-partial-voucher-failure is implemented.
- Idle vs hard timeout handling is implemented in Electron main sync launcher.
- ODBC helper protocol is implemented.
- ODBC fallback to XML is implemented.
- Backend `sync_meta` support is implemented with backward-safe sync log insert behavior.
- ERP 9 now prefers Day Book XML for voucher windows in code.

## What Still Needs To Be Tested Next

1. Restart Tally ERP 9.
2. Re-run live sync with the new ERP-9-specific Day Book-only voucher path.
3. Confirm whether one-day or monthly Day Book windows now succeed after removing the structured voucher attempt.
4. Confirm backend upload completes after vouchers succeed.
5. Optionally register the Tally ODBC DSN and re-test the hybrid path for groups, ledgers, and stock.
