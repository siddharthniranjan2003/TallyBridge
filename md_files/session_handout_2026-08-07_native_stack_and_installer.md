# Session Handout — Two local stacks, one shared schema, one 68 MB client installer

> Read this to continue in a fresh chat with full context.
> **Date:** 2026-08-07 · **Branch:** `feat/on-prem-supabase-stacks` · **Base:** `27e0bf1`
> Supersedes the packaging recommendations in
> `md_files/session_handout_2026-08-06_local_supabase_stack.md` §9 (its size estimate
> was wrong — see §8) and revives design v2 from
> `md_files/session_handout_local_windows_stack_design.md` §6.

---

## 0. TL;DR

- There are now **two deployments of one schema**, both verified against the live
  testing project, plus a **single 68 MB `.exe`** for the client.
- `local-supabase/` — Docker, 5 services, **has Studio**. The dev box.
- `native-stack/` — PostgreSQL + PostgREST + Caddy + backend as **Windows services**,
  no Docker, no Studio. The client deliverable.
- `local-stack-shared/` — the schema list, cutover, parity, ingest and desktop-switch
  scripts. Both stacks import it, so they cannot drift.
- **Everything is committed** on `feat/on-prem-supabase-stacks` (6 commits), including
  the installer binary. **Nothing is pushed.**
- **One thing is unverified: Windows service registration.** It needs administrator and
  the build session had none. See §7.

---

## 1. Why two stacks

The client's PC **cannot run Docker**, and Docker Desktop needs a logged-in Windows
session — an unattended reboot leaves their reports dead until someone signs in.
Windows services do not have that problem. Podman and Docker CE inside WSL2 were both
considered and rejected: they still sit on WSL2 and still need a session.

| | `local-supabase/` (Docker) | `native-stack/` (services) |
|---|---|---|
| Where | dev box | client PC |
| Disk | 4.9 GB images | ~850 MB |
| RAM | ~2 GB | ~450 MB |
| **Starts with nobody logged in** | **no** | **yes** |
| Needs WSL2 / virtualization | yes | no |
| Supabase Studio | yes | **no** — the one thing given up |
| Gateway | Kong `:54321` | Caddy `:8000` |
| Client machine needs | Docker Desktop + Node | **nothing** (all bundled) |

---

## 2. What is running / how to bring it back

```powershell
# Docker stack (dev box, has Studio)
cd local-supabase ; .\start.ps1
#   API http://127.0.0.1:54321 · Studio http://127.0.0.1:54323 · PG 127.0.0.1:54322

# Native stack
cd native-stack ; .\start.ps1
#   API http://127.0.0.1:8000 · PG 127.0.0.1:5432

# Backend — DOT-SOURCE, do not just run
cd backend ; . .\use-local.ps1  ; npm run dev     # -> Docker stack
cd backend ; . .\use-native.ps1 ; npm run dev     # -> native stack

# Desktop app (persists to %APPDATA%, backs up the cloud config first)
local-stack-shared\scripts\use-local-desktop.ps1 -Stack native
local-stack-shared\scripts\use-local-desktop.ps1 -Show
local-stack-shared\scripts\use-local-desktop.ps1 -Restore
```

**On a fresh machine the stacks need rebuilding** — `.env`, `config/`, `vendor/` and
`data/` are all gitignored:

```powershell
cd local-supabase ; .\install.ps1 -Seed                       # Docker
cd native-stack   ; .\fetch-vendor.ps1 -IncludePostgres
                    .\scripts\trim-vendor.ps1
                    .\install.ps1 -Seed                        # native (elevated)
```

Verification, any time:

```powershell
node local-supabase\scripts\healthcheck.mjs
node native-stack\scripts\healthcheck.mjs
$env:TB_ENV_FILE="...\config\.env"   # picks which stack the shared scripts act on
node local-stack-shared\scripts\schema-parity.mjs
node local-stack-shared\scripts\verify-ingest-path.mjs
```

---

## 3. The single-exe installer

```powershell
cd native-stack
.\fetch-vendor.ps1 -IncludePostgres
.\scripts\trim-vendor.ps1
.\build-installer.ps1 -Version 1.0.2
```

→ `dist-client\TallyBridgeServer-Setup-1.0.2.exe`, **68 MB** from 368 MB of payload.
Committed to the branch.

Client double-clicks it → UAC → optionally pastes project URL + service key on one
wizard page → waits. **Their machine needs nothing pre-installed.** Inside:
PostgreSQL, PostgREST, Caddy, WinSW, `node.exe`, the built Express backend, the 17
schema files and all the scripts.

`installer\post-install.ps1` does the work and is a plain script, so it re-runs by hand
as a repair: bootstrap → initdb + roles → schema → register 4 services → start →
optional seed → healthcheck.

**Deliberately not shipped:** `backend\.env` (holds the cloud service key), any Google
credential at all (§4), and the data directory — it lives in
`%ProgramData%\TallyBridge\data` and the uninstaller does not remove it, because after
cutover it is the client's entire books.

**Audited before committing:** the staged payload greps clean for the live
`service_role` key, the Firebase service account, the backend API key and all three
local secrets.

---

## 4. Code change outside the stacks

`backend/src/db/firebase.ts` used to `throw` at module load without
`FIREBASE_SERVICE_ACCOUNT_B64`, and `middleware/auth.ts` imports it — so the backend
refused to start without that variable. On-prem authenticates with `x-api-key` only, so
the sole effect was forcing a **Google service-account private key onto the client's
PC** to let the process boot.

Now lazy: `getFirebaseAuth()` initialises on the first Bearer request.
**Verified with the variable empty** — process starts, `x-api-key` → 200, Bearer → 401,
process survives. Cloud behaviour unchanged.

---

## 5. The traps — every one of these was hit for real

| Trap | What it looks like |
|---|---|
| **`SUPABASE_URL_Client` — capital C** (`sync.ts:20`) | Spell it `_CLIENT` and the MRP report silently serves **cloud** data. No error, no log line, every health check passes. |
| **Stale Supabase key → `404 Company not found`** | Not an auth error. Kong/PostgREST 401s, supabase-js empties the result, the route reports a missing company. Happens after `--rotate` or a reinstall without restarting the backend. |
| **`anon` had ALL grants, no RLS** | Kong was the only gate. Native has no Kong — an unauthenticated `GET` returned the whole stock table, and `anon` held DELETE. Closed by `903_lockdown_anon.sql`; asserted on every health check. |
| **`postgrest.exe` isn't self-contained on Windows** | No `libpq.dll` → exits `0xC0000135` writing **nothing** anywhere. Reads as a corrupt binary. Services inherit no PATH, so the XML states it. |
| **`--` is illegal in an XML comment** | This repo's style uses it as an ASCII em-dash. Made all four WinSW service definitions malformed and unregisterable. `bootstrap.mjs` now validates rendered XML. |
| **`pg_ctl runservice` / `pg_ctl start` under a wrapper** | `runservice` expects the SCM to have launched it; `start` forks and exits so the wrapper marks the service stopped while the DB runs on unmanaged. Use `postgres.exe` directly + `pg_ctl -m fast stop`. |
| **pgcrypto in `public`** | Publishes `armor`, `gen_salt`, `gen_random_uuid` etc. as REST RPCs — parity reported `rpcs live=13 local=19`. Install into an `extensions` schema, as Supabase does. |
| **`supabase_realtime` publication must exist** | `supabase_scan_jobs.sql:53` ALTERs it; stock PostgreSQL has no such publication and the schema apply aborts at file 2. Create it empty. |
| **`syncIngestUrl` needs `/functions/v1/ingest-sync`** | `cloud_pusher.py:224` splits on `/functions/`; a bare REST URL yields `""` and ingest fails with a message that reads like a missing setting. |
| **PowerShell 5.1 reads BOM-less `.ps1` as ANSI** | A UTF-8 em-dash becomes three chars ending in `"`, terminating whatever string it lands in. Keep every `.ps1` 7-bit ASCII. |
| **`-NoNewWindow` in `Start-Process`** | Children share the caller's console: they die with the shell and hold its pipe open, so anything piping the script blocks forever. Use `-WindowStyle Hidden`. |
| **Studio `unhealthy` while working** | Docker sets `HOSTNAME` to the container id and Next.js standalone binds to it, so Studio can't reach itself. Pin `HOSTNAME: "0.0.0.0"`. |
| **`--` … in PowerShell here-strings** | `@"..."@` interpolates `$`, and backslash is not its escape (backtick is). A `$$` plpgsql block written `\$\$` reaches psql literally. Use `@'...'@`. |

---

## 6. Verified numbers (2026-08-07)

Both stacks, identical results:

```
schema parity     live=18 tables / 13 RPCs   local=18 / 13   zero column diffs
data              13,253 stock · 45,076 vouchers · 102,818 voucher_items
                  2,904 ledgers · 517 outstanding · 290,071 rows total
/api/sync/stock   13,253 items
reorder-levels    13,253 scanned · 6,394 priced · 6 need reorder · 2.6s
auth boundary     unauthenticated 401 · anon key 401   (native)
                  kong rejects unauthenticated 401 · pg-meta refuses anon 403  (docker)
```

**Proven to be reading local, not just plausible:** a sentinel row inserted into one
stack only, then returned by the backend — run with **both stacks up at once**, so it
distinguishes them rather than merely showing "some data".

**Installer proven** by staging the payload, copying it to a separate directory on
separate ports, and running the real `post-install.ps1`: bootstrap → initdb → 17 schema
files → start → 13,303 rows seeded from live through the **bundled** node and copy
script. That exercises the build-time import rewrite, which would otherwise fail
silently.

---

## 7. STILL OPEN — read before the client install

1. **Windows service registration has never been run.** It needs administrator and the
   build session had none. `config/services/*.xml` are generated and validated, and
   `services.ps1 -Install` is written, but the step that makes the native stack survive
   a login-less reboot — its entire reason for existing — is unproven. **Run
   `.\install.ps1` elevated on a test machine first.**
2. **Backups.** After cutover the client PC is the only copy of their books. The cloud
   project is an accidental off-site copy today and stops being one. Nothing schedules
   a `pg_dump` of `%ProgramData%\TallyBridge\data`.
3. **The installer is not code-signed.** SmartScreen will warn, and antivirus may
   quarantine `postgrest.exe` or `caddy.exe` **weeks** after a clean install,
   presenting as "reports stopped working". Sign it, or add an AV exclusion at setup.
4. **The backend listens on all interfaces** (Express default on 3001), unlike Postgres
   and Caddy. It requires the API key, but bind it to 127.0.0.1 if nothing off-machine
   calls it.
5. **Parity is REST-surface only.** `schema-parity.mjs` compares tables, columns and
   RPCs — not indexes, constraints, triggers or RLS. A real `pg_dump --schema-only`
   diff is still blocked: the live Postgres password is unavailable (the Dashboard
   account owning `yynuuysvjeipawzfbeme` is inaccessible). Everything here was built
   through the `service_role` key and PostgREST's OpenAPI spec alone.
6. **The 68 MB exe is in git history forever.** Fine for carrying one build to another
   machine; for anything recurring, use a GitHub Release or Git LFS.

---

## 8. Corrections to earlier handouts

- `session_handout_2026-08-06_local_supabase_stack.md` §9 estimated "~1.5 GB instead of
  3–4 GB" for the trimmed Docker set. **It is 4.93 GB**, and 3 GB of that is
  `supabase/postgres` alone. Dropping `analytics` saves runtime memory, not disk.
- That same §9 recommended shipping the Docker compose to the client. **Superseded** —
  the client PC cannot run Docker. Ship `native-stack/`.
- `session_handout_local_windows_stack_design.md` §6 (design v2) is **revived and
  implemented**, with two corrections: the official PostgREST Windows zip is **not**
  self-contained (needs `libpq.dll`), and PostgreSQL's own binaries zip is 848 MB of
  which 673 MB is pgAdmin 4.

---

## 9. Repo layout added this session

```
local-stack-shared/          one schema, one cutover, one set of checks
  schema/90-post/            901_live_drift · 902_grants · 903_lockdown_anon
  scripts/                   schema-sources · env · secrets · copy-live-to-local
                             schema-parity · verify-ingest-path · use-local-desktop
local-supabase/              Docker stack (dev box, Studio)
native-stack/                native Windows services (client)
  installer/                 Inno Setup script + post-install / pre-uninstall
  templates/                 Caddyfile · postgrest.conf · winsw.xml
backend/use-local.ps1        point a shell's backend at the Docker stack
backend/use-native.ps1       ...at the native stack
dist-client/                 the committed 68 MB installer (packages gitignored)
```

Gitignored and therefore **absent on a fresh clone**: `local-supabase/.env`,
`local-supabase/volumes/api/kong.yml`, `native-stack/config/`, `native-stack/vendor/`,
`native-stack/data/`, `native-stack/logs/`.

---

## 10. Commits on this branch

```
b343561  feat(backend): initialise Firebase lazily, add local/native env switchers
87dde85  feat(local-stack): shared schema, cutover and verification layer
99d2bbc  feat(local-supabase): trimmed self-hosted Supabase stack for the dev box
b7bff18  feat(native-stack): Docker-free stack as Windows services, plus single-exe installer
28faf37  chore(dist): commit the 68 MB client installer binary
         docs: this handout
```

**Not pushed.** `env.deployment` was already untracked before this session and was
deliberately left alone.
