# TallyBridge — GCP Migration + Per-Item Discount: Session Handout

Context-transfer doc for a new chat. Picks up after the Render deployment handout
(`session_handout_purchase_ocr_render_deploy.md`). This session did two big things:
1. **Migrated the test backend + parsing OCR services off Render onto GCP Cloud Run.**
2. **Added per-item `discount` (absolute ₹) to the pushed voucher**, with a 3-case engine.

Last updated: 2026-06-01.

---

## 0. TL;DR — current state

- The purchase OCR pipeline now runs on **GCP Cloud Run** (project `tallybridge-test-ocr`,
  region `asia-south1`/Mumbai), **not Render**. Two public services, both warm
  (`min-instances=1`), `timeout=3600`.
- **Why we moved:** Render free tier spun down after ~15 min → recurring **429** (edge
  throttle) and **502** (cold backend unreachable) at the push step. GCP warm instances
  kill that. (RunPod GPU cold-start latency is separate and unchanged.)
- **New feature:** every item in `voucher_payload.items` now carries a `discount` field
  (absolute rupee value, NOT percent), covering 3 invoice cases. Verified live on GCP.
- ⚠️ **The discount code + GCP Dockerfiles are deployed from the LOCAL working tree via
  `gcloud run deploy --source` — they are NOT git-committed yet** (see §6).

---

## 1. The endpoint for sending PDFs (GCP)

```
POST https://tallybridge-parsing-950406969086.asia-south1.run.app/docstrange?purchase=all&source=runpod
Content-Type: application/pdf      (PDF as raw binary body)
```
- **No inbound auth** — the endpoint is public (the parsing→backend push is authed internally via `x-api-key`).
- `purchase=all` → full pipeline + push to queue. Drop it → raw OCR markdown only (no parse/push).
- `source=runpod` → Nanonets-OCR2-3B on RunPod (keep this).
- Canonical alias (identical): `https://tallybridge-parsing-xx3yz3b3kq-el.a.run.app/...`
- Warm call ~40–60s; first call slower if RunPod GPU worker is cold.

PowerShell:
```powershell
Invoke-WebRequest -Uri "https://tallybridge-parsing-950406969086.asia-south1.run.app/docstrange?purchase=all&source=runpod" `
  -Method Post -InFile "D:\Downloads\image sample\cp.pdf" -ContentType "application/pdf" -TimeoutSec 600 -UseBasicParsing
```

---

## 2. GCP setup (everything you need to drive it)

| Thing | Value |
|---|---|
| gcloud CLI | v570, installed via winget. **Not on PATH in old shells** — prepend `C:\Users\panka\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin` |
| Authed account | `rohan.psom@gmail.com` |
| Project | `tallybridge-test-ocr` (created fresh this session) |
| Region | `asia-south1` (Mumbai) |
| Billing account | `01682A-1111A6-A0D06B` ("My Billing Account"), linked |
| APIs enabled | `run`, `cloudbuild`, `artifactregistry` |

**Two services** (both: public `allUsers` invoker, `min-instances=1`, `timeout=3600`):

| Service | URL | Build | Resources |
|---|---|---|---|
| `tallybridge-backend` | `https://tallybridge-backend-950406969086.asia-south1.run.app` | Node, `--source backend`, `backend/Dockerfile` | 1Gi |
| `tallybridge-parsing` | `https://tallybridge-parsing-950406969086.asia-south1.run.app` | Docker, `--source parsing`, `parsing/Dockerfile` | 2Gi / 2 cpu |

Latest parsing revision serving 100%: **`tallybridge-parsing-00003-4wj`** (has the discount code).

### Env vars (stored in Cloud Run, preserved across redeploys)
**backend:** `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `API_KEY`, `FIREBASE_SERVICE_ACCOUNT_B64` (all TEST Supabase).
**parsing:** `RUNPOD_POD_URL`, `RUNPOD_POD_API_KEY`, `RUNPOD_MODEL`, `RUNPOD_TIMEOUT_SECONDS=600`,
`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SUPABASE_KEY`,
`MINICPM_PUSH_QUEUE_URL` (= backend `/api/sync/push-queue?company_name=K+V+ENTERPRISES`),
`MINICPM_PUSH_QUEUE_API_KEY` (= backend `API_KEY`).
(`MINICPM_HTTP_HOST=0.0.0.0` and `MINICPM_RUNPOD_PDF_DPI=200` are baked into `parsing/Dockerfile`.)

### Managing the services
- **Console:** https://console.cloud.google.com/run?project=tallybridge-test-ocr
- **CLI (prepend the gcloud bin path first):**
  ```
  gcloud run services describe tallybridge-parsing --region asia-south1 --project tallybridge-test-ocr
  gcloud run services update  tallybridge-parsing --region asia-south1 --project tallybridge-test-ocr --update-env-vars KEY=VALUE
  gcloud run deploy           tallybridge-parsing --source parsing --region asia-south1 --project tallybridge-test-ocr --min-instances=1 --memory=2Gi --cpu=2 --timeout=3600
  ```
- Redeploying with `--source` preserves env vars and scaling settings unless overridden.

### Permission gotchas (auto-mode classifier)
- `gcloud run deploy` works for me.
- **`--allow-unauthenticated` and the `allUsers` IAM binding are BLOCKED for me** → the USER must run them, e.g.:
  ```
  gcloud run services add-iam-policy-binding <svc> --region=asia-south1 --project=tallybridge-test-ocr --member=allUsers --role=roles/run.invoker
  ```
  (Or add a Bash permission rule for `gcloud run`.) Both services already have this grant.
- `-ExecutionPolicy Bypass` is also blocked — run `.ps1` scripts via `& script.ps1`, not bypass.

---

## 3. The databases — DO NOT CONFUSE (safety-critical, unchanged)

| Supabase ref | Which | Used by |
|---|---|---|
| `yynuuysvjeipawzfbeme` | **TEST** | GCP backend + parsing, local dev. **Safe to write.** |
| `ztugwhevemibdrzqafyw` | **CLIENT PRODUCTION** | client backend `tallybridge-h3do` → client TallyPrime. **NEVER write.** |

Rule still holds: parsing's `MINICPM_PUSH_QUEUE_URL` must point only at the TEST backend. The
whole GCP project is isolated test infra — client production is never in the path.

---

## 4. The per-item discount feature (the main code work)

**Goal:** each item in the pushed `voucher_payload.items` gets a `discount` field = absolute
rupee value (NOT a percent). Three — and only three — cases:

1. **No discount** → `discount = 0`.
2. **Per-item percentage** (CP/RR `Disc. %` column) → `discount = rate × qty × pct / 100`.
3. **One total discount value** stated outside the table (keyword `DISCOUNT` / `TOTAL DISCOUNT`
   followed by a number) → distribute proportionally:
   `item_discount = (item_amount / Σ item_amounts) × total_discount`.

### Files changed — `parsing/` only (backend stores the JSON as-is, no change)
- `parsing/purchase/models.py` — `PurchaseRawItem` gained `discount_pct: Decimal = Decimal("0")`.
- `parsing/purchase/voucher_builder.py`:
  - `combine_ocr_items` now sets `discount_pct` from the parsed `numeric_row`.
  - `adjust_items_to_target_subtotal` carries `discount_pct` through both item rebuilds.
  - **new** `compute_item_discounts(items, header_data)` — the 3-case engine. Case 2 if any
    item has `discount_pct > 0`; else Case 3 if `header_data.discount_amount` (preferred) or
    `discount_percent` (fallback) > 0; else Case 1. Proportional shares sum exactly (rounding
    remainder pushed onto the last item).
  - `build_voucher_payload` injects `"discount": float(...)` into each `voucher_item`.

### Verified
- **Unit math** (venv `parsing/.venv-ocrvl`): CP→7600, RR→3250, EMKAY case3 parts sum to 10474.92, case1→0. ✓
- **Live on GCP:** CP exact per-item (7600, 1380, 3000, 12000, 2683.5, 120000); EMKAY case 3 distributed and summed exactly. Both `queue_ok=True` into test `push_queue`.

### Known nuance on Case 3 (the EMKAY question)
Case 3 **prefers the absolute value** (`discount_amount`) and falls back to the **percent**
(`discount_percent`) only if the value wasn't captured. `_extract_discount_fields` reads the
trailing number on the **same line** as `DISCOUNT`. Nanonets output is non-deterministic:
- Earlier raw dump: `DISCOUNT 3.00 % -10,474.92` on one line → value captured.
- Live run: the model split the line, so the `-10,474.92` drifted off → only `3%` captured →
  fell back to % (gave 11,989.05 = 3% of subtotal, internally consistent but not the literal value).
- **GNL** (`TOTAL DISCOUNT 49,466.15-`, pure value) captures fine.
- **Refinement not yet done:** make `_extract_discount_fields` look ONE line ahead for the
  number so Case 3 always grabs the literal printed value regardless of line breaks.

---

## 5. How Nanonets renders discount, by vendor (from raw OCR dumps)

Pull raw OCR with `source=runpod` and NO `purchase=all` (returns `{markdown, html}`, no push).

| Vendor | Where discount lives | Type | Line amount | Difficulty |
|---|---|---|---|---|
| **CP** | per-line `Disc. %` column | percent | net | 🟢 |
| **RR** | per-line `Disc. %` column | percent | net | 🟢 |
| **EMKAY** | one line below table: `DISCOUNT 3.00 % -10,474.92` | percent, invoice-level | gross | 🟡 |
| **PIDILITE** | per-line `Discount In Rs.` column (blank in sample) | amount (₹) | gross→net | 🟡 |
| **GNL/Saint** | in description text `SPD/EB/TD %` + `TOTAL DISCOUNT 49,466.15-` | compound %, per-line + total | net | 🔴 |

---

## 6. Git state — IMPORTANT

- The discount edits (`models.py`, `voucher_builder.py`) and the new GCP files
  (`backend/Dockerfile`, `backend/.dockerignore`, `backend/.gcloudignore`) are in the **local
  working tree** at `D:\Desktop\TallyBridge` (branch `TallyBridge-Backend-Refactor`).
- They are **deployed to GCP via `gcloud run deploy --source` (from local disk), but NOT
  git-committed**, and **NOT pushed to the `tallybridge-test` repo**. The `tallybridge-test`
  GitHub repo therefore does NOT have the discount feature yet.
- To persist: `git add` + commit on `TallyBridge-Backend-Refactor` and/or copy into the
  `tallybridge-test` repo. (GCP deploys don't depend on git — they upload local source — so the
  cloud is ahead of git right now.)
- Render services from the old handout (`tallybridge-test`, `tallybridge-test-backend`) are
  **superseded** by GCP but still live; can be deleted.

---

## 7. PICK UP HERE — open items

1. **Harden Case-3 absolute-value capture** — `_extract_discount_fields` look-ahead one line so
   EMKAY grabs the literal `10,474.92` instead of falling back to 3%. (#1 discount refinement.)
2. **Run Case-1 live check** (pidilite → expect `discount=0`) and sweep the other vendors
   (RR/GNL/totem/wikus/addison) through the GCP endpoint to confirm `discount` across all.
3. **Commit the discount + GCP Dockerfile changes** to git / the `tallybridge-test` repo (§6).
4. **(Optional) Delete old Render test services.**
5. **(Lower priority now)** 429/502 retry-backoff in `post_to_push_queue` — less urgent since GCP
   is warm, but still good hygiene for transient blips.
6. **Rule-engine accuracy** — the #1 fix from the earlier handout (prefer canonical query over
   raw fallback in `match_item_to_live_stock`) is still pending.

---

## 8. Quick reference — all the moving parts

| Thing | Value |
|---|---|
| PDF endpoint | `https://tallybridge-parsing-950406969086.asia-south1.run.app/docstrange?purchase=all&source=runpod` |
| Backend | `https://tallybridge-backend-950406969086.asia-south1.run.app` |
| GCP project / region | `tallybridge-test-ocr` / `asia-south1` |
| gcloud account | `rohan.psom@gmail.com` |
| gcloud bin (prepend to PATH) | `C:\Users\panka\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin` |
| Parsing live revision | `tallybridge-parsing-00003-4wj` |
| TEST Supabase (safe) | `yynuuysvjeipawzfbeme` |
| CLIENT Supabase (NEVER write) | `ztugwhevemibdrzqafyw` / `tallybridge-h3do` |
| RunPod serverless | `vllm-5fjqw6zdgqxt8g` (vLLM 0.9.2, Nanonets-OCR2-3B) |
| Discount code | `parsing/purchase/voucher_builder.py` (`compute_item_discounts`) + `models.py` |
| 8 sample PDFs + answer CSVs | `D:\Downloads\image sample\` |
| Local venv (for unit tests) | `parsing/.venv-ocrvl/Scripts/python.exe` |
| Prior handout | `md_files/session_handout_purchase_ocr_render_deploy.md` |
