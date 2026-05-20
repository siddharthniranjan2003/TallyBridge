# TallyBridge — Company Selection & Sync Behaviour Analysis
**Session date:** 2026-04-30  
**Branch:** tally-bridge-tallyprime-reorder_level_final_attempt

---

## Context (from previous session — compacted)

The user was asking about creating another instance of TallyBridge specifically for **pushing data INTO TallyPrime** from the office for a live client. They mentioned a branch called `tallybridge-local-only-fallback` and asked if it could handle push-to-Tally functionality with everything running locally.

### Key finding from previous session
- `tallybridge-local-only-fallback` branch = **NOT suitable for push**. It's an old early-stage branch (8 commits) with only XML parsing experiments — no LocalPushServer, no push-invoice routes, no current push infrastructure.
- The **current main branch** already has everything needed:
  - `backend/src/routes/push-invoice.ts` — GST invoice builder
  - `backend/src/routes/push-voucher.ts` — raw passthrough
  - `src/main/local-push-server.ts` — Electron service on port 3002
  - Python `push_vouchers()` in `tally_pusher.py`
- Missing piece for Render → office push: `TALLYBRIDGE_LOCAL_PUSH_URL` on Render must be set to `https://sulphate-swiftly-annoying.ngrok-free.dev/push-voucher`, and ngrok must expose **port 3002** (not 3001).

---

## Question 1: How does TallyBridge treat a company across restarts when a different company is selected in TallyPrime?

### What is stored per company

Each company in the electron-store (`src/main/store.ts`) has:

| Field | Purpose |
|---|---|
| `id` | UUID, generated at creation |
| `name` | Company name from Tally |
| `tallyGuid` | Optional GUID from Tally |
| `enabled` | Boolean — controls whether company is synced |
| `addedAt` | ISO timestamp of when company was added |
| `lastSyncedAt` | ISO timestamp of last successful sync |
| `lastSyncStatus` | `"success" \| "error" \| "syncing" \| "idle"` |
| `lastSyncRecords` | Counts of records synced (groups, ledgers, vouchers, etc.) |
| `lastSyncError` | Error message if sync failed |
| `lastCompletedBackfillSignature` | Tracks manual date-range backfill state |

These persist across restarts. TallyPrime's state has no effect on the store.

---

### Startup sequence

```
TallyBridge launches
  → polls port 9000 every 60s until TallyPrime reachable  (sync-engine.ts:41-54)
  → once reachable: runAllCompanies("startup")             (sync-engine.ts:45)
     → filters store.get("companies") to enabled === true  (sync-engine.ts:130)
     → for each enabled company (sequentially):            (sync-engine.ts:153)
         → updateCompanyStatus → "syncing"
         → spawns Python with TALLY_COMPANY=<company.name>
         → Python sends SVCURRENTCOMPANY in XML requests
         → on success: lastSyncedAt updated, lastSyncStatus = "success"
  → schedules next heartbeat at syncIntervalMinutes (default 5 min)
```

Any company stuck in `"syncing"` from a crash is reset to `"idle"` before startup sync begins — `resetStaleSyncStatuses()` in `ipc-handlers.ts:512`.

---

### Full sync vs incremental

`sync-engine.ts:204`:
```typescript
const forceFullSync = !company.lastSyncedAt || shouldUseManualBackfill;
```

| Scenario | `lastSyncedAt` | Result |
|---|---|---|
| First ever sync | null | **Full sync** — pulls everything |
| Restart after successful sync | has value | **Incremental** — pulls only changes since last sync |
| Manual backfill triggered | any | **Full sync** for the configured date range |
| Heartbeat timer fires | has value | **Incremental** always |

---

### What if a different company is open in TallyPrime?

TallyBridge uses `<SVCURRENTCOMPANY>` in every XML request it sends (`tally_client.py:212`):

```xml
<STATICVARIABLES>
  <SVCURRENTCOMPANY>YourConfiguredCompany</SVCURRENTCOMPANY>
</STATICVARIABLES>
```

This is a **per-request context instruction**, not a "switch active company" command. TallyPrime processes the request in the context of the named company.

| TallyPrime state | What happens |
|---|---|
| Configured company IS open (even in background) | Works — TallyPrime serves data from that company's context |
| Configured company is NOT open at all | TallyPrime returns `STATUS=0` error → Python raises `RuntimeError` → sync fails |
| Configured company is open, different one is active in foreground | Works — `SVCURRENTCOMPANY` doesn't care about foreground |

**Exception — voucher push:** `SVCURRENTCOMPANY` is intentionally **omitted** from voucher/Day Book Collection requests (`definition_extractor.py:62-65`) because TallyPrime crashes with that combination. Those requests execute against whatever company is currently **active on screen** — vouchers land in the wrong company if you've switched.

---

### Push voucher company resolution (`local-push-server.ts:77-96`)

```
Payload has company_name/company field → use that
  └─ No payload company → count enabled companies in store
       ├─ Exactly 1 enabled → auto-use that one
       ├─ 0 enabled → error: "No enabled Tally companies"
       └─ 2+ enabled → error: "Multiple enabled... Include company_name"
```

---

## Question 2: Simple language — what happens when company changes in TallyPrime?

**Sync (read):** Safe. TallyBridge will just error and retry at the next interval if the configured company isn't open.

**Push (write):** Dangerous. It silently goes into whichever company is active in TallyPrime at that moment, because `SVCURRENTCOMPANY` is omitted from voucher imports.

---

## Question 3: How does TallyBridge identify the saved company during heartbeat?

### Simple version

TallyBridge identifies the company using **just the name** — the exact company name saved when it was added. Every heartbeat it says to TallyPrime:

> *"Give me data for a company called 'ABC Traders'"*

TallyPrime either recognizes that name (company is open) and responds, or it doesn't and throws an error. There is no deeper verification.

---

### Complex version — full chain

**Step 1 — Electron store (source of truth)**

`sync-engine.ts:130` filters `store.get("companies")` to `enabled === true` and picks:
- `company.name` → passed as `TALLY_COMPANY`
- `company.tallyGuid` → passed as `TALLY_COMPANY_GUID` (may be empty)

**Step 2 — Python process env vars (`sync-engine.ts:236-237`)**
```
TALLY_COMPANY      = "ABC Traders"    ← display name, always present
TALLY_COMPANY_GUID = "abc-uuid-..."   ← optional, may be empty string
```

**Step 3 — Python builds a cache key (`sync_main.py:75`)**
```python
COMPANY_CACHE_KEY = COMPANY_GUID or COMPANY
```
GUID is preferred if available (more stable than name). Used for **local caching only** — not sent to TallyPrime.

**Step 4 — XML request to TallyPrime (`tally_client.py:214`)**
```xml
<STATICVARIABLES>
  <SVCURRENTCOMPANY>ABC Traders</SVCURRENTCOMPANY>
</STATICVARIABLES>
```
This is a **name-only string match**. TallyPrime does a lookup of open companies by this name.

**Step 5 — No response verification**

TallyBridge never confirms TallyPrime actually served data from the correct company. It trusts the response. If `SVCURRENTCOMPANY` matches an open company → data comes back. If no match → `STATUS=0` error → Python raises `RuntimeError` → sync marks company as `"error"`.

**The GUID is never sent to TallyPrime for identification — it is only used internally for local caching and deduplication in the store.**

---

### One-line summary

> **Heartbeat identifies the company purely by name (string match via `SVCURRENTCOMPANY`). The GUID is used internally for caching only. There is no cross-verification of the response.**

---

## Key file references

| File | Relevant lines | Purpose |
|---|---|---|
| `src/main/sync-engine.ts` | 41-54 | Tally port polling on startup |
| `src/main/sync-engine.ts` | 124-169 | `runAllCompanies()` — heartbeat/startup/manual |
| `src/main/sync-engine.ts` | 204 | `forceFullSync` decision |
| `src/main/sync-engine.ts` | 232-254 | Env vars passed to Python per company |
| `src/main/store.ts` | 4-16 | Company schema |
| `src/main/local-push-server.ts` | 77-96 | `pickCompanyName()` for push |
| `src/python/sync_main.py` | 73-75 | `COMPANY`, `COMPANY_GUID`, `COMPANY_CACHE_KEY` |
| `src/python/tally_client.py` | 9, 214 | `TALLY_COMPANY` → `SVCURRENTCOMPANY` in XML |
| `src/python/definition_extractor.py` | 62-65 | Voucher requests intentionally omit `SVCURRENTCOMPANY` |
| `src/python/tally_pusher.py` | 449-453, 515 | Push import envelope with company |
