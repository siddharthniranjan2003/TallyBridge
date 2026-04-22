# Purchase Section — Implementation Explainer

Date: 2026-04-07
Files changed: `backend/src/routes/sync.ts`, `backend/full_schema.sql`, `backend/supabase_schema_v4.sql`

---

## What Was Built

A dedicated `purchases` table in the backend that is automatically populated from voucher data every time a sync runs. No changes were made to the desktop connector, the Python sync engine, or the Electron app.

The implementation added:

- A `purchases` table in Supabase with columns for `company_id`, `voucher_id`, `tally_guid`, `voucher_number`, `voucher_type`, `date`, `party_name`, `amount`, `narration`, `reference`, `is_cancelled`, `is_invoice`, `synced_at`
- A `isPurchaseVoucherType()` classifier that filters vouchers into purchase vs non-purchase
- Post-voucher-upsert logic that derives purchase rows from the just-synced voucher set and upserts them into `purchases`
- Reconciliation logic that removes stale purchase rows when vouchers are deleted or fall out of the sync window
- A `GET /api/sync/purchases` endpoint that returns all purchase rows for a company with full pagination

---

## Why This Approach — Backend Derived, Not a New Sync Section

The desktop sync payload already contains vouchers. Every purchase in Tally is a voucher with `voucher_type = "Purchase"`. Rather than teaching the Python connector to emit a separate `purchases` section (which would require changes to `sync_main.py`, the Electron IPC handlers, and the backend POST route), purchases are derived on the backend after vouchers are written.

This keeps two things stable:

1. The desktop-to-backend contract is unchanged. Any older version of the desktop app still syncs correctly.
2. The `purchases` table is always consistent with `vouchers` because it is built from the same data in the same transaction cycle.

The tradeoff is that purchases are always a computed view of vouchers, not an independently sourced dataset. That is acceptable here because Tally itself treats purchases as a voucher type — there is no separate purchase ledger that exists independently of the voucher record.

---

## Why the `purchases` Table Exists at All

Querying purchases directly from the `vouchers` table with a `WHERE voucher_type ILIKE '%purchase%'` filter works, but it has practical problems at scale:

- Stock has 6891 rows, outstanding has 4921 rows, and vouchers will grow over time. Filtering vouchers for purchase-type rows on every request adds unnecessary query cost.
- Purchase-specific indexes (`company_id + date`, `company_id + party_name`) cannot be added to `vouchers` without affecting the general voucher query path.
- Future purchase-specific columns (item totals, GST breakdowns, supplier lead time) would pollute the vouchers table if stored there.
- Supabase Row Level Security and API access can be scoped to `purchases` independently from `vouchers`.

A dedicated table makes purchases a first-class queryable entity.

---

## Why Purchase Orders Are Excluded

Tally has two separate voucher types that both contain the word "purchase":

| Type | What it is | Ledger effect | Stock effect |
|---|---|---|---|
| `Purchase` | Goods received, liability to supplier created | Yes — debits Purchase ledger, credits supplier | Updates actual closing stock |
| `Purchase Order` | Intent to buy, sent to supplier before delivery | None — no accounting entry | Updates "on order" quantity only |

Purchase Orders are not financial transactions. They have no debit or credit. Including them in a `purchases` table that is intended to represent actual procurement activity would corrupt any financial calculation built on top of it — total spend, supplier payables, stock cost basis, reorder history.

The classifier `isPurchaseVoucherType()` was originally written as a simple `.includes("purchase")` substring match, which would have silently included Purchase Orders. It was corrected to an explicit allowlist:

```
"purchase"
"local purchase"
"import purchase"
```

With a fallback for custom user-defined voucher types that contain "purchase" but not "order". This means a business that has created a custom type called "Purchase - Domestic" will still be captured, but "Purchase Order" and any order-variant will not.

---

## Why Reconciliation Needs Pagination

The reconciliation step loads all existing purchase rows in the current sync window from the database, compares them against the incoming set from Tally, and deletes any that are no longer present. This handles vouchers that were deleted in Tally since the last sync.

The original implementation did this with a bare Supabase `.select()` call, which has a hard cap of 1000 rows per response. For a company with more than 1000 purchase vouchers in the financial year:

- Rows 1001 onwards would never be loaded into the comparison set
- Those rows would never be identified as stale
- Deleted vouchers beyond position 1000 would remain in the database permanently
- The `purchases` table would accumulate ghost rows that no longer exist in Tally

The fix uses the `fetchAllPages()` helper which loops with `.range(from, to)` until Supabase returns fewer rows than the page size, guaranteeing all existing rows are loaded before the comparison runs.

The same pagination issue affected the `GET /stock`, `GET /outstanding`, `GET /vouchers`, and `GET /parties` read endpoints, which were fixed in the same session. All large-result Supabase queries now go through `fetchAllPages()`.

---

## Why This Matters for Reorder Level Functionality

Reorder level logic answers the question: *which stock items are running low, and who should we buy from to replenish them?*

That question has two parts:

**Part 1 — What is the current stock level?**

This comes from the `stock_items` table, which is already synced. Each row has `closing_qty`, `closing_value`, and `rate`. A reorder threshold can be compared directly against `closing_qty`.

**Part 2 — Who supplies each item, at what price, and how often?**

This is where `purchases` becomes the foundation. The purchase-to-item relationship is:

```
purchases  →  vouchers  →  voucher_items
```

`voucher_items` holds the line-item detail: `stock_item_name`, `quantity`, `rate`, `unit`. Every purchase voucher that contains a stock item has a corresponding row in `voucher_items` linked through `voucher_id`.

The `purchases` table gives you the supplier (`party_name`), date, and voucher reference. The `voucher_items` table gives you what was bought and at what price. Joining them gives you the full procurement history per stock item:

```sql
SELECT
  si.name                  AS stock_item,
  si.closing_qty           AS current_stock,
  p.party_name             AS supplier,
  p.date                   AS last_purchase_date,
  vi.quantity              AS qty_ordered,
  vi.rate                  AS unit_rate
FROM stock_items si
JOIN voucher_items vi ON vi.stock_item_name = si.name
JOIN purchases p      ON p.voucher_id = vi.voucher_id
WHERE si.company_id = $1
ORDER BY si.name, p.date DESC;
```

Without a clean `purchases` table, this join would have to go through the full `vouchers` table and filter by type at query time — slower and more fragile.

**What is still needed for reorder levels:**

1. A reorder threshold per stock item. Tally has `REORDERLEVEL` and `MINORDERLEVEL` fields on StockItem that are not currently fetched. These could be added to the `get_stock_items()` XML request in `tally_client.py` and stored in new columns on `stock_items`. Alternatively, thresholds can be defined inside TallyBridge independently.

2. A query or API endpoint that compares `closing_qty` against the threshold and returns items that need reordering, enriched with supplier history from `purchases` + `voucher_items`.

3. Optionally, a `reorder_rules` table if thresholds are to be managed within TallyBridge rather than read from Tally.

The `purchases` table as built is the correct and necessary foundation for step 2. It does not need to change for reorder level to work — the supplier history join will work as-is once the threshold comparison layer is added.

---

## Schema Migration Note

For existing Supabase deployments, run `backend/supabase_schema_v4.sql` to add the `purchases` table and its indexes.

Fresh deployments use `backend/full_schema.sql` which already includes the table.

No data migration is needed. On the next sync after the migration, purchase rows will be derived and populated automatically from the vouchers already in the database.
