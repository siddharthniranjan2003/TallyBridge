# Session Handout — MRP (last purchase rate) + `_CLIENT` Supabase override for reorder-levels

> Scope: everything done from the last context compact through now. Read this to
> continue in a fresh chat with full context. Branch: `TallyBridge-Backend-Refactor`.

---

## 0. TL;DR (current live state)

- **GCP service `tallybridge-backend`** (Cloud Run, project `tallybridge-test-ocr`, region `asia-south1`) is serving **revision `00011-8x4`** at **100% traffic**, built from local commit **`1671a3e`**.
- That deployed commit does **two** things on the `/api/sync/reorder-levels` endpoint **only**:
  1. **MRP change** — `purchase_rate` = the stored `voucher_items.rate` from each item's **most recent purchase voucher line** (ordered by `vouchers.date` DESC, across all history). Not the old 1-month average.
  2. **`_CLIENT` override** — reads its Supabase from `SUPABASE_URL_Client` + `SUPABASE_SERVICE_KEY_CLIENT` (the **client** project `ztugwhevemibdrzqafyw`), and authenticates `x-api-key` against `API_KEY_CLIENT`.
- The rest of the backend is **unchanged** (still shared `supabase` client → `yynuuysvjeipawzfbeme`, global `API_KEY`).
- **Verified live**: a real call returned 17,045 items with `purchase_rate` populated even where `purchase_qty_1m = 0` — proof the last-purchase-rate logic is active (old logic would be null there).
- **Commits are LOCAL ONLY** — nothing pushed to GitHub yet.

---

## 1. The endpoint

`backend/src/routes/sync.ts`

- `GET /api/sync/reorder-levels` — base (needs a company param; comment says "auto-detect" but code requires `company_name`/`company_id`/`company_guid`).
- `GET /api/sync/reorder-levels/:reportKey` — filtered to one report.

**Live URL (for n8n):**
```
https://tallybridge-backend-950406969086.asia-south1.run.app/api/sync/reorder-levels/{{ $json.reportId }}?company_name=K V ENTERPRISES&format=csv
```
Alias host: `https://tallybridge-backend-xx3yz3b3kq-el.a.run.app`

**Auth header required** (changed this session): `x-api-key: <API_KEY_CLIENT value>` (the `sb_publishable_IRsM8wDF6w9OyiPegwB2cw_a9…` publishable key). The old global `API_KEY` is now **rejected (401)** for this endpoint.

**Query params:** `company_name` | `company_id` | `company_guid`, `threshold` (₹ min, non-neg), `limit` (pos int), `format` (`json`|`csv`).

**`:reportKey` path param** resolves via aliases (id / key / display-name, case+punctuation-insensitive):

| reportId | key | name |
|---|---|---|
| R1 | ACT_NOW | ACT NOW (reorder-urgent) |
| R2 | HERO_SKU_HEALTH | HERO SKU HEALTH |
| R3 | DEAD_CAPITAL | DEAD CAPITAL |
| R4 | BUYING_MISTAKES | BUYING MISTAKES |
| R5 | WIND_DOWN | WIND-DOWN |
| R6 | RISK_WATCH | RISK WATCH |
| R7 | FULL_PORTFOLIO_HEALTH | FULL PORTFOLIO HEALTH (= all classified) |

**CSV output**: columns `stock_item_name, sales_qty_6m_avg, purchase_qty_1m, closing_stock_qty, purchase_rate, sales_amount, purchase_amount, closing_stock_amount, scenario_name`. Numeric cols rounded to int + `en-IN` formatted (so they contain commas → quoted). CRLF. Filename `reorder-levels-{reportId}-{company}-{date}.csv`. CSV returns only `items`, not the JSON summary envelope.

---

## 2. Report logic (identification layer) — unchanged this session

Three signals per item (compared in paise):
- **A** = `avg_sale_6m` = (6-mo GST-SALE item value) ÷ 6
- **P** = `last_month_purchase` = purchase item value in last 1 month
- **S** = `closing_stock_value` = `stock_items.closing_value` (abs)

`classifyInventoryScenarioV2(A, P, S)` → one of 15 scenarios → each maps to report keys (R7 always included). Reorder-urgent set R1 = STARVE_ZERO/CRITICAL/WATCH + PINCH.

> IMPORTANT: classification uses **native booked amounts** (A/P/S), **NOT** `purchase_rate`. The MRP change does **not** alter which report bucket an item lands in — only the rate/value columns.

---

## 3. The MRP change (`purchase_rate`)

Originated in commit `652a440` "mrp update(purchaseRate)"; carried into the deployed `1671a3e`.

**Before → After** (`buildInventoryIntelligenceReport`):
```ts
// OLD: 1-month averaged rate
purchaseRateRaw = purchaseQuantity1mRaw > 0 ? lastMonthPurchaseRaw / purchaseQuantity1mRaw : null;
// NEW: last purchase rate
purchaseRateRaw = lastPurchaseRateByItem.get(stockItemName) ?? null;
```

**New helper `fetchLastPurchaseRateByItem(companyId)`:**
1. Fetch all **non-cancelled** vouchers for the company, ordered `date` DESC then `id` DESC.
2. Keep purchase types via existing `isPurchaseVoucherType` (matches `GST PURCHASE` and anything containing "purchase", excludes "order"). Rank them (0 = newest).
3. Fetch their `voucher_items (stock_item_name, rate, voucher_id)` in chunks of 100.
4. Per `stock_item_name`, keep `rate` from the lowest rank (newest). **Skip `rate <= 0` / non-finite** (falls through to next-newest).

**Locked decisions (user-confirmed):**
- Source = stored `voucher_items.rate` **gross** (ignores `discount_pct`).
- Lookback = **all purchase history** (no date window).
- Downstream = **re-value** `sales_amount` / `purchase_amount` / `closing_stock_amount` (= qty × purchase_rate) and the `threshold` filter at the new rate.

**Side effect (intended):** items that sell but had no purchase last month (`purchase_qty_1m = 0`) used to show blank money columns; they now populate from their last historical purchase rate.

**Schema facts** (`backend/full_schema.sql`): `voucher_items` has a stored `rate` column (line 99); `date` and `voucher_type` live on `vouchers` (not `voucher_items`). `idx_voucher_items_voucher_id` exists.

---

## 4. The `_CLIENT` override (commit `1671a3e`) — reorder endpoint ONLY

Added to `sync.ts`:

```ts
import { createClient } from "@supabase/supabase-js";
import { timingSafeEqual } from "crypto";
// types Request, NextFunction added to the express import

const reorderSupabase =
  process.env.SUPABASE_URL_Client && process.env.SUPABASE_SERVICE_KEY_CLIENT
    ? createClient(process.env.SUPABASE_URL_Client, process.env.SUPABASE_SERVICE_KEY_CLIENT)
    : supabase; // fallback to shared client if env unset

function requireClientApiKey(req, res, next) {
  // validates x-api-key (timingSafeEqual) against process.env.API_KEY_CLIENT; 401 otherwise
}
```

- `resolveCompanyLookup` was **parameterized**: `options?: { requireSuccessfulSync?: boolean; client?: typeof supabase }`, `const db = options?.client ?? supabase;` — its 3 `companies` queries now use `db`. ~20 other callers unaffected (default = shared `supabase`).
- **6 reorder-path query sites** swapped `supabase` → `reorderSupabase`: `aggregateVoucherItemMetricsByName` (voucher_items), `fetchLastPurchaseRateByItem` (vouchers + voucher_items), `buildInventoryIntelligenceReport` (sale vouchers, purchase vouchers, stock_items).
- Both reorder routes: `requireApiKey` → `requireClientApiKey`, and pass `{ client: reorderSupabase }` to `resolveCompanyLookup`.

> ⚠️ Env var name **casing is exact / case-sensitive on Cloud Run**:
> `SUPABASE_URL_Client` (mixed case — capital C), `SUPABASE_SERVICE_KEY_CLIENT`, `API_KEY_CLIENT`.
> The code reads `process.env.SUPABASE_URL_Client` literally.

A hardcoded-credentials version was tried first, then **reverted** (`git restore`) and replaced with this env-based approach. No secrets are in the source.

---

## 5. Supabase projects & env vars

**Two relevant projects:**
| Project ref | Role | Used by |
|---|---|---|
| `yynuuysvjeipawzfbeme` | TEST | rest of backend (shared `supabase` client, `SUPABASE_URL`) |
| `ztugwhevemibdrzqafyw` | CLIENT | reorder-levels endpoint only (`SUPABASE_URL_Client`) |
| `hbaadljcliqzwjtpobex` | unrelated | n8n `gsheet_appscript.js` only (ignore) |

**Cloud Run env vars on `tallybridge-backend` (all confirmed present on the service):**
```
SUPABASE_URL, SUPABASE_SERVICE_KEY, API_KEY, FIREBASE_SERVICE_ACCOUNT_B64,
SUPABASE_URL_Client, SUPABASE_SERVICE_KEY_CLIENT, API_KEY_CLIENT
```
- `SUPABASE_URL_Client = https://ztugwhevemibdrzqafyw.supabase.co`
- `SUPABASE_SERVICE_KEY_CLIENT` = client project service-role JWT
- `API_KEY_CLIENT` = `sb_publishable_IRsM8wDF6w9OyiPegwB2cw_a9…` (Supabase publishable key, reused here purely as the endpoint's shared x-api-key secret)

Both shared and client clients use **service-role** keys (bypass RLS). The publishable key is **not** used for DB access — only as the caller token.

---

## 6. Git history (branch `TallyBridge-Backend-Refactor`, all LOCAL only)

```
1671a3e  feat(reorder-levels): serve client Supabase + API_KEY_CLIENT via *_CLIENT env   ← DEPLOYED HEAD
6726bf5  Reapply "mrp update(purchaseRate)"
141f24e  Revert "mrp update(purchaseRate)"
652a440  mrp update(purchaseRate)
56e760f  feat(push): apply per-item discount % when pushing vouchers to TallyPrime
```
- `652a440` is an ancestor of `1671a3e` (verified). The revert+reapply cancel out, so MRP logic is present in HEAD.
- Working tree is **clean** (HEAD == deployed source).
- Not pushed to GitHub (`siddharthniranjan2003/tallybridge-test`, private).

---

## 7. GCP / Cloud Run

- **Build path:** `backend/Dockerfile` runs `npm run build` (`tsc` → `dist/`) then `node dist/index.js`. So **`sync.ts` is the only source that matters**; the committed `backend/src/routes/sync.js` is stale/unused.
- **Deploy command used:**
  ```bash
  gcloud run deploy tallybridge-backend --source "D:/Desktop/TallyBridge/backend" \
    --region asia-south1 --project tallybridge-test-ocr --timeout=3600 --quiet
  ```
- **Service URL:** `https://tallybridge-backend-950406969086.asia-south1.run.app`

**⚠️ Traffic-pinning gotcha (hit twice this session):** if traffic gets pinned to a specific revision (`latestRevision=None`), `gcloud run deploy` builds a new revision but it lands at **0%** and does NOT auto-promote. Fixed with:
```bash
gcloud run services update-traffic tallybridge-backend --to-latest \
  --region asia-south1 --project tallybridge-test-ocr
```
Service now **follows LATEST** again, so future deploys auto-promote.

**Revision trail this session:** `00004-5s9` (pre-MRP) → `00005-844` (first MRP) → `00006`–`00009` (revert/redeploys/console env saves) → `00010-9gv` (_CLIENT override) → **`00011-8x4` (current live = `1671a3e`)**.

---

## 8. Live verification (done)

Call: `GET /api/sync/reorder-levels?company_name=K%20V%20ENTERPRISES&limit=8` with `x-api-key = API_KEY_CLIENT` (pulled from Cloud Run env; never printed). Result: **HTTP 200**, `company_name = K V ENTERPRISES`, `total_items_scanned = 17045`, `classified = 17045`.

Sample rows (note `purchase_qty_1m = 0` yet `purchase_rate` populated → confirms last-purchase-rate logic live):
| stock_item_name | purchase_rate | purchase_qty_1m |
|---|---|---|
| TAP 24 X 3 SET | 1224.36 | 0 |
| HSS EXTRA LONG DRILL 5 X 200 MIRANDA | 1781 | 0 |
| C-20 DEBURING BLADE | 60 | 0 |
| WD-40 (420ML) | 259.18 | 0 |

---

## 9. Open items / next steps

- [ ] **Push commits to GitHub** (`tallybridge-test` remote) — currently local-only.
- [ ] Optional: **squash** the `revert`+`reapply` pair (`141f24e`,`6726bf5`) for clean history (safe — not pushed).
- [ ] **n8n:** ensure the reorder HTTP node sends `x-api-key = API_KEY_CLIENT` value (not old `API_KEY`), and URL-encode `company_name`.
- [ ] Note: the all-history purchase fetch in `fetchLastPurchaseRateByItem` runs per report call — fine for current data (17k items OK), watch if a company's purchase history grows very large.

---

## 10. Key files

| File | Purpose |
|---|---|
| `backend/src/routes/sync.ts` | All reorder logic, MRP helper, `reorderSupabase`, `requireClientApiKey` |
| `backend/src/db/supabase.ts` | Shared client (`SUPABASE_URL`/`SUPABASE_SERVICE_KEY`) |
| `backend/src/middleware/auth.ts` | Global `requireApiKey` (Firebase JWT + `API_KEY`) |
| `backend/Dockerfile` | `tsc` build → `node dist/index.js` |
| `backend/full_schema.sql` | Schema (`voucher_items.rate` @99; `date`/`voucher_type` on `vouchers`) |
