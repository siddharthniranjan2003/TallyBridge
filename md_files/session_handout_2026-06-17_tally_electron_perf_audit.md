# TallyBridge — Electron ↔ TallyPrime Performance Audit

> Scope: hangs, slowdowns, memory leaks, thread bottlenecks, resource block/unblock, UI-vs-background-thread, and Electron-start-vs-TallyPrime-readiness latency.
> Method: 7 parallel deep-readers (main-process, renderer, Python engine) → 55 findings → each critical/high finding adversarially re-read against the cited code. 0 refuted, 55 confirmed. Generated 2026-06-17.

## TL;DR

The highest-impact issues, by name:

1. **[HIGH] Render-mode single-shot voucher POST** (`cloud_pusher.py` / `sync_main.py`) — entire company payload sent in one HTTP body; documented 413/OOM and item-loss root cause. Only chunked on direct/hybrid.
2. **[MEDIUM] Idle push-queue poller spawns full Python engines forever** (`push-queue-poller.ts`) — ~24 cold engine boots/min on a 2-company idle machine just to issue one HTTP GET.
3. **[MEDIUM] No concurrency cap / no child reaping on the push path** (`local-push-server.ts`) — N concurrent cloud POSTs fork N engines all hitting Tally:9000; `stop()` never kills in-flight workers (orphaned engine holds Tally across updater relaunch).
4. **[MEDIUM] Per-line IPC + SyncLog render storm** (`sync-engine.ts`, `SyncLog.tsx`) — one structured-clone IPC message per Python stdout line drives a full 500-row re-render + smooth-scroll animation per line.
5. **[MEDIUM] Fully serial fetch-everything-then-push in the Python engine** (`sync_main.py`) — zero fetch/upload overlap, whole company held resident, plus redundant double serialization just to measure bytes.

---

## 1. Sync optimization

**[HIGH] Render mode POSTs the entire company in one request — the documented OOM/413 path.** `src/python/sync_main.py:1770-1800` materializes every section (vouchers, ledgers, stock, financials) into one `payload` dict and calls `push(payload)` at `:1823`. In render mode, `cloud_pusher.push()` (`src/python/cloud_pusher.py:635-652`) passes the whole `request_payload` to `_post_sync_payload('render', ...)`, which does `requests.post(target_url, json=payload)` at `cloud_pusher.py:488-493` — one in-memory JSON body, no chunking. Direct/hybrid chunk vouchers at `DIRECT_VOUCHER_CHUNK_SIZE=2000` (`cloud_pusher.py:39`, `_build_direct_voucher_payloads:267-300`); render has no equivalent. Verified worse than reported: `_build_render_payload` (`cloud_pusher.py:255-264`) is **dead code, never called**, so render actually ships *all* sections, not a voucher-only subset.
**Fix:** Chunk the render voucher path mirroring direct mode (reuse `_chunk_rows` + chunk_index/is_final_chunk in `sync_meta`), masters on first chunk only; or delete the render single-shot branch and route render through the chunked builder. Near-term mitigation: the `migratedToHybridV1` startup migration (`src/main/index.ts`) already flips clients render→hybrid; exposure remains only when `SYNC_INGEST_MODE=='render'` *and* migration unset / `SYNC_INGEST_URL` absent. That gate is why this is HIGH not CRITICAL.

**[MEDIUM] Idle poller spawns a full engine per company every 5s.** `src/main/push-queue-poller.ts:9` (`DEFAULT_PUSH_QUEUE_POLL_INTERVAL_MS=5000`), re-armed in `pollOnce` finally (`:124`); each cycle loops all companies (`:112`) and `pollCompanyQueue` unconditionally `spawn`s the bundled engine (`:155`) just to run an empty-queue HTTP GET. ~24 cold engine boots/min on a 2-company idle machine. (Verified: the idle path does **not** probe Tally:9000 — `fetch_pending_push_vouchers` returns before any Tally call — so per-boot cost is interpreter bootstrap + one HTTP GET, still pure waste.)
**Fix:** Move the "any pending jobs?" check into TS — the poller already has `controlPlaneUrl`/`controlPlaneApiKey`/company identity in scope (`:100-102, 117-121`); issue the cheap GET directly and only `spawn` when ≥1 voucher is pending. Optionally add exponential backoff (up to ~60s) on consecutive empty polls.

**[MEDIUM] Redundant double serialization just to measure bytes.** `sync_main.py:1801-1802`: `build_payload_section_sizes(payload)` `json.dumps` each of 10 sections, then `estimate_payload_bytes(payload)` `json.dumps` the *entire* payload a second time only to take `len()` — at the exact peak-RAM moment, then `requests` serializes a third time. Verified the per-section bytes are *already* cached in `section_metrics[...]['bytes']` from fetch-time `build_section_metric`, so this is a 3rd+4th redundant full serialize.
**Fix:** Drop `estimate_payload_bytes(payload)`; derive total from cached `section_metrics` values + small overhead. Same observability output, no whole-payload transient string.

**[MEDIUM] `company_alter_ids` fetched twice per run.** `sync_main.py:1023` (in `fetch_company_info_with_fallback`, for `last_voucher_date`) and again at `:1415` (change detection) — two blocking XML-RPC round-trips + two parses every run, every heartbeat. **Fix:** fetch once, return the parsed dict for reuse.

**[MEDIUM] Masters/ledgers/stock always full re-fetch; only the voucher window is incremental.** `sync_main.py:1394-1411` hardcodes an all-true `sync_plan` *before* change detection; `build_sync_plan` gates ledgers/stock on `master_changed`/`voucher_changed` (`:425-428`) but any voucher change forces full ledger+stock+report re-fetch and re-upload of unchanged rows; only `TB_ENABLE_INCREMENTAL_VOUCHER_SYNC` (default off) narrows the voucher `from_date` (`:410-419`). A broad `except` at `:1479` means a change-detection failure silently forces a full company re-sync every heartbeat. **Fix:** narrow the pre-detection fallback plan; send changed masters by `alt_mst_id` delta where the backend supports it; enable incremental voucher window by default once validated.

**[MEDIUM] Cloud push uses bare `requests.get/post` — new TCP+TLS per call.** `cloud_pusher.py:312/351/398/488` — unlike `tally_client` (`SESSION` at `tally_client.py:10`), every backend call (masters POST + one POST per voucher chunk in direct mode) does a fresh TLS handshake. **Fix:** add a module-level `requests.Session()` in `cloud_pusher`.

Cross-refs: serial company loop (§2), per-line IPC (§4/§6), startup-eager sync (§7), double port preflight (§5), poll/push Tally contention (§5).

---

## 2. RAM → thread bottleneck

The heavy memory pressure lives in the **Python sync subprocess** (off Electron's threads), but it competes for machine RAM with TallyPrime and Electron, and several main-process buffers are unbounded.

**[MEDIUM] Whole company dataset held resident until process exit.** `sync_main.py:1482-1489` declares the eight section lists; all are referenced together by `payload` (`:1770-1800`) and kept alive through serialization, byte-measurement, and the *entire* multi-request `push()` (`:1823`) — peak RAM = full company. Verified: chunk slices share the same dict objects and `request_payload = dict(payload)` is shallow, so it's single-copy retention (not N× blow-up), which is why it's MEDIUM. It still holds `payload` alive purely so `:1848` can read `payload["sync_meta"]` for the success log.
**Fix (biggest win, surgical):** extract `sync_meta = payload["sync_meta"]` *before* `push()`, then `del payload` + the section locals immediately after `push()` returns so the dataset is collectable before `run_pending_push_cycle` (`:1840`). In direct mode, null out master refs after the masters POST and release each voucher chunk after its POST.

**[MEDIUM] Voucher window recursion builds two full copies.** `sync_main.py:827-902`: `fetch_recursive` extends one growing `all_vouchers` list across all month-windows/sub-splits (`:883`); then `dedupe_vouchers` (`:481-500`) sorts the *whole* list and builds a parallel `deduped` list + `seen_guids` set — at peak ≈ two full copies + sort temporaries. **Fix:** dedupe incrementally as windows arrive (carry `seen_guids` across windows, skip at extend-time); validate per-window streaming.

**[MEDIUM] Full Tally response buffered, ~2 copies.** `tally_client.py:52` `response.content` then `:70` `response.text`, no `stream=True` (`:98-106`) — entire Day Book/Ledger export buffered before any parse. (Verified ~2 simultaneous copies, not 3 as originally claimed.) **Fix (cheap):** decode once from the bytes you already hold and `del content` instead of falling through to `response.text`; decode the utf-16 trial once with the detected encoding rather than three attempts. The `stream=True` + incremental-parse rewrite is a larger refactor (every `get_*` returns a full `str`) — gate behind an observed OOM.

**Main-process unbounded buffers (smaller, but on the UI-driving process):**

- **[MEDIUM] `outputLines[]` accumulates every Python stdout line for the whole sync** (`sync-engine.ts:523-530`), consumed only to find the last `{`-line at `:430`. Verified pure waste. **Fix:** replace with `let lastJsonLine = ""` updated in the stdout handler; behavior-preserving since `:430` only wants the last `{`-prefixed line. (This is the [LOW]-severity variant at `:381-382, 523-539` re-cast — same root.)
- **[MEDIUM] Push-worker stdout/stderr concatenated unboundedly on the main heap** (`local-push-server.ts:231-261`; same shape in `push-queue-poller.ts:156-165`); a chatty/hung engine grows these strings without limit. **Fix:** keep only a bounded tail (last N KB / ring buffer); only the last JSON line is needed.

Cross-refs: serial fetch-then-push (§4), per-company poll spawns multiply RAM by company count (§5), SyncLog double-retention of every line (§6).

**[MEDIUM] Serial per-company loop serializes independent child processes.** `sync-engine.ts:226-240` (`await syncOneCompany` in a `for...of`) — total wall-clock = Σ per-company durations; child N+1 doesn't spawn until N exits. **Fix:** consider bounded concurrency (pool of 2) *only if* TallyPrime tolerates concurrent XML-RPC reads; otherwise keep serial and the memory/IPC fixes matter more (only one buffer live at a time). Do **not** go unbounded — single Tally port + N engines = RAM/connection pressure.

---

## 3. Background thread (Electron)

Architectural note up front: **no CPU work is offloaded to `worker_threads`/`utilityProcess`.** SyncEngine, PushQueuePoller, and LocalPushServer all run on the Electron **main** event loop; the heavy lifting is correctly in spawned **Python child processes** (true OS processes), so the main thread is mostly I/O-bound glue. The real background-thread issues are *child-process lifecycle hygiene*, not CPU on the main thread.

**[MEDIUM] LocalPushServer never reaps in-flight `tally_pusher` children.** `local-push-server.ts:166-172`: `stop()` is just `server.close(); this.server = null;` — the worker spawned at `:230` is a local var, never stored on `this` (class has only `server`, `port`). Verified: zero `.kill()`/SIGTERM/tracking anywhere in the file; both `before-quit` (`index.ts:117`) and the auto-updater `beforeInstall` (`index.ts:104`) call `stop()` synchronously with no await. The durable harm is the **updater path**: the new version installs/relaunches while an orphaned `tallybridge-engine.exe` still holds Tally:9000.
**Fix:** track children in a `Set<ChildProcess>`, `add` on spawn / `delete` on close, and in `stop()` `kill('SIGTERM')` in-flight workers (tree-kill the bundled `.exe`), escalate to SIGKILL after a grace timer, then `server.close(cb)`; make `stop()` return a Promise and await it in the `beforeInstall` path.

**[MEDIUM] Idle-watchdog `setInterval` busy-polls every 5s for the whole child lifetime.** `sync-engine.ts:393-394, 495-521` — a 5s interval wakes the loop purely to compare `Date.now() - lastActivityAt`, plus a hard-timeout `setTimeout`; neither is `.unref()`'d, so they keep the loop alive. **Fix:** replace the polling interval with a single resettable `setTimeout(idleTimeoutMs)` re-armed in `touchActivity()`; `.unref()` both timers.

**[MEDIUM] No timeout on push/poll child workers — a wedged Tally hangs the promise forever.** `local-push-server.ts:215-266` resolves only on `close`/`error`; if Tally stalls (modal/locked company) the child never exits, the HTTP response never sends, and the stdout buffer grows unbounded. Same no-timeout shape in `push-queue-poller.ts:136-196`. **Fix:** add a watchdog `setTimeout(() => proc.kill('SIGKILL'), deadlineMs)` cleared on close; reject/504 on timeout.

**[LOW] Pause-kill `taskkill /t` is fire-and-forget and unawaited** (`sync-engine.ts:112-119`) — async teardown races a subsequent `resume()→scheduleNext(0)→spawn`. Verified mostly safe because `currentProc`/`activeChildRunId` reset on `'close'` and `resume()` defers via `resumeImmediately` while `activeRunId!=null`. **Fix:** harden by capturing taskkill failure and ensuring the idle/hard fallback finalize still runs if `'close'` never fires.

Cross-refs: poller spawns full engine when idle (§1), no concurrency cap on inbound POSTs (§5), startup-eager sync/updater (§7), Tally retry/backoff (§4).

---

## 4. Memory & thread blocking

This section covers synchronous main-thread stalls and blocking I/O.

**[MEDIUM] Inbound push body buffered + `JSON.parse`d synchronously on the main thread.** `local-push-server.ts:42-75, 197-204`: `readJsonBody` does `Buffer.concat(chunks).toString('utf8')` then `JSON.parse` (up to `MAX_REQUEST_BYTES=1MB`) on the main event loop that also drives IPC to the renderer — a ~1MB voucher payload is a synchronous parse stall, and concurrent pushes serialize these stalls. **Fix:** main only needs `company_name`; stream-forward the raw body to the Python worker (which already gets `JSON.stringify(payload)` on stdin at `:263`) and parse minimally, or defer parse via `setImmediate`.

**[MEDIUM] No retry/backoff against Tally — a slow-but-up Tally blocks the worker for the full 45–75s read-timeout then hard-fails.** `tally_client.py:89-119` issues a single blocking `SESSION.post` (read timeout 45s default, 75s floor for `get_vouchers_collection_tdl` at `:430`), no `urllib3.Retry` mounted (verified: zero Retry/HTTPAdapter in the file). The TCP port-9000 pre-flight catches a *down* Tally; it does **not** cover the "accepts TCP but slow to produce the export" warm-up window. Each section is serial through this call, so one slow section stalls the whole run. **Fix:** bounded retry w/ capped backoff (0.5s, 1.5s) for *idempotent reads only* — never for `tally_pusher` writes (duplicate-voucher risk); and a cheap warm-up readiness probe (one light export, short timeout) before the full run, defer/back-off on timeout. Do **not** shorten the 75s floor — large ERP9 exports need it.

**[MEDIUM] `verify_remote_sync_completion` busy-waits up to 180s with `time.sleep`.** `cloud_pusher.py:422-447` loops `time.sleep(poll_seconds)` until `now + BACKEND_POST_VERIFY_SECONDS` (default 180s, `:49-53`), each iteration a blocking `fetch_remote_alter_ids()` (its own up-to-60s timeout, new connection), triggered after a render upload ReadTimeout (`:580-584`). On top of the elapsed upload timeout this serializes everything behind it for ~3 min. **Fix:** cap the verify window aggressively, back off the poll interval, use a short per-call timeout during verify.

**[MEDIUM] ODBC helper `stdout.readline()` has no per-read timeout — a hung helper hangs the engine forever.** `odbc_bridge.py:254-279`: `_send` writes the command then `readline()` (`:270`) with no timeout/select; `poll()` (`:259`) only catches an already-exited process, not a hung one. The PS helper is long-lived; one stuck query stalls the whole ODBC fallback. **Fix:** read on a thread with `join(timeout)` (or `select`) keyed to the payload's `timeout_seconds` + margin; on timeout kill+restart the helper and surface an error.

**[MEDIUM] Fully serial fetch-then-push with zero overlap.** `sync_main.py:1514-1761` fetches groups→ledgers→vouchers→stock→outstanding→reports strictly in series (each a blocking Tally call); `push()` only at `:1823` after *all* sections. The backend sits idle during the entire (minutes-long) Tally extraction and Tally sits idle during the upload; the lone heartbeat thread (`:333-348`) does no real work. This also forces worst-case peak RAM (§2). **Fix:** overlap extraction and upload — POST masters while vouchers are still being pulled (extend direct mode's masters-first behavior), or fetch independent sections via a small bounded `ThreadPoolExecutor`.

**[LOW] Logger writes synchronously to `process.stdout` per line, even packaged.** `logger.ts:42` unconditional `process.stdout.write(line)` + stream write; `initLogger` monkey-patches `console.*` (`:64-77`, invoked `index.ts:14`), so chatty SyncEngine logs incur a sync syscall per line. **Fix:** guard `if (isDev || process.stdout.isTTY)`; rely on the async WriteStream for the file log.

**[LOW] `save-settings` does 15 synchronous full-config atomic writes back-to-back.** `ipc-handlers.ts:542-556` — each `store.set` serializes the *entire* AppConfig (incl. `companies[]`) and atomic-writes synchronously; the renderer's `invoke()` can't resolve until all 15 finish. (Verified 15, not 14; conf supports object-form set.) **Fix:** collapse into a single `store.set({...})` → one serialize + one write. LOW because it's a rare user-triggered click on a few-KB file.

**[LOW] `updateCompanyStatus` rewrites whole `companies[]` to disk on every status transition.** `store.ts:103-111` → 2 full-config atomic writes per company per run (`sync-engine.ts:231` 'syncing' + one terminal at `:415/455/469`). **Fix:** keep transient 'syncing' in memory only (already emitted over IPC), persist only terminal transitions; treat unknown state as idle on startup to preserve crash recovery.

Cross-refs: render single-shot POST (§1), whole-payload retention (§2), per-line IPC blocking (§6), 60s Tally-down retry is correctly sleep-based not a spin loop (§5).

---

## 5. Resource block / unblock

This section covers Tally:9000 contention/serialization and timer/handle hygiene.

**[MEDIUM] No concurrency cap or back-pressure on `POST /push-voucher`.** `local-push-server.ts:192-204, 215-266`: `handleRequest` spawns one full engine per accepted POST — no counter, queue, or semaphore. Verified externally reachable: `backend/src/routes/push-voucher.ts` is a stateless forwarder to `127.0.0.1:3002` with no serialization, so N concurrent/retried client calls fork N engines all opening Tally:9000, whose XML gateway locks per company → contention + multiplied interpreter RAM. **Fix:** a process-wide per-company async mutex (p-limit(1) per company key) acquired by *both* `runPythonPushWorker` and `pollCompanyQueue` before spawning; return 503 + `Retry-After` when saturated. Per-company granularity so unrelated companies still parallelize.

**[MEDIUM] Poller doesn't pause for an in-progress push.** `push-queue-poller.ts:93-126`: `pollOnce` checks `syncEngine.isPaused()`/`isSyncInProgress()` (`:95-113`) but is unaware of LocalPushServer's in-flight `push_voucher` workers (the two classes share no coordination). So a 5s `poll_push_queue` engine can hit Tally:9000 while a cloud push is mid-write on the same company. **Fix:** the same shared single-flight/mutex around *all* Tally:9000 access (sync, poll, push); skip companies with an in-flight push. (The poller does correctly self-serialize its *own* spawns via the sequential loop + guards — verified.)

**[LOW] Double TallyPrime port preflight on startup.** `sync-engine.ts:57-61` (`waitForTallyThenSync`) and `:207` (`runAllCompanies`) each open a TCP socket to 9000 (each up to 3s, `:87-91`) before any work. **Fix:** pass a `skipPreflight` flag for the `'startup'` trigger.

**[LOW] Orphan 1000ms finalize-fallback timers never tracked/cleared.** `sync-engine.ts:508, 520`: after a timeout fires `kill()`, `setTimeout(() => finalize(1, ...), 1000)` is scheduled but never stored/cleared; it fires harmlessly (settled guard) but holds the loop ~1s and pins the closure (capturing `outputLines`/`errorOutput`) from GC. **Fix:** store and clear it in `finalize()`; clear the idle interval immediately after kill so it can't re-arm more fallbacks.

**[LOW] `this.timer` is shared by the 60s Tally-down retry and the normal scheduler with no epoch guard.** `sync-engine.ts:68, 95-100, 169-184` — the `waitForTallyThenSync` retry chain is guarded only by `this.paused`, not a `runId` token, so a `syncNow()`/`resume()` racing a mid-flight retry callback could schedule an overlapping timer. **Fix:** add a generation counter bumped on `stop()`/`syncNow()`/`resume()` and bail the `.then()` if it changed; optionally separate retry-timer vs schedule-timer fields. (The `scheduleNext` clear-before-arm prevents double-scheduling on the normal path — verified safe, no change needed there.)

**[LOW] Logger stream + hourly updater interval never closed on quit.** `logger.ts:35` `WriteStream` never `.end()`'d (buffered tail may not flush on quit-to-install); `updater.ts:76` `setInterval` handle discarded. **Fix:** store/clear the interval and `logStream?.end()` in `before-quit`. Fixed-cost handles, not growing leaks.

**[LOW] `probeOdbcCapabilities` spawns a fresh PowerShell per `check-tally-capabilities` call.** `ipc-handlers.ts:216-303, 642-663` — PS cold-start is hundreds of ms, awaited serially after the XML probe, no debounce/cache → overlapping spawns on rapid UI calls. **Fix:** cache result with short TTL keyed by `(tallyUrl, dsn)`; reuse a single pending promise to dedupe concurrent probes.

**[LOW] ODBC `probe()` re-spawns PowerShell per candidate.** `odbc_bridge.py:147-191` calls `close()` then `_start()` for each candidate. Cached per-`OdbcBridge`-instance, so one-time, but adds a PS cold-start to the sync path whenever ODBC is tried. **Fix:** cache candidate selection across runs; only respawn if the first candidate genuinely failed.

**Verified-safe (so they aren't mistaken for bugs):** the 60s Tally-down retry is a clean `setTimeout` sleep, *not* a spin loop, and always `destroy()`s its probe socket (`sync-engine.ts:55-71`); `resume()`+`scheduleNext` converge safely on one idempotent reschedule (`sync-engine.ts:131-137, 242-272`).

Cross-refs: child-reaping/no-timeout (§3), idle interval (§3).

---

## 6. UI thread vs background thread

The renderer is a React 19 SPA on Electron's UI thread; IPC cleanup is genuinely paired (no classic listener-stacking leak in audited pages). The real UI-thread cost is **high-frequency IPC during a sync** plus a couple of constant timers.

**[MEDIUM] Per-line IPC re-emit floods the renderer for the whole sync.** `sync-engine.ts:523-539, 563-567`: every stdout line / stderr chunk is individually `webContents.send`'d (`emit` at `:528/535`) — each a separate structured-clone IPC message + renderer-side `setState`. A large sync = thousands of messages competing with UI responsiveness; the line is also retained in `outputLines` (double-retention, §2). **Fix:** coalesce lines and flush on a short timer (~100–250ms) or in chunks of N; send the array, not one-per-line. Cuts IPC count by orders of magnitude.

**[MEDIUM] SyncLog re-renders ~500 rows + runs a smooth-scroll animation per line.** `SyncLog.tsx:14-32, 57-65`: each `sync-log` event does `setLogs(prev => [...prev.slice(-500), entry])` (full-list re-render) and a `[logs]`-dependent `useEffect` calls `scrollIntoView({behavior:'smooth'})` — a smooth-scroll that restarts before the next line arrives. Because each IPC event is a separate macrotask, React's auto-batching does **not** coalesce a burst → one re-render + one animation restart per line. (Verified bounded to 500 = render/CPU pressure not a memory leak; only while the SyncLog tab is mounted.) **Fix:** (1) buffer incoming lines in a `useRef` and flush once per `requestAnimationFrame`; (2) change scroll to `behavior:"auto"` and only auto-scroll when already pinned to bottom (one-line change at `:30-32`, kills most of the jank).

**[LOW] SyncLog uses array index as React key.** `SyncLog.tsx:57-58` `key={i}` — with `slice(-500)`, once full, every append shifts indices so React reconciles nearly every row instead of one. **Fix:** stable `id` (monotonic counter / time+seq), `key={log.id}`.

**[LOW] `isError` classification + `toLocaleTimeString` recomputed per line in the hot handler.** `SyncLog.tsx:19-23` — `Intl` formatting + two `toLowerCase().includes` scans per event; a multiplier on the render storm. **Fix:** amortized once events are batched; single lowercased pass / precompiled regex; format time lazily at render.

**[LOW] StatusBar 1Hz `setInterval` re-renders the bottom bar every second for the app's life.** `StatusBar.tsx:37-62` — drives a "Next in m:ss" countdown; mounted for the whole session, so constant UI-thread wakeups that defeat idle throttling (timers correctly cleared, no leak). **Fix:** drive from an absolute target timestamp, re-render only when the displayed string changes, pause on `document.hidden`.

**[LOW] `get-config` ships the entire store (incl. `companies[]` + secrets) over IPC on every read.** `ipc-handlers.ts:518` `() => store.store` structured-clones the full AppConfig including `apiKey`/`controlPlaneApiKey`/`syncIngestKey`. **Fix:** return only the fields the renderer needs (omit `companies[]` — there's a separate `get-companies` — and secrets).

**[MEDIUM] preload `on()` registers listeners with no de-dup.** `preload.ts:51-62` — relies entirely on callers pairing `off()` with the identical reference; a mismatch under repeated mounts leaks listeners (and trips the default 10-listener warning). Audited pages currently pair correctly, so this is a latent-trap not an active leak. **Fix:** track `(channel,callback)` pairs and ignore dupes, or return an unsubscribe closure so callers can't mismatch.

**[LOW] Tally XML parsing runs `matchAll` + per-field `new RegExp` synchronously in IPC handlers.** `ipc-handlers.ts:319-409` (`get-tally-companies` etc.) — O(companies) regex + a fresh `RegExp` per field on the main thread. Bounded by company count, so low. **Fix:** pre-compile field regexes at module scope.

**[LOW] Settings mount chains a Tally date-range round-trip after config load.** `Settings.tsx:157-198` awaits `getConfig` then `loadCompanyDateRanges` serially; gated on a potentially slow Tally read with no client-side timeout. **Fix:** render the form immediately, fetch ranges in parallel/lazily; enforce a bounded timeout in the IPC handler.

Cross-refs: per-line IPC source (§4 blocking, §1 sync), `resetStaleSyncStatuses` did-finish-load send (§7).

---

## 7. Startup latency: Electron start vs TallyPrime readiness

All boot orchestration is one synchronous `app.whenReady().then(...)` on the main thread; the window is `show:false` until `ready-to-show`, so *everything below runs before/around first paint*. The Tally readiness gap (the "TallyPrime has latency on app start" symptom) is **not handled** — see the no-retry/warm-up finding in §4.

**[MEDIUM] Startup init is fully serial inside `whenReady`; nothing non-paint-critical is deferred.** `index.ts:74-95` runs `createWindow()` then SyncEngine/LocalPushServer/PushQueuePoller construction, `localPushServer.start()` (binds TCP 3002), `setupTray()`, `setupIpcHandlers()`, `syncEngine.start()`, `pushQueuePoller.start()` — all in one event-loop turn. `setupTray` (`tray.ts:13-14`) does a synchronous disk read + image resize during the first-paint window. **Fix:** load the window first; move tray/push-server/poller/updater init into the `ready-to-show` handler or a `setImmediate`. Only IPC handlers must exist before the renderer's first `invoke`.

**[MEDIUM] One-time hybrid migration does synchronous store writes *before* `createWindow()`.** `index.ts:66-72` — up to two synchronous full-config atomic disk writes (`syncIngestMode`, then `migratedToHybridV1`) plus the store's lazy initial disk read, all before the BrowserWindow exists, directly delaying first paint. **Fix:** move after `createWindow()`/`loadFile`, combine into one `store.set({...})`. The migration is idempotent and not needed before paint.

**[MEDIUM] Auto-updater `checkForUpdates()` fires eagerly during `whenReady`.** `updater.ts:75-76` — `void autoUpdater.checkForUpdates()` triggers a GitHub release-metadata fetch during the exact window the app creates the window, binds the push server, and kicks off the Tally poll, competing for the boot network/event-loop budget. **Fix:** defer the initial check via `setTimeout(..., 30_000)` or after `ready-to-show`; keep the hourly interval.

**[LOW] Startup sync (port poll → Python spawn) fires eagerly during boot.** `index.ts:94` `syncEngine.start()` → `waitForTallyThenSync()` → on success `runAllCompanies('startup')` spawns the engine in the same boot turn. The port check itself is non-blocking (`sync-engine.ts:84-92`), so this is contention/scheduling, not event-loop blocking. **Fix:** defer `syncEngine.start()` until after `ready-to-show`.

**[LOW] `resetStaleSyncStatuses` does a synchronous full-`companies[]` rewrite on the startup path.** `ipc-handlers.ts:512-516` → `store.set('companies', ...)` at `store.ts:129` before the UI is interactive. **Fix:** defer off the critical path (its only consumer is the post-`did-finish-load` IPC send).

Cross-refs: serial company loop (§2), double port preflight (§5), poller idle spawns (§1), no Tally retry/warm-up probe (§4) — *the actual readiness-gap fix*, logger sync stdout writes (§4).

---

## Prioritized roadmap

*Quick wins first, then structural.*

1. Defer auto-updater initial `checkForUpdates`, the hybrid migration, and `syncEngine.start()` off the boot path (after `ready-to-show`); move tray/push-server/poller init out of the synchronous `whenReady` turn — **S** — §7 (§3).
2. Collapse `save-settings` 15 writes → one `store.set({...})`; keep transient 'syncing' status in memory only — **S** — §4.
3. Replace `outputLines[]` with a single `lastJsonLine`; cap push-worker stdout/stderr to a bounded tail — **S** — §2 (§6).
4. SyncLog: `behavior:"auto"` + pin-to-bottom check, and `requestAnimationFrame`-buffered flush; stable row keys — **S** — §6.
5. Drop the redundant `estimate_payload_bytes(payload)` whole-payload serialize; derive total from cached `section_metrics` — **S** — §1 (§2).
6. Fetch `company_alter_ids` once per run; reuse the parsed dict — **S** — §1.
7. Guard logger `process.stdout.write` behind `isDev || isTTY`; close logStream + clear updater interval on quit — **S** — §4 (§5).
8. Add a `requests.Session()` to `cloud_pusher`; decode Tally response once (`del content`, single utf-16 attempt) — **S** — §1/§2.
9. Batch/coalesce `sync-log` IPC emits (array per chunk or ~150ms timer) instead of one send per line — **M** — §6 (§4).
10. Move the poller's "any pending jobs?" check into TS; only `spawn` the engine when ≥1 voucher pending; add empty-poll backoff — **M** — §1 (§3).
11. Track + reap push/poll child processes on `stop()`; add a per-child watchdog timeout; make `stop()` awaitable and await it in `beforeInstall` — **M** — §3 (§5).
12. Add a process-wide per-company Tally:9000 mutex shared by sync/poll/push, with 503+Retry-After back-pressure on inbound POSTs — **M** — §5 (§1/§3).
13. Add bounded retry/backoff for idempotent Tally reads + a warm-up readiness probe before the full run (the actual TallyPrime-startup-latency fix); add a readline timeout to the ODBC helper; cap the 180s verify loop — **M** — §4 (§7).
14. Replace the 5s idle-watchdog interval with a resettable `unref`'d `setTimeout`; clear orphan fallback timers; epoch-guard the Tally-down retry; skip the double startup port preflight — **M** — §5 (§3).
15. Chunk the render-mode voucher POST (mirror direct mode: masters on first chunk, voucher chunks with `chunk_index`/`is_final`), or delete the render single-shot branch and route render through the chunked builder; remove dead `_build_render_payload` — **L** — §1 (§2).
16. Free section lists after their POST (`del payload`/`del` section locals post-`push`; null masters after masters POST; release voucher chunks); dedupe vouchers incrementally across windows — **L** — §2.
17. Overlap Tally extraction with backend upload (stream masters while vouchers are pulled, or bounded `ThreadPoolExecutor` per independent section) — **L** — §4 (§1).
18. Make masters/ledgers/stock genuinely incremental (delta by `alt_mst_id`); enable incremental voucher window by default once validated; narrow the pre-change-detection fallback plan — **L** — §1.
