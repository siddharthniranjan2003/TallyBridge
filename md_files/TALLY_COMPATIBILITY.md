# TallyBridge — Tally Version Compatibility Deep-Dive Report

## What Was Tested (dev captures)

The `/tally-responses/` folder shows the app was developed and tested against **one instance of TallyPrime** (likely 2.x–4.x based on REMOTEID GUID format and UTF-8 responses). No ERP 9 responses were captured. This is the core risk.

---

## Tally Version Landscape

| Version | Era | HTTP XML | GUID support | UTF-16 | ODBC DSN name |
|---------|-----|----------|--------------|--------|---------------|
| Tally 7.2 / 8.1 / 9 | 2004–2009 | Basic only | No | Yes | N/A |
| Tally ERP 9 R1–R3 (2009–2013) | Legacy | Yes | Partial | Yes | `TallyODBC_<port>` |
| Tally ERP 9 R4–R5 (2013–2017) | Mainstream | Yes | Yes | Yes | `TallyODBC_<port>` |
| Tally ERP 9 R6.x (2017–2020, last = 6.6.3) | GST era | Yes | Yes | Yes | `TallyODBC_<port>` |
| TallyPrime 1.x (2020–2021) | Transition | Yes | Yes | Sometimes | `TallyODBC64_<port>` |
| TallyPrime 2.x (2022) | Active | Yes | Yes | Rarely | `TallyODBC64_<port>` |
| TallyPrime 3.x (2023) | Active | Yes | Yes | No | `TallyODBC64_<port>` |
| TallyPrime 4.x (2023–2024) | Active | Yes | Yes | No | `TallyODBC64_<port>` |
| TallyPrime 5.x (2024–2025, latest) | Current | Yes | Yes | No | `TallyODBC64_<port>` |

---

## Feature-by-Feature Compatibility Analysis

---

### 1. Product Detection (`detect_tally_product`)

**How it works:** GET to `TALLY_URL`, looks for `"Tally.ERP 9"` or `"TallyPrime"` string in the response body. The result drives the entire voucher strategy.

**Risk — HIGH:**

- TallyPrime 1.x sometimes returns `"TallyPrime 1.0"` — matched ✅
- Tally.ERP 9 R3.x returns `"Tally.ERP 9"` — matched ✅
- Tally.ERP 9 R3 and earlier: HTTP root may return a generic HTML page with no product string → `product_name = None` → code falls through to TallyPrime voucher path → wrong path for ERP 9, will likely fail or return empty vouchers
- No sub-version distinction at all — the code treats all ERP 9 the same and all TallyPrime the same

---

### 2. Company Discovery (`get-tally-companies` IPC)

**How it works:** TDL Collection with `FETCH NAME, GUID, BASICCOMPANYFORMALNAME`; falls back to `<NAME>` tag scrape.

| Version | Status | Notes |
|---------|--------|-------|
| ERP 9 R4+ | ✅ Works | GUID present, `BASICCOMPANYFORMALNAME` present |
| ERP 9 R1–R3 | ⚠️ Partial | GUID may be absent; `BASICCOMPANYFORMALNAME` absent → fallback NAME scrape works |
| TallyPrime all | ✅ Works | GUID always present |

---

### 3. Company Info (`BOOKSFROM`, `BOOKSTO`, `GUID`, GST fields)

**How it works:** Collection with FETCH list; falls back to legacy `parse_company_info`.

| Field | ERP 9 R1–R3 | ERP 9 R4–R6 | TallyPrime |
|-------|------------|------------|-----------|
| `BOOKSFROM` | ✅ | ✅ | ✅ |
| `BOOKSTO` | ✅ | ⚠️ Often empty | ✅ |
| `GUID` | ⚠️ May be absent | ✅ | ✅ |
| `GSTREGISTRATIONTYPE` / `PARTYGSTIN` | ❌ Pre-GST | ✅ (R6.4+) | ✅ |

Code handles missing `BOOKSTO` with FY derivation — safe. Missing GUID falls back to name — safe.

---

### 4. Change Detection (`ALTERID`, `ALTVCHID`, `ALTMSTID`)

**How it works:** Collection fetching `ALTERID`, `MASTERID`, `CMPVCHID`, `ALTVCHID`, `ALTMSTID`, `LASTVOUCHERDATE` from Company object.

| Version | Status | Notes |
|---------|--------|-------|
| ERP 9 R4+ | ✅ | All fields present |
| ERP 9 R1–R3 | ⚠️ | `ALTERID` / `ALTMSTID` may be absent → `parse_alter_ids` returns zeros → triggers full sync (safe fallback) |
| TallyPrime all | ✅ | All present |
| `LASTVOUCHERDATE` | ⚠️ | Not documented as universally available; if absent, incremental voucher sync disabled (safe) |

---

### 5. Vouchers — The Most Critical Section

The code has two completely separate voucher strategies gated on `product_name == "Tally.ERP 9"`:

#### TallyPrime path (default / fallback when detection fails)

```python
fetch_structured_section("vouchers")     # definition-driven TDL Collection
  + get_vouchers_collection_tdl()        # inline TDL with FETCH list
  + fetch_day_book_voucher_window()      # only if ALLOW_DAYBOOK_FALLBACK=1
```

| Version | Status | Risk |
|---------|--------|------|
| TallyPrime 2.x–5.x | ✅ Likely works | Developed against this |
| TallyPrime 1.x | ⚠️ Unknown | Not tested; inline TDL may behave differently |
| ERP 9 (mis-detected) | ❌ Will likely fail | TDL Collection with `ALLINVENTORYENTRIES.LIST` FETCH does NOT work reliably on ERP 9 — either returns empty or crashes Tally |

#### ERP 9 path (only when `"Tally.ERP 9"` detected)

```python
get_voucher_headers_erp9()              # lightweight header collection, UTF-16 request
  + get_voucher_details_erp9_batch()   # 25-voucher MasterID batch fetch
```

`parse_voucher_headers` uses regex: `r"(<VOUCHER\b[^>]*REMOTEID=.*?</VOUCHER>)"` — requires `REMOTEID` attribute on each `<VOUCHER>` tag.

| Version | Status | Risk |
|---------|--------|------|
| ERP 9 R6.x | ✅ | `REMOTEID` attribute present (confirmed in captured XML) |
| ERP 9 R4–R5 | ✅ Likely | `REMOTEID` introduced with GUID support |
| ERP 9 R1–R3 | ❌ | `REMOTEID` absent → regex matches nothing → 0 vouchers returned, no error raised |
| TallyPrime (mis-detected as ERP 9) | ⚠️ | Won't happen by logic, but if detection inverted, this path would silently return partial data |

**Silent failure:** If `parse_voucher_headers` returns 0 rows (because `REMOTEID` is absent), `validate_voucher_batch` raises `ValueError("Voucher batch returned no rows")` and the sync errors out. So it fails loudly — but with a confusing message.

---

### 6. Financial Reports (P&L, Balance Sheet, Trial Balance)

**How it works:** `TYPE=Data/ID=<report name>` → regex scraping of display tags.

Tags scraped:
- **P&L:** `DSPDISPNAME`, `PLSUBAMT`, `BSMAINAMT` (fallback)
- **Balance Sheet:** `DSPDISPNAME`, `BSMAINAMT` / `BSSUBAMT` or `DSPCLDRAMT` / `DSPCLCKAMT`
- **Trial Balance:** `DSPDISPNAME`, `DSPCLDRAMT`, `DSPCLCKAMT`

The captured responses (`09_profit_and_loss.xml`, `08_trial_balance.xml`) confirm these tags exist in the current build. However:

| Risk | Detail |
|------|--------|
| `PLSUBAMT` is always empty in captured data | The code falls back to `BSMAINAMT` — this fallback path is what actually works, meaning `PLSUBAMT` is already broken and silently ignored |
| TallyPrime 4.0+ report restructure | Tally changes internal report rendering tags between major versions; `DSPDISPNAME` has been stable but amount tags (`PLSUBAMT`, `BSMAINAMT`) vary |
| ERP 9 older builds | These same display tags should exist (they're the standard XML export format since Tally 9), so reports are the most version-stable section |

---

### 7. Outstanding (Bills Receivable / Payable)

**How it works:** `TYPE=Data/ID=Bills Receivable` → regex scraping of `BILLFIXED`, `BILLCL`, `BILLDUE`, `BILLOVERDUE`, `BILLDATE`, `BILLREF`, `BILLPARTY`.

Captured response confirms the tag structure. This is the **oldest and most stable Tally report format** — unchanged since Tally 7.x.

| Version | Status |
|---------|--------|
| All ERP 9 | ✅ Stable |
| All TallyPrime | ✅ Stable |

---

### 8. Stock Items

**How it works:** 3-tier fallback:
1. `fetch_structured_section("stock_items")` — Collection with FETCH
2. `parse_stock(get_stock_items())` — Collection with FETCH (legacy parser)
3. `parse_stock(get_stock_summary_report())` — `TYPE=Data/ID=Stock Summary`

The stock summary report fallback scrapes `DSPDISPNAME`, `DSPCIQTY`, `DSPCLRATE`, `DSPCLAMT`.

Captured data shows: `DSPCIQTY` and `DSPCLAMT` are empty (no stock value/rate returned). The parser returns `closing_value=0`, `rate=0` silently. **This is a data quality bug present in the current version, not version-specific.**

---

### 9. ODBC

**How it works:** PowerShell helper tries DSN names `TallyODBC64_9000` then `TallyODBC_9000`.

| Version | DSN format | Status |
|---------|-----------|--------|
| ERP 9 all | `TallyODBC_9000` | ✅ Probed (second candidate) |
| TallyPrime 1.x–5.x | `TallyODBC64_9000` | ✅ Probed (first candidate) |
| TallyPrime ODBC license | Separate add-on | ⚠️ Most users won't have it |
| SQL field names (`$Name`, `$Parent`) | TDL-SQL syntax | ✅ Same across versions |

ODBC always degrades to XML gracefully — failure is safe.

---

## Summary: What Works, What Doesn't

### ✅ Solid across all versions
- Outstanding (Bills Receivable/Payable) — oldest stable format
- Groups and Ledgers via XML Collection — stable since ERP 9 R4+
- P&L, Balance Sheet, Trial Balance via report ID — display tags stable
- UTF-16 detection and decoding
- Company identification fallback (GUID → name)
- ODBC probe fail-safe

### ⚠️ Works but with caveats
- P&L amounts: `PLSUBAMT` is always empty; `BSMAINAMT` fallback works but is undocumented
- Stock values: `closing_value` and `rate` come back as 0 from current TallyPrime build (tag scraping misses them)
- TallyPrime 1.x vouchers: Untested; inline TDL may behave differently than 2.x+
- ERP 9 R4–R5 vouchers: `REMOTEID` should be present but unverified

### ❌ Known failures

| Scenario | What breaks | Failure mode |
|----------|-------------|--------------|
| ERP 9 with unrecognised HTTP root (no product string) | Entire voucher sync | Silent wrong-path → TDL Collection fails → error |
| ERP 9 R1–R3 | Voucher headers (no `REMOTEID`) | Loud error: "Voucher batch returned no rows" |
| ERP 9 R1–R3 | Change detection (no `ALTVCHID`) | Safe: triggers full sync |
| Stock value/rate on current TallyPrime | `closing_value=0`, `rate=0` in DB | Silent data quality issue |
| Python binary in production | Entire sync crashes | Binary not bundled (from earlier report) |

---

## Recommended Fixes (Tally-specific, priority order)

1. **Harden product detection** — if the root GET fails or returns no product string, probe with a known ERP 9 XML request (`TYPE=Data/ID=Day Book`) and a known TallyPrime request to determine version by response shape, not by string match

2. **`REMOTEID` guard in `parse_voucher_headers`** — if regex returns 0 blocks, emit a clear error: `"ERP 9 voucher headers not found — version may be too old (pre-R4)"`

3. **Stock value fix** — the `CLOSINGVALUE` / `CLOSINGRATE` tags in Collection are present but parsed to 0; check if they need `$STKCVALUE` / `$STKCLOSINGVALUE` tag names instead (version-specific naming)

4. **`PLSUBAMT` dead code** — remove the `PLSUBAMT` regex since it's always empty; use `BSMAINAMT` directly

5. **Sub-version logging** — surface the detected product + version string in the UI (Settings → Capabilities panel), not just in the sync log
