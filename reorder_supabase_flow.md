# Reorder Level — Supabase Interaction Flow

> Endpoint: GET /api/sync/reorder-levels
> Default as_of_date: 2019-03-31

---

## Default Call

```bash
curl "http://localhost:3001/api/sync/reorder-levels" \
  -H "x-api-key: sb_publishable_oFyc0kQxjkga2QZw0kq3ZQ_ovmR0P9h"
```

No params. `as_of_date` defaults to `2019-03-31`.

---

## Step 1 — companies

```ts
supabase.from("companies").select("id").limit(1).single()
```

Returns one row. We take its `id` — companyId. Everything after this uses that ID.

---

## Step 2 — Compute the Window

No Supabase involved. Pure JS:

```
2019-03-31 - 89 days = 2019-01-01
window: 2019-01-01 to 2019-03-31
```

---

## Step 3 — purchases

```ts
supabase.from("purchases")
  .select("voucher_id")
  .eq("company_id", companyId)
  .eq("is_cancelled", false)
  .gte("date", "2019-01-01")
  .lte("date", "2019-03-31")
```

Paginated — fetches 1000 rows per page until done.
Returns ~869 rows. Extract `voucher_id` from each and deduplicate.

---

## Step 4 — voucher_items

869 IDs is too many for one query. Chunks into batches of 250:

```
Batch 1: voucher_id IN (250 IDs) → ~750 rows
Batch 2: voucher_id IN (250 IDs) → ~750 rows
Batch 3: voucher_id IN (250 IDs) → ~750 rows
Batch 4: voucher_id IN (119 IDs) → ~602 rows
```

Total: ~2,852 rows across 4 queries.

In Node.js, loop through all rows and build a map:

```
"C-10 DEBURING BLADE" → 13000
"WHITE GLASS"         → 5000
...
```

Items with blank name or quantity <= 0 are skipped.

---

## Step 5 — stock_items

```ts
supabase.from("stock_items")
  .select("name, unit, closing_qty")
  .eq("company_id", companyId)
  .order("name", { ascending: true })
```

6,891 rows. Paginated — fetches in 7 pages of 1000.

---

## Step 6 — Merge in Node.js

For each of the 6,891 stock items:

```
reorder_trigger = map.get(item.name) ?? 0
needs_reorder   = reorder_trigger > 0 AND closing_qty <= reorder_trigger
```

Sort: needs_reorder = true first, then alphabetical within each group.

---

## Step 7 — Response

```json
{
  "as_of_date": "2019-03-31",
  "window_from": "2019-01-01",
  "window_to": "2019-03-31",
  "total_items": 6891,
  "needs_reorder_count": 1320,
  "items": [...]
}
```

---

## Total Supabase Round Trips: 13

| Query | Hits |
|-------|------|
| companies | 1 |
| purchases (paginated) | 1 |
| voucher_items (batched) | 4 |
| stock_items (paginated) | 7 |
| **Total** | **13** |

---

## Tables Used (in order)

| # | Table | What we get |
|---|-------|-------------|
| 1 | `companies` | company ID |
| 2 | `purchases` | voucher_ids in date window |
| 3 | `voucher_items` | qty per stock item name |
| 4 | `stock_items` | closing_qty per item |
