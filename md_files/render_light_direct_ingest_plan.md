# TallyBridge Render-Light Direct Ingest Plan

## Exact wording from the two earlier replies

### Reply 1

Assuming "how we used to do" means `desktop app -> direct data write` and using Render only for the `push-to-Tally queue`, the idea is valid.

**Pros**
- Much lower Render usage, because the heavy inbound sync payloads and backfills stop hitting your Render server.
- Fewer Render cold-start and request-timeout problems during big syncs.
- Cheaper architecture on the free tier, since Render becomes mostly a light control plane for push jobs.
- Faster bulk sync is possible if the desktop writes closer to the database/storage layer.

**Cons**
- You lose the central logic currently living in `backend /api/sync`: validation, batching, cleanup, upserts, snapshot handling, company resolution, and schema shielding.
- The desktop client becomes tightly coupled to your database schema, so every schema change becomes a desktop release problem.
- Security gets harder, because direct writes usually mean exposing more powerful credentials or designing stricter per-client auth/RLS.
- Debugging becomes more fragmented: some failures happen in the desktop, some in Render, some in the DB layer.
- It increases client complexity and makes the app harder to maintain across versions.

**Net**
- Good for saving Render credits.
- Risky if done as raw direct DB writes from the desktop.
- Best version of this approach is usually: keep Render for control/auth/orchestration, but move the heavy ingest path to a cheaper backend path or a more direct but still controlled write layer.

### Reply 2

What I mean is: keep Render as the "brain," but don't make it carry the full weight of every sync.

In your current shape, the desktop app sends large sync payloads to Render at `/api/sync`, and Render does the heavy upserts, cleanup, batching, and snapshot logic. That makes Render both the control plane and the data-ingest worker. On a free tier, that is the expensive part. A better split is:

1. Render handles lightweight responsibilities:
- API key auth
- company lookup and routing
- push queue creation and polling
- metadata like last sync status, sync timestamps, job states
- commands like "push this voucher back into Tally"

2. A cheaper path handles heavy inbound sync:
- the desktop app writes directly to Supabase using tightly scoped credentials or RPCs
- or the desktop uploads raw sync payloads to cheap object storage / queue
- or a serverless/background worker processes those payloads outside Render web requests

Why this helps:
- huge voucher/master/report payloads stop consuming Render request time and RAM
- cold starts matter less
- long syncs stop depending on one fragile web process
- Render stays alive for the parts that actually need a central coordinator

The key is "controlled direct write," not "desktop can do anything." That means the desktop should not get unrestricted database power. Good ways to control it are:

- Supabase RPC functions:
  The desktop calls specific database functions like `ingest_vouchers(company_id, payload)` instead of writing arbitrary tables.
- Row Level Security plus scoped auth:
  The desktop can only write rows for its own company/account.
- Staging tables:
  The desktop uploads raw data into a staging area, and DB functions or workers promote it into final tables.
- Signed short-lived tokens:
  Render issues temporary write permission, instead of storing a master secret in the desktop app.

The cleanest architecture for you is probably this:

- Desktop -> direct heavy ingest path for sync data
- Render -> auth, job orchestration, push-to-Tally queue, admin/status APIs
- Supabase/DB -> final persistence and maybe stored procedures for safe ingest

That gives you a nice split:
- inbound sync is cheap and scalable
- outbound push stays centralized and controllable
- Render credits are used for coordination, not bulk transport

The tradeoff is that you move some complexity into the database layer or ingestion layer. But that complexity is usually worth it, because it is more predictable than relying on a free Render web service to absorb large sync traffic.

## What this means for our current codebase

Right now the heavy inbound path is:

`Electron SyncEngine -> Python sync_main.py -> src/python/cloud_pusher.py -> POST {BACKEND_URL}/api/sync -> backend/src/routes/sync.ts -> Supabase`

The outbound push path is already lighter and should stay centralized:

`Render push_queue -> src/main/push-queue-poller.ts -> Python poll_push_queue -> local Tally push`

### Current heavy path files

- `D:\Desktop\TallyBridge\src\main\sync-engine.ts`
  Passes `BACKEND_URL` and `API_KEY` into the Python worker for every sync.
- `D:\Desktop\TallyBridge\src\python\cloud_pusher.py`
  Sends the full sync payload to `POST /api/sync`.
- `D:\Desktop\TallyBridge\backend\src\routes\sync.ts`
  Owns the large validation, upsert, cleanup, voucher graph, snapshot, and sync log logic.

### Current lightweight control-plane files we should keep on Render

- `D:\Desktop\TallyBridge\src\python\cloud_pusher.py`
  Uses:
  - `GET /api/sync/alter-ids`
  - `GET /api/sync/push-queue`
  - `POST /api/sync/push-results`
- `D:\Desktop\TallyBridge\backend\src\routes\sync.ts`
  Already exposes the control-plane and read APIs.
- `D:\Desktop\TallyBridge\src\main\push-queue-poller.ts`
  Polls outbound push jobs and should keep talking to Render.
- `D:\Desktop\TallyBridge\src\main\local-push-server.ts`
  Handles local push into Tally and does not need to move.

## The exact implementation we should do

We should not do raw desktop -> Supabase table writes with a service-role key.

We should do this instead:

`Desktop Python -> Supabase Edge Function ingest-sync -> SQL RPC / controlled DB functions -> Supabase tables`

and keep this as-is:

`Desktop Python -> Render control-plane endpoints for alter-ids, push-queue, push-results, read APIs`

That gives us:

- no Render credit burn for huge inbound sync payloads
- no service-role key inside the desktop app
- one controlled ingest surface
- no change to the outbound push-to-Tally flow

## Target architecture

### Control plane

Keep Render for:

- `GET /api/sync/alter-ids`
- `GET /api/sync/push-queue`
- `POST /api/sync/push-results`
- `POST /api/push-voucher`
- `POST /api/push-invoice`
- all read/dashboard endpoints under `GET /api/sync/*`

### Heavy ingest plane

Move the current `POST /api/sync` workload away from Render to:

- Supabase Edge Function:
  `https://<project>.supabase.co/functions/v1/ingest-sync`
- backed by SQL RPC / helper functions that perform:
  - company upsert
  - master upserts
  - voucher graph replace/upsert
  - snapshot replace for outstanding, profit/loss, balance sheet, trial balance
  - sync_log write
  - company sync timestamp updates

## Exact code changes by file

### 1. Split control-plane URL from ingest URL

#### `D:\Desktop\TallyBridge\src\main\store.ts`

Add new config fields:

- `controlPlaneUrl`
- `controlPlaneApiKey`
- `syncIngestMode`
- `syncIngestUrl`
- `syncIngestKey`

Recommended values:

- `controlPlaneUrl`
  Existing Render URL
- `controlPlaneApiKey`
  Existing app API key for Render
- `syncIngestMode`
  `"render"` or `"direct"`
- `syncIngestUrl`
  Supabase Edge Function URL
- `syncIngestKey`
  secret used only for the ingest edge function

Do not delete `backendUrl` and `apiKey` in one jump. First migration step should keep backward compatibility, then rename in a later cleanup.

### 2. Expose the new fields in Settings

#### `D:\Desktop\TallyBridge\src\renderer\pages\Settings.tsx`

Change the UI so it becomes explicit that we now have two network roles:

- `Control Plane URL (Render)`
- `Control Plane API Key`
- `Sync Ingest Mode`
- `Sync Ingest URL`
- `Sync Ingest Key`

Suggested behavior:

- default `Sync Ingest Mode = render`
- when `render`, keep current behavior unchanged
- when `direct`, `push()` goes to the Supabase ingest endpoint while queue/read operations still use Render

### 3. Pass two sets of env vars into Python

#### `D:\Desktop\TallyBridge\src\main\sync-engine.ts`

Today we pass:

- `BACKEND_URL`
- `API_KEY`

Change this to pass:

- `CONTROL_PLANE_URL`
- `CONTROL_PLANE_API_KEY`
- `SYNC_INGEST_MODE`
- `SYNC_INGEST_URL`
- `SYNC_INGEST_KEY`

Keep the old vars during the transition so the Python side can fall back safely.

### 4. Split cloud_pusher.py into control-plane calls vs ingest calls

#### `D:\Desktop\TallyBridge\src\python\cloud_pusher.py`

Keep these on Render:

- `fetch_remote_alter_ids()`
- `fetch_pending_push_vouchers()`
- `mark_push_results()`

Move only the heavy sync submit path:

- current `push(payload)` should become transport-aware

Recommended structure:

- `push(payload)`:
  - if `SYNC_INGEST_MODE == "render"`:
    - existing `POST {BACKEND_URL}/api/sync`
  - if `SYNC_INGEST_MODE == "direct"`:
    - `POST {SYNC_INGEST_URL}` with the same payload

Also add better log labels:

- `[Control]` for Render calls
- `[Ingest]` for direct ingest calls

This will make debugging much easier in `SyncLog`.

### 5. Keep Python sync_main.py mostly unchanged

#### `D:\Desktop\TallyBridge\src\python\sync_main.py`

This file should stay mostly the same.

It still:

- extracts data from Tally
- builds the payload
- calls `push(payload)`

The main change is only that `push(payload)` now points to a different transport in direct mode.

### 6. Add a Supabase Edge Function for ingest

Add a new Supabase function folder, for example:

- `D:\Desktop\TallyBridge\supabase\functions\ingest-sync\index.ts`

The function should:

1. verify `x-sync-key` or bearer token
2. parse the exact same JSON body we currently send to `POST /api/sync`
3. call a database RPC such as:
   - `tb_ingest_sync(payload jsonb)`
4. return the same response shape that the desktop currently expects:
   - `{ success: true, records: {...}, ... }`

Important:

- this function should stay thin
- it should not re-implement the whole Express route in Deno/TypeScript
- the heavy mutation logic should live in SQL helper functions

### 7. Move heavy mutation logic into SQL RPC/helper functions

Add a migration area, for example:

- `D:\Desktop\TallyBridge\supabase\migrations\2026xxxx_tb_ingest_sync.sql`

or, if we want to stay closer to the current repo layout:

- `D:\Desktop\TallyBridge\backend\tb_ingest_sync.sql`

The SQL layer should own these responsibilities:

- company lookup/upsert by `guid` or `name`
- group upsert
- ledger upsert
- stock item upsert
- outstanding snapshot replace
- profit_loss snapshot replace
- balance_sheet snapshot replace
- trial_balance snapshot replace
- voucher upsert by `(company_id, tally_guid)`
- voucher child rows replace:
  - `voucher_items`
  - `voucher_ledger_entries`
- purchases table refresh for purchase voucher types
- company sync state update:
  - `last_synced_at`
  - `last_outstanding_synced_at`
  - `last_profit_loss_synced_at`
  - `last_balance_sheet_synced_at`
  - `last_trial_balance_synced_at`
- sync log write

This is the most important design choice:

- Render should stop owning the heavy write algorithm
- the database layer should own the heavy write algorithm

### 8. Turn Render `/api/sync` into a compatibility fallback

#### `D:\Desktop\TallyBridge\backend\src\routes\sync.ts`

Do not delete `POST /api/sync` immediately.

During migration it should remain available for:

- rollback
- old desktop versions
- debugging comparison

After the direct path is proven stable, `POST /api/sync` can be:

- kept as a fallback shim that calls the same SQL RPC
- or eventually removed from the desktop path

The important point is:

- Render remains the control plane
- Render stops being the main bulk-ingest worker

## Recommended rollout order

### Phase 1. Transport split only

Goal:

- add the new config fields
- keep actual sync target as Render

Files:

- `src/main/store.ts`
- `src/renderer/pages/Settings.tsx`
- `src/main/sync-engine.ts`
- `src/python/cloud_pusher.py`

Result:

- we can switch between `render` and `direct` without changing payload-building logic

### Phase 2. Create direct ingest endpoint

Goal:

- deploy Supabase Edge Function `ingest-sync`
- deploy SQL migration with `tb_ingest_sync(...)`

Result:

- desktop can send the same sync payload to Supabase instead of Render

### Phase 3. Pilot on masters and snapshots first

Goal:

- first validate:
  - companies
  - groups
  - ledgers
  - stock_items
  - outstanding
  - profit_loss
  - balance_sheet
  - trial_balance

Reason:

- these are easier than the voucher graph

### Phase 4. Move vouchers and purchases

Goal:

- migrate the heaviest section:
  - vouchers
  - voucher_items
  - voucher_ledger_entries
  - purchases

Reason:

- this is where most Render credits and timeout risk are likely being spent

### Phase 5. Keep Render only for control/read/outbound push

At this point Render should mainly serve:

- change detection helpers
- read APIs
- queue APIs
- push-to-Tally orchestration

## Why this is the right execution plan for our code

Because our repo already has a clean separation between:

- desktop extraction
- cloud submission
- outbound queue polling

The smallest useful change is not "rewrite everything."

The smallest useful change is:

1. split transport roles
2. keep outbound push on Render
3. move only inbound heavy sync off Render

That preserves:

- `src/main/push-queue-poller.ts`
- `src/main/local-push-server.ts`
- `GET /api/sync/*` read APIs
- outbound queue flows

while replacing only the expensive part:

- `POST /api/sync`

## What we should not do

We should not:

- ship the Supabase service-role key inside the desktop app
- let the desktop directly upsert arbitrary tables with unrestricted credentials
- remove Render `/api/sync` before the direct path has a rollback-tested fallback
- try to migrate outbound push and inbound heavy sync in the same change

## Final recommendation

Yes, we should move heavy inbound sync off Render.

But in this repo, the right implementation is:

- keep Render as the control plane
- add a separate direct ingest plane
- make the desktop choose that plane only for heavy sync payloads
- implement the heavy write logic in a controlled Supabase layer
- leave push-to-Tally on Render exactly as you suggested

That gets us the Render savings you want without making the desktop app dangerously overpowered.
