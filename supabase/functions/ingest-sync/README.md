# ingest-sync

Phase 3 direct ingest endpoint for TallyBridge.

What it does now:

- validates `x-sync-key` with a timing-safe comparison
- validates `x-sync-contract-version`
- validates the sync payload envelope
- supports dry-run validation
- performs live writes for:
  - company identity upsert
  - groups
  - ledgers
  - stock items
  - outstanding
  - profit/loss
  - balance sheet
  - trial balance

What it does not do yet:

- no live voucher writes
- no purchase graph writes
- no replacement of the Render voucher route

Recommended desktop mode:

- use `hybrid`
- masters and snapshots go direct
- vouchers stay on Render

Required setup before live writes:

1. Deploy the Edge Function.
2. Apply `supabase/migrations/20260427_phase3_direct_ingest.sql`.
3. Set `SYNC_INGEST_KEY` for the function.
4. Ensure the function can access `SUPABASE_URL` and a service-role key.

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

- if the payload includes `vouchers`, the function will reject it
- use desktop `hybrid` mode for Phase 3
