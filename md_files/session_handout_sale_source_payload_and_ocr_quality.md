# TallyBridge — Sale `source_payload` + Match-Score Fix + OCR-Quality Investigation: Session Handout

Context-transfer doc for a new chat. This session did three things:
1. **Added `source_payload` to the sale push** (mirroring purchase) so the
   `push_queue` row records *how* each line was matched.
2. **Caught and fixed a wrong "score"** — it was a baked-in benchmark-accuracy
   metric (only valid for balaji); replaced with the real fuzzy match score.
3. **Diagnosed why a pushed sale voucher showed no discount in Tally**, then
   investigated **pre-OCR / readability checks** for both paths and evaluated
   external HTR resources the user supplied.

Sibling docs (read for upstream context):
- `session_handout_sale_minicpm_pushqueue.md` — MiniCPM wiring + auto push to queue.
- `session_handout_sale_discount.md` — sale per-item discount (metadata-only).
- `session_handout_purchase_discount.md` — purchase discount derivation + `source_payload`.
- `session_handout_discount_push_to_tally.md` — the outbound push that renders discount in Tally.

Last updated: 2026-06-03.

---

## 0. TL;DR — what this session did

1. **Sale `source_payload`** now persists to `push_queue`, like purchase. Shape:
   `{"items":[ <voucher item> + source + score + rate_source ]}`. **Only**
   `parsing/server/handler.py` needed changing — the backend already inserts
   `rawBody.source_payload` verbatim and the sale push posts its body at top level.
2. **The `score` was wrong, then fixed.** First version used `row.similarity`,
   which is a *positional accuracy comparison against a fixed answer-key Excel*
   (`challan_updated.xlsx`, the balaji ground truth) — meaningless for any other
   invoice and flaky run-to-run. Per user direction ("put fuzzy match score"), it
   now uses the **fuzzy string similarity** between the normalized OCR read and
   the matched stock name, bounded 0–100.
3. **Verified end-to-end on GCP** (revision `tallybridge-parsing-00013-ghq`):
   `source_payload` with stable fuzzy scores persisted into a real TEST
   `push_queue` row.
4. **Diagnosed the "no discount in Tally" question** — not a bug. Root cause is
   **non-deterministic matching** (same invoice line → `SEC TOTEM` one run,
   `SET TOTEM` the next), plus the established fact that sale discount is
   **metadata-only** (`amount = qty×rate`, GST on gross; discount not subtracted).
5. **Investigated pre-OCR checks** for `cp.pdf` (purchase) and `balaji_sale.pdf`
   (sale): both are **image-only scans (no text layer)**; `cp.pdf` is **low-res
   (~110 DPI)**, `balaji` is high-res but handwritten/cursive. Concluded pre-OCR
   image checks are mostly a *gate*, and the real levers are a **readability/
   confidence gate over the VLM** and **LoRA fine-tuning**.

⚠️ **All code changes are deployed to GCP from local disk but NOT git-committed.**

---

## 1. Feature: sale `source_payload` (mirror of purchase)

### Why
Purchase already writes `push_queue.source_payload` (`{"items":[...]}`) recording
per-line match provenance; sale left that column NULL. User wanted parity.

### How the plumbing works (no backend change needed)
- Backend `POST /api/sync/push-queue` reads `const source_payload =
  rawBody.source_payload ?? null` (`backend/src/routes/sync.ts:2154`) and inserts
  it **verbatim** (no whitelist) at `:2182`.
- The **sale** push posts `queue_request_payload` *as the top-level body*
  (`handler.py` sale push block, ~`:2016`). So adding a `source_payload` key to
  that dict lands exactly where the backend looks. (Purchase wraps in
  `tally_payload` + sibling `source_payload`; both shapes work because the backend
  reads `source_payload` from `rawBody` either way.)

### Shape produced (per item)
```json
{ "stock_item_name":"HSS- PMS ROLL TAP …","quantity":5.0,"rate":1905.0,
  "amount":9525.0,"discount_pct":0.0,"unit":"NOS","godown_name":"Main Location",
  "source":"Matching_Algorithem",      // mirrors purchase label (sale has no exact-lookup path)
  "score":"56%",                         // fuzzy match score, 0–100 (see §2)
  "rate_source":"different_party" }      // same_party | different_party | none (sale-specific)
```
`source`/`score` mirror purchase; `rate_source` is added because rate provenance
is the key review signal on the sale side (it was already computed, previously
discarded).

---

## 2. The `score` fix (the important correctness item)

### What was wrong
`source_payload.score` initially read `row["score"]` → `serialize_rows` set that
from `PredictionRow.similarity` → `compare_against_reference(predicted_name,
expected_name)` (`test_minicpm.py:1535`). `expected_name` is a **positional
lookup into a fixed Excel answer key**:
- `DEFAULT_BENCHMARK = SCRIPT_DIR / "challan_updated.xlsx"` (`test_minicpm.py:31`),
  baked into the deploy.
- The sale POST always passes it (`handler.py:2003`,
  `benchmark_path=DEFAULT_BENCHMARK if … exists()`).
- `expected_name = expected[idx]` (`test_minicpm.py:1606`) — compared by row index.

So `score` measured "how close the prediction matched the *balaji* ground truth at
that row index." Valid only for `balaji_sale.pdf`; garbage for any other invoice;
flaky run-to-run (proven: same item read **85%** one run, **0%** the next, because
OCR row order/count shifts the positional alignment).

### The fix (user direction: "put fuzzy match score")
`matches[0].score` (the matcher's internal ranking score) was rejected too — it's
a fuzzy blend **plus unbounded domain bonuses** (`+16`/number, `+14`/family, etc.,
`score_candidate` `test_minicpm.py:1344`) and exceeds 100 (we saw 139–185%). The
final fix computes the **pure fuzzy string similarity** between the normalized OCR
read and the matched stock name, the same way the codebase already measures it:
```python
match_score = max(int(fuzz.WRatio(query_norm, predicted_norm)),
                  int(fuzz.token_set_ratio(query_norm, predicted_norm)))
# query_norm = normalize_text(item.description); predicted_norm = normalize_text(predicted_name)
```
Bounded 0–100, stable, benchmark-independent. Known nuance: short subset reads
score 100% via `token_set_ratio` (e.g. read `ADDISON` → `HSS T/S DRILL 24.25
ADDISON`) — standard fuzzy behavior, consistent with `compare_against_reference`.

---

## 3. Files changed THIS session (parsing only)

| File | Change |
|---|---|
| `parsing/server/handler.py` | (1) new `format_sale_match_score()` (percent string, identical formatting to purchase's `format_match_score_percent`). (2) `build_sale_voucher_payload` now builds `source_items` parallel to `priced_items` and returns a **3-tuple** `(queue_request_payload{+source_payload}, priced_items, source_payload)`; sale push block unpacks the 3-tuple and stashes `sale_source_payload` in the response. (3) `serialize_rows` adds `"match_score": row.match_score`. |
| `parsing/test_minicpm.py` | (1) `PredictionRow` gains `match_score: float = 0.0`. (2) `build_prediction_rows` computes the bounded fuzzy `match_score` (WRatio/token_set_ratio of normalized read vs predicted name) and passes it in. |

Both compile (`py -3 -m py_compile`). Backend + purchase untouched.

**Caller arity note:** `build_sale_voucher_payload` returned a 2-tuple before; it
now returns a **3-tuple**. The only caller (sale push block in `do_POST`) was
updated. Grep confirmed no other code caller.

---

## 4. Deploy + verification state (GCP Cloud Run)

Project `tallybridge-test-ocr`, region `asia-south1`, account `rohan.psom@gmail.com`,
gcloud bin `C:\Users\panka\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd`.

Deploy command (the `gcloud run deploy` is gated by Claude Code's auto-mode
classifier — needs explicit user authorization each time):
```
gcloud run deploy tallybridge-parsing --source "D:/Desktop/TallyBridge/parsing" \
  --region asia-south1 --project tallybridge-test-ocr --min-instances=1 --memory=2Gi --cpu=2 --timeout=3600 --quiet
```

Revision history this session: `00011-xkv` (source_payload + benchmark score) →
`00012-t7s` (raw ranking score, >100%) → **`00013-ghq` (bounded fuzzy score — LIVE)**.

**Final verification (revision 00013, `balaji_sale.pdf` → `/?type=sale&push=queue`):**
- HTTP 200 ~57s, `queue_response.ok=true`.
- `push_queue` row `2396d991-…` in TEST Supabase: `status pending`, 20 source items.
- Scores: `56,100,87,90,87,83,86,83,100,85,85,60,95,93,92,94,92,94,92,94` — all
  0–100, stable. Response and persisted DB row match exactly.

Verified persistence directly via Supabase REST (no MCP needed):
```
curl "$SUPABASE_URL/rest/v1/push_queue?id=eq.<id>&select=id,status,source_payload" \
  -H "apikey: $SUPABASE_SERVICE_KEY" -H "Authorization: Bearer $SUPABASE_SERVICE_KEY"
```

---

## 5. Diagnosis: "I can't see discount in the pushed Tally voucher"

User showed a Tally `GST SALE` voucher (`KVE/25-26/1`, BALAJI H/W AGENCIES) with a
**blank Disc% column**. **Not a bug.** Findings:

1. The exact row (`push_queue` `8a2afbbd-…`, narration `SALE-20260602122918`,
   status `pushed`) had **`discount_pct = 0` on all 20 items** → blank is correct.
2. **Why 0:** sale `discount_pct` comes *only* from the **same-party rate history**
   (RPC `get_latest_rates_for_party`). Items priced from a different party's last
   rate get `discount_pct = 0.0` (`fetch_fallback_rate_for_item`). In that push,
   every tap line matched a **`SEC TOTEM`** SKU (no same-party discount) → all 0.
3. **The deeper issue — non-deterministic matching.** A *second* push of the SAME
   `balaji_sale.pdf` (row `SALE-20260602173206`) matched the same lines to
   **`SET TOTEM`** SKUs instead — different rate (419 → 1257, ~3×) **and** a 46%
   same-party discount on 4 lines. So the same invoice yields different SKUs,
   rates, totals, and discounts across runs. SEC vs SET is a 1-char OCR/match
   difference that flips everything downstream.
4. **Discount is metadata-only (by design, per `session_handout_sale_discount.md`).**
   Even when 46% appears, `amount = qty × rate` (gross) and GST is on gross —
   proven: item rate 1257 × qty 5 = 6285 = amount (not `×0.54`); `CGST 145,308.09
   = 9% × subtotal 1,614,534.38`. So in Tally the SET rows would show "rate 1257,
   Disc 46%, amount 6285" — internally inconsistent (cosmetic discount).

**The push-discount feature itself works** (proven earlier:
`session_handout_discount_push_to_tally.md` — `TBDISCTEST1` 25%, `TBQUEUETEST1`
15% rendered). This voucher simply had no discount in its data.

---

## 6. Pre-OCR / OCR-quality investigation (both paths)

### The two sample files (measured with PyMuPDF — installed into `.venv-ocrvl` this session)

| | PURCHASE `cp.pdf` | SALE `balaji_sale.pdf` |
|---|---|---|
| Text layer | **none (image-only scan)** | **none (image-only scan)** |
| Embedded image | 916×1280 px | 2434×3446 px |
| **Effective DPI** | **~110 (low)** | ~248 (good) |
| Sharpness (lap-var) | 2570 (soft) | 9729 (sharp) |
| Page size | 595×842 pt (A4) | 706×1000 pt |

### Key conclusions
- **Neither has a text layer** → the "digital PDF → `fitz` text extraction" route
  (which the purchase architecture half-implements per `CLAUDE.md`) **does not
  help these two files**. They are scans/photos.
- **`cp.pdf` is genuinely low-res (~110 DPI).** Critical gotcha: the pipeline
  renders PDFs at 300 DPI, but the page just contains a 916×1280 image stretched
  to A4 — **render-DPI ≠ source-DPI**; rasterizing at 300 just upscales the same
  pixels (no new detail). A pre-OCR check can **detect and flag** low effective
  DPI (embedded px ÷ page size); it cannot recover missing detail.
- **`balaji_sale.pdf` is high-res and sharp** → its errors (`Balaji→Bain`,
  SEC/SET) are **content** problems (handwriting/cursive, near-identical SKUs),
  **not** image-quality problems. Deskew/denoise/DPI won't fix them.
- **Net:** classic pre-OCR image checks are a *gate* (catch low-res `cp.pdf`
  before a 3-min RunPod call), not a fix.

### Do Nanonets / Docstrange provide pre-OCR checks?
- The Nanonets **SaaS platform** does (auto-rotate/deskew, denoise, blur/quality
  detection, confidence scores). But the deployment runs the **open-weights model
  `nanonets/Nanonets-OCR2-3B` on RunPod via vLLM** — a raw inference server that
  does **no image QA**. The `/docstrange?...&source=runpod` endpoint name is
  misleading; `source=runpod` bypasses any docstrange preprocessing.
- **Conclusion: effectively no vendor-provided pre-OCR check in the current stack.**
  (Worth confirming against the exact handler, but the architecture implies it.)

### "A layer over MiniCPM-V 4.5 that says if a handwritten doc can be read" — recommended design
Four options, cheapest first:
1. **VLM self-assessment pre-flight** — one small MiniCPM call: "printed/handwritten?
   legibility 0–100? line-item table readable? → JSON." Route low-legibility to
   human review. VLMs are instructable, so this is sound.
2. **Token-logprob confidence** — vLLM (≥0.10.2, which the MiniCPM worker runs) can
   return logprobs; low avg logprob on the line-item region = unsure = flag.
3. **Self-consistency** — run extraction 2× (or temp>0), diff. High disagreement =
   low confidence. **Directly catches the SEC/SET flip.** Highest-signal for this
   codebase's actual problem.
4. **Dedicated handwriting detector** (small classifier/heuristic). Mostly
   subsumed by #1.

Recommended: **#1 + #3 + an effective-DPI gate** (§6) → a real "should a human look
at this?" signal for both sale and purchase.

### External HTR resources the user supplied — verdict
- **HTR-best-practices (georgeretsi)** and the **CRNN+CTC Medium article** — *not a
  fit / a downgrade.* Both are line-level CRNN+CTC trained on IAM (English cursive
  prose), no layout/table understanding, need self-labeled training data. 2025
  reviews are explicit that CRNN/TrOCR-class models are not competitive with modern
  VLMs. You already run SOTA VLMs. Only salvage: their augmentation lists if you
  fine-tune.
- **2024/2025 OCR-review threads** — *useful*: (a) confirm VLM is the right class;
  (b) menu of alternatives to A/B test (Qwen2.5-VL, PaddleOCR-VL-0.9B,
  DeepSeek-OCR-3B, dots.ocr); (c) the real accuracy lever for handwriting is
  **LoRA fine-tuning a VLM** on your own labeled invoices (freeze vision encoder,
  adapt attention/MLP).

---

## 7. What was NOT done (open items / pick-up points)

1. **Git commit** — none of this session's changes are committed. Two files:
   `parsing/server/handler.py`, `parsing/test_minicpm.py`. Branch
   `TallyBridge-Backend-Refactor`. User's prior pattern: push to **`tallybridge-test`
   only**, never `origin`. (Also un-asked: a temporary `pip install pymupdf` into
   `.venv-ocrvl` was done for measurement — not part of the app; ignore/leave.)
2. **No code written for OCR-quality work** — §6 is investigation/recommendation
   only. Nothing built yet. Suggested first build: the **readability gate**
   (self-assessment call + effective-DPI check) as a pre-step in the sale/purchase
   handler; helps regardless of base model.
3. **The matching-instability root cause (SEC vs SET) is unfixed** — this is the
   highest-value real bug. Same invoice → different SKUs/rates/totals across runs.
   Candidate fixes: self-consistency gate (#3 above), match-score gating (the new
   `score` field), totals reconciliation (Σ line amounts + tax vs grand total),
   SET/SEC disambiguation in the matcher, rate-sanity flag (3× swings).
4. **Sale discount is cosmetic** (metadata-only) — if a real discount is wanted,
   `amount = qty×rate×(1−pct/100)` + GST on the discounted subtotal. Deliberately
   not changed (matches the established sale stance).
5. **No dedup/idempotency** — re-running `balaji_sale.pdf` creates a new
   `push_queue` row each time (`SALE-<timestamp>`); this session created several.
6. **Memory not saved** — offered but not yet written: the gotcha that
   `row.similarity`/`score` is a benchmark-accuracy metric vs `challan_updated.xlsx`,
   not a match confidence.

---

## 8. Databases — DO NOT CONFUSE (safety-critical, unchanged)

| Supabase ref | Which | Notes |
|---|---|---|
| `yynuuysvjeipawzfbeme` | **TEST** | GCP parsing + backend, local dev. **Safe to write.** All this session's pushes landed here. |
| `ztugwhevemibdrzqafyw` | **CLIENT PRODUCTION** | client backend `tallybridge-h3do` → client TallyPrime. **NEVER write.** |

`.env` files never committed. Local `parsing/.env` confirmed `SUPABASE_URL`
→ TEST `yynuuysvjeipawzfbeme`. The `rpa_` RunPod key in `.env` is account-wide
(authenticates both the MiniCPM serverless endpoint and the Nanonets pod).

---

## 9. Quick reference

| Thing | Value |
|---|---|
| Sale endpoint (push) | `https://tallybridge-parsing-950406969086.asia-south1.run.app/?type=sale&push=queue` (PDF binary) |
| Purchase endpoint | `…/docstrange?purchase=all&source=runpod` |
| Backend | `https://tallybridge-backend-950406969086.asia-south1.run.app` |
| Parsing live revision | **`tallybridge-parsing-00013-ghq`** |
| Sale source_payload + score | `parsing/server/handler.py` → `build_sale_voucher_payload`, `format_sale_match_score`, `serialize_rows` |
| Fuzzy match_score | `parsing/test_minicpm.py` → `PredictionRow.match_score`, `build_prediction_rows` |
| Benchmark answer-key (the score trap) | `parsing/challan_updated.xlsx` (`DEFAULT_BENCHMARK`, `test_minicpm.py:31`) |
| Sale discount source | `voucher_items.discount_pct`, latest same-party row via RPC `get_latest_rates_for_party` (metadata-only) |
| MiniCPM RunPod (sale) | `vllm-vhm6qdmcjavvps` (`openbmb/MiniCPM-V-4_5`, vLLM ≥0.10.2) |
| Nanonets RunPod (purchase) | `vllm-5fjqw6zdgqxt8g` (`nanonets/Nanonets-OCR2-3B`, vLLM 0.9.2) |
| Local venv (measurement) | `parsing/.venv-ocrvl/Scripts/python.exe` (now also has `pymupdf`) |
| Sale sample | `D:\Downloads\sale sample\balaji_sale.pdf` (scan, ~248 DPI, handwritten) |
| Purchase sample | `D:\Downloads\image sample\cp.pdf` (scan, ~110 DPI, low-res) |
| TEST Supabase (safe) | `yynuuysvjeipawzfbeme` |
| CLIENT Supabase (NEVER) | `ztugwhevemibdrzqafyw` / `tallybridge-h3do` |
| Git | uncommitted; prior pattern = `tallybridge-test` only |
