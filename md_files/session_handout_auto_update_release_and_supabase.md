# Session Handout — Auto-Update, GitHub Release Flow, 413 Diagnosis, Supabase→Client Switch

**Date:** 2026-06-05
**Branch:** `TallyBridge-Backend-Refactor`
**Purpose:** Full context handoff for a new chat. Covers everything done this session.

---

## 0. TL;DR of what was accomplished

1. Deep-dived the **TallyPrime ↔ Electron** interaction (read context only).
2. Built an **in-app auto-update feature** (GitHub Releases via electron-updater) and proved it **live on the production public repo**.
3. Uncovered and documented several **electron-builder release gotchas**.
4. Established the **remote-update release runbook** (commands).
5. Diagnosed a separate **413 "Request Entity Too Large"** sync error.
6. Mapped **where to switch Supabase creds to the client DB** (GCP Cloud Run).

---

## 1. TallyPrime ↔ Electron architecture (reference)

The Electron/TS process never speaks XML to Tally directly — it does a TCP port pre-flight then spawns the Python engine, which owns all `http://localhost:9000` XML traffic.

- **Read (Tally→Cloud):** `src/main/sync-engine.ts` → spawns `src/python/sync_main.py` → `tally_client.py` (XML export) → `cloud_pusher.py` → backend `/api/sync`.
- **Push (Cloud→Tally):** `src/main/push-queue-poller.ts` polls backend; `src/main/local-push-server.ts` (port 3002) receives → spawns `sync_main.py` (`TB_COMMAND=push_voucher`) → `tally_pusher.py` → Tally Import XML.
- **Change detection:** alter-ids (`ALTERID/ALTVCHID/ALTMSTID/LASTVOUCHERDATE`) cached; no change → sync skipped.
- **TallyPrime gotcha:** TDL voucher *collection* used to crash TallyPrime on port 9000; it now routes through the **Day Book fallback** path (`should_skip_voucher_family` reverted to always-False; `is_tallyprime` flag enables Day Book fallback). See top-of-file changelog in `sync_main.py`.

**"Does pulled data reflect the company date?"** — Books/FY dates yes (`BOOKSFROM/BOOKSTO`, range clamping in `clamp_range_to_company_books`), per-voucher `<DATE>` yes. Tally's *current/working date* (`SVCURRENTDATE`) is **not** fetched.

---

## 2. Auto-update feature (the main deliverable)

In-app update via **electron-updater** against GitHub Releases. Committed as **`69f21e8`** (click-based banner). 6 files:

| File | Change |
|------|--------|
| `src/main/updater.ts` | electron-updater setup; emits `update-available/progress/downloaded/error` over IPC; `download-update`/`install-update` IPC handlers; installs only when no sync is running (`installWhenIdle`, 5s recheck). |
| `src/main/index.ts` | passes `mainWindow` to `setupAutoUpdater`; `isQuitting` flag + close-to-tray bypass so install-restart works. |
| `src/main/preload.ts` | allowlists `update-*` channels; exposes `downloadUpdate`/`installUpdate`. |
| `src/renderer/electron.d.ts` | types for the two methods. |
| `src/renderer/components/UpdateBanner.tsx` | **new** — the banner UI (available → progress → restart-to-install; error gated to user-initiated actions only). |
| `src/renderer/App.tsx` | mounts `<UpdateBanner/>`. |

### ⚠️ CURRENT BEHAVIOR IS "FULLY AUTOMATIC" AND UNCOMMITTED
`src/main/updater.ts` was **subsequently edited (uncommitted)** to:
- `autoDownload = true` → downloads automatically when an update is found.
- `update-downloaded` → calls `installWhenIdle()` → **auto restart+install** once no sync runs. **No clicks required.**

So:
- **Committed (`69f21e8`)** = click-based ("Update now" → "Restart to install").
- **Working tree (uncommitted)** = fully automatic (banner is informational only).
- **The behavior a client experiences is baked into the build THEY have** — it's decided by the installed app's code, not the new release. Whatever updater.ts state was present when each version was built governs that version.
- **Decision needed:** commit the automatic change (or revert to click-based). Currently uncommitted.

The updater logs to `%APPDATA%\TallyBridge\logs\tallybridge.log` (`[updater] ...` lines).

---

## 3. electron-builder release gotchas (learned the hard way)

1. **`electron-builder.yml` is IGNORED.** A bare `electron-builder` / `npm run dist` loads the **`build` block in `package.json`** and ignores the `.yml`. The two are near-duplicate; the yml is dead config. (Recommend deleting `electron-builder.yml` for a single source of truth — still present, untouched.)
2. **Publish repo is inferred from git `origin`.** package.json `build` has no `publish` field, so the repo = `siddharthniranjan2003/TallyBridge` (from the git remote). To target a *different* repo you must pass `-c electron-builder.yml`.
3. **Releases publish as DRAFT.** electron-updater clients **cannot see drafts** — every publish must be **un-drafted**.
4. **Repo must be PUBLIC.** Private repos 404 on tokenless update checks. (Prod `TallyBridge` is public.)
5. **The Python engine is NOT auto-rebuilt.** `npm run build` and electron-builder only handle TS. The bundled `python-dist/tallybridge-engine.exe` is regenerated **only** by `npm run build:python` (PyInstaller). If `src/python/*.py` changed and you skip it, the client gets new UI on the **old engine**.

---

## 4. Remote-update release runbook (commands)

Run in PowerShell from `D:\Desktop\TallyBridge`:

```powershell
# 1. Edit "version" in package.json — must be HIGHER than clients have.

# 2. Only if src/python/*.py changed:
npm run build:python

# 3. Build + publish to the public TallyBridge repo:
$env:GH_TOKEN = (gh auth token)
npm run build
npx electron-builder --publish always

# 4. Un-draft so clients can see it (use your new tag):
gh release edit vX.Y.Z --draft=false --repo siddharthniranjan2003/TallyBridge
```

Clients then auto-discover (launch + hourly) and update per the installed build's behavior (auto or click).

---

## 5. Production release state (as of handoff)

- **`package.json` version: `1.2.8`.**
- Repo `siddharthniranjan2003/TallyBridge` (PUBLIC) releases:
  - **`v1.2.8` — Latest (published).** ← what clients update to.
  - `v1.2.7` — **Draft** (leftover, never un-drafted).
  - `v1.2.6` — **Draft** (leftover).
  - `v1.2.4` — published.
- Proven live earlier: installed **1.2.3 → published 1.2.4 → banner → updated to 1.2.4** on the public repo (full end-to-end, real client experience).
- **Test repo `tallybridge-test`** is **PRIVATE** again, with throwaway `v1.2.3`/`v1.2.4` test releases (harmless; delete if tidying).

**Cleanup candidates:** delete draft `v1.2.6`/`v1.2.7`; delete dead `electron-builder.yml`; commit the `package.json` 1.2.8 bump + the uncommitted `updater.ts` automatic-update change.

---

## 6. 413 "Request Entity Too Large" — separate real bug (NOT fixed)

Seen on company **K V ENTERPRISES** sync. Generic Google-frontend HTML 413 (not Express's JSON) = a **fronting proxy rejects the body before Express**. Backend is on **Cloud Run (hard ~32 MB request cap)**.

- **Root cause:** in **render mode** (default), `cloud_pusher.py::push()` sends **all vouchers in ONE POST** to `/api/sync` (no chunking). Chunking (`DIRECT_VOUCHER_CHUNK_SIZE = 2000`) only happens in **direct/hybrid** mode.
- Large company → single payload > 32 MB → 413.
- **Fix options (not yet done):** (a) switch that company's ingest mode to hybrid/direct (config — chunks at 2000); (b) add chunking to the render path in `cloud_pusher.py` (proper fix; verify backend `/api/sync` handles chunk metadata); (c) raise proxy limit (likely impossible on Cloud Run).

---

## 7. Switch Supabase creds to the CLIENT database

**One place controls the whole backend (sync + push queue):** `backend/src/db/supabase.ts` creates a single client from `SUPABASE_URL` + `SUPABASE_SERVICE_KEY`. `sync.ts` uses that same client for `push_queue` (e.g. `sync.ts:2283`). So change those two vars.

- **Backend is deployed on GCP Cloud Run** — service **`tallybridge-backend`**, region **`asia-south1`**, project **`tallybridge-test-ocr`** (containerized via `backend/Dockerfile`). The local `backend/.env` does **not** affect the live service.
- Env vars on the service are **plain** (not Secret Manager): `SUPABASE_URL, SUPABASE_SERVICE_KEY, API_KEY, FIREBASE_SERVICE_ACCOUNT_B64, SUPABASE_URL_Client, SUPABASE_SERVICE_KEY_CLIENT, API_KEY_CLIENT`.
- **To switch:** update `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` on the Cloud Run service (Console → Edit & Deploy New Revision → Variables & Secrets, or `gcloud run services update tallybridge-backend --region asia-south1 --update-env-vars ...`). Deploying a new revision applies it.
- The client's creds may **already** be in `SUPABASE_URL_Client` / `SUPABASE_SERVICE_KEY_CLIENT` (currently used **only** by the `/reorder-levels` endpoint, `sync.ts:13-25`). Could copy those into the main vars.
- **Prerequisite:** apply **`backend/full_schema.sql`** to the client's Supabase first, or push_queue/sync will 404 ("Could not find table public.push_queue").
- Desktop app needs **no change** in render mode — it talks to the backend (`controlPlaneUrl` + `API_KEY`); the backend holds Supabase creds. (`direct`/`hybrid` mode would instead use `SYNC_INGEST_URL` + `SYNC_INGEST_KEY` → the `supabase/functions/ingest-sync` edge function.)

---

## 8. Open decisions / next steps

- [ ] Commit or revert the **uncommitted `updater.ts`** (auto-update vs click). Currently auto, uncommitted.
- [ ] Commit `package.json` 1.2.8 bump.
- [ ] Delete dead `electron-builder.yml` (single config source).
- [ ] Clean up draft releases `v1.2.6`, `v1.2.7` on the prod repo.
- [ ] (If needed) fix the **413** via render-path chunking or per-company ingest mode.
- [ ] (When ready) switch Cloud Run `SUPABASE_URL`/`SUPABASE_SERVICE_KEY` to the client DB + apply schema.

---

## 9. Key file map

| File | Purpose |
|------|---------|
| `src/main/updater.ts` | Auto-update logic (currently automatic, uncommitted) |
| `src/renderer/components/UpdateBanner.tsx` | Update banner UI |
| `src/main/sync-engine.ts` | Spawns Python, port pre-flight, pause/resume |
| `src/python/sync_main.py` | Python entry; read modes, change detection, dispatch |
| `src/python/cloud_pusher.py` | Batches/POSTs to backend (render = unchunked → 413 risk) |
| `backend/src/db/supabase.ts` | The single Supabase client (`SUPABASE_URL`/`SUPABASE_SERVICE_KEY`) |
| `backend/src/routes/sync.ts` | Ingest + `push_queue` access; `*_CLIENT` reorder-levels overrides |
| `backend/Dockerfile` | Cloud Run container for `tallybridge-backend` |
| `package.json` (`build` block) | The LIVE electron-builder config (yml is ignored) |

**GCP:** project `tallybridge-test-ocr`, region `asia-south1`, services `tallybridge-backend` + `tallybridge-parsing`.
