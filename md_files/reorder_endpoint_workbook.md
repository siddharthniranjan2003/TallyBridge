# Reorder Levels Endpoint Workbook

> Endpoint: GET /api/sync/reorder-levels
> File: backend/src/routes/sync.ts
> Base URL: http://localhost:3001

---

## What It Does

Looks at all purchase vouchers in a 90-day window, sums the quantity bought per item,
and compares that against the current closing stock to determine if a reorder is needed.

---

## Formula

```
window_from     = as_of_date - 89 days   (inclusive, so total = 90 days)
window_to       = as_of_date

reorder_trigger = SUM(quantity) from purchase line items in [window_from, window_to]
                  -- raw total only, no division, no averaging, no lead time

needs_reorder   = reorder_trigger > 0
                  AND closing_qty <= reorder_trigger
```

The `reorder_trigger > 0` guard ensures items with zero purchases in the window
are never flagged, even if closing stock is also zero.

---

## Authentication

Every request must include:
```
x-api-key: sb_publishable_oFyc0kQxjkga2QZw0kq3ZQ_ovmR0P9h
```

Without it: 401 Unauthorized

---

## Company

No company param needed. The endpoint auto-detects the single company in Supabase.

---

## Query Params

| Param | Required | Default | Format | Description |
|-------|----------|---------|--------|-------------|
| `as_of_date` | No | `2019-03-31` | YYYY-MM-DD | The end date of the 90-day purchase window |

If `as_of_date` is missing or not in YYYY-MM-DD format, defaults to `2019-03-31`.

---

## How the Window Works

```
as_of_date = 2019-03-31
  window_from = 2019-03-31 - 89 days = 2019-01-01
  window_to   = 2019-03-31
  purchases counted: Jan 1 to Mar 31 2019 (90 days inclusive)

as_of_date = 2019-01-31
  window_from = 2019-01-31 - 89 days = 2018-11-02
  window_to   = 2019-01-31
  purchases counted: Nov 2 2018 to Jan 31 2019

as_of_date = 2018-12-31
  window_from = 2018-12-31 - 89 days = 2018-10-02
  window_to   = 2018-12-31
  purchases counted: Oct 2 to Dec 31 2018
```

---

## All Valid Requests

### 1. Default call (no params)
```bash
curl "http://localhost:3001/api/sync/reorder-levels" \
  -H "x-api-key: sb_publishable_oFyc0kQxjkga2QZw0kq3ZQ_ovmR0P9h"
```
Window: 2019-01-01 to 2019-03-31

---

### 2. Custom end date
```bash
curl "http://localhost:3001/api/sync/reorder-levels?as_of_date=2019-01-31" \
  -H "x-api-key: sb_publishable_oFyc0kQxjkga2QZw0kq3ZQ_ovmR0P9h"
```
Window: 2018-11-02 to 2019-01-31

---

### 3. Earlier in the FY
```bash
curl "http://localhost:3001/api/sync/reorder-levels?as_of_date=2018-12-31" \
  -H "x-api-key: sb_publishable_oFyc0kQxjkga2QZw0kq3ZQ_ovmR0P9h"
```
Window: 2018-10-02 to 2018-12-31

---

## Response Shape

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
    },
    {
      "stock_item_name": "WHITE GLASS",
      "unit": "NOS",
      "total_qty_purchased": 5000,
      "reorder_trigger": 5000,
      "closing_qty": 0,
      "needs_reorder": true
    },
    {
      "stock_item_name": "SOME ITEM WITH ENOUGH STOCK",
      "unit": "NOS",
      "total_qty_purchased": 200,
      "reorder_trigger": 200,
      "closing_qty": 500,
      "needs_reorder": false
    }
  ]
}
```

---

## Response Fields Explained

| Field | Type | Description |
|-------|------|-------------|
| `as_of_date` | string | The date used as the window end point |
| `window_from` | string | Start of the 90-day purchase window |
| `window_to` | string | End of the 90-day purchase window |
| `total_items` | number | Total stock items in the company |
| `needs_reorder_count` | number | How many items are flagged for reorder |
| `items[].stock_item_name` | string | Name of the stock item |
| `items[].unit` | string | Unit of measurement (NOS, KG, MTR, etc.) |
| `items[].total_qty_purchased` | number | Raw sum of qty bought in the 90-day window |
| `items[].reorder_trigger` | number | Same as total_qty_purchased (the threshold) |
| `items[].closing_qty` | number | Current stock on hand (FY-end snapshot) |
| `items[].needs_reorder` | boolean | true if reorder_trigger > 0 AND closing_qty <= reorder_trigger |

---

## Sorting

Items are always returned in this order:
1. `needs_reorder = true` first (sorted alphabetically within this group)
2. `needs_reorder = false` after (sorted alphabetically within this group)

---

## What Counts as a Purchase

Only vouchers that meet ALL of these conditions are included in the sum:

| Condition | Reason |
|-----------|--------|
| From `purchases` table | Only purchase-type vouchers |
| `is_cancelled = false` | Cancelled vouchers don't count |
| `date >= window_from` | Inside the 90-day window |
| `date <= window_to` | Inside the 90-day window |
| `stock_item_name` not blank | Skips malformed line items |
| `quantity > 0` | Skips returns and credit notes |

---

## What Does Not Trigger a Reorder

| Scenario | needs_reorder | Why |
|----------|---------------|-----|
| No purchases in window, zero stock | false | reorder_trigger = 0, guard prevents flag |
| No purchases in window, positive stock | false | reorder_trigger = 0, guard prevents flag |
| Had purchases, closing stock > reorder_trigger | false | Stock is sufficient |
| Had purchases, closing stock = reorder_trigger | true | Equal to threshold counts as needing reorder |
| Had purchases, closing stock < reorder_trigger | true | Stock is insufficient |

---

## Internal Query Steps

```
Step 1: SELECT id FROM companies LIMIT 1
        --> get company_id (auto-detect)

Step 2: SELECT voucher_id FROM purchases
        WHERE company_id = ?
        AND is_cancelled = false
        AND date >= window_from
        AND date <= window_to
        --> ~869 voucher IDs (for default window)

Step 3: SELECT stock_item_name, quantity FROM voucher_items
        WHERE voucher_id IN (batches of 250)
        --> ~2,852 rows fetched in 4 batches
        --> aggregated in Node.js to Map<name, sum(qty)>

Step 4: SELECT name, unit, closing_qty FROM stock_items
        WHERE company_id = ?
        --> 6,891 rows fetched

Step 5: Merge in Node.js
        For each stock item:
          reorder_trigger = Map.get(name) ?? 0
          needs_reorder   = reorder_trigger > 0 AND closing_qty <= reorder_trigger

Step 6: Sort + return JSON
```

Total Supabase round trips: ~7

---

## Telegram Formatter (n8n Code Node)

```js
const data = $input.first().json;
const items = data.items || [];

const reorderItems = items.filter(i => i.needs_reorder).slice(0, 20);

const lines = reorderItems
  .map((i, idx) => {
    const comparison = `${i.closing_qty.toLocaleString()} <= ${i.reorder_trigger.toLocaleString()} -> ${i.needs_reorder}`;
    return (
      `${idx + 1}. ${i.stock_item_name}\n` +
      `   Total Qty (3m) : ${i.total_qty_purchased.toLocaleString()} ${i.unit || ''}\n` +
      `   Reorder Level  : ${i.reorder_trigger.toLocaleString()} ${i.unit || ''}\n` +
      `   Closing Stock  : ${i.closing_qty.toLocaleString()} ${i.unit || ''}\n` +
      `   Needs Reorder? : ${comparison}\n` +
      `   Status         : REORDER NOW`
    );
  })
  .join('\n\n');

const header =
  `Reorder Report\n` +
  `As of: ${data.as_of_date}\n` +
  `Window: ${data.window_from} to ${data.window_to}\n` +
  `${data.needs_reorder_count} of ${data.total_items} items flagged\n` +
  `──────────────────────\n`;

return [{ json: { text: header + (lines || 'All items are sufficiently stocked.') } }];
```

---

## Sample Telegram Output

```
Reorder Report
As of: 2019-03-31
Window: 2019-01-01 to 2019-03-31
1320 of 6891 items flagged
──────────────────────

1. C-10 DEBURING BLADE
   Total Qty (3m) : 13,000 NOS
   Reorder Level  : 13,000 NOS
   Closing Stock  : 5,650 NOS
   Needs Reorder? : 5,650 <= 13,000 -> true
   Status         : REORDER NOW

2. WHITE GLASS
   Total Qty (3m) : 5,000 NOS
   Reorder Level  : 5,000 NOS
   Closing Stock  : 0 NOS
   Needs Reorder? : 0 <= 5,000 -> true
   Status         : REORDER NOW

3. WATER PROOF PAPER NO- 320 JOHNOKEY
   Total Qty (3m) : 4,240 NOS
   Reorder Level  : 4,240 NOS
   Closing Stock  : 120 NOS
   Needs Reorder? : 120 <= 4,240 -> true
   Status         : REORDER NOW
```

---

## Known Limitations

- `closing_qty` is the FY-end snapshot from Tally, not a live running balance.
  For this company it aligns with 2019-03-31. For mid-year dates it may not reflect true stock.
- The 90-day window is fixed. It does not adjust for calendar months.
- No lead time, safety stock, or seasonality factored in.
- Items never purchased in the window are excluded from reorder consideration entirely.
