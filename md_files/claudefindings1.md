# Claude Findings 1 — ERP 9 Voucher Date Filter Root Cause

Date: 2026-04-08

---

## Summary

ERP 9 returns the same 332 vouchers for every date range because the wrong request envelope is being used. The TallyPrime-style envelope is sent to ERP 9, which ignores the date variables and returns all vouchers. The correct ERP 9 envelope already exists in `tally_client.py` but is never called.

---

## Three Request Shapes in tally_client.py

| Function | Envelope Style | Date Format | Used by ERP 9? |
|---|---|---|---|
| `get_vouchers()` line 349 | `EXPORTDATA/REPORTNAME=Day Book` (TallyPrime style) | `DD-Mon-YYYY` with `TYPE="Date"` | **Yes — current (wrong for ERP 9)** |
| `get_vouchers_legacy_data_request()` line 374 | `TYPE=Data / ID=Day Book` (ERP 9 style) | `YYYYMMDD` without `TYPE="Date"` | **No — exists but not wired** |
| `get_vouchers_collection_tdl()` line 397 | `TYPE=Collection` + inline TDL | `YYYYMMDD` with `TYPE="Date"` | No |

---

## Root Cause 1: Wrong Envelope for ERP 9 Day Book

`get_vouchers()` uses the TallyPrime-style structure:

```xml
<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Export Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <EXPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>Day Book</REPORTNAME>
        <STATICVARIABLES>
          <SVFROMDATE TYPE="Date">01-Apr-2018</SVFROMDATE>
          <SVTODATE TYPE="Date">31-Mar-2019</SVTODATE>
          ...
        </STATICVARIABLES>
      </REQUESTDESC>
    </EXPORTDATA>
  </BODY>
</ENVELOPE>
```

ERP 9 6.6.3 does not recognize the `EXPORTDATA/REQUESTDESC/REPORTNAME` structure. It silently ignores the date variables and returns the full company voucher set (all 332 vouchers, all dated 2019-03-31).

---

## Root Cause 2: Legacy Request Exists But Is Never Called

`get_vouchers_legacy_data_request()` uses the correct ERP 9 structure:

```xml
<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Data</TYPE>
    <ID>Day Book</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
        <SVFROMDATE>20180401</SVFROMDATE>
        <SVTODATE>20190331</SVTODATE>
        <SVCURRENTCOMPANY>...</SVCURRENTCOMPANY>
      </STATICVARIABLES>
    </DESC>
  </BODY>
</ENVELOPE>
```

Key differences from the TallyPrime shape:
- `HEADER/TYPE=Data` + `ID=Day Book` instead of `REPORTNAME`
- `BODY/DESC/STATICVARIABLES` instead of `BODY/EXPORTDATA/REQUESTDESC`
- YYYYMMDD date format **without** `TYPE="Date"` attribute

This function is defined in `tally_client.py` but `fetch_day_book_voucher_window()` in `sync_main.py` calls `get_vouchers()` instead.

---

## Root Cause 3: TDL Collection Has No Date Filter

`get_vouchers_collection_tdl()` puts `SVFROMDATE`/`SVTODATE` in `STATICVARIABLES`:

```xml
<STATICVARIABLES>
  <SVFROMDATE TYPE="Date">20180401</SVFROMDATE>
  <SVTODATE TYPE="Date">20190331</SVTODATE>
</STATICVARIABLES>
```

But a custom `<COLLECTION>` does not automatically filter by `SVFROMDATE`/`SVTODATE`. Those are session-level report variables. Without an explicit `<FILTERS>` formula, the collection returns all vouchers.

The correct filter formula to add:

```xml
<SYSTEM TYPE="FORMULAE" NAME="DateRangeFilter">
  $Date >= $$Date:SVFromDate AND $Date <= $$Date:SVToDate
</SYSTEM>
```

And wire it in the collection:

```xml
<COLLECTION NAME="Vouchers" ISMODIFY="No">
  <TYPE>Voucher</TYPE>
  <FILTERS>NotCancelledVouchers,NotOptionalVouchers,DateRangeFilter</FILTERS>
  <FETCH>...</FETCH>
</COLLECTION>
```

---

## The Fix

### Fix 1 (immediate): Wire legacy request for ERP 9 Day Book

In `fetch_day_book_voucher_window()` in `sync_main.py`, change:

```python
rows = parse_vouchers(get_vouchers(from_date, to_date))
```

to:

```python
rows = parse_vouchers(get_vouchers_legacy_data_request(from_date, to_date))
```

This uses the correct ERP 9 envelope with YYYYMMDD dates and no `TYPE="Date"` attribute.

### Fix 2 (for TDL collection path): Add date filter formula

In `get_vouchers_collection_tdl()` in `tally_client.py`, add `DateRangeFilter` to `<FILTERS>` and add the formula `<SYSTEM>` element. Without this the TDL collection path will also ignore date ranges.

---

## Files Involved

| File | Location | Issue |
|---|---|---|
| `src/python/tally_client.py` | lines 349–394 | `get_vouchers()` is TallyPrime-only; `get_vouchers_legacy_data_request()` exists but unused for ERP 9 |
| `src/python/tally_client.py` | lines 397–432 | `get_vouchers_collection_tdl()` missing date filter formula |
| `src/python/sync_main.py` | `fetch_day_book_voucher_window()` | calls wrong Day Book function for ERP 9 |

---

## Expected Outcome After Fix

- ERP 9 Day Book returns different voucher counts for different date windows
- Monthly batch windows return only that month's vouchers
- `validate_voucher_batch()` no longer aborts on single-date collapse
- `section_sources["vouchers"]` reports `"xml_daybook"` with correct date coverage
