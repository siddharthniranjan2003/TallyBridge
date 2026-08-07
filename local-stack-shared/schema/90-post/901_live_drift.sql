-- Objects that exist on the live testing project (yynuuysvjeipawzfbeme) but are
-- created by no .sql file anywhere in this repo.
--
-- They were hand-applied through the Supabase SQL Editor — several repo .sql files
-- instruct exactly that in their header comments, which is the mechanism behind
-- this drift and behind the earlier push_queue drift. Without this file a local DB
-- built from backend/full_schema.sql + supabase/migrations/ is missing 2 tables,
-- 2 RPCs and 3 columns, and `schema-parity.mjs` fails.
--
-- Every column name, type, nullability and default below was read verbatim off
-- live's PostgREST OpenAPI spec (GET /rest/v1/). Only the two function BODIES are
-- reconstructed — flagged individually below.
--
-- Idempotent: safe to re-run.

-- ── voucher_items: denormalized trio ───────────────────────────────────────────
-- Live voucher_items has 12 columns; backend/full_schema.sql declares 9.
--
-- Worth knowing before relying on these: they are NULL in all 102,818 live rows.
-- Nothing in the sync path populates them, so they are dead weight on live too,
-- not just locally. They exist here only so the column list matches.
ALTER TABLE public.voucher_items ADD COLUMN IF NOT EXISTS date         DATE;
ALTER TABLE public.voucher_items ADD COLUMN IF NOT EXISTS party_name   TEXT;
ALTER TABLE public.voucher_items ADD COLUMN IF NOT EXISTS voucher_type TEXT;

-- ── Purchase_Matching ──────────────────────────────────────────────────────────
-- Curated vendor-description -> Tally-item lookup, read by the purchase OCR path
-- (parsing/purchase/company_context.py:174). 321 rows on live.
--
-- Mixed-case identifier and mixed-case column names with spaces, so every
-- reference must stay double-quoted. This matches the existing convention of
-- backend/supabase_audit_trail_purchase.sql, which calls the style out explicitly.
--
-- `id` is BIGINT NOT NULL with NO default on live — rows carry ids supplied by
-- whatever loaded them. Reproduced as-is rather than "improved" to an identity
-- column, because copy-live-to-local.mjs carries primary keys across verbatim.
CREATE TABLE IF NOT EXISTS public."Purchase_Matching" (
  id                         BIGINT NOT NULL,
  "Vendor Name"              TEXT,
  "Invoice Item Description" TEXT,
  "Tally Item Description"   TEXT,
  CONSTRAINT "Purchase_Matching_pkey" PRIMARY KEY (id)
);

-- ── purchase_matching_api ──────────────────────────────────────────────────────
-- Same four columns, same 321 rows, and PostgREST reports no NOT-NULL columns for
-- it — that is precisely how PostgREST describes a view. Tried first by the parser
-- (company_context.py:169) with the base table as the fallback, so the two must
-- agree.
CREATE OR REPLACE VIEW public.purchase_matching_api AS
  SELECT
    id,
    "Vendor Name",
    "Invoice Item Description",
    "Tally Item Description"
  FROM public."Purchase_Matching";

-- ── get_sale_items_for_party / get_purchase_items_for_party ────────────────────
-- BODIES RECONSTRUCTED, NOT READ. The signature (p_party_name text) and the fact
-- that both exist are read from live's spec; the SQL is inferred from
-- aiaccountant/md_files/handoff_2026-06-08_pickers_editing_config.md §2.
--
-- Safe to get slightly wrong, and cheap to keep: nothing in this repo calls
-- either one (grep is clean), and because they read voucher_items.party_name —
-- NULL in every live row, see above — they return zero rows on live as well.
-- The app moved to a `vouchers!inner` embed instead. They are here for parity
-- only; if a caller ever appears, verify the body against live first.
CREATE OR REPLACE FUNCTION public.get_sale_items_for_party(p_party_name text)
 RETURNS TABLE(stock_item_name text, quantity numeric, unit text, rate numeric, discount_pct numeric, amount numeric, date date)
 LANGUAGE sql
 STABLE
AS $function$
  SELECT vi.stock_item_name, vi.quantity, vi.unit, vi.rate, vi.discount_pct, vi.amount, vi.date
  FROM public.voucher_items vi
  WHERE lower(vi.party_name) = lower(p_party_name)
    AND vi.voucher_type ILIKE '%SALE%'
  ORDER BY vi.date DESC NULLS LAST;
$function$;

CREATE OR REPLACE FUNCTION public.get_purchase_items_for_party(p_party_name text)
 RETURNS TABLE(stock_item_name text, quantity numeric, unit text, rate numeric, discount_pct numeric, amount numeric, date date)
 LANGUAGE sql
 STABLE
AS $function$
  SELECT vi.stock_item_name, vi.quantity, vi.unit, vi.rate, vi.discount_pct, vi.amount, vi.date
  FROM public.voucher_items vi
  WHERE lower(vi.party_name) = lower(p_party_name)
    AND vi.voucher_type ILIKE '%PURCHASE%'
  ORDER BY vi.date DESC NULLS LAST;
$function$;
