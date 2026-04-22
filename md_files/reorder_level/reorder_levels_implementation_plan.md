# Plan: Rewrite `/api/sync/reorder-levels` — Inventory Intelligence

## Context

The current `/api/sync/reorder-levels` endpoint (`sync.ts:1576–1690`) uses a purchase-quantity-based heuristic: it sums purchase **quantities** from the `purchases` table over a 90-day window and compares against `closing_qty`. The new endpoint must compute reorder levels from **sale and purchase values (INR)**, classify each stock item into one of 9 scenarios (A–I), apply a 5-lakh threshold filter, and return a full inventory intelligence report. The scenario trigger words (STARVE, INERT, etc.) will become Telegram menu buttons via n8n.

## Data Flow

```
┌──────────────────────────────────────────────────────┐
│                    vouchers table                     │
│  voucher_type = 'GST Sale'  │  purchase types (3)    │
│  is_cancelled = false       │  is_cancelled = false   │
│  date in 6M window          │  date in 1M window      │
└───────────┬─────────────────┴──────────┬──────────────┘
            │ voucher IDs                │ voucher IDs
            ▼                            ▼
┌──────────────────────────────────────────────────────┐
│               voucher_items table                     │
│  SUM(abs(amount)) per stock_item_name                │
│  → saleByItem map          → purchaseByItem map      │
└───────────┬─────────────────┴──────────┬──────────────┘
            │ col_i = sum/6              │ col_ii
            ▼                            ▼
┌──────────────────────────────────────────────────────┐
│               stock_items table                       │
│  name, unit, closing_value (= col_iii)               │
└──────────────────────────┬───────────────────────────┘
                           ▼
┌──────────────────────────────────────────────────────┐
│  For each item:                                       │
│  1. Threshold gate: skip if all 3 cols < 500,000     │
│  2. Classify scenario A–I                            │
│  3. Compute reorder_level, needs_reorder, order_needed│
│  4. Sort by priority, then alphabetical              │
└──────────────────────────────────────────────────────┘
```

## File to Modify

`backend/src/routes/sync.ts` — replace the handler at lines 1576–1690 (everything from the `// ── GET /api/sync/reorder-levels` comment block through the closing `});` before `export default router`).

## Utilities to Reuse

| Utility | Location | Purpose |
|---------|----------|---------|
| `fetchAllPages(label, buildQuery)` | `sync.ts:1243` | Paginated Supabase reads (1000/page) |
| `selectRowsByIn(table, column, values, label)` | `sync.ts:277` | Chunked IN-clause lookups (250/chunk), selects `*` |
| `isPurchaseVoucherType(value)` | `sync.ts:556` | Case-insensitive purchase type matching (includes "purchase", excludes "order") |
| `supabase` client | imported from `../db/supabase.js` | |
| `requireApiKey` middleware | imported from `../middleware/auth.js` | |

## Implementation

### 1. Add scenario metadata constant (before the handler)

```ts
const SCENARIO_META: Record<string, { trigger_word: string; color: string; priority: number }> = {
  A: { trigger_word: "INERT",  color: "orange", priority: 4 },
  B: { trigger_word: "ONSET",  color: "green",  priority: 5 },
  C: { trigger_word: "GHOST",  color: "red",    priority: 2 },
  D: { trigger_word: "BLOAT",  color: "red",    priority: 3 },
  E: { trigger_word: "BLAZE",  color: "green",  priority: 7 },
  F: { trigger_word: "TAPER",  color: "orange", priority: 6 },
  G: { trigger_word: "SURGE",  color: "green",  priority: 4 },
  H: { trigger_word: "DRAIN",  color: "orange", priority: 8 },
  I: { trigger_word: "STARVE", color: "green",  priority: 1 },
};
```

### 2. Replace the handler

Delete lines 1576–1690 and write the new handler. Pseudocode structure:

**a) Company + date setup** (same auto-detect pattern as current):
- `as_of_date` from query param, default `2019-03-31`
- 6M window: `as_of_date - 179 days` to `as_of_date` (180 days inclusive)
- 1M window: `as_of_date - 29 days` to `as_of_date` (30 days inclusive)

**b) Fetch GST Sale voucher IDs (6M window)**:
```ts
const saleVoucherRows = await fetchAllPages("Reorder GST Sales", (from, to) =>
  supabase.from("vouchers").select("id")
    .eq("company_id", companyId)
    .eq("voucher_type", "GST Sale")
    .eq("is_cancelled", false)
    .gte("date", from6mIso).lte("date", toIso)
    .order("id", { ascending: true }).range(from, to)
);
```

**c) Fetch purchase voucher IDs (1M window)** — query `vouchers` table (not `purchases`), fetch all in 1M window, filter in JS via `isPurchaseVoucherType()` for case-insensitive matching:
```ts
const purchaseVoucherRows = await fetchAllPages("Reorder Purchases", (from, to) =>
  supabase.from("vouchers").select("id, voucher_type")
    .eq("company_id", companyId)
    .eq("is_cancelled", false)
    .gte("date", from1mIso).lte("date", toIso)
    .order("id", { ascending: true }).range(from, to)
);
const purchaseVoucherIds = purchaseVoucherRows
  .filter((v: any) => isPurchaseVoucherType(v.voucher_type))
  .map((v: any) => v.id);
```

**d) Aggregate voucher_items amounts** — for each set of voucher IDs, use `selectRowsByIn("voucher_items", "voucher_id", ids, label)`, then aggregate `Math.abs(amount)` per `stock_item_name` into `Map<string, number>`. Use `Math.abs()` because Tally can store negative amounts for sales.

**e) Fetch stock items**:
```ts
const stockItems = await fetchAllPages("Reorder stock items", (from, to) =>
  supabase.from("stock_items").select("name, unit, closing_value, closing_qty")
    .eq("company_id", companyId)
    .order("name", { ascending: true }).range(from, to)
);
```

**f) Merge, threshold, classify** — for each stock item:
- `total_6m_sale = saleByItem.get(name) ?? 0`
- `col_i = total_6m_sale / 6` (avg monthly sale)
- `col_ii = purchaseByItem.get(name) ?? 0` (last 1M purchase)
- `col_iii = Math.abs(closing_value)` (closing value, ensure positive)
- **Threshold gate**: skip if `col_i < 500_000 && col_ii < 500_000 && col_iii < 500_000` (strict less-than; items AT 5L pass)
- **Classify** using the decision tree (see below)
- **Compute reorder_level** (see below)

### 3. Classification Decision Tree

```
if col_i === 0:
  col_ii === 0                          → A (INERT)
  abs(col_ii - col_iii) < 0.01         → B (ONSET)
  col_ii > col_iii                      → C (GHOST)
  else (col_ii < col_iii)              → D (BLOAT)
else if col_iii === 0:
  abs(col_i - col_ii) < 0.01          → E (BLAZE)
  col_i > col_ii                        → F (TAPER)
  else (col_i < col_ii)               → G (SURGE)
else:  // col_i > 0 AND col_iii > 0
  col_iii <= col_i                      → I (STARVE)
  else                                  → H (DRAIN)
```

Note: Floating-point equality uses `Math.abs(a - b) < 0.01` tolerance for scenarios B and E.

### 4. Reorder Level Computation

```
if col_i === 0:
  reorder_level = 0, needs_reorder = false, order_needed = 0

else if scenario === 'F' || scenario === 'H':
  reorder_level = total_6m_sale / 3
  needs_reorder = false, order_needed = 0

else:
  reorder_level = total_6m_sale / 3
  needs_reorder = col_iii <= reorder_level
  order_needed = Math.max(0, reorder_level - col_iii)
```

### 5. Response Shape

Per item:
```json
{
  "stock_item_name": "GI Pipe 1 inch",
  "unit": "Nos",
  "scenario": "I",
  "trigger_word": "STARVE",
  "color": "green",
  "priority": 1,
  "avg_monthly_sale": 10000,
  "last_1m_purchase": 0,
  "closing_value": 5000,
  "reorder_level": 20000,
  "needs_reorder": true,
  "order_needed": 15000
}
```

Envelope:
```json
{
  "as_of_date": "2019-03-31",
  "sale_window_from": "2018-10-03",
  "sale_window_to": "2019-03-31",
  "purchase_window_from": "2019-03-02",
  "purchase_window_to": "2019-03-31",
  "threshold": 500000,
  "total_items_scanned": 6891,
  "items_above_threshold": 42,
  "needs_reorder_count": 15,
  "items": [...]
}
```

Sort: by `priority` ascending (1 = STARVE first), then alphabetical by `stock_item_name` within same priority.

## Edge Cases

1. **Amount sign** — use `Math.abs(item.amount)` when aggregating voucher_items, since Tally can store sale amounts as negative.
2. **Floating-point equality** — scenarios B and E use `Math.abs(a - b) < 0.01` tolerance.
3. **Stock items with no vouchers** — appear from stock_items with col_i = 0, col_ii = 0. Only show if col_iii >= 500,000 (Scenario A).
4. **Items in voucher_items but not in stock_items** — skipped, since we iterate stock_items.
5. **Empty stock_item_name** — skip blank names when aggregating voucher_items (same guard as current code).

## Verification

1. **Build**: `cd backend && npx tsc --noEmit`
2. **Start server**: `npm run dev`
3. **Hit endpoint**: `curl -H "x-api-key: <key>" "http://localhost:3001/api/sync/reorder-levels?as_of_date=2019-03-31"`
4. **Check**: response has scenario classification, threshold filter, priority sort, reorder_level
5. **Verify**: items with all 3 columns below 5L are filtered out
6. **Verify**: STARVE items (priority 1) appear first
