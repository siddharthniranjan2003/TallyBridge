# Backend Implementation Plan

## Context

`backend/src/index.ts` imports two route files that do not yet exist:
- `./routes/push-voucher.js` → mounted at `/api/push-voucher`
- `./routes/push-invoice.js` → mounted at `/api/push-invoice`

The compiled `index.js` already contains the `push-voucher` import, meaning the server is broken at startup right now. These are "PUSH PHASE 2" endpoints: standalone, purpose-specific REST paths for the outbound Tally voucher queue, separate from the already-working `/api/sync/push-queue*` routes.

The push queue infrastructure already exists:
- DB table: `push_queue` (in `full_schema.sql`)
- Shared DB client: `backend/src/db/supabase.ts`
- Auth middleware: `backend/src/middleware/auth.ts` (`requireApiKey`)
- Core logic pattern: `POST /api/sync/push-queue`, `GET /api/sync/push-queue`, `POST /api/sync/push-results` inside `backend/src/routes/sync.ts`

The new routes need to reuse the same DB table and the same validation/resolution functions — copied inline (not extracted to a shared module, per YAGNI principle).

---

## Files to Create

### 1. `backend/src/routes/push-voucher.ts`

Mounted at `/api/push-voucher`. Handles **general vouchers** (Sales, Purchase, Receipt, Payment, Debit Note, Credit Note). The `items` array is optional.

**Endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| `POST /` | `/api/push-voucher` | Validate & enqueue a voucher to `push_queue` |
| `GET /` | `/api/push-voucher` | List `pending` jobs for a company |
| `POST /results` | `/api/push-voucher/results` | Mark jobs as `pushed` or `failed` |

**Allowed voucher types** (case-insensitive, configurable via `TB_PUSH_VOUCHER_ALLOWED_TYPES` env var):
```
Sales, Purchase, Receipt, Payment, Debit Note, Credit Note,
GST SALE, GST PURCHASE
```

Validation mirrors `normalizePushVoucherPayload` from `sync.ts`:
- `company_id` / `company_guid` / `company_name` → resolved via `resolveCompanyLookup` pattern
- `voucher_payload.voucher_type` → must be in allowed set
- `voucher_payload.date` → ISO-like or compact date
- `voucher_payload.party_name` → required
- `voucher_payload.ledger_entries` → non-empty array, each entry needs `ledger_name`, `amount`, `is_deemed_positive`
- `voucher_payload.items` → optional array; if present, each item needs `stock_item_name`, `quantity`, `unit`, `rate`, `amount`

---

### 2. `backend/src/routes/push-invoice.ts`

Mounted at `/api/push-invoice`. Handles **invoice-type vouchers** (GST SALE, GST PURCHASE). The `items` array is **required** and must be non-empty.

**Endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| `POST /` | `/api/push-invoice` | Validate & enqueue an invoice to `push_queue` |
| `GET /` | `/api/push-invoice` | List `pending` invoice jobs for a company |
| `POST /results` | `/api/push-invoice/results` | Mark jobs as `pushed` or `failed` |

**Allowed voucher types** (case-insensitive, configurable via `TB_PUSH_INVOICE_ALLOWED_TYPES` env var):
```
GST SALE, GST PURCHASE
```

Validation is identical to `push-voucher.ts` except:
- `voucher_payload.items` → **required**, non-empty array (not optional)

---

## Implementation Notes

Both files share the same internal helper pattern. Each file is self-contained (no new shared module). Key helpers to inline in each:

- `normalizeTrimmedString(value)` — string coercion + trim
- `normalizeIsoLikeDate(value)` — YYYY-MM-DD or YYYYMMDD
- `normalizeFiniteNumber(value)` — numeric coercion
- `normalizeBoolean(value)` — bool coercion
- `resolveCompanyLookup({ companyId, companyGuid, companyName })` — queries Supabase `companies` table by id/guid/name, returns `{ status, companyId, companyName, companyGuid }` or error
- `withSupabaseSchemaGuidance(message)` — appends schema hint on Supabase schema errors

The `results` endpoint is identical between both files: loop over `[{id, status, error_message?, tally_response?}]`, update `push_queue` rows.

---

## Flow Diagram

```
POST /api/push-voucher   POST /api/push-invoice
         │                        │
   [requireApiKey]          [requireApiKey]
         │                        │
   resolveCompany           resolveCompany
    (id/guid/name)           (id/guid/name)
         │                        │
  validateVoucher          validateInvoice
  (items optional)         (items required)
         │                        │
         └─────────┬──────────────┘
                   │
        supabase.from("push_queue")
             .insert({ company_id,
                       voucher_payload,
                       status: "pending" })
                   │
         return { success: true, job: { id, status, created_at } }
```

---

## Verification

1. Run `npm run dev` in `backend/` — server must start without module-not-found errors
2. `POST /api/push-voucher` with a valid payload → `{ success: true, job: { id, status: "pending", created_at } }`
3. `GET /api/push-voucher?company_name=X` → `{ jobs: [...] }`
4. `POST /api/push-voucher/results` with `[{id, status: "pushed"}]` → `{ success: true, updated: 1 }`
5. Same three checks for `/api/push-invoice`, noting that a payload without `items` → 400 error
6. `GET /health` → `{ status: "ok" }` (regression check)