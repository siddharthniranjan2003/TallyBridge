# Plan: Push Purchase Voucher to TallyPrime

## Context

The existing codebase already handles **sale** vouchers end-to-end:
- `tally_pusher.py` builds and sends the XML to Tally
- `push-invoice.ts` is a convenience backend route that accepts simple inputs (party, items, date) and assembles the full voucher payload with correct `ledger_entries` and `is_deemed_positive` flags
- `/api/push-invoice` is registered in `index.ts`

The Python layer (`tally_pusher.py`) **already fully supports purchase vouchers** — `DEFAULT_ALLOWED_PUSH_TYPES` includes both `"Purchase"` and `"GST PURCHASE"`, `_voucher_kind()` routes to `"purchase"` when "PURCHASE" is in the type, and `_build_inventory_entry_xml()` already flips `ISDEEMEDPOSITIVE` to `"Yes"` for purchase. No Python changes are needed.

What is missing is the **convenience backend route** for purchase, analogous to `push-invoice.ts`.

## What changes

```
backend/src/routes/push-purchase.ts   [CREATE]
backend/src/index.ts                  [MODIFY — 2 lines]
```

No changes to Python, Electron, Supabase schema, or local push server.

## Data flow (unchanged)

```
Client
  │  POST /api/push-purchase  {party_name, items, …}
  ▼
backend/src/routes/push-purchase.ts
  │  buildVoucher() → full voucher object with ledger_entries
  │  POST http://127.0.0.1:3002/push-voucher  (forwarded)
  ▼
src/main/local-push-server.ts (Electron)
  │  pickCompanyName()  →  spawn Python worker
  ▼
src/python/sync_main.py  →  run_single_push_command()
  ▼
src/python/tally_pusher.py  →  push_vouchers()
  │  _build_import_envelope()  →  XML
  │  POST http://localhost:9000  (Tally)
  ▼
TallyPrime  →  response XML parsed → JSON back up the chain
```

## Polarity reference (purchase vs sale)

| Ledger entry | Sale (`push-invoice.ts`) | Purchase (`push-purchase.ts`) |
|---|---|---|
| Party (sundry debtor/creditor) | `is_deemed_positive: true` (Dr) | `is_deemed_positive: false` (Cr) |
| Sales/Purchase ledger | `is_deemed_positive: false` (Cr) | `is_deemed_positive: true` (Dr) |
| CGST / SGST component | `is_deemed_positive: false` (Cr output) | `is_deemed_positive: true` (Dr input) |

`_signed_tally_amount(amount, is_deemed_positive)` in `tally_pusher.py`:
- `true` → `-abs(amount)` (debit = negative in Tally)
- `false` → `+abs(amount)` (credit = positive in Tally)

`voucher_is_deemed_positive` on the `<VOUCHER>` tag = `party_entry["is_deemed_positive"]`:
- Sale → `"Yes"`, Purchase → `"No"` ✓ (Tally convention)

## Implementation detail

### `backend/src/routes/push-purchase.ts` (new file — mirror of push-invoice.ts)

```typescript
import { Router } from "express";
import { request as httpRequest } from "node:http";
import { requireApiKey } from "../middleware/auth.js";

const router = Router();
const GST_RATE = 0.09;

// resolveLocalPushUrl, todayTallyDate, round2, postJson  — identical helpers to push-invoice.ts

interface PurchaseItem {
  stock_item_name: string;
  quantity: number;
  rate: number;
  amount: number;
  unit?: string;
  godown_name?: string;
}

interface PushPurchasePayload {
  party_name: string;
  items: PurchaseItem[];
  date?: string;
  voucher_number?: string;
  reference?: string;
  narration?: string;
  company_name?: string;
}

function buildVoucher(payload: PushPurchasePayload) {
  const { party_name, items, company_name } = payload;
  const date = payload.date || todayTallyDate();
  const voucher_number = payload.voucher_number || `PUR-${Date.now()}`;
  const reference = payload.reference || voucher_number;
  const narration = payload.narration || "Purchase invoice";

  const subtotal = round2(items.reduce((sum, item) => sum + item.amount, 0));
  const cgst = round2(subtotal * GST_RATE);
  const sgst = round2(subtotal * GST_RATE);
  const total = round2(subtotal + cgst + sgst);

  return {
    date,
    voucher_type: "GST PURCHASE",
    voucher_number,
    party_name,
    narration,
    reference,
    inventory_ledger_name: "GST PURCHASE",
    company_name,
    ledger_entries: [
      { ledger_name: party_name,     amount: total,    is_deemed_positive: false },  // Cr
      { ledger_name: "GST PURCHASE", amount: subtotal, is_deemed_positive: true  },  // Dr
      { ledger_name: "CGST",         amount: cgst,     is_deemed_positive: true  },  // Dr input
      { ledger_name: "SGST",         amount: sgst,     is_deemed_positive: true  },  // Dr input
    ],
    items: items.map((item) => ({
      stock_item_name: item.stock_item_name,
      quantity:        item.quantity,
      unit:            item.unit || "NOS",
      rate:            item.rate,
      amount:          item.amount,
      godown_name:     item.godown_name || "Main Location",
    })),
  };
}

router.post("/", requireApiKey, async (req, res) => {
  const payload = req.body as PushPurchasePayload;
  if (!payload?.party_name || !Array.isArray(payload.items) || payload.items.length === 0) {
    return res.status(400).json({ ok: false, error: "Payload must include party_name and a non-empty items array" });
  }
  try {
    const voucher = buildVoucher(payload);
    const forwarded = await postJson(resolveLocalPushUrl(), voucher);
    // … same pattern as push-invoice.ts
  } catch (error) { … }
});

export default router;
```

### `backend/src/index.ts` (2-line change)

```typescript
// Add after existing pushInvoiceRouter import:
import pushPurchaseRouter from "./routes/push-purchase.js";

// Add after existing pushInvoiceRouter mount:
app.use("/api/push-purchase", pushPurchaseRouter);
```

## Verification

1. Start the backend: `cd backend && npm run dev`
2. POST to `/api/push-purchase` with a test payload (Tally must be open with the company):
   ```json
   {
     "party_name": "Test Supplier",
     "items": [{ "stock_item_name": "Widget", "quantity": 2, "rate": 100, "amount": 200 }]
   }
   ```
3. Expect `{"ok": true, "created": 1, ...}` back and the purchase voucher visible in Tally Day Book.
4. Alternatively, use the existing `/api/push-voucher` route with a full payload to verify the Python layer independently before testing the new convenience route.