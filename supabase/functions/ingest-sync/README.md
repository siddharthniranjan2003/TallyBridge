# ingest-sync

Phase 2 direct ingest skeleton for TallyBridge.

What it does now:

- validates `x-sync-key`
- validates `x-sync-contract-version`
- validates the sync payload envelope
- reports which sync domains are present
- supports dry-run tests only

What it does not do yet:

- no database writes
- no SQL RPC dispatch
- no domain ingestion logic

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
    "vouchers": []
  }'
```

Live sync uploads should remain on Render until Phase 3 starts.
