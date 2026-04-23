# Plan: Push Vouchers into TallyPrime

## Context

The connector already fetches data FROM TallyPrime (XML Export → parse → cloud_pusher → backend → Supabase). This plan implements the **reverse**: accepting vouchers from the backend and importing them INTO TallyPrime via its XML Import API (`TALLYREQUEST=Import`).

TallyPrime exposes the same HTTP port (default 9000) for both read and write. The write path uses a different envelope shape: `<TALLYREQUEST>Import</TALLYREQUEST>` + `<TALLYMESSAGE>` + `<VOUCHER ACTION="Create">`. The response contains `<CREATED>`, `<ERRORS>`, `<LINEERROR>` tags.

---

## Architecture

```
Web UI / External API
     │
     ▼ POST /api/sync/push-queue
Backend (Express)
     │  stores row in push_queue table (Supabase)
     ▼
Supabase push_queue table
     ▲ GET /api/sync/push-queue  (connector polls)
     │
Python connector (sync_main.py)
     │ calls push cycle
     ▼
tally_pusher.py  ──build_voucher_xml()──▶  TallyPrime HTTP :9000
                 ◀──parse_push_response()──
     │
     ▼ POST /api/sync/push-results
Backend marks job pushed / failed in Supabase
```

---

## Critical files to read/modify

| File | Action |
|------|--------|
| `src/python/tally_client.py` | Reference — `_post()`, `_check_response()`, `_xml_escape()` must be reused |
| `src/python/cloud_pusher.py` | Add `fetch_pending_push_vouchers()` + `mark_push_results()` |
| `src/python/sync_main.py` | Add optional push cycle at end of `main()` |
| `backend/src/routes/sync.ts` | Add `GET /push-queue`, `POST /push-results`, `POST /push-queue` |
| `backend/full_schema.sql` | Reference — existing table shapes for `vouchers`, `voucher_ledger_entries` |
| `backend/supabase_new_tables.sql` | Append `push_queue` table migration |

---

## Step-by-step implementation

### 1. Database migration — `backend/supabase_new_tables.sql` (append)

```sql
CREATE TABLE IF NOT EXISTS push_queue (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id  UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  voucher_payload JSONB NOT NULL,          -- same shape as voucher + ledger_entries + items
  status      TEXT NOT NULL DEFAULT 'pending'
              CHECK (status IN ('pending','pushed','failed')),
  error_message TEXT,
  tally_response JSONB,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  pushed_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_push_queue_company_status
  ON push_queue(company_id, status, created_at);
```

### 2. New file — `src/python/tally_pusher.py`

All XML building lives here. Reuses `_post`, `_xml_escape`, `TALLY_URL`, `TALLY_COMPANY` from `tally_client.py`.

**Key XML rules (from official docs + open-source study):**

| ISDEEMEDPOSITIVE | Amount sign | Meaning |
|-----------------|-------------|---------|
| Yes             | positive    | Debit entry |
| No              | negative    | Credit entry |
| Sum of all amounts must equal 0 for a valid voucher |

```python
# Envelope shape for a 2-ledger Payment voucher
<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Import</TALLYREQUEST>
    <TYPE>Data</TYPE>
    <ID>Vouchers</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY>
      </STATICVARIABLES>
    </DESC>
    <DATA>
      <TALLYMESSAGE xmlns:UDF="TallyUDF">
        <VOUCHER VCHTYPE="{voucher_type}" ACTION="Create">
          <DATE>{YYYYMMDD}</DATE>
          <VOUCHERNUMBER>{voucher_number}</VOUCHERNUMBER>
          <PARTYLEDGERNAME>{party_name}</PARTYLEDGERNAME>
          <NARRATION>{narration}</NARRATION>
          <ALLLEDGERENTRIES.LIST>
            <LEDGERNAME>{ledger_name}</LEDGERNAME>
            <ISDEEMEDPOSITIVE>{Yes|No}</ISDEEMEDPOSITIVE>
            <AMOUNT>{signed_amount}</AMOUNT>
          </ALLLEDGERENTRIES.LIST>
          ...
          <!-- for inventory vouchers: -->
          <ALLINVENTORYENTRIES.LIST>
            <STOCKITEMNAME>{name}</STOCKITEMNAME>
            <ACTUALQTY>{qty} {unit}</ACTUALQTY>
            <BILLEDQTY>{qty} {unit}</BILLEDQTY>
            <RATE>{rate}/{unit}</RATE>
            <AMOUNT>{signed_amount}</AMOUNT>
          </ALLINVENTORYENTRIES.LIST>
        </VOUCHER>
      </TALLYMESSAGE>
    </DATA>
  </BODY>
</ENVELOPE>
```

**Functions to implement:**

```python
def _iso_to_tally_date(iso: str) -> str:
    # "2024-04-23" → "20240423"

def _build_ledger_entry_xml(entry: dict) -> str:
    # entry keys: ledger_name, amount, is_deemed_positive
    # ISDEEMEDPOSITIVE = "Yes" if is_deemed_positive else "No"

def _build_inventory_entry_xml(item: dict) -> str:
    # item keys: stock_item_name, quantity, unit, rate, amount

def _build_voucher_xml_block(voucher: dict) -> str:
    # voucher keys: date, voucher_type, voucher_number, party_name,
    #               narration, ledger_entries (list), items (list)

def _build_import_envelope(vouchers: list[dict], company: str) -> str:
    # wraps one or more TALLYMESSAGE blocks in Import envelope

def parse_push_response(xml_text: str) -> dict:
    # returns {"created": int, "altered": int, "errors": int,
    #          "line_errors": [str], "raw": str}

def push_vouchers(vouchers: list[dict], company: str = TALLY_COMPANY) -> dict:
    # calls _post() from tally_client, then parse_push_response
    # returns aggregated result dict
```

**Response parsing — look for:**
```xml
<IMPORTRESULT>
  <CREATED>1</CREATED>
  <ERRORS>0</ERRORS>
</IMPORTRESULT>
```
and `<LINEERROR>...</LINEERROR>` for per-voucher errors.

### 3. Modify `src/python/cloud_pusher.py`

Add after existing functions:

```python
def fetch_pending_push_vouchers() -> tuple[list[dict], str]:
    # GET {BACKEND_URL}/api/sync/push-queue
    # params: company_guid or company_name
    # returns (list_of_jobs, status)
    # each job: {id: UUID, voucher_payload: dict}

def mark_push_results(job_results: list[dict]) -> bool:
    # POST {BACKEND_URL}/api/sync/push-results
    # body: [{id, status: "pushed"|"failed", error_message, tally_response}]
```

### 4. Modify `src/python/sync_main.py`

Add a push cycle after the existing fetch sync. Controlled by a new env var `TB_ENABLE_PUSH` (default off) so it doesn't break existing deployments:

```python
ENABLE_PUSH = os.environ.get("TB_ENABLE_PUSH", "").strip().lower() in {"1","true","yes","on"}
```

At the end of `main()`, after the existing push to cloud:

```python
if ENABLE_PUSH:
    from tally_pusher import push_vouchers
    print("[Push] Checking for vouchers to push to Tally...")
    pending_jobs, status = fetch_pending_push_vouchers()
    if pending_jobs:
        print(f"[Push] Found {len(pending_jobs)} job(s) to push")
        job_results = []
        for job in pending_jobs:
            result = push_vouchers([job["voucher_payload"]])
            job_results.append({
                "id": job["id"],
                "status": "pushed" if result["errors"] == 0 else "failed",
                "error_message": "; ".join(result["line_errors"]) if result["line_errors"] else None,
                "tally_response": result,
            })
        mark_push_results(job_results)
```

### 5. Modify `backend/src/routes/sync.ts`

Add three new routes at the bottom of the file (before `export default router`):

**`POST /api/sync/push-queue`** — Enqueue a voucher for pushing:
```typescript
router.post("/push-queue", requireApiKey, async (req, res) => {
  // Accepts: { company_id|company_guid|company_name, voucher_payload }
  // Inserts a row into push_queue with status='pending'
})
```

**`GET /api/sync/push-queue`** — Connector polls for pending work:
```typescript
router.get("/push-queue", requireApiKey, async (req, res) => {
  // Query by company, return status='pending' rows
  // Returns: { jobs: [{id, voucher_payload}] }
})
```

**`POST /api/sync/push-results`** — Connector reports outcomes:
```typescript
router.post("/push-results", requireApiKey, async (req, res) => {
  // body: { results: [{id, status, error_message, tally_response}] }
  // Updates each push_queue row
})
```

---

## Amount sign convention (critical)

The existing parsed voucher data in `voucher_ledger_entries` already stores `is_deemed_positive` (bool) and `amount` (NUMERIC). The push path must honour the same convention:

- `is_deemed_positive = true` → `<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>`, amount positive
- `is_deemed_positive = false` → `<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>`, amount negative

For a voucher pulled from the DB, `amount` in `vouchers` table is the absolute value. The signed per-entry amounts are in `voucher_ledger_entries.amount` — use those directly.

---

## Voucher payload shape (the contract between backend and pusher)

```json
{
  "date": "2024-04-23",
  "voucher_type": "Payment",
  "voucher_number": "PV/001",
  "party_name": "HDFC Bank",
  "narration": "Supplier payment",
  "ledger_entries": [
    {"ledger_name": "Supplier A", "amount": -5000, "is_deemed_positive": false},
    {"ledger_name": "HDFC Bank",  "amount":  5000, "is_deemed_positive": true}
  ],
  "items": []
}
```

---

## Verification

1. Start TallyPrime, open a company.
2. Set `TALLY_URL=http://localhost:9000`, `TALLY_COMPANY=<name>`.
3. Run directly:
   ```python
   from tally_pusher import push_vouchers
   result = push_vouchers([{
     "date": "2024-04-23",
     "voucher_type": "Payment",
     "voucher_number": "TEST/001",
     "party_name": "Cash",
     "narration": "Test push",
     "ledger_entries": [
       {"ledger_name": "Cash",    "amount": -1000, "is_deemed_positive": False},
       {"ledger_name": "Capital", "amount":  1000, "is_deemed_positive": True},
     ],
     "items": []
   }])
   assert result["created"] == 1, result
   ```
4. Open TallyPrime Day Book — voucher should appear.
5. Test error case: unbalanced voucher (amounts don't sum to 0) — expect `result["errors"] > 0` and a line error message.
6. Backend: `POST /api/sync/push-queue` with a voucher payload, then `GET /api/sync/push-queue` from the connector side.
7. Set `TB_ENABLE_PUSH=1` on the connector, trigger a sync, confirm the voucher appears in Tally.

---

## Key sources

- [TallyHelp — Integration With TallyPrime](https://help.tallysolutions.com/integration-with-tallyprime/)
- [TallyHelp — XML Integration](https://help.tallysolutions.com/xml-integration/)
- [TallyHelp — Case Study 1: XML Request & Response Formats](https://help.tallysolutions.com/case-study-1/)
- [TallyHelp — Sample XML](https://help.tallysolutions.com/sample-xml/)
- [Postman — Tally XMLs for Integration](https://documenter.getpostman.com/view/13855108/TzeRpAMt)
- [GitHub — NoumaanAhamed/tally-prime-api-docs](https://github.com/NoumaanAhamed/tally-prime-api-docs/blob/main/index.md)
- [GitHub — anwinantino/invoice-to-tally](https://github.com/anwinantino/invoice-to-tally)
- [GitHub — Mitalee/Import_Wrapper_Tally_ERP](https://github.com/Mitalee/Import_Wrapper_Tally_ERP/blob/master/csv_to_tally_v05.py)
- [PyPI — tally-integration](https://pypi.org/project/tally-integration/)