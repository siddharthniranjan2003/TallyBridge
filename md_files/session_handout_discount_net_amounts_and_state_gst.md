# TallyBridge — Net (Post-Discount) Item Amounts + State-Based GST: Session Handout

Context-transfer doc for a new chat. This session changed **both** the sale and
purchase pipelines so each item's `amount` is now the **net amount after its own
discount**, the voucher totals and **GST are computed on the discounted (net)
base**, and the sale path now chooses **CGST+SGST vs IGST from the party's state**.
Deployed to GCP and verified live.

Picks up after these sibling handouts (read for upstream context):
- `session_handout_sale_minicpm_pushqueue.md` — sale OCR wiring + push_queue.
- `session_handout_sale_discount.md` — sale per-item `discount_pct` (was metadata-only).
- `session_handout_discount_push_to_tally.md` — outbound push renders `<DISCOUNT>`.
- `session_handout_sale_source_payload_and_ocr_quality.md` — sale `source_payload` + score.

Last updated: 2026-06-04.

---

## 0. TL;DR — what this session did

**Before this session**, discount was *metadata-only*: sale `amount = qty×rate`
(gross), purchase `amount` = the parsed table figure (and for some vendors the
items got rescaled to the printed total, which hid the discount). The `<DISCOUNT>`
% showed in Tally but the line amount/total/GST never reflected it.

**Now** (both paths, end-to-end through push_queue and the Tally XML):
1. **Each item `amount` is NET** = `gross × (1 − discount_pct/100)`, where
   `gross = qty × rate`. `RATE` stays gross; `<DISCOUNT>` still shows the %.
2. **Totals and GST are on the net subtotal** (GST after discount).
3. **Sale GST is state-aware**: party's `state` from Supabase `ledgers`
   (`group_name = Sundry Debtors`) → **Haryana ⇒ CGST+SGST (9%+9%)**, otherwise
   **IGST (18%)**.
4. **`discount_total`** (sum of per-item rupee discounts) is now emitted for
   **sale** too (purchase already had it).
5. **Deployed** to GCP (`tallybridge-parsing-00014-cz4`) and **verified live** for
   `emkay.pdf` (purchase, IGST) and `balaji_sale.pdf` (sale, CGST+SGST).

⚠️ **Not git-committed yet.** Only two files changed:
`parsing/server/handler.py`, `parsing/purchase/voucher_builder.py`.
**No backend or `tally_pusher.py` change was needed** (see §5).

---

## 1. The decisions that shaped the implementation (Q&A with the user)

These are the agreed answers — implement to these, do not re-litigate:

- **Q1 (purchase: trust printed invoice_total or recompute?) → (b) recompute.**
  Compute net per item from `gross × (1 − pct/100)`; do **not** anchor item
  amounts to the printed total. (Caveat surfaced later — see §4 and §8.)
- **Q2 (GST base) → GST is computed AFTER discount**, for both sale and purchase
  (CGST/SGST/IGST all on the net base).
- **Q3 (purchase per-item already-net detection, "matches neither" case) → trust
  the parsed amount** (leave it as-is).
- **Q4 (sale `discount_total`) → yes, emit it** (= Σ per-item (gross − net)),
  purely informational; the item amounts are already net so it is not subtracted
  again.
- **Q5 (sale IGST vs CGST/SGST) → look up the debtor in Supabase `ledgers`**
  filtered by `name = <party>` AND `group_name = Sundry Debtors`; read the `state`
  column. **state == Haryana ⇒ CGST+SGST; else ⇒ IGST 18%.** (Hardcode Haryana as
  the home state.) The user also asked to update "the function that builds XML and
  pushes to Tally" — it turned out **no change was needed there** (already generic;
  §5).
- **Keep the discount % in the final Tally XML** (display), even though `<AMOUNT>`
  is now net.

---

## 2. SALE changes — `parsing/server/handler.py`

### 2a. New constants (near the other `SALE_*`, ~line 119)
```python
SALE_IGST_RATE = float(env_value("SALE_IGST_RATE", "0.18"))
SALE_HOME_STATE = env_value("SALE_HOME_STATE", "Haryana")
SALE_LEDGER_TABLE = env_value("MINICPM_SALE_LEDGER_TABLE", "ledgers")
SALE_DEBTOR_GROUP = env_value("MINICPM_SALE_DEBTOR_GROUP", "Sundry Debtors")
```
(`SALE_GST_RATE = 0.09` unchanged — that's the per-half CGST/SGST rate.)

### 2b. New helper `fetch_party_state(party_name)` (just above `fetch_fallback_rate_for_item`)
Queries the Supabase REST `ledgers` table via the existing `supabase_get`:
`select=state & name=eq.<party> & group_name=eq.Sundry Debtors & limit=1`.
Returns the `state` string, or `""` on miss/unconfigured (caller then defaults).

### 2c. `build_sale_voucher_payload` (the core)
- Per item: `gross = round(qty*rate,2)`; **`amount = round(gross*(1-discount_pct/100),2)`**.
- `subtotal = Σ net amounts`.
- `discount_total = round(Σ (gross − net), 2)`.
- `party_state = fetch_party_state(party_name)`;
  `is_intra_state = party_state.casefold() == SALE_HOME_STATE.casefold()`.
  - intra ⇒ `CGST = SGST = round(subtotal*0.09,2)` (two tax ledger entries)
  - inter ⇒ `IGST = round(subtotal*0.18,2)` (one tax ledger entry)
- `total = subtotal + Σ tax`; party debited `total`; `GST SALE` credited `subtotal`;
  tax ledgers credited.
- `voucher_payload` now includes `"discount_total": discount_total`.
- `discount_pct` stays on each item; the source_payload items inherit the net amount.

---

## 3. PURCHASE changes — `parsing/purchase/voucher_builder.py` (`build_voucher_payload`)

### 3a. Detect discount + capture the doc's taxable base (top of function)
```python
has_discount = (
    any(decimal_value(getattr(item, "discount_pct", 0) or 0) > 0 for item in raw_items)
    or round2(decimal_value(header_data.get("discount_amount", 0) or 0)) != 0
    or decimal_value(header_data.get("discount_percent", 0) or 0) > 0
)
doc_taxable = round2(invoice_total - tax_total) if invoice_total > 0 else Decimal("0")
```

### 3b. Skip the printed-total anchor when discounted
The pre-existing `adjust_items_to_target_subtotal(...)` (which scales items so they
sum to `invoice_total − tax_total`, and in doing so **rescales the rate to net,
hiding the discount**) now runs **only when `not has_discount`**. With a discount,
items keep their raw **gross** rate/qty/amount.

### 3c. Per-item NET with the Q3 already-net check (in the `voucher_items` loop)
```python
gross = round2(rate * qty)
discount_pct = round2(discount_value / gross * 100) if gross > 0 else 0
parsed_amount = round2(item["amount"])
if discount_value <= 0:
    net_amount = parsed_amount
else:
    expected_net = round2(gross - discount_value)
    tol = max(Decimal("1"), round2(gross * Decimal("0.01")))   # 1% / ₹1 tolerance
    if abs(parsed_amount - expected_net) <= tol: net_amount = parsed_amount  # already net → keep
    elif abs(parsed_amount - gross) <= tol:      net_amount = expected_net   # gross → deduct
    else:                                        net_amount = parsed_amount  # neither → trust parsed (Q3)
```
Item emits `amount = net_amount`, plus existing `discount` (₹) and `discount_pct`.

### 3d. GST recomputed on net — robust to the bad tax parse
`subtotal = Σ net`. Then (in priority):
1. **If the printed grand total is usable** (`invoice_total > 0`,
   `implied_tax = invoice_total − subtotal > 0`, and the doc's tax total is absent
   or off by >20% from `implied_tax`): **trust `implied_tax` as the real GST** and
   split it across the doc's tax ledgers (CGST/SGST equal halves; a lone IGST takes
   all). *This is what fixes emkay (see §4).*
2. **Elif** `has_discount and doc_taxable > 0`: keep each tax line's document rate
   (inferred from `doc_taxable`) and scale by `subtotal/doc_taxable`.
3. **Else**: keep the document tax amounts as-is.

`tax_total = Σ recomputed tax`; `invoice_total = subtotal + tax_total`; ledgers
rebuilt (party = invoice_total credit-side, purchase ledger = subtotal, taxes).

---

## 4. The emkay tax-parser quirk (why §3d exists)

For `emkay.pdf` the OCR/parser produced:
```
tax_entries = [{"ledger_name": "IGST", "amount": 18.0}]   # 18 is the RATE %, not rupees
invoice_total = 399653.0                                   # reliable (= net + 18% IGST)
discount_percent = 3.0,  discount_amount = -10474.92
```
- Gross subtotal ≈ **349,164**; 3% discount ⇒ net **338,689.08**; IGST 18% on net =
  **60,963.92**; total **399,653** ✓.
- A naïve "scale the doc tax amount" gave **IGST = 15.25** (scaling the bogus 18).
  The original code was *also* broken here (it used 18 directly / inflated items).
- Fix: when the doc tax is implausible vs `invoice_total − net`, derive GST from the
  reliable grand total (§3d rule 1) ⇒ correct **IGST 60,963.92**.

---

## 5. Why NO backend / tally_pusher change was needed (verified)

- **Backend** `backend/src/routes/sync.ts` `normalizePushVoucherPayload`:
  `ledger_entries` pass through generically by name (so **IGST is preserved**), and
  `discount_total` is whitelisted at **`sync.ts:247`** (generic top-level — works for
  sale too). No GST recomputation server-side. Confirmed by reading rows back
  (§7): `discount_total`, IGST, CGST/SGST all persisted.
- **`src/python/tally_pusher.py`**: the XML builder renders whatever
  `ledger_entries` it's handed; `_is_tax_like_ledger` already lists `IGST`
  (line ~104), so IGST renders as a top-level tax ledger and is excluded from
  inventory-ledger detection. RATE=gross, `<DISCOUNT>`=%, `<AMOUNT>`=net all flow
  from the payload. The user expected a change here; **none required**.

Local XML render check confirmed:
- Purchase: `RATE 1121.04/NOS`, `DISCOUNT 3`, `AMOUNT -108740.88`, top-level
  `IGST -60963.92` (purchase sign convention: party `ISDEEMEDPOSITIVE No` / positive,
  items+taxes negative).
- Sale: `RATE 1257.00/NOS`, `DISCOUNT 46`, `AMOUNT 3393.90`, top-level
  `CGST/SGST 144213.95` each (party `ISDEEMEDPOSITIVE Yes`).

---

## 6. How it was tested (reproduce locally)

The pipeline runs as an HTTP server (POST-only) on `127.0.0.1:5003`. The local
`.env` has `SUPABASE_URL` (TEST), Nanonets `RUNPOD_POD_URL/KEY`, `API_KEY`. The
sale-OCR (MiniCPM) and push-queue env are **not** in `.env`, so set them when
launching:

```bash
cd /d/Desktop/TallyBridge/parsing
set -a; source .env; set +a
export PYTHONPATH="/d/Desktop/TallyBridge/parsing"        # REQUIRED: handler imports purchase_docstrange_pipeline (top-level)
export SALE_OCR_BACKEND=runpod
export MINICPM_RUNPOD_URL="https://api.runpod.ai/v2/vllm-vhm6qdmcjavvps/openai"
export MINICPM_RUNPOD_API_KEY="$RUNPOD_POD_API_KEY"       # the account rpa_ key works on the MiniCPM endpoint
export MINICPM_RUNPOD_TIMEOUT_SECONDS=600
export MINICPM_PUSH_QUEUE_URL="https://tallybridge-backend-950406969086.asia-south1.run.app/api/sync/push-queue"
.venv-ocrvl/Scripts/python.exe server/handler.py --serve     # GET / returns 404 = up (POST-only)
```
POST:
```bash
curl -s -X POST "http://127.0.0.1:5003/docstrange?purchase=all&source=runpod" \
  -H "Content-Type: application/pdf" --data-binary @"/d/Downloads/image sample/emkay.pdf" --max-time 600
curl -s -X POST "http://127.0.0.1:5003/?type=sale&push=queue" \
  -H "Content-Type: application/pdf" --data-binary @"/d/Downloads/sale sample/balaji_sale.pdf" --max-time 600
```

**Gotchas hit:**
- **`PYTHONPATH` must include `parsing/`** or you get
  `ModuleNotFoundError: No module named 'purchase_docstrange_pipeline'`.
- **Windows path mismatch**: Git Bash `/tmp` ≠ Windows-Python `/tmp` (=`D:\tmp`).
  Write curl output to a file inside `parsing/` (e.g. `./_resp.json`) and read it
  with the venv python by relative path, or `cygpath -w`. Also a `cat file | py - <<EOF`
  heredoc *replaces* stdin — pass the path instead.
- The Python module is cached; **restart the server** after editing
  `voucher_builder.py` / `handler.py`.
- Also did a fast **deterministic math unit test** (stubbing OCR + Supabase) that
  exercised both builders incl. the Haryana→CGST/SGST and non-Haryana→IGST branches
  before the OCR run.

---

## 7. Test results (both local AND live GCP — identical)

`BALAJI H/W AGENCIES` confirmed in `ledgers`: `group_name = Sundry Debtors`,
`state = Haryana` ⇒ CGST+SGST.

| | emkay.pdf (Purchase) | balaji_sale.pdf (GST SALE) |
|---|---|---|
| net subtotal | **338,689.08** | **1,602,377.27** |
| GST | **IGST 60,963.92** (18%) | **CGST 144,213.95 + SGST 144,213.95** (9%+9%) |
| party total | **399,653.00** | **1,890,805.17** |
| `discount_total` | **10,474.92** | **12,157.11** |
| discounted lines | 4 @ 3% (all lines) | 4 @ 46% (the "SET TOTEM" lines) |
| sample net | 112104 → 108740.88 | 6285 → 3393.90 |

push_queue rows verified (status `pending`, won't push to Tally until activated):
- **Local run**: purchase `f181880f-…`, sale `c1241051-…`.
- **GCP run**: purchase `579e2fd3-…`, sale `201c1ade-…`.
- Deleted one **bad pre-fix** purchase row `1f7a4f82-…` (had wrong IGST 15.25 from
  before §3d was added).
- All good rows read back from Supabase with `discount_total`, IGST/CGST/SGST, and
  net item amounts intact.

---

## 8. The one semantic shift to be aware of

For the **discounted purchase** case, the voucher **total now equals the printed
`invoice_total`**, because the reliable GST is derived from it (`§3d` rule 1). Item
amounts are still computed independently as `gross × (1 − pct)` per **Q1(b)**; only
the **GST total** leans on the printed grand total (the parsed tax line is
unreliable). If `subtotal` (from gross×(1−pct)) ever diverges from the document's
own net, that small delta is absorbed into the GST line. This is consistent with
"GST after discount" and was accepted (Q1(b) allowed the total to differ; here it
happens to match).

Sale has no such dependency — its GST is computed directly from `SALE_GST_RATE` /
`SALE_IGST_RATE` on the net subtotal.

---

## 9. Deploy state (GCP Cloud Run)

| Thing | Value |
|---|---|
| Project / region | `tallybridge-test-ocr` / `asia-south1` |
| Parsing live revision | **`tallybridge-parsing-00014-cz4`** (100% traffic) |
| Parsing URL | `https://tallybridge-parsing-950406969086.asia-south1.run.app` |
| Backend (unchanged) | `https://tallybridge-backend-950406969086.asia-south1.run.app` |
| gcloud bin / account | `C:\Users\panka\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd` / `rohan.psom@gmail.com` |

Redeploy command (env vars persist on the service):
```
"<gcloud.cmd>" run deploy tallybridge-parsing --source "D:/Desktop/TallyBridge/parsing" \
  --region asia-south1 --project tallybridge-test-ocr --min-instances=1 --memory=2Gi --cpu=2 --timeout=3600 --quiet
```
(The `gcloud run deploy` step is gated by Claude Code's auto-mode classifier and
needs explicit user authorization each time.)

RunPod endpoints used: **sale** MiniCPM-V 4.5 `vllm-vhm6qdmcjavvps` (account `rpa_`
key); **purchase** Nanonets `vllm-5fjqw6zdgqxt8g` (`RUNPOD_POD_URL` in `.env`).

---

## 10. Git state — IMPORTANT

- **Uncommitted.** Two modified files: `parsing/server/handler.py`,
  `parsing/purchase/voucher_builder.py`. GCP is **ahead of git** (deployed from
  local source).
- Branch: `TallyBridge-Backend-Refactor`.
- Prior pattern: push to **`tallybridge-test`** only, **not `origin`**. Confirm
  before pushing. No `.env`/secrets. (Also: do not commit the temporary
  `pymupdf`/local test installs.)

---

## 11. Databases — DO NOT CONFUSE (safety-critical)

| Supabase ref | Which | Notes |
|---|---|---|
| `yynuuysvjeipawzfbeme` | **TEST** | only DB/backend safe to write. Stock/party/rate/state + push_queue all here. |
| `ztugwhevemibdrzqafyw` | **CLIENT PRODUCTION** | client backend `tallybridge-h3do` → client TallyPrime. **NEVER write / never point Control Plane here while testing.** |

Local `.env` `SUPABASE_URL` = TEST; push queue URL used in tests = TEST GCP backend.
`.env` never committed.

---

## 12. Open items / carry-overs

1. **Commit** the two files to `tallybridge-test` (pending user OK).
2. **Backend deploy not needed this round** — but if a *new* top-level field is ever
   added it must be whitelisted in `normalizePushVoucherPayload` (`sync.ts`).
3. **Purchase tax robustness** depends on a reliable `invoice_total`. If a vendor's
   grand total is mis-parsed *and* the tax line is also bad, GST can be wrong —
   consider parsing the GST *rate* explicitly per line.
4. **Sale state lookup is exact-match** on `ledgers.name`. If the matched
   `party_name` doesn't exactly equal the ledger `name`, `fetch_party_state` returns
   "" and the path **defaults to inter-state IGST**. Watch for name-format drift.
5. **No dedup/idempotency** — re-running a PDF still creates a new push_queue row
   (observed; `SALE-<ts>` / re-pushed purchase). Carry-over from prior handouts.
6. Carry-overs: confidence gating, pgvector matching, SEC/SET non-deterministic
   match (see `..._sale_source_payload_and_ocr_quality.md`).

---

## 13. Quick reference

| Thing | Value |
|---|---|
| Files changed | `parsing/server/handler.py`, `parsing/purchase/voucher_builder.py` |
| Sale builder | `build_sale_voucher_payload` (+ `fetch_party_state`) in `handler.py` |
| Purchase builder | `build_voucher_payload` in `purchase/voucher_builder.py` |
| Net formula | `amount = gross × (1 − discount_pct/100)`, `gross = qty × rate` |
| Sale GST rule | Haryana ⇒ CGST+SGST (9/9); else IGST (18). State from `ledgers` (Sundry Debtors). |
| Purchase GST rule | on net; trust `invoice_total − net` when doc tax implausible (split across tax ledgers) |
| Parsing live rev | `tallybridge-parsing-00014-cz4` |
| Sale endpoint (push) | `…/?type=sale&push=queue` (PDF binary) |
| Purchase endpoint | `…/docstrange?purchase=all&source=runpod` (PDF binary) |
| Test files | `D:\Downloads\image sample\emkay.pdf`, `D:\Downloads\sale sample\balaji_sale.pdf` |
| TEST Supabase (safe) | `yynuuysvjeipawzfbeme` |
| CLIENT (NEVER) | `ztugwhevemibdrzqafyw` / `tallybridge-h3do` |
| GCP push_queue jobs (this session) | purchase `579e2fd3`, sale `201c1ade` |
