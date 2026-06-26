# TallyBridge ⇄ TallyPrime (port 9000) — Live-Test Runbook

Purpose: observe whether TallyBridge traffic causes TallyPrime to hang/lag, by firing **one isolated interaction at a time**, lightest→heaviest, while watching TallyPrime's UI. Every section cites `file:line`. Built 2026-06-17 from a 51-interaction code map.

---

## Part A — Lifecycle map

How TallyBridge touches TallyPrime from launch to steady state:

1. **Startup TCP pre-flight (wait-for-Tally).** On `app.whenReady` the engine starts a poll loop: a raw `net.Socket` TCP connect to `127.0.0.1:9000` (3s timeout). If 9000 is closed it re-probes every 60s until reachable. No XML — pure socket. `src/main/sync-engine.ts:52-93`; started `index.ts:94`.

2. **Product detection.** Once reachable, the spawned Python does a **plain HTTP GET** to `http://localhost:9000` (no envelope) and string-matches the landing page for `Tally.ERP 9` / `TallyPrime` + version regex. Decides ERP9 two-pass vs TallyPrime Day Book branching. `src/python/tally_client.py:160-195`; call site `sync_main.py:1338`.

3. **Company info + change-detection.** Reads `CompanyInfo` (FY dates/GSTIN/GUID — `tally_client.py:200-231`), optionally an alter-id probe to extend the date range (`sync_main.py:1023`), then the **change-detection gate**: `get_company_alter_ids()` (`tally_client.py:236-266`) → `build_sync_plan()` compares counters vs `.alter_ids_cache.json` (`sync_main.py:1415`). **If nothing changed, the run returns early — zero further 9000 traffic.** This gate is what normally keeps steady-state quiet.

4. **Sync read sequence (fixed order, gated per-section by `need_*` flags):**
   - **Masters:** groups (`sync_main.py:1514`) → ledgers (`:1546`). In auto/hybrid these prefer **ODBC** (PowerShell helper, SQL `SELECT … from Groups/Ledger`); XML collection is fallback.
   - **Vouchers (the heavy path):** `fetch_vouchers_with_batches` splits the FY into **monthly windows** — so **ONE logical "voucher sync" fans into MANY port-9000 requests**, one+ per month. A window that times out is **recursively bisected** into halves down to single-day (`sync_main.py:849-867`), re-issuing requests. On ERP9 each window is **two-pass**: 1 header collection + `ceil(unique_master_ids/25)` detail-batch requests (`sync_main.py:737`). The per-window full collection materializes `LEDGERENTRIES.LIST` / `ALLINVENTORYENTRIES.LIST` / `BILLALLOCATIONS.LIST` (`tally_client.py:395-432`) — prime hang suspect, hence its 75s read-timeout floor.
   - **Stock:** stock items (`sync_main.py:1625`), ODBC-eligible; XML path may issue a 2nd Stock Summary report if sparse.
   - **Outstanding:** always 2 XML requests — receivables + payables (`sync_main.py:1656`).
   - **Financial reports (in order):** P&L (`:1688`) → Balance Sheet (`:1714`) → Trial Balance (`:1740`), always XML, date-ranged.

5. **Cloud → Tally push/write path.** The **only writes.** Two uncoordinated entry points: (a) the **local push server** on `:3002` spawns a Python push worker (`local-push-server.ts:174-266`); (b) the **push-queue poller** spawns a worker per company per tick (`push-queue-poller.ts:128-197`). Both call `tally_pusher.push_vouchers()` → `TALLYREQUEST=Import / TYPE=Data / ID=Vouchers` with `ACTION="Create"`, **one 9000 POST per voucher**, no read-before-write/dedupe (`tally_pusher.py:454-484`, `:528-531`).

6. **Steady-state cadence.** Two forever-timers: **heartbeat sync** every `syncIntervalMinutes` (default 5 min) — TCP pre-flight then one Python child per company sequentially (`sync-engine.ts:169-184`); and the **push-queue poller** every **5s** (`push-queue-poller.ts:9`). The poller *defers* to sync via `isPaused()`/`isSyncInProgress()` (`push-queue-poller.ts:95/108/113`), **but the local push server `:3002` shares no mutex** — a cloud-initiated push can hit 9000 **concurrently** with a running sync. The TCP pre-flight also fires **twice** on the startup path (`sync-engine.ts:207-218`).

**Uncoordinated 9000 contention points:** local push server (`:3002`) vs sync engine vs heartbeat — no shared lock. Within voucher sync: monthly windowing × recursive bisection × ERP9 detail batches = request fan-out multiplier.

---

## Part B — Ordered test runbook (LIGHTEST → HEAVIEST)

> Each section is run **on request, one at a time**, then **pause to observe TallyPrime** before continuing. Writes are gated.

| # | Section | Cost | R/W | Channel | Watch in TallyPrime |
|---|---------|------|-----|---------|---------------------|
| 1 | Startup TCP pre-flight probe | low | R | tcp-9000 | Nothing; any flicker = finding (~3s) |
| 2 | Product identity probe (HTTP GET) | low | R | http-get | Nothing (~1s) |
| 3 | Company info collection | low | R | xml-9000 | Nothing (<1s) |
| 4 | Company alter-ids (change-detection) | low | R | xml-9000 | Nothing (<1s) |
| 5 | Groups master | low | R | xml-9000 | Nothing (~1s) |
| 6 | Ledgers master (extended) | medium | R | xml-9000 | Brief pause on large party sets |
| 7 | Stock items master (closing valns) | medium | R | xml-9000 | Short "Calculating…" on large inventory |
| 8 | Voucher **headers** export, 1 window | medium | R | xml-9000 | Brief pause, no sub-list expansion |
| 9 | Outstanding receivables + payables | medium | R | xml-9000 | Short report-render pause ×2 |
| 10 | P&L → Balance Sheet → Trial Balance | medium ×3 | R | xml-9000 | "Calculating…" per report; watch compounding |
| 11 | ⚠ **HEAVY** full voucher collection, 1 window (sub-lists) | high | R | xml-9000 | **Freeze / "Calculating…" / cursor lag — prime suspect** |
| 12 | ⚠ **HEAVY** full voucher sync, monthly windows + bisection | high | R | xml-9000 | Repeated freeze episodes; does lag accumulate? |
| 13 | ⚠ **CONCURRENCY** 2+ reads at once (no mutex) | high | R | xml-9000 ×2 | Harder/longer freeze than either alone |
| 14 | ⚠ **CADENCE** chunked-with-gaps vs one big request | high | R | xml-9000 | Recovers between chunks? vs one long freeze |
| 15 | 🛑 **WRITE** voucher Import (Sales/Purchase create) | medium | **W** | xml-9000 | **CONSENT REQUIRED — test company only; creates real voucher, no dedupe** |

### Section detail (key envelopes)

- **§1** `net.Socket` connect `127.0.0.1:9000`, 3s, no XML — `sync-engine.ts:52-93`, 2nd probe `:207-218`.
- **§2** `SESSION.get(http://localhost:9000)`, body string-match — `tally_client.py:160-195`.
- **§3** `Export/Collection/CompanyInfo`, `COLLECTION TYPE=Company` FETCH FY/GST/GUID — `tally_client.py:200-231`.
- **§4** `Export/Collection/CompanyAlterIds` FETCH ALTERID/CMPVCHID/ALTVCHID/ALTMSTID, dummy 1970 dates — `tally_client.py:236-266`; gate `sync_main.py:1415`.
- **§5** `Export/Collection/Group` FETCH NAME/PARENT/MASTERID/… — `tally_client.py:271-298`.
- **§6** `Export/Collection/Ledger` FETCH ~20 fields incl balances/GST/bank — `tally_client.py:303-334`.
- **§7** `Export/Collection/StockItem` FETCH closing bal/value/rate (fallback Stock Summary `:551-568`) — `tally_client.py:522-548`.
- **§8** `Export/Collection/VoucherHeadersERP9`, **no LIST sub-entries**, date-ranged, NotCancelled/NotOptional/DateRange — `tally_client.py:435-472`.
- **§9** `Export/Data/Bills Receivable` then `Bills Payable` — `tally_client.py:573-590`, `:593-610`.
- **§10** `Export/Data` ×3: `Profit and Loss`, `Balance Sheet`, `Trial Balance`, date-ranged — `tally_client.py:615-678`.
- **§11** `Export/Collection/Vouchers` FETCH … **+ LEDGERENTRIES.LIST, ALLINVENTORYENTRIES.LIST, BILLALLOCATIONS.LIST**, DateRange filter, 75s timeout floor, date-ranged (start 1 month) — `tally_client.py:395-432`.
- **§12** windowed: `fetch_vouchers_with_batches` monthly + `split_window` bisection + ERP9 1+N/25 — `sync_main.py:1591`, `:849-867`, `:737`; bodies `tally_client.py:395-517`.
- **§13** two read envelopes (e.g. §11 + §10) issued in parallel — contention from `local-push-server.ts:174-266` vs `sync-engine.ts:186-273` vs `push-queue-poller.ts:93-126`.
- **§14** (a) per-month collections with gaps vs (b) one full-FY `ID=Vouchers` collection — `sync_main.py:1591` vs `tally_client.py:395-432`.
- **§15** 🛑 `TALLYREQUEST=Import/TYPE=Data/ID=Vouchers`, `<VOUCHER ACTION="Create">` per voucher, only `TB_PUSH_ALLOWED_TYPES` (Sales/Purchase/GST), **no dedupe** — `tally_pusher.py:454-484`, `:356-451`, `:528-531`; transport `tally_client.py:89-119`.
