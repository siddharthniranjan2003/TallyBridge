# Phase 1: ERP 9 Full-Sync Scope Matrix

Date: 2026-04-08  
Project: TallyBridge (KV Enterprises target: Tally.ERP 9 Release 6.6.3 + latest TallyPrime compatibility)

## Goal

Define exactly what "sync all data" means for this project, with an implementation-ready mapping:

- source transport (`xml` / `odbc` / `hybrid`)
- destination table(s)
- sync behavior (`full snapshot` / `incremental`)
- status (`implemented` / `missing`)

---

## A. Already Implemented (Current Baseline)

| Domain | Source | Destination | Sync Mode | Status |
|---|---|---|---|---|
| Company Info | XML | `companies` | Upsert | Implemented |
| Change Markers | XML | `companies` (`alter_id`, `alt_vch_id`, `alt_mst_id`) | Incremental trigger | Implemented |
| Groups | ODBC-first + XML fallback | `groups` | Upsert | Implemented |
| Ledgers | ODBC-first + XML fallback | `ledgers` | Upsert | Implemented |
| Vouchers | XML (ERP 9 Day Book batching) | `vouchers` | Incremental/full by date window | Implemented |
| Voucher Inventory Lines | XML | `voucher_items` | Replace per voucher | Implemented |
| Voucher Ledger Entries | XML | `voucher_ledger_entries` | Replace per voucher | Implemented |
| Purchases (derived from vouchers) | Derived | `purchases` | Reconciled | Implemented |
| Stock Items | ODBC-first + XML fallback | `stock_items` | Upsert | Implemented |
| Outstanding (Receivable/Payable) | XML | `outstanding` | Snapshot by `synced_at` | Implemented |
| Profit & Loss | XML | `profit_loss` | Snapshot by `synced_at` | Implemented |
| Balance Sheet | XML | `balance_sheet` | Snapshot by `synced_at` | Implemented |
| Trial Balance | XML | `trial_balance` | Snapshot by `synced_at` | Implemented |
| Sync Audit Metadata | Sync pipeline | `sync_log` | Append | Implemented |

---

## B. Missing for "Full ERP 9 Data" Target

| Domain | Preferred Source | Destination | Sync Mode | Status |
|---|---|---|---|---|
| Voucher Types / Numbering metadata | XML | New table (`voucher_types`) | Upsert | Missing |
| Stock Groups | ODBC-first + XML fallback | New table (`stock_groups`) | Upsert | Missing |
| Stock Categories | ODBC-first + XML fallback | New table (`stock_categories`) | Upsert | Missing |
| Units of Measure | ODBC-first + XML fallback | New table (`units`) | Upsert | Missing |
| Godowns / Locations | ODBC-first + XML fallback | New table (`godowns`) | Upsert | Missing |
| Batch / Lot level stock | XML (report/detail) | New table (`stock_batches`) | Snapshot/incremental | Missing |
| Cost Categories | XML | New table (`cost_categories`) | Upsert | Missing |
| Cost Centres | XML | New table (`cost_centres`) | Upsert | Missing |
| Purchase Orders | XML | New table (`purchase_orders`) | Incremental | Missing |
| Sales Orders | XML | New table (`sales_orders`) | Incremental | Missing |
| Delivery/Receipt Notes | XML | New table(s) | Incremental | Missing |
| GST/statutory register extracts (project-specific) | XML reports | New table(s) | Snapshot | Missing |

Notes:

- The above are the practical missing domains for a real "full ERP 9 accounting + inventory" sync.
- Payroll/HR is intentionally excluded unless KV explicitly asks for it.

---

## C. Phase 1 Deliverables (Scope Lock)

1. Freeze required domains with KV Enterprises (must-have vs optional).
2. Freeze date policy for historical backfill (one FY, multi-FY, or all years).
3. Freeze identity keys per domain:
   - company key: `guid`
   - voucher key: `tally_guid`
   - master keys: `master_id` + natural keys where needed
4. Freeze section-level source policy:
   - ODBC-first for heavy master reads when stable
   - XML authoritative fallback for all sections
5. Freeze acceptance checks for "full sync complete":
   - row counts per domain
   - oldest/newest voucher date per company
   - totals reconciliation for key reports

---

## D. Phase 1 Acceptance Criteria

Phase 1 is complete when:

- there is an agreed required-domain list (signed off for KV use case),
- each required domain has a source + destination + sync mode mapping,
- and we can start implementation without further architecture ambiguity.

---

## E. Immediate Next Step After Phase 1

Implement backfill controls in sync runtime:

- explicit `from_date` and `to_date` overrides,
- resumable chunk checkpoints for large voucher history,
- and section-level completion markers in sync metadata.
