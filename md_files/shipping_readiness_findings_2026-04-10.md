# Shipping Readiness Findings

Date: 2026-04-10
Repo: `D:\Desktop\TallyBridge`

## Overall View

The current app is in a solid state for local development and live ERP 9 testing, but it is not fully ready for production packaging yet.

The biggest blocker before shipping is the Python runtime/binary path in packaged builds.

## Confirmed Working

- Settings persist across restart via Electron Store.
- Add Company flow is wired and duplicate companies are blocked by GUID or normalized name.
- Tally connection test is implemented in Settings.
- Capabilities check shows XML/ODBC state.
- Sync timer is active and `syncNow()` is wired.
- Sync overlap is prevented by `isSyncing`.
- Live sync logs stream into the UI.
- Company success/error state is updated after sync.
- App hides to tray instead of closing.
- Tray "Sync Now" is wired.
- Idle timeout is implemented with a default of 10 minutes.
- Hard timeout is implemented with a default of 45 minutes.
- ERP 9 two-pass voucher sync is implemented in the Python layer.

## At Risk / Not Done

- Production Python runtime is not wired correctly.
  - `src/main/sync-engine.ts` expects:
    - `process.resourcesPath/python-runtime/tallybridge-engine.exe`
  - Packaging config currently copies:
    - `src/python/` -> `resources/python/`
  - This mismatch means packaged production builds are likely to fail when they try to launch the sync engine.

- Backend URL and API key are passed through to Python, but there is no strong preflight validation before spawning the sync process.
  - Current behavior depends too much on downstream failure messages.

- Tally/network/backend failure messages are improved compared to earlier iterations, but not fully normalized into clear user-facing categories.
  - Some failure paths still fall back to generic error text.

- Installer / packaged app flow has not been confirmed end to end on a clean machine.

- Sync log persistence is not implemented.
  - Logs are streamed to the renderer, but not durably written to disk.

## Nuanced Notes

- "ODBC unavailable = XML fallback" is true for the sections that already implement that transport strategy, but should not be described as a blanket guarantee for every section.
- Date range clamping exists in the UI, but correctness still depends on the backend/Python layer as the final authority.
- ERP 9 support is materially stronger now because of the two-pass voucher strategy, but shipping readiness still depends more on packaging/runtime reliability than on sync logic.

## Recommended Priority Order

1. Fix production Python runtime packaging and launch path.
2. Add explicit preflight validation for backend URL and API key before spawn.
3. Test `npm run dist` and the installed app on a clean machine.
4. Add persistent log storage.
5. Improve error classification for Tally-down, backend-down, auth failure, and timeout cases.

## Bottom Line

The app is close on functionality.

The main shipping problem is not ERP 9 sync logic anymore. It is the production packaging/runtime story for the Python engine.
