# Reorder Level — Test Run 2 Results

> Date context: 31/03/2019 (current date for this test)
> Window: last 3 months = 01/01/2019 to 31/03/2019
> Company: K.V. ENTERPRISES 18-19
> Formula: raw 3-month purchase total = reorder level (no averaging, no lead time)

---

## Formula Used

```
reorder_level  = total_qty_purchased_3m   (raw total, no division)
needs_reorder  = closing_qty <= reorder_level
```

This differs from Test Run 1 which used `reorder_level = total_qty / 3` (avg monthly).

---

## How It Was Calculated

### Step 1 — Get Company ID
Queried `companies` table → found one company:
- **K.V. ENTERPRISES 18-19** → `id = abf1cdd2-e919-4cea-a902-8cab3e71c9fc`

**Table:** `companies`
**Method:** GET /rest/v1/companies (no filters)

---

### Step 2 — Get Purchase Vouchers in Window
Queried `purchases` table filtered by:
- `company_id = abf1cdd2...`
- `date >= 2019-01-01` and `date <= 2019-03-31`
- `is_cancelled = false`

**Table:** `purchases`
**Method:** GET /rest/v1/purchases
**Columns returned:** `voucher_id` only

Result: **869 purchase vouchers** in the 3-month window

---

### Step 3 — Fetch Line Items for All 869 Vouchers
Queried `voucher_items` with `voucher_id IN (...)` in **9 batches of 100 IDs each**

**Table:** `voucher_items`
**Method:** GET /rest/v1/voucher_items × 9 batches
**Filter per batch:** `voucher_id=in.(id1,id2,...,id100)`
**Why batched:** Single IN clause with 869 UUIDs would exceed URL length limits; batched at 100 per request.
**Local processing:** Sum `quantity` per `stock_item_name` in Node.js after all 9 fetches.

Result: **2,852 line item rows** → aggregated locally → **1,753 unique items** purchased

Top 10 by total quantity:

| Rank | Item | Total Qty (3 months) |
|------|------|----------------------|
| 1 | C-10 DEBURING BLADE | 13,000 NOS |
| 2 | WHITE GLASS | 5,000 NOS |
| 3 | WATER PROOF PAPER NO- 320 JOHNOKEY | 4,240 NOS |
| 4 | CIRCLIP INTERNAL 40 | 4,000 NOS |
| 5 | ZIRKON DISC 4" G36 | 3,700 NOS |
| 6 | HSS DRILL 3.1 MIRANDA GOLD | 3,420 NOS |
| 7 | HSS BLADE 12 X 1/2 MIRANDA | 3,000 NOS |
| 8 | NOSE MASK PAPER | 3,000 NOS |
| 9 | PARTING WHEEL 300 X 2 JKA | 2,954 NOS |
| 10 | S S COTTER PIN 1/8 X 2-1/2 | 2,400 NOS |

Items selected for test: **C-10 DEBURING BLADE** and **WHITE GLASS** (ranks 1 and 2)

---

### Step 4 — Get Current Closing Stock
Queried `stock_items` for the 2 selected items

**Table:** `stock_items`
**Method:** GET /rest/v1/stock_items
**Filters:** `company_id = abf1cdd2...`, `name=in.(C-10 DEBURING BLADE,WHITE GLASS)`
**Columns returned:** `name`, `closing_qty`, `unit`
**Note:** `closing_qty` is the end-of-FY snapshot as of 31/03/2019

| Item | Closing Qty |
|------|-------------|
| C-10 DEBURING BLADE | 5,650 NOS |
| WHITE GLASS | 0 NOS |

---

## Reorder Level Calculation

**Formula:**
```
reorder_level  = total_qty_purchased_3m
needs_reorder  = closing_qty <= reorder_level
```

| Item | Total Qty (3m) | Reorder Level | Closing Stock | Condition | Status |
|------|---------------|---------------|---------------|-----------|--------|
| C-10 DEBURING BLADE | 13,000 | 13,000 NOS | 5,650 NOS | 5,650 ≤ 13,000 | **REORDER NOW** |
| WHITE GLASS | 5,000 | 5,000 NOS | 0 NOS | 0 ≤ 5,000 | **REORDER NOW** |

Both items need reorder.

---

## Supabase Interaction Summary

| Step | Table | Method | Filter |
|------|-------|--------|--------|
| 1 | `companies` | GET REST | none |
| 2 | `purchases` | GET REST | company_id, date range, is_cancelled |
| 3 | `voucher_items` | GET REST × 9 batches | voucher_id IN (100 IDs per batch) |
| 4 | `stock_items` | GET REST | company_id, name IN (2 items) |

All queries used Supabase PostgREST REST API. Aggregation (sum qty per item) was done locally in Node.js after fetching raw rows.

---

## Comparison: Test Run 1 vs Test Run 2

| Item | Closing Stock | Reorder Level (Run 1: avg/month) | Status (Run 1) | Reorder Level (Run 2: raw total) | Status (Run 2) |
|------|---------------|----------------------------------|----------------|----------------------------------|----------------|
| C-10 DEBURING BLADE | 5,650 | 4,333 | OK | 13,000 | REORDER NOW |
| WHITE GLASS | 0 | 1,667 | REORDER NOW | 5,000 | REORDER NOW |

Run 2 uses a stricter threshold (3× higher reorder level) — it asks "do I have enough stock to cover the full 3-month purchase volume?" rather than "do I have one month's worth of buffer?"

---

## Notes

- Raw-total formula is the most conservative reorder trigger — any closing stock below 3 months of purchases flags as reorder
- 1,753 unique items in the report → UI will need search/filter
- An RPC SQL function (Postgres-side aggregation) would reduce the 9-batch round trips to 1 round trip at scale
