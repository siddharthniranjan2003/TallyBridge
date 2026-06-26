# Session Handout — Stock Item "Part Code" Investigation (Tally → Supabase)

**Date:** 2026-06-11
**Branch:** `TallyBridge-Backend-Refactor`
**Context:** User asked whether the stock-item **part code / part number** can be fetched from TallyPrime, then whether it's currently being fetched, and whether it exists in Supabase. All answers verified against **live TallyPrime** (port 9000) and the **live client Supabase**.

---

## 0. TL;DR

| Question | Answer |
|---|---|
| Can we fetch item part code from TallyPrime? | **Yes.** `$PartNo` is a valid StockItem method; 3,195 of 13,237 items have a value. |
| What/where is the value? | It equals the stock item's **Mailing Name** (`$PartNo == $MailingName`, verified 0 mismatches). Values look like `07PU-NUM-00087`, `21160363`, `M01DG01`. |
| How to fetch it reliably? | `FETCH MAILINGNAME` (the `PARTNO` tag is a *calculated* method and does **not** serialize via plain `FETCH`). |
| Are we fetching it right now? | **No.** Not in Python fetch output, not in the parser, not sent to backend. |
| Does it exist in Supabase? | **No.** `public.stock_items` has no part-code column. |

**To wire it end-to-end:** (1) Python FETCH + parser emit `part_code`, (2) `ALTER TABLE public.stock_items ADD COLUMN part_code text;`, (3) backend needs no change (it spreads `...s`).

**Status:** Investigation only — **nothing implemented yet.** Awaiting user confirmation that MailingName (not the alias) is the intended "part code."

---

## 1. How the part code was identified in live Tally

TallyPrime was live on `localhost:9000` (confirmed via `Test-NetConnection`). Company has **13,237 stock items**.

### The trap: `FETCH` silently drops unknown method names
A StockItem collection with `<FETCH>NAME, PARTNO, PARTNUMBER, PARTNUM, ITEMPARTNO, ...</FETCH>` returned **STATUS=1 (success)** but emitted **none** of those tags. Tally **omits** both unknown methods *and* empty methods from XML export — so an empty result does **not** distinguish "wrong tag name" from "field exists but empty." (Note: exported tags carry attributes, e.g. `<BASEUNITS TYPE="String">NOS</BASEUNITS>`, so regex must allow attributes.)

### The decisive test: reference the method inside a FILTER expression
Unlike `FETCH`, a `$`-expression on a **non-existent** method throws `LINEERROR`. So:

```xml
<COLLECTION NAME="TBProbe" ISMODIFY="No">
  <TYPE>StockItem</TYPE>
  <FETCH>NAME, PARTNO</FETCH>
  <FILTER>TBHasPart</FILTER>
</COLLECTION>
<SYSTEM TYPE="Formulae" NAME="TBHasPart">NOT $$IsEmpty:$PartNo</SYSTEM>
```

Result: **no LINEERROR** → `$PartNo` is a valid method. Filter matched **3,195 items** → 3,195 items have a non-empty part number.

### Confirming what `$PartNo` actually returns
The 3,195 filtered items each carried a `<MAILINGNAME.LIST>` with a code (e.g. `07PU-LET-00001`). A second filter:

```xml
<SYSTEM TYPE="Formulae" NAME="TBPartNeMail">(NOT $$IsEmpty:$PartNo) AND ($PartNo != $MailingName)</SYSTEM>
```

returned **0 items** → `$PartNo` is **identical to** `$MailingName` for every item. Plain `FETCH MAILINGNAME` **does** serialize the value; `FETCH PARTNO` does not.

### Live sample (15 of 3,195)
```
#  NAME                                   ALIAS        PART CODE (=MailingName)
1  112BH093 IST SET WITH HOLDER 3/32"                  07PU-LET-00001
2  112BH125 IST SET WITH HOLDER 1/8"      FCA0100902   07PU-NUM-00087
3  122501 10.3 SC DRILL PROSTEEL HA 4XD                21160363
4  2109A10 DIAL GAUGE 0.001                            M01DG01
5  2NC CNGA 120408 BNC200                              IN00060T
11 511-701 BORE GAUGE 18-35                            TGBGE16
12 7661 PAWL HOLDER ASSY 7661 BRADMA                   996703939000
```

### Two distinct identifiers per item (user must pick which is "part code")
1. **MailingName / `$PartNo`** — codes like `07PU-NUM-00087` — **3,195 items**. ← recommended; this is Tally's "Part No."
2. **Alias** (a 2nd `<NAME>` in `NAME.LIST`) — like `FCA0100902` — only **69 items**.

### Working request to actually retrieve it (drop-in)
```xml
<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE><ID>StockItem</ID>
  </HEADER>
  <BODY><DESC>
    <STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES>
    <TDL><TDLMESSAGE>
      <COLLECTION NAME="StockItem" ISMODIFY="No">
        <TYPE>StockItem</TYPE>
        <FETCH>NAME, PARENT, BASEUNITS, CLOSINGBALANCE, CLOSINGVALUE, CLOSINGRATE, MAILINGNAME</FETCH>
      </COLLECTION>
    </TDLMESSAGE></TDL>
  </DESC></BODY>
</ENVELOPE>
```

---

## 2. Current pipeline state (why it's NOT being captured)

| Stage | File / line | What it does with part code |
|---|---|---|
| Python fetch | `src/python/tally_client.py:542` | FETCH lists `PARTNO` — calculated method, returns nothing. `MAILINGNAME` not fetched. |
| Parser | `src/python/xml_parser.py:454-461` | Emits only `name, group_name, unit, closing_qty, closing_value, rate`. Reads neither `PARTNO` nor `MAILINGNAME`. |
| Cloud push | `src/python/cloud_pusher.py` | Forwards `stock_items` list as-is. |
| Backend write | `backend/src/routes/sync.ts:2061-2063` | `rows = stock_items.map(s => ({ ...s, company_id, synced_at }))` → upsert into `stock_items` on conflict `company_id,name`. Passes through any field already present; can't invent one. |

Net: even though 3,195 Tally items have a part code, **none** reaches Supabase.

---

## 3. Supabase state (live, client DB)

**Project:** `ztugwhevemibdrzqafyw` — name **"rohan.psom@gmail.com's Project"**, region `ap-southeast-2`, Postgres 17, ACTIVE_HEALTHY.

> ⚠️ **Identity correction:** `ztugw` and "rohan.psom's project" are the **SAME** database (one ref `ztugwhevemibdrzqafyw`), **not two separate ones**. The existing `supabase-db-identity` memory treats them as distinct — it is wrong and should be updated. This is the client/production DB the MCP is connected to.

### `public.stock_items` columns (17,053 rows)
```
id (uuid, pk), company_id (uuid, fk→companies), name (text),
group_name (text), unit (text), closing_qty (numeric),
closing_value (numeric), rate (numeric), synced_at (timestamptz)
```
**No `part_code` / `part_no` / `mailing_name` column.** Upsert conflict key is `company_id,name`.

### ⚠️ Security advisory surfaced by Supabase (critical)
**RLS is DISABLED on all 14 public tables** (companies, groups, ledgers, vouchers, voucher_items, voucher_ledger_entries, purchases, stock_items, outstanding, profit_loss, balance_sheet, trial_balance, sync_log, push_queue). Anyone with the anon key can read/write every row. **Not auto-fixed** — enabling RLS without policies would lock out the backend. Remediation = `ALTER TABLE ... ENABLE ROW LEVEL SECURITY;` per table **plus** policies; user must decide.

### Incidental note — push_queue CHECK constraint
`public.push_queue.status` has `CHECK (status = ANY (ARRAY['pending','pushed','failed']))`. **Implication for the earlier stub work:** a trigger forcing `status='stubbed'` would **violate this constraint** on this DB and fail the insert. (Context from the prior session's `tb_stub_push_queue_trg`; flagged here in case the stub is revisited.)

---

## 4. The 3-step change to capture part code (NOT yet done)

1. **Python** — `src/python/tally_client.py:542`: append `, MAILINGNAME` to the FETCH list.
2. **Parser** — `src/python/xml_parser.py` (~line 454 dict): add `"part_code": safe_str(item.get("MAILINGNAME", "")),`.
3. **Supabase** — `ALTER TABLE public.stock_items ADD COLUMN part_code text;`
4. **Backend** — **no change** (the `...s` spread at `sync.ts:2062` carries `part_code` through once it exists in the dict and the column).

Optionally surface `part_code` in any UI/reporting that lists stock items.

---

## 5. Open items / next steps
- [ ] User to confirm: **MailingName** (3,195 items) is the intended "part code", not the alias (69 items).
- [ ] If confirmed, apply the 3-step change above (Python + parser + ALTER COLUMN).
- [ ] Decide whether to also capture the **alias** as a separate field (only 69 items have one).
- [ ] Update `supabase-db-identity` memory: `ztugwhevemibdrzqafyw` = `ztugw` = "rohan.psom's project" = ONE client DB (not two).
- [ ] (Tangential) Decide on RLS for the 14 exposed tables.
- [ ] (Tangential, from prior session) If revisiting the push_queue stub, note the `status` CHECK constraint forbids `'stubbed'` on this DB.

---

## 6. Quick reference
| Item | Value |
|---|---|
| Tally endpoint | `http://localhost:9000` (live, TallyPrime) |
| Stock items in Tally | 13,237 |
| Items with part code | 3,195 |
| Part code method | `$PartNo` (== `$MailingName`); fetch via `MAILINGNAME` |
| Client Supabase ref | `ztugwhevemibdrzqafyw` (rohan.psom / ztugw — same DB) |
| stock_items rows | 17,053 |
| Part-code column in Supabase | **absent** |
| Files to change | `tally_client.py:542`, `xml_parser.py:~454`, + `ALTER TABLE stock_items` |

## 7. One-line summary
Part code = TallyPrime stock-item **Mailing Name** (`$PartNo`, fetch via `MAILINGNAME`), present on 3,195/13,237 items; **currently fetched nowhere and absent from Supabase `stock_items`** — needs Python+parser emit + one `ADD COLUMN part_code`. Also: `ztugw` and rohan.psom's project are the same DB; RLS is off on all 14 tables.
