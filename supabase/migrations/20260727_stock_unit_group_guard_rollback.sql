-- ROLLBACK of 20260727_stock_unit_group_guard.sql
-- Restores public.tb_ingest_masters to its 20260619_stock_items_part_code.sql
-- form: unconditional overwrite of unit / group_name, and 'Nos' accepted as a
-- literal unit value. Run this to undo the guard.
--
-- NOTE: this only restores the function. Any rows whose unit/group_name were
-- preserved (rather than overwritten) while the guard was active keep the value
-- they have now -- rolling back does not retroactively re-corrupt them, and does
-- not restore rows the guard already protected.

CREATE OR REPLACE FUNCTION public.tb_ingest_masters(
  p_company_id UUID,
  p_synced_at TIMESTAMPTZ DEFAULT now(),
  p_groups JSONB DEFAULT NULL,
  p_ledgers JSONB DEFAULT NULL,
  p_stock_items JSONB DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql
SET statement_timeout = '120s'
AS $$
DECLARE
  v_result JSONB := '{}'::jsonb;
  v_synced_at TIMESTAMPTZ := COALESCE(p_synced_at, now());
BEGIN
  IF p_company_id IS NULL THEN
    RAISE EXCEPTION 'p_company_id is required';
  END IF;

  IF p_groups IS NOT NULL THEN
    IF jsonb_typeof(p_groups) <> 'array' THEN
      RAISE EXCEPTION 'p_groups must be a JSON array';
    END IF;

    INSERT INTO public.groups (
      company_id,
      name,
      parent,
      master_id,
      is_revenue,
      affects_stock,
      is_subledger,
      synced_at
    )
    SELECT
      p_company_id,
      TRIM(group_row->>'name'),
      NULLIF(TRIM(group_row->>'parent'), ''),
      NULLIF(TRIM(group_row->>'master_id'), '')::INTEGER,
      NULLIF(TRIM(group_row->>'is_revenue'), ''),
      NULLIF(TRIM(group_row->>'affects_stock'), ''),
      NULLIF(TRIM(group_row->>'is_subledger'), ''),
      v_synced_at
    FROM jsonb_array_elements(COALESCE(p_groups, '[]'::jsonb)) AS group_row
    WHERE NULLIF(TRIM(group_row->>'name'), '') IS NOT NULL
    ON CONFLICT (company_id, name) DO UPDATE
      SET parent = EXCLUDED.parent,
          master_id = EXCLUDED.master_id,
          is_revenue = EXCLUDED.is_revenue,
          affects_stock = EXCLUDED.affects_stock,
          is_subledger = EXCLUDED.is_subledger,
          synced_at = EXCLUDED.synced_at;

    v_result := v_result || jsonb_build_object(
      'groups',
      jsonb_array_length(COALESCE(p_groups, '[]'::jsonb))
    );
  END IF;

  IF p_ledgers IS NOT NULL THEN
    IF jsonb_typeof(p_ledgers) <> 'array' THEN
      RAISE EXCEPTION 'p_ledgers must be a JSON array';
    END IF;

    INSERT INTO public.ledgers (
      company_id,
      name,
      group_name,
      opening_balance,
      closing_balance,
      master_id,
      email,
      phone,
      mobile,
      pincode,
      gstin,
      state,
      country,
      credit_period,
      credit_limit,
      bank_account,
      ifsc_code,
      pan,
      mailing_name,
      guid,
      synced_at
    )
    SELECT
      p_company_id,
      TRIM(ledger_row->>'name'),
      NULLIF(TRIM(ledger_row->>'group_name'), ''),
      COALESCE(NULLIF(TRIM(ledger_row->>'opening_balance'), '')::NUMERIC, 0),
      COALESCE(NULLIF(TRIM(ledger_row->>'closing_balance'), '')::NUMERIC, 0),
      NULLIF(TRIM(ledger_row->>'master_id'), '')::INTEGER,
      NULLIF(TRIM(ledger_row->>'email'), ''),
      NULLIF(TRIM(ledger_row->>'phone'), ''),
      NULLIF(TRIM(ledger_row->>'mobile'), ''),
      NULLIF(TRIM(ledger_row->>'pincode'), ''),
      NULLIF(TRIM(ledger_row->>'gstin'), ''),
      NULLIF(TRIM(ledger_row->>'state'), ''),
      NULLIF(TRIM(ledger_row->>'country'), ''),
      NULLIF(TRIM(ledger_row->>'credit_period'), ''),
      COALESCE(NULLIF(TRIM(ledger_row->>'credit_limit'), '')::NUMERIC, 0),
      NULLIF(TRIM(ledger_row->>'bank_account'), ''),
      NULLIF(TRIM(ledger_row->>'ifsc_code'), ''),
      NULLIF(TRIM(ledger_row->>'pan'), ''),
      NULLIF(TRIM(ledger_row->>'mailing_name'), ''),
      NULLIF(TRIM(ledger_row->>'guid'), ''),
      v_synced_at
    FROM jsonb_array_elements(COALESCE(p_ledgers, '[]'::jsonb)) AS ledger_row
    WHERE NULLIF(TRIM(ledger_row->>'name'), '') IS NOT NULL
    ON CONFLICT (company_id, name) DO UPDATE
      SET group_name = EXCLUDED.group_name,
          opening_balance = EXCLUDED.opening_balance,
          closing_balance = EXCLUDED.closing_balance,
          master_id = EXCLUDED.master_id,
          email = EXCLUDED.email,
          phone = EXCLUDED.phone,
          mobile = EXCLUDED.mobile,
          pincode = EXCLUDED.pincode,
          gstin = EXCLUDED.gstin,
          state = EXCLUDED.state,
          country = EXCLUDED.country,
          credit_period = EXCLUDED.credit_period,
          credit_limit = EXCLUDED.credit_limit,
          bank_account = EXCLUDED.bank_account,
          ifsc_code = EXCLUDED.ifsc_code,
          pan = EXCLUDED.pan,
          mailing_name = EXCLUDED.mailing_name,
          guid = EXCLUDED.guid,
          synced_at = EXCLUDED.synced_at;

    v_result := v_result || jsonb_build_object(
      'ledgers',
      jsonb_array_length(COALESCE(p_ledgers, '[]'::jsonb))
    );
  END IF;

  IF p_stock_items IS NOT NULL THEN
    IF jsonb_typeof(p_stock_items) <> 'array' THEN
      RAISE EXCEPTION 'p_stock_items must be a JSON array';
    END IF;

    INSERT INTO public.stock_items (
      company_id,
      name,
      group_name,
      unit,
      closing_qty,
      closing_value,
      rate,
      part_code,
      synced_at
    )
    SELECT
      p_company_id,
      TRIM(stock_row->>'name'),
      NULLIF(TRIM(stock_row->>'group_name'), ''),
      NULLIF(TRIM(stock_row->>'unit'), ''),
      COALESCE(NULLIF(TRIM(stock_row->>'closing_qty'), '')::NUMERIC, 0),
      COALESCE(NULLIF(TRIM(stock_row->>'closing_value'), '')::NUMERIC, 0),
      COALESCE(NULLIF(TRIM(stock_row->>'rate'), '')::NUMERIC, 0),
      NULLIF(TRIM(stock_row->>'part_code'), ''),
      v_synced_at
    FROM jsonb_array_elements(COALESCE(p_stock_items, '[]'::jsonb)) AS stock_row
    WHERE NULLIF(TRIM(stock_row->>'name'), '') IS NOT NULL
    ON CONFLICT (company_id, name) DO UPDATE
      SET group_name = EXCLUDED.group_name,
          unit = EXCLUDED.unit,
          closing_qty = EXCLUDED.closing_qty,
          closing_value = EXCLUDED.closing_value,
          rate = EXCLUDED.rate,
          part_code = EXCLUDED.part_code,
          synced_at = EXCLUDED.synced_at;

    v_result := v_result || jsonb_build_object(
      'stock',
      jsonb_array_length(COALESCE(p_stock_items, '[]'::jsonb))
    );
  END IF;

  RETURN v_result;
END;
$$;