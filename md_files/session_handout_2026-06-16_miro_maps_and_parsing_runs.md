# Session Handout — Miro System Maps, Endpoint Maps & Parsing Test Runs (2026-06-16)

**Purpose:** complete handoff for a new chat. Covers everything done after the last compact: building a multi-diagram Miro board of the whole TallyBridge + aiaccountant system, code-verified corrections to the architecture, the full GCP/Supabase/RunPod environment map, and a live debugging session sending a purchase PDF through both riplara parsing environments. A new session should be able to resume from this file alone.

> Companion docs: `handoff_2026-06-11_architecture_review.md` (the prior architecture review + roadmap) and the memory files under `…/memory/`. This handout supersedes a couple of facts in the 06-11 one (noted inline).

---

## 1. The Miro board (primary deliverable)

**Board:** https://miro.com/app/board/uXjVHGfjArM=/ — *"TallyBridge — System Interaction Map (Current + Recommended)"*

It became the single canvas for **all** the visual work. Items, with deep links:

| Item | Jump link (`?moveToWidget=`) | What it shows |
|---|---|---|
| **START HERE** doc | `3458764675411839506` | Legend, two-repo overview, ports/transport table, problems, recommendations |
| **1 — Component & Interaction Map** (flowchart) | `3458764675411755054` | Every runtime/port/protocol grouped by where it physically runs |
| **2 — Sync flow** (sequence) | `3458764675411755276` | Tally → Electron/Python → backend → Supabase (render vs hybrid/direct) |
| **3 — Push flow** (sequence) | `3458764675411755548` | Mobile scan → parsing → push_queue → 5 s desktop poll → Tally → results |
| **4 — Recommended Target Arch** (flowchart) | `3458764675411755766` | 1 Supabase/client + tenant registry + realtime push |
| **5 — Endpoint & API Interaction Map** (flowchart) | `3458764675412476683` | App ↔ Backend ↔ TallyBridge ↔ TallyPrime, every route as a labeled edge |
| **Endpoint Reference** doc | `3458764675412573050` | Exhaustive table: method · path · caller · auth · target · purpose |
| **Per-action flows** intro doc | `3458764675413306931` | UX-topic index + "which actions are Supabase-direct vs backend" |
| **T1 — Sign in** (sequence) | `3458764675413306986` | Firebase phone/OTP; Supabase init at launch |
| **T2 — Scan invoice** (sequence) | `3458764675413307253` | Upload → Parsing → match → push_queue → realtime |
| **T3 — Review & edit voucher** | `3458764675413307337` | Queue detail reads/edits/deletes push_queue directly |
| **T4 — Push to Tally** | `3458764675413307473` | The only flow spanning all five parties |
| **T5 — Live queue & history** | `3458764675413307689` | Passive Supabase realtime subscriptions |
| **T6 — Reports & export** | `3458764675413307817` | Backend CSV (reorder-levels → ztugw) |
| **6 — Parsing backend: one/many requests** | `3458764675480158873` | Concurrency model (see §5) |

**Miro MCP note:** the server requires re-auth each session — only `mcp__miro__authenticate` / `complete_authentication` load until authorized, then `board_create` / `diagram_create` / `doc_create` / etc. appear. Use `diagram_get_dsl` before `diagram_create`. `board_create` was **rejected** (likely a plan board-count limit), so everything lives on the one board as separated regions.

---

## 2. Code-verified corrections (trust these over the 06-11 handoff)

1. **Backend mounts THREE routers** (`backend/src/index.ts`): `/api/sync`, `/api/push-voucher`, `/api/push-invoice`, plus `GET /health`. The 06-11 handoff only knew about `/api/sync`.
2. **`/api/push-voucher` + `/api/push-invoice` are loopback relays** — they forward to `http://127.0.0.1:3002/push-voucher` (the Electron `local-push-server`). Unreachable from Cloud Run → effectively **dead in the deployed backend**, local/dev only. `push-invoice` builds a GST SALE voucher from items first.
3. **`config.dart` is NOT hardcoded to ztugw anymore.** It's **env-injected** via `String.fromEnvironment` / `--dart-define-from-file`. Bare-build **defaults = testing**: Supabase `yynuu` + backend `tallybridge-backend-828647628834` (testing.riplara). Only `mrpApiKey` (x-api-key for reorder reports) still defaults to the **ztugw** publishable key `sb_publishable_IRsM…`.
4. **The Flutter app is mostly a Supabase-direct client.** Scanning (via Parsing), the live queue, voucher review/edit (`push_queue` read/update/delete), and history all bypass the backend. **Only Push-now/activate and Reports go through the backend; only Push-now reaches TallyPrime.**
5. **`sync.ts` route map** (line numbers): `POST /` 1809 (ingest), `push-queue` 2239, `push-queue/activate` 2310, `GET push-queue` 2391, `push-results` 2430, reports — party-ledger 2510, vouchers 2617, outstanding 2636, stock 2659, purchases 2677, pnl 2696, balance-sheet 2716, trial-balance 2736, alter-ids 2757, parties 2783; `reorder-levels` 2845 + `/:reportKey` 2872 (both `requireClientApiKey`).
6. **Auth** (`middleware/auth.ts`): `requireApiKey` = Firebase Bearer **or** `x-api-key === API_KEY` (timingSafeEqual). `requireClientApiKey` = `API_KEY_CLIENT` (the ztugw path).
7. **Python `TB_COMMAND` dispatch** (`sync_main.py`): `sync` / `poll_push_queue` / `push_voucher`. Ingest modes: `render` (whole payload → `POST /api/sync`) vs `hybrid`/`direct` (chunks → Supabase Edge Fn via `SYNC_INGEST_URL` + `x-sync-key`, **bypassing the backend**).
8. **Electron main:** `index.ts` runs `SyncEngine`, `LocalPushServer` (:3002 loopback), `PushQueuePoller` (spawns the Python exe every 5 s, `TB_COMMAND=poll_push_queue`), tray, IPC, updater; one-time `migratedToHybridV1` flip.

---

## 3. Environment map (CRITICAL reference — projects, URLs, DBs, RunPod)

> Project **number** is what's embedded in the Cloud Run hostname: `tallybridge-<svc>-<projnumber>.asia-south1.run.app`.

### A. testing.riplara — project `tally-bridge-testing-env` (number **828647628834**)
- gcloud config `testing-riplara`, account `testing.riplara@gmail.com`. Org: **Riplar Ventures Private Limited**.
- **Parsing:** `https://tallybridge-parsing-828647628834.asia-south1.run.app`
  - Sale: `/?type=sale&push=queue` · Purchase: `/docstrange?purchase=all&source=runpod`
- **Backend:** `https://tallybridge-backend-828647628834.asia-south1.run.app`
- **Supabase:** `yynuu` (testing). push_queue writes land in **yynuu**.
- **RunPod:** Nanonets `vllm-oubuuwydlz9t7b`, MiniCPM `vllm-l3ra5g9jipnoeu`.
- **Billing:** was **inactive** → caused instant 503s; **activated 2026-06-16** (₹28,710 credits, ₹271 used, exp 2026-09-10). The Flutter testing-flavor default backend.

### B. deployment.riplara — project `tally-bridge-deployment-env` (number **822222628942**)
- gcloud config `deployment-riplara`, account `deployment.riplara@gmail.com`.
- **Parsing:** `https://tallybridge-parsing-822222628942.asia-south1.run.app` (same route suffixes)
- **Backend:** `https://tallybridge-backend-822222628942.asia-south1.run.app` (**healthy**)
- **Parsing service env (read via gcloud 2026-06-16):** `SUPABASE_URL/SUPABASE_KEY = ztugw` with an **anon** key (JWT role=anon); `MINICPM_PUSH_QUEUE_API_KEY = sb_publishable_IRsM…` (ztugw publishable). `MINICPM_PUSH_QUEUE_URL` **was wrongly pointing at the PROD backend 950406969086 (DOWN)** → **repointed by user to `…822222628942…/api/sync/push-queue`** on 2026-06-16.
- **RunPod:** Nanonets `vllm-e2026vl152kynt`, MiniCPM `vllm-9px3t17t9fn1y7` (`SALE_OCR_BACKEND=runpod`).

### C. Older testing — project `tallybridge-testing-env` (number **366926737745**)
- gcloud config `default`, account `rohan.psom@gmail.com`. Uses **PROD RunPod**. (Different project from A despite the similar name — easy to confuse.)

### D. Prod / client backend — number **950406969086**
- `https://tallybridge-backend-950406969086.asia-south1.run.app` — **DOWN (503 on /health, instant) as of 2026-06-16.** Needs investigation (billing / traffic-to-latest / crash). The aiaccountant prod/client config historically points here.

### Supabase identity
- `yynuu` (`yynuuysvjeipawzfbeme`) = **TESTING** db. `ztugw` (`ztugwhevemibdrzqafyw`) = **CLIENT/prod** db. (06-11 user-confirmed; an earlier note had these reversed.)

---

## 4. The parsing test runs (cp.pdf)

**File:** `D:\Downloads\image sample\cp.pdf` (1.38 MB). **Vendor:** CP GRAT-EX MANUFACTURING COMPANY · **Invoice:** 1280/2025-26 · **Date:** 10-Mar-26 · **Invoice total (read):** ₹268,611.06 (IGST). **Buyer/company:** K V ENTERPRISES (default; override with `&company=<name>`).

**How it was sent (raw PDF body works):**
```
curl.exe -s -S -X POST "<parsing-base>/docstrange?purchase=all&source=runpod" \
  -H "Content-Type: application/pdf" --data-binary "@D:\Downloads\image sample\cp.pdf" \
  --max-time 580 -o <out.json> -w "HTTP %{http_code} in %{time_total}s, %{size_download} bytes\n"
```
`/docstrange` accepts raw `application/pdf`, `multipart/form-data`, or JSON (`image_base64`/`file_base64`). `purchase=all` auto-enqueues to push_queue.

### Run 1 — testing.riplara (after billing activation)
- **HTTP 200 in 164 s** (RunPod ~11 s inference). Saved: `D:\Desktop\TallyBridge\_cp_purchase_resp.json`.
- 6/6 items matched, **1 weak match: HEX HOLD KIT (10.94%)**; `invoice_exists=false`.
- Built voucher: Purchase, total **₹266,540.16** (PURCHASE GST 225,881.50 + IGST 40,658.66), `discount_total` 146,663.5.
- **Enqueued** → `tallybridge-backend-828647628834`, **job `64e37449-dfb5-4d6a-9d30-d710462d8e82`**, status pending.
- Items: C-10 TIN DEBURRING BLADES 20@950 (40%); CS-30 3 FLS COUNTERSINK TOOL 2@2300 (30%); CSH HANDLE 50@200 (30%); C-15 DEBURING BLADE 40@750 (40%); HEX HOLD KIT 1@8945 (30%, weak); C-10 DEBURING BLADE 500@600 (40%).
- ⚠ Voucher total 266,540.16 ≠ invoice 268,611.06 (~₹2,071 gap — built from matched Supabase rates/discounts, not raw invoice figures).

### Run 2 — deployment.riplara (after user repointed push-queue URL)
- **HTTP 200 in 70 s** (RunPod ~8 s). Saved: `D:\Desktop\TallyBridge\_cp_purchase_resp_deploy.json`.
- 6/6 matched, **0 weak**; `invoice_exists=true` (already seen — informational, still enqueued).
- **Enqueued** → `tallybridge-backend-822222628942`, **job `ce590d81-3e11-4e6c-b3d9-61f39c374cdd`**, status pending.

Both vouchers are `pending` in their respective push_queues (visible in the app; pushable to Tally by a polling desktop).

---

## 5. Parsing backend request handling (concurrency model)

From `parsing/server/handler.py`:
- Server = Python stdlib **`ThreadingHTTPServer`** (`handler.py:2192`), one process, handler `MiniCPMHandler`. **One thread per request — no pool, no Lock, no in-process queue, no back-pressure.**
- Routes: `POST /` = **sale**, `POST /docstrange` = **purchase**, `POST /raw-vlm` = vendor previews (testing), `GET /` = info/404.
- Per request: read body (≤10 MB) → write to `uploads/` → render PDF (poppler/`pdf2image`, 300 DPI for the RunPod path) → **call RunPod and block** (serverless: `/run` then poll `/status` ~3 s up to `RUNPOD_TIMEOUT_SECONDS`; the riplara envs set this to 2000) → match party/items vs Supabase → build GST voucher → `POST` backend `/api/sync/push-queue`.
- **The real concurrency limit is RunPod, not the parser.** Threads are I/O-bound waiting on RunPod. Serverless = job queue + worker autoscale (cold starts); pod = one vLLM doing continuous batching. Cloud Run also autoscales instances above the thread layer. No rate-limit means a burst = a burst of threads + RunPod jobs (cost/cold-starts), and the client connection is held for the whole inference (≤180 s typical).

**Side recommendation given:** for the two serverless workers, **keep baking the model into the image** rather than RunPod Network Storage — a network volume pins the endpoint to one datacenter, which hurts scarce 48 GB MiniCPM availability more than it helps. If rebuilds hurt, put weights in a stable lower Docker layer.

---

## 6. Issues found & fixed this session

1. **Testing parsing 503 (instant).** Cause: the **billing account was not activated** ("requires ₹1,000 one-time payment" + "Verify now"). A suspended billing account disables Cloud Run on all linked projects. **Fix:** user paid + verified → services resumed.
2. **Deployment enqueue failed (HTTP 400 wrapping a backend 503).** Parse succeeded, but the parsing service's **`MINICPM_PUSH_QUEUE_URL` pointed at the PROD backend `950406969086` (DOWN)** instead of the deployment backend. **Fix:** user repointed it to `…822222628942…`.
3. **Prod backend `950406969086` is DOWN (503)** — still open.

**Diagnostic rules learned (now in memory):**
- **503 in <0.2 s = service disabled** (billing inactive / no-traffic revision / crash loop). **503 after a long wait = cold start** (Cloud Run would normally queue).
- For enqueue failures: **read the parsing service's actual `MINICPM_PUSH_QUEUE_URL` and probe THAT backend's `/health`** — don't assume it's the same-project backend.
- Read a service's env: `gcloud run services describe tallybridge-parsing --region asia-south1 --configuration <cfg> --format="value(spec.template.spec.containers[0].env)"`. gcloud configs: `default`=rohan.psom, `testing-riplara`, `deployment-riplara`.

---

## 7. Memory updates made this session

- `aiaccountant_repo_relationship.md` — corrected `config.dart` to env-injected (not hardcoded ztugw); added the 3-router + loopback-relay note.
- `miro_system_interaction_board.md` — **created**: board URL, diagram inventory, Miro re-auth note.
- `riplara_testing_deployment_envs.md` — added the **billing gotcha** (instant-503 tell) and the **deployment push-queue misconfig + fix** (was → prod 950406969086 DOWN, repointed → 822222628942; ztugw anon-key note).
- `MEMORY.md` — added the Miro board index line.

---

## 8. Open items / next steps

- [ ] **Investigate prod backend `950406969086` (503)** — billing? `update-traffic --to-latest`? crash? Anything still pointing at it will fail.
- [ ] **Weak match: HEX HOLD KIT (10.94%)** in the testing run — review/correct the stock mapping before pushing to Tally. (Deployment run matched it clean — worth understanding why they differ.)
- [ ] **Voucher-vs-invoice ~₹2,071 gap** — reconcile the rate/discount logic vs raw invoice totals.
- [ ] Both queued vouchers (`64e37449…`, `ce590d81…`) are `pending` — review in app / push to Tally as desired.
- [ ] **Deployment Supabase intent unresolved:** parsing reads ztugw (anon) but the deployment backend's push_queue write target needs confirming (testing inherited yynuu). Decide whether deployment should be prod-facing.
- [ ] The **06-11 architecture roadmap still stands** (split `sync.ts`, push realtime to replace 5 s spawn-poll, tenant registry + `getSupabaseForTenant()`, migration fan-out, onboarding). See `handoff_2026-06-11_architecture_review.md`.

---

## 9. Quick reference — sending a PDF

```
# Purchase (auto-enqueues):
curl.exe -s -S -X POST "<PARSING_BASE>/docstrange?purchase=all&source=runpod[&company=<NAME>]" \
  -H "Content-Type: application/pdf" --data-binary "@<path-to.pdf>" --max-time 580 \
  -o resp.json -w "HTTP %{http_code} in %{time_total}s\n"

# Sale:
curl.exe ... -X POST "<PARSING_BASE>/?type=sale&push=queue" ...

# Bases:  testing  = https://tallybridge-parsing-828647628834.asia-south1.run.app
#         deploy   = https://tallybridge-parsing-822222628942.asia-south1.run.app
```
Parse the response with `ConvertFrom-Json`; key fields: `ok`, `summary`, `matched_items`, `weak_matches`, `voucher_payload`, `queue_response.body.job` (id/status), `duplicacy.invoice_exists`.
