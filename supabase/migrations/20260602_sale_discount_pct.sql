-- Sale discount: extend the same-party rate RPC to also return discount_pct so
-- the sale push pipeline can stamp each item with its latest same-party discount.
-- Return type changes, so the function must be dropped before recreate.
DROP FUNCTION IF EXISTS public.get_latest_rates_for_party(text);

CREATE OR REPLACE FUNCTION public.get_latest_rates_for_party(p_party_name text)
 RETURNS TABLE(stock_item_name text, rate numeric, discount_pct numeric)
 LANGUAGE plpgsql
AS $function$
BEGIN
  RETURN QUERY
  SELECT DISTINCT ON (vi.stock_item_name)
    vi.stock_item_name,
    vi.rate,
    vi.discount_pct
  FROM voucher_items vi
  JOIN vouchers v ON v.id = vi.voucher_id
  WHERE v.party_name ILIKE p_party_name
    AND vi.rate IS NOT NULL
    AND vi.rate > 0
    AND v.is_cancelled = false
  ORDER BY vi.stock_item_name, v.date DESC;
END;
$function$;
