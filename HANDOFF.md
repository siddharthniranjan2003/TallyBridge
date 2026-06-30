# TallyBridge — Production-Readiness Fix Branch: Handoff

**Branch:** `fix/prod-readiness-4gb-tally` (off `production-release-28-06`)
**Audience:** the engineer who will build/test/ship this.
**Goal:** desktop-app fixes for the 4GB-i3 + TallyPrime-dependency production target.

---

## 1. Safety architecture (read this first)

You can adopt this incrementally and back out cleanly — nothing here is one-way:

- **Isolated branch.** `production-release-28-06` and `main` are **untouched**. If you decide not to ship, do nothing — or `git checkout production-release-28-06`.
- **Atomic commits.** Every fix is its own commit (see §5). To back out exactly one fix: `git revert <sha>` — no need to unwind the rest.
- **Kill-switches without rebuilding.** The new protective guards and timeouts are controlled by **environment variables** (§4). If a guard misbehaves in the field, set the env var and relaunch — no code change, no new build.

---

## 2. What's verified vs not

| Area | Status |
|------|--------|
| Python engine logic (wipe guards, push validation, memory, ack budget) | ✅ **18 unit tests pass** (`python3 src/python/tests/test_*.py`) |
| Electron main process (`src/main/*.ts`) | ✅ **`tsc -p tsconfig.main.json` type-checks clean** |
| Renderer change (`CompanyCardStable.tsx`) | ✅ compiles clean (additive amber warning note) |
| Real installer build (`npm run dist`) | ⚠️ **not run here** — must be built on a Windows box (PyInstaller + electron-builder). Do this before shipping. |
| On-device smoke test with live Tally | ⚠️ **not done** — see §3 checklist |

---

## 3. Build + smoke-test checklist (Windows build box)

```bash
npm install                 # restore deps
npm run build               # build:renderer (vite) + build:main (tsc)
npm run build:python        # PyInstaller -> python-dist/tallybridge-engine.exe  (NEW: now part of dist)
npm run dist                # full signed* installer -> release/   (*signing: see §6)
```
Dev mode (no installer): `npm run dev`.

**Smoke test (with TallyPrime open + a company loaded):**
1. App launches; tray icon appears.
2. Add a company; first **Sync All Now** completes (watch the **Sync Log** page).
3. Degraded-Tally check: open a modal/popup in Tally mid-sync → confirm the sync **skips/defers** and the card shows an **amber ⚠ warning** (not a green success, not a wipe).
4. Push path: enqueue a voucher from the cloud → confirm it imports once (no duplicate) and the queue clears.
5. **Quit from the tray** → confirm no leftover `tallybridge-engine.exe` in Task Manager (it's reaped on quit now).

---

## 4. Kill-switches & tunables (env vars — set on the machine, no rebuild)

These are read by the engine via the app's environment, so a Windows **System Environment Variable** + relaunch is enough to change behaviour in the field.

| Env var | Default | Effect |
|---------|---------|--------|
| `TB_DISABLE_MASTER_WIPE_GUARD` | off | `1` = stop refusing empty master pushes (if the guard ever false-trips). Masters only — reports have their own switch below |
| `TB_DISABLE_VOUCHER_WIPE_GUARD` | off | `1` = stop refusing empty voucher pushes |
| `TB_DISABLE_REPORT_WIPE_GUARD` | off | `1` = stop refusing empty financial-report (P&L/BS/TB/outstanding) pushes — independent of the master switch |
| `TB_MASTER_WIPE_GUARD_MIN_RATIO` / `TB_VOUCHER_WIPE_GUARD_MIN_RATIO` | `0.5` | how much shrink is allowed before the guard blocks (master ratio also governs reports) |
| `TB_MASTER_WIPE_GUARD_MIN_BASELINE` / `TB_VOUCHER_WIPE_GUARD_MIN_BASELINE` | `10` / `1000` | min rows before a section is policed (small companies aren't; master baseline also governs reports) |
| `TB_PIN_COMPANY_ALL_REQUESTS` | on | `0` = revert to old behaviour (don't pin SVCURRENTCOMPANY on Data/voucher exports) if a Tally build rejects it |
| `TB_ODBC_READ_TIMEOUT_SECONDS` | `30` | cap on one ODBC helper response before it's torn down and XML is used (min 5) |
| `TB_LOCAL_PUSH_PORT` | `3002` | move the local push server if 3002 is taken (it now retries 5× w/ backoff, no modal) |
| `TB_PUSH_WORKER_TIMEOUT_MS` | `90000` | watchdog that kills a wedged push worker |
| `TB_PUSH_ACK_TIMEOUT_SECONDS` | `8` | per-ack HTTP timeout |
| `TB_PUSH_CYCLE_BUDGET_SECONDS` | `70` | stop a push batch before the watchdog |
| `TB_PUSH_QUEUE_POLL_INTERVAL_MS` | `5000` | cloud→Tally poll cadence (min 1000) |

**Fastest "make it behave like before" lever:** set `TB_DISABLE_MASTER_WIPE_GUARD=1` and `TB_DISABLE_VOUCHER_WIPE_GUARD=1` — that reverts the new data-protection behaviour while keeping the crash/lifecycle fixes.

---

## 5. What changed (commits, newest→oldest)

```
A6   per-section cache advance on master-guard trip (no re-sync storm)
fr-01 cold-start cloud baseline for the master wipe guard
B-H1 prevent two push workers writing to Tally at once
A7   bound push-ack time + per-cycle budget under the watchdog
A5   surface wipe-guard warnings in the UI (was showing green)
A9   push watchdog fallback-settle + stdin EPIPE guard
C2   stop silently acking un-imported vouchers (silent loss)   [CRITICAL]
A2-A4 reap sync child on quit/update; graceful tray quit; tree-kill
C1   sync engine honors the tally-gate (sync-vs-push c0000005)  [CRITICAL]
A1   rebuild Python engine as part of `dist` (stop shipping stale exe)
(plus an earlier round: memory/Sync-All-Now crash, voucher wipe guard,
 per-job push ack, kill push workers, single build config, response validation,
 dev-script race fix)
```
Full reasoning is in each commit message and in `../tallybridge-rereview-report.md`.

---

## 6. Known follow-ups (NOT done on this branch)

**⚠️ P0 voucher-wipe fix shipped — recovery note for affected installs.** AlterID-incremental sync was stamping a full-financial-year reconciliation range onto a delta-only payload, so the cloud marked every *unchanged* voucher stale and tried to delete it (~98% of the table). `tb_guard` (migration `20260619`) blocked it → the sync stuck in a retry loop and new vouchers never landed. **Pre-`tb_guard`, this silently wiped the voucher table.** Fixed (commit `9c4835c`) by suppressing the reconciliation range for alter-id-delta payloads. **The fix self-heals stuck installs on the next sync.** But any install that was **wiped before `tb_guard` existed** won't auto-restore — trigger a **forced full sync** (clear the alter-id cache `.alter_ids_cache.json`, or change the manual backfill range) to re-push the full voucher set.
- **Voucher deletion reconciliation in incremental mode** — by design the fix no longer reconciles Tally-side *deletions* during alter-id-incremental (it never could correctly). Deletions only clear on a full sync. Follow-up: a periodic full voucher reconciliation, or an explicit deletion channel (needs backend = Option B).

**Best done with the app RUNNABLE (risky to do blind, so left for the desktop env):**
- **B-M10** — financial-year / timezone / clock date math (don't hard-clamp `books_to` to today; widen the FY fallback). Needs runtime + timezone testing.
- **B-M11** — sleep/wake handling via Electron `powerMonitor` (re-arm timers / drop half-open sockets after resume).

**Need backend coordination (out of desktop scope):**
- **A10** render-mode voucher chunking — the default `hybrid` mode already chunks, so this only bites the legacy `render` path; chunking it needs the backend to accept partial voucher batches.
- **Full duplicate-push closure** — the desktop side now acks per-job + has a cycle budget, but full idempotency needs a backend atomic claim/lease + a voucher idempotency key.

**Excluded by request:**
- **Code signing** — needs an OV/EV Authenticode cert (`CSC_LINK`/`CSC_KEY_PASSWORD`); until then the installer trips Windows SmartScreen. (Scaffolded in `electron-builder.yml`.)

**Cosmetic backlog:** ~22 LOW findings (logging verbosity, minor leak edges, naming) — none are crash/data-loss risks.

**Already done this round (was previously listed here):** multi-company `SVCURRENTCOMPANY` pinning (B-H4), `cloud_pusher` retry session (B-H10), no-company pre-flight, report wipe guard (B-M4), backfill retry bound (B-M16), key redaction (B-M6), and more — see §5 / git log.

---

## 7. If something goes wrong in the morning — quick triage

1. **App won't launch / behaves oddly after build** → it's almost certainly the *build*, not the code (main process type-checks clean). Re-run `npm run build` on a clean `dist/`; on Windows: `rmdir /s /q dist` then `npm run dev`.
2. **A sync shows an amber ⚠ "skipped" warning** → that's *working as designed* (a guard protected cloud data). Check Tally is open with the company loaded, then re-sync.
3. **A guard is blocking a legitimate change** → set the matching `TB_DISABLE_*` env var (§4), relaunch, then re-sync.
4. **Need to back out one fix** → `git revert <sha>` (§5), rebuild.
5. **Need to abandon the whole branch** → `git checkout production-release-28-06`. Nothing else is affected.
