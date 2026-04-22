# Plan: Inventory Intelligence System

## Context

K.V. Enterprises is a hardware trader. The business need is a per-SKU health dashboard that surfaces the right action for each stock item — reorder, exit, investigate, or watch. The spec defines three columns of data per item and 9 named scenarios (A–I) derived from comparing those columns. Items are filtered to those with any column value > ₹5 lakhs, and each scenario gets a color (green/orange/red) and priority rank (1 = most urgent).

This plan covers both the backend API endpoint and the frontend page.

---

## Data Sources

```
Column I  = avg monthly sale value (last 6 months)
          = SUM(voucher_items.amount) WHERE voucher.type=Sales, date ∈ [asOf-180d, asOf]  ÷ 6

Column II = last 1-month purchase value
          = SUM(voucher_items.amount) WHERE voucher.type=Purchase, date ∈ [asOf-30d, asOf]

Column III = closing stock value today
           = stock_items.closing_value
```

Join path (same as existing `/reorder-levels`):
```
purchases/vouchers (date, type, company_id) → voucher_id
  └── voucher_items (voucher_id, stock_item_name, amount)
stock_items (company_id, name, closing_value)
```

---

## Scenario Classification Logic

All values in ₹. NIL threshold: value < 1.

| col_i | col_ii | col_iii | Scenario | Label |
|-------|--------|---------|----------|-------|
| 0 | 0 | >0 | **A** | Non-moving / Dead stock |
| 0 | >0 | <1 | **C** | Stock went negative (purchase filled gap) |
| 0 | >0 | ≤ II×0.75 | **C** | Stock mostly absorbed by negative position |
| 0 | >0 | ≤ II×1.5 | **B** | Sales about to begin |
| 0 | >0 | > II×1.5 | **D** | Dead stock still being purchased |
| >0 | 0 | <1 | **F** | Reducing — end state |
| >0 | 0 | ≥ I×2 | **H** | Reducing — in progress (wind-down) |
| >0 | 0 | 0<x<I×2 | **I** | ⚠️ CRITICAL: proven demand, about to stock out |
| >0 | >0 | <1 and II≥I×1.5 | **G** | High demand, can't keep stocked |
| >0 | >0 | <1 | **E** | Fastest moving |
| >0 | >0 | >0 | **–** | Healthy / Normal (no alert) |

Priority & color:
| Scenario | Color | Priority |
|----------|-------|----------|
| I | green | 1 |
| C | red | 2 |
| D | red | 3 |
| A | orange | 4 |
| G | green | 4 |
| B | green | 5 |
| H | green | 5 |
| F | orange | 6 |
| E | green | 7 |
| – | – | 8 |

Threshold filter: include item if `max(col_i, col_ii, col_iii) > 500_000`

---

## Backend: New Endpoint

**File:** `backend/src/routes/sync.ts`  
**Route:** `GET /api/sync/inventory-intelligence`

Add after the existing `/reorder-levels` route (~120 lines).

**Implementation steps:**

1. Resolve company via `resolveCompanyLookup()` (existing helper, line ~1419)
2. Parse `as_of_date` query param (default: auto-detect as `MAX(vouchers.date)` for the company via a single Supabase query)
3. Compute windows: `sixMonthsAgo = asOf - 180d`, `oneMonthAgo = asOf - 30d`
4. **Three fetches (can run in parallel via `Promise.all`):**
   - Sales voucher IDs in 6M window → `voucher_items` → aggregate `amount` per `stock_item_name` → divide by 6 = col_i map
   - Purchase voucher IDs in 1M window → `voucher_items` → aggregate `amount` per `stock_item_name` = col_ii map
   - All `stock_items` for company → col_iii map (keyed by name)
5. Build union of all item names across all three maps
6. For each item: apply scenario classification logic above
7. Apply ₹5L filter: `Math.max(col_i, col_ii, col_iii) > 500_000`
8. Sort: by priority ascending, then alphabetical by name
9. Return response

**Response shape:**
```json
{
  "as_of_date": "2024-03-31",
  "six_month_window_from": "2023-10-01",
  "one_month_window_from": "2024-03-01",
  "total_skus": 312,
  "filtered_count": 18,
  "items": [
    {
      "name": "GI Pipe 1 inch",
      "scenario": "I",
      "label": "Very high demand, under-purchased",
      "color": "green",
      "priority": 1,
      "col_i": 85000,
      "col_ii": 0,
      "col_iii": 40000
    }
  ]
}
```

**Reuse from existing code:**
- `resolveCompanyLookup()` — already in sync.ts (line ~1419)
- `fetchAllPages()` — already in sync.ts (used by `/reorder-levels`)
- `requireApiKey` middleware

---

## Frontend: New Page

**New file:** `frontend/src/pages/InventoryIntelligence.tsx`

Layout (inline styles, matching existing page patterns):
- Header: "Inventory Intelligence" + subtitle with item count and as_of_date
- Summary chips: one per scenario color (red N, orange N, green N)
- Table columns: **Item Name** | **Avg Monthly Sale (I)** | **Last 1M Purchase (II)** | **Closing Stock (III)** | **Scenario**
- Scenario column: colored badge showing scenario letter + label (e.g., "I — Very high demand, under-purchased")
- Row left-border color: red / orange / green per `item.color`
- Sort is done server-side; no client-side sort needed
- Loading state same pattern as `Inventory.tsx`

Color badge styles:
- red: `background: #fee2e2, color: #dc2626`
- orange: `background: #fef3c7, color: #92400e`
- green: `background: #dcfce7, color: #166534`

---

## Routing & Navigation

**`frontend/src/main.tsx`** — Add:
```tsx
import InventoryIntelligence from "./pages/InventoryIntelligence";
// inside <Route path="/" element={<Layout />}>:
<Route path="inventory-intelligence" element={<InventoryIntelligence />} />
```

**`frontend/src/components/Layout.tsx`** — Add to `links` array:
```tsx
{ to: "/inventory-intelligence", label: "Inv Intelligence", icon: "🧠" }
```

---

## Files to Modify / Create

| File | Change |
|------|--------|
| `backend/src/routes/sync.ts` | Add `GET /api/sync/inventory-intelligence` (~120 lines) |
| `frontend/src/pages/InventoryIntelligence.tsx` | New file (~120 lines) |
| `frontend/src/main.tsx` | Add import + route (2 lines) |
| `frontend/src/components/Layout.tsx` | Add nav link to `links` array (1 line) |

---

## Verification

1. Call `GET /api/sync/inventory-intelligence?company_name=...` — verify response shape, scenario labels, priority sort order
2. Test with `as_of_date=YYYY-MM-DD` param — verify windows shift correctly
3. Confirm ₹5L filter: items where all three columns < ₹5L should be absent
4. Verify scenario I items appear first, then C, D, A/G, etc.
5. Frontend `/inventory-intelligence` renders table with colored badges
6. Check nav link highlights correctly when on the page