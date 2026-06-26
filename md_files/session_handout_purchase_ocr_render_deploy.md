# TallyBridge — Purchase OCR Pipeline + Render Deployment: Session Handout

Context-transfer doc for a new chat. Covers the purchase-invoice OCR pipeline, the
per-vendor rule engine + live-Supabase matcher, and the **isolated test deployment
on Render**. Read this top-to-bottom to pick up exactly where we left off.

Last updated: 2026-06-01.

---

## 0. TL;DR — what state are we in

- The purchase OCR pipeline is **fully working end-to-end and deployed to Render**, in a
  **brand-new private repo `tallybridge-test`** that is **completely isolated from the
  client's production**.
- A real PDF push was verified: it OCRs, converts via the rule engine + live-Supabase
  match, and inserts a `pending` row into the **test** Supabase `push_queue`. ✅
- The client's production (repo `TallyBridge`, service `tallybridge-h3do`, client Supabase
  `ztugwhevemibdrzqafyw`, client TallyPrime) was **never touched** and is out of the path.
- **Open items:** optional cleanup of old Render services; run the other 7 sample PDFs;
  improve the rule engine (90% → higher); optionally rename the OCR service.

---

## 1. The endpoint and what it does

`POST https://tallybridge-test.onrender.com/docstrange?purchase=all&source=runpod`
(PDF as binary body, `Content-Type: application/pdf`)

- `source=runpod` → Nanonets OCR on RunPod (NOT local DocStrange).
- `purchase=all` → run full pipeline **and** push to queue. (Drop it → raw markdown only,
  no parse, no push.)

### End-to-end flow
```
PDF
 → OCR service renders each page to JPEG (Poppler, 200 DPI)
 → RunPod Nanonets-OCR2-3B (vLLM 0.9.2) returns markdown per page
 → detect_vendor() → per-vendor RULE ENGINE (build_candidate_queries) → canonical Tally name
 → fuzzy match vs LIVE Supabase stock_items (match_item_to_live_stock) → nearest real item
 → build_voucher_payload() → voucher (party, date, voucher_no, ledger_entries, items)
 → POST {tally_payload:{...}} to backend /api/sync/push-queue (x-api-key auth)
 → backend inserts row into Supabase push_queue (status=pending) → returns {success, job}
```
Response is one big JSON: `vendor`, `voucher_payload`, `matched_items` (w/ scores),
`summary.master_source="supabase_live_stock"`, `queue_response` (the backend's `{success,job}`
= proof it landed), `markdown` (raw OCR), `runpod` (timing).

**The two-stage conversion is the heart of it:**
- Stage 1 = **per-vendor deterministic rule engine** (`parsing/purchase/voucher_builder.py:build_candidate_queries`)
  transforms raw OCR text → canonical Tally name (e.g. `M2 HSS PSTD 8.9mm → HSS DRILL 8.9 ADDISON`).
- Stage 2 = **fuzzy match vs live stock** (`match_item_to_live_stock`) scores the canonical
  name against the company's full Supabase `stock_items` (thousands of rows) → picks nearest.
- (An exact-map shortcut via `purchase_matching_api`/`Purchase_Matching` runs first if the
  description is a known pair.)

---

## 2. The deployed architecture (Render)

Two services, both from the **private repo `github.com/siddharthniranjan2003/tallybridge-test`**,
branch `main`, region Oregon:

| Service | Render name | URL | Runtime / Root dir |
|---|---|---|---|
| OCR pipeline | `tallybridge-test` | `https://tallybridge-test.onrender.com` | Docker / `parsing` |
| Test backend | `tallybridge-test-backend` | `https://tallybridge-test-backend.onrender.com` | Node / `backend` |

- OCR is CPU-only (the GPU OCR runs on RunPod). Backend is the Express API that writes to Supabase.
- **They connect via ONE env var:** the OCR service POSTs to `MINICPM_PUSH_QUEUE_URL`
  (= the backend URL + `/api/sync/push-queue`), authenticated by `MINICPM_PUSH_QUEUE_API_KEY`
  which must equal the backend's `API_KEY`. It's just an HTTPS call — no private networking.

### Env vars
**OCR service (`tallybridge-test`):**
```
RUNPOD_POD_URL=https://api.runpod.ai/v2/vllm-5fjqw6zdgqxt8g/openai
RUNPOD_POD_API_KEY=<rpa_ full-access key>
RUNPOD_MODEL=nanonets/Nanonets-OCR2-3B
RUNPOD_TIMEOUT_SECONDS=600
SUPABASE_URL=https://yynuuysvjeipawzfbeme.supabase.co        # TEST project
SUPABASE_SERVICE_KEY=<test service_role key>
MINICPM_PUSH_QUEUE_URL=https://tallybridge-test-backend.onrender.com/api/sync/push-queue?company_name=K+V+ENTERPRISES
MINICPM_PUSH_QUEUE_API_KEY=sb_publishable_c2Cov2CT8hxOd1FFEo_yaA_6nEt8_Nl
```
(`MINICPM_HTTP_HOST=0.0.0.0` and `MINICPM_RUNPOD_PDF_DPI=200` are baked into `parsing/Dockerfile`.)

**Test backend (`tallybridge-test-backend`):**
```
SUPABASE_URL=https://yynuuysvjeipawzfbeme.supabase.co        # TEST project
SUPABASE_SERVICE_KEY=<test service_role key>
API_KEY=sb_publishable_c2Cov2CT8hxOd1FFEo_yaA_6nEt8_Nl        # must match OCR's MINICPM_PUSH_QUEUE_API_KEY
FIREBASE_SERVICE_ACCOUNT_B64=<same as client backend; required or app crashes on boot>
```
Backend build: `npm install && npm run build`, start: `npm start`, health: `/health`.

---

## 3. The databases — DO NOT CONFUSE (safety-critical)

| Supabase project ref | Which | Used by |
|---|---|---|
| `yynuuysvjeipawzfbeme` | **TEST** | test backend + OCR service + local dev |
| `ztugwhevemibdrzqafyw` | **CLIENT PRODUCTION** | client backend `tallybridge-h3do` → client TallyPrime |

⚠️ A successful push to the **client** backend would flow into the client's real TallyPrime.
The whole point of the test repo + test backend is to never touch `ztugwhevemibdrzqafyw`.
**Rule: the OCR service's `MINICPM_PUSH_QUEUE_URL` must point at the TEST backend, never `tallybridge-h3do`.**

---

## 4. Why a separate repo (the key decision)

Originally we deployed onto branch `firebase-implementation-backend` of the client repo
`TallyBridge`. Problem: the **client backend auto-deploys from that same branch**, so any code
fix risked triggering a client redeploy. Solution = **`tallybridge-test` private repo** holding
`backend/` + `parsing/`, fully decoupled. All future test code changes commit there; the client
repo/branch is never touched again.

The repo was seeded from the local branch **`TallyBridge-Backend-Refactor`** (HEAD `ae5872a`),
which has the correct/newer code (see §5). No `.env` is committed; secrets live only in Render env.

---

## 5. The bug that blocked it (and the fix)

The push kept failing with **HTTP 400 "company_name required"**. Root cause: the
`firebase-implementation-backend` branch had an **older `backend/src/routes/sync.ts`** whose
`POST /push-queue` read `company_name` from the **flat body**, but the OCR pipeline sends it
**nested in `tally_payload`**. The **newer `sync.ts`** (on `TallyBridge-Backend-Refactor`)
accepts the `tally_payload` shape (reads `rawBody.tally_payload ?? rawBody ?? rawQuery`) and adds
a `POST /push-queue/activate` route — fully backward-compatible. The `tallybridge-test` repo uses
this newer version, which is why it works.

Failure-mode history (all were config/contract, not OCR):
- 401 Unauthorized → `MINICPM_PUSH_QUEUE_API_KEY` ≠ backend `API_KEY`.
- 429 Too Many Requests → transient free-tier/edge throttle (cold start).
- 404 "Cannot POST /" → `MINICPM_PUSH_QUEUE_URL` missing the `/api/sync/push-queue` path.
- 400 "company_name required" → the `tally_payload` contract mismatch above (the real blocker).
- 404-on-everything (old test backend) → was a transient cold-start, NOT a real bug.

---

## 6. The conversion pipeline internals (for improving accuracy)

Benchmark: **47/52 items = 90% exact** vs the per-vendor answer-key CSVs in
`D:\Downloads\image sample\*.csv` (CSVs are ground-truth only, NOT used in the pipeline).
Per vendor: ADDISON 4/4, CP 6/6, EMKAY 3/4, PIDILITE 1/1, RR 22/24, SAINT_GLOBAL 2/3,
TOTEM 6/6, WIKUS 3/4.

Rule-engine status is documented in `md_files/purchase_rule_engine_status.md`. Highlights:
- All 8 vendors have a rule branch in `build_candidate_queries` (ET/ADDISON HIGH confidence,
  RR/TOTEM/GNL MEDIUM, CP/PIDILITE/STANLEY/WIKUS newer).
- Spec for the rules: `D:\Downloads\Vendor_Matching_Algorithms_Detailed_v4.docx`.
- **#1 improvement (highest impact):** `build_candidate_queries` appends a raw-text fallback
  query; in Stage 2 the raw query sometimes OUT-scores the correct canonical query and lands on
  a wrong item (the EMKAY/RR misses). Fix = prefer/weight the canonical query over the raw
  passthrough in `match_item_to_live_stock`.
- Other gaps: master-list-dependent rules (decimal forms, OD tables, HSS-vs-HSS-E routing,
  GNL dimension orientation) not derivable from text; some misses are upstream OCR variance.

### DPI finding (important)
Nanonets (Qwen2.5-VL) has a fixed visual-token budget. **200 DPI is the sweet spot** for these
A4 invoices (recovers ADDISON/WIKUS dense item tables). 300 DPI dropped those tables; 400 DPI
collapsed output entirely. The Dockerfile ships `MINICPM_RUNPOD_PDF_DPI=200`. (Local default is
still 300 in code; pass the env var to override.)

---

## 7. RunPod / OCR engine gotchas (don't relearn)

- **vLLM 0.9.2 ONLY** serves Nanonets-OCR2-3B. 0.20.x crashes (`lm_head not initialized`) /
  garbage `!!!!`; 0.17.1 hangs. Custom worker pinned to 0.9.2 required.
- Custom worker repo: `github.com/siddharthniranjan2003/tb-vllm-worker`. Serverless endpoint
  `vllm-5fjqw6zdgqxt8g`. Pod fallback `snwslqwd4kzglr` (~$0.27/hr, stop when idle).
- Nanonets takes **images only**; PDFs rasterized our side (Poppler), each page OCR'd separately.
- Custom serverless workers don't proxy the OpenAI route → handler uses native `/run`+`/status`
  (auto-detected by `api.runpod.ai/v2` in `RUNPOD_POD_URL`).
- Local dev only: after editing parser code, restart 5003 AND clear non-venv `__pycache__`
  (stale bytecode silently served old code repeatedly).

---

## 8. Key files

| File | Purpose |
|---|---|
| `parsing/server/handler.py` | HTTP server (5003 local); RunPod call, DPI render, /docstrange route, push |
| `parsing/purchase_docstrange_pipeline.py` | Orchestrates markdown → vendor parse → live match → push payload |
| `parsing/purchase/voucher_builder.py` | **Rule engine** (`build_candidate_queries`) + live matcher (`match_item_to_live_stock`, `build_voucher_payload`) |
| `parsing/purchase/company_context.py` | `resolve_supabase_company_context` — fetches live stock_items + ledgers |
| `parsing/purchase/vendor.py` | `detect_vendor()` keyword → vendor code |
| `parsing/Dockerfile` | Render Docker build (python:3.11-slim + poppler-utils, 0.0.0.0:$PORT, DPI=200) |
| `backend/src/routes/sync.ts` | `/api/sync/*` incl. push-queue (NEWER version accepts tally_payload) |
| `backend/src/db/supabase.ts` | `createClient(SUPABASE_URL, SUPABASE_SERVICE_KEY)` — env-driven |
| `md_files/purchase_rule_engine_status.md` | Per-vendor rule-engine coverage + gaps |

---

## 9. Git / repo state

- **Local working branch:** `TallyBridge-Backend-Refactor`. HEAD `ae5872a`
  ("feat(parsing): live Supabase stock matcher + Render deploy files"). The live-matcher rewire
  (`purchase_docstrange_pipeline.py` now calls `resolve_supabase_company_context` +
  `build_voucher_payload`, CSV path removed) is committed here.
- **`firebase-implementation-backend`** (client deploy branch): pushed to origin at `01b419a`
  (we added `parsing/` there earlier — additive, backend untouched). The client backend still
  works; we did NOT change its `sync.ts`. We chose to stop using this branch for testing.
- **`tallybridge-test`** (NEW private repo): seeded from `TallyBridge-Backend-Refactor`, contains
  `backend/` + `parsing/` + `.gitignore` + `README.md`. Initial commit `cbaba57`. **This is the
  repo to commit future test changes to.** Both Render test services deploy from its `main`.

---

## 10. How to test (safe — writes only to TEST Supabase)

```powershell
# full pipeline + push to test push_queue
$r = Invoke-WebRequest -Uri "https://tallybridge-test.onrender.com/docstrange?purchase=all&source=runpod" `
  -Method Post -InFile "D:\Downloads\image sample\cp.pdf" `
  -ContentType "application/pdf" -TimeoutSec 600 -UseBasicParsing
($r.Content | ConvertFrom-Json) | Select vendor, status,
  @{n='items';e={($_.voucher_payload.items).Count}},
  @{n='queue_ok';e={$_.queue_response.ok}}
```
Success = HTTP 200, `master_source=supabase_live_stock`, `queue_response.ok=true`, and a
`pending` row in test Supabase `push_queue`. Free Render instances cold-start (~30–60s); warm
both URLs first. Verify rows via Supabase MCP (project `yynuuysvjeipawzfbeme`):
```sql
select id, status, voucher_payload->>'voucher_number' vch,
       jsonb_array_length(voucher_payload->'items') items, created_at
from push_queue order by created_at desc limit 10;
```
8 sample PDFs: `D:\Downloads\image sample\{addison,cp,emkay,pidilite,rr,saint global,totem,wikus}.pdf`.

---

## 11. PICK UP HERE — open items / next steps

1. **(Optional) Delete old/duplicate Render services** now superseded:
   `TallyBridge-1(ocr)` (`...1-ocr`) and `TallyBridge_duplicate_(testing)` (`...testing-2ok7`).
   Leave the client's `TallyBridge`/`tallybridge-h3do` alone.
2. **Run the remaining 7 sample PDFs** through `tallybridge-test` to confirm all 8 land in
   test `push_queue` (expect ~90% item accuracy, ADDISON/WIKUS recovered at 200 DPI).
3. **Improve the rule engine** — start with the #1 fix in §6 (prefer canonical query over raw
   fallback in `match_item_to_live_stock`). Then port remaining v4-manual rules per vendor.
   Commit changes to the **`tallybridge-test`** repo (auto-deploys to the test services).
4. **(Optional) Rename the OCR service** — `tallybridge-test` is a confusing name (looks like the
   "main" one). Render Settings → General → Name. NOTE: the subdomain/URL may stay fixed after
   create; if so, delete+recreate to get a clean URL. Whatever the URL becomes, only your own test
   calls need updating (nothing internal depends on the OCR service's URL).
5. **Local servers** (5003 OCR + 3001 backend) are currently DOWN. To run locally:
   `cd D:\Desktop\TallyBridge\parsing ; py -3.11 n8n_minicpm_server.py --serve` (prefix
   `MINICPM_RUNPOD_PDF_DPI=200`) and `cd backend ; npm run dev`. Local push targets local 3001 →
   test Supabase (safe; `backend/.env` SUPABASE_URL is the test project).

---

## 12. Quick reference — all the moving parts

| Thing | Value |
|---|---|
| OCR endpoint | `https://tallybridge-test.onrender.com/docstrange?purchase=all&source=runpod` |
| Test backend | `https://tallybridge-test-backend.onrender.com` |
| Test repo (private) | `github.com/siddharthniranjan2003/tallybridge-test` (branch `main`) |
| Test Supabase | `yynuuysvjeipawzfbeme` (push_queue, stock_items, ledgers, purchase_matching_api) |
| Client backend (DO NOT TARGET) | `https://tallybridge-h3do.onrender.com` |
| Client repo/branch | `TallyBridge` / `firebase-implementation-backend` |
| Client Supabase (DO NOT WRITE) | `ztugwhevemibdrzqafyw` |
| RunPod serverless endpoint | `vllm-5fjqw6zdgqxt8g` (vLLM 0.9.2 Nanonets-OCR2-3B) |
| Render API_KEY / push key | `sb_publishable_c2Cov2CT8hxOd1FFEo_yaA_6nEt8_Nl` |
| Source-of-truth branch | `TallyBridge-Backend-Refactor` (HEAD `ae5872a`) |
| 8 sample PDFs + answer CSVs | `D:\Downloads\image sample\` |
| Vendor rules spec | `D:\Downloads\Vendor_Matching_Algorithms_Detailed_v4.docx` |
| Rule-engine status doc | `md_files/purchase_rule_engine_status.md` |
