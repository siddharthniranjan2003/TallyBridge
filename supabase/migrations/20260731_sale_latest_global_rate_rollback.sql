-- ROLLBACK of 20260731_sale_latest_global_rate.sql
-- Drops the two batch lookup functions added for the global-latest sale rate.
--
-- Both were purely additive -- nothing else in the schema references them and
-- get_latest_rates_for_party was never modified -- so dropping them is enough to
-- undo the DB half of the change.
--
-- NOTE: the code half must be reverted too, or the sale path will silently price
-- every line at 0. parsing/server/handler.py keeps the old helpers
-- (fetch_latest_rates_for_party, fetch_fallback_rate_for_item, build_sale_rate_map)
-- byte-identical and uncalled, so the revert is a one-line change at the
-- build_sale_voucher_payload call site: build_sale_rate_map_global ->
-- build_sale_rate_map.

DROP FUNCTION IF EXISTS public.get_latest_sale_rates_for_items(text[]);
DROP FUNCTION IF EXISTS public.get_latest_party_discounts_for_items(text, text[]);
