# Purchase Section Implementation Notes

## Goal

Add a dedicated `purchases` section/table alongside existing synced sections like `companies`, `balance_sheet`, and `trial_balance`, without changing the desktop sync payload shape.

## How It Was Implemented

### 1. Kept desktop sync payload unchanged

The desktop connector still sends vouchers as part of the normal sync payload.

No new `purchases` section was added to the Python/Electron sync request.

Reason:
- purchases are already present inside `vouchers`
- deriving them on the backend is simpler and avoids changing the live desktop contract

### 2. Added a new `purchases` table in the backend schema

Added the table in:
- [backend/full_schema.sql](/D:/Desktop/TallyBridge/backend/full_schema.sql)
- [backend/supabase_schema_v4.sql](/D:/Desktop/TallyBridge/backend/supabase_schema_v4.sql)

The table stores:
- `company_id`
- `voucher_id`
- `tally_guid`
- `voucher_number`
- `voucher_type`
- `date`
- `party_name`
- `amount`
- `narration`
- `reference`
- `is_cancelled`
- `is_invoice`
- `synced_at`

It also has:
- `UNIQUE(company_id, tally_guid)`
- indexes on `company_id + tally_guid`, `company_id + date`, and `company_id + party_name`

### 3. Derived purchase rows from voucher rows during backend sync

Implemented in:
- [backend/src/routes/sync.ts](/D:/Desktop/TallyBridge/backend/src/routes/sync.ts)

Added helper:
- `isPurchaseVoucherType(value)`

Current rule:
- any voucher whose `voucher_type` contains `"purchase"` case-insensitively is treated as a purchase

Example matches:
- `Purchase`
- `Purchase Order` if present
- `Local Purchase` if present

### 4. Created purchase rows after voucher upsert

Flow inside `POST /api/sync`:

1. upsert normal vouchers into `vouchers`
2. read back voucher ids using `tally_guid`
3. build purchase rows only for purchase-like voucher types
4. upsert those rows into `purchases`

This keeps `purchases.voucher_id` linked to the canonical `vouchers` row.

### 5. Added reconciliation for purchases

Also in:
- [backend/src/routes/sync.ts](/D:/Desktop/TallyBridge/backend/src/routes/sync.ts)

Added:
- `selectPurchasesForReconciliation(...)`
- `reconcilePurchaseScope(...)`

Behavior:
- on full or incremental voucher sync, stale purchase rows are removed using `tally_guid`
- this mirrors the existing voucher reconciliation logic

### 6. Added purchase count to sync records

Updated record counting in:
- [backend/src/routes/sync.ts](/D:/Desktop/TallyBridge/backend/src/routes/sync.ts)

`records.purchases` is now included when purchase vouchers are present.

### 7. Added a dedicated GET API

Added route:
- `GET /api/sync/purchases`

Implemented in:
- [backend/src/routes/sync.ts](/D:/Desktop/TallyBridge/backend/src/routes/sync.ts)

Behavior:
- resolves company by `company_id`, `company_guid`, or `company_name`
- reads from `purchases`
- sorts by `date desc`, then `id desc`
- uses the shared pagination helper

## Why This Approach Was Chosen

This backend-derived design was chosen because:
- purchases are already a subset of vouchers
- it avoids changing the desktop sync payload
- it keeps `purchases` consistent with voucher reconciliation
- it gives Supabase a clean dedicated table for purchase-specific queries/UI

## What Must Be Run In Supabase

Run:
- [backend/supabase_schema_v4.sql](/D:/Desktop/TallyBridge/backend/supabase_schema_v4.sql)

That creates the `purchases` table and indexes.

## Follow-Up Notes

Current classification is broad: any voucher type containing `"purchase"` is included.

If needed later, this can be tightened to:
- exact voucher types only
- a configurable allowlist
- separate tables for purchases vs purchase orders
