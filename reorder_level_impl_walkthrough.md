# Reorder Level Implementation Walkthrough

> Created: 2026-04-09
> Branch: `codex/erp9-sync`
> Review: Codex pass applied 2026-04-09

---

## What We Are Building

A Telegram command `/reorder` that:
1. Triggers an n8n workflow
2. n8n calls the TallyBridge backend API
3. Backend queries Supabase and computes reorder triggers
4. Result is sent back to the user as formatted text in Telegram

---

## Metric Definition

This is a **90-day purchase-volume trigger**, not a classical reorder level.
Classical reorder level would include lead time and safety stock. This version does not.

```
window_from   = as_of_date - 89 days   (inclusive, so [window_from, as_of_date] = 90 days)
window_to     = as_of_date

reorder_trigger = SUM(quantity) of non-cancelled purchase line items in [window_from, window_to]
                  -- raw 90-day purchase total, no division, no averaging, no lead time

needs_reorder = closing_qty <= reorder_trigger
```

**Why 89 days, not 90:** The window is inclusive on both ends.
`2019-03-31 - 89 days = 2019-01-01`, giving exactly Jan 1 to Mar 31 = 90 days.
Subtracting 90 would give Dec 31 to Mar 31 = 91 days.

---

## Local Configuration (not universal)

The values below are environment-specific. Do not treat them as defaults in the code.

| Config item | Local value | How to set |
|-------------|-------------|-----------|
| n8n public URL | `https://profligately-frugal-laila.ngrok-free.dev` | ngrok tunnel |
| Telegram credential ID | `MzDsXbhbeli4cRfn` | n8n credentials panel |
| Company name (test) | `K.V. ENTERPRISES 18-19` | query param |
| Default as_of_date | `2019-03-31` | backend fallback for this company's FY |
| API key | value from `backend/.env` `API_KEY` | env var |

---

## Architecture

```
User sends /reorder in Telegram
  |
  v
Telegram sends webhook to ngrok public URL
  |
  v
n8n (localhost:5678) receives the message
  |
  v
IF node: does message start with /reorder?
  |-- no  --> stop
  |-- yes -->
        |
        v
  HTTP Request: GET http://localhost:3001/api/sync/reorder-levels
  (n8n and backend are both local, no tunnel needed between them)
        |
        v
  Backend queries Supabase:
    purchases --> voucher_items --> aggregate qty per item
    stock_items --> closing_qty per item
        |
        v
  JSON response returned to n8n
        |
        v
  Code node formats JSON into readable text
        |
        v
  Telegram Send Message node replies to user
```

---

## Database Schema (join path)

```
purchases
  company_id, date, is_cancelled, voucher_id

voucher_items
  voucher_id, stock_item_name, quantity, unit
  (joined locally in Node.js by voucher_id)

stock_items
  company_id, name, closing_qty, unit
  (joined locally in Node.js by name)
```

---

## Step 1 -- Backend Endpoint

**File:** `backend/src/routes/sync.ts`
**Insert location:** Before `export default router;` (last line of file)

### Endpoint

```
GET /api/sync/reorder-levels
Header: x-api-key: <API_KEY>
```

### Query Params

| Param | Default | Description |
|-------|---------|-------------|
| `company_name` | -- | Company name (or use `company_id` / `company_guid`) |
| `as_of_date` | `2019-03-31` | End of 90-day window (YYYY-MM-DD) -- local default for this FY |
| `limit` | `50` | Max items returned; `0` = all |

### Internal Logic (step by step)

1. `resolveCompanyLookup()` -- get `company_id` from name/guid/id
2. Parse `as_of_date` (YYYY-MM-DD), default `2019-03-31` if missing or invalid format
3. Compute window:
   ```ts
   const toDate = new Date(asOfDate);
   const fromDate = new Date(asOfDate);
   fromDate.setDate(toDate.getDate() - 89);   // -89 for inclusive 90-day window
   const fromIso = fromDate.toISOString().slice(0, 10);
   const toIso   = toDate.toISOString().slice(0, 10);
   // example: as_of_date=2019-03-31 -> fromIso=2019-01-01, toIso=2019-03-31
   ```
4. `fetchAllPages()` on `purchases` table:
   - Filters: `company_id`, `is_cancelled = false`, `date >= fromIso`, `date <= toIso`
   - Select: `voucher_id` only
5. Deduplicate voucher IDs with `new Set(...)`
6. `selectRowsByIn()` on `voucher_items` with `voucher_id IN (...)`:
   - Skip rows where `stock_item_name` is null or empty
   - Skip rows where `quantity <= 0` (returns/adjustments should not inflate the trigger)
   - Aggregate: `Map<stock_item_name, sum(quantity)>` in Node.js
7. `fetchAllPages()` on `stock_items`:
   - Filters: `company_id`
   - Select: `name, unit, closing_qty`
8. For each stock item:
   ```ts
   reorder_trigger = qtyByItem.get(name) ?? 0   // raw 90-day purchase total
   needs_reorder   = closing_qty <= reorder_trigger
   ```
9. Sort: `needs_reorder = true` first, then alphabetical by name
10. Apply `limit` (slice to N if limit > 0)

### Explicit Filters in Backend Code

| Filter | Reason |
|--------|--------|
| `is_cancelled = false` on purchases | Cancelled vouchers should not count toward purchase volume |
| Skip `stock_item_name` null/empty in voucher_items | Prevents aggregation into a blank-key bucket |
| Skip `quantity <= 0` in voucher_items | Credit notes / returns would reduce the trigger incorrectly |

### Reused Helpers (already in sync.ts -- do NOT recreate)

| Helper | Approx. line | Purpose |
|--------|-------------|---------|
| `resolveCompanyLookup()` | ~504 | Resolves company from query param |
| `fetchAllPages()` | ~1065 | Paginated SELECT (1000 rows/page) |
| `selectRowsByIn()` | ~277 | Batched IN query (250 IDs/batch) |

Note: line numbers are approximate and shift as the file grows. Search by function name.

### Response Shape

```json
{
  "as_of_date": "2019-03-31",
  "window_from": "2019-01-01",
  "window_to": "2019-03-31",
  "total_items": 1753,
  "needs_reorder_count": 45,
  "items": [
    {
      "stock_item_name": "C-10 DEBURING BLADE",
      "unit": "NOS",
      "total_qty_purchased": 13000,
      "reorder_trigger": 13000,
      "closing_qty": 5650,
      "needs_reorder": true
    },
    {
      "stock_item_name": "WHITE GLASS",
      "unit": "NOS",
      "total_qty_purchased": 5000,
      "reorder_trigger": 5000,
      "closing_qty": 0,
      "needs_reorder": true
    }
  ]
}
```

Note: `total_items` and `needs_reorder_count` reflect current Supabase data and will differ
from test-run numbers if new syncs have occurred.

---

## Step 2 -- n8n Workflow

**File:** `challan-to-invoice.json`
Currently has only 1 node (Telegram Trigger, inactive). Add 4 nodes and wire them up.

### Node Flow

```
[Node 1: Telegram Trigger]
        |
        v
[Node 2: IF -- starts with /reorder?]
        |
      true
        |
        v
[Node 3: HTTP Request -- call backend]
        |
        v
[Node 4: Code -- format text]
        |
        v
[Node 5: Telegram Send Message]
```

---

### Node 1 -- Telegram Trigger (already exists, no change needed)

- **ID:** `d07ac206-306a-49a7-acf5-d536bf6940f5`
- **Type:** `n8n-nodes-base.telegramTrigger`
- **Credential:** `MzDsXbhbeli4cRfn` (local -- your Telegram account in n8n)
- **Tracks:** `message` updates

---

### Node 2 -- IF (command router)

- **Type:** `n8n-nodes-base.if`
- **Name:** `IF: is /reorder`
- **Condition:** `{{ $json.message.text }}` starts with `/reorder`
- **True branch:** goes to Node 3
- **False branch:** no connection (workflow stops silently)

---

### Node 3 -- HTTP Request (call TallyBridge backend)

- **Type:** `n8n-nodes-base.httpRequest`
- **Name:** `GET reorder-levels`
- **Method:** GET
- **URL:** `http://localhost:3001/api/sync/reorder-levels`
- **Headers:** `x-api-key` set to the value from `backend/.env` `API_KEY`
- **Query Parameters:**
  - `company_name` = `K.V. ENTERPRISES 18-19` (local test value)
  - `limit` = `20`
  - `as_of_date` omitted (backend defaults to `2019-03-31`)
- **Response:** JSON (auto-parse on)

---

### Node 4 -- Code (format Telegram text)

- **Type:** `n8n-nodes-base.code`
- **Name:** `Format reorder text`
- **Language:** JavaScript

```js
const data = $input.first().json;
const items = data.items || [];

// Escape Telegram Markdown special chars in item names to prevent broken formatting
function escMd(str) {
  return String(str).replace(/[_*[\]()~`>#+\-=|{}.!]/g, '\\$&');
}

const reorderItems = items.filter(i => i.needs_reorder).slice(0, 20);

const lines = reorderItems
  .map((i, idx) =>
    `${idx + 1}\\. ${escMd(i.stock_item_name)}\n` +
    `   Stock: ${i.closing_qty} \\| Trigger: ${i.reorder_trigger} ${escMd(i.unit || '')}`
  )
  .join('\n\n');

const header =
  `*Reorder Report*\n` +
  `As of: ${data.as_of_date}\n` +
  `Window: ${data.window_from} to ${data.window_to}\n` +
  `${data.needs_reorder_count} of ${data.total_items} items need reorder\n\n`;

const body = lines || 'All items are sufficiently stocked.';

return [{ json: { text: header + body } }];
```

Note: Uses MarkdownV2 escaping. Set Parse Mode to `MarkdownV2` in Node 5.

---

### Node 5 -- Telegram Send Message

- **Type:** `n8n-nodes-base.telegram`
- **Name:** `Send reorder reply`
- **Operation:** Send Message
- **Chat ID:** `{{ $('Telegram Trigger').item.json.message.chat.id }}`
- **Text:** `{{ $json.text }}`
- **Parse Mode:** `MarkdownV2`
- **Credential:** `MzDsXbhbeli4cRfn` (same Telegram account)

---

## Step 3 -- Activate

After adding all nodes and wiring connections in n8n:

1. Verify ngrok is running: `ngrok http --url=profligately-frugal-laila.ngrok-free.dev 5678`
2. Verify backend is running: `npm run dev` inside `backend/`
3. Toggle the workflow active in n8n UI (or set `"active": true` in the JSON)

---

## Verification

### 1. Backend only (curl)

```bash
curl "http://localhost:3001/api/sync/reorder-levels?company_name=K.V.%20ENTERPRISES%2018-19&limit=5" \
  -H "x-api-key: YOUR_KEY_HERE"
```

Check:
- `needs_reorder_count` > 0
- `window_from` = `2019-01-01`, `window_to` = `2019-03-31`
- C-10 DEBURING BLADE appears with `reorder_trigger: 13000`, `closing_qty: 5650`
- WHITE GLASS appears with `closing_qty: 0`

### 2. Date shift test

```bash
curl "http://localhost:3001/api/sync/reorder-levels?company_name=K.V.%20ENTERPRISES%2018-19&as_of_date=2019-01-31&limit=5" \
  -H "x-api-key: YOUR_KEY_HERE"
```

`window_from` should be `2018-11-02` (89 days before 2019-01-31). Different items may appear.

### 3. No-limit test

```bash
curl "http://localhost:3001/api/sync/reorder-levels?company_name=K.V.%20ENTERPRISES%2018-19&limit=0" \
  -H "x-api-key: YOUR_KEY_HERE"
```

All stock items returned. Actual count depends on current synced data.

### 4. End-to-end Telegram test

1. Send `/reorder` in the Telegram chat
2. Expect a reply within ~5 seconds
3. Verify C-10 DEBURING BLADE and WHITE GLASS appear near the top
4. Verify item names with special characters do not break message formatting

---

## Known Limitations

- `closing_qty` in `stock_items` is the FY-end snapshot, not a live running balance.
  For this company (K.V. ENTERPRISES 18-19), as_of_date `2019-03-31` aligns with that snapshot.
  For other dates, closing_qty may not reflect mid-year stock accurately.
- The 90-day window is fixed. A future improvement could make it configurable via query param.
- The `reorder_trigger` metric does not account for lead time, safety stock, or seasonality.
  It answers: "Do I have enough stock to cover the last 90 days of purchases?"

---

## Files to Change

| File | Change |
|------|--------|
| `backend/src/routes/sync.ts` | Add `GET /api/sync/reorder-levels` before `export default router` |
| `challan-to-invoice.json` | Add 4 nodes + connections, set `active: true` |
| `reorder_level_impl_walkthrough.md` | This file |
