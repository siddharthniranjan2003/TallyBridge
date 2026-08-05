-- Sale party matching: source candidates from the customer master, not from
-- invoice history.
--
-- 20260803_distinct_party_names.sql fixed how MUCH of the voucher party list the
-- matcher received (211 -> every distinct name). It did not fix WHICH list.
--
-- Candidates were built from vouchers.party_name -- transaction history -- so on
-- the client:
--
--   * 2,207 of 3,578 Sundry Debtors (61.7%) were not candidates at all. A customer
--     who has not been invoiced in the synced window could never match, however
--     clean the read. That is not a scoring failure; they were never on the list.
--   * 101 Sundry Creditors WERE candidates, because `vouchers` also holds Purchase
--     (93 supplier parties) and Payment (82) rows. A sale scan could be filed
--     against a supplier.
--   * `Cash`, `BHARTI AIRTEL` and 21 other non-party ledgers were candidates too.
--
-- Filtering by voucher_type is the wrong lever: it still leaves 22 suppliers and
-- LOSES 53 real customers who appear only on Receipt or Credit Note vouchers.
-- Voucher type is a proxy; ledgers.group_name is the answer, and the Flutter app
-- has been using it all along (lib/data/customers_cache.dart selects ledgers where
-- group_name = 'Sundry Debtors' and pages with range()). This puts the OCR on the
-- same list as the app instead of leaving the two on different universes.
--
--                        client            testing
--   before               1,495             1,368
--   after                3,578  (+2,207)   2,346
--
-- SURGICAL BY DESIGN: same function name, same RETURNS TABLE(party_name text), so
-- no application change is needed. The paging added in 8720c6f already handles a
-- result set past PostgREST's 1000-row cap, which 3,578 rows very much is.
--
-- SUPERSEDES 20260803_distinct_party_names.sql. CREATE OR REPLACE works whether
-- the function already exists (testing) or not (client), so this one file is the
-- only thing that needs applying to either project, and both land in the same
-- state. Do not apply 20260803 to the client.
--
-- FAILS SAFE: if `ledgers` were empty or the group name differed, this returns
-- zero rows; fetch_distinct_party_names then returns None (not []) and the caller
-- falls back to _party_candidates_by_paging. A wrong migration degrades to the old
-- behaviour rather than wiping the candidate list.
--
-- KNOWN AND ACCEPTED: 29 parties with real GST SALE vouchers are not Sundry
-- Debtors -- 22 Sundry Creditors (171 sale invoices between them), plus Traders,
-- Manufacturer_* and Cash-in-hand -- so they leave this list. They stay reachable
-- through fetch_targeted_party_rows, which still ILIKEs `vouchers` for terms taken
-- from the read. Leaving that path on vouchers is deliberate: it is the safety net
-- for exactly these 29.
--
-- Idempotent: safe to run on any client/testing project, and safe to re-run.

-- 2026-08-05: widened from 'Sundry Debtors' to include 'Sundry Creditors'.
-- Evidence from the first five real client scans: A.V. UNIPACK PRIVATE LIMITED is
-- filed under Sundry Creditors and was invoiced anyway (34 GST SALE vouchers). It
-- only matched because fetch_targeted_party_rows still ILIKEs `vouchers`. 22 such
-- suppliers carry 171 sale invoices between them, so they belong in the list
-- rather than depending on the rescue. Client total: 3,578 -> 4,193.
--
-- STILL NOT COVERED, deliberately: 7 parties with real GST SALE vouchers sit under
-- neither group -- MOHIT SALES AGENCIES, M S ENTERPRISES and R.R.TOOLS &
-- EQUIPMENTS under `Traders`, CP GRAT-EX and WIKUS INDIA under
-- `Manufacturer_ Micro/ Small`, EMKAY TOOLS under `Manufacturer_ Medicum/ Large`,
-- and `Cash`. MOHIT SALES AGENCIES was scanned on 2026-08-04 and matched through
-- the ILIKE rescue, which is what keeps these reachable. To cover them in the main
-- list instead, add:
--
--     UNION ALL
--     SELECT v.party_name FROM vouchers v WHERE v.voucher_type = 'GST SALE'

CREATE OR REPLACE FUNCTION public.get_distinct_party_names()
 RETURNS TABLE(party_name text)
 LANGUAGE sql
 STABLE
AS $function$
  SELECT DISTINCT btrim(l.name)
  FROM ledgers l
  WHERE l.group_name IN ('Sundry Debtors', 'Sundry Creditors')
    AND l.name IS NOT NULL
    AND btrim(l.name) <> ''
  ORDER BY 1;
$function$;
