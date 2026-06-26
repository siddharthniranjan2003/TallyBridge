# Session Handout — Client Supabase Pause/Resume + voucher_items & push_queue incidents

> For a fresh chat. Covers: why the client Supabase was overloaded, the data-loss incident, how we **paused it server-side** (stubbed the ingest RPCs), the voucher_items date/party_name fix, the push_queue "Push Now" incident, the **current state**, and **exactly how to resume** safely.

---

## 0. TL;DR
- The client's **free** Supabase was being crushed by TallyBridge running a **full re-mirror of all vouchers every minute** (incremental sync is OFF by default).
- Fixing a `statement_timeout` (8s→120s) let that full sync **complete** for the first time → its **full-mode reconciliation deleted ~84,000 historical vouchers** (2014–Mar 2025). Data loss. Source is safe in Tally; no Supabase backup (free tier).
- The relentless full re-mirror then **exhausted the free instance** (timeouts, "exhausting resources" banner, even DDL/connections timing out).
- We **paused sync server-side** (no client access needed) by **stubbing both ingest RPCs to no-ops**. DB is healthy now.
- This is a **HOLDING measure**. To resume: ship the **incremental** app update first, then restore the RPCs. Un-stubbing without that = overload + data-loss returns.

---

## 1. Identifiers / access
- **Client Supabase project:** `ztugwhevemibdrzqafyw` (owner rohan.psom@gmail.com, **FREE**, ap-southeast-2)
  - REST: `https://ztugwhevemibdrzqafyw.supabase.co/rest/v1/`
  - Edge fn (ingest): `https://ztugwhevemibdrzqafyw.supabase.co/functions/v1/ingest-sync`
- **Creds for queries:** `D:\Desktop\TallyBridge\.env_clinet` — holds the client `SUPABASE_URL` + a **service_role** JWT (`eyJ…`). Use it for REST reads (RLS-bypassing). The Supabase **MCP** also connects to this project but drops intermittently; REST via `.env_clinet` is the reliable fallback. DDL (CREATE FUNCTION/TRIGGER) must be run in the **Supabase SQL editor** (REST can't do DDL).
- **Client company:** `K V ENTERPRISES`, `company_id = c47da0b1-b7dd-4589-bb66-73081022c2a3`.
- **App:** client is on **1.2.8** (auto-update works; the UI's "v1.0.0" is a hardcoded label in `Sidebar.tsx`/`About.tsx` — cosmetic, ignore).
- **TEST Supabase (safe sandbox):** `yynuuysvjeipawzfbeme` — NOT the client. Never run test pushes against the client.

---

## 2. Root-cause chain (verified in code + DB)
1. **Incremental sync is OFF by default** — `TB_ENABLE_INCREMENTAL_VOUCHER_SYNC` unset → false (`src/python/sync_main.py:82-85`). So **every** sync runs in **`"full"`** mode covering the whole books range (`from_date = books_from`, `sync_main.py:1057`).
2. Hybrid chunks the full voucher set (`DIRECT_VOUCHER_CHUNK_SIZE = 2000`, `src/python/cloud_pusher.py`) to the edge fn → `tb_ingest_vouchers` RPC.
3. The RPC hit the role's **8s `statement_timeout`** → run aborted partway (~April) → never reached the **final chunk** → never reconciled (so for a long time, no deletion) → **April–June voucher_items never landed**, and the local cache never advanced so it **retried the same failing full sync every minute** → free-tier exhaustion.

This is why the original complaint was "voucher headers current, but `voucher_items` missing from ~April 2026 onward."

---

## 3. The `statement_timeout` fix — and the data-loss it triggered
- We applied (and verified) on **both** RPCs:
  ```sql
  ALTER FUNCTION public.tb_ingest_vouchers(uuid,timestamptz,jsonb,jsonb,jsonb,boolean,jsonb)        SET statement_timeout = '120s';
  ALTER FUNCTION public.tb_ingest_phase3_hybrid(uuid,timestamptz,jsonb,jsonb,jsonb,jsonb,jsonb,jsonb,jsonb) SET statement_timeout = '120s';
  ```
- **Result:** the full sync **completed** → April–June items backfilled (good) **BUT** the full-mode reconciliation ran.
- **Reconciliation bug** (`supabase/migrations/20260428_phase4_voucher_ingest.sql`, lines ~273-282): in `full` mode it does
  `DELETE … WHERE company_id = p_company_id AND synced_at <> v_synced_at` — **no date filter**. Since the full sync only covered **books-from ≈ 2025-04-01 → today**, it **deleted everything older**.
- **Outcome (measured):** `vouchers` went **124,593 → 40,351**; earliest date `2014-04-01 → 2025-04-01`. **~84,000 vouchers (2014 → Mar 2025) deleted.** 2014/2019/2024 = 0 rows.
- **The 8s timeout had been *accidentally protecting* the old data** (sync never finished → reconciliation never ran). Raising it to 120s = the trigger.
- **Not recoverable from Supabase** (free tier = no backups/PITR). Source data is intact in TallyPrime.

---

## 4. The overload crisis
- The per-minute full re-mirror saturated the free instance: "Your project is currently exhausting multiple resources"; table editor wouldn't load; even `CREATE OR REPLACE FUNCTION` and plain reads returned **"Connection terminated due to connection timeout"** (free-tier connection pool exhausted + CPU burst credits depleted).
- Constraints: **can't pause the client app** (no remote access), **can't upgrade** Supabase (budget), **GCP migration rejected** (huge effort, needs client repoint, doesn't fix the per-minute re-mirror — the workload is the problem, not the platform).

---

## 5. HOW WE PAUSED IT (server-side, no client access, reversible)
The heavy DB work is the two ingest RPCs. Stubbing them to no-ops makes every sync cheap → DB relaxes. **push_queue is a separate path (direct INSERT), unaffected.**

### 5a. The stubs (run in SQL editor — must keep the original param DEFAULTS or you get `42P13: cannot remove parameter defaults`)
```sql
-- vouchers
CREATE OR REPLACE FUNCTION public.tb_ingest_vouchers(
  p_company_id uuid, p_synced_at timestamptz DEFAULT now(),
  p_vouchers jsonb DEFAULT '[]'::jsonb, p_sync_meta jsonb DEFAULT '{}'::jsonb,
  p_alter_ids jsonb DEFAULT '{}'::jsonb, p_is_final_chunk boolean DEFAULT false,
  p_record_counts jsonb DEFAULT '{}'::jsonb
) RETURNS jsonb LANGUAGE sql AS $$ SELECT '{}'::jsonb $$;

-- masters (wrapper that calls tb_ingest_masters + tb_ingest_snapshots)
CREATE OR REPLACE FUNCTION public.tb_ingest_phase3_hybrid(
  p_company_id uuid, p_synced_at timestamptz DEFAULT now(),
  p_groups jsonb DEFAULT NULL::jsonb, p_ledgers jsonb DEFAULT NULL::jsonb,
  p_stock_items jsonb DEFAULT NULL::jsonb, p_outstanding jsonb DEFAULT NULL::jsonb,
  p_profit_loss jsonb DEFAULT NULL::jsonb, p_balance_sheet jsonb DEFAULT NULL::jsonb,
  p_trial_balance jsonb DEFAULT NULL::jsonb
) RETURNS jsonb LANGUAGE sql AS $$ SELECT '{}'::jsonb $$;
```

### 5b. Beating the connection/lock contention
The DDL itself timed out under load. Two techniques that worked:
- **Terminate in-flight ingest, then replace** (one block; retry if needed):
  ```sql
  SET lock_timeout = '8s';
  SELECT pg_terminate_backend(pid) FROM pg_stat_activity
   WHERE pid <> pg_backend_pid() AND query ILIKE '%tb_ingest_vouchers%';
  -- (then the CREATE OR REPLACE above)
  ```
- **If it still won't connect: Restart the DB** (Dashboard → Settings → General → **Restart project**), then run the `CREATE OR REPLACE` in the clean ~30–60s window before the per-minute sync re-saturates. (This is how the masters stub finally landed.)

### 5c. Stubbing = SAFE
- No-op only swaps the function body; touches **no data**. Terminating an in-flight RPC rolls it back atomically. Stub also **stops the data-loss reconciliation** from running. Fully reversible (§7).

---

## 6. Other fixes applied this session
### 6a. voucher_items `date` / `party_name` (were NULL on new rows)
Columns exist but the sync never populated them. Fixed with a backfill + a trigger (auto-fills on every future insert, all paths):
```sql
UPDATE voucher_items vi SET date = v.date, party_name = v.party_name
  FROM vouchers v WHERE v.id = vi.voucher_id AND (vi.date IS NULL OR vi.party_name IS NULL);

CREATE OR REPLACE FUNCTION public.tb_fill_voucher_item_meta()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.date IS NULL OR NEW.party_name IS NULL THEN
    SELECT v.date, v.party_name INTO NEW.date, NEW.party_name
    FROM public.vouchers v WHERE v.id = NEW.voucher_id;
  END IF; RETURN NEW;
END; $$;
DROP TRIGGER IF EXISTS trg_fill_voucher_item_meta ON public.voucher_items;
CREATE TRIGGER trg_fill_voucher_item_meta BEFORE INSERT ON public.voucher_items
  FOR EACH ROW EXECUTE FUNCTION public.tb_fill_voucher_item_meta();
```
Verified: 0 rows with NULL date/party; trigger exists.

### 6b. push_queue "Push Now" incident (NOT a bug)
- A balaji **sale OCR** voucher (`SALE-20260608095548`, party BALAJI H/W AGENCIES) reached the client's **live TallyPrime** "with no one pressing push."
- Cause: **the user pressed "Push Now" in the app while connected to the CLIENT Supabase.** "Push Now" = `POST /api/sync/push-queue/activate` → flips `pending`→`push_now`; the poller then delivers `push_now` rows to Tally.
- The flow (verified, `backend/src/routes/sync.ts`): enqueue → `status:"pending"` (2289); activate/"Push Now" → `push_now` (2363); poller `GET /push-queue` returns **`push_now` only** (2410); the desktop poller hits the **backend** endpoint (`cloud_pusher.py:352`), **never** Supabase directly → **it cannot pick up `pending` rows.** Both branches enqueue `pending` (not push_now).
- Tally result for that voucher: `CREATED:1` (voucher created) but `LINEERROR: Stock Item 'ADDISON' does not exist` → status `failed`. **It exists in the client's live Tally, partially.** ⚠️ Client should review/delete it if it was a test.
- **Guardrail:** never press "Push Now" / run test pushes while pointed at the CLIENT Supabase — it delivers to the client's live Tally.

---

## 7. HOW TO RESUME (un-pause) — do in this order
**Do NOT just un-stub.** Restoring the RPCs while incremental is still OFF brings back the per-minute full re-mirror (overload) **and** the full-mode reconciliation (more deletion). Correct order:

1. **Ship the incremental app update** (stops the full re-mirror; makes reconciliation date-scoped/safe):
   - In `src/main/sync-engine.ts`, add `TB_ENABLE_INCREMENTAL_VOUCHER_SYNC: "true"` to the spawn env (main-process change → `npm run build`, **no** `build:python` needed).
   - Bump version → `npm run build` → `npx electron-builder --publish always -c electron-builder.yml` → **un-draft** the GitHub release. Client auto-updates (no machine access).
   - (Optional hardening: also fix the full-mode reconciliation in `20260428_phase4_voucher_ingest.sql` to be date-scoped, so a `full` run can never global-delete again.)
2. **Confirm the client picked up the new version** (sidebar still shows hardcoded v1.0.0 — instead confirm via behavior / `companies.last_synced_at` advancing after un-stub, or check the build version some other way).
3. **Restore the RPCs** (un-stub) by re-running the originals:
   - `supabase/migrations/20260427_phase3_direct_ingest.sql` (restores `tb_ingest_phase3_hybrid` + masters/snapshots)
   - `supabase/migrations/20260428_phase4_voucher_ingest.sql` (restores `tb_ingest_vouchers`)
   - (Re-apply `SET statement_timeout='120s'` on both afterward if you want the headroom — but with incremental ON, payloads are tiny so it matters less.)
4. **Watch** `companies.last_synced_at` advance and the DB stay calm (small incremental payloads, no overload).

---

## 8. CURRENT STATE (verified live)
| Item | State |
|---|---|
| `tb_ingest_vouchers` | **STUBBED** (no-op) |
| `tb_ingest_phase3_hybrid` | **STUBBED** (no-op) |
| `trg_fill_voucher_item_meta` | **EXISTS** |
| voucher_items with NULL date/party | **0** |
| push_queue pending/push_now rows | **0** (nothing queued to auto-push) |
| Supabase health | **Healthy/usable** (sync load = zero) |
| vouchers count / range | **40,351**, 2025-04-01 → present (history pre-2025-04 deleted) |

---

## 9. REMAINING WORK
1. **Resume sync** = ship incremental app update, then restore RPCs (§7). Until then sync is paused (no new data).
2. **Recover deleted history** (2014–Mar 2025, ~84K vouchers) — only from TallyPrime (re-sync that range), and only feasible if the client's Tally still exposes it past books-from; do it on a non-overloaded DB and **after** the reconciliation is made date-safe. No Supabase backup exists.
3. **Fix the reconciliation bug** — full-mode delete has no date filter (latent data-loss). Date-scope it, or rely on incremental (which is date-scoped).
4. **Client Tally cleanup** — review/delete `SALE-20260608095548` (BALAJI, partial create).
5. **Long-term** — free tier is fine *with incremental*; only consider Supabase Pro (backups/PITR) — **not** a GCP migration — if more headroom/safety is wanted.

---

## 10. Quick reference — useful queries (run with `.env_clinet` key or SQL editor)
- RPC stub state: `SELECT proname, CASE WHEN prosrc ILIKE '%SELECT ''{}''::jsonb%' THEN 'STUBBED' ELSE 'ACTIVE' END FROM pg_proc WHERE proname IN ('tb_ingest_vouchers','tb_ingest_phase3_hybrid');`
- DB health ping: `SELECT count(*) FROM companies;` (instant = healthy)
- voucher coverage: `SELECT count(*), min(date), max(date) FROM vouchers;`
- items gap: `SELECT count(*) FROM voucher_items WHERE date IS NULL OR party_name IS NULL;`
- push_queue: `SELECT id, status, created_at, voucher_payload->>'party_name' FROM push_queue ORDER BY created_at DESC LIMIT 10;`
- active load: `SELECT pid, state, now()-query_start, left(query,80) FROM pg_stat_activity WHERE state<>'idle' AND pid<>pg_backend_pid();`

## 11. Critical files
- `src/python/sync_main.py` — `ENABLE_INCREMENTAL_VOUCHER_SYNC` (line 82), full-sync books range (1057), build_sync_plan.
- `src/main/sync-engine.ts` — engine spawn env (add `TB_ENABLE_INCREMENTAL_VOUCHER_SYNC`).
- `src/python/cloud_pusher.py` — `DIRECT_VOUCHER_CHUNK_SIZE`, poller `fetch_pending_push_vouchers` (340), pushes via backend.
- `backend/src/routes/sync.ts` — push_queue enqueue(2289)/activate(2363)/poll(2410); full-mode reconciliation reference.
- `supabase/migrations/20260427_phase3_direct_ingest.sql`, `20260428_phase4_voucher_ingest.sql` — **the originals to restore the stubbed RPCs**.
