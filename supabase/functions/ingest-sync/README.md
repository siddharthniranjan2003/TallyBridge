# ingest-sync

Phase 4 full direct-ingest endpoint for TallyBridge.

What it does now:

- validates `x-sync-key` with a timing-safe comparison
- validates `x-sync-contract-version`
- validates the sync payload envelope
- supports dry-run validation
- performs live writes for:
  - company identity upsert
  - groups
  - ledgers
  - vouchers
  - voucher items
  - voucher ledger entries
  - purchases
  - stock items
  - outstanding
  - profit/loss
  - balance sheet
  - trial balance

Recommended desktop mode:

- use `hybrid`
- inbound sync goes direct to Supabase
- keep `render` mode only as the emergency fallback

Required setup before live writes:

1. Deploy the Edge Function.
2. Apply `supabase/migrations/20260427_phase3_direct_ingest.sql`.
3. Apply `supabase/migrations/20260428_phase4_voucher_ingest.sql`.
4. Set `SYNC_INGEST_KEY` for the function.
5. Ensure the function can access `SUPABASE_URL` and a service-role key.

Health check:

```sh
curl https://<project>.supabase.co/functions/v1/ingest-sync
```

Dry-run test:

```sh
curl -X POST https://<project>.supabase.co/functions/v1/ingest-sync \
  -H "content-type: application/json" \
  -H "x-sync-key: <SYNC_INGEST_KEY>" \
  -H "x-sync-contract-version: 1" \
  -H "x-sync-dry-run: 1" \
  -d '{
    "sync_contract_version": 1,
    "company_name": "Demo Company",
    "sync_meta": {},
    "groups": [],
    "ledgers": [],
    "stock_items": []
  }'
```

Live-write note:

- voucher uploads must be chunked to `2000` rows or fewer per request
- the final voucher chunk finalizes reconciliation, updates `last_synced_at`, and writes `sync_log`
