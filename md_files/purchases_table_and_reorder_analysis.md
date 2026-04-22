# Purchases Table & Reorder Level Analysis

---

## Part 1: How the `purchases` Table is Formed

### Source: Tally Vouchers

When Tally syncs, it sends a large JSON payload to `POST /api/sync`. Inside that payload is a `vouchers` array — every voucher from Tally (Purchase, Sales, Journal, Payment, everything).

### Filter: Only Purchase-Type Vouchers

The code filters that vouchers array down to only ones where `voucher_type` matches:

```
"purchase"
"local purchase"
"import purchase"
...or any custom type containing "purchase" (but not "purchase order")
```

These become `purchaseVouchers`.

### What Gets Written into the `purchases` Table

Each purchase voucher maps to one row:

| Column | Where it comes from |
|--------|-------------------|
| `company_id` | Auto-attached from the sync session |
| `voucher_id` | Looked up from `vouchers` table after insert (Supabase UUID) |
| `tally_guid` | Tally's own unique ID for the voucher |
| `voucher_number` | e.g. "PV-001" — from Tally |
| `voucher_type` | "Purchase" / "Local Purchase" — from Tally |
| `date` | Voucher date from Tally |
| `party_name` | Supplier name from Tally |
| `amount` | Total invoice amount from Tally |
| `narration` | Notes/remarks from Tally |
| `reference` | Reference number from Tally |
| `is_cancelled` | Whether it was cancelled in Tally |
| `is_invoice` | Whether it's a tax invoice |
| `synced_at` | Timestamp of this sync run |

### The Key Link: `voucher_id`

The `purchases` table doesn't stand alone. It points to the `vouchers` table via `voucher_id`. The voucher holds the full header. The `voucher_items` table holds the line items (which items were bought, what qty). So:

```
purchases row  →  vouchers row  →  voucher_items rows
(one purchase)    (same voucher)    (each item on the invoice)
```

### In Short

`purchases` is not raw Tally data. It is a filtered, flattened copy of Tally vouchers — only the purchase-type ones — stored in Supabase for fast querying without needing to scan all voucher types every time.

---

## Part 2: Is Fetching from `purchases` Viable for Reorder Level?

### Standard Reorder Level Formula (Indian Accounting / Global)

The textbook formula is:

```
Reorder Level = (Average Daily Usage × Lead Time in days) + Safety Stock
```

**We deliberately skipped all of this.** No averaging, no lead time, no safety stock. Our formula is:

```
reorder_trigger = total qty purchased in last 90 days (raw sum)
needs_reorder   = closing_qty <= reorder_trigger  (AND reorder_trigger > 0)
```

This is a simplified operational trigger — not a classical reorder level. It answers: *"did we buy more in the last 3 months than we currently have in stock?"* That is a business judgement call, not an accounting standard. It works for K.V. ENTERPRISES because the goal is a quick Telegram alert, not a formal procurement system.

---

### Is Fetching from `purchases` the Right Call?

**Yes — and here's why according to Tally's own definitions:**

In Tally (ERP9 and TallyPrime), a **Purchase Voucher (F9)** is what records the actual inward movement of stock — goods physically received and invoiced. This is the only voucher type that:

- Updates stock item quantities (increases closing stock)
- Records the supplier (party name)
- Is GST-compliant inward supply

What we correctly excluded from our `purchases` table:

| Voucher Type | Included? | Why |
|---|---|---|
| Purchase Voucher (F9) | Yes | Actual stock receipt |
| Local Purchase | Yes | Same as above, local GST |
| Import Purchase | Yes | Same as above, import |
| Purchase Order | No | Just a booking, stock hasn't moved |
| Receipt Note | No | Interim receipt, not final |
| Journal / Payment | No | Expense, not stock inward |

The code does exactly this — it filters by `isPurchaseVoucherType()` which explicitly blocks anything containing "order", so Purchase Orders never enter the `purchases` table.

---

### One Gap Worth Knowing

Our formula uses purchase qty as the reorder trigger. But Tally's Purchase Voucher can also include:

- **Fixed assets** (machinery, equipment) — these inflate qty but aren't consumable stock
- **Services with GST** — no stock item, qty = 0, so harmless
- **Expense items** (stationery, freight) recorded in purchase mode

For K.V. ENTERPRISES (industrial hardware dealer), this is unlikely to cause noise because their purchases are almost entirely stock items. But if someone books a fixed asset through a purchase voucher, that qty would add to the reorder trigger for that item name — potentially a false flag.

---

### Bottom Line

Our approach is pragmatically correct for this business. The `purchases` table contains exactly what Tally defines as actual stock inward transactions, and we sum the right column (`quantity` from `voucher_items`) for the right voucher types. The formula is simplified intentionally — it is an operational alert, not textbook inventory management.

---

## Sources

- [Reorder Level Formula — AccountingTools](https://www.accountingtools.com/articles/reorder-level-formula)
- [Reorder Level in Inventory Management — aajenterprises.com](https://www.aajenterprises.com/reorder-level/)
- [Purchase Order vs Purchase Voucher in TallyPrime — TallySchool](https://tallyschool.com/purchase-order-vs-purchase-voucher-in-tally/)
- [Record Purchases under GST — TallyHelp](https://help.tallysolutions.com/article/Tally.ERP9/Tax_India/gst/recording_purchases_gst.htm)
- [Inventory Vouchers — TallyHelp](https://help.tallysolutions.com/article/Tally.ERP9/Voucher_Entry/Inventory_Vouchers/Inventory_Vouchers.htm)
