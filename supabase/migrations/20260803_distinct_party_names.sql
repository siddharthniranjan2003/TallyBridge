-- Party matching: hand the matcher every customer, in one query.
--
-- fetch_supabase_party_candidates built its candidate list by paging the
-- vouchers table -- 6 pages x 1000 rows, ordered by party_name. But vouchers
-- holds one row per INVOICE, not per customer, so 6000 rows is 6000 invoices
-- and the busiest early-alphabet customers consume the whole budget.
--
-- Measured on testing: 6000 rows fetched -> 211 distinct customers, reaching
-- only '3S DESIGN' .. 'Cash'. 1165 of 1376 customers were unreachable, so the
-- fuzzy matcher never even saw them.
--
-- That is how MUNDHRA AGENCIES became a garbage invoice on 2026-08-03. The
-- model read the header perfectly; the customer simply was not a candidate.
-- Every name it scored started with A or B. The targeted ILIKE rescue query
-- missed too: the ledger says MUNDHA-RA, the challan says MUNDH-RA, and a
-- transposition defeats a substring match -- exact matching upstream of a
-- fuzzy matcher, so the fuzzy stage never got the chance.
--
-- DISTINCT is the operation actually wanted, and PostgREST cannot express it.
-- One query returns ~1.4k names instead of 6000 rows yielding 211.
--
-- Idempotent: safe to run on any client/testing project.

CREATE OR REPLACE FUNCTION public.get_distinct_party_names()
 RETURNS TABLE(party_name text)
 LANGUAGE sql
 STABLE
AS $function$
  SELECT DISTINCT btrim(v.party_name)
  FROM vouchers v
  WHERE v.party_name IS NOT NULL
    AND btrim(v.party_name) <> ''
  ORDER BY 1;
$function$;
