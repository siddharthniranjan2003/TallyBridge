# TallyBridge Client Handoff Checklist

## Before GitHub / Render

- Rotate `SUPABASE_SERVICE_KEY` and `API_KEY` if they were exposed during development.
- Do not treat local `.env` values as production-final; set final values in Render.
- Clean tracked backend junk before pushing:
  - `backend/backend-dev.stderr.log`
  - `backend/backend-dev.stdout.log`
  - `backend/reorder_report_test.pdf`
- Commit the intended backend schema and route changes only.
- Decide whether compiled backend `js` / `d.ts` artifacts under `backend/src/` should remain committed or be removed.

## Backend / Supabase

- Confirm the backend is deployed publicly, for example on Render.
- Confirm backend env vars are set in hosting:
  - `SUPABASE_URL`
  - `SUPABASE_SERVICE_KEY`
  - `API_KEY`
- Confirm Supabase schema is up to date.
- For a fresh project, run `backend/full_schema.sql`.
- At minimum, ensure the `push_queue` table exists from `backend/supabase_new_tables.sql`.
- Confirm backend health endpoint works:
  - `GET /health`

## Electron EXE Readiness

- Verify Python runtime bundling for production is correct.
- The installed app expects:
  - `resources/python-runtime/tallybridge-engine.exe`
  - `resources/python/sync_main.py`
- Test `npm run dist` and produce the NSIS installer from `release/`.
- Install the EXE on a clean Windows machine or VM before client delivery.
- Confirm the app starts correctly after install and stays in tray as expected.

## Client-Editable Settings

- The client should only need to edit app settings, not Supabase credentials directly.
- Confirm these values can be entered and saved in the app:
  - `backendUrl`
  - `apiKey`
  - `tallyUrl`
  - `syncIntervalMinutes`
  - `readMode`
  - `odbcDsnOverride`
  - `syncFromDate`
  - `syncToDate`
- Confirm local config persistence works in:
  - `%APPDATA%\tallybridge\tallybridge-config.json`

## Tally Setup On Client Machine

- TallyPrime must be installed and open.
- Tally HTTP/XML server must be enabled.
- `tallyUrl` should normally be `http://localhost:9000`.
- Confirm the required company is visible in TallyBridge and can be added.
- Run one successful sync first so the backend knows the company before queued voucher push is attempted.

## Queue Push Flow

- Confirm TallyBridge is configured with the production `backendUrl` and matching `apiKey`.
- Confirm the client company exists in the backend `companies` table.
- Confirm n8n final step posts to:
  - `/api/sync/push-queue`
- Confirm the n8n payload includes:
  - `company_name`
  - `voucher_payload`
- Confirm queued jobs are picked up by the app and marked `pushed` or `failed`.

## Failsafe Improvements Recommended

- Keep idle and hard timeout protection enabled for sync worker processes.
- Add startup checks for:
  - backend connectivity
  - Tally connectivity
  - local push server health
- Add retry with backoff for transient backend and Tally failures.
- Use per-client API keys instead of one shared API key.
- Prefer secure local secret storage such as Windows Credential Manager / `keytar` over plain JSON for API keys.
- Code-sign the installer to reduce SmartScreen friction.

## Logging / Supportability

- Add persistent log files under `%APPDATA%\TallyBridge\logs\`.
- Split logs into:
  - app
  - sync
  - push
  - python stderr
- Include timestamps, app version, company, backend URL, and Tally URL in logs.
- Preserve the last Python stdout/stderr block for failed sync or push runs.
- Add log rotation.
- Add an `Export diagnostics` action for support.

## Final Acceptance Test

- Fresh install of EXE
- Save settings
- Detect Tally companies
- Add company
- Run sync successfully
- Confirm backend receives company data
- Send one queued voucher from n8n
- Confirm voucher imports into client Tally
- Confirm failure path produces usable logs
