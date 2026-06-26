# Handout — Client backend cutover, the client's own Supabase resource crisis (+ stub), and graphify removal

> Continuation handout for a fresh chat. Covers everything since the last compact:
> (1) whether/how to point the **client** TallyBridge at the GCP backend to fix the
> auto-push bug, (2) the discovery of a **third** Supabase — the client's own,
> free-tier, resource-exhausted — and the DB-level **stub** applied to it, plus the
> plan/compute/disk-IO upgrade saga, and (3) the **removal of the graphify skill**
> from the repo and globally.

---

## 0. TL;DR / current live state

- **There are THREE Supabase projects in play** (this is the biggest new fact):
  - **`yynuu` (`yynuuysvjeipawzfbeme`)** = the GCP test/dev pipeline DB ("my supabase" to the developer). The GCP Cloud Run backend + parsing both point here.
  - **`ztugw` (`ztugwhevemibdrzqafyw`)** = secondary; only `*_CLIENT` env / `.env_clinet` / `/reorder-levels`. Anon key.
  - **`rohan.psom@gmail.com`'s Project** = **the actual CLIENT's own Supabase.** AWS `ap-southeast-2`. This is what the client's **Render** backend writes to. **NEW — not previously identified.** ⚠️ This means "production client data" likely lives **here**, not on `yynuu`. Revisit the `supabase-db-identity` memory with this in mind.
- **The GCP backend that fixes the auto-push bug:** `https://tallybridge-backend-xx3yz3b3kq-el.a.run.app` (alias `https://tallybridge-backend-950406969086.asia-south1.run.app`). Serving revision **`tallybridge-backend-00021-854`**, has the `push_now` gate (commit `feeb928`), `/health` = 200, points at **`yynuu`**.
- **A DB-level stub is currently ACTIVE on the client's (rohan.psom) `push_queue`** — trigger `tb_stub_push_queue_trg` forces every insert to `status='stubbed'`, so the desktop poller never serves it → **nothing pushes to Tally.** This is the safety brake. The user tried to `DROP` it but the DROP **hung on a lock** (see §3). Current state: **likely still applied** unless the DROP eventually succeeded.
- **The client's Supabase was upgraded** Free → **Pro**, compute **Nano → Micro**. After that a new warning appeared: **"depleting its Disk IO Budget"** (disk-I/O throttling, not a hard block).
- **graphify removed:** repo artifacts + dependency staged for deletion (not committed); global skill dir deleted from `~/.claude/skills/`.

---

## 1. The client backend cutover (fixing the auto-push bug)

### The bug (from the prior handout, recap)
The client's **Render** backend is the pre-`feeb928` version: `GET /push-queue` serves `status="pending"` rows **directly** to the desktop poller → any insert (e.g. an OCR parsing run) pushes to live Tally within ~5s, **no human press required.** The `push_now` activation gate exists only on the refactor-branch backend (`feeb928`, 2026-05-20).

### The proposed fix: repoint the client to the GCP backend
- **URL to enter in the client's Control Panel:** `https://tallybridge-backend-xx3yz3b3kq-el.a.run.app`
  - **Base URL only — no trailing slash, no `/api/...`.** The app appends routes (`{BACKEND_URL}/api/sync/push-queue`).
  - Verified canonical via `gcloud run services describe tallybridge-backend`; `/health` = 200.
- **Why it fixes it:** revision `00021-854` is refactor-HEAD code → `GET /push-queue` filters `status="push_now"`; inserts land as `pending` and sit until something calls `/push-queue/activate`.
- **How the client app resolves its backend URL:** `controlPlaneUrl` (fallback legacy `backendUrl`) in **electron-store** (`tallybridge-config.json` in the app's userData dir). It's **runtime-configurable** via the Settings UI → `save-settings` IPC (`src/main/ipc-handlers.ts:524`), which writes both `controlPlaneUrl`/`controlPlaneApiKey` and the legacy `backendUrl`/`apiKey`. The poller (`push-queue-poller.ts`) uses `controlPlaneUrl` → `BACKEND_URL`. **No new app build needed** to repoint.

### ⚠️ The critical gap discovered: nothing in the client app activates rows
- Repo-wide search: **`/push-queue/activate` / `push_now` / `pushNow` appear ONLY in `backend/src/routes/sync.ts`.** Nothing in the desktop app, the Python engine, or the frontend ever calls activate.
- **Consequence:** if you point the client at the gated backend, inserts become `pending` and **nothing ever flips them to `push_now` → no voucher ever reaches Tally.** The gate is half-built (backend enforces, no client trigger).
- **User's resolution:** they said *"this [the GCP backend] does the activate"* — i.e. an **external/admin dashboard** (not in this repo) calls `/push-queue/activate`. **Verify that dashboard exists and uses the matching API key** before relying on it.

### Parsing service already targets the same backend
- `tallybridge-parsing` env `MINICPM_PUSH_QUEUE_URL` = `https://tallybridge-backend-950406969086.asia-south1.run.app/api/sync/push-queue?company_name=K+V+ENTERPRISES` — a **URL alias of the same `tallybridge-backend` service**. So OCR enqueues land in the exact queue the client polls. No change needed there.

### Pre-deploy checklist (before flipping the client)
1. **API key must match in 4 places** or 401: client `controlPlaneApiKey` == backend `API_KEY` == parsing `MINICPM_PUSH_QUEUE_API_KEY` == the activate dashboard's key. (On `yynuu` these are the `c2Co…` value.)
2. **One Supabase across everything:** backend `SUPABASE_URL`, parsing Supabase, the activate dashboard, **and the `vouchers` table the duplicacy check reads** must all be the same DB. If repointing env to the client's own DB, repoint **all four together**, not just the backend.
3. **`company_name` must match exactly.** Parsing enqueues `company_name=K V ENTERPRISES`; the client's configured company + the company-filtered `GET /push-queue` must match, or the poller **silently fetches nothing**. (Most likely silent failure.)
4. **Clean the target `push_queue` first** — no row left in `push_now`, or it pushes on the first poll within ~5s. (Leftover `pending` is safe — gated.)
5. **Smoke test on the real client config:** OCR-run one invoice → confirm row appears `pending` and does NOT reach Tally → activate via dashboard → confirm it pushes.
6. **Never** change the backend URL *and* drop the DB stub at the same time on-site; do it in steps watching one test voucher.

**Recommendation given:** the cutover has too many moving parts to do safely while standing at the client; the stub already makes the client safe — do the proper cutover remotely after the 4-way alignment is confirmed.

---

## 2. The client's own Supabase (rohan.psom) — resource crisis

### What it is
A **third** Supabase project = the client's actual production DB. AWS `ap-southeast-2`. Started on **Free** tier. Table editor showed `public.vouchers` (duplicacy check reads `vouchers` ✓). **Unconfirmed whether `public.push_queue` exists here** — must verify; no `push_queue` table = backend enqueue fails regardless of resources.

### The warnings, in order
1. **"Your project is currently exhausting multiple resources, and its performance is affected — Upgrade your compute…"** → generic resource exhaustion. Rule given: *disk-size* exhaustion → project goes **read-only** (writes/inserts FAIL); *compute/egress/connections* exhaustion → writes still work, just slow. "Check usage" tells which.
2. User **upgraded Free → Pro** — but banner persisted, because **Pro alone keeps the same tiny compute instance**; you must separately buy a **compute add-on**.
3. User **resized compute Nano → Micro** (DB restarts ~1–2 min, "Project is resizing"). ⚠️ Warned: verify it doesn't land back on **Nano**.
4. After Micro, new warning: **"Project is depleting its Disk IO Budget."** = AWS gp3 **baseline IOPS + burst budget** model; workload (heavy sync upserts + poller + likely missing indexes) outruns the baseline, burning the burst budget; when depleted, I/O throttles to baseline → everything crawls. **Not a hard block.**

### Fixes given for the Disk-IO-budget warning (pick by permanence)
1. **Targeted:** Settings → Compute and Disk → **Disk** → raise **IOPS/throughput** (gp3 lets you provision IOPS independent of compute). Cheapest for an I/O-bound workload.
2. **Bump compute** Small/Medium (higher IOPS baseline too).
3. **Reduce I/O load** (cheapest/durable): add missing indexes on hot tables (the "expensive queries" the banner flagged), lower sync frequency, and **stop the self-inflicted I/O** from the hung DROP + poller retries.
- **Recommendation:** let it settle first (cancel hung query, pause poller, watch if budget refills); if still depleting under normal load, bump Disk IOPS; the durable fix is indexes, not a bigger instance.

---

## 3. The stub applied to the client's push_queue (ACTIVE — handle on next session)

### What was applied
A **BEFORE INSERT trigger** that parks every new `push_queue` row so the poller (which serves only `pending`/`push_now`) never sees it → **no push to Tally**, while inserts still succeed (parsing/backend get success):

```sql
create or replace function tb_stub_push_queue()
returns trigger language plpgsql as $$
begin
  new.status := 'stubbed';
  return new;
end;
$$;
drop trigger if exists tb_stub_push_queue_trg on public.push_queue;
create trigger tb_stub_push_queue_trg
  before insert on public.push_queue
  for each row execute function tb_stub_push_queue();
```
(Ran clean → `status` column is plain text, accepted `'stubbed'`. If it were an enum, would need an existing value like `'failed'`.)

Optional companion to park already-armed rows (the trigger is INSERT-only):
```sql
update public.push_queue set status='stubbed' where status in ('pending','push_now');
```

### Undo (the user attempted this — it HUNG)
```sql
drop trigger if exists tb_stub_push_queue_trg on public.push_queue;
drop function if exists tb_stub_push_queue();
```
**The DROP hung on "Running…"** — diagnosed as a **lock**, not compute: `DROP TRIGGER` needs ACCESS EXCLUSIVE on `push_queue`, blocked by the poller hitting the table every ~5s (or an idle-in-transaction). Fix given:
```sql
set lock_timeout = '3s';
drop trigger if exists tb_stub_push_queue_trg on public.push_queue;
drop function if exists tb_stub_push_queue();
```
If "could not obtain lock" → **pause the TallyBridge sync/poller in the app** so `push_queue` goes quiet, then DROP succeeds instantly. To see the blocker:
```sql
select pid, state, wait_event, now()-xact_start as age, left(query,60) q
from pg_stat_activity
where datname=current_database() and pid<>pg_backend_pid()
order by xact_start nulls last;
```

### ⚠️ State to confirm next session
- **Is `tb_stub_push_queue_trg` still present?** (DROP may not have completed.) Check:
  ```sql
  select tgname from pg_trigger
  where tgrelid='public.push_queue'::regclass and not tgisinternal;
  ```
- **Reminder:** dropping the stub while the client is still on the **old Render backend** re-arms the auto-push bug — any insert pushes to Tally in ~5s. Only drop it once cut over to the gated backend (or you intend pushes to flow).

### (Not applied) upload-stub alternative
The user briefly considered stubbing the **upload** (Tally→Supabase ingest) instead of the push path, to relieve the strained DB. Recommended the cleaner lever = **pause sync in the app** (real resource relief at the source; SQL `DO INSTEAD NOTHING` rules on ingest tables don't reduce compute and risk silent data loss). Ingest tables identified: `vouchers, voucher_items, stock_items, purchases, ledgers, outstanding, profit_loss, balance_sheet, trial_balance` (push_queue excluded). **They upgraded the plan instead — these rules were NOT applied** (verify none exist if in doubt).

---

## 4. graphify skill removal (DONE — staged, not committed)

### Repo (branch `TallyBridge-Backend-Refactor`)
- **Staged for deletion:** `src/graphify-out/` — 39 generated AST-cache JSON files (were already deleted from disk; staged the deletions).
- **Removed dependency:** `graphifyy==0.8.5` from `src/python/requirements.txt` — **nothing imports it** (`git grep` clean); it was added accidentally in commit `feeb928` and only bloated the PyInstaller bundle. requirements.txt now: `requests`, `xmltodict`, `python-dotenv`.
- **`.gitignore`:** appended `src/graphify-out/` and `graphify-out/` so output never returns.
- **NOT committed** — all changes are staged in the working tree, awaiting the user's call to commit.
- Verified: zero tracked references to graphify remain except the intentional `.gitignore` rules; no refs in `.claude/settings.local.json`.

### Global
- **Deleted** `C:\Users\panka\.claude\skills\graphify\` (`SKILL.md` + `.graphify_version` v0.8.5). Skills dir now empty. No graphify refs in global settings.
- Note: graphify still appears in **this** session's available-skills list (loaded at startup) but is gone from disk → unavailable in new sessions.
- **Still installed:** the `graphifyy==0.8.5` **pip package** in the Python env. Offered `pip uninstall graphifyy`; **user has not confirmed** — pending.

---

## 5. Open items for the next chat
1. **Confirm the client stub state** — is `tb_stub_push_queue_trg` still on the rohan.psom `push_queue`? Finish the DROP (with `lock_timeout` + poller pause) if/when pushes should resume.
2. **Don't drop the stub until** the client is cut over to the gated GCP backend (or you accept auto-push).
3. **Verify `public.push_queue` exists** in the client's (rohan.psom) DB.
4. **Do the backend cutover remotely** with the 4-way alignment (URL + API key + company_name + same Supabase) and the smoke test (§1 checklist).
5. **Resolve the 3-DB identity** — establish which DB is the client's real production (likely rohan.psom, not yynuu) and update the `supabase-db-identity` memory accordingly.
6. **Disk-IO budget** on the client DB — confirm compute landed above Nano, watch the budget after load settles, add indexes / bump Disk IOPS if it keeps depleting.
7. **graphify** — commit the staged repo removal when ready; optionally `pip uninstall graphifyy`.

---

## 6. Quick reference

| Item | Value / location |
|---|---|
| GCP backend URL (client Control Panel) | `https://tallybridge-backend-xx3yz3b3kq-el.a.run.app` (base only) |
| GCP backend alias | `https://tallybridge-backend-950406969086.asia-south1.run.app` |
| Serving backend revision | `tallybridge-backend-00021-854` (gated, `feeb928`, → yynuu) |
| Desktop backend-URL config | `controlPlaneUrl` / legacy `backendUrl` in electron-store `tallybridge-config.json`; set via Settings → `save-settings` (`src/main/ipc-handlers.ts:524`) |
| Poller | `src/main/push-queue-poller.ts` → `BACKEND_URL=controlPlaneUrl` → python `poll_push_queue` |
| Push-queue fetch | `src/python/cloud_pusher.py:352` `GET {BACKEND_URL}/api/sync/push-queue` |
| Backend queue routes | `backend/src/routes/sync.ts`: enqueue `2239`, activate `2310`, GET `2391` |
| Parsing enqueue target | `tallybridge-parsing` env `MINICPM_PUSH_QUEUE_URL` (company `K V ENTERPRISES`) |
| Client's own Supabase | `rohan.psom@gmail.com`'s Project, AWS `ap-southeast-2`, Pro/Micro |
| The three DBs | yynuu (GCP pipeline) · ztugw (secondary/_CLIENT) · rohan.psom (client's own) |
| Stub trigger | `tb_stub_push_queue_trg` + `tb_stub_push_queue()` on client `push_queue` (ACTIVE/uncertain) |
| graphify repo removal | staged: `src/graphify-out/` deleted, `graphifyy` dropped from `src/python/requirements.txt`, `.gitignore` updated (uncommitted) |
| graphify global | `~/.claude/skills/graphify/` deleted; `graphifyy` pip pkg still installed |
| GCP project / region | `tallybridge-test-ocr` / `asia-south1` |

---

## 7. One-line summary for the next chat
> The client runs the old (pre-`feeb928`) Render backend on **their own** Supabase
> (rohan.psom, a third DB), so any push_queue insert auto-pushes to Tally in ~5s.
> A DB **stub trigger** (`tb_stub_push_queue_trg`) was applied to freeze that — but
> the DROP to remove it hung on a lock, and the client DB was simultaneously
> resource-exhausted (Free→Pro, Nano→Micro, now disk-IO-budget throttled). The real
> fix is to cut the client over to the gated GCP backend
> (`tallybridge-backend-xx3yz3b3kq-el.a.run.app`) with API-key + company_name + same-DB
> alignment and an external activate dashboard, then drop the stub. Separately,
> graphify was removed from the repo (staged) and globally.
