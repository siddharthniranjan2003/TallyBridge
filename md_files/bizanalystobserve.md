# Biz Analyst — Observation & Interception Guide

## Goal
Understand how Biz Analyst fetches data from Tally ERP 9 — what requests it sends, what methods it uses, and what responses it gets — so we can replicate or improve on its approach in TallyBridge.

---

## Methods Biz Analyst May Use

| Method | How It Works | Interceptable? |
|---|---|---|
| XML over HTTP | POST XML to Tally's HTTP server on port 9000 | Yes — Wireshark or proxy |
| ODBC | Windows ODBC driver → SQL-like queries → Tally | Yes — API Monitor or ODBC Trace |
| Direct file access | Read Tally's `.900` data files from disk | Yes — Process Monitor |
| TDL injection | Drop a `.tdl` file into Tally's config, adds new collections | Yes — check Tally config folder |

---

## Step 1: Find Out Which Methods It Uses

Run **Process Monitor** (Sysinternals — free):
1. Download from: https://learn.microsoft.com/en-us/sysinternals/downloads/procmon
2. Open Process Monitor → Filter → `Process Name is BizAnalyst.exe`
3. Trigger a sync in Biz Analyst
4. In Process Monitor, check:
   - **File** operations → sees if it opens any `.900` Tally data files or `.tdl` files
   - **Network** operations → sees if it connects to port 9000

This tells you which methods it actually uses before you go deeper.

---

## Step 2: Intercept XML over HTTP (Port 9000)

### Option A — Wireshark (captures raw packets)
1. Open Wireshark
2. Capture on **Npcap Loopback Adapter** (loopback / 127.0.0.1)
3. Filter: `tcp.port == 9000`
4. Trigger Biz Analyst sync
5. Right-click any packet → **Follow → TCP Stream**
6. You'll see raw XML requests and responses

Note: Tally uses **UTF-16LE encoding** for requests — Wireshark will show garbled text unless you set the stream encoding to UTF-16.

### Option B — Python Logging Proxy (cleaner, readable output)
- Run a proxy on port 9001 that forwards to Tally on 9000
- Configure Biz Analyst to connect to port 9001 instead of 9000
- Proxy logs both directions to a clean file
- Only works if Biz Analyst lets you configure the Tally port

---

## Step 3: Intercept ODBC Calls

### Option A — Windows Built-in ODBC Trace
1. Open **ODBC Data Source Administrator** (search in Start)
2. Go to **Tracing** tab
3. Set log file path (e.g. `C:\odbc_trace.log`)
4. Click **Start Tracing Now**
5. Trigger Biz Analyst sync
6. Click **Stop Tracing**
7. Open the log file — shows every SQL query and result

### Option B — API Monitor (most detailed)
1. Download from: http://www.rohitab.com/apimonitor
2. Open API Monitor → File → Monitor New Process → select `BizAnalyst.exe`
3. In the API filter, enable **ODBC 3.x** category
4. Trigger sync
5. See every `SQLExecDirect`, `SQLFetch`, etc. with actual query strings and data

---

## Step 4: Check for TDL Injection

Look for `.tdl` files that Biz Analyst may have installed into Tally:
- `C:\Users\<user>\AppData\Roaming\Tally.ERP9\`
- Tally's install directory (usually `C:\Program Files\Tally.ERP9\`)
- Tally's configuration file `Tally.ini` — look for `TDL=` lines pointing to Biz Analyst TDL files

If TDL files are found, open them — they define custom collections and reports which reveal exactly what data Biz Analyst pulls.

---

## What to Look For

Once you're capturing traffic, look for:

1. **Which sections it fetches** — ledgers, vouchers, stock, cost centres, etc.
2. **Date filtering** — does it use `SVFROMDATE`/`SVTODATE`? What format? Does ERP 9 honour it?
3. **Request shape** — `EXPORTDATA/REPORTNAME` (TallyPrime style) vs `TYPE=Data/ID=` (ERP 9 legacy style) vs `TYPE=Collection` (TDL)
4. **FETCH fields** — which fields it asks for per object type
5. **Batch strategy** — does it fetch one month at a time, or dump everything at once?
6. **How it handles the 332-voucher date collapse problem** — this is the key insight we want

---

## Context

TallyBridge already confirmed that on ERP 9 6.6.3:
- Both XML Day Book shapes ignore `SVFROMDATE`/`SVTODATE` and return all vouchers
- TDL collections with nested `.LIST` fields (ledger entries, inventory entries) hang ERP 9's XML server
- We solved this with a two-pass approach (header collection for date scoping + batched detail fetch by MasterID)

Seeing how Biz Analyst solves the same problem — or whether it just dumps all vouchers and filters client-side — would be valuable.
