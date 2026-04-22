# Review of plan4.md — ODBC vs XML for Tally Data Fetching

## What the plan gets right

**The core transport split is correct.** Vouchers must stay on XML — Tally's ODBC interface cannot expose full voucher entry detail (line items, sub-vouchers, narration, cost centre allocations). ODBC only exposes a flattened summary view. Keeping vouchers on XML with monthly batching + binary split on timeout is the right call.

**Alter ID change detection is the highest-leverage optimization.** When masters haven't changed, skipping the fetch entirely is better than any transport choice. The plan correctly preserves this.

**The fallback chain (ODBC → XML) is the right architecture.** ODBC as an optional accelerator, never a hard dependency, is correct.

---

## Where the plan is weak or incomplete

### 1. ODBC blocks Tally's UI thread
This is the most important thing missing from the plan. When you run an ODBC query against Tally, Tally's engine processes it synchronously — it **freezes the UI** for the duration of the query. For a company with 5,000+ ledgers, the ODBC query can take 30–60 seconds during which Tally is completely locked. The plan has no mention of this risk, no user warning, and no throttling strategy.

### 2. ODBC's advantage over XML for masters is overstated
For masters (groups, ledgers, stock), the XML `Collection + FETCH` request is **also a single HTTP call** that returns all records at once. It's not round-trip per record. For ERP 9 with a well-behaved HTTP server, the difference between ODBC and XML for masters is marginal — typically under 5 seconds even for large datasets. The plan implies ODBC is significantly faster, but the actual win is mainly reliability on older ERP 9 builds where the XML response can be malformed.

### 3. Field coverage gap between ODBC and XML is not audited
Compare the current ODBC query for ledgers:
```sql
Select $Name, $Parent, $OpeningBalance, $ClosingBalance, $MasterID, $Email, ...
```
vs the XML `FETCH`:
```
NAME, PARENT, OPENINGBALANCE, CLOSINGBALANCE, MASTERID, EMAIL, LEDGERPHONE,
LEDGERMOBILE, PINCODE, PARTYGSTIN, LEDSTATENAME, COUNTRYNAME, CREDITPERIOD,
CREDITLIMIT, BANKACCOUNT, IFSCODE, INCOMETAXNUMBER, MAILINGNAME, GUID
```

The ODBC query has the same fields, which is good. But `$IFSCCode` vs `IFSCODE` column name variants across ERP 9 versions are inconsistent — the `sources` array in `odbc_sections.json` only has `["$IFSCCode", "IFSCODE", "IFSCCode"]` and may miss `BANKIFSCCODE` which appears in some ERP 9.6.x builds. The shadow mode comparison only checks row count and key set, **not field values**, so this gap won't be caught automatically.

### 4. The PowerShell bridge is fragile for large datasets
The ODBC fetch timeout is hardcoded at **15 seconds** in `odbc_bridge.py:fetch_section`. For stock items in a manufacturing company with 10,000+ SKUs, this will routinely fail and fall back to XML — making the ODBC path a no-op for the companies that would benefit most. The plan doesn't specify how to surface or tune this.

### 5. ODBC subprocess startup overhead isn't free
The helper process starts lazily on first `_send()`. For a sync that probes then falls back immediately to XML (no ODBC support), you're paying ~2–3 seconds of PowerShell startup time for zero benefit. The probe in `ipc-handlers.ts` (one-shot per `check-tally-capabilities`) and the probe in `odbc_bridge.py` (cached after first call) are separate processes — two PowerShell startups per sync cycle on the desktop path.

### 6. No plan for TallyPrime's newer HTTP capabilities
TallyPrime 3.x+ has better-behaved TDL Collection responses and some builds expose richer `ALTERID`-based filtering. The plan doesn't differentiate between TallyPrime versions at all — it treats "TallyPrime" as a single target. A version gate (`product_version >= 3.0` → use extended fetch) could reduce bandwidth for recent TallyPrime users without ODBC.

---

## Is ODBC the best way to fetch big data from Tally?

**No — and yes, for specific sections, but the plan is already correct about where.**

The real performance killers in order of impact:

| Problem | Best Fix | Status in plan |
|---|---|---|
| Fetching unchanged masters repeatedly | Alter ID change detection | Done |
| Full-year voucher dump timing out | Monthly batching + binary split | Done |
| Large ledger XML response unreliable on ERP 9 | ODBC as fallback/accelerator | Done (but overstated as a win) |
| Tally UI freezes during ODBC queries | Off-hours scheduling or warning | **Missing** |
| ODBC doesn't cover vouchers | Keep vouchers on XML | Done |

ODBC is genuinely useful for **ERP 9 specifically**, where the XML engine is older and sometimes produces truncated or malformed responses for large master exports. For TallyPrime, XML Collection+FETCH is reliable and ODBC adds complexity for marginal gain.

---

## Concrete suggestions

1. **Add a UI warning** in Settings when `readMode` is `auto` or `hybrid`: "ODBC queries may temporarily freeze TallyPrime/ERP 9 while running. Schedule syncs during off-hours for large companies."

2. **Make the ODBC query timeout configurable** via an env var (`TB_ODBC_QUERY_TIMEOUT_SECONDS`) rather than hardcoded `15` — or raise the default to 60s for stock items.

3. **Improve shadow mode comparison** to be field-level, not just key-set-level, so the IFSC/bank field variants get caught before they cause silent data differences.

4. **Deduplicate the probe**: the desktop-side `probeOdbcCapabilities` in `ipc-handlers.ts` and the Python-side `odbc_bridge.py` probe spawn separate PowerShell processes. The result of the Python probe should be passed back through the sync log's final JSON and used to update the UI, rather than having two independent probe paths.

5. **Add a version-specific XML path for TallyPrime 3.x+**: detect `product_version` and use extended ALTERID-filtered collection requests where supported, reducing full master fetches.
