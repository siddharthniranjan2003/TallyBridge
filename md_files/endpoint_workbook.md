# TallyBridge Endpoint Workbook

> Reference: all GET/POST endpoints in `backend/src/routes/sync.ts`
> Base URL: `http://localhost:3001`
> All endpoints require header: `x-api-key: <your API_KEY from backend/.env>`

---

## How Endpoints Are Structured

Every endpoint in this backend follows the same skeleton:

```ts
router.get("/route-name", requireApiKey, async (req, res) => {
  try {
    // 1. Resolve company
    // 2. Parse query params
    // 3. Query Supabase
    // 4. Process in Node.js
    // 5. Return JSON
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});
```

All routes are mounted at `/api/sync` in `backend/src/index.ts`, so the full path is always:
```
/api/sync/<route-name>
```

---

## Authentication

Every endpoint is protected by `requireApiKey` middleware (`backend/src/middleware/auth.ts`).

You must send this header with every request:
```
x-api-key: sb_publishable_oFyc0kQxjkga2QZw0kq3ZQ_ovmR0P9h
```

Without it: `401 Unauthorized`
Wrong key: `401 Unauthorized`

---

## Company Resolution

Most endpoints use `resolveCompanyLookup()` to find which company to query.
You pass one of three identifying params:

| Param | Example | Type |
|-------|---------|------|
| `company_id` | `abf1cdd2-e919-4cea-a902-8cab3e71c9fc` | UUID from Supabase |
| `company_guid` | `some-tally-guid` | Tally internal GUID |
| `company_name` | `K.V. ENTERPRISES 18-19` | Exact name match |

If none is passed: `400 Bad Request`
If company not found: `404 Not Found`
If company exists but has never synced: `409 Conflict`

**Exception:** `/reorder-levels` auto-detects the single company. No param needed.

---

## All Endpoints

---

### POST /api/sync
**Purpose:** Receives synced data from the Python worker (Tally -> Supabase push)
**Called by:** `cloud_pusher.py`, not by n8n or frontend
**Body:** Large JSON payload with vouchers, ledgers, stock, etc.
**Response:** `{ success: true, records_synced: { ... } }`

Not a query endpoint. Skip this for reporting/read use cases.

---

### GET /api/sync/vouchers

**Purpose:** All vouchers for a company with their line items

**Params:**
| Param | Required | Default | Description |
|-------|----------|---------|-------------|
| `company_name` / `company_id` / `company_guid` | Yes | -- | Company identifier |

**Query:**
```
SELECT *, voucher_items(*) FROM vouchers
WHERE company_id = ?
ORDER BY date DESC
```

**Response:**
```json
{
  "vouchers": [
    {
      "id": "uuid",
      "tally_guid": "...",
      "voucher_number": "PV-001",
      "voucher_type": "Purchase",
      "date": "2019-03-15",
      "party_name": "ABC Traders",
      "amount": 15000,
      "is_cancelled": false,
      "voucher_items": [
        { "stock_item_name": "CEMENT", "quantity": 100, "unit": "BAG", "rate": 150, "amount": 15000 }
      ]
    }
  ]
}
```

**Curl:**
```bash
curl "http://localhost:3001/api/sync/vouchers?company_name=K.V. ENTERPRISES 18-19" \
  -H "x-api-key: sb_publishable_oFyc0kQxjkga2QZw0kq3ZQ_ovmR0P9h"
```

---

### GET /api/sync/purchases

**Purpose:** All purchase vouchers for a company

**Params:**
| Param | Required | Default | Description |
|-------|----------|---------|-------------|
| `company_name` / `company_id` / `company_guid` | Yes | -- | Company identifier |

**Query:**
```
SELECT * FROM purchases
WHERE company_id = ?
ORDER BY date DESC
```

**Response:**
```json
{
  "purchases": [
    {
      "id": "uuid",
      "tally_guid": "...",
      "voucher_number": "PV-001",
      "date": "2019-03-15",
      "party_name": "ABC Traders",
      "amount": 15000,
      "is_cancelled": false
    }
  ]
}
```

**Curl:**
```bash
curl "http://localhost:3001/api/sync/purchases?company_name=K.V. ENTERPRISES 18-19" \
  -H "x-api-key: sb_publishable_oFyc0kQxjkga2QZw0kq3ZQ_ovmR0P9h"
```

---

### GET /api/sync/stock

**Purpose:** All stock items with closing qty and value

**Params:**
| Param | Required | Default | Description |
|-------|----------|---------|-------------|
| `company_name` / `company_id` / `company_guid` | Yes | -- | Company identifier |

**Query:**
```
SELECT * FROM stock_items
WHERE company_id = ?
```

**Response:**
```json
{
  "stock_items": [
    {
      "id": "uuid",
      "name": "C-10 DEBURING BLADE",
      "group_name": "Consumables",
      "unit": "NOS",
      "closing_qty": 5650,
      "closing_value": 28250,
      "rate": 5
    }
  ]
}
```

**Curl:**
```bash
curl "http://localhost:3001/api/sync/stock?company_name=K.V. ENTERPRISES 18-19" \
  -H "x-api-key: sb_publishable_oFyc0kQxjkga2QZw0kq3ZQ_ovmR0P9h"
```

---

### GET /api/sync/outstanding

**Purpose:** Receivables and payables — who owes what and how overdue

**Params:**
| Param | Required | Default | Description |
|-------|----------|---------|-------------|
| `company_name` / `company_id` / `company_guid` | Yes | -- | Company identifier |

**Note:** Returns empty array if outstanding has never been synced for this company.

**Query:**
```
SELECT * FROM outstanding
WHERE company_id = ?
AND synced_at = <latest sync timestamp>
ORDER BY days_overdue DESC
```

**Response:**
```json
{
  "outstanding": [
    {
      "party_name": "ABC Traders",
      "type": "receivable",
      "voucher_number": "INV-101",
      "voucher_date": "2019-01-15",
      "due_date": "2019-02-15",
      "original_amount": 50000,
      "pending_amount": 30000,
      "days_overdue": 45
    }
  ]
}
```

**Curl:**
```bash
curl "http://localhost:3001/api/sync/outstanding?company_name=K.V. ENTERPRISES 18-19" \
  -H "x-api-key: sb_publishable_oFyc0kQxjkga2QZw0kq3ZQ_ovmR0P9h"
```

---

### GET /api/sync/pnl

**Purpose:** Profit and Loss statement (snapshot from last sync)

**Params:**
| Param | Required | Default | Description |
|-------|----------|---------|-------------|
| `company_name` / `company_id` / `company_guid` | Yes | -- | Company identifier |

**Note:** Returns empty array if P&L has never been synced.

**Response:**
```json
{
  "profit_loss": [
    {
      "particulars": "Sales",
      "amount": 5000000,
      "is_debit": false
    },
    {
      "particulars": "Purchases",
      "amount": 3800000,
      "is_debit": true
    }
  ]
}
```

**Curl:**
```bash
curl "http://localhost:3001/api/sync/pnl?company_name=K.V. ENTERPRISES 18-19" \
  -H "x-api-key: sb_publishable_oFyc0kQxjkga2QZw0kq3ZQ_ovmR0P9h"
```

---

### GET /api/sync/balance-sheet

**Purpose:** Balance sheet snapshot from last sync

**Params:**
| Param | Required | Default | Description |
|-------|----------|---------|-------------|
| `company_name` / `company_id` / `company_guid` | Yes | -- | Company identifier |

**Response:**
```json
{
  "balance_sheet": [
    {
      "particulars": "Capital Account",
      "amount": 1200000,
      "side": "liability"
    },
    {
      "particulars": "Fixed Assets",
      "amount": 850000,
      "side": "asset"
    }
  ]
}
```

**Curl:**
```bash
curl "http://localhost:3001/api/sync/balance-sheet?company_name=K.V. ENTERPRISES 18-19" \
  -H "x-api-key: sb_publishable_oFyc0kQxjkga2QZw0kq3ZQ_ovmR0P9h"
```

---

### GET /api/sync/parties

**Purpose:** All customers and suppliers with outstanding totals

**Params:**
| Param | Required | Default | Description |
|-------|----------|---------|-------------|
| `company_name` / `company_id` / `company_guid` | Yes | -- | Company identifier |

**Query:** Pulls from `ledgers` (Sundry Debtors + Sundry Creditors groups) and joins outstanding totals per party.

**Response:**
```json
{
  "parties": [
    {
      "name": "ABC Traders",
      "group": "Sundry Creditors",
      "type": "supplier",
      "closing_balance": -45000,
      "total_outstanding": 45000
    }
  ],
  "total": 120
}
```

**Curl:**
```bash
curl "http://localhost:3001/api/sync/parties?company_name=K.V. ENTERPRISES 18-19" \
  -H "x-api-key: sb_publishable_oFyc0kQxjkga2QZw0kq3ZQ_ovmR0P9h"
```

---

### GET /api/sync/party-ledger

**Purpose:** Full transaction history for one specific party

**Params:**
| Param | Required | Default | Description |
|-------|----------|---------|-------------|
| `company_name` / `company_id` / `company_guid` | Yes | -- | Company identifier |
| `party_name` | Yes | -- | Exact party name |

**Curl:**
```bash
curl "http://localhost:3001/api/sync/party-ledger?company_name=K.V. ENTERPRISES 18-19&party_name=ABC Traders" \
  -H "x-api-key: sb_publishable_oFyc0kQxjkga2QZw0kq3ZQ_ovmR0P9h"
```

---

### GET /api/sync/alter-ids

**Purpose:** Returns Tally alter IDs used for incremental sync detection
**Called by:** Python worker before deciding full vs incremental sync
**Note:** Does not require a successful sync (unlike other endpoints)

**Curl:**
```bash
curl "http://localhost:3001/api/sync/alter-ids?company_name=K.V. ENTERPRISES 18-19" \
  -H "x-api-key: sb_publishable_oFyc0kQxjkga2QZw0kq3ZQ_ovmR0P9h"
```

---

### GET /api/sync/reorder-levels

**Purpose:** 90-day purchase-volume reorder trigger for all stock items.
Auto-detects the single company. No company param needed.

**Formula:**
```
window = [as_of_date - 89 days,  as_of_date]   (inclusive 90 days)
reorder_trigger = SUM(purchase qty in window)    (raw total, no averaging)
needs_reorder   = reorder_trigger > 0 AND closing_qty <= reorder_trigger
```

**Params:**
| Param | Required | Default | Description |
|-------|----------|---------|-------------|
| `as_of_date` | No | `2019-03-31` | End of 90-day window (YYYY-MM-DD) |

**Sorting:** needs_reorder=true first, then alphabetical.

**Response:**
```json
{
  "as_of_date": "2019-03-31",
  "window_from": "2019-01-01",
  "window_to": "2019-03-31",
  "total_items": 6891,
  "needs_reorder_count": 1320,
  "items": [
    {
      "stock_item_name": "C-10 DEBURING BLADE",
      "unit": "NOS",
      "total_qty_purchased": 13000,
      "reorder_trigger": 13000,
      "closing_qty": 5650,
      "needs_reorder": true
    }
  ]
}
```

**Curl variants:**
```bash
# Default (window: Jan 1 - Mar 31 2019)
curl "http://localhost:3001/api/sync/reorder-levels" \
  -H "x-api-key: sb_publishable_oFyc0kQxjkga2QZw0kq3ZQ_ovmR0P9h"

# Custom end date
curl "http://localhost:3001/api/sync/reorder-levels?as_of_date=2019-01-31" \
  -H "x-api-key: sb_publishable_oFyc0kQxjkga2QZw0kq3ZQ_ovmR0P9h"
```

---

## Quick Reference Table

| Endpoint | Method | Company Param | Other Params | Returns |
|----------|--------|---------------|--------------|---------|
| `/api/sync/` | POST | in body | -- | Sync result |
| `/api/sync/vouchers` | GET | Yes | -- | All vouchers + line items |
| `/api/sync/purchases` | GET | Yes | -- | All purchase vouchers |
| `/api/sync/stock` | GET | Yes | -- | All stock items + qty |
| `/api/sync/outstanding` | GET | Yes | -- | Receivables + payables |
| `/api/sync/pnl` | GET | Yes | -- | P&L snapshot |
| `/api/sync/balance-sheet` | GET | Yes | -- | Balance sheet snapshot |
| `/api/sync/parties` | GET | Yes | -- | Customers + suppliers |
| `/api/sync/party-ledger` | GET | Yes | `party_name` | One party transaction history |
| `/api/sync/alter-ids` | GET | Yes | -- | Tally sync IDs |
| `/api/sync/reorder-levels` | GET | Auto | `as_of_date` | Reorder trigger per item |

---

## How to Add a New Endpoint

1. Open `backend/src/routes/sync.ts`
2. Before `export default router;` add:

```ts
router.get("/your-route", requireApiKey, async (req, res) => {
  try {
    // If you need company: use resolveCompanyLookup()
    const companyLookup = await resolveCompanyLookup({
      companyId: req.query.company_id,
      companyGuid: req.query.company_guid,
      companyName: req.query.company_name,
    });
    if (companyLookup.status !== 200) {
      return res.status(companyLookup.status).json({ error: companyLookup.error });
    }

    // For paginated queries: use fetchAllPages()
    const data = await fetchAllPages("Label", (from, to) =>
      supabase.from("your_table")
        .select("col1, col2")
        .eq("company_id", companyLookup.companyId)
        .order("id", { ascending: true })
        .range(from, to)
    );

    // For batched IN queries: use selectRowsByIn()
    const related = await selectRowsByIn("other_table", "foreign_key_col", ids, "Label");

    res.json({ your_data: data });
  } catch (err: any) {
    console.error("[YourRoute] Error:", err.message);
    res.status(500).json({ error: err.message });
  }
});
```

3. Restart the backend: `npm run dev` inside `backend/`
4. Test: `curl "http://localhost:3001/api/sync/your-route" -H "x-api-key: ..."`

---

## Helper Functions (reuse, do not recreate)

| Function | Purpose | When to use |
|----------|---------|-------------|
| `resolveCompanyLookup()` | Find company by id/guid/name | Any endpoint that needs company_id |
| `fetchAllPages()` | Paginated SELECT (1000 rows/page) | Any table that could exceed 1000 rows |
| `selectRowsByIn()` | Batched IN query (250 IDs/batch) | Querying child rows by parent IDs |
| `upsertInBatches()` | Batched upsert | Writing data in bulk |
| `chunkArray()` | Split array into chunks | Manual batching |

---

## Common Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `401 Unauthorized` | Missing or wrong `x-api-key` header | Check header name and value |
| `400 Bad Request` | No company param passed | Add `company_name`, `company_id`, or `company_guid` |
| `404 Not Found` | Company name doesn't match exactly | Check spelling, case sensitive |
| `409 Conflict` | Company exists but never synced | Run a sync first |
| `500 Internal Server Error` | Supabase query failed | Check `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` in `.env` |
| Connection refused | Backend not running | `cd backend && npm run dev` |
