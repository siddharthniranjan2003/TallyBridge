# TallyBridge — Purchase Invoice OCR Pipeline: Session Handout

Context-transfer doc for a new chat. Covers the invoice OCR → vendor parse →
Tally voucher → Supabase `push_queue` pipeline, the RunPod serverless setup, and
the hard-won gotchas so none of it is relearned.

Last updated: 2026-05-31.

---

## 1. What this pipeline does

```
Purchase invoice PDF
  → 5003 handler renders each PDF page to JPEG (300 DPI)
  → Nanonets-OCR2-3B on RunPod (vLLM 0.9.2) returns markdown per page
  → vendor identification (detect_vendor)
  → vendor-specific row parser (parse_vlm_invoice) → raw items + header
  → conversion algorithm (fuzzy match vs per-vendor CSV) → Tally voucher
  → POST /api/sync/push-queue (backend 3001) → Supabase push_queue (status=pending)
  → (later) activate → desktop poller → tally_pusher → TallyPrime
```

Endpoint used for testing:
`POST http://127.0.0.1:5003/docstrange?purchase=all&source=runpod` (PDF as binary body).
- `source=runpod` → Nanonets on RunPod (what we use). NOT DocStrange.
- `source=local` (default) → real DocStrange server on :8010 (not running).
- Drop `purchase=all` to get raw OCR markdown only (no parse, no Supabase write).
- ⚠️ Every `purchase=all` call inserts a REAL row into Supabase `push_queue`.

8 vendors supported, all working: addison(ADDISON), cp(CP), emkay(ET),
saint global(GNL), pidilite(PIDILITE), rr(RR), totem(TOTEM), wikus(WIKUS).
Sample PDFs: `D:\Downloads\image sample\*.pdf`.

---

## 2. Critical facts (do NOT relearn these the hard way)

- **vLLM 0.9.2 is the ONLY version that serves Nanonets-OCR2-3B correctly.**
  - vLLM 0.20.x (`vllm/vllm-openai:latest`, RunPod prebuilt worker-vllm v2.17–v2.19):
    tied `language_model.lm_head` is mishandled → **serverless crashes**
    (`lm_head not initialized`); **pod emits garbage** (`!!!!`).
  - worker-vllm v2.16 = vLLM 0.17.1 → generation hangs / workers unhealthy.
  - Every RunPod prebuilt worker-vllm release is vLLM ≥0.15, so a **custom image
    pinned to 0.9.2 is required** (see §3).
- **Nanonets takes IMAGES only** (it's a vision model). PDFs are rasterized to JPEG
  on our side. Multi-page = each page rendered + OCR'd separately, markdown joined.
- **DPI is ours to set** (Poppler rasterization), nothing to do with Nanonets.
  300 DPI is now default for the RunPod path — fixed Pidilite's dense header.
- **VLM OCR is non-deterministic on dense layouts** even at temperature 0. Pidilite's
  multi-column header sometimes drops the "Document No" label. 300 DPI made it reliable.
- **RunPod serverless gotchas** that cost hours:
  1. GPU placement: a **network volume pins you to one datacenter** → throttling.
     Fix: detach volume + enable ALL data centers + broad 24 GB GPU list.
  2. API key scope: a **restricted/old key 401s on new endpoints**. Use a full-access key.
  3. Custom workers don't proxy the OpenAI route → use native `/run` + `/status`.

---

## 3. RunPod serverless setup (current production OCR)

- **Custom worker repo (public):** `github.com/siddharthniranjan2003/tb-vllm-worker`
  - `Dockerfile` (`FROM vllm/vllm-openai:v0.9.2` + runpod handler), `handler.py`, `README.md`.
  - Source also lives at `parsing/runpod-serverless-worker/`.
- **Endpoint:** `vllm-5fjqw6zdgqxt8g` (built from that GitHub repo).
  - Env: `MODEL_NAME=nanonets/Nanonets-OCR2-3B`, `MAX_MODEL_LEN=16384`,
    `GPU_MEMORY_UTILIZATION=0.9`, `DTYPE=bfloat16`.
  - GPU 24 GB, network volume NONE, all data centers, container disk 35 GB,
    idle timeout 60s, FlashBoot on, max workers 1–2.
- **Pod fallback (still exists):** `snwslqwd4kzglr` running `vllm/vllm-openai:v0.9.2`,
  L4 24 GB, port 8000, start cmd `--model nanonets/Nanonets-OCR2-3B ... --max-model-len 16384`.
  Key = `sk-snwslqwd4kzglr`. Bills ~$0.27/hr while running — STOP it when not needed.

### Switching pod ↔ serverless (parsing/.env)
The handler auto-detects `api.runpod.ai/v2` and uses the native job API; otherwise
treats `RUNPOD_POD_URL` as a direct vLLM OpenAI server. Switch = swap two lines.

Active (serverless):
```
RUNPOD_POD_URL=https://api.runpod.ai/v2/vllm-5fjqw6zdgqxt8g/openai
RUNPOD_POD_API_KEY=<full-access RunPod API key, rpa_...>
RUNPOD_MODEL=nanonets/Nanonets-OCR2-3B
RUNPOD_TIMEOUT_SECONDS=300
```
Pod fallback (commented in .env): uncomment the two `snwslqwd4kzglr` lines, comment serverless.

---

## 4. How to run it locally

Two local servers (both must be up for `purchase=all`):
```
# OCR/parse handler on 5003
py -3.11 parsing\n8n_minicpm_server.py --serve
# cloud backend on 3001 (writes to Supabase)
cd backend ; npm run dev
```
Notes:
- After editing parser code, restart 5003 AND clear non-venv `__pycache__`
  (stale bytecode silently served old code repeatedly this session).
- First serverless call after idle = ~30–60s cold start; warm ≈ 50s.
- Offline parser harness (no RunPod, fast iteration on saved markdown):
  `py -3.11 parsing\outputs\_raw_md\offline_test.py`

Supabase project: `yynuuysvjeipawzfbeme` (matches backend/.env). Verify rows:
```sql
select voucher_payload->'ledger_entries'->0->>'ledger_name' as party, status,
       voucher_payload->>'voucher_number' as vch_no,
       jsonb_array_length(voucher_payload->'items') as items, created_at
from push_queue order by created_at desc limit 10;
```

---

## 5. Key files

| File | Purpose |
|------|---------|
| `parsing/server/handler.py` | HTTP server (5003); RunPod call, DPI render, /docstrange route, push |
| `parsing/purchase_docstrange_pipeline.py` | Orchestrates markdown → voucher → push_queue payload |
| `parsing/purchase/vlm_vendor_parser.py` | Vendor detect + per-vendor row parsers + header extraction |
| `parsing/purchase/voucher_builder.py` | Per-vendor query building + fuzzy match conversion |
| `parsing/purchase/vendor.py` | `detect_vendor()` keyword → vendor code |
| `parsing/purchase/README.md` | Per-vendor architecture + outputs/vendors layout |
| `parsing/runpod-serverless-worker/` | Custom vLLM 0.9.2 serverless worker (Dockerfile + handler) |
| `backend/src/routes/sync.ts` | `/api/sync/push-queue` enqueue/activate/consume + push_queue schema |

Relevant config knobs in handler.py:
- `RUNPOD_PDF_RENDER_DPI` (env `MINICPM_RUNPOD_PDF_DPI`, default 300)
- `RUNPOD_PDF_RETRY_DPI` (env `MINICPM_RUNPOD_PDF_RETRY_DPI`, default 400) — used only by the disabled B fallback
- `RUNPOD_TIMEOUT_SECONDS` (default 300)

---

## 6. Pidilite-specific fixes (done)

- **Party name:** `_extract_vendor_name()` returns canonical `Pidilite Industries Limited`
  as soon as a header line contains `PIDIL`/`STEELGRIP` (fixes OCR "Pidillite" double-L,
  `<img>logo` bleed, address-line grabs). Same pattern ADDISON uses.
- **Invoice number = "Document No":** PIDILITE-specific high-priority pattern
  `DOCUMENT NO : <digits>` in `_extract_invoice_number()`, matched before Eway/Order/LR.
- **Date helper** (`lib/numeric.py parse_date_to_iso`): empty-safe + slash + 2-digit-year formats.
- **300 DPI (option A) is what actually fixed it** — primary parse now reliably yields
  `9830309222`. Verified live across multiple runs.

### A/B/C investigation outcome
- **A (300 DPI): the fix. KEPT, now default for all PDFs.**
- **B (field-targeted VLM re-ask): built but COMMENTED OUT** in handler.py (adds latency;
  A made it unnecessary). `recover_invoice_number()` helper remains; re-enable by uncommenting.
- **C (DocStrange `extract_data`): REJECTED.** Default mode uploads private invoices to
  NanoNets cloud (PII/GSTIN) and its install conflicts local deps. Uninstalled; env clean.

### Current behaviour when no invoice number is detected
Response still pushes the voucher to Supabase but includes `"invoice": "not detected"`.
When the number IS found, that field is absent and `voucher_number` holds the value.

---

## 7. Known limitations / open items

- **Invoice number depends on OCR run** for dense layouts: if Nanonets drops the label,
  no parser can recover it (B is the optional mitigation, currently off). 300 DPI made this rare.
- **WIKUS invoice number** occasionally misreads a leading digit (OCR digit variance), e.g.
  `002604029` vs `802504029`. Not a parser bug.
- **Local servers run manually** — 5003 + 3001 are not daemonized; start them per §4.
- **Changes are in the working tree, not committed.** Branch: `TallyBridge-Backend-Refactor`.
- Cleanup: stop pod `snwslqwd4kzglr` when idle; delete dead serverless endpoint
  `hszrdiegzg0k5c` (crash-looping from earlier attempts).

---

## 8. Quick test snippet (PowerShell)

```powershell
# raw OCR markdown only (no Supabase write)
$r = Invoke-WebRequest -Uri "http://127.0.0.1:5003/docstrange?source=runpod" `
  -Method Post -InFile "D:\Downloads\image sample\pidilite.pdf" `
  -ContentType "application/pdf" -TimeoutSec 600 -UseBasicParsing
($r.Content | ConvertFrom-Json).markdown

# full pipeline + push to Supabase
$r = Invoke-WebRequest -Uri "http://127.0.0.1:5003/docstrange?purchase=all&source=runpod" `
  -Method Post -InFile "D:\Downloads\image sample\pidilite.pdf" `
  -ContentType "application/pdf" -TimeoutSec 600 -UseBasicParsing
$r.Content | ConvertFrom-Json | ConvertTo-Json -Depth 8
```
