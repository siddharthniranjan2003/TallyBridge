-- Sale OCR pricing: decouple rate from discount.
--
-- Until now build_sale_rate_map ran a two-tier waterfall in which rate AND discount
-- always came off the same voucher row: tier 1 = latest row for the same party (any
-- voucher type), tier 2 = latest GST SALE from a different party. Two problems with
-- that. Party history acted as a stale anchor -- once you had sold an item to a party
-- they were pinned to their own last rate forever, even when a newer and more
-- representative sale existed elsewhere. And tier 1 had no voucher_type filter, so a
-- party who is both customer and supplier could have a sale priced off their last
-- PURCHASE (supplier cost price on a customer invoice) -- the bug 5bcbc80 fixed for
-- tier 2 but never for tier 1.
--
-- New rule, two independent lookups:
--   rate     = latest GST SALE of the item to ANYONE   (party irrelevant)
--   discount = latest GST SALE of the item to THIS party (no history -> 0%)
-- so rate and discount now routinely come from different vouchers. That is intended:
-- rate tracks the item's current market price, discount tracks this customer's terms.
--
-- Both take the whole item-name array so a scan costs 2 round-trips instead of the
-- old 1 + N serial PostgREST calls (each of which paid a fresh TLS handshake).
--
-- Deliberately NOT touching get_latest_rates_for_party: n8n/gsheet_appscript.js:42
-- calls it for the Google Sheet rate-fill tool.
--
-- No hygiene filters (no is_cancelled, no rate > 0, no unit match) -- specified.
-- No company_id filter -- matches the previous behaviour.
--
-- NULLS LAST on both ordering keys is deliberate and is NOT a filter: plain DESC in
-- Postgres is NULLS FIRST, so a NULL vouchers.date would outrank every dated voucher
-- and a NULL rate would win every same-date tie. get_latest_rates_for_party still has
-- that latent bug; the tier-2 PostgREST query already guarded it with nullslast.
--
-- No statement_timeout: the parsing service authenticates with the service key, which
-- has no such limit (see 20260619_ingest_statement_timeout.sql, which exists because
-- PostgREST under the anon role does).
--
-- Idempotent: safe to run on any client/testing project. Applied to yynuu (testing).

-- Rate: latest GST SALE of each item to ANYONE. Ties on the invoice date are broken
-- by the highest rate. party_name comes back only so the caller can label rate_source
-- same_party vs different_party; it is not persisted anywhere.
CREATE OR REPLACE FUNCTION public.get_latest_sale_rates_for_items(p_item_names text[])
 RETURNS TABLE(stock_item_name text, rate numeric, party_name text)
 LANGUAGE sql
 STABLE
AS $function$
  SELECT DISTINCT ON (vi.stock_item_name)
    vi.stock_item_name,
    vi.rate,
    v.party_name
  FROM voucher_items vi
  JOIN vouchers v ON v.id = vi.voucher_id
  WHERE vi.stock_item_name = ANY(p_item_names)
    AND v.voucher_type = 'GST SALE'
  ORDER BY vi.stock_item_name, v.date DESC NULLS LAST, vi.rate DESC NULLS LAST;
$function$;

-- Discount: latest GST SALE of each item to THIS party. Same ORDER BY as the rate
-- query, so on a same-date tie both answers come off the highest-rate row.
-- Exact case-insensitive party match, NOT ILIKE: the party name is chosen by the
-- fuzzy matcher out of vouchers.party_name, so it is already an exact existing value,
-- and raw ILIKE would treat % / _ in a party name as wildcards.
CREATE OR REPLACE FUNCTION public.get_latest_party_discounts_for_items(
  p_party_name text,
  p_item_names text[]
)
 RETURNS TABLE(stock_item_name text, discount_pct numeric)
 LANGUAGE sql
 STABLE
AS $function$
  SELECT DISTINCT ON (vi.stock_item_name)
    vi.stock_item_name,
    vi.discount_pct
  FROM voucher_items vi
  JOIN vouchers v ON v.id = vi.voucher_id
  WHERE vi.stock_item_name = ANY(p_item_names)
    AND v.voucher_type = 'GST SALE'
    AND lower(v.party_name) = lower(p_party_name)
  ORDER BY vi.stock_item_name, v.date DESC NULLS LAST, vi.rate DESC NULLS LAST;
$function$;
