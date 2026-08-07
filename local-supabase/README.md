# TallyBridge local Supabase (Docker)

A self-contained Supabase that runs on one Windows PC and replaces the hosted
project for **reports and stock info**. Same REST API, same Studio UI, same
schema — so no application code changes.

> **There are two stacks.** This one is the **dev box**: Docker, and the only one
> with Supabase Studio. `../native-stack/` is the **client deliverable**:
> PostgreSQL + PostgREST + Caddy as Windows services, no Docker, no Studio — and
> unlike this one it starts without anyone logging in. Both serve the identical
> `/rest/v1` surface and share their schema, cutover and verification scripts via
> `../local-stack-shared/`, so they cannot drift. Only `SUPABASE_URL` differs
> (Kong `:54321` here, Caddy `:8000` there).

```powershell
.\install.ps1 -Seed          # install + copy data from the project in backend\.env
node scripts\healthcheck.mjs # prove it works
```

| | |
|---|---|
| API (PostgREST via Kong) | `http://127.0.0.1:54321` |
| Studio (the web UI) | `http://127.0.0.1:54323` |
| Postgres | `127.0.0.1:54322`, user `postgres` |

Ports and keys are in `.env`, generated per machine. **All three bind to
127.0.0.1** and are unreachable from the network.

---

## What runs, and what deliberately does not

Five services instead of the eleven in Supabase's official `docker-compose`:

| Service | Why it is here |
|---|---|
| `db` | `supabase/postgres` — the roles and extensions PostgREST expects |
| `rest` | PostgREST. Every read and write the app makes |
| `kong` | Serves `/rest/v1` with `apikey` auth, which is the shape supabase-js and the Python engine already speak |
| `meta` | postgres-meta, Studio's backend |
| `studio` | The Supabase web UI |

Dropped: `auth` (login is Firebase), `storage` + `imgproxy` (invoice images are in
GCS), `realtime` (nothing subscribes), `functions` (the `ingest-sync` edge
function is not in the live path — see the `/functions/` note below), and
`analytics` + `vector` (Logflare, the heaviest part of the file).

Kong stays even though it looks like an extra hop: it is what makes
`http://127.0.0.1:54321/rest/v1/...` work natively, so `SUPABASE_URL` is the only
thing that changes anywhere in the app.

### Sale and Purchase are not part of this deployment

The client profile is **reports and stock info only**. That is a function of what
gets installed, not of anything removed from the database:

- `install.ps1 -Seed -Profile reports` copies only the tables the reports read.
  `push_queue`, `Purchase_Matching`, `Audit_Trail_Purchase` and `scan_jobs` — the
  outbound push queue and the purchase-OCR lookups — are left empty.
- The **tables and RPCs are still created**, because the schema is verified as a
  whole against the live project. Dropping them would break that check and make
  turning the features on later a schema migration instead of a config change.
- Nothing writes to them because the parsing service and the push poller are not
  installed on the client machine.

---

## Connecting the rest of TallyBridge

### Backend (required — the reports are served by it)

```powershell
cd ..\backend
. .\use-local.ps1   ;   npm run dev     # dot-source it, do not just run it
```

**There are two Supabase clients in one backend process.** Setting one pair
leaves the other half of the app reading the cloud with every health check still
passing:

| Endpoints | Client | Env vars |
|---|---|---|
| `/stock`, `/vouchers`, `/outstanding`, `/parties`, `/pnl`, `/balance-sheet` | global (`db/supabase.ts`) | `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` |
| `/reorder-levels` (the MRP report) | `reorderSupabase` (`sync.ts:19`) | `SUPABASE_URL_Client`, `SUPABASE_SERVICE_KEY_CLIENT` |

> **`SUPABASE_URL_Client` — capital C, everything else lowercase.** Read literally
> at `sync.ts:20`. Spell it `SUPABASE_URL_CLIENT` and the override silently
> evaporates: no error, no log line, and the MRP report serves **cloud** data.

`use-local.ps1` sets both pairs and never edits `backend\.env`, so a fresh
terminal goes back to the cloud with no cleanup.

### Desktop app (Electron + Python engine)

```powershell
..\local-stack-shared\scripts\use-local-desktop.ps1                  # this stack
..\local-stack-shared\scripts\use-local-desktop.ps1 -Stack native    # the native one
..\local-stack-shared\scripts\use-local-desktop.ps1 -Show            # where does it point?
..\local-stack-shared\scripts\use-local-desktop.ps1 -Restore         # back to cloud
```

This one persists — it patches `%APPDATA%\tallybridge\tallybridge-config.json`,
after backing it up. **Restart TallyBridge afterwards**; the config is read when
the Python engine is spawned.

The app has two independent paths to the database and both must be moved:

- `controlPlaneUrl` / `controlPlaneApiKey` → the Express backend (`render` mode
  and all control-plane traffic).
- `syncIngestUrl` / `syncIngestKey` → PostgREST directly, bypassing the backend.
  Used by `hybrid` and `direct` modes, and **`hybrid` is the default**
  (`store.ts:78`). Miss this one and masters keep syncing to the cloud while the
  reports read local.

> **`syncIngestUrl` must keep its `/functions/v1/ingest-sync` shape** even though
> this stack runs no edge functions. `cloud_pusher.py:224` derives the REST base
> by splitting the URL on `/functions/`; give it a bare REST URL and it returns
> `""`, and ingest fails with *"Direct ingest needs SYNC_INGEST_URL to derive the
> Supabase REST endpoint"* — which reads like a missing setting rather than a
> wrongly-shaped one. Nothing ever requests the `/functions/` path itself.

Verify the write path without needing TallyPrime running:

```powershell
node scripts\verify-ingest-path.mjs
```

### Frontend

`frontend\` has no `.env`. Create one — these are the three vars its source reads:

```
VITE_BACKEND_URL=http://localhost:3001
VITE_API_KEY=localdevkey
VITE_COMPANY=K V ENTERPRISES
```

---

## Shipping it to a client

```powershell
.\build-client-package.ps1                  # ~1 MB, pulls images on site
.\build-client-package.ps1 -IncludeImages   # ~4.9 GB, installs with no internet
```

The packager flattens every SQL file into `schema\bundled\` in apply order (the
repo copy reads them from `..\backend` and `..\supabase\migrations`, which will
not exist on the client machine), and refuses to finish if `.env` or the rendered
`kong.yml` ends up inside the package.

On the client machine: install Docker Desktop and Node 18+, then

```powershell
.\install.ps1 -Seed -LiveUrl <project-url> -LiveKey <service-key> -Profile reports
```

**Secrets are generated per install** — Postgres password, JWT secret, and the
anon/service keys signed with it. Nothing is shared between two installs and
nothing is committed.

> This is why the package exists rather than shipping `supabase start`. The CLI's
> own banner says it: everything binds to `0.0.0.0`, and its keys
> (`sb_secret_N7UND0Ug...`) are compile-time constants — identical on every
> machine and published in Supabase's docs. On a shop PC that is the client's
> whole ledger on every network interface behind a publicly known key.

### Before go-live

- **Backups.** Once the client is off the cloud, this machine is the only copy of
  their books. The cloud project is an accidental off-site backup today and stops
  being one at cutover. Volume: `tallybridge_supabase_db_data`.
- **Docker Desktop needs a logged-in Windows session.** It does not run at the
  lock screen. TallyPrime and the TallyBridge tray app need one too, so this only
  matters if someone checks reports at night with the PC locked.
- **The backend listens on all interfaces** (Express default on port 3001), unlike
  the Supabase ports. It requires the API key, but bind it to 127.0.0.1 if
  nothing off-machine calls it.

---

## Everyday commands

```powershell
.\start.ps1                            # start (handles the post-reboot case)
.\stop.ps1                             # stop, keep data
.\stop.ps1 -Destroy                    # stop AND DELETE the database
node scripts\healthcheck.mjs           # end-to-end check
node scripts\apply-schema.mjs          # re-apply after adding a migration

# Shared with the native stack. TB_ENV_FILE picks which one they act on; it
# defaults to this stack, so it is only needed when targeting the other.
node ..\local-stack-shared\scripts\schema-parity.mjs        # diff against the cloud project
node ..\local-stack-shared\scripts\copy-live-to-local.mjs   # re-copy data (upserts, repeatable)
node ..\local-stack-shared\scripts\verify-ingest-path.mjs   # desktop-app write path
```

Data survives stop/start — it lives in a named volume, not in the containers.
Only `stop.ps1 -Destroy` (`docker compose down -v`) removes it.

### When something is wrong

| Symptom | Cause |
|---|---|
| Backend returns `404 {"error":"Company not found"}` but Studio shows the company | **A stale Supabase key in the backend.** Kong answers `401`, supabase-js turns that into an empty result, and the route reports a missing company. Happens after `bootstrap --rotate` or a reinstall without restarting the backend. Fix: `. .\use-local.ps1 ; npm run dev` again. |
| `password authentication failed for user "authenticator"` in the `rest` log | The db init scripts did not run. Almost always because `volumes/db` got mounted as a **directory** over `/docker-entrypoint-initdb.d`, which hides the image's own role-creating scripts. Mount individual files only. |
| `supabase start is already running` / `container is not running: exited` | Docker Desktop restarted. Run `.\start.ps1`, which does the required `stop` first. |
| A report returns 200 with plausible-looking numbers that match the cloud exactly | Prove which database answered before trusting it: write a sentinel row locally (`INSERT ... group_name='SENTINEL'`) and check whether the endpoint returns it. Matching counts prove nothing when local is a copy of live. |

---

## Notes for whoever maintains this

**The repo's SQL files cannot build this database on their own.** Three defects in
`supabase/migrations/` break `supabase db reset` on any machine, and
`apply-schema.mjs` works around all three — see its header for the detail:

1. **No base schema.** The migrations are deltas; the tables they `ALTER` are
   created in `backend/full_schema.sql` plus three loose files.
2. **Duplicate migration versions.** Three files parse to version `20260619`, and
   the CLI keys its migration table on that — a reset dies mid-run on a
   duplicate-key violation, leaving a half-applied database.
3. **Rollback scripts sort after their forward migration**, so a reset applies
   both and silently undoes the work.

**And they do not reproduce the live project.** Two tables, two RPCs and three
columns exist only on live, hand-applied through the SQL Editor — several repo
`.sql` files instruct exactly that in their headers. `schema/90-post/901_live_drift.sql`
closes the gap; every column in it was read off live's OpenAPI spec.

`schema-parity.mjs` proves the result, and states its own limit: PostgREST shows
tables, columns and RPCs, **not** indexes, constraints, triggers or RLS. A real
`pg_dump --schema-only` diff remains the stronger check, and is currently
impossible — the live Postgres password is not available (the Dashboard account
that owns the project is not accessible). Everything here was built through the
`service_role` key alone.

**No table enables RLS**, matching both `full_schema.sql` and the live project.
`902_grants.sql` then grants ALL to `anon`, so for a while the only thing between
an open port and the whole ledger was Kong's key-auth. The native stack has no
Kong, which made that a real hole — closed for both stacks by
`903_lockdown_anon.sql`, which revokes every table privilege from `anon`. Nothing
here authenticates as anon; the backend and the Python engine both use the
service key. If this stack is ever exposed beyond 127.0.0.1, RLS becomes
mandatory regardless.

**Keep every `.ps1` here 7-bit ASCII.** Windows PowerShell 5.1 reads a BOM-less
`.ps1` as ANSI, so a UTF-8 em-dash arrives as three characters ending in `"`,
which terminates whatever string it lands in and produces a pile of
"Unexpected token" errors on lines that look perfectly fine.
