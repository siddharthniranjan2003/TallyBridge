# Session Handout — TallyBridge 503/401 Diagnosis + Control-Plane / Backend Interaction (code-verified)

> Pickup doc for a fresh chat. Covers everything from the last compact:
> the K V ENTERPRISES `503` + `401` diagnosis, the fix that was applied, and the
> code-verified explanation (with diagrams) of how TallyBridge talks to the backend
> and to TallyPrime. Every claim below is traced to a `file:line`.

---

## 0. TL;DR

| Thing | State |
|---|---|
| `[Control][Push] 401 Unauthorized` on K V ENTERPRISES | **FIXED** — Control Plane URL repointed to a backend whose `API_KEY` matches the app's Control Plane API Key |
| `[Ingest][direct-masters-snapshots] HTTP 503` | **NOT fixed / parked by user** — separate target (Direct Ingest URL + `x-sync-key`), not the control plane |
| Control Plane URL now set to | `https://tallybridge-backend-828647628834.asia-south1.run.app` (the **testing** backend → testing Supabase `yynuu`) |
| ⚠️ Open concern | K V ENTERPRISES is now pointed at the **testing** backend. If it's a live client, its queue/alter-id/read traffic is running against testing data, not production `ztugw`. Confirm intent. |

---

## 1. The original errors (from the Electron app log)

```
K V ENTERPRISES
[ERR] [Error] Sync failed: HTTP 503
[Ingest][direct-masters-snapshots] Upload failed: HTTP 503
[Ingest][direct-masters-snapshots] Response: HTTP 503
[Control][Push] Queue fetch failed: HTTP 401
[Control][Push] Response: {"error":"Unauthorized"}
[Push] Outbound push queue check was skipped because the backend queue could not be read (lookup_failed).
```

These are **two independent failures on two different endpoints with two different keys** — not one outage.

---

## 2. Diagnosis (code-verified)

### Key structural finding
The Electron client splits traffic across **two targets, two keys**:

| Log line | Target setting | Key sent | Header |
|---|---|---|---|
| `[Ingest][direct-masters-snapshots]` → **503** | `syncIngestUrl` (DIRECT INGEST) | `syncIngestKey` | `x-sync-key` |
| `[Control][Push]` → **401** | `controlPlaneUrl` (CONTROL PLANE) | `controlPlaneApiKey` | `x-api-key` |

### The 401 (push queue) — app-level, definite
- Push queue GET is guarded by `requireApiKey` (`backend/src/routes/sync.ts:2391`).
- `requireApiKey` compares incoming `x-api-key` to `process.env.API_KEY` with `timingSafeEqual` (`backend/src/middleware/auth.ts:28-41`); mismatch → `401 {"error":"Unauthorized"}`.
- The clean JSON body proves the backend was **up and healthy**, actively rejecting the key.
- Cause: the app's Control Plane API Key didn't match that backend's `API_KEY` (suspected fallout of commit `1671a3e feat(reorder-levels): serve client Supabase + API_KEY_CLIENT via *_CLIENT env`, which reorganized client backend env vars).

### The 503 (sync ingest) — infrastructure, NOT app code
- Grepping the whole backend: it returns **401/500/502 but NEVER 503**. So the 503 comes from the layer *in front* of the app (Cloud Run / Supabase edge function / proxy).
- Likely causes: ingest target down/paused/cold-start fail, or OOM/timeout on the large `direct-masters-snapshots` payload (same OOM/413 class noted in the hybrid-sync migration).
- This path is the **Direct Ingest** target (`SYNC_INGEST_URL` + `x-sync-key`), a different field from Control Plane. **Parked by user for now.**

### The fix that was applied
User set **Control Plane URL** = `https://tallybridge-backend-828647628834.asia-south1.run.app`.
Probe confirmed that backend is alive (returns clean `401` to a bogus key via Google Frontend/Express; `/` → 404 is normal — no root route). Because that backend's `API_KEY` matches the app's Control Plane API Key, the `401` disappeared.

---

## 3. Control Plane URL & Control Plane API Key — purpose (code-verified)

**Control Plane URL** = base URL of the backend the engine sends *coordination* traffic to (not bulk data writes). Becomes env `CONTROL_PLANE_URL` (legacy fallback `BACKEND_URL`).
- injected by Electron: `src/main/push-queue-poller.ts:148-151`, `src/main/sync-engine.ts:368-373`
- read by Python: `src/python/cloud_pusher.py:9-11`, `src/python/sync_main.py:117-119`
- URL builder: `src/python/cloud_pusher.py:125-128` (`_control_plane_url(path)`)

**Control Plane API Key** = the secret that authorizes those requests. Becomes `CONTROL_PLANE_API_KEY`, attached as `x-api-key` on **every** control-plane call (`cloud_pusher.py:131-132`). Backend validates via `requireApiKey` → `timingSafeEqual` vs `process.env.API_KEY` (`auth.ts:28-41`). Mismatch → `401`.

> URL = *where* to send coordination traffic; Key = the credential that authorizes it. Together they authenticate one logical channel on the backend.

### The COMPLETE set of control-plane calls TallyBridge makes (verified — only 4)
`_control_plane_url()` is referenced in exactly 4 places in `cloud_pusher.py` — nothing else:

| # | Purpose | Python caller | Method + path | Backend route |
|---|---|---|---|---|
| 1 | **Change detection** (skip sync if nothing changed) | `fetch_remote_alter_ids()` `cloud_pusher.py:303` | `GET /api/sync/alter-ids` `:313` | `sync.ts:2757` |
| 2 | **Pull push queue** (active cloud→Tally jobs) | `fetch_push_queue()` `cloud_pusher.py:341` | `GET /api/sync/push-queue` `:352` | `sync.ts:2391` |
| 3 | **Report push result** | `store_push_results()` `cloud_pusher.py:390` | `POST /api/sync/push-results` `:399` | `sync.ts:2430` |
| 4 | **Render/hybrid voucher ingest** | `_build_ingest_target("render")` `cloud_pusher.py:155` | `POST /api/sync` `:156` | `sync.ts:1809` |

### What is NOT on this channel / NOT done by TallyBridge (corrections made this session)
- **`POST /api/sync/push-queue/activate`** — done by the **Flutter app** (`aiaccountant/lib/features/queue/voucher_detail_sheet.dart:695` `_activate()` → `Config.activateUrl`, `config.dart:40-43`). It's the human approval gate; TallyBridge never claims/activates. (Backend route exists at `sync.ts:2310` but the TB client has zero references to `activate`.)
- **`GET /api/sync/{vouchers,stock,outstanding,purchases,pnl,balance-sheet,trial-balance,party-ledger,parties}`** — these read APIs (`sync.ts:2510-2843`) are consumed by the **Flutter app**, not TallyBridge. The TB client only calls the 4 paths above.
- **Bulk masters/snapshots** — separate **Direct Ingest** URL (`SYNC_INGEST_URL`) + `x-sync-key` (`cloud_pusher.py:135-149`). This is the 503 path.

### Visual — Control Plane
```
┌──────────── TallyBridge Settings ─────────────┐
│  Control Plane URL ....  https://…run.app      │   →  env CONTROL_PLANE_URL
│  Control Plane API Key   ●●●●●●●●●●             │   →  env CONTROL_PLANE_API_KEY
└───────────────────────┬────────────────────────┘   (poller.ts:148 / sync-engine.ts:368)
                        ▼
        ┌───────────────────────────────────────┐
        │  Python engine (cloud_pusher.py)        │
        │  url = CONTROL_PLANE_URL + path  (:125) │
        │  hdr = { x-api-key: KEY }         (:131) │
        └───────────────────┬─────────────────────┘
                            ▼
        ┌──────────────────────────────────────────────────────────────┐
        │  BACKEND — requireApiKey (auth.ts:28-41)                       │
        │     x-api-key == process.env.API_KEY ? allow : 401 ◄ old error │
        ├──────────────────────────────────────────────────────────────┤
        │  SYNC direction (Tally → Cloud):                               │
        │   ① GET  /api/sync/alter-ids     "has anything changed?"       │
        │   ④ POST /api/sync               render/hybrid vouchers up     │
        │                                                                │
        │  PUSH direction (Cloud → Tally):                               │
        │   ② GET  /api/sync/push-queue    "any approved jobs to push?"  │
        │   ③ POST /api/sync/push-results  "here's what I pushed"        │
        └──────────────────────────────────────────────────────────────┘
```

---

## 4. Full system interaction — TallyPrime ↔ TallyBridge ↔ Backend (code-verified)

### Actors & ports
- **TallyPrime** — XML/HTTP server on `localhost:9000` (`tally_client.py:8` → `TALLY_URL=http://localhost:9000`). Reads AND writes are HTTP-POSTed XML envelopes (`<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>`, `tally_client.py:164,213…`).
- **Electron main** — spawns Python engine; hosts a local push HTTP server on `localhost:3002` (`local-push-server.ts:10,159`).
- **Python engine** — `sync_main.py` (read + push), spawned as subprocess.
- **Backend** — Cloud Run; control plane (`x-api-key`/`API_KEY`) + direct ingest (`x-sync-key`/`SYNC_INGEST_KEY`).

### Flow A — SYNC (TallyPrime → Cloud)
```
TallyPrime                Electron / Python engine                     Backend (Cloud Run)
:9000
  │   ① preflight: TCP probe :9000 (sync-engine.ts) — skip sync if Tally closed
  │◄──────────────────────  sync-engine.ts spawns sync_main.py (env: TALLY_URL,
  │                          CONTROL_PLANE_URL/_API_KEY, SYNC_INGEST_URL/_KEY)
  │
  │  ② read masters+vouchers: HTTP POST XML envelopes
  │◄────────────────────────  tally_client.py:164  (groups/ledgers/stock/
  │   ──XML response────►      vouchers/outstanding/pnl/bs/tb)
  │
  │                       ③ change-detection (skip if nothing changed):
  │                          fetch_remote_alter_ids() ──GET /api/sync/alter-ids──►  :2757
  │                          compares Tally live alter-id vs cloud's last
  │
  │                       ④ push data up, split by ingest mode:
  │                          • direct: masters/snapshots ──POST SYNC_INGEST_URL──►  (x-sync-key)
  │                            (cloud_pusher.py:135-149)        [the 503 path]
  │                          • render/hybrid: vouchers ──POST /api/sync──────────►  :1809 (x-api-key)
  │                            (cloud_pusher.py:155-163)
```

### Flow B — PUSH (Cloud → TallyPrime), with the corrected job lifecycle
Two transports exist; both end at Tally `:9000`. The **activate** step is the Flutter app, not TB.
```
   Parsing / sync                Flutter app (aiaccountant)            TallyBridge engine
   enqueues voucher              user reviews & approves               polls + pushes
        │                                │                                     │
        ▼                                ▼                                     │
   push_queue row    ──────►   POST /push-queue/activate   ──────►   GET /api/sync/push-queue
   (status: pending)            (voucher_detail_sheet.dart:695)       (only picks up ACTIVE jobs)
                                 flips row → active                          │
                                                                            ▼
                                                              builds XML → POST to Tally :9000
                                                                            │
                                                                            ▼
                                                              POST /api/sync/push-results

   WEBHOOK variant (same machine / LAN): backend push-voucher route → client localhost:3002
   /push-voucher (local-push-server.ts:192) → spawns python (tally_pusher) → POST XML → Tally :9000
   (502 "Could not reach local TallyBridge service" if unreachable, push-voucher.ts:99)
```

### Two backend channels recap (same backend URL, different auth)
| Channel | Header / key | Endpoints (called by TB) | Direction |
|---|---|---|---|
| **Control plane** | `x-api-key` = `API_KEY` | `alter-ids`, `push-queue`, `push-results`, `POST /api/sync` | both |
| **Direct ingest** | `x-sync-key` = `SYNC_INGEST_KEY` | `SYNC_INGEST_URL` (masters/snapshots, voucher chunks) | up only |

> Only the Python engine ever touches TallyPrime — always HTTP-POSTing XML to `:9000` (reads for sync, writes for push). The backend never talks to Tally directly. On **sync** the engine reads Tally then pushes up; on **push** it pulls approved jobs (or receives them on local `:3002`) and writes them into Tally as XML vouchers. A TCP pre-flight on `:9000` gates everything so a closed Tally just skips.

---

## 5. Reference: the two auth keys (don't confuse them)

| App field | env var | header | backend check |
|---|---|---|---|
| Control Plane API Key | `CONTROL_PLANE_API_KEY` (a.k.a. `apiKey`) | `x-api-key` | `requireApiKey` vs `process.env.API_KEY` (`auth.ts`) |
| Sync Ingest Key | `SYNC_INGEST_KEY` | `x-sync-key` | direct-ingest target (Supabase edge fn / backend) |
| (reorder-levels only) | `API_KEY_CLIENT` | `x-api-key` | `requireClientApiKey` vs `process.env.API_KEY_CLIENT` (`sync.ts:22-40`) |

---

## 6. Open items for the next chat
1. **503 on Direct Ingest** (parked) — needs the `syncIngestUrl` value for K V ENTERPRISES, then probe it: `curl -i -X POST "<syncIngestUrl>" -H "x-sync-key: probe" -d "{}"`. 401/400 = alive (so 503 was payload/OOM); conn-error/503 = service down/paused.
2. **Production-vs-testing concern** — confirm whether K V ENTERPRISES should be on the testing backend (`828647628834` → `yynuu`) at all, or repointed to the production/client backend (→ `ztugw`).
3. Carryover from prior session (unrelated to this diagnosis): Supabase migration cutover (realtime + `ingest-sync` deploy + repoint), RunPod baking, deployment push-queue cross-link — see `session_handout_riplara_multienv_and_supabase_migration.md`.
