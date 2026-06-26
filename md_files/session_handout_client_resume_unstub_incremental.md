# Session Handout — Resuming the Client (un-stub + date-scoped fix + incremental ship)

> For a fresh chat. Covers everything from the last compact: the GCP-migration
> decision, shipping the incremental app update (v1.2.9), the deep analysis of how
> the "pause via RPC stub" actually behaves, the **date-scoped `tb_ingest_vouchers`**
> that simultaneously **un-stubbed + fixed** the client DB, and the **verified live
> result** — sync resumed, gap caught up, BUT the client is still running **full mode
> (v1.2.8)** and re-mirroring the entire books every few minutes.

---

## 0. TL;DR — current state (verified live)

- **Client sync is RESUMED.** `tb_ingest_vouchers` is **un-stubbed + ACTIVE** on the client DB with the data-loss fix baked in. Data is flowing again.
- **Gap caught up:** vouchers `40,351 → 40,518`, `max_date 2026-06-08 → 2026-06-09` (today), last sync `13:12 UTC`. DB healthy/responsive.
- **BUT the client is still on v1.2.8 → `mode: full`.** Every sync re-mirrors the **entire books** (~40.5k vouchers, ~96.5k items, ~107k ledger entries) — two ran 5.5 min apart. It's surviving only because of the **120s statement_timeout** we baked in. This is the same heavy workload that caused the original crisis; it's surviving, not thriving.
- **v1.2.9 (incremental) is published + un-drafted but NOT yet installed** on the client (the `full` mode proves it). It installs on app **restart**.
- **`tb_ingest_phase3_hybrid` (masters) is STILL STUBBED** — only vouchers were un-stubbed.

---

## 1. Identifiers / access (unchanged from prior handouts)

- **CLIENT Supabase:** `ztugwhevemibdrzqafyw` (FREE, ap-southeast-2). Creds in `D:\Desktop\TallyBridge\.env_clinet` (service_role JWT, REST reads bypass RLS). DDL via Supabase SQL editor.
- **Client company:** `K V ENTERPRISES`, `company_id = c47da0b1-b7dd-4589-bb66-73081022c2a3`.
- **TEST Supabase (safe sandbox):** `yynuuysvjeipawzfbeme` — never confuse with client.
- **App release repo:** `siddharthniranjan2003/TallyBridge` (electron-builder publish target, set in `electron-builder.yml` only — NOT package.json).
- **Client app:** was 1.2.8; we published 1.2.9; client still effectively running 1.2.8 until restart.

### Safety constraints (still in force)
- TEST `yynuuysvjeipawzfbeme` is the ONLY DB safe to write test data to.
- **NEVER** inject test vouchers into the client DB / call the RPC with synthetic payloads / point a test sync at the client — that writes phantom rows into the client's live books (the balaji-class mistake).
- `gcloud run deploy`, un-drafting GitHub releases = gated, require explicit per-action authorization.
- Never commit `.env*` files.

---

## 2. The GCP-migration decision (resolved: defer, resume on Supabase first)

User wanted to migrate Supabase → GCP ("no other way"). Conclusion after analysis:

- **The platform was never the bottleneck — the workload is** (per-minute full re-mirror because incremental was OFF). Migrating without fixing the workload just pays GCP to be crushed by the same load.
- **The one legit reason to move = backups/PITR** (the 84k-voucher loss was unrecoverable purely because free Supabase has no backups). Cloud SQL has automated backups + PITR.
- **Ranking for the data-safety goal:** Supabase Pro ($25, zero migration, managed PITR) > Cloud SQL (managed, needs edge-fn folded into backend) > **self-host Supabase on GCE** (most ops, single-VM SPOF, DIY backups — the *worst* option for the actual goal, despite "least code change").
- **Decision:** resume on Supabase now (this session's work); treat GCP migration as a **separate, TEST-first** future effort. Not started.

Supabase surface area mapped (for whenever migration happens): Postgres + RPCs (port directly), **Edge Function `ingest-sync`** (Python direct/hybrid target `SYNC_INGEST_URL`; must fold into Express backend), **PostgREST `/rest/v1`** (route via backend), RLS/JWT (drop, backend-only). Python's `cloud_pusher.py:135-149` shows direct/hybrid posts to the edge fn; render mode posts to the Express backend `/api/sync`.

---

## 3. Step 1 — Shipped the incremental app update (v1.2.9) ✅

**The flag already exists in Python** (`sync_main.py:82-85`, `ENABLE_INCREMENTAL_VOUCHER_SYNC` from `TB_ENABLE_INCREMENTAL_VOUCHER_SYNC`), it was just never set → every sync defaulted to `full`.

**Change made (main-process only):** `src/main/sync-engine.ts:366` — added to the Python spawn env:
```ts
TB_ENABLE_INCREMENTAL_VOUCHER_SYNC: "true",
```
No `build:python` needed (the engine exe already reads the env var; we only changed who *sets* it).

**Shipped:** bumped `package.json` 1.2.8 → **1.2.9** → `npm run build` → `$env:GH_TOKEN=(gh auth token)` → `npx electron-builder --publish always -c electron-builder.yml` (the `-c` is mandatory) → `gh release edit v1.2.9 --repo siddharthniranjan2003/TallyBridge --draft=false`. Verified assets: `latest.yml`, `.exe`, `.blockmap`, `draft:false`. ✅

**How incremental works** (`build_sync_plan`, `sync_main.py:369-435`): when `voucher_changed`, mode defaults to `full` (line 405); flips to `incremental` ONLY if `ENABLE_INCREMENTAL` AND `current_last > cached_last` (line 415) → window = `max(books_from, cached_last − 7 days)` (`VOUCHER_OVERLAP_DAYS=7`, line 417). Incremental reconciliation is **date-scoped** (safe); full was **not** (the bug).

---

## 4. Key analysis — how "pause via stub" actually behaves (critical mental model)

We paused by making the RPCs **return success** (`'{}'`), NOT by stopping the engine. Consequence, traced in code:

- Every cycle: client fetches Tally (ok) → pushes to stubbed RPC → gets HTTP 200 → runs **`save_cached_ids(current_ids)`** (`sync_main.py:1828-1829`), advancing its **local watermark** to current Tally state. The cache only *skips* advancing if the **Tally-side** fetch fails (`voucher_family_skipped`, lines 1497/1609) — nothing to do with our stub.
- So the client's **local cache advanced over data that never landed** in Supabase.
- Change detection compares current Tally vs **local cache** (not vs Supabase). After un-stub: `current == cached` → **`no_changes`** → it does NOT auto-replay the gap. Forward-only.

**Where "the pause point" is recorded:** the stub never ran the final-chunk `UPDATE public.companies` (lines 310-319), so the **`companies` row is frozen at the last real sync** = the pause marker. (Verified: `last_synced_at 2026-06-08 07:25`, `last_voucher_date 2026-06-25`, `alt_vch_id 959558`; `vouchers` `max(date)=06-08`, `count=40,351`.)

**Catch-up:** the 7-day overlap usually heals a small gap on the next voucher change — BUT here the watermark `last_voucher_date` is pinned at **06-25** (a post-dated voucher), so `current_last > cached_last` stays false for any voucher dated ≤06-25 → engine keeps choosing **full**. (This is why it's still full mode even now, and a risk that **full may persist even after 1.2.9 installs** — see §7.)

---

## 5. Step 2+4 combined — the date-scoped `tb_ingest_vouchers` (un-stub + fix) ✅

Because the function was stubbed, one `CREATE OR REPLACE` did **both** jobs: restore the real logic **and** install the fix. Ran on the client SQL editor → `Success` → verified `state = ACTIVE`.

**Two changes vs the original `supabase/migrations/20260428_phase4_voucher_ingest.sql`:**
1. Added `SET statement_timeout = '120s'` to the function definition (survives future replaces; no separate ALTER needed).
2. **Date-scoped the `full` reconciliation branch** — the data-loss bug. Original `full` branch deleted `WHERE company_id=... AND synced_at <> run` with **no date filter** (that's what nuked 84k vouchers when a full run only covered part of the range). Now `full` behaves exactly like `incremental`:
   ```sql
   IF v_voucher_sync_mode = 'full'
     AND v_voucher_from_date IS NOT NULL
     AND v_voucher_to_date IS NOT NULL THEN
     ... WHERE company_id = p_company_id
         AND date >= v_voucher_from_date AND date <= v_voucher_to_date
         AND synced_at <> v_synced_at;   -- bounded, can't global-delete
   ```
   And if `full` but dates are NULL → **no delete at all** (was: global delete).

Everything else byte-identical (upsert ON CONFLICT, items/entries delete-then-reinsert, purchases, companies update, sync_log). Re-running is idempotent + safe. All 7 param defaults kept (avoids `42P13`).

**The full SQL is in chat history** (the big `CREATE OR REPLACE FUNCTION public.tb_ingest_vouchers(...)`). Worth saving as `supabase/migrations/20260609_phase4_voucher_reconcile_datescope.sql` for version control (NOT yet done).

### The ABORT BUTTON (re-stub to no-op) — keep ready
```sql
CREATE OR REPLACE FUNCTION public.tb_ingest_vouchers(
  p_company_id uuid, p_synced_at timestamptz DEFAULT now(),
  p_vouchers jsonb DEFAULT '[]'::jsonb, p_sync_meta jsonb DEFAULT '{}'::jsonb,
  p_alter_ids jsonb DEFAULT '{}'::jsonb, p_is_final_chunk boolean DEFAULT false,
  p_record_counts jsonb DEFAULT '{}'::jsonb
) RETURNS jsonb LANGUAGE sql AS $$ SELECT '{}'::jsonb $$;
```

---

## 6. Verified live result (after un-stub)

`sync_log` (timestamp col is **`synced_at`**, not `created_at`; cols: id, company_id, synced_at, status, records_synced, error_message, sync_meta):

| synced_at (UTC) | status | vouchers | voucher_items | mode |
|---|---|---|---|---|
| 2026-06-09 13:12:27 | success | 40,518 | 96,553 | **full** |
| 2026-06-09 13:06:50 | success | 40,514 | 96,538 | **full** |
| 2026-06-08 07:32:08 | success | 40,351 | 96,147 | full (pre-stub) |

→ Two real client syncs landed **after** the un-stub, both `success`, gap caught up to today, DB responsive. **Mode `full` on both = client still on v1.2.8.** Per-sync it re-mirrors ~40.5k vouchers / ~96.5k items / ~107k ledger entries.

---

## 7. Open items / next steps

1. **Get v1.2.9 installed on the client** — it installs on app **restart** (in-app banner = user-controlled download/install; they're still running the pre-1.2.9 session). After restart, re-check mode:
   ```sql
   SELECT synced_at, status, sync_meta->>'voucher_sync_mode' AS mode
   FROM sync_log WHERE company_id='c47da0b1-b7dd-4589-bb66-73081022c2a3'
   ORDER BY synced_at DESC LIMIT 5;
   ```
   Want `mode = incremental`.
2. **⚠️ Incremental may NOT engage even on 1.2.9** because the watermark `last_voucher_date=06-25` (post-dated voucher) pins `current_last`, so `current_last > cached_last` stays false → engine stays in `full`. **Watch the mode after the update.**
3. **If full mode sticks → ship the chunk-size fix (the real mitigation):** `src/python/cloud_pusher.py` `DIRECT_VOUCHER_CHUNK_SIZE = 2000 → 250`. This IS a Python change → needs `build:python` + version bump (1.3.0) + publish + un-draft. Smaller chunks each finish well under 120s, so full re-mirrors complete safely regardless of incremental.
4. **Masters still paused** — `tb_ingest_phase3_hybrid` is still a no-op stub. Un-stub it (re-run `supabase/migrations/20260427_phase3_direct_ingest.sql`) when ready for ledgers/stock to flow again. Note `records_synced` shows `groups:0, ledgers:0` because masters are stubbed (stock count moves because stock rides a different path / or is stale).
5. **Save the date-scoped function as a migration file** for version control (§5).
6. **Don't forget the GCP migration is still a deferred, TEST-first future effort** (§2).
7. **Recovery of deleted 2014–Mar 2025 history** (84k) — user said they don't care; only re-syncable from Tally on a calm DB after reconciliation is date-safe (it now is). Optional.

---

## 8. Monitoring queries

```sql
-- mode + counts of recent syncs
SELECT synced_at, status, records_synced, sync_meta->>'voucher_sync_mode' AS mode
FROM sync_log WHERE company_id='c47da0b1-b7dd-4589-bb66-73081022c2a3'
ORDER BY synced_at DESC LIMIT 5;

-- progress (baseline: 40,351 / 2026-06-08)
SELECT count(*) total, max(date) max_date, max(synced_at) last_sync
FROM vouchers WHERE company_id='c47da0b1-b7dd-4589-bb66-73081022c2a3';

-- live load (abort signal: anything stuck >120s, or "exhausting resources" banner)
SELECT pid, state, now()-query_start running_for, left(query,60)
FROM pg_stat_activity WHERE state<>'idle' AND pid<>pg_backend_pid();

-- stub/active state
SELECT proname, CASE WHEN prosrc ILIKE '%SELECT ''{}''::jsonb%' THEN 'STUBBED' ELSE 'ACTIVE' END state
FROM pg_proc WHERE proname IN ('tb_ingest_vouchers','tb_ingest_phase3_hybrid');
```

---

## 9. Critical files

| Item | Location |
|---|---|
| Incremental flag (shipped) | `src/main/sync-engine.ts:366` (`TB_ENABLE_INCREMENTAL_VOUCHER_SYNC: "true"`) |
| Incremental read + plan | `src/python/sync_main.py:82-85`, `build_sync_plan` 369-435, `VOUCHER_OVERLAP_DAYS` 129, cache write 1828-1829 |
| Chunk size (fallback fix) | `src/python/cloud_pusher.py` `DIRECT_VOUCHER_CHUNK_SIZE` (line 39) → 250 |
| Ingest transport (edge vs backend) | `src/python/cloud_pusher.py:135-160` |
| Voucher RPC (un-stubbed + fixed) | client DB `tb_ingest_vouchers`; source `supabase/migrations/20260428_phase4_voucher_ingest.sql` |
| Masters RPC (still stubbed) | client DB `tb_ingest_phase3_hybrid`; restore `supabase/migrations/20260427_phase3_direct_ingest.sql` |
| Publish config | `electron-builder.yml` (publish: github siddharthniranjan2003/TallyBridge) |
| Prior handouts | `session_handout_supabase_pause_resume.md`, `session_handout_pushqueue_immediate_push_rootcause.md` |

---

## 10. One-line summary for the next chat

> Client voucher sync is **resumed** — `tb_ingest_vouchers` un-stubbed on the client DB
> with a **date-scoped `full` reconciliation** (kills the 84k-delete bug) + baked-in 120s
> timeout. Gap caught up (max_date=today, DB healthy). BUT the client is **still v1.2.8 →
> full mode**, re-mirroring all ~40.5k vouchers every few minutes; v1.2.9 (incremental) is
> published but installs on **restart**, and may not even flip to incremental due to a
> post-dated 06-25 watermark — in which case ship `DIRECT_VOUCHER_CHUNK_SIZE=250` (1.3.0).
> Masters (`tb_ingest_phase3_hybrid`) still stubbed. Abort button = re-stub to no-op.
