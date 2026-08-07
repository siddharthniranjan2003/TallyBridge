# TallyBridge native stack — Supabase-compatible, no Docker

PostgreSQL + PostgREST + Caddy running as **Windows services**. Serves the same
`/rest/v1` API as the hosted Supabase project, so nothing in the app changes —
only `SUPABASE_URL`.

```powershell
.\install.ps1 -Seed          # from an ELEVATED PowerShell
node scripts\healthcheck.mjs
```

| | |
|---|---|
| API (PostgREST via Caddy) | `http://127.0.0.1:8000` |
| PostgreSQL | `127.0.0.1:5432` |
| Data | `.\data` (configurable with `-DataDir`) |

Both bind to 127.0.0.1. Secrets are generated per machine into `config\.env`.

---

## Why this exists alongside `local-supabase/`

They are two deployments of **one schema**. Pick by machine, not by preference:

| | `local-supabase/` (Docker) | `native-stack/` (this) |
|---|---|---|
| Where | the dev box | the client's PC |
| Disk | 4.9 GB of images | ~850 MB |
| RAM | ~2 GB | ~450 MB |
| **Starts without anyone logging in** | **no** | **yes** |
| Needs WSL2 / virtualization | yes | no |
| Supabase Studio | yes | no |

Docker Desktop needs a logged-in Windows session, so an unattended reboot leaves
the client's reports dead until someone signs in. Windows services do not have
that problem — that is the entire reason this exists. Studio is the thing given
up, which is why the Docker stack stays on the dev box.

**The schema, the data cutover and every verification script are shared**
(`local-stack-shared/`), so the two cannot drift.

---

## The security difference that matters

The Docker stack has Kong in front of PostgREST rejecting any request without a
registered key. **This stack has no Kong.** Caddy rewrites and proxies; it does
not authenticate.

So the only thing between an open port and the client's ledger is that `anon`
holds no table privileges. Before `903_lockdown_anon.sql` existed, measured on
this machine:

```
GET http://127.0.0.1:3000/stock_items?select=name      (no headers at all)
-> 200  [{"name":"SOLID CARBIDE DRILL 8.6 YG"}, ...]
```

and `has_table_privilege('anon','stock_items','DELETE')` was `true`. `healthcheck.mjs`
now asserts both the unauthenticated and the anon-key request are refused, on
every run. Do not remove those checks.

---

## Layout

```
install.ps1            one-shot: vendor -> secrets -> initdb -> schema -> services -> seed -> verify
start.ps1 / stop.ps1   service mode if registered, process mode otherwise
fetch-vendor.ps1       pinned downloads into .\vendor
scripts/
  bootstrap.mjs        secrets + postgrest.conf + Caddyfile + WinSW XMLs
  db-init.ps1          initdb, Supabase roles, pgcrypto, realtime publication
  apply-schema.mjs     the shared SQL list, via native psql
  services.ps1         register / remove the Windows services (needs admin)
  healthcheck.mjs      end-to-end, including the auth-boundary checks
templates/             the three config templates bootstrap renders
config/                GENERATED, gitignored — holds this machine's secrets
vendor/                GENERATED, gitignored — postgrest, caddy, winsw, pgsql
data/                  the database
```

---

## Five things that are not obvious

**1. `postgrest.exe` is not self-contained on Windows.** Without `libpq.dll` on
PATH it exits `0xC0000135` (`STATUS_DLL_NOT_FOUND`) having written *nothing* to
stdout, stderr or the event log — it reads as a corrupt binary. The DLLs ship in
`vendor\pgsql\bin`. A Windows service inherits none of the shell's PATH, so the
service XML sets it explicitly.

**2. Caddy must use `handle_path`, not `handle`.** supabase-js builds
`${baseUrl}/rest/v1/<table>`; PostgREST serves `/<table>` and has no url-prefix
option. `handle_path` strips the prefix, `handle` does not — swap them and every
query 404s. That was the defect in design v1.

**3. pgcrypto goes in its own schema.** `backend/full_schema.sql:1` asks for it in
`public`, and PostgREST then publishes `armor`, `dearmor`, `gen_salt`,
`gen_random_uuid`, `pgp_armor_headers` and `pgp_key_id` as REST RPCs — parity
reported `rpcs live=13 local=19`. `db-init.ps1` installs it into `extensions`
first, exactly as Supabase does, which makes full_schema's statement a harmless
no-op. `gen_random_uuid()` still resolves: it is a core builtin from PG13 on, not
pgcrypto's.

**4. An empty `supabase_realtime` publication has to exist.**
`backend/supabase_scan_jobs.sql:53` does `ALTER PUBLICATION supabase_realtime ADD
TABLE scan_jobs`, which is a hard error on stock PostgreSQL and aborts the schema
apply at the second file. `db-init.ps1` creates it. Nothing subscribes; it exists
so one schema definition serves Docker, native and cloud alike.

**5. `syncIngestUrl` must keep its `/functions/v1/ingest-sync` suffix**, even
though no edge functions run here. `cloud_pusher.py:224` derives the REST base by
splitting on `/functions/`; a bare REST URL yields `""` and ingest fails with
*"Direct ingest needs SYNC_INGEST_URL to derive the Supabase REST endpoint"*.

---

## Connecting the rest of TallyBridge

```powershell
# backend  — dot-source it, do not just run it
cd ..\backend ; . .\use-native.ps1 ; npm run dev

# desktop app
..\local-stack-shared\scripts\use-local-desktop.ps1 -Stack native
..\local-stack-shared\scripts\use-local-desktop.ps1 -Restore    # back to cloud
```

> **`SUPABASE_URL_Client` — capital C, everything else lowercase.** Read literally
> at `sync.ts:20`. Spell it `SUPABASE_URL_CLIENT` and the MRP report silently
> serves **cloud** data while every health check passes. `use-native.ps1` sets
> both client pairs.

---

## Shipping it — one .exe

```powershell
.\fetch-vendor.ps1 -IncludePostgres
.\scripts\trim-vendor.ps1            # 848 MB -> 117 MB, drops pgAdmin and friends
.\build-installer.ps1 -Version 1.0.0
```

Produces `dist-client\TallyBridgeServer-Setup-1.0.0.exe`. The client
double-clicks it, accepts the UAC prompt, optionally pastes their project URL and
service key on one wizard page to copy existing data, and waits. **Their machine
needs nothing pre-installed** — PostgreSQL, PostgREST, Caddy, WinSW, `node.exe`
and the built Express backend are all inside.

What the installer does, in `installer\post-install.ps1` — a plain script, so the
same sequence can be re-run by hand as a repair:

1. generate this machine's secrets and config
2. `initdb`, Supabase roles, pgcrypto in `extensions`, the realtime publication
3. apply the 17 schema files
4. register and start **four** services (postgres, postgrest, gateway, backend)
5. optionally copy data with the `reports` profile
6. health check

### What is deliberately not in it

- **`backend\.env`.** It holds the cloud `service_role` key and the Firebase
  service-account private key. The backend service takes its configuration from
  its WinSW XML, generated per machine.
- **Any Google credential at all.** `db/firebase.ts` initialises lazily, so an
  API-key-only deployment boots without `FIREBASE_SERVICE_ACCOUNT_B64`. It used
  to throw at module load, which would have forced a private key onto the client's
  PC purely to let the process start.
- **The data directory.** It lives in `%ProgramData%\TallyBridge\data`, not under
  Program Files, and the uninstaller does not remove it — after cutover that
  folder is the client's entire books.

### The script path is still there

For a machine that already has Node, or for development:

```powershell
.\build-client-package.ps1                    # ~1 MB, downloads binaries on site
.\install.ps1 -Seed -LiveUrl <url> -LiveKey <service-key>
```

**Add an antivirus exclusion for the install folder at setup.** `postgrest.exe`,
`caddy.exe` and the WinSW copies are unsigned; AV quarantining one *weeks* after a
clean install presents as "reports stopped working" with nothing recent to blame.

### Still open before go-live

- **Backups.** After cutover this machine is the only copy of the client's books.
  The cloud project is an accidental off-site copy today and stops being one.
  Nothing schedules a `pg_dump` of `.\data` yet.
- **The backend listens on all interfaces** (Express default on 3001), unlike
  Postgres and Caddy. It requires the API key, but bind it to 127.0.0.1 if nothing
  off-machine calls it.
- **Parity is REST-surface only.** `schema-parity.mjs` compares tables, columns
  and RPCs — not indexes, constraints, triggers or RLS. A real `pg_dump
  --schema-only` diff against live is still blocked on the live DB password.

---

## Everyday commands

```powershell
.\start.ps1                                       # service mode if registered
.\stop.ps1                                        # data always kept
.\scripts\services.ps1 -Status                    # are the services registered?
.\scripts\services.ps1 -Install                   # (elevated)
node scripts\healthcheck.mjs
node scripts\apply-schema.mjs                     # after adding a migration
$env:TB_ENV_FILE=".\config\.env"                  # then the shared scripts:
node ..\local-stack-shared\scripts\schema-parity.mjs
node ..\local-stack-shared\scripts\copy-live-to-local.mjs --profile reports
node ..\local-stack-shared\scripts\verify-ingest-path.mjs
```
