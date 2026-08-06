# Session Handout — Local Windows stack: serving MRP/reorder reports from the client's own PC

> **Purpose:** read this to continue in a fresh chat with full context. Nothing has been
> implemented — this session produced **design only**, plus two published artifacts.
> Branch: `update/absolute-latest-rate-etc`. Date: 2026-08-06.
> **Working tree was not modified** except for this handout.

---

## 0. TL;DR — where we are

- **Goal:** move TallyBridge's data plane onto the client's Windows PC (data sovereignty),
  while the MRP/reorder reports stay reachable from the owner's phone anywhere.
- **Scope narrowed hard during the session:** **reports only**. Sale + Purchase are
  **paused** (RunPod GPU is off), which removed scanning, push queue, invoice images,
  pricing RPCs and Realtime from scope.
- **Two designs were published as artifacts (v1, v2).** v2 supersedes v1.
- **v2 was then superseded mid-flight** by a late user decision: *"the idea is to run the
  supabase whole locally"* — i.e. the **official self-hosted Supabase docker-compose**,
  first on the dev Mac, later on the client PC. **v3 has not been written yet.**
- **One question is open and blocking v3** (see §8): how much of the 13-service compose to run.

**Artifacts published this session**
| Ver | URL | Status |
|---|---|---|
| v1 | `https://claude.ai/code/artifact/79c9e243-187b-41c5-835d-4d6f986ee6cd` | superseded, has a known defect |
| v2 | `https://claude.ai/code/artifact/6060d5a1-0642-4dfe-98a0-3e264d85a1d0` | superseded by the v3 pivot |

Full contents of both are reproduced in §5 and §6 so a new chat needs neither URL.

---

## 1. What the MRP report is (context pulled at the start of the session)

"MRP" in this codebase is **not** Material Requirements Planning. It is the
**`purchase_rate` column of the reorder-levels / Inventory Intelligence report** —
the *last purchase rate* per stock item. The name survives in `MRP_API_KEY`
(Flutter config) and commit `652a440 "mrp update(purchaseRate)"`.

- **Before:** `purchase_rate` = 1-month average (`lastMonthPurchase / purchaseQty1m`)
- **Now:** the stored `voucher_items.rate` from each item's **most recent purchase
  voucher line, across all history**

### Where it lives — all in `backend/src/routes/sync.ts`

| Piece | Line |
|---|---|
| `reorderSupabase` (client-project Supabase override) | `sync.ts:19` |
| `requireClientApiKey` (timingSafeEqual vs `API_KEY_CLIENT`) | `sync.ts:28` |
| `isPurchaseVoucherType` (contains "purchase", excludes "order") | `sync.ts:862` |
| `INVENTORY_REPORT_META` R1–R7 | `sync.ts:875` |
| `INVENTORY_SCENARIO_META` (15 scenarios) | `sync.ts:890` |
| **`fetchLastPurchaseRateByItem()` — the MRP helper** | `sync.ts:1252` |
| `classifyInventoryScenarioV2(A, P, S)` | `sync.ts:1318` |
| `buildInventoryIntelligenceReport()` | `sync.ts:1384` |
| `purchase_rate` assignment | `sync.ts:1499` |
| `effectiveClosingStockRaw` fallback | `sync.ts:1509` |
| Routes `/reorder-levels` and `/reorder-levels/:reportKey` | `sync.ts:3189`, `sync.ts:3216` |

### MRP algorithm
Fetch all non-cancelled `vouchers` for the company ordered `date DESC, id DESC` → rank
(0 = newest) → keep purchase types → fetch their `voucher_items(stock_item_name, rate,
voucher_id)` in chunks of 100 → per item keep the rate from the lowest rank, skipping
`rate <= 0`. **Gross rate, ignores `discount_pct`. No date window, no cache** — it runs
in full on every report call.

### Reports
| reportId | key |
|---|---|
| R1 | `ACT_NOW` |
| R2 | `HERO_SKU_HEALTH` |
| R3 | `DEAD_CAPITAL` |
| R4 | `BUYING_MISTAKES` |
| R5 | `WIND_DOWN` |
| R6 | `RISK_WATCH` |
| R7 | `FULL_PORTFOLIO_HEALTH` |

### Two things in `md_files/session_handout_mrp_client_supabase.md` are STALE
1. **§2 "classification uses native booked amounts, NOT `purchase_rate`" is no longer true.**
   Commit `30d7fa9` added
   `effectiveClosingStockRaw = closingStockRaw > 0 ? closingStockRaw : (closingStockAmountRaw ?? 0)`
   (`sync.ts:1509`). Tally stock valuation stopped being synced (per-item valuation was a
   TallyPrime freeze source — `tally_client.py:680`, `sync_main.py:1473`), so
   `stock_items.closing_value` is 0 and the S signal now falls back to
   `closing_qty × MRP`. **MRP now decides which bucket an item lands in.**
2. **Deployment target moved.** That handout has the report on
   `tallybridge-backend-950406969086` (`tallybridge-test-ocr`). `.env.deployment` now
   points the client at `tallybridge-backend-822222628942` (deployment.riplara /
   `tally-bridge-deployment-env`).

---

## 2. Locked decisions (full Q&A trail)

Answers are the user's, verbatim where they deviated from the offered options.

| # | Question | Decision |
|---|---|---|
| 1 | What drives "run it local"? | **Data sovereignty, internet OK.** Books stay on premises; Firebase login, Sentry, updates may stay cloud |
| 2 | Where must the phone work? | **Anywhere, via Tailscale private tunnel** |
| 3 | What's the machine? | Same PC as TallyPrime. *"will ask the client for upgrade to 16 gb ram, so tell me for both cases"* |
| 4 | Which app parts must work locally? | **Reports only, for now** |
| 5 | Which stack? | *"A, but also write B to show client"* |
| 6 | How does local Postgres get data? | **Dual-write via a mirror** |
| 7 | Which web app is Firebase-hosted? | **AiAccountant Flutter web build** (`aiaccountant-b60ed`) |
| 8 | Staging? | *"focus on reports only for now"* |
| 9 | Who opens the web app? | **Only the owner, 1–2 devices** |
| 10 | What about RunPod OCR? | *"we are pausing sale feature and purchase feature, which requires runpods"* |
| 11 | Drop the mirror? | **Keep the mirror anyway** |
| 12 | Web or APK? | **Both** |
| 13 | App UI rework? | **Unhide Reports, hide Sale/Purchase, land on Reports** |
| 14 | Cloud stack after cutover? | **Keep it live for other clients** |
| 15 | How to split the hosted web app? | *"cant we keep the firebase web app as is"* → **yes, kept as-is** (see §2.1) |
| 16 | Who runs the box? | **Haven't decided yet** — OPEN |
| 17 | Backups? | *"skip"* — OPEN, flagged as biggest risk |
| 18 | Is the pause global? | *"for the client yes... we dont have any other client so"* — **one client only** |
| 19 | Companies? | **One — `K V ENTERPRISES`** |
| 20 | Docker on the client PC? | *"no i need to see the supabase ui"* |
| 21 | Where should Studio run? | *"lets keep it simple i may need first in my testing env, then will shift to client pc, but the idea is to run the supabase whole locally"* → **THE v3 PIVOT** |
| 22 | Compose profile? | **INTERRUPTED — still open.** See §8 |

### 2.1 Why the hosted web app can stay untouched
Because the **mirror is kept**, the cloud DB holds the same data as local. So:
- **Hosted web app** → cloud backend → cloud DB *(fed by the mirror)* — unchanged
- **Installed APK** → Tailscale → the box → local Postgres — the sovereign path

`Config` is compile-time (`String.fromEnvironment`), so one hosted build can only point at
one backend. Keeping the cloud copy accurate means we never have to point it elsewhere.
**The day the mirror is switched off (stage 2), that hosted build goes stale** and must be
split — separate Firebase Hosting site, or a runtime-configurable URL. Deferred, not avoided.

### 2.2 Sovereignty is DEFERRED, not delivered, in stage 1
Keeping the mirror means the books **still land in the cloud**. Stage 1 puts an
authoritative copy on the premises and makes it the system of record, but it does not mean
the data is only on their machine. **Do not sell stage 1 as "your data is now local."**

---

## 3. Verified dependency map

### Easier than expected (all verified by reading the code)
- Python engine talks **raw PostgREST over `requests`** (`cloud_pusher.py:224`), no Supabase SDK
- Express backend uses **only** `.from().select()/.insert()` — zero Auth/Storage/Realtime/RPC
- `backend/full_schema.sql` + all 14 `supabase/migrations/*.sql` are **vanilla Postgres** —
  only `pgcrypto`/`gen_random_uuid()`. **No RLS policies, no `auth.uid()`, no `storage.*`**
- `supabase/functions/ingest-sync` (Edge Function) is **NOT in the live path**
  (`cloud_pusher.py:210` says it stalled ~37%; live path is `_post_direct_via_postgrest`)
- GCS invoice storage is **optional**, gated on `INVOICE_BUCKET` (`gcs.ts:11`)
- The invoice image route **never touches the DB** (`sync.ts:2424` → straight to GCS)
- `SYNC_INGEST_MODE` defaults to **`render`** (`cloud_pusher.py:57`) → Python talks only to Express
- **Reports path needs only Postgres + Express + `x-api-key`** — no Firebase, no Realtime, no OCR

### Blockers / constraints (verified)
- Flutter app hits Supabase **directly**: `.channel()` at `history_screen.dart:75` and
  `stock_item_create_sheet.dart:148`; `client.rpc()` at `sale_pricing.dart:73`;
  `.from()` at `queue_screen.dart:190`, `stock_info_screen.dart:138/159/479`
  — **all out of scope now that Sale/Purchase are paused**
- **Firebase phone-OTP is the login gate** (`auth_gate.dart`, `main.dart:22`) — needs internet
- Parsing/OCR depends on **RunPod GPU** (Nanonets-OCR2-3B, `parsing/server/handler.py:105`)
  and 4 RPCs (`get_distinct_party_names`, `get_latest_rates_for_party`,
  `get_latest_sale_rates_for_items`, `get_latest_party_discounts_for_items`) — **paused**
- App URLs are **compile-time** `String.fromEnvironment` (`config.dart`)
- `AndroidManifest.xml` has **no** `usesCleartextTraffic`/`networkSecurityConfig` →
  Android 9+ blocks `http://192.168.x.x:3001`. **Tailscale HTTPS removes this problem**
- `ApiClient` (`BACKEND_BASE_URL`) is used by exactly **two** paths: the report call
  (`report_screen.dart:76`) and invoice images (`getBytes('/api/sync/push-queue/{id}/image/{n}')`)
- Desktop app is a **close-to-tray Electron app** with user-toggleable `openAtLogin`
  (`ipc-handlers.ts:825`) — needs a logged-in Windows session, as does TallyPrime

### Third-party facts checked live (2026-08-06)
- **PostgREST v14.16** ships `postgrest-v14.16-windows-x86-64.zip` (15.9 MB) — official Windows binary
- **Tailscale pricing:** Personal **$0** (6 users, unlimited devices, 50 tagged resources) but
  *"only suitable for non-commercial use"*; **Standard $8/user/mo**; Premium $18/user/mo.
  This deployment needs **1 user**
- **`tailscale serve localhost:3001`** terminates HTTPS with an auto-provisioned real cert.
  Windows support not explicitly documented (their docs only call out macOS caveats) → **verify on the box**
- **`supabase-js` builds `${baseUrl}/rest/v1`, `/realtime/v1`, `/auth/v1`, `/storage/v1`,
  `/functions/v1`** (`SupabaseClient.ts:287–291, 341`)
- **Supabase Studio** has **no `depends_on`** in the current compose and sets
  `ENABLED_FEATURES_LOGS_ALL: "false"`. It needs `STUDIO_PG_META_URL: http://meta:8080`
  and `SUPABASE_URL: http://kong:8000`. `meta` (postgres-meta) has
  `depends_on: db (service_healthy)` and takes `PG_META_DB_HOST/PORT/NAME/USER/PASSWORD`
  → **minimum for Table/SQL editor = `studio` + `meta` + Postgres**

### The infrastructure inventory
| Piece | Cloud dep | Verdict for this project |
|---|---|---|
| TallyPrime | none | already local |
| Electron app + Python engine | HTTPS to backend | already local, just repoint |
| Postgres + PostgREST | Supabase cloud | **moves local** |
| Supabase Realtime | cloud | **out of scope** (Sale/Purchase paused) |
| Supabase RPC functions (4) | cloud | **out of scope** |
| Express backend | Cloud Run | **moves local** (`npm run build` → `node dist/index.js`) |
| GCS invoice images | cloud, optional | **out of scope** |
| Firebase Auth (phone OTP) | cloud | **stays cloud** |
| Firebase Hosting (Flutter web) | cloud | **stays cloud, unchanged** |
| Parsing/OCR + RunPod | cloud GPU | **paused** |
| Sentry | cloud, optional | stays |

---

## 4. Repo / deployment facts worth having

- **Hosted web app** = Flutter web build of AiAccountant.
  `AiAccountant/firebase.json` → `public: "build/web"`, SPA rewrites.
  `.firebaserc`: `default`/`prod` = `aiaccountant-b60ed`, `testing` =
  `tallybridge-testing-env-636d4`, `deployment` = `tallybridge-deployment-env`
- `TallyBridge/frontend/` is a **separate React dashboard** (Dashboard, Inventory,
  Outstanding, Parties, P&L, Sales, Balance Sheet) hitting `VITE_BACKEND_URL` with
  `x-api-key` — **dev-only, not the hosted app**
- Commit `d8763e7` hid **Purchase and Report**; camera opens straight to Sale.
  With Sale/Purchase paused this leaves the client unable to reach the only live feature
- **Key reuse smell:** `API_KEY_CLIENT` is a Supabase *publishable* key reused as a shared
  secret, and the identical string also appears as `SUPABASE_ANON_KEY`, `BACKEND_API_KEY`
  and `ACTIVATE_API_KEY` in `.env.deployment`. **One key doing five jobs — rotate for local**
- **`SUPABASE_URL_Client` is mixed case and read literally** (`sync.ts:20`). Setting
  `SUPABASE_URL_CLIENT` makes the reorder endpoint silently fall back to the shared client
  and serve **cloud** data while looking healthy. Highest-value trap in the whole project

---

## 5. DESIGN v1 — full content (superseded; contains a defect)

> Published at `https://claude.ai/code/artifact/79c9e243-187b-41c5-835d-4d6f986ee6cd`
> **Do not build from this.** Retained because the client-facing A-vs-B comparison is still useful.

### v1 · The decision
| | |
|---|---|
| Driver | Data sovereignty; machine keeps internet |
| Reach | Tailscale, anywhere |
| The box | Same PC as Tally, sized for 8 GB |
| Scope | Reports first |
| Stack | Option A, with B documented for the client |
| Ingest | Dual-write mirror |

### v1 · The seam
The `_CLIENT` override built for MRP already gives one backend process **two independent
database targets**: `reorder-levels` reads `SUPABASE_URL_Client`, everything else reads
`SUPABASE_URL`. Reports can be served from local Postgres while other routes still read
cloud — **with no code change**.

### v1 · Topology
Premises box containing: TallyPrime :9000 → TallyBridge desktop → Express :3001 →
postgrest.exe :3000 → PostgreSQL :5432. Everything binds to `127.0.0.1`. Exactly two
crossings of the premises boundary: an outbound mirror of the sync payload to the cloud
backend, and an inbound Tailscale tunnel. No port forwarding, no firewall holes.

### v1 · Data flow (three states)
- **Today:** Tally → Cloud DB; phone reads reports from Cloud
- **Stage 1:** Tally → Local DB; Local ⇢ mirror ⇢ Cloud; phone reads Local; Cloud still feeds scan+pricing
- **Stage 2:** Tally → Local DB only; Cloud retired

**Mirror rules:** (1) local commits first; (2) the mirror can never fail the sync —
log and drop; (3) same body, same key.

**Accepted trade-off:** a dropped mirror call means the cloud silently misses a sync window
and scan-pricing quotes stale rates. Mitigation: a mirror-failure counter surfaced in the
desktop app, plus the next full sync re-sending everything.

### v1 · What runs on the box
| Component | Port | Install | RAM (est.) |
|---|---|---|---|
| PostgreSQL 16 | 5432 | EDB installer, Windows service | 150–250 MB |
| PostgREST 14.16 | 3000 | Windows zip + NSSM | 40–80 MB |
| Express backend | 3001 | `node dist/index.js` via NSSM | 100–150 MB |
| Tailscale | 443 | Official installer + `tailscale serve` | ~50 MB |
| TallyPrime / TallyBridge desktop | 9000 / 3002 | already installed | unchanged |

≈ **400–500 MB** new resident memory.

### v1 · Database setup
```sql
CREATE ROLE anon NOLOGIN;
CREATE ROLE authenticated NOLOGIN;
CREATE ROLE service_role NOLOGIN BYPASSRLS;
CREATE ROLE authenticator LOGIN PASSWORD '…' NOINHERIT;
GRANT anon, authenticated, service_role TO authenticator;

psql -d tallybridge -f backend/full_schema.sql
psql -d tallybridge -f supabase/migrations/<each, by date>
```
No RLS policies exist, so plain grants suffice — we reproduce Supabase's role *names*, not
its policy engine.

### v1 · Configuration
| Variable | Value | Effect |
|---|---|---|
| `SUPABASE_URL` | `http://127.0.0.1:3000` | Ingest lands local |
| `SUPABASE_SERVICE_KEY` | locally-signed `service_role` JWT | auth to PostgREST |
| `SUPABASE_URL_Client` | `http://127.0.0.1:3000` | Reports read local — **case-sensitive** |
| `SUPABASE_SERVICE_KEY_CLIENT` | same JWT | reports read local |
| `API_KEY_CLIENT` | **new** 32-byte secret | key the phone sends |
| `TB_MIRROR_SYNC_URL` | cloud backend `/api/sync` | **new** — enables mirror |
| `TB_MIRROR_API_KEY` | existing cloud `API_KEY` | **new** — authenticates mirror |
| `FIREBASE_SERVICE_ACCOUNT_B64` | same as cloud | verifies app token on image requests |
| `INVOICE_BUCKET` | same as cloud | keeps invoice images working |
| `GOOGLE_APPLICATION_CREDENTIALS` | path to GCP key | **new** — replaces Cloud Run ADC |

### v1 · Exposure
```bash
tailscale serve --bg localhost:3001
# → https://<machine>.<tailnet>.ts.net
```

### v1 · The app change
```json
// env/local.json
{ "FLAVOR": "local",
  "BACKEND_BASE_URL": "https://<machine>.<tailnet>.ts.net",
  "MRP_API_KEY": "<the new API_KEY_CLIENT>" }
```
`flutter build apk --dart-define-from-file=env/local.json`

**Consequence:** repointing `BACKEND_BASE_URL` also moves invoice-image fetching to the
local box. That route never touches the DB, so it works given bucket name + GCP key +
Firebase service account. Miss those and reports work while images quietly 404.

### v1 · Option A vs B (the client-facing comparison)
- **A — Native:** 3 processes, ≈450 MB, runs on 8 GB, **₹0/mo**
- **B — Docker:** 8 processes, ≈3.2 GB, needs 16 GB, **₹0/mo\*** (Docker Desktop free at this
  size) + ~₹3–5k RAM upgrade
- **Tailscale, both:** ₹0 on Personal (non-commercial licence) or **$8/user/mo** Standard

*B is A plus five services, four of which go unused:* Storage → replaced by cloud storage,
GoTrue → by Firebase, Kong → by binding to loopback, Studio → by any Postgres client.
**Only Realtime does work we'd otherwise have to replace by hand.**

Recommendation was **A**, and the reason was **failure surface, not memory**: Docker Desktop
on a shop PC that gets rebooted and logged out is a service that will stop and not come back.

### v1 · What can break
| Failure | Effect | Response |
|---|---|---|
| Internet down | mirror fails, cloud falls behind | local unaffected; failures counted, not swallowed |
| PC asleep/off | no reports, no sync | disable sleep, auto-start on power |
| Tailscale not running | phone can't reach reports | install as system service, unattended mode |
| Postgres won't start | sync + reports fail | auto-restart; desktop app already skips cleanly |
| Disk fills or lost | **the books are gone** | nightly `pg_dump` to a second disk + encrypted off-site |
| Wrong-cased env var | reports serve cloud data, looking healthy | startup log of resolved hosts + health check |

### v1 · Proving it works
1. Schema parity — restore cloud dump, row counts must match
2. **Report parity — all 7 reports as CSV from cloud and local must be byte-identical** (acceptance gate)
3. Mirror integrity — sync, compare counts; then pull the cable and confirm local succeeded and the failure was reported
4. Reach — mobile data, foreign WiFi, router rebooted
5. Restart — reboot without logging in, load a report

### v1 · Stages
- **1 (≈6 days)** — reports off local data. Done when the owner opens ACT NOW from outside the shop and numbers match cloud
- **1.5 (≈2 days)** — direct SQL; `fetchLastPurchaseRateByItem` currently pulls every voucher
  and every voucher line over REST per call and groups in JS; one `DISTINCT ON (stock_item_name)` replaces it
- **2 (2–3 weeks)** — retire the cloud DB; app's own Supabase client points local; Realtime or polling; repoint parsing RPCs

### v1 · Open questions
Backups owner · the scan/RunPod sovereignty contradiction · who administers the box ·
multi-company · uptime expectation

---

## 6. DESIGN v2 — full content (superseded by the v3 pivot)

> Published at `https://claude.ai/code/artifact/6060d5a1-0642-4dfe-98a0-3e264d85a1d0`

### v2 · The decision
| | |
|---|---|
| Driver | Data sovereignty; machine keeps internet |
| Scope | **Reports only.** Sale/Purchase paused — scanning, queue, images, pricing all out |
| Client | One client, one company — `K V ENTERPRISES` |
| The box | Same PC as Tally; designed for 8 GB, 16 GB welcome not required |
| Reach | Tailscale, owner only, 1–2 devices |
| Entry points | **Both** — hosted web app unchanged, plus a new local-flavour APK |
| Ingest | Dual-write mirror |
| Cloud stack | Stays live — rollback path and future clients |

### v2 · What changed from v1 — three corrections

**1. DEFECT — v1 would not have worked.** v1 pointed the code at a bare `postgrest.exe`.
`supabase-js` builds `${baseUrl}/rest/v1` (`SupabaseClient.ts:341`) and the Python engine
does the same (`cloud_pusher.py:225`), but **plain PostgREST serves tables at `/`**.
Every query would have 404'd. Fix: a path-rewriting gateway in front.

**2. Option B eliminated.** With Sale/Purchase paused the reports path goes entirely
through Express, leaving Realtime, GoTrue, Storage, Kong and Studio with nothing to do.

**3. The mirror's job changed.** It no longer feeds scan-pricing (paused). It is now
(a) the rollback path and (b) **what lets the hosted web app carry on completely untouched.**

### v2 · Two paths, one dataset
- **Installed APK** → Tailscale HTTPS → local Express → local Postgres
- **Browser (hosted web app, UNCHANGED)** → public HTTPS → cloud backend + Supabase
- Local Postgres ⇢ *mirrors* ⇢ cloud, which is what keeps both showing the same numbers
- TallyPrime + sync engine → local Postgres

### v2 · Inside the box
Chain: TallyBridge desktop → `/api/sync` → **Express :3001** → supabase-js →
**Caddy :8000** → *rewritten* → **PostgREST :3000** → SQL → **PostgreSQL :5432**

The callout: `supabase-js` asks for `/rest/v1/vouchers?select=…`; PostgREST serves
`/vouchers?select=…`; **Caddy strips the prefix — without it, every query 404s.**

| Component | Port | Install | RAM (est.) |
|---|---|---|---|
| PostgreSQL 16 | 5432 | EDB installer, Windows service | 150–250 MB |
| PostgREST 14.16 | 3000 | `postgrest-v14.16-windows-x86-64.zip` + NSSM | 40–80 MB |
| **Caddy** | 8000 | single Windows binary via NSSM | ~30 MB |
| Express backend | 3001 | `node dist/index.js` via NSSM | 100–150 MB |
| Tailscale | 443 | official installer + `tailscale serve` | ~50 MB |

≈ **400–560 MB**.

### v2 · Why there's a gateway
Supabase's hosted API puts PostgREST behind **Kong**, which maps `/rest/v1/*` onto it.
Every client we own was written against that convention. PostgREST has **no URL-prefix
option**, so something must do the mapping.

```
# Caddyfile — loopback only, no TLS needed here
:8000 {
  handle_path /rest/v1/* {
    reverse_proxy 127.0.0.1:3000
  }
}
```
`handle_path` strips the matched prefix before proxying; `handle` does not.

**Quirk for later:** if ingest mode ever switches from `render` to `direct`, the Python
engine derives its PostgREST base by splitting `SYNC_INGEST_URL` on `/functions/`
(`cloud_pusher.py:224`), so that variable must be shaped to suit. In stage 1 the mode
stays `render`, so this doesn't arise.

### v2 · Configuration (shrank to 7 vars)
| Variable | Value | Effect |
|---|---|---|
| `SUPABASE_URL` | `http://127.0.0.1:8000` | ingest lands local, via Caddy |
| `SUPABASE_SERVICE_KEY` | locally-signed `service_role` JWT | auth to PostgREST |
| `SUPABASE_URL_Client` | `http://127.0.0.1:8000` | reports read local — **capital C** |
| `SUPABASE_SERVICE_KEY_CLIENT` | same JWT | reports read local |
| `API_KEY_CLIENT` | **new** 32-byte secret | the key the APK sends |
| `TB_MIRROR_SYNC_URL` | cloud backend `/api/sync` | **new** — enables mirror |
| `TB_MIRROR_API_KEY` | existing cloud `API_KEY` | **new** — authenticates mirror |

**No GCP key, no `INVOICE_BUCKET`, no Firebase service account** — invoice images are out of
scope with Purchase paused, and the reports route authenticates on `x-api-key` alone.

### v2 · App changes
Three reversible edits (flag flips, not deletions): unhide **Report**; hide **Sale** and
**Purchase**; land on **Reports** instead of the camera. All Sale/Purchase code stays in the
tree. Because there is only one client, these go in the shared build. Plus `env/local.json`
as a fourth flavour beside `testing`/`deployment`/`prod`.

### v2 · From the client's chair
**No Docker, nothing new to open.** Four **Windows Services** — same category as the print
spooler; they start with the machine, run with nobody logged in, no window, no icon.

- **What the accountant sees:** TallyPrime (unchanged), TallyBridge tray icon (unchanged),
  **Tailscale — the one new icon**
- **What runs underneath, invisible:** PostgreSQL, PostgREST, Caddy, Express API
- **A normal day:** identical to today
- **Install day:** ~2 hours, we do it, a few wizards + one Tailscale sign-in each on PC and phone
- **If a report won't load — three checks:** (1) is the PC on and awake? (2) is Tailscale
  connected on the phone? (3) on the PC? Anything past that is ours

**Four things that bite a non-technical user:**
1. **Tailscale keys expire by default** — the box silently drops off the tailnet months
   later and reports stop with no visible cause. **Disable key expiry for both devices.**
   Most likely quiet failure mode of the whole deployment
2. **Android shows a permanent VPN key icon** — non-technical users switch these off
3. **Windows won't trust the new binaries** — `postgrest.exe`, `caddy.exe`, `nssm.exe` are
   unsigned; SmartScreen warns at install and AV may quarantine *weeks later*, presenting as
   "reports stopped working". Add AV exclusions at setup
4. **"I switch it off at night to save electricity"** — reasonable, and it means no reports
   from outside until morning

**One behaviour worth knowing:** the four services survive a login-less reboot, so reports
keep working — but TallyPrime and the TallyBridge tray app need a desktop session
(`ipc-handlers.ts:825`), so **the figures stop updating** until someone logs in.

### v2 · What can break
Same as v1 plus: **Tailscale key expires** (box drops off tailnet weeks later),
**antivirus quarantines a binary** (fails long after a clean install),
**Caddy or PostgREST down** (404s / connection errors).

### v2 · Stages
- **1 (≈6 days)** — Postgres, PostgREST, Caddy, backend service, mirror, Tailscale, app UI
  rework + local flavour, migration, parity testing
- **1.5 (≈2 days)** — direct SQL, **drops PostgREST and Caddy entirely**
- **2 (when scanning returns)** — switch the mirror off. This is where sovereignty is actually
  delivered, and where the deferred bill arrives: the hosted web app goes stale the moment the
  mirror stops and needs its own hosting site or a runtime-configurable URL

### v2 · Open questions
- **Backups — deferred, still the biggest risk.** The mirror is an accidental off-site copy
  today but disappears in stage 2. Needs an answer **before go-live**
- Who runs the box (migrations, builds, logs) — Tailscale can carry remote access with consent
- Uptime — is the client willing to leave the PC on?
- The 16 GB upgrade — **no longer required** under v2; Option A fits 8 GB

---

## 7. DESIGN v3 — the pivot (NOT YET WRITTEN)

Triggered by: *"lets keep it simple i may need first in my testing env, then will shift to
client pc, but the idea is to run the supabase whole locally"*

### What v3 changes
**Stack becomes the official self-hosted Supabase `docker-compose` + the Express backend.**

Why this is actually better reasoning than v2's:
1. **Kong does the `/rest/v1` routing natively** — the Caddy hack disappears; that's literally
   what Kong is there for
2. **Studio comes in the box** — the user's stated requirement
3. **Zero divergence** between the dev Mac and the client PC: same compose file, two machines
4. **Stage 2 gets cheap** — Realtime/GoTrue/Storage are already there when Sale/Purchase return

### What v3 costs — must be stated to the client
| | v2 (native) | v3 (Docker) |
|---|---|---|
| Docker on the client PC | none | **required**, needs a logged-in Windows session |
| "One new tray icon" story | holds | **broken** — Docker Desktop is visible and prompts for updates |
| Login-less reboot | **reports still serve**, figures go stale | **nothing serves at all** |
| RAM | ~400–560 MB | ~1.5 GB trimmed / ~3–4 GB stock |
| 16 GB upgrade | optional | **likely required if running stock** |
| `/rest/v1` gateway | Caddy (hand-rolled) | Kong (upstream) |

Softening factor: TallyPrime and the tray app **already** need a logged-in session for
syncing, so the session requirement is not wholly new — but reports currently survive a
login-less reboot and would not any more.

### Services in the official compose (13)
`studio`, `kong`, `auth` (GoTrue), `rest` (PostgREST), `realtime`, `storage`, `imgproxy`,
`meta` (postgres-meta), `functions` (edge-runtime), `analytics` (Logflare), `db` (Postgres),
`supavisor`, `vector`

For reports-only, the needed subset is **`db`, `kong`, `rest`, `meta`, `studio`**.
`analytics` + `vector` (Logflare) are the heaviest and most commonly stripped.

### Still true in v3 (carry forward from v2)
Everything in §2 (decisions), §3 (dependency map), §4 (repo facts), the mirror design, the
app UI rework, `env/local.json`, Tailscale exposure, the parity acceptance gate, the four
non-technical-user gotchas, and both open questions (backups, who runs the box).

---

## 8. THE OPEN QUESTION — this is where the session stopped

**The official compose is 13 services. For a reports-only scope, how much do we actually run?**

Options put to the user (interrupted before answering):

| Option | Trade-off |
|---|---|
| **Stock on the Mac, trimmed on the client PC** *(was my recommendation)* | Simplest while developing, lean in production. Two configurations to keep in step |
| **Stock everywhere** | Truly simplest, upstream defaults, nothing to maintain. ~3–4 GB → the 16 GB upgrade goes from optional back to **required** |
| **Trimmed everywhere** | Cut `analytics`, `vector`, `storage`, `imgproxy`, `realtime`, `functions` from the start — nothing in scope uses them. ~1.5 GB, fits 8 GB. Small divergence from upstream to re-check on each version bump |
| **Stock now, measure before trimming** | Avoids optimising against guessed numbers — **all RAM figures in this document are estimates, not measurements** |

**Answer this first in the new chat, then write v3.**

---

## 9. Corrections made during the session (so they aren't re-made)

1. **v1's bare-PostgREST plan was wrong** — `supabase-js` and the Python engine both append
   `/rest/v1`; plain PostgREST serves at `/`. Needs Kong (v3) or Caddy (v2)
2. **"Studio is replaced by any Postgres client" was wrong** — the user needs the actual
   Supabase UI, which is what triggered the v3 pivot
3. **The MRP handout's §2 is stale** — MRP now *does* drive classification (see §1)
4. **Option B's justification changed twice** — needed in v1 (Realtime), dead in v2
   (reports-only), back in v3 (Studio + simplicity)

---

## 10. Key files

| File | Why it matters |
|---|---|
| `backend/src/routes/sync.ts` | All reorder/MRP logic, the `_CLIENT` seam, both report routes |
| `backend/src/db/supabase.ts` | Shared client (`SUPABASE_URL`/`SUPABASE_SERVICE_KEY`) |
| `backend/src/middleware/auth.ts` | `requireApiKey` — Firebase JWT then API-key fallback |
| `backend/src/db/gcs.ts` | Invoice images; optional, gated on `INVOICE_BUCKET` |
| `backend/full_schema.sql` | Vanilla Postgres; `voucher_items.rate` @ line 99 |
| `supabase/migrations/*.sql` | 14 migrations, all portable |
| `backend/Dockerfile` | `tsc` → `node dist/index.js` |
| `src/python/cloud_pusher.py` | PostgREST transport, `_postgrest_base()` @ 224, mode @ 57 |
| `src/python/tally_client.py` | `get_stock_items()` @ 677 — valuation deliberately not fetched |
| `src/main/ipc-handlers.ts` | `openAtLogin` @ 825 |
| `AiAccountant/lib/core/config.dart` | Compile-time URLs; `mrpApiKey` @ 34 |
| `AiAccountant/lib/services/api_client.dart` | `x-api-key` @ 35, `getRaw` @ 95 |
| `AiAccountant/lib/features/report/report_screen.dart` | The report call @ 76 |
| `AiAccountant/firebase.json`, `.firebaserc` | Hosting config + project aliases |
| `.env.deployment` | Where the five-jobs-one-key problem is visible |
| `md_files/session_handout_mrp_client_supabase.md` | Prior MRP handout — **§2 is stale** |

---

## 11. Next steps

1. **Answer §8** (compose profile) — blocks everything
2. **Write design v3** as an artifact, carrying forward §2–§4 and the v2 content that survives
3. **Commit the spec** to `docs/superpowers/specs/2026-08-06-local-windows-stack-design.md`
   (the brainstorming flow stopped before this step)
4. **Then** turn it into an implementation plan
5. Unresolved before go-live, regardless of design: **backups** and **who administers the box**

### Not yet done
- No code written, no branch created, no migration run
- The mirror (`TB_MIRROR_SYNC_URL`) does not exist yet — it is the only new backend code in the plan
- `env/local.json` does not exist
- The app UI rework (unhide Reports / hide Sale + Purchase) has not been started
