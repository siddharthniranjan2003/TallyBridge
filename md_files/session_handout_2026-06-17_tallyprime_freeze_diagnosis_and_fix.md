# Session Handout — TallyPrime Freeze: Diagnosis & Fix (deep)

**Date:** 2026-06-17 · **Branch:** `TallyBridge-Backend-Refactor`
**Baseline commit:** `5b37f7a` (2026-06-16, "chore: sync in-flight work across electron, backend, and parsing")
**State of work:** code changes applied to working tree, **NOT committed**, builds verified. Live-tested against a real TallyPrime.

This handout covers everything done from commit `5b37f7a` to now: the investigation, the root-cause findings (concrete vs hypothesis), the code fix that was applied and verified, and the open items. A new chat should be able to continue from here.

---

## 0. The mission

User's real goal (refined over the session): **"While TallyBridge runs its sync, the user's TallyPrime must not hang/freeze."** Staff reported TallyPrime regularly freezing when TallyBridge runs — worst right after launch. We had to find *exactly* what froze it and fix it, measuring against the live system.

---

## 1. The single most important fact (root constraint)

**TallyPrime's gateway on port 9000 is single-threaded and shares ONE calc engine with its on-screen UI.** Every request — TallyBridge's reads *and* the user's keystrokes — serialize through that one engine. So **any request that makes the engine compute also freezes the UI for exactly that compute time.** We cannot make TallyPrime multi-threaded; the only lever is to keep each request's compute short enough that it isn't felt as a hang.

Saved to memory: `tally_9000_single_threaded_hang.md`, `feedback_live_tally_test_caution.md` (be conservative when live-testing the user's Tally — we crashed it to "not responding" twice early on with oversized synthetic requests; the user flags every hang).

---

## 2. Test environment (the live system we measured)

- **TallyPrime Silver**, `tally.exe`, ~14 threads, ~886 MB RAM. Gateway live on `127.0.0.1:9000`.
- **Company:** `K V ENTERPRISES` · GUID `f3e9df46-fc4f-4b85-930a-abe1e427e900` · MasterID 29 · State Haryana.
  - **2,904 ledgers**, **13,237 stock items**, **45,067 vouchers**.
  - Books from `2024-04-01`; last voucher `2026-06-16`. **Voucher volume is dense Apr 2024 → ~Jul 2025** (~2,300–3,600/month), then drops to ~0 (test copy frozen ~mid-2025, a few stray 2026 entries).
  - Tally change counters: `ALTERID=793032`, `ALTVCHID=852990`.
- **`%APPDATA%\TallyBridge\tallybridge-config.json` (the real client config):**
  - `tallyUrl: http://localhost:9000`, `readMode: auto`, `syncIngestMode: hybrid`, `syncIntervalMinutes: 1`
  - `backendUrl`/`controlPlaneUrl`: `https://tallybridge-backend-828647628834.asia-south1.run.app` (= **testing.riplara**, GCP Cloud Run, asia-south1)
  - `syncIngestUrl`: `https://yynuuysvjeipawzfbeme.supabase.co/functions/v1/ingest-sync` (`yynuu` = **TESTING Supabase**)
  - `syncFromDate: 2024-04-01`, `syncToDate: 2026-06-05`
  - `companies[0].lastCompletedBackfillSignature: "2024-04-01..2026-04-28"`, `lastSyncedAt: 2026-05-04`, **`lastSyncError: "HTTP 503"`**
  - `migratedToHybridV1: true`
- **`%APPDATA%\TallyBridge\.alter_ids_cache.json`:** for the GUID → `alter_id 793032`, `last_voucher_date 2025-07-30` (matches current Tally counter — so change-detection *would* say "no change").
- Python on this machine: **3.14.3**, `requests 2.32.5`. ODBC DSN present: **`TallyODBC64_9000`** (auto mode uses it for masters).

> Note: a ₹1 test **Receipt** voucher was written during testing (party `AMAR TOOLS`, date 16-Jun-2026, narration "TallyBridge write-lock test (delete me)"). It should be **deleted from the test company's Day Book** — it's a real (harmless) voucher we created.

---

## 3. Measurement method — the "canary"

Software can't *feel* a UI freeze, and `tally.exe` CPU counters read ~0 (useless). So we used a **canary**: a tiny `CompanyAlterIds` request fired repeatedly *while* a heavy request runs. Because the gateway is single-threaded, the canary waits in the same queue → **canary latency = the freeze a human would feel.**
- Idle warm canary ≈ **1.5–2 ms**.
- During a heavy read it ballooned to **1,643 ms** — and the user, watching the screen, independently confirmed **"a solid 1–1.5 s freeze."** Instrument validated against human perception.

Harness pattern used throughout (PowerShell + .NET `HttpClient`): fire the heavy request async, poll a canary every ~120–250 ms, classify samples (`>1 s` = freeze, `<100 ms` = responsive), and for full-engine runs launch `sync_main.py` via `Start-Process` with stdout redirected, interleaving its log with canary timings.

---

## 4. Full interaction catalog (every way TallyBridge touches Tally:9000) + measured status

Five channels: **(A)** TCP pre-flight probe, **(B)** plain HTTP GET (product detect), **(C)** XML-HTTP POST reads, **(D)** ODBC/SQL via `TallyODBC64_9000` (masters in auto/hybrid), **(E)** XML-HTTP POST writes (voucher Import — the only writes).

| Interaction | Channel | Measured | Status |
|---|---|---|---|
| TCP pre-flight probe | A | ~3.5 ms, 0 engine | Harmless |
| Product GET probe | B | 20–56 ms, returns `<RESPONSE>TallyPrime Server is Running</RESPONSE>` (no version string) | Harmless |
| Company info collection | C | ~390 ms round-trip, ~0 engine | Harmless |
| Alter-ids (change-detection gate) | C | ~2 ms warm | Harmless — gate that keeps idle quiet |
| Groups master | C/D | ~430 ms, 47 groups | Harmless |
| **Ledgers master** | D (ODBC) | **~0.8 s** freeze, 2,904 rows | ⚠ one-time residual |
| **Stock items** | D (ODBC) | **~0.75 s** (synthetic XML was 1.64 s); valuation `CLOSINGVALUE` is the cost | ⚠ one-time residual |
| Voucher headers, 1 window | C | ~41 ms (light path) | Cheap |
| Outstanding (recv+pay) | C | ~284 ms total | Light (scales w/ open bills) |
| P&L → BS → Trial Balance | C | ~30–273 ms each | Light (scales w/ ledgers) |
| **HEAVY voucher collection, 1 window (sub-lists)** | C | **2.4 s freeze → 0.59 s after fix** | ✅ FIXED |
| **HEAVY full voucher sync (windows)** | C | **16 freezes>1s, ~27 s frozen → 0 freezes after fix** | ✅ FIXED |
| Concurrency — 2 reads at once (no mutex) | C | *not tested standalone* | ❓ open risk |
| Cadence — chunked vs one-big | C | small chunks win; one-big = the "not responding" crash | ✅ applied (= the fix) |
| WRITE voucher Import | E | created 1 voucher, **~0.28 s** lock | Tested; minor |

The heavy voucher path (rows 11–12) was the **prime suspect and the actual cause**. Full-FY-in-one-request and a giant open ledger-list UI view were what produced hard "not responding" crashes — NOT normal engine behavior; the engine's per-window chunking keeps each request survivable.

Artifacts: `md_files/session_handout_2026-06-17_tally_interaction_runbook.md` (full 15-section runbook), `md_files/session_handout_2026-06-17_tally_electron_perf_audit.md` + `TallyBridge_Perf_Audit_2026-06-17.html` (a 55-finding static perf audit of the whole app, broader than just the freeze).

---

## 5. Root cause (concrete, measured)

**5a. The freeze unit.** TallyBridge pulled vouchers **one calendar month per request**. Each busy month (~2,800 vouchers, full detail) occupied the single engine **~1.2–1.8 s** → one freeze per month. A full historical load = **16 busy months fired back-to-back ≈ 27 s of freezing** in a ~57 s sync. (Verified live: baseline run = 16 freezes >1 s, max 1,839 ms.)

**5b. Why it happens on EVERY launch — the backfill re-run.** TallyBridge runs a one-time "backfill" (load all history). It is only marked complete on a **successful cloud upload**. The upload fails with **HTTP 503**, so it never reaches "success", `lastCompletedBackfillSignature` is never saved (`sync-engine.ts:452-454`), and every startup re-arms the full backfill (`sync-engine.ts:317-325`).

**5c. Why it re-downloads data the backend already has (design flaw).** Backfill mode **forces a full re-pull of the entire configured date range and bypasses change-detection** — it does NOT reconcile per-window against the backend; it re-extracts everything and relies on the backend to upsert/dedupe by GUID. The backfill is *armed* because `syncToDate` was extended (`..2026-04-28` → `..2026-06-05`), so the signature mismatches the last completed one — yet instead of pulling only the ~5-week delta, it re-pulls all ~104 weeks. Change-detection (`ALTERID` matches the cache) would otherwise skip the whole thing — but a manual backfill overrides it. Note: **heartbeat** triggers ignore the backfill (`shouldUseManualBackfill = backfillPending && trigger !== "heartbeat"`); only **startup/manual** re-pull.

**5d. The 503 itself.** Probed both endpoints: backend `/health` = 200 (51 ms warm), Supabase ingest fn = 200 — but **both cold-start at ~3.2–3.5 s**. So the 503 is consistent with a **transient Cloud Run cold-start/scaling 503** on the (large backfill) push, not an outage. It's now fully decoupled from the freeze.

---

## 6. THE FIX (applied to working tree, verified, NOT committed)

`git diff HEAD` touches **2 files** (`src/python/sync_main.py` +38, `src/main/index.ts` +6):

### 6a. Smaller voucher windows — the primary fix (`sync_main.py`)
- New env knob **`TB_VOUCHER_WINDOW_DAYS`, default `7`** (0 = legacy monthly).
- `build_month_windows()` now emits fixed-N-day windows instead of calendar months:
  ```python
  if VOUCHER_WINDOW_DAYS > 0:
      window_end = min(cursor + timedelta(days=VOUCHER_WINDOW_DAYS - 1), end)
  else:
      next_month = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
      window_end = min(next_month - timedelta(days=1), end)
  ```
- Effect: each voucher request now pulls ~500–800 vouchers and computes **<0.6 s** instead of ~1.5 s.

### 6b. Pacing knob — added but OFF by default (`sync_main.py`)
- New env knob **`TB_TALLY_PACING_MS`, default `0` (off)** + `pace_tally()` helper, called before each voucher window and before each section (groups/ledgers/vouchers/stock/outstanding/P&L/BS/TB — 9 call sites).
- **Why off:** live A/B test proved pacing does NOT reduce the freeze (the inter-window gaps were already responsive — while Python parses a window's response, Tally's engine is idle). It only adds time. Kept as a tuning knob.

### 6c. Defer startup sync (`index.ts`)
- `syncEngine.start();` → `setTimeout(() => syncEngine.start(), 8000);` so the heavy backfill doesn't collide with the app window + TallyPrime both still warming up.

**Verification:** `py -3 -m py_compile sync_main.py` → OK; `npx tsc -p tsconfig.main.json` → clean. **Builds NOT run for production:** to ship, run `npm run build:python` (bundles `sync_main.py` into `python-dist/tallybridge-engine.exe`) and `npm run build:main`. The user's live log already showed 7-day windows, so they rebuilt or ran dev.

---

## 7. Results — verified live (full 2-year backfill, real engine, identical workload)

| Measure | BEFORE (monthly) | 14-day windows | **7-day (shipping default)** |
|---|---|---|---|
| Freezes > 1 s | **16** | 0 | **0** |
| Worst voucher freeze | **1,839 ms** | 946 ms | **589 ms** |
| Worst masters/probe blip | — | ~0.8 s | **820 ms** (one-time) |
| Total UI-frozen time | **~27 s** | ~0 | **≈ 0 (sub-second blips only)** |

The repeated multi-second freezing is **eliminated**. Iteration log: monthly (baseline) → 14-day (0 freezes >1s but up to 946 ms) → **7-day (0 >1s, max voucher 589 ms)** chosen as default. Supervisor-facing writeup: `TallyPrime_Freeze_RootCause_and_Fix_2026-06-17.html`.

---

## 8. What is STILL open (next chat should pick these up)

1. **The HTTP 503 / backfill re-run (highest value).** The freeze is fixed but the full backfill still re-runs every startup (re-reading all 45k vouchers the backend already has) because the 503 stops completion. Fix options: (a) **retry on 503** in `cloud_pusher` (cold-start backoff); (b) **decouple/resumable completion** — mark backfill progress per uploaded chunk so a 503 doesn't discard everything; (c) **only pull the delta** of the backfill range, or have the client ask the backend for its latest voucher/alter-id and fetch only the gap; (d) raise Cloud Run min-instances to avoid cold starts.
   - **Immediate no-code workaround for the user:** clear `syncFromDate`/`syncToDate` in Settings → no backfill armed → change-detection (ALTERID matches cache) skips the re-pull entirely.
2. **Concurrency / no mutex (open risk).** `SyncEngine`, `PushQueuePoller`, and `LocalPushServer` (:3002) share no lock on port 9000. A cloud push landing *during* a sync read can stack two requests on the one engine → an intermittent longer freeze. Never measured. Fix = one shared per-company mutex/queue around all 9000 access (the audit's "single mutex / back-pressure" item).
3. **Masters ~0.8 s one-time blips.** ODBC probe + bulk ledger(2,904)/stock(13,237) reads. Could be chunked to go sub-500 ms, but carries some risk to the working ODBC path; not the repeated freeze, so deprioritized.
4. **Duplicate log lines.** Each sync-log line renders twice (cosmetic double-emit in the SyncLog view). Harmless; worth cleaning.
5. **Broader perf audit (separate).** `TallyBridge_Perf_Audit_2026-06-17.html` has 55 verified findings (unbounded `outputLines[]`, per-line IPC storm, electron-store sync writes, etc.) — not freeze-critical but a backlog.

---

## 9. Quick reference — env knobs & commands

- `TB_VOUCHER_WINDOW_DAYS` (default 7; 0 = monthly) — voucher slice size; the freeze lever.
- `TB_TALLY_PACING_MS` (default 0) — optional inter-request gap; off, proven not to help.
- Run the engine standalone (what the harness does): set `TALLY_URL`, `TALLY_COMPANY`, `TALLY_COMPANY_GUID`, `TB_READ_MODE=auto`, `SYNC_INGEST_MODE=hybrid`, `TB_SYNC_TRIGGER=startup`, `TB_SYNC_FROM_DATE`/`TB_SYNC_TO_DATE`, `TB_FORCE_FULL_SYNC=1`, `TB_USER_DATA_DIR=<temp>` (avoid clobbering real cache), empty `CONTROL_PLANE_URL`/`SYNC_INGEST_URL` to skip cloud (reads still run; push fails harmlessly after all Tally reads). Launch: `py -3 -u src\python\sync_main.py`.
- Compile checks: `py -3 -m py_compile src/python/sync_main.py` · `npx tsc -p tsconfig.main.json`.
- Production build: `npm run build:python` + `npm run build:main`.

## 10. Suggested next action
Commit the two-file fix (`sync_main.py`, `index.ts`) on `TallyBridge-Backend-Refactor`, then take on **item #1 (the 503 / resumable backfill)** — it's the highest-value remaining work: it stops the wasteful per-launch re-sync *and* gets the data into the cloud. Be conservative with further live Tally tests (see `feedback_live_tally_test_caution.md`).
