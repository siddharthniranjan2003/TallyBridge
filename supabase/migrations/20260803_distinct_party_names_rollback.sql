-- ROLLBACK of 20260803_distinct_party_names.sql
--
-- Purely additive -- nothing else references this function -- so dropping it is
-- enough to undo the DB half.
--
-- The code half needs no revert: fetch_supabase_party_candidates treats a
-- missing RPC as "fall back to the old paging loop", so dropping this function
-- silently restores the previous behaviour (211 reachable customers) rather
-- than breaking party matching. That fallback is deliberate -- a 404 that
-- wiped the candidate list would turn every scan into a garbage invoice.

DROP FUNCTION IF EXISTS public.get_distinct_party_names();
