# Final Plan: Replace Quantity-Based Reorder Route with Value-Based Inventory Intelligence

## Summary
Replace the current `/api/sync/reorder-levels` logic in `backend/src/routes/sync.ts` with the value-based scenario engine we have now locked.

This implementation will:
- use **GST SALE** item amounts for 6-month average sales
- use **Purchase** vouchers from `vouchers` for last-1-month purchase amounts
- use `stock_items.closing_value` as Closing Stock and `stock_items.closing_qty` as Current Quantity
- compute `Reorder Level = Avg Sale (6m) * 2`
- expose **trigger-word endpoints** for `INERT`, `ONSET`, `GHOST`, `BLOAT`, `BLAZE`, `TAPER`, `SURGE`, `DRAIN`, `STARVE`
- keep exact equality by **normalizing to paise first**, then comparing exactly
- exclude the non-spec gap case (`col1 > 0 && col2 > 0 && col3 > 0`) from scenario output

## Key Changes
### Backend behavior
- Replace the current 90-day purchase-quantity route with a new value-based engine.
- Keep `GET /api/sync/reorder-levels` as the full classified report.
- Add `GET /api/sync/reorder-levels/:triggerWord` as the trigger-word endpoint for Telegram/n8n.
- Use `resolveCompanyLookup()` instead of `.limit(1).single()`.
- Default `as_of_date` to the company’s latest synced voucher date (`MAX(vouchers.date)`); allow override via query param.
- Use rolling inclusive windows:
  - sales window: `as_of_date - 179 days` to `as_of_date`
  - purchase window: `as_of_date - 29 days` to `as_of_date`
- Add optional query params:
  - `company_id` / `company_guid` / `company_name`
  - `as_of_date`
  - `threshold` with default `500000`
  - `limit` for response trimming
- For the trigger-word route, validate the path segment against the 9 allowed trigger words and return `400` on invalid values.

### Data sourcing
- `col1`:
  - fetch `vouchers.id` where `voucher_type = 'GST SALE'`, `is_cancelled = false`, date in 180-day window
  - fetch matching `voucher_items`
  - aggregate `Math.abs(voucher_items.amount)` by `stock_item_name`
  - compute `avg_sale_6m = total_6m_sales / 6`
- `col2`:
  - fetch `vouchers.id, voucher_type` where `is_cancelled = false`, date in 30-day window
  - keep only vouchers where `isPurchaseVoucherType(voucher_type)` is true
  - fetch matching `voucher_items`
  - aggregate `Math.abs(voucher_items.amount)` by `stock_item_name`
- `col3`:
  - fetch from `stock_items.closing_value`
- `current_quantity`:
  - fetch from `stock_items.closing_qty`
- Use `stock_items.name` as the driving key; join sales/purchase aggregates by `voucher_items.stock_item_name`.

### Scenario classification
Normalize `col1`, `col2`, and `col3` to integer paise for comparisons only. Keep rupee values in the response.

Decision tree:
- `A / INERT`: `col1 = 0`, `col2 = 0`, `col3 > 0`
- `B / ONSET`: `col1 = 0`, `col2 > 0`, `col2 = col3`
- `C / GHOST`: `col1 = 0`, `col2 > 0`, `col3 < col2`
- `D / BLOAT`: `col1 = 0`, `col2 > 0`, `col3 > col2`
- `E / BLAZE`: `col1 > 0`, `col3 = 0`, `col2 = col1`
- `F / TAPER`: `col1 > 0`, `col3 = 0`, `col2 < col1`
- `G / SURGE`: `col1 > 0`, `col3 = 0`, `col2 > col1`
- `H / DRAIN`: `col1 > 0`, `col2 = 0`, `col3 > col1`
- `I / STARVE`: `col1 > 0`, `col2 = 0`, `col3 <= col1`

Gap case:
- if `col1 > 0 && col2 > 0 && col3 > 0`, do not force-fit into A-I
- exclude it from scenario output
- include `unclassified_count` in the full-report envelope

Threshold rule:
- include item if **any one** of `col1`, `col2`, `col3` is `>= threshold`
- default threshold is `500000`, but allow override for trial/debug runs

### Reorder metrics
For every classified item:
- `reorder_level = avg_sale_6m * 2`
- `reorder_at = reorder_level`
- `reorder_value_gap = max(reorder_level - closing_stock_value, 0)`

Quantity conversion:
- if `reorder_value_gap = 0`, `reorder_quantity = 0`
- else if `stock_items.rate > 0`, `reorder_quantity = ceil(reorder_value_gap / rate)`
- else if `closing_qty > 0 && closing_value > 0`, use fallback rate `closing_value / closing_qty`
- else return `reorder_quantity = null` and `reorder_quantity_status = 'manual_rate_required'`

This is required because the live Supabase snapshot has many zero-rate items.

### Response contract
Per item, return:
- `stock_item_name`
- `unit`
- `scenario`
- `trigger_word`
- `priority`
- `color`
- `avg_sale_6m`
- `last_month_purchase`
- `closing_stock_value`
- `reorder_level`
- `reorder_at`
- `current_quantity`
- `reorder_quantity`
- `reorder_quantity_status`
- `needs_reorder`

`needs_reorder`:
- `true` when `closing_stock_value <= reorder_at`
- `false` otherwise

Full-report envelope:
- `company_id`
- `company_guid`
- `company_name`
- `as_of_date`
- `sale_window_from`
- `sale_window_to`
- `purchase_window_from`
- `purchase_window_to`
- `threshold`
- `total_items_scanned`
- `items_above_threshold`
- `classified_count`
- `unclassified_count`
- `needs_reorder_count`
- `items`

Sorting:
- full report: `priority asc`, then `stock_item_name asc`
- trigger-word route: same ordering after filtering

## Test Plan
### Pure logic tests
Add table-driven tests for:
- all 9 scenario branches
- threshold boundary where value is exactly `500000`
- exact-equality behavior after paise normalization
- gap case exclusion
- reorder quantity with:
  - positive rate
  - fallback `closing_value / closing_qty`
  - no usable rate leading to `manual_rate_required`

### Route tests
Verify:
- `GET /api/sync/reorder-levels` returns the full classified envelope
- `GET /api/sync/reorder-levels/SURGE` returns only SURGE rows
- invalid trigger word returns `400`
- missing company selector returns the same company-lookup errors as other endpoints
- pagination queries remain deterministic by ordering on `id` where multi-page voucher reads occur

### Live acceptance checks
Use the current Supabase snapshot as a sanity check, not a strict fixture:
- `SURGE` should include rows like `BIMETAL BANDSAW 3500 X 27 X .9 4 TPI WIKUS` with positive reorder quantity
- `DRAIN` rows should show `reorder_quantity = 0`
- `INERT` rows should show `avg_sale_6m = 0`, `last_month_purchase = 0`, `closing_stock_value > 0`
- current quantity must come from `stock_items.closing_qty`

## Assumptions and Defaults
- Sales type is the exact stored value `GST SALE`.
- Purchase selection is still done through `isPurchaseVoucherType()` so custom purchase-like voucher types continue to work.
- Windows are rolling `180d/30d`, not calendar months.
- Exact equality means exact equality after paise normalization, not float tolerance and not rupee rounding.
- `closing_value` is the scenario/reorder stock column; `closing_qty` is only for current-quantity display and rate fallback.
- Items present in vouchers but absent in `stock_items` are skipped.
- The older quantity-based 90-day reorder logic is fully retired; this is a replacement, not a side-by-side second algorithm.
