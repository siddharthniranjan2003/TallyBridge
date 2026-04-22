# Reorder Level Code Changes Summary

## Overview
The reorder-level implementation was moved from the older quantity-based purchase summary into a value-based inventory intelligence flow inside the backend.

## Code File Updated
- `backend/src/routes/sync.ts`

## What Changed
- Added a new inventory intelligence report builder that reads from:
  - `companies`
  - `vouchers`
  - `voucher_items`
  - `stock_items`
- Switched the calculation basis to:
  - `Avg Sale (6m)` from `GST SALE` voucher item amounts over the last 180 days, divided by 6
  - `Last Month Purchase` from purchase voucher item amounts over the last 30 days
  - `Closing Stock` from `stock_items.closing_value`
  - `Current Quantity` from `stock_items.closing_qty`
- Added scenario classification for:
  - `INERT`
  - `ONSET`
  - `GHOST`
  - `BLOAT`
  - `BLAZE`
  - `TAPER`
  - `SURGE`
  - `DRAIN`
  - `STARVE`
- Added paise-normalized equality checks so value comparisons are exact in business terms.

## New Backend Outputs
Each item now returns:
- `stock_item_name`
- `unit`
- `scenario`
- `trigger_word`
- `color`
- `priority`
- `avg_sale_6m`
- `last_month_purchase`
- `closing_stock_value`
- `reorder_level`
- `reorder_at`
- `current_quantity`
- `reorder_quantity`
- `reorder_quantity_status`
- `needs_reorder`

## Reorder Logic Added
- `reorder_level = avg_sale_6m * 2`
- `reorder_at = reorder_level`
- `reorder_value_gap = max(reorder_level - closing_stock_value, 0)`
- `reorder_quantity` is derived from the value gap and item `rate`
- if `rate` is missing, fallback uses `closing_value / closing_qty`
- if neither rate path is usable, the item is marked `manual_rate_required`

## New/Updated Routes
- `GET /api/sync/reorder-levels`
  - returns the full classified report
- `GET /api/sync/reorder-levels/:triggerWord`
  - returns only one trigger-word category such as `SURGE` or `DRAIN`

## Query Support
The new route supports:
- `company_id`
- `company_guid`
- `company_name`
- `as_of_date`
- `threshold`
- `limit`

## Important Notes
- No Supabase schema change was required for this implementation.
- The report is value-based, not quantity-based.
- `closing_qty` is used for display and reorder quantity conversion, not for scenario classification.
- Trigger-word filtering is handled in the backend, so Telegram/n8n can call one scenario directly.

## Validation Done
- Backend build passed with `npm run build`
- Live route testing confirmed:
  - full report output
  - trigger-word output such as `SURGE`
  - reorder quantity calculation for positive-gap items
