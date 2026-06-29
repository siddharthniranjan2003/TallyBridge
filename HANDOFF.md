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
| `TB_DISABLE_MASTER_WIPE_GUARD` | off | `1` = stop refusing empty master pushes (if the guard ever false-trips) |
| `TB_DISABLE_VOUCHER_WIPE_GUARD` | off | `1` = stop refusing empty voucher pushes |
| `TB_MASTER_WIPE_GUARD_MIN_RATIO` / `TB_VOUCHER_WIPE_GUARD_MIN_RATIO` | `0.5` | how much shrink is allowed before the guard blocks |
| `TB_MASTER_WIPE_GUARD_MIN_BASELINE` / `TB_VOUCHER_WIPE_GUARD_MIN_BASELINE` | `10` / `1000` | min rows before a section is policed (small companies aren't) |
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

- **Code signing** — needs an OV/EV Authenticode cert (`CSC_LINK`/`CSC_KEY_PASSWORD`). Until then the installer trips Windows SmartScreen. (Scaffolded in `electron-builder.yml`.)
- **A10** render-mode voucher chunking (default `hybrid` already chunks, so lower urgency).
- **Multi-company:** add `SVCURRENTCOMPANY` to voucher/report exports if more than one company is ever loaded at once.
- **Transport resilience:** `cloud_pusher` could use a `requests.Session` + retry.
- Full duplicate-push closure needs a backend atomic claim/lease + idempotency key (out of desktop scope).

---

## 7. If something goes wrong in the morning — quick triage

1. **App won't launch / behaves oddly after build** → it's almost certainly the *build*, not the code (main process type-checks clean). Re-run `npm run build` on a clean `dist/`; on Windows: `rmdir /s /q dist` then `npm run dev`.
2. **A sync shows an amber ⚠ "skipped" warning** → that's *working as designed* (a guard protected cloud data). Check Tally is open with the company loaded, then re-sync.
3. **A guard is blocking a legitimate change** → set the matching `TB_DISABLE_*` env var (§4), relaunch, then re-sync.
4. **Need to back out one fix** → `git revert <sha>` (§5), rebuild.
5. **Need to abandon the whole branch** → `git checkout production-release-28-06`. Nothing else is affected.
