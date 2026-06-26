# TallyBridge — Sale Path: MiniCPM Wiring + Auto Push to push_queue: Session Handout

Context-transfer doc for a new chat. Picks up after the GCP migration + discount handout
(`session_handout_gcp_migration_discount.md`). This session did three big things:
1. **Wired the MiniCPM-V 4.5 RunPod worker into the `POST /?type=sale` path** (replacing local Ollama).
2. **Moved sale stock matching to live Supabase + added PDF input.**
3. **Made sale fully automated like purchase**: one PDF → OCR → match → rate lookup → build Sales voucher → **push to `push_queue`** → return response. (The old Google-Sheet human-review flow was scrapped.)

Last updated: 2026-06-02.

---

## 0. TL;DR — current state

- **Sale now works end-to-end and is live on GCP**, mirroring purchase. Send one PDF to one
  endpoint; it pushes a `GST SALE` voucher into the TEST Supabase `push_queue`.
- Live-verified on `D:\Downloads\sale sample\balaji_sale.pdf`: `queue_response.ok=true`,
  push_queue row `9fb5d367-…` (`pending`, GST SALE, BALAJI H/W AGENCIES, 20 items, 19/20 priced).
- OCR backend for sale = **MiniCPM-V 4.5 on RunPod** (`vllm-vhm6qdmcjavvps`), the GPU worker
  uploaded this round. Stock + party + rate data all read live from **TEST Supabase**.
- ⚠️ **All of this session's code is deployed to GCP from local source but NOT git-committed yet** (see §7).

---

## 1. The endpoints (GCP Cloud Run)

**Sale — full pipeline + push (the new one):**
```
POST https://tallybridge-parsing-950406969086.asia-south1.run.app/?type=sale&push=queue
Content-Type: application/pdf        (PDF or image as raw binary body)
```
- `&push=queue` → OCR + match + rate lookup + build Sales voucher + **push to push_queue**, returns `queue_response`.
- Plain `?type=sale` (no `push=queue`) → returns parsed JSON only, **no push** (unchanged behavior).
- `&company=<name>` optional → which company's stock to match against (default `K V ENTERPRISES`).

**Purchase (unchanged):**
```
POST https://tallybridge-parsing-950406969086.asia-south1.run.app/docstrange?purchase=all&source=runpod
```

**Backend (push target, unchanged):** `https://tallybridge-backend-950406969086.asia-south1.run.app`

PowerShell:
```powershell
Invoke-WebRequest -Uri "https://tallybridge-parsing-950406969086.asia-south1.run.app/?type=sale&push=queue" `
  -Method Post -InFile "D:\Downloads\sale sample\balaji_sale.pdf" -ContentType "application/pdf" `
  -TimeoutSec 1200 -UseBasicParsing
```

---

## 2. The sale flow (what runs on each call)

```
PDF (binary body)
 → materialize_upload → Poppler renders PDF to a JPEG (PDF input now allowed for sale)
 → run_pipeline_for_image (handler.py):
     • load_sale_stock(company) → LIVE Supabase stock_items (CSV fallback)
     • query_ollama_chat → MiniCPM-V 4.5 (RunPod) reads line items
     • parse_ocr_items → build_prediction_rows (rule-engine + rapidfuzz family-view match)
     • extract_party_name → MiniCPM reads header crop → match vs vouchers.party_name
         └─ FULL-IMAGE FALLBACK if the crop doesn't confidently match (see §4)
 → (if push=queue) sale push block in do_POST:
     • build_sale_rate_map(party, items) → rate per item
     • build_sale_voucher_payload → GST SALE voucher
     • post_to_push_queue → backend /api/sync/push-queue (x-api-key)
 → returns JSON: rows[], party_name, sale_voucher_payload, sale_rate_items, queue_response
```

**Two MiniCPM calls per request** (items + party), sometimes **three** (party full-image fallback).

---

## 3. The MiniCPM interaction (how the OCR call works)

One helper: `query_minicpm_vlm(image_b64, prompt)` in `parsing/test_minicpm.py`.
- Builds an **OpenAI chat body**: system + user(`image_url` data:jpeg;base64 + text prompt),
  `temperature 0`, `max_tokens 4096`, `chat_template_kwargs={"enable_thinking": false}`
  (suppresses MiniCPM-V 4.5 reasoning).
- Transport = **RunPod native serverless** (URL is `api.runpod.ai/v2/<id>`, OpenAI route not proxied):
  `POST /run {input: body}` → job id → poll `GET /status/<id>` every 2s until `COMPLETED`
  (or the timeout deadline) → unwrap `output.choices[0].message.content` → **strip `<think>…</think>`**.
- Both sale OCR call sites branch on `SALE_OCR_BACKEND`: `runpod` → MiniCPM, else local Ollama (kept as fallback for local dev).

---

## 4. The full-image party fallback (why it exists)

Party name is read in two attempts in `extract_party_name` (`handler.py`):
1. **Header crop** (`crop_party_header_image`, top ~2–22%) → MiniCPM → fuzzy-match vs Supabase `vouchers.party_name`.
2. If **not confident** (no override / no Supabase hit above threshold) → **re-OCR the whole page**
   (`load_full_image_jpeg`) with the same party prompt → match again. Source tagged `..._fullimg`.

Reason: the narrow crop misread cursive **"Balaji" → "Bain"** (matched nothing); the full image
has more context and reads `"BALAJI H/W AGENCY"` → matches `"BALAJI H/W AGENCIES"`. Fallback only
fires on a miss (most calls stay at one OCR).

---

## 5. Files changed this session (parsing/ only; purchase path untouched)

| File | Change |
|---|---|
| `parsing/test_minicpm.py` | `query_minicpm_vlm` (RunPod OpenAI client) + `_strip_think` + `_extract_openai_content`; env knobs `SALE_OCR_BACKEND`, `MINICPM_RUNPOD_*`; `query_ollama_chat` branches to MiniCPM; `_finalize_stock_frame` refactor + `load_stock_from_supabase` (live stock) |
| `parsing/server/handler.py` | `query_ollama_text` branches to MiniCPM; `run_pipeline_for_image` uses `load_sale_stock` (Supabase, CSV fallback) + `company_name`; PDF guard relaxed for sale; **sale rate lookup** (`fetch_latest_rates_for_party` RPC, `fetch_fallback_rate_for_item`, `build_sale_rate_map`); **`build_sale_voucher_payload`** (GST SALE); `request_options` allows `push=queue`; sale push block; `SALE_*` constants |
| `parsing/requirements.txt` | added `pandas`, `openpyxl` (sale deps — were missing; would crash `import test_minicpm`) |
| `parsing/.gcloudignore` | **new** — keeps the 4.4 GB venv out of the Cloud Build upload |

**Sale voucher shape** (mirrors purchase envelope; Sales ledger directions = inverse of purchase):
`voucher_type: "GST SALE"`, party debited (`is_deemed_positive:true`), `GST SALE`+`CGST`+`SGST` credited (`false`),
items `{stock_item_name, quantity, rate, amount, unit, godown_name}`. Envelope `{company_name, voucher_payload}`.

**Defaults (all env-overridable):** `SALE_GST_RATE=0.09` (→18% total, mirrors backend `push-invoice.ts`),
`SALE_VOUCHER_TYPE="GST SALE"`, `SALE_LEDGER_NAME="GST SALE"`, `SALE_DEFAULT_UNIT="NOS"`,
`voucher_number="SALE-<timestamp>"`, missing qty → 1, NO-MATCH rows skipped.

---

## 6. GCP / deployment state

| Thing | Value |
|---|---|
| Project / region | `tallybridge-test-ocr` / `asia-south1` |
| gcloud account / bin | `rohan.psom@gmail.com` / `C:\Users\panka\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin` |
| Parsing live revision | `tallybridge-parsing-00008-6cz` (100% traffic) |
| Cloud Run request timeout | `3600s` |
| Sale OCR (RunPod) | MiniCPM-V 4.5 endpoint `vllm-vhm6qdmcjavvps`, repo `tb-minicpm-vllm-worker` |
| Purchase OCR (RunPod) | Nanonets endpoint `vllm-5fjqw6zdgqxt8g` |

**Env vars set on `tallybridge-parsing` this session:**
`SALE_OCR_BACKEND=runpod`, `MINICPM_RUNPOD_URL=https://api.runpod.ai/v2/vllm-vhm6qdmcjavvps/openai`,
`MINICPM_RUNPOD_API_KEY=<account rpa_ key>`, `MINICPM_RUNPOD_TIMEOUT_SECONDS=600`
(bumped from the 300 default to match `RUNPOD_TIMEOUT_SECONDS=600`; warm inference was ~275s, too close to 300).
Pre-existing: `RUNPOD_POD_URL` (Nanonets), `SUPABASE_URL/KEY`, `MINICPM_PUSH_QUEUE_URL`, `MINICPM_PUSH_QUEUE_API_KEY`.

**Redeploy command (env vars persist; rebuilds image from local source):**
```
gcloud run deploy tallybridge-parsing --source "D:/Desktop/TallyBridge/parsing" \
  --region asia-south1 --project tallybridge-test-ocr \
  --min-instances=1 --memory=2Gi --cpu=2 --timeout=3600
```
(Use absolute source path — relative `parsing` fails under Git Bash. `--allow-unauthenticated`/`allUsers`
IAM bindings are blocked for the agent; both services are already public.)

---

## 7. Git state — IMPORTANT

- **Last commit:** `daea5f2` (per-item discount + GCP Dockerfiles, from the prior session).
- **This session's changes** (`test_minicpm.py`, `handler.py`, `requirements.txt`, new `.gcloudignore`)
  are **deployed to GCP from local disk but NOT git-committed.** GCP is ahead of git.
- To persist: `git add` the 4 files + commit on `TallyBridge-Backend-Refactor`. No `.env`/secrets.

---

## 8. The databases — DO NOT CONFUSE (safety-critical, unchanged)

| Supabase ref | Which | Used by |
|---|---|---|
| `yynuuysvjeipawzfbeme` | **TEST** | GCP backend + parsing, local dev. **Safe to write.** |
| `ztugwhevemibdrzqafyw` | **CLIENT PRODUCTION** | client backend `tallybridge-h3do` → client TallyPrime. **NEVER write.** |

Sale rate lookup reads `vouchers`, `voucher_items`, and the RPC `get_latest_rates_for_party` on TEST.
Stock match reads `stock_items` (filtered by company_id). Party match reads `vouchers.party_name`.

---

## 9. The scrapped Google-Sheet flow (context only — no longer in the path)

The old sale flow was: endpoint JSON → n8n → Apps Script (`doPost`) writes the "Challan" sheet →
human reviews item matches via a column-E dropdown + Supabase rate fill → `submitDone` sends back to
n8n's `resume_url`. The rate lookup logic (`get_latest_rates_for_party` RPC + different-party fallback
from `voucher_items`/`vouchers`) was **ported server-side into `handler.py`**. The Apps Script also had
a **hardcoded Supabase `service_role` key** — avoid that pattern if the Sheet path is ever revived.

---

## 10. Industry-practice gaps (researched this session) — candidate next work

| Area | Industry norm | Us | Gap |
|---|---|---|---|
| Extraction | VLM doc understanding | MiniCPM/Nanonets VLM | ✅ aligned |
| Item matching | embeddings + fuzzy hybrid (pgvector) | rule-engine + rapidfuzz only | ⚠️ add vector retrieval |
| Review | confidence-gated straight-through | full auto, `confidence` computed but **not gated** | ⚠️ gate low-confidence rows |
| Dedup/idempotency | mandatory pre-post control | sale has **none** (`SALE-<ts>` → re-run dupes) | ⚠️ add dedup key |
| Async/queue | submit/poll/queue | same | ✅ aligned |
| Validation | totals/tax/PO checks | sale = flat 18% GST regardless of doc | ⚠️ add checks |

---

## 11. PICK UP HERE — open items

1. **Commit this session's 4 changed files** to git (`TallyBridge-Backend-Refactor`). (§7)
2. **Dedup / idempotency on the sale push** — `SALE-<timestamp>` means re-running the same challan
   creates a new push_queue row every time (already observed duplicate purchase rows too). Derive a
   stable key (party + date + item-set hash, or a real challan number) and skip/flag duplicates.
3. **Confidence-threshold gating** — per-row `confidence` is already computed; auto-push high-confidence,
   flag/skip low (e.g. line 10 `STEEL GRIP → DIE 3/4" BSP` at conf 58; the one unpriced item).
4. **Validation** — sale auto-applies 18% GST; either read tax off the doc or make it explicit per company.
   Also `unit` is hardcoded `NOS` (not the real stock unit).
5. **Embedding-based matching** (pgvector) for SKU↔catalog — higher effort, where the field is heading.
6. **Carry-overs from prior handouts:** Case-3 discount absolute-value capture; rule-engine #1 fix
   (prefer canonical over raw in `match_item_to_live_stock`); delete old Render services; 429/502 retry-backoff.

---

## 12. Quick reference

| Thing | Value |
|---|---|
| Sale endpoint (push) | `…/?type=sale&push=queue` (PDF binary) |
| Sale endpoint (read-only) | `…/?type=sale` |
| Purchase endpoint | `…/docstrange?purchase=all&source=runpod` |
| Parsing base URL | `https://tallybridge-parsing-950406969086.asia-south1.run.app` |
| Backend | `https://tallybridge-backend-950406969086.asia-south1.run.app` |
| Parsing live revision | `tallybridge-parsing-00008-6cz` |
| MiniCPM RunPod (sale) | `vllm-vhm6qdmcjavvps` (`openbmb/MiniCPM-V-4_5`, vLLM ≥0.10.2) |
| Nanonets RunPod (purchase) | `vllm-5fjqw6zdgqxt8g` (vLLM 0.9.2) |
| TEST Supabase (safe) | `yynuuysvjeipawzfbeme` |
| CLIENT Supabase (NEVER write) | `ztugwhevemibdrzqafyw` / `tallybridge-h3do` |
| Sale test PDF | `D:\Downloads\sale sample\balaji_sale.pdf` |
| Local venv (unit tests) | `parsing/.venv-ocrvl/Scripts/python.exe` (now has pandas/openpyxl/rapidfuzz/pdf2image) |
| Prior handouts | `session_handout_gcp_migration_discount.md`, `session_handout_purchase_ocr_render_deploy.md` |
