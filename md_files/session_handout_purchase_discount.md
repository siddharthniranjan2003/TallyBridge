# TallyBridge — Purchase Path: Per-Item discount_pct + voucher discount_total: Session Handout

Context-transfer doc for a new chat. This session added **per-item derived
discount percentage** and a **voucher-level discount total** to the *purchase*
invoice pipeline (`/docstrange?purchase=all&source=runpod`), end-to-end and
persisted to `push_queue`.

Sibling docs (read for the sale path): `session_handout_sale_discount.md`,
`session_handout_sale_minicpm_pushqueue.md`.

Last updated: 2026-06-02.

---

## 0. TL;DR — what this session did

1. Each **purchase voucher item** now carries `discount_pct` = a **derived
   effective percent** = `rupee_discount / (rate × qty) × 100`, computed
   uniformly across all three discount cases.
2. The **voucher payload** now has `discount_total` (sum of all per-item rupee
   discounts) inserted **right below `narration`**.
3. **Backend** whitelists `discount_total` so it survives the `push_queue`
   reshape (`discount_pct` per item was already whitelisted from the sale work).
4. **Deployed both services to GCP, committed `29ebb28`, pushed to
   `tallybridge-test`.**

✅ Live-verified on `cp.pdf` (Case 2) and `emkay.pdf` (Case 3): both
`discount_total` and per-item `discount_pct` present in the HTTP response **and**
persisted in the `push_queue` rows.

**Important semantics:** discount is **metadata-only** — item `amount` and the
GST/ledger math are unchanged (discount is NOT subtracted). Same stance as sale.

---

## 1. Where purchase discount comes from (vs sale)

- **Purchase** discount comes from the **invoice itself** (OCR'd), NOT from
  Supabase billing history. (Contrast: the *sale* path looks up `discount_pct`
  from `voucher_items` history via an RPC.)
- Two OCR channels feed it:
  - **Per-line `discount_pct`** — vendor-specific row parsers pull a `Disc. %`
    column into `numeric_rows[i].discount_pct` → `PurchaseRawItem.discount_pct`
    (e.g. CP, RR).
  - **Header-level total** — `_extract_discount_fields` (vlm_vendor_parser.py)
    reads a `DISCOUNT` line into `header_data.discount_percent` /
    `header_data.discount_amount` (e.g. EMKAY/ET, GNL).

---

## 2. The three discount cases (`compute_item_discounts`, voucher_builder.py)

Produces an **absolute rupee discount per item**, priority order:

| Case | Trigger | Rupee discount per item |
|---|---|---|
| 1 — none | no discount anywhere | `0` |
| 2 — per-line % | any item has `discount_pct` > 0 | `gross × pct/100`, `gross = rate×qty`; **short-circuits** (header total ignored) |
| 3 — total outside table | header `discount_amount` or `discount_percent` | distributed pro-rata: `(item.amount / Σ amounts) × total`; rounding remainder pushed onto last item |

Case 3 detail: `total = discount_amount` if **> 0**, else falls back to
`Σamount × discount_percent/100`. (Seen live: emkay's `discount_amount` was
**negative −10474.92**, so it fell back to the 3% rate → total 11989.05.)

### The derived percent (this session's addition)
In `build_voucher_payload`, after the rupee discounts are computed:

```python
gross = round2(decimal_value(item["rate"]) * decimal_value(item["quantity"]))
discount_pct = round2(discount_value / gross * 100) if gross > 0 else 0
```

- **Case 2:** recovers the exact column percent (e.g. CP item: 7600/(950×20)=40%).
- **Case 3:** yields the effective percent of the distributed share. For a flat
  header % where `amount ≈ rate×qty`, it collapses back to the header rate
  (emkay → 3% on every line). It only varies per item when effective discounts
  differ (lump rupee discount with `amount ≠ rate×qty`, or mixed per-line %).

---

## 3. Code touched this session

| File | Change |
|---|---|
| `parsing/purchase/voucher_builder.py` | `build_voucher_payload`: compute `discount_total = round2(sum(item_discounts))`; rewrote the `voucher_items` comprehension into a loop that adds **`discount_pct`** (derived) to each item (kept the existing rupee `discount` key); inserted `"discount_total"` into `voucher_payload` right after `"narration"`. `compute_item_discounts` itself unchanged. |
| `backend/src/routes/sync.ts` | `normalizePushVoucherPayload`: added `discount_total: normalizeFiniteNumber(raw.discount_total) ?? 0` to the top-level voucher whitelist (after `narration`). Item-level `discount_pct` was already whitelisted. |

**Item object in the pushed `voucher_payload` (Python/HTTP response):**
```json
{ "stock_item_name":"...", "quantity":20.0, "rate":950.0, "amount":11400.0,
  "discount":7600.0, "discount_pct":40.0, "unit":"NOS", "godown_name":"Main Location" }
```

---

## 4. What persists vs what is stripped (KEY GOTCHA)

The backend `normalizePushVoucherPayload` rebuilds the voucher from a **strict
whitelist** — anything not listed is dropped on the `push_queue` insert.

| Field | HTTP response | push_queue row |
|---|---|---|
| per-item rupee `discount` | ✅ present | ❌ **null** (NOT whitelisted) |
| per-item `discount_pct` | ✅ present | ✅ persisted |
| top-level `discount_total` | ✅ present | ✅ persisted |

So `discount_total` (computed in Python as the true sum of per-item rupees)
persists, and per-item `discount_pct` persists, but the per-item **rupee**
`discount` does NOT. This matched the goal (only `discount_pct` per item +
`discount_total`). To also persist the rupee value: add `discount` to the item
whitelist in `sync.ts` (line ~233, next to `discount_pct`) + redeploy backend.

---

## 5. Deploy state (GCP Cloud Run, project `tallybridge-test-ocr`, asia-south1)

| Service | Live revision | URL (both forms route to the same service) |
|---|---|---|
| `tallybridge-parsing` | **`tallybridge-parsing-00010-fsb`** (100% traffic, `--min-instances=1`, warm) | `…-xx3yz3b3kq-el.a.run.app` / `…-950406969086.asia-south1.run.app` |
| `tallybridge-backend` | **`tallybridge-backend-00004-5s9`** | `…-xx3yz3b3kq-el.a.run.app` / `…-950406969086.asia-south1.run.app` |

Redeploy commands (env vars persist; absolute source path required):
```
gcloud run deploy tallybridge-parsing --source "D:/Desktop/TallyBridge/parsing" \
  --region asia-south1 --project tallybridge-test-ocr --min-instances=1 --memory=2Gi --cpu=2 --timeout=3600 --quiet
gcloud run deploy tallybridge-backend --source "D:/Desktop/TallyBridge/backend" \
  --region asia-south1 --project tallybridge-test-ocr --timeout=3600 --quiet
```
gcloud bin: `C:\Users\panka\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd` (account `rohan.psom@gmail.com`).
⚠️ **Don't deploy both in parallel** — concurrent Cloud Build status polling hit
HTTP 429 (`GetRequestsPerMinutePerProject`, limit 60/min) and failed the second
deploy. Deploy sequentially. Builds take ~3–5 min each.

---

## 6. Git state

- **Commit:** `29ebb28` — `feat(purchase): per-item derived discount_pct + voucher discount_total`
  (2 files: `voucher_builder.py`, `sync.ts`; +23/−12). Surgical — unrelated
  dirty-tree churn (package files, electron-builder, graphify cache, engine exe)
  NOT staged.
- **Branch:** `TallyBridge-Backend-Refactor`.
- **Pushed:** `b24fa33..29ebb28` → `tallybridge-test`
  (`https://github.com/siddharthniranjan2003/tallybridge-test.git`).
- Origin `TallyBridge` was NOT pushed this session.
- Prior commit `b24fa33` = the sale-discount work (previous session).

---

## 7. Test results (live, this session)

POST to `…/docstrange?purchase=all&source=runpod`, Content-Type application/pdf,
binary body. RunPod OCR ~170–190s per invoice (warm).

| Case | Invoice | Vendor / party | push_queue job | discount_total | per-item discount_pct |
|---|---|---|---|---|---|
| 2 — per-line % | `cp.pdf` | CP / CP GRAT-EX MANUFACTURING | `35a18ae6…` | **146663.5** | 40, 30, 30, 40, 30, 40 |
| 3 — distributed total | `emkay.pdf` | ET / EMKAY TOOLS LIMITED | `8b685a72…` | **11989.05** | 3, 3, 3, 3 |

Both verified in the actual `push_queue` rows (Supabase MCP `execute_sql` on
`yynuuysvjeipawzfbeme`), not just the HTTP response. Both rows `pending`.
Sample invoices live in `D:\Downloads\image sample\` (addison, cp, emkay,
pidilite, rr, saint global, totem, wikus — each `.pdf` + reference `.csv`).

---

## 8. Databases — DO NOT CONFUSE (safety-critical)

| Supabase ref | Which | Notes |
|---|---|---|
| `yynuuysvjeipawzfbeme` | **TEST** | only DB reachable via MCP; matching/stock/push target. **Safe to write.** |
| `ztugwhevemibdrzqafyw` | **CLIENT PRODUCTION** | client backend `tallybridge-h3do` → client TallyPrime. **NEVER write.** |

Parsing service `MINICPM_PUSH_QUEUE_URL` must point only at the TEST backend.
`.env` files never committed.

---

## 9. Open items / gaps

1. **Discount math is metadata-only** — `amount`/GST not reduced. Apply if
   wanted: `amount = qty×rate×(1−pct/100)` + recompute GST on discounted subtotal.
2. **Per-item rupee `discount` not persisted** — only `discount_pct` +
   `discount_total` land in the row. Add `discount` to the `sync.ts` item
   whitelist if the rupee value is needed downstream.
3. **Negative `discount_amount` quirk** — emkay's header `discount_amount` came
   through negative, so Case 3 fell back to `discount_percent`. If a vendor
   states a real positive rupee discount it would be used directly. Worth
   confirming the OCR sign convention if a vendor's rupee discount looks wrong.
4. **Dedup/idempotency** — re-POSTing the same invoice creates a new push_queue
   row each time (no dedup on voucher_number).
5. Purchase pipeline general gaps (confidence gating, etc.) unchanged from prior
   handouts.

---

## 10. Quick reference

| Thing | Value |
|---|---|
| Purchase endpoint | `https://tallybridge-parsing-xx3yz3b3kq-el.a.run.app/docstrange?purchase=all&source=runpod` (PDF binary) |
| Backend | `https://tallybridge-backend-xx3yz3b3kq-el.a.run.app` |
| Parsing live rev | `tallybridge-parsing-00010-fsb` |
| Backend live rev | `tallybridge-backend-00004-5s9` |
| Git commit | `29ebb28` on `TallyBridge-Backend-Refactor` (pushed to `tallybridge-test`) |
| Discount math | `parsing/purchase/voucher_builder.py` → `compute_item_discounts` + `build_voucher_payload` |
| Backend whitelist | `backend/src/routes/sync.ts` → `normalizePushVoucherPayload` |
| RunPod OCR model (purchase) | `nanonets/Nanonets-OCR2-3B` (env `RUNPOD_MODEL`) |
| Default purchase company | `K V ENTERPRISES` (env `MINICPM_PURCHASE_COMPANY`) |
| TEST Supabase (safe) | `yynuuysvjeipawzfbeme` |
| CLIENT Supabase (NEVER write) | `ztugwhevemibdrzqafyw` / `tallybridge-h3do` |
| Sample purchase PDFs | `D:\Downloads\image sample\` |
| Sale-path sibling handout | `session_handout_sale_discount.md` |
