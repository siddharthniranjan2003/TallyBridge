# Reorder Level — Complete Explainer

> Company: K.V. ENTERPRISES 18-19
> Date of report: 2026-04-09
> Live data from Supabase via TallyBridge backend

---

## Table of Contents

1. [What is Reorder Level](#1-what-is-reorder-level)
2. [The Formula](#2-the-formula)
3. [Database Schema](#3-database-schema)
4. [How We Interact with Supabase](#4-how-we-interact-with-supabase)
5. [The Endpoint](#5-the-endpoint)
6. [Live Results](#6-live-results)
7. [How the Window Works](#7-how-the-window-works)
8. [What Gets Flagged and What Doesnt](#8-what-gets-flagged-and-what-doesnt)
9. [Telegram Integration](#9-telegram-integration)
10. [Known Limitations](#10-known-limitations)

---

## 1. What is Reorder Level

Reorder level is the stock quantity at which you need to place a new purchase order
to avoid running out. When your closing stock drops to or below this level, it is time to reorder.

In this implementation, the reorder level is not calculated using lead time or safety stock.
It uses a simpler heuristic:

> "If you bought X units in the last 3 months and you have less than X in stock right now,
> you need to reorder."

This answers the question: do I have enough stock to cover what I typically buy in 3 months?

---

## 2. The Formula

```
window_from     = as_of_date - 89 days
window_to       = as_of_date
                  (both ends inclusive = 90 days total)

reorder_trigger = SUM of all quantity purchased in [window_from, window_to]
                  -- raw 90-day total
                  -- no division, no averaging, no lead time

needs_reorder   = reorder_trigger > 0
                  AND closing_qty <= reorder_trigger
```

### Why 89 days and not 90

Both ends of the window are inclusive. Subtracting 89 days from 2019-03-31 gives 2019-01-01.
That is exactly 90 days: Jan 1 to Mar 31 inclusive.
Subtracting 90 would give Dec 31 to Mar 31 = 91 days.

### Why reorder_trigger > 0 guard

Without this guard, an item with zero purchases and zero closing stock would be flagged
because 0 <= 0 is true. That is noise — if nothing was purchased in 3 months,
there is no purchase pattern to trigger a reorder. The guard excludes those items entirely.

---

## 3. Database Schema

The reorder level calculation touches three tables.

### purchases

Stores one row per purchase voucher. Used to identify which vouchers fall in the window.

```
purchases
  id           UUID  primary key
  company_id   UUID  foreign key -> companies.id
  voucher_id   UUID  foreign key -> vouchers.id
  date         DATE  the voucher date
  is_cancelled BOOL  whether the voucher was cancelled
  party_name   TEXT  supplier name
  amount       NUM   total value
  tally_guid   TEXT  unique Tally identifier
```

Key filters used: company_id, is_cancelled = false, date between window_from and window_to.
Only voucher_id is selected — nothing else is needed from this table.

### voucher_items

Stores one row per line item inside a voucher. This is where quantities live.

```
voucher_items
  id               UUID  primary key
  voucher_id       UUID  foreign key -> vouchers.id
  stock_item_name  TEXT  name of the stock item
  quantity         NUM   quantity in this line item
  unit             TEXT  unit of measurement
  rate             NUM   rate per unit
  amount           NUM   line item total
```

Key filters: voucher_id IN (list of purchase voucher IDs).
Selected columns: stock_item_name, quantity.
Aggregated locally: SUM(quantity) per stock_item_name.

### stock_items

Stores one row per stock item with its closing balance at the end of the FY.

```
stock_items
  id           UUID  primary key
  company_id   UUID  foreign key -> companies.id
  name         TEXT  stock item name (joined to voucher_items.stock_item_name)
  unit         TEXT  unit of measurement
  closing_qty  NUM   quantity on hand at FY end
  closing_value NUM  value of closing stock
```

Key filters: company_id.
Selected columns: name, unit, closing_qty.

### Join Path

```
purchases.voucher_id
    --> voucher_items.voucher_id (get quantities per item name)
    --> aggregated in Node.js: Map<stock_item_name, total_qty>

stock_items.name
    --> matched to Map key (get closing_qty)
    --> compute needs_reorder
```

There are no SQL JOINs. All joining happens in Node.js after fetching from each table separately.

---

## 4. How We Interact with Supabase

All Supabase queries use the PostgREST REST API via the Supabase JS client.
No raw SQL, no RPC functions. All aggregation happens in Node.js after fetching rows.

### Step 1 — Auto-detect company

```
Table:   companies
Method:  SELECT
Columns: id
Filter:  none
Limit:   1
```

Returns one row. Extracts company_id for all subsequent queries.

Supabase client call:
```ts
supabase.from("companies").select("id").limit(1).single()
```

Result: company_id = abf1cdd2-e919-4cea-a902-8cab3e71c9fc

---

### Step 2 — Fetch purchase voucher IDs in window

```
Table:   purchases
Method:  SELECT (paginated, 1000 rows per page)
Columns: voucher_id
Filters:
  company_id  = abf1cdd2-...
  is_cancelled = false
  date >= 2019-01-01
  date <= 2019-03-31
Order:   id ASC
```

Supabase client call:
```ts
supabase
  .from("purchases")
  .select("voucher_id")
  .eq("company_id", companyId)
  .eq("is_cancelled", false)
  .gte("date", "2019-01-01")
  .lte("date", "2019-03-31")
  .order("id", { ascending: true })
  .range(0, 999)
```

Result for default window: 869 voucher IDs
These are deduplicated with new Set() in Node.js.

---

### Step 3 — Fetch line items for those vouchers (batched)

869 IDs cannot go into a single IN clause — the URL would be too long.
The backend batches them at 250 IDs per request using selectRowsByIn().

```
Table:   voucher_items
Method:  SELECT x 4 batches
Columns: stock_item_name, quantity, unit
Filter:  voucher_id IN (batch of 250 IDs)
```

Supabase client call per batch:
```ts
supabase
  .from("voucher_items")
  .select("*")
  .in("voucher_id", batchOf250)
```

Batches for 869 IDs:
  Batch 1: IDs 1-250
  Batch 2: IDs 251-500
  Batch 3: IDs 501-750
  Batch 4: IDs 751-869

Total rows returned: approximately 2,852 line item rows.

After all batches are fetched, Node.js aggregates:
```ts
for (const item of lineItems) {
  if (!item.stock_item_name || item.quantity <= 0) continue;
  map.set(item.stock_item_name, (map.get(item.stock_item_name) ?? 0) + item.quantity);
}
```

Result: Map of 1,753 unique item names to their total 3-month purchase quantity.

---

### Step 4 — Fetch all stock items

```
Table:   stock_items
Method:  SELECT (paginated, 1000 rows per page)
Columns: name, unit, closing_qty
Filter:  company_id = abf1cdd2-...
Order:   name ASC
```

Supabase client call:
```ts
supabase
  .from("stock_items")
  .select("name, unit, closing_qty")
  .eq("company_id", companyId)
  .order("name", { ascending: true })
  .range(0, 999)   // repeated for pages 2-7
```

Total rows: 6,891 stock items fetched across 7 pages (1000 per page, last page partial).

---

### Step 5 — Merge and compute in Node.js

No further Supabase calls. Pure in-memory computation.

```ts
for (const stockItem of stockItems) {
  const totalQtyPurchased = map.get(stockItem.name) ?? 0;
  const reorderTrigger    = totalQtyPurchased;
  const closingQty        = Number(stockItem.closing_qty) || 0;
  const needsReorder      = reorderTrigger > 0 && closingQty <= reorderTrigger;

  result.push({
    stock_item_name:     stockItem.name,
    unit:                stockItem.unit,
    total_qty_purchased: totalQtyPurchased,
    reorder_trigger:     reorderTrigger,
    closing_qty:         closingQty,
    needs_reorder:       needsReorder,
  });
}
```

Result: 6,891 items, each with reorder status computed.

---

### Step 6 — Sort and return

```ts
result.sort((a, b) => {
  if (a.needs_reorder !== b.needs_reorder) return a.needs_reorder ? -1 : 1;
  return a.stock_item_name.localeCompare(b.stock_item_name);
});
```

Final order: needs_reorder=true items first (alphabetical), then needs_reorder=false items (alphabetical).

---

### Total Supabase Round Trips

| Step | Table | Calls | Rows Returned |
|------|-------|-------|---------------|
| 1 | companies | 1 | 1 |
| 2 | purchases | 1 | 869 |
| 3 | voucher_items | 4 (batched) | ~2,852 |
| 4 | stock_items | 7 (paginated) | 6,891 |
| Total | -- | 13 | ~10,613 |

All aggregation, joining, and sorting: Node.js in memory.

---

## 5. The Endpoint

```
GET /api/sync/reorder-levels
Host: localhost:3001
Header: x-api-key: sb_publishable_oFyc0kQxjkga2QZw0kq3ZQ_ovmR0P9h
```

### Query Params

| Param | Required | Default | Description |
|-------|----------|---------|-------------|
| as_of_date | No | 2019-03-31 | End of 90-day window in YYYY-MM-DD format |

No company param. Auto-detected.

### Requests

Default window (Jan 1 to Mar 31 2019):
```bash
curl "http://localhost:3001/api/sync/reorder-levels" \
  -H "x-api-key: sb_publishable_oFyc0kQxjkga2QZw0kq3ZQ_ovmR0P9h"
```

Custom end date:
```bash
curl "http://localhost:3001/api/sync/reorder-levels?as_of_date=2019-01-31" \
  -H "x-api-key: sb_publishable_oFyc0kQxjkga2QZw0kq3ZQ_ovmR0P9h"
```

### Response Shape

```json
{
  "as_of_date": "2019-03-31",
  "window_from": "2019-01-01",
  "window_to": "2019-03-31",
  "total_items": 6891,
  "needs_reorder_count": 1320,
  "items": [
    {
      "stock_item_name": "C-10 DEBURING BLADE",
      "unit": "NOS",
      "total_qty_purchased": 13000,
      "reorder_trigger": 13000,
      "closing_qty": 5650,
      "needs_reorder": true
    }
  ]
}
```

### HTTP Error Codes

| Code | Meaning | Cause |
|------|---------|-------|
| 200 | OK | Success |
| 401 | Unauthorized | Missing or wrong x-api-key header |
| 404 | Not Found | No company in database |
| 500 | Server Error | Supabase query failed |

---

## 6. Live Results

Data pulled from Supabase on 2026-04-09 for default window (2019-01-01 to 2019-03-31).

### Summary

| Metric | Value |
|--------|-------|
| Total stock items | 6,891 |
| Items needing reorder | 1,320 |
| Items OK | 5,571 |
| Purchase vouchers in window | 869 |
| Line item rows fetched | ~2,852 |
| Unique items purchased | ~1,753 |

### Top 10 Items Needing Reorder

| # | Item | Total Qty (3m) | Reorder Trigger | Closing Stock | Status |
|---|------|---------------|-----------------|---------------|--------|
| 1 | 2NU VCMW 160404 BN250 | 100 NOS | 100 NOS | 20 NOS | REORDER NOW |
| 2 | ADJUSTABLE WRENCH 10" (1172) TAPARIA | 10 NOS | 10 NOS | 9 NOS | REORDER NOW |
| 3 | ADJUSTABLE WRENCH 12" (1173) TAPARIA | 10 NOS | 10 NOS | 5 NOS | REORDER NOW |
| 4 | ADJUSTABLE WRENCH 4" (1169) TAPARIA | 10 NOS | 10 NOS | 0 NOS | REORDER NOW |
| 5 | ADJUSTABLE WRENCH 8" (1171) TAPARIA | 10 NOS | 10 NOS | 7 NOS | REORDER NOW |
| 6 | AEROSOL-OS | 100 NOS | 100 NOS | 0 NOS | REORDER NOW |
| 7 | ALLEN BOLT 10 X 60 | 50 NOS | 50 NOS | 50 NOS | REORDER NOW |
| 8 | ALLEN BOLT 10 X 90 | 50 NOS | 50 NOS | 10 NOS | REORDER NOW |
| 9 | ALLEN BOLT 12 X 120 LPS | 50 NOS | 50 NOS | 0 NOS | REORDER NOW |
| 10 | ALLEN BOLT 12 X 80 | 50 NOS | 50 NOS | 0 NOS | REORDER NOW |

Note: ALLEN BOLT 10 X 60 has closing_qty = reorder_trigger (50 = 50).
It is flagged because the condition is <= (less than OR equal to).

### Sample OK Items

| Item | Total Qty (3m) | Closing Stock | Why OK |
|------|---------------|---------------|--------|
| 112BH093 IST SET WITH HOLDER 3/32" | 0 | 0 | No purchases in window, guard prevents flag |
| 2NC CCGW 09T308 WH BNC2020 | 0 | 5 | No purchases in window, not flagged |

---

## 7. How the Window Works

The as_of_date is the anchor point. The window always stretches 89 days back from it,
giving an inclusive 90-day range.

```
as_of_date = 2019-03-31
  window_from = 2019-01-01   (2019-03-31 - 89 days)
  window_to   = 2019-03-31
  Covers: Q1 of FY 2018-19 (Jan, Feb, Mar)
  needs_reorder_count: 1,320

as_of_date = 2019-01-31
  window_from = 2018-11-03   (2019-01-31 - 89 days)
  window_to   = 2019-01-31
  Covers: Nov 2018 to Jan 2019
  needs_reorder_count: 1,384

as_of_date = 2018-12-31
  window_from = 2018-10-02   (2018-12-31 - 89 days)
  window_to   = 2018-12-31
  Covers: Oct to Dec 2018
```

Different windows give different reorder counts because purchase patterns vary by month.

---

## 8. What Gets Flagged and What Doesnt

### Flagged (needs_reorder = true)

- Had purchases in the 90-day window
- AND closing stock is at or below the purchase total

```
Example: ADJUSTABLE WRENCH 10" (1172) TAPARIA
  total_qty_purchased = 10
  reorder_trigger     = 10
  closing_qty         = 9
  9 <= 10 -> true -> REORDER NOW
```

```
Example: AEROSOL-OS
  total_qty_purchased = 100
  reorder_trigger     = 100
  closing_qty         = 0
  0 <= 100 -> true -> REORDER NOW
```

```
Example: ALLEN BOLT 10 X 60 (edge case: exactly equal)
  total_qty_purchased = 50
  reorder_trigger     = 50
  closing_qty         = 50
  50 <= 50 -> true -> REORDER NOW
```

### Not Flagged (needs_reorder = false)

- Had zero purchases in the window (reorder_trigger = 0, guard kicks in)
- OR closing stock is above the reorder trigger

```
Example: 112BH093 IST SET WITH HOLDER 3/32"
  total_qty_purchased = 0
  reorder_trigger     = 0
  closing_qty         = 0
  Guard: reorder_trigger > 0 is false -> NOT flagged
  (Without the guard: 0 <= 0 would be true — this was the original bug)
```

```
Example: item with enough stock
  total_qty_purchased = 200
  reorder_trigger     = 200
  closing_qty         = 500
  500 <= 200 -> false -> OK
```

---

## 9. Telegram Integration

The endpoint is called by an n8n workflow triggered by the Telegram command /reorder.

### Flow

```
User sends /reorder in Telegram
  |
  v
Telegram webhook -> ngrok -> n8n (localhost:5678)
  |
  v
IF node: message starts with /reorder?
  |-- no  -> stop
  |-- yes ->
        |
        v
  HTTP GET http://localhost:3001/api/sync/reorder-levels
  Header: x-api-key: ...
        |
        v
  Code node: format JSON into text (top 20 reorder items)
        |
        v
  Telegram Send Message -> reply to user
```

### What the User Sees

```
Reorder Report
As of: 2019-03-31
Window: 2019-01-01 to 2019-03-31
1320 of 6891 items flagged
──────────────────────

1. 2NU VCMW 160404 BN250
   Total Qty (3m) : 100 NOS
   Reorder Level  : 100 NOS
   Closing Stock  : 20 NOS
   Needs Reorder? : 20 <= 100 -> true
   Status         : REORDER NOW

2. ADJUSTABLE WRENCH 10" (1172) TAPARIA
   Total Qty (3m) : 10 NOS
   Reorder Level  : 10 NOS
   Closing Stock  : 9 NOS
   Needs Reorder? : 9 <= 10 -> true
   Status         : REORDER NOW

... (up to 20 items)
```

### n8n Workflow File

challan-to-invoice.json — 5 nodes total:
  Node 1: Telegram Trigger (listens for messages)
  Node 2: IF — checks if text starts with /reorder
  Node 3: HTTP Request — calls the backend endpoint
  Node 4: Code — formats JSON into readable text
  Node 5: Telegram Send Message — sends reply

---

## 10. Known Limitations

### closing_qty is a snapshot, not live

closing_qty in stock_items is the FY-end closing balance synced from Tally.
For K.V. ENTERPRISES 18-19 this aligns with 2019-03-31.
If you use a different as_of_date like 2018-12-31, the closing_qty still reflects
the March 31 snapshot — not what stock actually looked like on December 31.
This means the comparison is not fully accurate for mid-year dates.

### 90-day window is rolling, not calendar months

The window is always exactly 90 days backwards from as_of_date.
It does not snap to calendar month boundaries.
If you need Jan-Feb-Mar specifically, use as_of_date=2019-03-31.

### No lead time or safety stock

The formula does not account for:
- How long a supplier takes to deliver (lead time)
- Buffer stock to handle demand spikes (safety stock)
- Seasonal variation in purchase patterns

It only answers: do I have enough stock to cover the last 90 days of purchases?

### Equal-to threshold counts as reorder

If closing_qty exactly equals reorder_trigger (e.g. both are 50),
the item is flagged as needing reorder. This is intentional (the condition is <=)
but may be surprising for items sitting exactly at the threshold.

### Items with zero purchases are excluded

Items never purchased in the 90-day window are not evaluated for reorder,
even if they have zero closing stock. This is by design — no purchase history
means no basis for a reorder calculation.
