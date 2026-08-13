# Session Handout — TallyBridge becomes the backend: scope cut to MRP + Rate, local stack folded into the app

> Read this to continue in a fresh chat with full context.
> **Date:** 2026-08-13 · **Branch:** `feat/on-prem-supabase-stacks` (suggest branching
> `feat/app-serves-local-stack` from it) · **Supersedes** the "delete the native stack"
> direction proposed earlier in this same session — see §3.

---

## 0. TL;DR

> **STATUS: BUILT AND WORKING.** `TallyBridge Setup 2.0.0.exe` installs, starts the whole
> data stack itself, and serves both Website 2 screens from the client's machine with no
> Supabase and no Cloud Run. Website 2 was built and opened against it and rendered real
> data. Committed and pushed as `feat/app-serves-local-stack` (`7235b7c`).
> **Start at §12 if you are picking this up fresh.**

- **Scope cut.** The product is now three features: **MRP report (R1–R7)**, **Stock Info /
  Rate**, and **price list** — price list **deferred**, not in this phase.
- **TallyBridge becomes the backend.** It spawns Postgres + PostgREST + the existing
  Express backend as child processes, the way it already spawns the Python engine.
  One installer, one app, no Docker, no separate Windows services.
- **We are NOT porting the backend to raw SQL.** Bundling PostgREST keeps
  `sync.ts`, `copy-live-to-local.mjs` and `schema-parity.mjs` working unchanged. This
  reversed an earlier decision in this session; the reason is in §3.
- **Lifecycle:** seed local from cloud → verify → **dual-write** → verify → stop the
  cloud write. The dual-write step is mandatory and was missing from the first plan.
- **Testing is on this dev box**, which has TallyPrime running — the full loop is
  reproducible here.
- **⚠ A live `service_role` key is committed in this PUBLIC repo.** See §7. Not fixed;
  user asked to be told, not for files to be touched.

---

## 1. Decisions locked this session

| Question | Decision |
|---|---|
| Features kept | MRP report, Stock Info / Rate. Price list **deferred**. |
| Who serves them | TallyBridge desktop app, on the client's machine |
| Store | PostgreSQL, bundled inside TallyBridge |
| API shape | PostgREST — bundled, **not** replaced with hand-written SQL |
| Reach | Public internet, via Cloudflare Tunnel (domain not yet purchased) |
| Cutover source | cloud **testing** `yynuuysvjeipawzfbeme` first, then client `ztugwhevemibdrzqafyw` |
| Company | one — `K V ENTERPRISES` |
| Clients | one |
| Website 1 (`ops`) | stays on cloud; its pickers freeze after cutover — **accepted** |
| Release | build the `.exe` locally, install by hand. **No GitHub publish yet.** |
| Drivers | cost + client wants data on-prem |
| Deadline | ASAP |

---

## 2. Target architecture

```
                          Tally PC  (= this dev box, for now)
┌──────────────────────────────────────────────────────────────────┐
│  TallyPrime :9000                                                 │
│      ▲ XML                                                        │
│  tallybridge-engine.exe (Python)                                  │
│      ├──► cloud Supabase        (Website 1 — unchanged)           │
│      └──► local Express backend (new sink)                        │
│                                                                   │
│  Electron main  ── spawns and supervises ──┐                      │
│      ├─ postgres.exe            :5432       │                     │
│      ├─ postgrest.exe           :3000       │  all child procs,   │
│      ├─ gateway (Caddy :8000)               │  all loopback-bound │
│      ├─ node.exe dist/index.js  :3001       │                     │
│      └─ cloudflared.exe  (later)  ──────────┘                     │
└──────────────────────────────────────────────────────────────────┘
                              ▲ HTTPS (later)
                  Flutter Website 2 (rate) on Firebase Hosting
```

The client sees **one app**. Everything above is internal.

---

## 3. Why we are NOT porting to raw SQL (reversal)

Earlier in this session the plan was: fold Postgres in, delete PostgREST and Caddy,
reimplement the endpoints as SQL in the Electron main process. That was sound **while
the scope was two read endpoints**.

It stopped being sound the moment the goal became **"stop the write to Supabase"**.
That requires the entire *ingest* path to run locally, not just the two reads — and the
Express backend already implements all of it against a PostgREST-shaped API. Keeping
PostgREST means:

- `backend/src/routes/sync.ts` (3,253 lines) runs **unchanged** — point `SUPABASE_URL`
  at the local gateway.
- `local-stack-shared/scripts/copy-live-to-local.mjs` keeps working. **This matters:**
  it writes to local over PostgREST (`copy-live-to-local.mjs:170`), so deleting
  PostgREST would have broken the cutover tool itself.
- `schema-parity.mjs` and `verify-ingest-path.mjs` keep working.
- The Flutter Rate screen talks PostgREST directly
  (`stock_info_screen.dart:201,222,584`) — pointing `Config.supabaseUrl` at the local
  gateway should need **no Dart data-layer rewrite**, only two values in
  `env/testing.json`. *(Verify — see §8.)*

Cost of keeping it: ~30 MB and two extra child processes. Against ASAP, that is cheap.

**Nothing gets deleted.** `native-stack/` is now the payload TallyBridge ships, not a
competing deliverable. What does go away is `native-stack/installer/` (Inno Setup) and
`dist-client/` — TallyBridge's own NSIS installer replaces them. `local-supabase/`
(Docker) stays as the dev-box convenience or gets dropped; it is not on the client path
either way.

---

## 4. Lifecycle — the order that matters

The user's instinct was "copy cloud → local, then go". That is right, and for a stronger
reason than convenience:

> Seed local from cloud and both sides hold **identical rows**. Run the MRP report
> against each. The CSVs must come out byte-identical. Any diff is a bug in the local
> stack — nothing else can explain it. Re-syncing from Tally instead gives two different
> datasets, and then a diff proves nothing.

Same technique as `tools/price_list/qa2.py` in the aiaccountant repo: freeze a known-good
reference, then diff.

```
0  capture golden CSVs from Cloud Run (R1..R7)     ← do FIRST, can't be recovered later
1  start native stack, confirm what's in it
2  seed / top up from cloud testing
3  run the same reports against the LOCAL backend, diff vs golden
4  Flutter on loopback  → proves the read path end to end   (no tunnel needed)
5  Electron spawns and supervises the stack        → packaging
6  build the .exe, install on this box, test with TallyPrime
7  Python dual-writes cloud + local                → proves freshness
8  Cloudflare Tunnel + Cloudflare Access           → when the domain lands
9  stop the cloud write                            → cutover
```

**Step 7 is mandatory and was missing from the first plan.** Without it, local is frozen
at seed time; stop the cloud write at step 3 and the MRP report reports last week's
inventory forever.

Steps 0–4 need **no TallyPrime at all** — the data comes from cloud, not Tally. Tally
only enters at step 6.

---

## 5. What "stop the write to Supabase" actually costs

The Tally sync feeds `stock_items`, `ledgers`, `vouchers`, `voucher_items`. Website 1
(`ops`) reads all four via `voucher_detail_sheet.dart`. After cutover:

| Still works | Freezes |
|---|---|
| Queue screen (`push_queue`) | Item picker — new Tally items never appear |
| History screen (`push_queue`) | Customer/vendor pickers (`CustomersCache`/`VendorsCache`) |
| Push-to-Tally | Rate history (`voucher_detail_sheet.dart:3607`) |

**Accepted** — masters change rarely at K V ENTERPRISES.

Note the bill: push-to-Tally still polls Cloud Run (`push-queue-poller.ts:198` uses
`config.controlPlaneUrl`), so stopping the Supabase write saves Supabase storage and
egress — **not** the Cloud Run bill. Cloud Run only goes away when Website 1 does.

---

## 6. The API key — when it is actually needed

It guards the endpoints TallyBridge serves. Nothing else.

- **Loopback (`127.0.0.1`, browser on the same PC)** — **not needed.** Only local
  processes can reach the port. Auth here is theatre. This is steps 0–6.
- **Once the tunnel is up** — **mandatory.** The tunnel makes the port a public HTTPS
  URL. Unauthenticated, anyone who learns the hostname runs
  `GET /api/sync/reorder-levels/R7?format=csv` and downloads the client's full inventory
  *with purchase rates* — their cost basis and margins, as a spreadsheet.
- **Honest limit:** a key compiled into a Flutter **web** bundle is not secret. The
  browser downloads that bundle; the key is readable from the JS in a minute.
  `Config.mrpApiKey` already has this property today. A key stops scanners, not a person
  who has opened the site.
- **Right answer:** **Cloudflare Access** in front of the tunnel — free to 50 users,
  demands an email/Google login *before* traffic reaches the machine. Then the key is
  only defence in depth.

---

## 7. ⚠ SECURITY — live `service_role` key in a PUBLIC repo

`github.com/siddharthniranjan2003/TallyBridge` is **public** (`"private": false` from the
unauthenticated GitHub API).

`n8n/gsheet_appscript.js` contains a committed Supabase JWT. Decoded header/payload
(key itself not reproduced here):

```
role = service_role     project_ref = hbaadljcliqzwjtpobex     exp ≈ April 2036
```

`service_role` bypasses RLS — full read/write/delete on that project, for another ten
years, in a repo anyone can clone. Automated scanners grep GitHub for exactly this.

**Deleting the line does not fix it.** It is in the history of a public repo permanently.
**Rotating the key in the Supabase dashboard is the only fix.**

Lesser, same repo:

- `sb_secret_N7UND0…` committed in `local-supabase/README.md` and
  `local-supabase/scripts/bootstrap.mjs` — local-stack secret.
- `sb_publishable_oFyc0k…` throughout `md_files/endpoint_workbook.md` — publishable keys
  are safe to expose **only if RLS is on**. Handout 2026-08-07 §5 records that `anon` had
  ALL grants and no RLS on the local stack; worth confirming that is not also true of the
  cloud projects.

Per user instruction this session: **reported, files not touched.**

---

## 8. Open implementation questions

1. **Gateway: keep Caddy, or proxy inside Electron?** supabase-js appends `/rest/v1/`,
   so something must route that to PostgREST. Caddy already does it and is verified —
   keep it for now. Collapsing it into the Electron HTTP server is ~30 lines and can be
   a later trim.
2. **Ingest mode must be `render`, not `direct`.** `direct`/`hybrid` post to
   `/functions/v1/ingest-sync`, which needs an edge-function runtime we are not
   bundling. `render` routes Python → Express backend → PostgREST → Postgres.
   (See the `cloud_pusher.py:224` trap in handout 2026-08-07 §5.)
3. **Dual-write mechanism** — does `cloud_pusher.py` support two sinks, or does it need
   a second target URL added? Unverified.
4. **Flutter reading local PostgREST — VERIFIED WORKING, but with the wrong key.**
   The Rate screen's exact embedded query runs unchanged against the local gateway
   (§11). It was tested with the **service_role** key, which bypasses RLS entirely and
   therefore **cannot** ship in a web bundle. `903_lockdown_anon.sql` closed `anon`, so
   right now there is *no* usable read-only credential for the Flutter app.
   **This is the top open item.** Options: (a) a new restricted role with SELECT on the
   four tables only; (b) route the Rate screen through the backend instead of direct
   PostgREST; (c) Cloudflare Access as the real gate plus a restricted key.
   (a)+(c) is the intended answer.
5. **`as_of` date override.** `buildInventoryIntelligenceReport` calls `getTodayIsoDate()`
   and derives its 6-month/1-month windows from it, so a golden CSV captured today stops
   matching tomorrow. Add an override **before** capturing goldens, or the test rots in
   24 hours.
6. **Backups.** Still nothing schedules a `pg_dump`. Flagged in handout 2026-08-07 §7 and
   now more urgent — after cutover this machine is the only copy.
7. **Version + release hygiene.** Bump to `2.0.0`. `updater.ts:22-26` sets
   `autoDownload = true` and `autoInstallOnAppQuit = true`, and `electron-builder.yml`
   publishes to the live repo — so **never** publish a test build. Use
   `npm run dist -- --publish never`. (Currently safe by accident: the newest release is
   ≤ 1.2.19, so a local 2.0.0 finds nothing newer.)
8. **AV / code signing.** Unsigned, and `postgres.exe` / `postgrest.exe` / `cloudflared.exe`
   raise false-positive odds — handout 2026-08-07 §7.3 already flagged this pattern.

---

## 9. Environment as found (2026-08-13, this box)

```
TallyPrime          RUNNING — :9000 open; tally, tallygatewayserver, tallyscheduler
native-stack        INSTALLED, STOPPED — config/ vendor/ data/ present, :5432 :8000 closed
backend             stopped (:3001 closed)
TallyBridge         stopped (:3002 closed)
Docker stack        stopped (:54321 :54322 closed)
node                v24.19.0 at %LOCALAPPDATA%\Microsoft\WinGet\Packages\OpenJS.NodeJS.LTS_*\
                    NOT on PATH in fresh shells
env files present   backend\.env · native-stack\config\.env · local-supabase\.env
repo                PUBLIC · newest release <= 1.2.19 · package.json at 1.2.19
```

---

## 10. THE EXE — built, installed and verified

```
release\TallyBridge Setup 2.0.0.exe        203.7 MB    built with --publish never
```

Installed silently to `%LOCALAPPDATA%\Programs\TallyBridge`, launched, and it brought up
the entire stack **by itself** from its own bundled copies:

```
[stack] stackDir=...\Programs\TallyBridge\resources\stack
[stack] starting from ...\resources\stack (data: C:\ProgramData\TallyBridge\data)
[stack] postgres ready on 5432 · postgrest 3000 · gateway 8000 · backend 3001
[stack] all services up
[Push API] Listening on http://127.0.0.1:3002
```

Endpoints served by the installed app:

| | |
|---|---|
| `/api/sync/reorder-levels` | 13,237 scanned, 13,237 classified, as_of 2026-08-13 |
| R1..R7 csv | 1 / 0 / 13,203 / 0 / 33 / 0 / 13,237 rows |
| Stock Info | real sale history with party, qty, rate, discount |

### ⚠ Read the report numbers with the data in mind

R3 (DEAD CAPITAL) holds 13,203 of 13,237 items and R2/R4/R6 are empty. That is **not a
bug** -- it is what the data says. The full sync showed voucher windows returning **0
rows from 2025-08-04 onward**, with only a handful of 1-3 row weeks in 2026. The report's
sales signal is a **6-month window** (2026-02-13 to 2026-08-13 today), which is therefore
almost empty, so nearly every item classifies as dead. The cloud-backed data behaves the
same way because it came from the same Tally.

Also note `needs_reorder` moved 5 -> 1 between the cloud-seeded and Tally-built
databases. Same 6,394 priced items; the difference comes from the 16 stock items and 8
vouchers that differ. Small, explainable, worth knowing before anyone reads the demo as
a business signal.

### Handing Website 2 over

`C:\project\AiAccountant\env\testing-local.json` is written and BOM-free. It is a
**separate file** on purpose: both sites build from the same env file, so editing
`testing.json` would repoint Website 1 (ops) at localhost on its next rebuild.

Changed from `testing.json`: `SUPABASE_URL` -> `http://127.0.0.1:8000`,
`SUPABASE_ANON_KEY` -> the local **service_role** key, `BACKEND_BASE_URL` ->
`http://127.0.0.1:3001`, `BACKEND_API_KEY` / `MRP_API_KEY` -> `BACKEND_API_KEY` from
`config\.env`. Everything else carried over.

```
cd C:\project\AiAccountant
flutter build web --release --dart-define-from-file=env/testing-local.json --dart-define=SITE=rate
```

Serve that build locally (or `flutter run -d chrome`). **Do not deploy it to Firebase** --
it points at 127.0.0.1, so it only works in a browser on the Tally PC, and it carries the
service_role key.

## 11. Corrections to earlier sections of THIS document

Written progressively during the session; these superseded themselves:

- **§4's lifecycle (seed from cloud -> dual-write -> stop cloud write) was not what
  happened.** The user chose a **fresh install + full sync from Tally** instead, which is
  strictly better: it proves the extraction AND the disaster-recovery path, where seeding
  from cloud only proves a copy. See §10's Tally-built database results.
- **§4's golden-CSV diff was never run**, and matters much less than first argued. Once
  PostgREST was kept, the *same backend code* runs against both databases, so a diff tests
  data completeness rather than a port. The row-count diff in §10 covers that.
- **The `--config-dir` fix in §8 is still open.** `local-stack.ts` works around it by
  mirroring ProgramData into the stack directory.

---

## 12. START HERE — picking this up in a new chat

### Where it stands

| | |
|---|---|
| Local Postgres, fed from Tally, no cloud | done, verified |
| MRP report + Stock Info served locally | done, verified |
| `TallyBridge Setup 2.0.0.exe` (203.7 MB) | built, installed, runs the whole stack itself |
| Website 2 built and opened against it | done — rendered real data |
| Branch `feat/app-serves-local-stack` (`7235b7c`) | pushed |

### Bring it up on this box

```powershell
# 1. TallyBridge (starts postgres, postgrest, caddy, backend itself -- ~15s)
Start-Process "$env:LOCALAPPDATA\Programs\TallyBridge\TallyBridge.exe"

# 2. Website 2 -- Flutter 3.47.0 is at C:\src\flutter; a NEW shell has it on PATH
cd C:\project\AiAccountant
flutter run -d chrome --dart-define-from-file=env/testing-local.json --dart-define=SITE=rate
```

If `flutter` is not found, the shell predates the install: `$env:Path = "C:\src\flutter\bin;$env:Path"`.

`env/testing-local.json` is **gitignored** and exists only on this machine. It is a
separate file from `testing.json` on purpose: both sites build from the same env file, so
editing `testing.json` would repoint Website 1 (ops) at localhost on its next rebuild.

### Reading the reports

`hero_sku_health`, `buying_mistakes` and `risk_watch` come back **empty**, and
`dead_capital` holds 13,203 of 13,237 items. **That is the data, not a bug** — the Tally
books have almost no vouchers after 2025-08-04, so the report's 6-month sales window is
nearly empty and almost everything classifies as dead. Use `/full_portfolio_health` to
see everything.

### Do this next, in order

1. **Make the app read the API key from the stack config.** `bootstrap.mjs` generates
   `BACKEND_API_KEY` per machine, but the desktop config keeps its own `apiKey` /
   `controlPlaneApiKey`. When they diverge the push-queue poller gets
   `[Control][Push] Queue fetch failed: HTTP 401` forever. It was aligned **by hand** on
   this machine and **will break on any fresh install**. Smallest change with the biggest
   payoff.
2. **Read credential + RLS.** The Rate screen currently authenticates to PostgREST with
   the **service_role** key, which bypasses RLS and cannot ship anywhere public. Fine on
   loopback, mandatory to fix before any exposure. `903_lockdown_anon.sql:31-37` already
   prescribes the answer: RLS policies plus targeted SELECT grants on the four tables, not
   re-granting ALL to anon. And `902_grants.sql:10-15` makes it non-optional: "if this
   stack is ever exposed beyond loopback, RLS becomes mandatory".
3. **Domain -> Cloudflare Tunnel + Access.** Until this exists Website 2 works **only in a
   browser on the Tally PC**. The user does not own a domain yet. Nothing else blocks it.
4. **Verify first-run bootstrap.** `LocalStack.bootstrap()` runs `install.ps1` when no
   config exists — **it has never actually executed**, because every run here found an
   existing config. On a genuinely fresh machine the packaged app is *supposed* to install
   itself; unproven.
5. **Backups.** Still nothing schedules a `pg_dump`. After cutover this machine is the
   only copy of the books.

### Setting up a second machine

Committed and sufficient: all source, the 17 schema files, `install.ps1` / `start.ps1` /
`stage-for-app.ps1`, and `python-dist\tallybridge-engine.exe` (so **no Python or
PyInstaller needed**).

Prerequisites: Node 18+, Git, **Windows Developer Mode ON**, and Flutter for Website 2.

```powershell
git clone https://github.com/siddharthniranjan2003/TallyBridge.git
cd TallyBridge ; git checkout feat/app-serves-local-stack
npm install
cd backend ; npm install ; npm run build ; cd ..
cd native-stack
.\fetch-vendor.ps1 -IncludePostgres ; .\scripts\trim-vendor.ps1
.\install.ps1 -NoServices -SkipVendor -DataDir "$env:ProgramData\TallyBridge\data"
cd .. ; npm run build:stack ; npx electron-builder --publish never
```

Then fill the database: a full sync from a TallyPrime on that machine, or
`copy-live-to-local.mjs` from cloud (which needs a `backend\.env` holding the live keys —
**not in git**).

**Two things will bite:**

1. **Developer Mode.** Without it *both* `electron-builder` (symlinks in the signing
   toolchain) and `flutter build web` (plugin symlinks) fail. Registry toggle, needs
   admin once, no reboot.
2. **Every key is per-machine.** `SERVICE_ROLE_KEY` and `BACKEND_API_KEY` are regenerated
   by `bootstrap.mjs` on each install, so `env/testing-local.json` must be rebuilt with
   *that* machine's values, and the desktop config's `apiKey` must be set to its
   `BACKEND_API_KEY` (see item 1 above).

### Not in git

`env.deployment` (deliberately untouched) · `native-stack\config` (this machine's
secrets) · `native-stack\data` and `%ProgramData%\TallyBridge\data` (the books) ·
`native-stack\vendor` (306 MB, re-fetch it) · `dist-client\` and `release\` (including the
2.0.0 installer) · `backend\.env` · `node_modules`. In the **aiaccountant** repo:
`env/testing-local.json` is gitignored, and the Flutter build rewrote `pubspec.lock`,
`analysis_options.yaml` and the generated plugin registrants — left uncommitted.

### Still outstanding, non-code

**Rotate the `service_role` key in `n8n/gsheet_appscript.js`** (§7). The repo is public,
the key is valid to 2036, and pushing this branch also pushed the previously-local-only
`feat/on-prem-supabase-stacks` commits — including the 68 MB installer binary — to that
public repo.

---

## 11. VERIFIED END TO END on this box (2026-08-13)

The stack was brought up and **both Flutter data paths were served from local Postgres
with zero code changes**. This is the evidence behind the §3 decision.

```
stack        postgres :5432 · postgrest :3000 · caddy :8000 · backend :3001   all up
healthcheck  1 company (K V ENTERPRISES) · caddy strips /rest/v1 · unauth 401 · anon 401
data         13,253 stock · 45,076 vouchers · 1,02,818 voucher_items · 2,904 ledgers · 517 outstanding
             (identical to the 2026-08-07 numbers — already seeded from cloud testing)
```

**Report screen path** — `GET /api/sync/reorder-levels?company_name=K V ENTERPRISES`:
```
total_items_scanned 13253 · classified 13253 · needs_reorder 6 · as_of 2026-08-13
```
and the exact call `report_screen.dart:77` makes, `/reorder-levels/R1?format=csv`,
returned the correct 9-column header and 6 data rows.

**Rate screen path** — the embedded PostgREST query from `stock_info_screen.dart:222`
(`vouchers!inner`, `order=vouchers(date).desc`, `limit=5`) returned real sale history for
`TAP 24 X 3 SET`: 5 rows, correct dates, parties, qty, rate and `discount_pct`.

> Conclusion: **no backend or Flutter data-layer porting is required.** The remaining
> work is packaging, the read-credential problem (§8.4), dual-write, and the tunnel.

### The full Tally loop, verified

The shipped `python-dist\tallybridge-engine.exe` (14.6 MB, built 2026-08-06) was run
directly with the env `sync-engine.ts:445-468` passes it, against the TallyPrime running
on this box. **Cloud was never contacted.**

```
[Tally] Connected to TallyPrime
[ODBC] Probe succeeded via TallyODBC64_9000   sections: groups, ledgers, stock_items
[Ingest][direct-masters-snapshots] -> http://127.0.0.1:8000/rest/v1   200   749 ms
[Ingest][direct-voucher-chunk-1]   -> http://127.0.0.1:8000/rest/v1   200   251 ms
{"status":"success","records":{"vouchers":1,"stock":13237},"voucher_sync_mode":"incremental"}
total_sync_ms 14979
```

Landed, exactly as reported:

| | before | after |
|---|---|---|
| `companies.last_synced_at` | 2026-08-05T18:32:38 | **2026-08-13T13:19:45** |
| `vouchers` | 45,076 | **45,077**  (+1) |
| `voucher_items` | 102,818 | **102,831**  (+13) |
| `reorder-levels` needs_reorder | 6 | **5** (recomputed on the new data) |

> **Tally -> engine -> local Postgres -> MRP report is proven end to end on one machine
> with no cloud involvement.** Note it ran in `hybrid` mode, whose `direct` transport
> writes to PostgREST and bypasses the Express backend — another reason PostgREST has to
> stay.

Python is **not installed on this box** (`python.exe` is the Microsoft Store alias stub,
`py` is absent). Use the PyInstaller exe, which is what production runs anyway.

### The Tally-built database (fresh install, no cloud)

A second, independent test: a **fresh cluster** at `%ProgramData%\TallyBridge\data`
(17 schema files, no `-Seed`), filled by a **full sync from TallyPrime**. It took
**93 seconds**, not the 30-60 minutes estimated -- 491 distinct voucher dates,
2024-04-01 to 2026-08-04, uploaded in 23 chunks over PostgREST.

```
{"status":"success","records":{"groups":47,"ledgers":2904,"vouchers":45070,"stock":13237},
 "voucher_sync_mode":"full","total_sync_ms":93415}
```

Tally vs the cloud-seeded reference:

| | cloud | from Tally | delta |
|---|---|---|---|
| ledgers | 2,904 | 2,904 | **same** |
| vouchers | 45,078 | 45,070 | -8 |
| voucher_items | 1,02,836 | 1,02,810 | -26 |
| stock_items | 13,253 | 13,237 | -16 |
| groups | 49 | 47 | -2 |
| outstanding | 517 | **0** | not fetched |

**Tally reproduces 99.98% of cloud, and is smaller on every table** -- consistent with
the cloud carrying stale rows Tally has since deleted and never reconciled away, rather
than Tally missing history. Counts alone cannot prove that; the direction and magnitude
are both right. The report served fine off it: 13,237 scanned, 6,394 priced, 2.4s.

`outstanding` is a genuine gap -- the full sync fetched 0 bytes for it and for the three
financial sections. **Website 2 does not read `outstanding`**, so it is not a blocker,
but it is a real difference from cloud.

> This also proves the disaster-recovery path: with Supabase gone, a lost local database
> is rebuilt from Tally in 93 seconds.

### The exe

- `src/main/local-stack.ts` -- spawns and supervises postgres / postgrest / caddy /
  backend as child processes, each gated on a real port probe, with graceful
  `pg_ctl -m fast stop` on quit and capped crash restarts. `start()` is idempotent: it
  adopts an already-listening service instead of spawning a duplicate.
- `src/main/index.ts` -- stack comes up after the window and before the sync engine;
  `before-quit` holds the quit until Postgres has checkpointed.
- `native-stack/stage-for-app.ps1` -- wraps `build-client-package.ps1` (which already
  flattens the schema in apply order and rewrites the shared-module imports) and adds
  the built Express backend. Asserts `config\`, `data\` and `backend\.env` are absent.
- `electron-builder.yml` -- `dist-client/tallybridge-native/` -> `resources\stack`.
- `package.json` -- version **2.0.0**, plus `build:stack` and a `dist:app` that skips
  `build:python` (PyInstaller is not installed on this box; the prebuilt engine exe is
  used as-is).

**Config durability.** `bootstrap.mjs:23` hardcodes `config/` inside the package, but in
a packaged build that is `resources\stack\config`, which an app update replaces
wholesale -- taking the Postgres password and PGDATA pointer with it and leaving the
data directory unreadable. `local-stack.ts` therefore treats
`%ProgramData%\TallyBridge\config` as authoritative and mirrors it into the stack
directory before anything reads it, and back after bootstrap. Every existing script keeps
working unmodified, which matters because they are also the by-hand repair path. The
tidier fix is a `--config-dir` argument threaded through bootstrap.mjs, install.ps1,
db-init.ps1, apply-schema.mjs and start.ps1; that is still open.

### ⚠ THE BIG ONE: a UTF-8 BOM has been stopping TallyBridge from starting since 7 Aug

`use-local-desktop.ps1:164` wrote the desktop config with
`Set-Content -Encoding utf8`. **PowerShell 5.1 writes a BOM**, electron-store hands the
raw bytes to `JSON.parse`, and `JSON.parse` rejects a leading U+FEFF:

```
SyntaxError: Unexpected token 'ï»¿', "ï»¿{ ... is not valid JSON
  at ElectronStore._deserialize -> dist\main\store.js:20
```

`store.ts` is imported at the top of `index.ts`, so this throws at **module load,
before `app.whenReady()`**. The consequences are what made it expensive to find:

- the app process starts and stays alive (Responding=True, three Electron processes)
- **nothing is written to the log at all** -- the logger is never reached
- no crash dump
- port 3002 never opens

So it presents as "the app silently does nothing", with no evidence anywhere. The
desktop config was switched to local on 7 Aug; the last log entry is **4 Aug**.
TallyBridge has not started since. `tallybridge-config.cloud-backup.json` has no BOM,
which confirms the switcher wrote it and the app did not.

This repo already knows the trap -- `build-client-package.ps1:57,95` uses
`UTF8Encoding($false)` precisely because "PowerShell 5.1's `Set-Content -Encoding utf8`
writes one, and Postgres rejects the leading U+FEFF". The desktop-config switcher was
simply missed.

**Fixed** in `use-local-desktop.ps1` (now `[System.IO.File]::WriteAllText` with
`UTF8Encoding($false)`), and the BOM was stripped from the live config. Anything else
that writes JSON from PowerShell 5.1 should be audited for the same thing.

### Three fixes in `local-stack.ts` found by running it

1. **Stale absolute paths in `config\.env`.** `VENDOR_DIR` pointed at the dev checkout
   and `BACKEND_DIR` at `C:\Program Files\TallyBridge Server\backend`, left over from an
   earlier experiment and nonexistent. A packaged build now ignores both and uses its
   bundled copies; the backend is located by finding the first candidate that actually
   contains `dist\index.js`, rather than trusting config.
2. **The backend was getting no configuration.** It reads `SUPABASE_URL`,
   `SUPABASE_SERVICE_KEY`, `API_KEY`, `SUPABASE_URL_Client`,
   `SUPABASE_SERVICE_KEY_CLIENT`, `API_KEY_CLIENT` and `PORT` from the environment, and
   would otherwise fall back to `backend\.env` -- which holds the **cloud** service key
   and is deliberately stripped from the package. All seven are now passed explicitly.
   Note `BACKEND_API_KEY` in `config\.env` is the real key; `use-native.ps1` hardcodes
   `localdevkey`, which is why a hand-run backend and the app disagree.
3. **Child stdio went to `ignore`.** Correct for avoiding the inherited-handle hang, but
   it discarded the only explanation of a service refusing to start -- the backend
   exited 1 three times with nothing to show. Now redirected to real file descriptors
   under `logs\stack\<name>.{out,err}.log`: no pipe, no lost output.

### Packaging: code signing needs elevation

`electron-builder` downloads `winCodeSign-2.6.0.7z` to get `signtool.exe`, and that
archive contains macOS **symlinks**. Creating a symlink on Windows requires elevation,
so extraction fails four times and the build dies *after* `win-unpacked` is complete:

```
ERROR: Cannot create symbolic link : A required privilege is not held by the client.
       ...winCodeSign\...\darwin\10.12\lib\libcrypto.dylib
```

Worked around by pre-extracting the cached archive with `-xr!darwin` into
`%LOCALAPPDATA%\electron-builder\Cache\winCodeSign\winCodeSign-2.6.0`. An elevated
shell, or Windows Developer Mode, would also fix it.

### Two real bugs found in `native-stack\start.ps1`

1. **Stale pid file + PID reuse silently skips PostgREST.** `Start-Tracked` (line 52)
   treats "a process with this id exists" as "our process is running". After a reboot
   Windows had recycled pid 7996 to `svchost`, so the script printed
   `postgrest already running (pid 7996)` and started nothing. Port 3000 stayed closed
   and the stack looked healthy — exactly the failure mode the comment at lines 43-45
   warns about. **Fix:** compare the process *name/path*, not just the id.
   **Workaround:** delete `logs\postgrest.pid` and re-run.

2. **Line 48 blocks forever when stdout is piped.** `pg_ctl … start | Out-Null` is the
   one path that does not use `Start-Process -WindowStyle Hidden`, so the postgres it
   launches inherits the piped stdout handle and the pipeline never sees EOF. The trap
   is documented at lines 58-66 but only fixed in `Start-Tracked`. Harmless
   interactively; hangs any script or agent that captures output. Only triggers when
   Postgres is not already running.
