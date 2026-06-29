# TallyBridge + aiaccountant — Architecture & Path to Production

> **Provenance:** Generated 2026-06-29 by a multi-agent Claude Code workflow
> (`tallybridge-arch-and-prod-audit`, run `wf_07fda478-ddb`): 6 parallel agents
> mapped each subsystem's architecture, then 6 assessed production-readiness.
> Full per-agent structured detail is in the sibling file
> `architecture_and_prod_audit_2026-06-29.raw.json`.
> Severity/file:line claims were point-in-time — re-verify against current code
> before acting. One agent (data-integrity/multi-tenancy) failed its structured
> output; that dimension is synthesized from the backend, security, CI/CD, and
> observability findings.

---

## Part 1 — System Architecture

Six moving parts across two repos. Three run at the **client site**, three run in
**the cloud**, and they meet at one Supabase project per client.

```
┌──────────────── CLIENT SITE (Windows PC) ─────────────────┐
│  TallyPrime ◄──XML/HTTP :9000──► TallyBridge (Electron)    │
│  (single-threaded!)              ├─ main process (TS)      │
│                                  │   ├ sync-engine ──spawn──┐
│                                  │   ├ local-push :3002      │
│                                  │   ├ push-queue-poller     ▼
│                                  │   └ updater         Python engine (per-run subprocess)
│                                  └─ renderer (React 19)   ├ tally_client  (READ  :9000)
│                                                           ├ cloud_pusher  (→ cloud)
│                                                           └ tally_pusher  (WRITE :9000)
└────────────────────┬───────────────────────────┬─────────────────────────────────────┘
       HTTPS (push proxy → :3002)        HTTPS (sync ingest / push-queue)
                     │                           │
        ┌────────────┴─────────── GCP Cloud Run (asia-south1) ───────────┐
        │  Express backend  :3001  (sync.ts, 2992 LOC)                   │
        │  Parsing/OCR  (stdlib http.server) ──► RunPod serverless GPU   │
        │                                        (Nanonets / MiniCPM-V)  │
        └────────────────────────────┬───────────────────────────────────┘
                  service_role key (bypasses RLS) │ PostgREST
                                                  ▼
                    Supabase — ONE PROJECT PER CLIENT
                    (yynuu = testing · ztugw = client prod)
                                                  ▲
                    anon/publishable key + RLS    │ PostgREST + Realtime
        ┌─────────────────────────────────────────┴──────────┐
        │  aiaccountant (Flutter, Android + Web)              │
        │  Firebase Phone-OTP auth · scan→parse→edit→push     │
        └──────────────────────────────────────────────────────┘
```

### The six components

| # | Component | Stack | Role |
|---|-----------|-------|------|
| 1 | **Electron main** (`src/main`, ~2,400 LOC TS) | electron-store, electron-updater | Orchestrator. Spawns the Python engine per company, runs the loopback push server (:3002), polls the cloud push-queue every 5s, owns config + tray + auto-update. Talks to the renderer over a context-isolated IPC allow-list. |
| 2 | **React renderer** (`src/renderer`, React 19) | Vite, HashRouter | Thin control surface: add/remove companies, settings, live sync log. **No direct access** to Tally/cloud/Supabase — everything via IPC. Good security posture. |
| 3 | **Python sync engine** (`src/python`, ~5,700 LOC) | requests, xmltodict, optional ODBC | The real work. Reads Tally XML on :9000 (ODBC fast-path), batches vouchers into 7-day windows with recursive split-on-timeout, uploads via `render` (→ backend) or `direct/hybrid` (→ Supabase RPCs), and pushes vouchers back into Tally. |
| 4 | **Express backend** (`backend/src`, Cloud Run) | Express 5 ESM, Supabase, Firebase, GCS | One giant `sync.ts` (2,992 LOC): ingest, push-queue lifecycle, dashboard reads, and an in-process inventory "reorder-levels" report. Auth = Firebase JWT **or** one shared API key. Multi-tenancy = one Supabase project per client + manual `company_id` filters. |
| 5 | **Parsing / OCR** (`parsing`, Cloud Run + RunPod) | stdlib `http.server` (not Flask), pdf2image, vLLM | Invoice → draft voucher. Renders PDF→JPEG, OCRs on a RunPod GPU (Nanonets for purchase, MiniCPM-V for sale), runs a per-vendor regex rule engine + fuzzy-match against live Supabase masters, enqueues into `push_queue`. Lots of experimental/dead pipelines around one production path. |
| 6 | **aiaccountant** (Flutter) | supabase_flutter, firebase_auth, ML Kit | Phone/web client. Scans invoices → parsing service, reviews/edits the voucher in a Realtime queue, one-tap "push to Tally". Writes **directly** to Supabase with the anon key (isolation rests entirely on RLS). |

### The two data flows that matter

- **Sync (Tally → Cloud):** `sync-engine.ts` → spawns `sync_main.py` → `tally_client` reads :9000 → `cloud_pusher` → `/api/sync` (or Supabase RPC) → Supabase.
- **Push (Cloud → Tally):** Flutter scans → parsing OCR → `push_queue` row → user taps Push → `/push-queue/activate` → desktop poller → `local-push-server :3002` → `tally_pusher` writes :9000.

**The single most important constraint:** Tally's port 9000 is single-threaded and
shared with the live UI. Heavy/full-range reads *freeze TallyPrime* — root cause of
the 1.2.11 client-freeze incident, and the reason live-Tally testing is dangerous.

---

## Part 2 — Production-Readiness verdict

The system **works** and has thoughtful touches (port pre-flight, BetterStack log
mirror, snapshot/restore, contextIsolation). But across every dimension there are no
automated guardrails, and a handful of **critical, ship-blocking** issues. Headline:
**money flows through this system with zero automated tests and at least one live
secret leak in a public repo.**

### 🔴 Critical — fix before any client install (some in *hours*)

| Issue | Evidence | Why it blocks ship |
|-------|----------|-------------------|
| **Secrets in PUBLIC git history** | `git log --all -- parsing/.env backend/.env` → commits `feeb928`, `0b536fe`, `ae5872a`… Repo is public. Contains Supabase **`service_role`** JWT, backend `API_KEY`, **RunPod** key, **Firebase service-account** private key. (Verified: gitignored *now* but permanently recoverable from history.) | Anyone can `git show <sha>:parsing/.env` → full DB write (bypassing RLS) + mint Firebase tokens. **Rotate everything now; scrub history.** |
| **All RLS policies are `USING(true) WITH CHECK(true)`** | `supabase_new_tables.sql`, `supabase_schema_v2.sql`, `scan_jobs.sql`; `full_schema.sql` has *no* RLS. Flutter writes directly with the anon key. | The "one project per client" boundary is the *only* real isolation. Anyone with the anon key + a row id can edit/delete any voucher in that tenant. |
| **Parsing/OCR service has no auth** | No `x-api-key`/bearer on parse endpoints (reverted per handoff notes). | Anyone who learns a `*-parsing-*.run.app` URL can drive unbounded RunPod GPU spend **and inject draft vouchers** into a client's push-queue. |
| **No automated tests on money math** | `backend/package.json` test = `exit 1`; no runner anywhere; two divergent rounding regimes (JS `toFixed` float `sync.ts:1004` vs Python `Decimal ROUND_HALF_UP` in parsing) can disagree by a paise. | GST/voucher data for a paying client. Two correctness incidents already shipped (1.2.11 freeze; `push_queue` column drift that silently emptied the client queue). |
| **No crash reporting anywhere** | Zero Sentry/crashReporter across all 4 surfaces. | MTTD a client incident = "until the client complains." *(See progress note below — started.)* |

### 🟠 High — fix during the hardening sprint

- **Unsigned Electron installer** auto-updating from a public repo → SmartScreen warns on every install/update.
- **Non-transactional ingest** — `sync.ts` is a hand-rolled snapshot+rollback across many PostgREST calls; a Cloud Run timeout/OOM (100 MB body limit) mid-ingest leaves a tenant half-written, and restore only `console.error`s on failure.
- **Single shared `API_KEY`, no tenant scoping** — `/push-results` and `/push-queue/activate` mutate `push_queue` by id with no company ownership check.
- **No retry/backoff** in `cloud_pusher` — one transient 503 aborts the whole sync until the next 6h cycle.
- **Swallowed errors report failure as success** — `sync-engine.ts:455` falls back to stale counts and still emits `success`; missing-column 400s are swallowed.
- **Static health checks** — `/health` returns `{status:ok}` with no dependency probe, so uptime monitoring stays green while Supabase is down.
- **No CI/CD** — every build/sign/release/deploy is a manual shell sequence in untracked `command.txt`; documented gotchas (Cloud Run "new revision serves 0% traffic", push-queue 401) are tribal knowledge.
- **Manual per-client Supabase migration fan-out** — half-adopted `supabase/migrations/`, *no* `config.toml`, committed `.temp` link-state pointing at testing, and the one automation script (`deploy-supabase-direct-ingest.ps1`) has a fatal `param()`-placement bug. This caused the ztugw drift.
- **Four god-files** — `sync.ts` (2,992), `sync_main.py` (1,905, with in-flight "CHANGE 1-4" *revert markers*), `cloud_pusher.py` (933), Flutter `voucher_detail_sheet.dart` (3,511).

### 🟡 Medium — polish & hygiene

`com.example.aiaccountant` placeholder Android applicationId (can't change post-ship);
22 stale `.js/.d.ts` compiler artifacts committed in `backend/src`; orphan `frontend/`
(30 files) + dead Guided/non-Guided React duplication; no lint/format/typecheck or
pre-commit hooks anywhere; `prod` Flutter env still points at the *testing* Supabase
(`_PROD_PENDING_CONFIRM`); hardcoded `company_name='K V ENTERPRISES'` in reports;
placeholder `support@yourcompany.com`; StatusBar lies (hardcoded `internetOk=true`,
cosmetic countdown).

---

## Part 3 — Roadmap to production grade

### P0 — Stop the bleeding (this week)
1. **Rotate every secret** that ever touched git history — Supabase `service_role` (yynuu + every client project), backend `API_KEY`/`API_KEY_CLIENT`, RunPod key, regenerate the Firebase service account. Treat old values as compromised.
2. **Scrub history** with `git-filter-repo`, force-push all branches, make the source repo **private** (decouple auto-update releases to a separate public releases-only repo). Add **gitleaks** as pre-commit + CI gate.
3. **Fix RLS** on each tenant project: replace `USING(true)` with `auth.uid()`/`company_id`-scoped predicates; scope service-role policies `TO service_role`; forbid anon `DELETE`/`UPDATE` on financial rows. Verify with Supabase `get_advisors`.
4. **Authenticate the parsing service** (shared `x-api-key` + rate limit + per-tenant RunPod quota).
5. **Repo hygiene:** `git rm` the 22 compiler artifacts + 4 committed logs; broaden `.gitignore`; delete dead Guided code and orphan `frontend/`.

### P1 — Safety net: CI + automated tests *(see dedicated section)*

### P2 — Resilience & correctness hardening
- **Sentry** on all four surfaces, tagged company/`tally_guid`/`app_version`/`ingest_mode`.
- **Real `/readyz`** checks (backend→Supabase, parsing→RunPod+Supabase) + GCP uptime + alerting.
- **Retry/backoff with jitter** in `cloud_pusher`; make ingest **idempotent** via existing alter-id markers.
- **Transactional ingest** — converge `render` mode onto a transactional Postgres RPC (the `direct` path already uses `tb_ingest_*` RPCs).
- **Per-run heartbeat** → backend, so "client hasn't synced in 24h" / "error rate > X" is an *operator* alert.
- Fix the swallowed-success bug and the lying StatusBar.

### P3 — Release engineering & decomposition
- **Code-sign** the NSIS installer (OV/EV cert or Azure Trusted Signing) with `forceCodeSigning`; sign the bundled engine exe.
- **4 GitHub Actions pipelines:** Electron release-on-tag, backend Cloud Run deploy (with `update-traffic --to-latest` + smoke), parsing deploy, Flutter flavor matrix. GCP **Workload Identity Federation**.
- **Supabase migrations as system of record:** add `config.toml`, gitignore `.temp/`, fix the migration script, fan-out workflow over a committed registry of client project-refs + `get_advisors` drift check.
- Set a real Android `applicationId`; wire a true `prod` Flutter env.
- **Decompose the god-files.** Add eslint+prettier, ruff, pre-commit; add a root README/runbook.

---

## Automated testing — the recommended shape

Two hard constraints: **never test against live Tally** (single-threaded :9000
freezes), and **money math is the product** (that's where coverage goes).

**Toolchain:** Vitest (backend + Electron main), pytest + pytest-cov (Python engine +
parsing), flutter_test + **mocktail** (Flutter), **Playwright-for-Electron** (E2E),
**Testcontainers Postgres** *or* Supabase MCP branches (integration). GitHub Actions as
the one gate. Optional: fast-check / Hypothesis property tests on the money helpers.

| Phase | What | Why first |
|-------|------|-----------|
| **0 — Infra** (1–2 days) | Replace `test: exit 1`, add runners, one `ci.yml` gating merges. | Without CI, tests rot like the stale Flutter `widget_test.dart` did. |
| **1 — Money correctness** (~1.5 wk) | Golden-value unit tests: `toMoney`/`toPaise` (`sync.ts:1004`), `round2`/`build_sale_voucher_payload`, `voucher_builder` scaling. **One cross-path test** asserting backend & parsing agree to the paise on the same `(qty,rate,discount,gst)`. | Directly defends the books; catches float-vs-Decimal divergence. |
| **2 — Idempotency & ingest** (~2 wk) | Ephemeral Postgres + `full_schema.sql`: POST same `/api/sync` body twice → identical rows; inject mid-ingest failure → snapshot fully restores; same OCR payload twice → one `push_queue` row. | Idempotency + clean rollback is the key reliability property. |
| **3 — Contract** (~days) | Assert `electron.d.ts` matches `preload.ts` channels (3 missing); snapshot parsing→backend and backend→:3002 JSON shapes; generate Supabase types. | Locks boundaries that drift silently. |
| **4 — E2E w/ mocked Tally** (~2–3 wk) | Playwright drives add-company/sync/settings against a **recorded-XML replay server** on 127.0.0.1:9000 (reuse `src/python/tally-responses/`). Flutter widget+golden with mocked Supabase/Firebase. | Tests the whole desktop loop in CI **without ever touching TallyPrime**. |

**Coverage targets:** 90%+ on money/parse pure functions; ~60% on backend route
handlers; happy-path-only E2E. Don't chase coverage on the OCR regex engine — pin it
with characterization fixtures. Seed the suite with the two known incidents as
permanent regression tests.

**Realistic minimum before a paid pilot:** P0 + testing Phases 0–2 ≈ **~4 weeks for one
engineer** covers the security leaks and money-correctness/idempotency risks that
matter most. Phases 3–4 + P2/P3 are the follow-on hardening.

---

## Implementation progress

- **2026-06-29 — Crash reporting (P2), aiaccountant ↔ backend boundary: STARTED.**
  Sentry wired both ends (DSN-gated, no-op until `SENTRY_DSN` set). Flutter:
  `SentryFlutter.init` wraps `runApp`, uid-tagged, `ApiClient`/`_activate` capture
  failed backend calls with body + `x-request-id`. Backend (`@sentry/node`):
  `instrument.ts`, request-id middleware, uid tag in `auth.ts`,
  `setupExpressErrorHandler`. Correlation = `x-request-id` flowing both ways.
  Pending: provision 2 Sentry projects + DSNs. See memory `crash_reporting_sentry`.
