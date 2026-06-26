# Session Handout — part_code + PostgREST Transport + Voucher Guard + Client-Release Sync Failure

**Dates:** 2026-06-19 → 2026-06-20
**Branch:** `TallyBridge-Backend-Refactor` (all code changes **uncommitted**)
**Arc:** Shipped stock `part_code` end-to-end → discovered the ingest edge-function is unreliable and replaced it with a PostgREST transport → hit & recovered a catastrophic voucher wipe on testing → built a DB-level delete-guard → released the engine to the client → **client sync now fails (OPEN, being diagnosed).**

---

## 0. CURRENT STATUS / TL;DR

| Item | Status |
|---|---|
| `part_code` feature (Tally Part No. == Mailing Name) | ✅ built across all paths; verified live (3,195/13,237) |
| PostgREST ingest transport (replaces flaky edge fn) | ✅ in `cloud_pusher.py`; verified on testing AND prod |
| `statement_timeout` fix (voucher 57014) | ✅ migration written; applied to testing; **re-applied to prod 06-20** |
| Voucher delete-GUARD | ✅ built, self-test PROVEN on testing AND prod |
| Testing DB (yynuu) | ✅ fully restored: 45,069 vouchers, 3,195 part codes |
| Prod DB (ztugw) schema | ✅ part_code col + all 3 migrations applied; **schema == testing** (verified) |
| **CLIENT SYNC (after GitHub release)** | 🔴 **OPEN — client app runs but no sync completes; `part_code`=0, `last_synced_at` stuck at 06-19 13:22** |
| Engine release to client | ✅ shipped (this is what surfaced the client failure) |
| Commit the branch | ⏭️ still uncommitted |

**THE LIVE PROBLEM:** after the GitHub release, the client's TallyBridge stopped completing syncs. **No new-engine sync has ever succeeded on prod** (`part_code` still 0). Cause not yet confirmed; strongest hypothesis is the anon-role `statement_timeout` (now fixed but client hasn't recovered). **Next step: get the client's live sync-log line, OR run the new engine against prod to reproduce.**

---

## 1. Environments & Identities

- **Testing Supabase `yynuu` = `yynuuysvjeipawzfbeme`** ("niranjansiddharth0@gmail.com's Project"), region ap-northeast-1 (Tokyo).
  - Company **K V ENTERPRISES**: Supabase id `74704231-5692-465f-8bea-34588dcdb86b`, Tally GUID `f3e9df46-fc4f-4b85-930a-abe1e427e900`.
  - Publishable key (works for PostgREST): `sb_publishable_c2Cov2CT8hxOd1FFEo_yaA_6nEt8_Nl`. Service key: `backend/.env` → `SUPABASE_SERVICE_KEY` (+ `SUPABASE_URL`).
- **Client/PROD Supabase `ztugw` = `ztugwhevemibdrzqafyw`** ("rohan.psom@gmail.com's Project", PRO), region ap-southeast-2 (Sydney).
  - Company **K V ENTERPRISES**: id `c47da0b1-b7dd-4589-bb66-73081022c2a3`, **same Tally GUID `f3e9df46-...`** (the user appears to sync the *same* Tally to both yynuu and ztugw).
  - Creds: `.env_clinet` (url + publishable `sb_publishable_IRsM8wDF6w9OyiPegwB2cw_a9aqW9lt` + anon JWT) and `backend.env.yaml` (`*_CLIENT` keys incl. `SUPABASE_SERVICE_KEY_CLIENT` = service_role JWT).
- **Control plane (testing):** `https://tallybridge-backend-828647628834.asia-south1.run.app` (riplara *testing* GCP; reads yynuu). Used only by `fetch_remote_alter_ids` (`/api/sync/alter-ids`), not for hybrid data writes.
- **Tally:** `localhost:9000`, TallyPrime, K V ENTERPRISES (~13–17k stock, ~41–45k vouchers).
- **Access reality:** the user can run **SQL on the client Supabase (ztugw)** but **cannot touch the client's machine/app/Tally** (no live log, no restart).
- **MCP:** claude.ai Supabase MCP is flaky/often-disconnected and only ever lists yynuu (ztugw is a different org). Live work done via **PostgREST + Python `requests`** (`py -3`; `python` not on PATH; foreground `sleep` blocked in Bash → use `run_in_background`/Monitor).

---

## 2. part_code feature (DONE)

part_code = Tally StockItem `$PartNo` == `$MailingName` (3,195/13,237 items). Fetch via `MAILINGNAME`.

**Gotcha A — nested:** serialized as `<MAILINGNAME.LIST><MAILINGNAME>code</MAILINGNAME></MAILINGNAME.LIST>`; `item.get("MAILINGNAME")` → None; the structured resolver can't descend the dotted `.LIST` tag. Must unwrap.

**Gotcha B — write path = RPC, not sync.ts:** hybrid clients write stock via RPC `tb_ingest_masters` (explicit column list), NOT `backend/src/routes/sync.ts:2078` (render-only). K V ENTERPRISES fetches stock via **ODBC** (`source=odbc`) so the ODBC path mattered.

**Files changed (uncommitted):** `tally_client.py` (+MAILINGNAME in stock FETCH), `xml_parser.py` (`_stock_part_code()` + emit in `parse_stock`), `definition_extractor.py` (new `mailing_name` transform + `_mailing_name_value()`), `definitions/structured_sections.json` + `definitions/odbc_sections.json` (stock part_code field), `backend/full_schema.sql` (+`part_code TEXT`).

---

## 3. DB migrations (THREE, `supabase/migrations/`) — apply per DB

1. **`20260619_stock_items_part_code.sql`** — `ALTER TABLE stock_items ADD COLUMN IF NOT EXISTS part_code TEXT;` + `CREATE OR REPLACE tb_ingest_masters(...)` writing part_code, with `SET statement_timeout='120s'` baked in (so re-deploy doesn't reset it).
2. **`20260619_ingest_statement_timeout.sql`** — `DO $$ … ALTER FUNCTION <each ingest rpc> SET statement_timeout='120s' …$$;` over `tb_ingest_vouchers, tb_ingest_phase3_hybrid, tb_ingest_phase4_full, tb_ingest_masters, tb_ingest_snapshots`.
3. **`20260619_voucher_delete_guard.sql`** — the delete-guard (§6).

**Applied:** testing (all 3) ✅; prod ztugw (all 3, guard self-test PROVEN, timeout re-run 06-20) ✅; riplara pending.

---

## 4. Edge-function 503 → PostgREST transport (`cloud_pusher.py`)

**Symptom (testing/my machine):** `direct-masters-snapshots` POST to `…/functions/v1/ingest-sync` stalled ~2m44s → HTTP 503; masters dead since 2026-05-16.

**Diagnosed:** GET health 200; wrong-key POST 401 in 181ms; no-op RPC direct 837ms; FULL masters RPC direct 3.8s; **edge fn POST stalls/503**. Dry-run bisection: 160KB ok, ≥480KB stall; **even 160KB stalls ~37% (3/8)** → `functions/v1` intermittently stalls on POST regardless of size. `rest/v1` rock-solid. Deployed edge code == repo. **Important: the 503 was from MY machine → Tokyo (yynuu). The client → Sydney (ztugw) never had the 503 — they synced fine via the edge function for 2 months.**

**Fix:** the `direct` transport now uses PostgREST. In `cloud_pusher.py`:
- `_post_sync_payload` → `if transport == "direct": return _post_direct_via_postgrest(...)`.
- `_postgrest_base()` derives `…/rest/v1` from `SYNC_INGEST_URL` (`/functions/...`→`/rest/v1`); `_postgrest_headers()` uses publishable `SYNC_INGEST_KEY` (works because **RLS is OFF** — now load-bearing).
- `_resolve_direct_company_id()` replicates the edge fn company upsert (by GUID then name).
- masters → `tb_ingest_phase3_hybrid` (one call); vouchers → `tb_ingest_vouchers` per chunk (`DIRECT_VOUCHER_CHUNK_SIZE=2000`). Masters-chunking attempt was **reverted**.
- **Verified on prod (06-20):** `_post_direct_via_postgrest` no-op masters + vouchers both return ok; prod company GUID resolves cleanly.

---

## 5. Voucher 57014 statement_timeout

PostgREST runs as the **anon/publishable role** whose default `statement_timeout` is too short for a heavy `tb_ingest_vouchers`/`tb_ingest_phase3_hybrid` call → `57014`. The edge fn used the **service role** (no timeout) — which is why the old build worked. Fixed by migration #2 / the baked-in SET. **This role difference is the leading hypothesis for the client failure (§9).** If a chunk fails with a 504/gateway timeout instead, lower `DIRECT_VOUCHER_CHUNK_SIZE`.

---

## 6. Voucher-wipe incident + the GUARD (testing only)

**What:** fixing masters unblocked the voucher push (dead 1 month). `tb_ingest_vouchers`' reconciliation `DELETE FROM vouchers WHERE company_id=X AND synced_at <> this_run` (full-mode, on `is_final_chunk`) wiped yynuu's ~45k vouchers → 1, because a run arrived `mode=full` but carrying only 1 voucher. Pre-existing logic (`20260428_phase4_voucher_ingest.sql` ~L267-308), surfaced by the fix.

**GUARD (`20260619_voucher_delete_guard.sql`):** `AFTER DELETE … FOR EACH STATEMENT` trigger on `public.vouchers` (`REFERENCING OLD TABLE AS deleted_rows`) → `tb_guard_voucher_mass_delete()`: RAISE/rollback if a single delete removes **≥1000** rows, single-company, and leaves **fewer than it removed**; allows company cascade via `NOT EXISTS companies` check. Gotcha: no `min(uuid)` → use `SELECT company_id … LIMIT 1`. Self-test (throwaway company + 1500 vouchers → blocked=true, remaining=1500 → cleanup) proven on **both** DBs. Supabase SQL editor doesn't show `RAISE NOTICE` → use a temp-table + final SELECT.

**Do NOT remove the delete layer** — it's how Tally deletions/cancellations propagate to the cloud; without it, ghost vouchers accumulate and reports drift. The guard keeps the benefit, kills the danger. (Long-term "proper" option: soft-delete with `deleted_at`.)

---

## 7. Testing recovery + TallyPrime degradation

App "Sync All Now" can't force full. Change-detection: local cache `%APPDATA%\tallybridge\.alter_ids_cache.json` (delete → forces full plan) + remote `companies.alter_id` via control plane (only *forces* full if missing). **Reliable recovery = `TB_FORCE_FULL_SYNC=1` running `sync_main.py` directly** (bypasses both). Command used:
```
$env:TALLY_URL='http://localhost:9000'; $env:TALLY_COMPANY='K V ENTERPRISES'
$env:TALLY_COMPANY_GUID='f3e9df46-fc4f-4b85-930a-abe1e427e900'; $env:SYNC_INGEST_MODE='hybrid'
$env:SYNC_INGEST_URL='https://yynuuysvjeipawzfbeme.supabase.co/functions/v1/ingest-sync'
$env:SYNC_INGEST_KEY='sb_publishable_c2Cov2CT8hxOd1FFEo_yaA_6nEt8_Nl'; $env:SYNC_CONTRACT_VERSION='1'
$env:TB_FORCE_FULL_SYNC='1'; $env:TB_SYNC_TRIGGER='manual'; $env:TB_READ_MODE='auto'
$env:TB_USER_DATA_DIR='D:\Desktop\TallyBridge\.tmp-restore'; $env:CONTROL_PLANE_URL=''; $env:PYTHONUNBUFFERED='1'
py -3 -u 'D:\Desktop\TallyBridge\src\python\sync_main.py'
```
**TallyPrime degradation:** mid-recovery, Tally's XML voucher collection returned **0 for every date window** (empty 1511-byte) while ODBC masters still worked — after many heavy full-FY pulls (single-threaded hang, `[[tally_9000_single_threaded_hang]]`). **Fix: restart TallyPrime + reset period (Alt+F2) to full FY.** Then the force-full restore pulled all 45,069 and pushed via PostgREST (23 clean chunks). Tally also showed "(e) Data exceptions exist".

---

## 8. 🔴 OPEN: Client sync stopped after the GitHub release

**Reported:** after releasing the new engine, the client's TallyBridge "froze / stopped uploading more vouchers." User can SQL the client DB but cannot touch the client machine.

**Prod (ztugw) state (06-20):** vouchers **41,561**, purchases 5,641, stock_items 17,053, **part_code 0 / 17,053**, `last_synced_at` stuck at **2026-06-19T13:22:57**. Recent `sync_log` entries are all `mode=full` (~every 3 min, 41,557→41,561) — i.e. the engine was doing **repeated full syncs** (note `client_1211_freeze_rca`: incremental detection not engaging → repeated heavy full-FY reads can freeze single-threaded Tally; mitigations uncommitted/HEAD-regressed).

**Diagnostics done (all point AWAY from schema/transport):**
- **PostgREST transport WORKS on prod** — tested actual `_post_direct_via_postgrest` no-op masters + vouchers → both ok; prod company GUID `f3e9df46` matches Tally → resolves cleanly. So **not** auth/key/RLS/URL.
- **Schema diff yynuu vs ztugw IDENTICAL** (via PostgREST OpenAPI specs, service keys on both): same tables, **column-for-column identical** on `vouchers/voucher_items/voucher_ledger_entries/purchases/stock_items` (both have part_code, discount_pct, etc.), all ingest RPCs present on both. Only diffs: prod missing `Purchase_Matching`/`purchase_matching_api` tables + `get_latest_rates_for_party` RPC (sale/purchase-matching, irrelevant). So **not** a missing column/function.
- **Guard is NOT the cause of a normal sync** — it only fires on a >half wipe (≥1000 deleted AND remaining<deleted). A healthy full re-sync upserts all vouchers with one timestamp → cleanup deletes few. (Could only fire if the sync were already broken / sending inconsistent per-chunk `synced_at`.) Not 100% excludable without the real error.

**Leading hypothesis:** the **anon-role `statement_timeout`**. Old build = edge fn = service role (no timeout) → worked 2 months. New build = PostgREST = anon role = short timeout → heavy masters/voucher RPC hits `57014` → sync dies *before writing anything* (consistent with `part_code` still 0 and `last_synced_at` unchanged). User **re-ran the timeout block on prod (06-20, success)** but the client app has **not yet recovered** (still 0 / 06-19) — either it hasn't retried, or the timeout wasn't the (sole) cause.

**NEXT STEP (pick one) to get the definitive cause:**
1. **Client's live Sync-Log line** — open the running TallyBridge → Sync Log → the last ~15 lines (anything `Upload failed` / `57014` / `tb_guard` / red). Fastest, no prod write.
2. **Run the new engine against prod yourself** (writes to guard-protected prod + reads the user's Tally; pause the client app first to avoid Tally contention) — reproduces the client sync and either completes (confirming the fix + populating part_code) or prints the exact error.

**Fallback de-risk:** **roll back the GitHub release** to the 2-month-working build (user controls the release, not the client machine) → client recovers immediately → re-release after the new build is confirmed clean.

**Read-only prod check snippet (publishable key from `.env_clinet`):**
```python
import requests
base="https://ztugwhevemibdrzqafyw.supabase.co"; key="sb_publishable_IRsM8wDF6w9OyiPegwB2cw_a9aqW9lt"
H={"apikey":key,"Authorization":f"Bearer {key}"}; cid="c47da0b1-b7dd-4589-bb66-73081022c2a3"
hc=dict(H); hc["Prefer"]="count=exact"
# last_synced_at, part_code count, voucher count, recent sync_log...
```

---

## 9. What's left
1. **Resolve the client sync failure** (§8) — get the live error or reproduce; confirm the timeout fix or find the real cause; then verify prod populates `part_code`.
2. **Commit** the branch (part_code feature; transport+guard+timeout) — currently all uncommitted (and `client_1211_freeze_rca` notes HEAD regresses the freeze mitigations).
3. **Re-release** the engine once the client sync is confirmed clean.
4. **Riplara** DBs: apply the 3 migrations + engine if active.
5. (Hardening) **Enable RLS + policies** — the PostgREST transport leans on RLS being OFF.
6. (Reliability) Fix the **repeated-full-sync** behavior so the client doesn't re-hammer single-threaded Tally (incremental detection / freeze mitigations).

## 10. Gotchas cheat-sheet
- `MAILINGNAME` nested in `MAILINGNAME.LIST` — unwrap. Real stock write path = RPC `tb_ingest_masters`, not `sync.ts`.
- Edge fn `functions/v1` intermittently stalls on POST (from my machine→Tokyo); client→Sydney didn't. Use PostgREST `rest/v1`.
- PostgREST transport works **because RLS is OFF**; publishable key then has full write.
- Service role (edge fn) has no statement_timeout; **anon role (PostgREST) does** → heavy ingest RPC → `57014`. Apply `SET statement_timeout='120s'` on the ingest functions.
- `CREATE OR REPLACE FUNCTION` wipes a function's `SET statement_timeout` — bake it in or re-run the timeout block after.
- No `min(uuid)` in Postgres — use `LIMIT 1`. Supabase SQL editor doesn't show `RAISE NOTICE` — return a table.
- Voucher reconciliation deletes "anything not in this sync" — guarded now; never remove the layer.
- App "Sync All Now" can't force full; use `TB_FORCE_FULL_SYNC=1` via direct `sync_main.py`.
- Repeated full-FY voucher pulls degrade single-threaded TallyPrime (serves 0 vouchers) — restart + reset period.
- Schema-diff trick: `GET {base}/rest/v1/` (OpenAPI) with a **JWT service key** (publishable key returns an empty spec) → diff `definitions` (columns) + `/rpc/*` paths.
- `python` not on PATH → `py -3`. Foreground `sleep` blocked in Bash → `run_in_background`/Monitor.
- The classifier blocks **writes/mass-deletes against prod** — keep prod checks read-only / no-op unless the user explicitly authorizes a prod write.
