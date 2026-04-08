# One-Day Fetch Issue — Investigation Log

Date: 2026-04-07

---

## Question

Is the sync fetching voucher data for only one day (31-03-2019)?

---

## Answer

**No. 31-03-2019 is the `to_date` (end of financial year), not both the start and end.**

The sync window is a full financial year. 31-03-2019 appearing in output is the closing date of the books, not a one-day window.

---

## How the Date Window Is Built

### Source: `sync_main.py` → `fetch_company_info_with_fallback()`

```python
from_date = fy_date_from_iso(books_from)
effective_to = min(parse_iso_date(books_to) or date.today(), date.today())
to_date = format_tally_compact(effective_to)
```

### For a company whose FY is April 2018 – March 2019:

| Step | Value |
|---|---|
| Tally returns `BOOKSFROM` | `"20180401"` |
| Parsed to ISO | `"2018-04-01"` |
| Converted to compact → `from_date` | `"20180401"` |
| Tally returns `BOOKSTO` | `"20190331"` |
| Parsed to ISO | `"2019-03-31"` |
| `min("2019-03-31", today)` → `to_date` | `"20190331"` |
| **Final call** | `get_vouchers("20180401", "20190331")` |

This is a full financial year — correct behaviour.

---

## When Would It Actually Fetch One Day

The only scenario where `from_date == to_date == "20190331"` (single day) is if Tally returns `BOOKSFROM = BOOKSTO = "20190331"`. This would be a Tally company configuration issue, not a code bug.

---

## Potential Bug: End Before Start

If Tally returns `BOOKSFROM = "20190401"` and `BOOKSTO = "20190331"` (end before start — mismatched FY), then:

- `from_date` = `"20190401"`
- `to_date` = `"20190331"`

Tally Day Book would return 0 vouchers for this invalid window. No error is raised — the sync completes silently with 0 vouchers.

This is worth guarding against. A fix would be:

```python
if parse_tally_compact_date(from_date) and parse_tally_compact_date(to_date):
    if parse_tally_compact_date(to_date) < parse_tally_compact_date(from_date):
        print(f"[Tally] WARNING: to_date ({to_date}) is before from_date ({from_date}), resetting to fallback")
        from_date, to_date = get_fy_dates_fallback()
```

---

## How to Verify at Runtime

The log line already exists in `sync_main.py` at line 520:

```python
print(f"[Tally] Company FY: {books_from} to {books_to}")
```

And at line 578:

```python
print(f"[Tally] Date range: {from_date} to {to_date}")
```

Run the sync with Tally open and look for these lines in stdout to confirm the exact window being used.

---

## Files Involved

| File | Role |
|---|---|
| `src/python/sync_main.py` | Computes `from_date` / `to_date` from company info |
| `src/python/xml_parser.py` | Parses `BOOKSFROM` / `BOOKSTO` from Tally XML via `parse_tally_date()` |
| `src/python/tally_client.py` | Sends `<SVFROMDATE>` / `<SVTODATE>` in `YYYYMMDD` format to Day Book |

---

## Date Format Note

`get_vouchers()` in `tally_client.py` uses `YYYYMMDD` format without a `TYPE="Date"` attribute:

```xml
<SVFROMDATE>{from_date}</SVFROMDATE>
<SVTODATE>{to_date}</SVTODATE>
```

This is the correct format for Tally's Day Book report. Other requests (`get_company_alter_ids`) use `TYPE="Date"` with `DD-Mon-YYYY` format — that is a different context (Collection queries, not report exports). No mismatch issue here.
