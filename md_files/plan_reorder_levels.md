# Plan: Reorder Level Feature

## Context
Calculate reorder levels for stock items based on the last 3 months of purchase data. Reorder level is the inventory quantity at which a new purchase order should be placed to avoid stockouts.

**Formula:**
```
avg_daily_qty   = total_qty_purchased_last_90_days / 90
reorder_level   = avg_daily_qty × lead_time_days (default 30, user-configurable)
needs_reorder   = closing_qty <= reorder_level
```

Lead time is configurable per API call via `?lead_time_days=N` (defaults to 30).

---

## Schema Understanding

**Data join path for purchase quantities:**
```
purchases (date, is_cancelled, company_id, voucher_id)
    └── voucher_items (voucher_id, stock_item_name, quantity)

stock_items (company_id, name, closing_qty, unit)
    └── joined by stock_item_name to get current stock
```

---

## Implementation Plan

### Step 1 — Backend API endpoint (`backend/src/routes/sync.ts`)

Add `GET /api/sync/reorder-levels` after the existing `/purchases` route.

**Logic:**
1. Resolve company via `resolveCompanyLookup()` (existing helper in sync.ts)
2. Parse `lead_time_days` from query param (default 30, clamp 1–365)
3. Compute window: `from = today - 90 days`
4. Two-fetch + merge approach (consistent with existing patterns):
   - Fetch purchase `voucher_id`s from `purchases` for company + last 90 days + not cancelled
   - Fetch `voucher_items` for those voucher IDs → aggregate qty per `stock_item_name`
   - Fetch `stock_items` for company → map by name for `closing_qty` + `unit`
5. Merge: compute `avg_daily_qty`, `reorder_level`, `needs_reorder` per item
6. Include stock items with zero purchases (avg_daily_qty = 0, reorder_level = 0)

**Response shape:**
```json
[
  {
    "stock_item_name": "Cement OPC 53 Grade",
    "unit": "Bag",
    "total_qty_3m": 450,
    "avg_daily_qty": 5.0,
    "reorder_level": 150,
    "closing_qty": 80,
    "needs_reorder": true
  }
]
```

### Step 2 — Frontend page (`frontend/src/pages/ReorderLevel.tsx`)

New page following existing patterns (inline styles, useState, useEffect, axios via `get()`).

**Layout:**
- Lead time input at top: label + number input (default 30) + Apply button → refetches
- Summary count of items needing reorder
- Table: Item Name | Unit | Avg Daily Qty | Current Stock | Reorder Level | Status
- Status badge: "Reorder Now" (red) / "OK" (green)
- Sort: needs_reorder items first, then alphabetical

### Step 3 — Wire routing + sidebar

- `frontend/src/main.tsx` — add `<Route path="/reorder" element={<ReorderLevel />} />`
- Sidebar layout file — add "Reorder Levels" nav link to `/reorder`

---

## Critical Files to Modify

| File | Change |
|------|--------|
| `backend/src/routes/sync.ts` | Add `GET /api/sync/reorder-levels` (~60 lines) |
| `frontend/src/pages/ReorderLevel.tsx` | New file |
| `frontend/src/main.tsx` | Add `/reorder` route |
| Sidebar layout component | Add nav link (confirm exact file during implementation) |

---

## Verification

1. Call `GET /api/sync/reorder-levels?company_name=...&lead_time_days=30` — check response shape and values
2. Change `lead_time_days=15` — reorder_level values should halve
3. Frontend `/reorder` renders table with correct data
4. Lead time Apply button refetches and updates table
5. Items with `closing_qty <= reorder_level` show "Reorder Now" badge
