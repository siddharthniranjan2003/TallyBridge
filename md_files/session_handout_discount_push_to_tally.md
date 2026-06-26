# TallyBridge — Pushing Per-Item Discount % into TallyPrime: Session Handout

Context-transfer doc for a new chat. This session took the per-item discount
that already lives in the `push_queue` and made the **outbound push to
TallyPrime actually render it** (the push XML previously hardcoded 0%). Then we
diagnosed why a live `push_now` row wasn't pushing from the desktop app.

Sibling docs (read for the upstream parsing/discount-derivation work):
- `session_handout_purchase_discount.md` — how `discount_pct` / `discount_total`
  are computed in the purchase parsing pipeline and persisted to `push_queue`.
- `session_handout_sale_discount.md`, `session_handout_sale_minicpm_pushqueue.md`
  — the sale path.

Last updated: 2026-06-02.

---

## 0. TL;DR — what this session did

1. **Fixed the push** so vouchers sent to TallyPrime carry the per-item discount.
   The push builder hardcoded `<DISCOUNT>0</DISCOUNT>` / `<BATCHDISCOUNT>0</BATCHDISCOUNT>`,
   silently dropping the `discount_pct` that flows through the queue.
2. **Verified end-to-end against a live TallyPrime** (company `K V ENTERPRISES`):
   - manual purchase push, 25% → created, Disc % column shows 25%
   - manual sale push (`GST SALE`), 10% → created
   - **full real poll cycle** (backend queue → `run_pending_push_cycle` → Tally)
     at 15% → created
   - the desktop app itself later pushed the stuck emkay row at 3% → created
3. **Rebuilt the bundled engine** (`npm run build:python`) so the installed app
   ships the fix, and **committed + pushed** to `tallybridge-test`.
4. **Diagnosed the "stuck at push_queue"** problem (auto-sync paused + local
   backend not running) and the **413 sync error** (Cloud Run 32 MB request cap
   vs a very large company).

**Key semantics (unchanged from parsing side):** discount is **display-only**.
The item `amount` is *already net* (after discount); `rate` is gross. So the
voucher already balanced at net — the only missing thing was the `<DISCOUNT>`
tag. RATE stays gross, AMOUNT stays net, discount % is unsigned for both
purchase and sale.

---

## 1. The push pipeline (Cloud → TallyPrime)

The desktop app does **not** talk to Supabase directly. It polls the **backend**
(control plane), which talks to Supabase. Full chain:

```
Electron app (timer every 5s)
  PushQueuePoller.pollOnce()                       src/main/push-queue-poller.ts:93
   -> bails if isPaused() / sync-in-progress / no url+key+company  (:95,:104,:108)
   -> spawns Python (TB_COMMAND=poll_push_queue)                   (:155)
        sync_main.run_poll_push_queue_command()                    src/python/sync_main.py:1198
         -> run_pending_push_cycle()                               sync_main.py:1106
             -> fetch_pending_push_vouchers()  GET /api/sync/push-queue  cloud_pusher.py:340
                  backend returns rows where status = "push_now"   backend sync.ts:2304
                  filtered to allowed voucher types                sync.ts:2312
             -> push_vouchers([voucher_payload], company)          tally_pusher.py:515
                  builds XML, POSTs to TallyPrime :9000
             -> mark_push_results()  POST /api/sync/push-results    cloud_pusher.py:389
                  backend writes status pushed/failed              sync.ts:2323
```

Separate one-shot path (not used by the poller): `src/main/local-push-server.ts`
listens on **port 3002** loopback, accepts `POST /push-voucher`, spawns Python
with `TB_COMMAND=push_voucher` (`sync_main.py:1208`) — pushes a single voucher
immediately, no queue.

### `push_queue.status` lifecycle (4 states)

```
(enqueue)        (activate)            (poll+push)
  -> pending  -->  push_now  -->  pushed
                              \->  failed
```

- `pending` — inserted by `POST /push-queue` (`sync.ts:2183`). **Poller ignores it.**
- `push_now` — set by `POST /push-queue/activate` (`sync.ts:2257`); only `pending`
  rows can flip. **Only status the poller fetches.**
- `pushed` — set by `/push-results` on Tally success; stamps `pushed_at`.
- `failed` — set by `/push-results` on Tally error; records `error_message`.
- No `cancelled`/`processing` state. No idempotency: an interrupted push stays
  `push_now` and re-pushes next poll (creates a duplicate Tally voucher).
- **No retry path**: `activate` only accepts `pending`, so a `failed`/`pushed` row
  can't be re-activated without first resetting it to `pending` by other means.

---

## 2. The fix (code touched THIS session)

Only one source file + the rebuilt binary.

| File | Change |
|---|---|
| `src/python/tally_pusher.py` | (1) new `_format_discount()` helper — renders an unsigned percent, trims `40.00`→`40`, `12.50`→`12.5`, empty→`0`. (2) `_normalize_items` now reads `item["discount_pct"]` into the normalized item. (3) `_build_inventory_entry_xml` (~line 335): `<DISCOUNT>0</DISCOUNT>` → `<DISCOUNT>{pct}</DISCOUNT>`. (4) `_build_batch_allocations_xml` (~line 315): `<BATCHDISCOUNT>0</BATCHDISCOUNT>` → `<BATCHDISCOUNT>{pct}</BATCHDISCOUNT>`. |
| `python-dist/tallybridge-engine.exe` | rebuilt via `npm run build:python` (PyInstaller) so the desktop app ships the change. |

Diff was surgical (+15/−2). `RATE` stays gross, `AMOUNT` stays net, sign rules
unchanged; no-discount items still emit `0` (backward compatible). Same code path
serves purchase and sale.

### Backend / parsing were NOT changed this session
The backend already whitelists what's needed; parsing already computes it. Those
were done in prior commits:
- `29ebb28` — `backend/src/routes/sync.ts` (+`discount_total` whitelist) +
  `parsing/purchase/voucher_builder.py` (derived `discount_pct` + `discount_total`).
- `b24fa33` — `backend/src/routes/sync.ts` (+ per-item `discount_pct` whitelist) +
  `parsing/server/handler.py` + `supabase/migrations/20260602_sale_discount_pct.sql`.

`normalizePushVoucherPayload` (`sync.ts:121`) rebuilds the voucher from a strict
whitelist; it **keeps** `ledger_entries`, `inventory_ledger_name`, per-item
`discount_pct` (`sync.ts:233`), and top-level `discount_total` (`sync.ts:247`).
It **drops** the per-item rupee `discount` (not whitelisted) — only `discount_pct`
+ `discount_total` persist. That's fine: the Tally `<DISCOUNT>` uses the percent.

---

## 3. The Tally XML the push produces (with discount)

Both wrapped in the same import envelope (`_build_import_envelope`, `tally_pusher.py:441`):
`<ENVELOPE><HEADER><TALLYREQUEST>Import</TALLYREQUEST>...<SVCURRENTCOMPANY>` +
one `<VOUCHER>` per voucher.

Key per-item tags after the fix (purchase example, 40% on rate 950 × qty 20):
```xml
<RATE>950.00/NOS</RATE>
<DISCOUNT>40</DISCOUNT>        <!-- was 0 -->
<AMOUNT>-11400.00</AMOUNT>     <!-- net, negative for purchase -->
... <BATCHDISCOUNT>40</BATCHDISCOUNT> ... <BATCHRATE>950.00/NOS</BATCHRATE>
```
Sale is identical but `<ISDEEMEDPOSITIVE>No`, AMOUNT positive; `<DISCOUNT>` is
unsigned the same way.

Structural facts (easy to miss):
- The **inventory ledger** (Sales/Purchase) is **not** a top-level
  `<LEDGERENTRIES.LIST>` — it lives inside each item's
  `<ACCOUNTINGALLOCATIONS.LIST>`. Top-level ledger entries = party + tax only.
- Sign rules: purchase items negative / `ISDEEMEDPOSITIVE Yes`; sale positive /
  `No` (`_signed_inventory_amount:75`, `_build_inventory_entry_xml:316`).
- Inventory ledger auto-detected by name+amount match unless
  `inventory_ledger_name` passed; must match item total within ±0.05.
- **Tally treats supplied `AMOUNT` as authoritative**, uses RATE+DISCOUNT for
  display only → no rounding/double-discount risk (proven live).

---

## 4. Live test results (all against company `K V ENTERPRISES`, Tally :9000)

Real masters were queried first (to avoid auto-creating junk):
- party (purchase) `AAYUDH TOOLS` (Sundry Creditor); party (sale) `3S DESIGN`
  (Sundry Debtor)
- purchase ledger `PURCHASE GST`; sale ledger `GST SALE`; taxes `CGST`/`SGST`
- stock `112BH040 IST SET WITH HOLDER 4 mm` (unit SET); godown `Main Location`
- real voucher types: `Purchase` and `GST SALE` (there is **no** "GST PURCHASE";
  first attempt failed with "Voucher Type 'GST PURCHASE' does not exist!").

| Test | Voucher no | Type | Disc | Rate→Net | Total | Result |
|---|---|---|---|---|---|---|
| manual purchase | `TBDISCTEST1` | Purchase | 25% | 1000→1500 | 1770 | created, Disc% shows ✅ |
| manual sale | `TBDISCTEST2` (`KVE/25-26/1`) | GST SALE | 10% | 1000→1800 | 2124 | created ✅ |
| **queue→poll→push** | `TBQUEUETEST1` | Purchase | 15% | 1000→1700 | 2006 | created via real poll cycle ✅ |
| desktop app (emkay) | `GN25Y-32738` | Purchase | 3% (×4 items) | — | 3,99,653 | created by the app itself ✅ |

The queue test was the important one: POSTed a controlled row to the **TEST GCP
backend**, activated it, confirmed `GET /push-queue` returned `discount_pct:15`
intact, then ran `sync_main.run_pending_push_cycle()` (the exact function the
poller calls) → Tally `created=1` → row marked `pushed`.

**Cleanup:** test vouchers `TBDISCTEST1`, `KVE/25-26/1`, `TBQUEUETEST1` (plus the
real emkay `GN25Y-32738`) are live in `K V ENTERPRISES` — delete the synthetic
ones when done.

---

## 5. Diagnostics: "stuck at push_queue" + the 413

### Why a `push_now` row wasn't pushing from the desktop app
Two independent blockers (both must be fixed):
1. **Auto-sync was paused** — `pollOnce()` returns immediately if `isPaused()`
   (`push-queue-poller.ts:95`). The app's bottom bar showed "Auto-sync paused".
   Resume from the **Home** screen.
2. **Control Plane URL was `http://localhost:3001` but no backend was running
   there** (`curl` → HTTP 000). The desktop app's control plane is a *local*
   backend you must run (`cd backend && npm run dev`, `PORT=3001`,
   `SUPABASE_URL=yynuuysvjeipawzfbeme`). The "push button" only flips
   `pending→push_now`; it can't push to Tally — only the desktop poller can
   (Tally is on `localhost:9000`).

**Resolution chosen:** point Control Plane URL at the always-on **TEST GCP
backend** instead of running a local backend:
`https://tallybridge-backend-xx3yz3b3kq-el.a.run.app` (base URL only, no trailing
slash, no `/api`; the code appends `/api/sync/...`). The GCP backend talks to the
*same* TEST Supabase, so rows are shared. With that + auto-sync resumed, the app
pushed the stuck emkay row (3% discount) successfully.

### The 413 "Request Entity Too Large" (separate from push)
After switching the control plane to GCP, **inbound sync** (Tally→cloud) failed:
```
Sync failed: 413 Request Entity Too Large  (Google Front End / Cloud Run HTML page)
```
- **Cause:** `POST /api/sync` uploads the whole company dataset. `K V ENTERPRISES`
  is huge (~2,904 ledgers, ~13,237 stock items) → body exceeds **Cloud Run's hard
  32 MiB request cap** (infra-level; `TB_JSON_BODY_LIMIT=100MB` can't help — the
  HTML error proves it's rejected before reaching Express).
- This is almost certainly **why a local backend on 3001 was used originally** —
  local Express has no such limit.
- **Push is unaffected** (small payloads); only the big **sync** hits the wall.
- **Options:** (a) Settings → Sync Ingest Mode = `hybrid`/`direct` so bulk
  sections go straight to the Supabase edge function (Sync Ingest URL already set
  to `...supabase.co/functions/v1/ingest-sync`), bypassing Cloud Run; (b) keep a
  local backend for sync; (c) chunk `POST /api/sync` into <32 MB requests (code
  change). Not yet implemented — left as the open decision.

---

## 6. Endpoints + URLs (TEST)

| Thing | Value |
|---|---|
| TEST GCP backend (control plane) | `https://tallybridge-backend-xx3yz3b3kq-el.a.run.app` |
| Activate (push button) | `POST {backend}/api/sync/push-queue/activate` body `{job_id, company_name?}` |
| Fetch queue (poller) | `GET {backend}/api/sync/push-queue?company_name=...&limit=` (returns `push_now` only) |
| Enqueue | `POST {backend}/api/sync/push-queue` body `{company_name, voucher_payload}` |
| Report results | `POST {backend}/api/sync/push-results` body `{results:[{id,status,...}]}` |
| Auth | header `x-api-key: <API_KEY>` (value in `backend/.env`, matches GCP backend) |
| Local backend (alt) | `http://localhost:3001` (`cd backend && npm run dev`) |
| Tally XML server | `http://localhost:9000` |
| Local push (one-shot) | `POST http://127.0.0.1:3002/push-voucher` |

Allowed push voucher types: `Sales`, `Purchase`, `GST SALE`, `GST PURCHASE`
(override via `TB_PUSH_ALLOWED_TYPES`). `activate` is type-agnostic; the type
check is enforced at enqueue and at the poller fetch.

---

## 7. Git state

- **Commit `56e760f`** — `feat(push): apply per-item discount % when pushing
  vouchers to TallyPrime` (`src/python/tally_pusher.py` + rebuilt
  `python-dist/tallybridge-engine.exe`; +15/−2).
- **Branch:** `TallyBridge-Backend-Refactor`.
- **Pushed:** `29ebb28..56e760f` → **`tallybridge-test`** only
  (`github.com/siddharthniranjan2003/tallybridge-test.git`).
- **`origin`** (`github.com/siddharthniranjan2003/TallyBridge.git`) was **NOT**
  pushed — it is missing all 3 discount commits (`b24fa33`, `29ebb28`,
  `56e760f`). Push there separately if/when desired.
- First commit attempt mangled the message (PowerShell here-string used in the
  Bash tool leaked a literal `@`); amended to a clean message.
- Untracked junk in `backend/`: `_3001.err`, `_3001.out`, `_srv3001.log` (local
  backend run logs) — not committed; safe to delete.

---

## 8. Databases — DO NOT CONFUSE (safety-critical)

| Supabase ref | Which | Notes |
|---|---|---|
| `yynuuysvjeipawzfbeme` | **TEST** | the only DB/backed env safe to write. GCP backend + local backend both point here. |
| `ztugwhevemibdrzqafyw` | **CLIENT PRODUCTION** | client backend `tallybridge-h3do` → client TallyPrime. **NEVER** point the app's Control Plane URL here while testing — a push flows into the client's real Tally. |

`.env` files never committed (secrets only in env / GCP). `backend/.env` →
`SUPABASE_URL=yynuuysvjeipawzfbeme`, `PORT=3001`, 47-char `API_KEY` (matches GCP).

---

## 9. Open items / gaps

1. **Engine rebuilt locally only** — `56e760f` ships the new `.exe` on
   `tallybridge-test`. If the user installs from a build pipeline, ensure that
   pipeline picks up the rebuilt engine.
2. **413 on large-company sync via Cloud Run** — unresolved by design choice;
   pick hybrid/direct ingest, local backend, or chunked upload (§5).
3. **No retry for `failed` rows** — `activate` won't re-activate non-`pending`
   rows; would need a reset-to-pending route.
4. **No dedup/idempotency** — re-pushing a voucher_number creates a new Tally
   voucher; interrupted pushes re-fire.
5. **Discount is metadata/display only** — does not change Tally's GST math
   because `amount` is already net; consistent with the parsing stance.
6. **Per-item rupee `discount` not persisted** — only `discount_pct` +
   `discount_total` survive the backend whitelist.
7. **`origin` not updated** — only `tallybridge-test` has the discount work.

---

## 10. Quick reference

| Thing | Value |
|---|---|
| Push discount code | `src/python/tally_pusher.py` → `_format_discount`, `_normalize_items`, `_build_inventory_entry_xml`, `_build_batch_allocations_xml` |
| Rebuild engine | `npm run build:python` → `python-dist/tallybridge-engine.exe` |
| Poller | `src/main/push-queue-poller.ts` (5s; pauses gate at :95) |
| Push cycle (py) | `src/python/sync_main.py:1106` `run_pending_push_cycle` |
| Backend queue routes | `backend/src/routes/sync.ts:2132` (enqueue), `:2203` (activate), `:2284` (fetch), `:2323` (results) |
| Whitelist | `backend/src/routes/sync.ts:121` `normalizePushVoucherPayload` (keeps discount_pct@233, discount_total@247) |
| Test company / Tally | `K V ENTERPRISES` @ `http://localhost:9000` |
| TEST backend | `https://tallybridge-backend-xx3yz3b3kq-el.a.run.app` |
| TEST Supabase (safe) | `yynuuysvjeipawzfbeme` |
| CLIENT (NEVER) | `ztugwhevemibdrzqafyw` / `tallybridge-h3do` |
| Commit | `56e760f` on `TallyBridge-Backend-Refactor` → `tallybridge-test` |
| Sibling handout | `session_handout_purchase_discount.md` |
