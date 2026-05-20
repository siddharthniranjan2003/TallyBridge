# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture

TallyBridge is a desktop sync application that bridges TallyPrime/ERP9 with Supabase cloud storage. Four components work together:

1. **Electron Main Process** (`src/main/`) orchestrates everything: spawns the Python engine, hosts a local HTTP server on port 3002 for incoming push requests, manages the system tray, and communicates with the renderer via IPC.
2. **React Renderer** (`src/renderer/`) is a Vite + React 19 + TailwindCSS UI running in the Electron renderer process.
3. **Python Sync Engine** (`src/python/`) is spawned as a subprocess by `sync-engine.ts`; it talks to TallyPrime's XML-RPC API on port 9000, falls back to ODBC, and pushes data to the cloud backend.
4. **Express Backend** (`backend/src/`) is the cloud API on port 3001; it is authenticated via Firebase JWT or API key and writes to Supabase.

### Data flows

**Sync (Tally -> Cloud):** `sync-engine.ts` spawns `sync_main.py` -> `tally_client.py` reads from TallyPrime port 9000 -> `cloud_pusher.py` POSTs to `POST /api/sync` on the backend -> Supabase.

**Push (Cloud -> Tally):** `push-queue-poller.ts` polls backend -> backend forwards to `local-push-server.ts` (port 3002) -> spawns `tally_pusher.py` -> TallyPrime port 9000.

### Ingest modes (`SYNC_INGEST_MODE`)

- `render` - Python sends all sections to the cloud backend
- `hybrid` - mixed: some sections direct to Supabase, some via backend
- `direct` - Python writes directly to Supabase (groups, ledgers, stock, outstanding, financials)

### Key config stored in electron-store

`tallyUrl`, `syncIntervalMinutes`, `syncPaused`, `readMode` (`auto`/`xml-only`/`hybrid`/`shadow`), `syncIngestMode`, `companies[]` (array with GUIDs).

## Commands

### Electron desktop app
```bash
npm install          # root - installs all deps
npm run dev          # Vite (port 5173) + Electron in dev mode
npm run build        # build:renderer + build:main -> dist/
npm run dist         # full release build -> release/TallyBridge-*.exe
```

### Backend (cloud API)
```bash
cd backend
npm install
npm run dev          # tsx watch - hot-reload TypeScript on port 3001
npm run build        # tsc -> dist/index.js
npm start            # production: node dist/index.js
```

### Python engine
```bash
# Development (run directly)
cd src/python
python sync_main.py

# Production bundle (PyInstaller)
npm run build:python  # -> python-dist/tallybridge-engine.exe
```

### OCR / parsing scripts (experimental)
```bash
cd parsing
python purchase_paddle_runner.py   # PaddleOCR invoice runner
python n8n_minicpm_server.py       # Flask server for VLM integration
```

## Environment variables

**Root `.env`** (Electron / Vite):
- `VITE_BACKEND_URL` - cloud backend URL

**`backend/.env`**:
- `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`
- `API_KEY` - service-to-service secret (compared with `timingSafeEqual`)
- `FIREBASE_SERVICE_ACCOUNT_B64` - base64-encoded service account JSON
- `PORT` (default 3001), `TB_JSON_BODY_LIMIT` (default 100MB)

## Key files

| File | Purpose |
|------|---------|
| `src/main/sync-engine.ts` | Spawns Python process, handles run-tracking, pause/resume |
| `src/main/local-push-server.ts` | HTTP server on port 3002; receives cloud-initiated push requests |
| `src/main/push-queue-poller.ts` | Polls backend for pending voucher pushes |
| `src/python/sync_main.py` | Python entry point; decides read mode, calls tally/odbc, calls cloud_pusher |
| `src/python/tally_client.py` | XML-RPC client for TallyPrime port 9000 |
| `src/python/cloud_pusher.py` | Batches and POSTs data to backend API |
| `backend/src/routes/sync.ts` | Main ingest endpoint - handles all section types |
| `backend/src/middleware/auth.ts` | Firebase JWT + API key auth |
| `parsing/purchase_paddle_runner.py` | PaddleOCR engine for scanned invoice parsing |

## Tally connectivity

Before any sync, the engine polls TCP port 9000 to confirm TallyPrime is running. This is implemented in `sync-engine.ts` as a port pre-flight check. If Tally is unreachable, sync is skipped instead of crashing.

## Python process lifecycle

`sync-engine.ts` spawns `tallybridge-engine.exe` (production) or `sync_main.py` (development) with env vars passed as `child_process.spawn` environment. The process ID is tracked so pause/resume can send signals. Logs from the Python process are captured and forwarded to the Electron log file.

## OCR pipeline (in development)

`parsing/` contains an experimental invoice OCR pipeline. Current state:
- `purchase_paddle_runner.py` - PaddleOCR two-stage (detect + recognize), handles deskew/denoise/sharpen preprocessing
- Architecture decision: native digital PDFs use `fitz` (PyMuPDF) direct text extraction; scanned PDFs route to a VLM (H2OVL-Mississippi-2B planned via vLLM)
- TIN truncation repair is required on both paths (it is a vendor billing software column-width issue, not an OCR artifact)
