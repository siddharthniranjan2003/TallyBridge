# TallyBridge — Sale Path: Per-Item Discount on push_queue: Session Handout

Context-transfer doc for a new chat. Picks up after
`session_handout_sale_minicpm_pushqueue.md` (MiniCPM wiring + auto push to push_queue).
This session added **per-item discount** to the sale voucher pipeline, end-to-end.

Last updated: 2026-06-02.

---

## 0. TL;DR — what this session did

1. **Denormalized `voucher_items`** — added `date` + `party_name` columns (backfilled
   from the parent `vouchers` row) so the table can be filtered/sorted by party+date
   without a join. (`discount_pct` column already existed.)
2. **Extended the rate RPC** `get_latest_rates_for_party` to also return `discount_pct`
   (same latest-date row as the rate → guaranteed consistent, one round-trip).
3. **Parsing** now stamps each sale item with its latest same-party `discount_pct`.
4. **Backend** `normalizePushVoucherPayload` now whitelists `discount_pct` so it
   survives the `push_queue` insert (it was being silently stripped).
5. **Deployed both services to GCP, committed, and pushed to `tallybridge-test`.**

✅ Live-verified on `D:\Downloads\sale sample\balaji_sale.pdf`: 20/20 items carry
`discount_pct`, 4 non-zero (@46%), persisted in the `push_queue` row.

**Discount semantics (as specified):** same-party only, latest by `date`, **metadata
only** — `amount` is still `quantity × rate` and GST math is unchanged. Discount is
NOT subtracted.

---

## 1. Where the discount comes from

When the party is identified (e.g. `BALAJI H/W AGENCIES`), for each matched item the
pipeline reads the **latest same-party discount from billing history** in
`voucher_items`:

```sql
-- inside RPC get_latest_rates_for_party(p_party_name)
SELECT DISTINCT ON (vi.stock_item_name)
       vi.stock_item_name, vi.rate, vi.discount_pct
FROM   voucher_items vi
JOIN   vouchers v ON v.id = vi.voucher_id
WHERE  v.party_name ILIKE p_party_name
  AND  vi.rate IS NOT NULL AND vi.rate > 0
  AND  v.is_cancelled = false
ORDER  BY vi.stock_item_name, v.date DESC;   -- newest voucher per item wins
```

- It's the **last discount this same party got** on that item — not a price list/rule.
- If an item has no same-party history (fell back to a different-party rate, or none)
  → `discount_pct = 0`.
- Source DB = **TEST** `yynuuysvjeipawzfbeme` → values reflect test/seed history.

---

## 2. The sale flow (with discount, unchanged elsewhere)

```
POST /?type=sale&push=queue   (PDF binary)
 → Poppler renders PDF → JPEG
 → run_pipeline_for_image:
     • load_sale_stock(company) → LIVE Supabase stock_items
     • MiniCPM-V 4.5 (RunPod) reads line items → rule-engine + rapidfuzz match
     • extract_party_name (header crop → full-image fallback) → vouchers.party_name
 → (push=queue) build_sale_voucher_payload:
     • build_sale_rate_map(party, items):
         – RPC get_latest_rates_for_party → {rate, discount_pct} (same_party)
         – fetch_fallback_rate_for_item   → rate only, discount 0 (different_party)
     • each item gets: stock_item_name, quantity, rate, amount,
                       discount_pct, unit, godown_name
     • GST SALE voucher (party debit; GST SALE/CGST/SGST credit)
 → post_to_push_queue → backend /api/sync/push-queue (x-api-key)
     → normalizePushVoucherPayload (now keeps discount_pct) → push_queue row
 → returns queue_response + sale_voucher_payload + sale_rate_items
```

---

## 3. Files / changes this session

| File | Change |
|---|---|
| **DB** `voucher_items` | added `date date`, `party_name text`, backfilled from parent `vouchers` (215,929 rows). `discount_pct numeric` already existed. |
| **DB** RPC `get_latest_rates_for_party` | now `RETURNS TABLE(stock_item_name, rate, discount_pct)` — dropped+recreated (return type changed) |
| `parsing/server/handler.py` | `fetch_latest_rates_for_party` reads `discount_pct` into the rate map (`source: same_party`); different-party fallback sets `discount_pct: 0.0`; `build_sale_voucher_payload` adds `"discount_pct"` to each item |
| `backend/src/routes/sync.ts` | `normalizePushVoucherPayload` item whitelist now includes `discount_pct: normalizeFiniteNumber(rawItem.discount_pct) ?? 0` |
| `supabase/migrations/20260602_sale_discount_pct.sql` | **new** — captures the RPC change so it's reproducible from git |

**Item object now in the pushed `voucher_payload`:**
```json
{ "stock_item_name":"...", "quantity":5.0, "rate":1257.0, "amount":6285.0,
  "discount_pct":46.0, "unit":"NOS", "godown_name":"Main Location" }
```

---

## 4. Bug caught + fixed this session

First redeploy showed `discount_pct` in the **HTTP response** but `0` items had the key
in the **stored push_queue row**. Root cause: the backend `normalizePushVoucherPayload`
rebuilds each item from a fixed whitelist (lines ~227–236 in `sync.ts`) and dropped the
unknown `discount_pct`. Fix = add it to the whitelist + redeploy the backend. Lesson:
**the push_queue payload is reshaped by the backend, not stored verbatim** — any new
item field must be whitelisted there too.

---

## 5. Deploy state (GCP Cloud Run, project `tallybridge-test-ocr`, asia-south1)

| Service | Live revision | Notes |
|---|---|---|
| `tallybridge-parsing` | **`tallybridge-parsing-00009-t8g`** | discount-aware parsing |
| `tallybridge-backend` | **`tallybridge-backend-00003-8th`** | discount_pct whitelist |

Redeploy commands (env vars persist; absolute source path required):
```
gcloud run deploy tallybridge-parsing --source "D:/Desktop/TallyBridge/parsing" \
  --region asia-south1 --project tallybridge-test-ocr --min-instances=1 --memory=2Gi --cpu=2 --timeout=3600
gcloud run deploy tallybridge-backend --source "D:/Desktop/TallyBridge/backend" \
  --region asia-south1 --project tallybridge-test-ocr --timeout=3600
```
gcloud bin: `C:\Users\panka\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd`
(account `rohan.psom@gmail.com`).

---

## 6. Git state

- **Commit:** `b24fa33` — `feat(sale): per-item discount on GST SALE voucher push`
  (3 files: `handler.py`, `sync.ts`, the migration). Surgical — unrelated dirty-tree
  churn (package files, electron-builder, graphify cache, engine exe) was NOT staged.
- **Branch:** `TallyBridge-Backend-Refactor`.
- **Pushed to `tallybridge-test`:** both `TallyBridge-Backend-Refactor` and
  `gcp-live-backup` now at `b24fa33` (= local HEAD = live GCP). `main` (`cbaba57`) is the
  old seed, untouched.
- `tallybridge-test` remote: `https://github.com/siddharthniranjan2003/tallybridge-test.git`
- (Origin `TallyBridge` was NOT pushed this session — only `tallybridge-test`. Push to
  origin if you want it in sync too.)

---

## 7. Test results (live, this session)

`balaji_sale.pdf` → `…/?type=sale&push=queue`:
- HTTP 200 (~60s warm), `queue_response.ok=true`
- push_queue job `0be9870d-be80-4850-a09a-3a40b927fddf` → `pending`,
  party `BALAJI H/W AGENCIES`, voucher `SALE-20260602081639`, 20 items
- **20/20 items have `discount_pct`** in the stored row; **4 non-zero @ 46%**:
  HSS TAP 6 X 1 / 8 X 1.25 / 10 X 1.5 / 12 X 1.75 SET TOTEM
- The other 16 = different_party rate (discount 0) or no history; 1 unpriced
  (`GRINDING VICE SCREW GV 90 TOOLFAST`, rate 0).

---

## 8. Databases — DO NOT CONFUSE (safety-critical)

| Supabase ref | Which | Notes |
|---|---|---|
| `yynuuysvjeipawzfbeme` | **TEST** | only DB reachable via MCP; rate/discount/stock/party all read here; push target. **Safe to write.** |
| `ztugwhevemibdrzqafyw` | **CLIENT PRODUCTION** | client backend `tallybridge-h3do` → client TallyPrime. **NEVER write.** Not in MCP scope. |

Live GCP env confirmed this session: parsing `SUPABASE_URL=https://yynuuysvjeipawzfbeme.supabase.co`,
`MINICPM_PUSH_QUEUE_URL` → TEST backend. `.env` never committed.

---

## 9. Open items / gaps (carry-over + new)

1. **Discount math is metadata-only** — if you want it applied:
   `amount = qty × rate × (1 − discount_pct/100)` and GST recomputed on the discounted
   subtotal. Currently NOT done (by request).
2. **Discount fallback** — same-party only; different-party items get 0. Could add a
   different-party discount fallback like rate has, if desired.
3. **New voucher_items inserts** — the `date`/`party_name` denormalized columns are a
   one-time backfill; future inserts via the sync path won't auto-fill them unless that
   writer is updated or a trigger is added. (Discount lookup uses the join anyway, so
   it's unaffected; this only matters if you query the denormalized columns directly.)
4. **Sale dedup/idempotency** — `SALE-<timestamp>` still creates a new push_queue row on
   every re-run (re-running balaji_sale.pdf 3× this session = 3 pending rows).
5. **Confidence gating, pgvector matching, real-GST/real-unit** — unchanged from prior
   handout §10/§11.

---

## 10. Quick reference

| Thing | Value |
|---|---|
| Sale endpoint (push) | `https://tallybridge-parsing-950406969086.asia-south1.run.app/?type=sale&push=queue` (PDF binary) |
| Backend | `https://tallybridge-backend-950406969086.asia-south1.run.app` |
| Parsing live rev | `tallybridge-parsing-00009-t8g` |
| Backend live rev | `tallybridge-backend-00003-8th` |
| Git commit | `b24fa33` on `TallyBridge-Backend-Refactor` (pushed to `tallybridge-test`) |
| Discount source | `voucher_items.discount_pct`, latest same-party row via RPC `get_latest_rates_for_party` |
| MiniCPM RunPod (sale) | `vllm-vhm6qdmcjavvps` (`openbmb/MiniCPM-V-4_5`) |
| TEST Supabase (safe) | `yynuuysvjeipawzfbeme` |
| CLIENT Supabase (NEVER write) | `ztugwhevemibdrzqafyw` / `tallybridge-h3do` |
| Sale test PDF | `D:\Downloads\sale sample\balaji_sale.pdf` |
| Prior handout | `session_handout_sale_minicpm_pushqueue.md` |
