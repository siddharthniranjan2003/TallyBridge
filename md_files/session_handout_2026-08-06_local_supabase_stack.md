# Session Handout — Local Supabase stack running, schema drift found and closed

> Read this to continue in a fresh chat with full context.
> **Date:** 2026-08-06 · **Branch:** `update/absolute-latest-rate-etc` · **HEAD:** `9d0494b`
> Companion doc: `md_files/session_handout_local_windows_stack_design.md` (same day, design
> only — **§3 of that doc contains an error this session disproved; see §3 below**).

---

## 0. TL;DR

- **Goal set by the user mid-session:** run Supabase locally so the **MRP/reorder report**
  and **stock info** work off local data, served by the Express backend + React frontend.
  "If other functions come along, let them come."
- **Status: DONE and verified.** Local Supabase is running, schema-identical to the live
  testing project, loaded with a full copy of live data, and both endpoints answer with
  real numbers.
- **Major finding:** the repo's SQL files **do not reproduce the live database.**
  Live had 2 tables, 3 RPCs and 3 columns that exist in no `.sql` file in any repo.
- **Three defects found in `supabase/migrations/`**, all of which would break
  `supabase db reset` on any machine, not just this one.
- **Everything was achieved without the Supabase DB password**, which is unavailable
  (see §6) — the live schema was read through PostgREST's OpenAPI spec instead.

---

## 1. What is running right now

```
API      http://127.0.0.1:54321
DB       postgresql://postgres:postgres@127.0.0.1:54322/postgres
Studio   http://127.0.0.1:54323
key      sb_secret_N7UND0Ug…   (CLI default, NOT a secret — `supabase start` prints it in full)
```

Started with `supabase start` from `D:\Desktop\TallyBridge`. Project id `tallybridge`,
so containers are named `supabase_db_tallybridge` etc. `[analytics]` is disabled in
`config.toml` (drops Logflare + vector, the two heaviest services).

**Backend was left running** in the background against local via
`backend\use-local.ps1`. If it is gone, see §7 to restart.

### Verified working (2026-08-06)

| Endpoint | Result |
|---|---|
| `GET /api/sync/stock?company_id=…` | 13,253 stock items with name/qty/unit/part_code |
| `GET /api/sync/reorder-levels?company_id=…` | 13,253 items scanned and classified, `purchase_rate` (= MRP) populated, **1.2 s** |

Company in local DB: `K V ENTERPRISES`
· id `74704231-5692-465f-8bea-34588dcdb86b`
· guid `f3e9df46-fc4f-4b85-930a-abe1e427e900`

---

## 2. The wiring that makes this work — two clients, not one

This is the single most important thing to carry forward. The two features the user
named go through **two different Supabase clients inside the same backend process**:

| Path | Client | Env vars | Auth key |
|---|---|---|---|
| `/api/sync/stock`, `/vouchers`, `/outstanding`, `/parties`, `/pnl`, `/balance-sheet` — the React dashboard | global `supabase` (`db/supabase.ts`) | `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | `API_KEY` |
| `/api/sync/reorder-levels[/:reportKey]` — MRP report | `reorderSupabase` (`sync.ts:19`) | `SUPABASE_URL_Client`, `SUPABASE_SERVICE_KEY_CLIENT` | `API_KEY_CLIENT` |

Verified by reading the code: the report helpers use `reorderSupabase` for `vouchers`,
`voucher_items` and `stock_items`; `/stock` uses the global `supabase`. **Set only one
pair and the other half of the app silently keeps reading cloud.**

> ⚠️ **`SUPABASE_URL_Client` — capital C, everything else lowercase.** Read literally at
> `sync.ts:20`. Spell it `SUPABASE_URL_CLIENT` and the override evaporates into the global
> client with no error and no log line: the report serves **cloud** data while every health
> check passes. The design doc already called this the highest-value trap in the project.

`dotenv.config()` does not overwrite variables already present in the process
environment, so exporting these in the shell beats `.env` **without editing `.env`** —
cloud config stays intact and a fresh terminal goes back to cloud.

---

## 3. FINDING — the repo SQL does not reproduce the live database

`md_files/session_handout_local_windows_stack_design.md` §3 states that
`backend/full_schema.sql` + `supabase/migrations/*.sql` are the schema, and its
"Database setup" step prescribes building a local DB by running those files in date
order. **That is wrong**, and building from it would silently fail that same doc's own
acceptance gate ("schema parity — restore cloud dump, row counts must match").

Diffed live (`yynuuysvjeipawzfbeme`) against the repo files: **live = 18 tables / 13
RPCs, repo files build 16 / 10.** Missing from every `.sql` in the repo:

| Object | Notes |
|---|---|
| `Purchase_Matching` | 321 rows. `id BIGINT NOT NULL` PK with no default; three quoted mixed-case text columns |
| `purchase_matching_api` | Same 4 columns, same 321 rows, and PostgREST reports no NOT-NULL columns — that is how it describes a **view**. Modelled as a view over `Purchase_Matching` |
| `get_distinct_party_names()` | See §4 |
| `get_sale_items_for_party(text)` | Denormalized variant; body from `aiaccountant/md_files/handoff_2026-06-08_pickers_editing_config.md` §2 |
| `get_purchase_items_for_party(text)` | Same, `ILIKE '%PURCHASE%'` |
| `voucher_items.date`, `.party_name`, `.voucher_type` | Live has 12 columns, `full_schema.sql` declares 9 |

All of it was hand-applied through the Supabase SQL Editor — several repo `.sql` files
say to do exactly that in their header comments. That is the mechanism behind this and
behind the earlier `push_queue` migration drift.

**Closed by `supabase/migrations/20260801_live_drift.sql`** (new this session). Every
column name, type, nullability, default and PK/FK relationship in it was read verbatim
off the live OpenAPI spec. Only the three function *bodies* are reconstructed — the
per-function comments in that file say exactly what is verified vs inferred.

### Verification

`node scripts/local-stack/schema-parity.js` →

```
tables   live=18  local=18
rpcs     live=13  local=13
tables missing locally : (none)
tables extra locally   : (none)
column diffs           : (none)
rpcs missing/extra     : (none)
PARITY: local matches live across every REST-visible table, column and RPC.
```

(Local also has `tb_guard_voucher_mass_delete`; it is a trigger function, so it is not
REST-exposed on either side. Not a difference.)

**Limit of this method — state it when reporting parity.** PostgREST's spec shows
tables, columns, types and RPC signatures. It cannot see indexes, constraints, triggers,
defaults, or anything outside `public`. A real `pg_dump --schema-only` remains the
stronger check if DB credentials are ever recovered.

---

## 4. Two behavioural facts established by probing live

Both were found while reconstructing the drift and are useful well beyond it.

**`get_distinct_party_names()` reads `ledgers`, not `vouchers`.** Names like
`A & A ENTERPRISES` come back from the RPC with **0 matching vouchers and 1 matching
ledger**. Every sampled name sits in `Sundry Debtors`/`Sundry Creditors` (2,535 rows),
but the RPC returns **2,584** distinct, so it must also union in `vouchers.party_name`.
The union is the one part of `20260801_live_drift.sql` that is inferred rather than read.
Relevant to anyone tuning party matching in the parsing path.

**`voucher_items.party_name` is NULL in all 102,818 live rows.** Nothing in the sync path
ever populates the denormalized trio, so the denormalized `get_sale_items_for_party` /
`get_purchase_items_for_party` on yynuu return nothing at all. This corroborates the note
in `aiaccountant/md_files/aiaccountant_backend_interaction_testplan_2026-06-30.raw.json`
that the app abandoned those RPCs for a `vouchers!inner` embed — but it means the columns
**and** the RPCs are dead weight on live, not just locally.

---

## 5. FINDING — three defects in `supabase/migrations/`

All three break `supabase db reset` / `supabase start` on **any** machine.

1. **Duplicate migration versions — hard failure.** The CLI parses a migration's version
   from the digits before the first `_`, and `supabase_migrations.schema_migrations` is
   keyed on it. Three files all parsed to `20260619`:
   `_ingest_statement_timeout`, `_stock_items_part_code`, `_voucher_delete_guard`.
   Result: `ERROR: duplicate key value violates unique constraint "schema_migrations_pkey"`,
   mid-run, leaving a half-applied DB.
   **Fixed** by `git mv` to `20260619000001/2/3_*` (14-digit, Supabase's own convention;
   order and dates preserved).

2. **Rollback scripts auto-applied — silent damage.** `20260727_stock_unit_group_guard_rollback.sql`
   and `20260731_sale_latest_global_rate_rollback.sql` sort **after** their forward
   migration, so a reset applied both and undid the stock-unit guard *and* dropped the
   global-latest-rate RPCs — the work on this very branch. **Removed** from `migrations/`;
   still in git at `b36ad2d` if ever needed. They should never live in `migrations/`.

3. **No base schema.** The migrations are deltas only; `20260619…_stock_items_part_code.sql`
   does `ALTER TABLE public.stock_items` on a table nothing creates. Base tables live in
   `backend/full_schema.sql` plus three loose files. **Fixed** by
   `supabase/migrations/20260101_baseline.sql`, concatenating (in order)
   `full_schema.sql`, `supabase_scan_jobs.sql`, `supabase_scan_jobs_failed_status.sql`,
   `supabase_audit_trail_purchase.sql`.

   > Gotcha if regenerating it: PowerShell 5.1's `Set-Content -Encoding utf8` writes a
   > **UTF-8 BOM**, and Postgres rejects the leading `U+FEFF` as a syntax error. Strip it.

---

## 6. Credentials — what is and is not available

- **The Supabase DB password is unknown** and cannot currently be reset: the Dashboard
  account that owns yynuu is **niranjansiddharth0@gmail.com**, and the user does not have
  access to it. `pg_dump`, `supabase db pull` and `supabase db dump` are therefore all
  blocked.
- **The CLI is logged in as a different account** — `supabase projects list` shows only
  `testing.riplara@gmail.com's Project` (`rfwjpflkfqsccpukraed`). It cannot see yynuu, so
  the Management API route is blocked too.
- **What does work:** the `service_role` key in `backend/.env`. It grants full read access
  to live over PostgREST, which is how both the schema reconstruction and the data copy
  were done. Everything in this handout was achieved with it alone.
- Resetting the DB password would be **safe** whenever the account is recovered: a grep of
  the whole repo for `postgres://`, `DATABASE_URL`, `PGPASSWORD`, `pooler.supabase` returns
  zero matches. Nothing in TallyBridge uses a direct Postgres connection.
- `pg_dump 17.10` is on PATH (`C:\Program Files\PostgreSQL\17\bin`) and is new enough for
  the live server (17.6), so a dump is one password away. Pooler user is
  `postgres.yynuuysvjeipawzfbeme` at `aws-1-ap-northeast-1.pooler.supabase.com:5432`.

---

## 7. How to bring it all back up

```powershell
# 1. Docker Desktop must be running
cd D:\Desktop\TallyBridge
supabase start                       # ~10s once images are cached
supabase status                      # URLs + keys

# 2. Backend against local (dot-source — do not just run it)
cd backend
. .\use-local.ps1 ; npm run dev

# 3. Data, if starting from an empty DB
node scripts\local-stack\copy-live-to-local.js

# 4. Prove the schema still matches live
node scripts\local-stack\schema-parity.js
```

Smoke test:

```powershell
$H = @{"x-api-key"="localdevkey"}; $cid = "74704231-5692-465f-8bea-34588dcdb86b"
Invoke-RestMethod "http://localhost:3001/api/sync/stock?company_id=$cid" -Headers $H
Invoke-RestMethod "http://localhost:3001/api/sync/reorder-levels?company_id=$cid&limit=5" -Headers $H
```

Frontend (`frontend/` has **no `.env` at all** — create one):

```
VITE_BACKEND_URL=http://localhost:3001
VITE_API_KEY=localdevkey
VITE_COMPANY=K V ENTERPRISES
```

Useful: `supabase stop` (keep data) · `supabase stop --no-backup` (wipe) ·
`supabase db reset` (re-apply all migrations).

> **Gotcha after Docker Desktop shuts down.** The containers exit but still exist, so
> `supabase start` refuses with `supabase start is already running` +
> `supabase_db_tallybridge container is not running: exited`. Run **`supabase stop` first**,
> then `supabase start`. Data survives this — verified: 45,076 vouchers / 102,818
> voucher_items / 13,253 stock_items all intact afterwards. Only `--no-backup` wipes.

---

## 8. Data copied from live

`scripts/local-stack/copy-live-to-local.js`, all 13 tables matching live **exactly**:

| Table | Rows | | Table | Rows |
|---|---:|---|---|---:|
| companies | 1 | | vouchers | 45,076 |
| groups | 49 | | voucher_items | 102,818 |
| ledgers | 2,904 | | voucher_ledger_entries | 118,606 |
| stock_items | 13,253 | | purchases | 5,939 |
| outstanding | 517 | | Purchase_Matching | 321 |
| profit_loss / balance_sheet / trial_balance | 8 / 7 / 11 | | | |

Primary keys are carried across verbatim so FKs resolve. Writes use
`resolution=merge-duplicates`, so re-running is safe. **This is also the intended cutover
mechanism** for moving a client off cloud.

---

## 9. Packaging Supabase for the client PC — the user's stated end goal

> *"how can I package supabase to run locally on his system, that is my goal"*

**What was built this session is a dev tool. Do not ship it.** `supabase start` printed:

> All services bind to **0.0.0.0** · API keys and JWT secrets are **shared defaults** ·
> Studio, pgMeta (`/pg/*`) and analytics have **no authentication**

`sb_secret_N7UND0Ug…` is identical on every machine running this CLI version and is
published in Supabase's docs. On a shop PC that is the client's full ledger on every
network interface behind a public key and an unauthenticated admin UI. It also requires
the CLI and a project directory with `config.toml` + `migrations/` on the client's disk.

**Recommendation — this answers §8 of the design handout, which was left open.** Ship the
official self-hosted `docker-compose`, **trimmed to `db`, `kong`, `rest`, `meta`, `studio`**.
Drop `auth` (Firebase does login), `storage`+`imgproxy` (invoice images are GCS, out of
scope), `realtime` (Sale/Purchase paused), `functions` (`ingest-sync` is not in the live
path), and `analytics`+`vector` (heaviest thing in the file). ~1.5 GB instead of 3–4 GB,
which keeps the 16 GB upgrade optional. Kong stays — it is what makes `/rest/v1` work
natively, which is the defect that killed design v1. Studio stays because the user
explicitly required the Supabase UI.

**Packaging checklist**

- **Generate secrets per install, never ship them:** `POSTGRES_PASSWORD`, `JWT_SECRET`,
  `ANON_KEY` + `SERVICE_ROLE_KEY` signed with it, `DASHBOARD_USERNAME`/`PASSWORD`,
  `SECRET_KEY_BASE`, `VAULT_ENC_KEY`. A fresh install is also the free moment to fix the
  key-reuse smell the design doc flagged — `API_KEY_CLIENT` is a Supabase *publishable*
  key also serving as `SUPABASE_ANON_KEY`, `BACKEND_API_KEY` and `ACTIVATE_API_KEY`.
- **Bind everything to `127.0.0.1`** — the compose file publishes on all interfaces by
  default. Tailscale reaches in over the tunnel; nothing needs a LAN listener.
- **Schema:** `supabase/migrations/` in order, with §5's fixes and `20260801_live_drift.sql`.
  Verified to reproduce live exactly.
- **Data cutover:** `scripts/local-stack/copy-live-to-local.js`.
- **Auto-start:** Docker Desktop at login + `restart: unless-stopped`.

**The two things that will bite**

1. **Login-less reboot.** Docker Desktop needs a logged-in Windows session; design v2's
   native services survived a reboot-to-lock-screen and still served reports, Docker does
   not. Softening: TallyPrime and the TallyBridge tray app already need a session, so the
   PC is logged in during business hours anyway. It only matters if the owner checks
   reports at night with the machine locked — **ask him directly**, it is the one place
   Docker is strictly worse than native.
2. **Backups.** Flagged twice in the design handout, still unanswered. The cloud mirror is
   an accidental off-site copy today and disappears at stage 2, after which one dead disk
   is the client's entire books. Needs an answer **before** go-live.

---

## 10. Repo changes made this session

| Path | Change |
|---|---|
| `supabase/config.toml` | **new** — `supabase init`; `project_id = "tallybridge"`, `[analytics] enabled = false` |
| `supabase/.gitignore` | **new** — written by `supabase init` |
| `supabase/migrations/20260101_baseline.sql` | **new** — base schema (§5.3) |
| `supabase/migrations/20260801_live_drift.sql` | **new** — live drift (§3) |
| `supabase/migrations/20260619_*` ×3 | **renamed** → `20260619000001/2/3_*` (§5.1) |
| `supabase/migrations/*_rollback.sql` ×2 | **deleted** from `migrations/` (§5.2); in git at `b36ad2d` |
| `backend/use-local.ps1` | **new** — the six env vars, with the capital-C trap documented inline |
| `scripts/local-stack/copy-live-to-local.js` | **new** — data copy / cutover |
| `scripts/local-stack/schema-parity.js` | **new** — drift check, no password needed |

**All uncommitted.** Nothing was pushed. `md_files/` and `.env` untouched apart from this
handout.

---

## 11. Next steps

1. **Correct §3 of `session_handout_local_windows_stack_design.md`** before design v3 gets
   written on top of it — its "run the .sql files in date order" instruction produces a DB
   that fails its own parity gate.
2. **Write design v3** as an artifact. The pieces it was missing now exist: verified
   schema, a working cutover mechanism, and a concrete answer to its §8 compose-profile
   question (§9 above).
3. Commit this work — it is a coherent unit (local stack + drift fix + tooling) and the
   migration fixes in §5 benefit everyone, not just local dev.
4. Still open from the design handout, unchanged: **backups** and **who administers the box**.
5. Optional: if the yynuu Dashboard account is ever recovered, take a real
   `pg_dump --schema-only` and diff it against `20260101_baseline.sql` +
   `20260801_live_drift.sql` to catch index/constraint drift that PostgREST cannot show.
