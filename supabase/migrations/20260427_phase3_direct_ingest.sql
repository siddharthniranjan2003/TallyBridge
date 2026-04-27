CREATE OR REPLACE FUNCTION public.tb_ingest_masters(
  p_company_id UUID,
  p_synced_at TIMESTAMPTZ DEFAULT now(),
  p_groups JSONB DEFAULT NULL,
  p_ledgers JSONB DEFAULT NULL,
  p_stock_items JSONB DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql
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
      v_synced_at
    FROM jsonb_array_elements(COALESCE(p_stock_items, '[]'::jsonb)) AS stock_row
    WHERE NULLIF(TRIM(stock_row->>'name'), '') IS NOT NULL
    ON CONFLICT (company_id, name) DO UPDATE
      SET group_name = EXCLUDED.group_name,
          unit = EXCLUDED.unit,
          closing_qty = EXCLUDED.closing_qty,
          closing_value = EXCLUDED.closing_value,
          rate = EXCLUDED.rate,
          synced_at = EXCLUDED.synced_at;

    v_result := v_result || jsonb_build_object(
      'stock',
      jsonb_array_length(COALESCE(p_stock_items, '[]'::jsonb))
    );
  END IF;

  RETURN v_result;
END;
$$;


CREATE OR REPLACE FUNCTION public.tb_ingest_snapshots(
  p_company_id UUID,
  p_synced_at TIMESTAMPTZ DEFAULT now(),
  p_outstanding JSONB DEFAULT NULL,
  p_profit_loss JSONB DEFAULT NULL,
  p_balance_sheet JSONB DEFAULT NULL,
  p_trial_balance JSONB DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
  v_result JSONB := '{}'::jsonb;
  v_synced_at TIMESTAMPTZ := COALESCE(p_synced_at, now());
BEGIN
  IF p_company_id IS NULL THEN
    RAISE EXCEPTION 'p_company_id is required';
  END IF;

  IF p_outstanding IS NOT NULL THEN
    IF jsonb_typeof(p_outstanding) <> 'array' THEN
      RAISE EXCEPTION 'p_outstanding must be a JSON array';
    END IF;

    INSERT INTO public.outstanding (
      company_id,
      party_name,
      type,
      voucher_number,
      voucher_date,
      due_date,
      original_amount,
      pending_amount,
      days_overdue,
      synced_at
    )
    SELECT
      p_company_id,
      TRIM(outstanding_row->>'party_name'),
      TRIM(outstanding_row->>'type'),
      NULLIF(TRIM(outstanding_row->>'voucher_number'), ''),
      NULLIF(TRIM(outstanding_row->>'voucher_date'), '')::DATE,
      NULLIF(TRIM(outstanding_row->>'due_date'), '')::DATE,
      COALESCE(NULLIF(TRIM(outstanding_row->>'original_amount'), '')::NUMERIC, 0),
      COALESCE(NULLIF(TRIM(outstanding_row->>'pending_amount'), '')::NUMERIC, 0),
      COALESCE(NULLIF(TRIM(outstanding_row->>'days_overdue'), '')::INTEGER, 0),
      v_synced_at
    FROM jsonb_array_elements(COALESCE(p_outstanding, '[]'::jsonb)) AS outstanding_row;

    UPDATE public.companies
      SET last_outstanding_synced_at = v_synced_at
      WHERE id = p_company_id;

    DELETE FROM public.outstanding
      WHERE company_id = p_company_id
        AND synced_at <> v_synced_at;

    v_result := v_result || jsonb_build_object(
      'outstanding',
      jsonb_array_length(COALESCE(p_outstanding, '[]'::jsonb))
    );
  END IF;

  IF p_profit_loss IS NOT NULL THEN
    IF jsonb_typeof(p_profit_loss) <> 'array' THEN
      RAISE EXCEPTION 'p_profit_loss must be a JSON array';
    END IF;

    INSERT INTO public.profit_loss (
      company_id,
      particulars,
      amount,
      is_debit,
      synced_at
    )
    SELECT
      p_company_id,
      TRIM(profit_loss_row->>'particulars'),
      COALESCE(NULLIF(TRIM(profit_loss_row->>'amount'), '')::NUMERIC, 0),
      COALESCE(NULLIF(TRIM(profit_loss_row->>'is_debit'), '')::BOOLEAN, FALSE),
      v_synced_at
    FROM jsonb_array_elements(COALESCE(p_profit_loss, '[]'::jsonb)) AS profit_loss_row;

    UPDATE public.companies
      SET last_profit_loss_synced_at = v_synced_at
      WHERE id = p_company_id;

    DELETE FROM public.profit_loss
      WHERE company_id = p_company_id
        AND synced_at <> v_synced_at;

    v_result := v_result || jsonb_build_object(
      'profit_loss',
      jsonb_array_length(COALESCE(p_profit_loss, '[]'::jsonb))
    );
  END IF;

  IF p_balance_sheet IS NOT NULL THEN
    IF jsonb_typeof(p_balance_sheet) <> 'array' THEN
      RAISE EXCEPTION 'p_balance_sheet must be a JSON array';
    END IF;

    INSERT INTO public.balance_sheet (
      company_id,
      particulars,
      amount,
      side,
      synced_at
    )
    SELECT
      p_company_id,
      TRIM(balance_sheet_row->>'particulars'),
      COALESCE(NULLIF(TRIM(balance_sheet_row->>'amount'), '')::NUMERIC, 0),
      COALESCE(NULLIF(TRIM(balance_sheet_row->>'side'), ''), 'asset'),
      v_synced_at
    FROM jsonb_array_elements(COALESCE(p_balance_sheet, '[]'::jsonb)) AS balance_sheet_row;

    UPDATE public.companies
      SET last_balance_sheet_synced_at = v_synced_at
      WHERE id = p_company_id;

    DELETE FROM public.balance_sheet
      WHERE company_id = p_company_id
        AND synced_at <> v_synced_at;

    v_result := v_result || jsonb_build_object(
      'balance_sheet',
      jsonb_array_length(COALESCE(p_balance_sheet, '[]'::jsonb))
    );
  END IF;

  IF p_trial_balance IS NOT NULL THEN
    IF jsonb_typeof(p_trial_balance) <> 'array' THEN
      RAISE EXCEPTION 'p_trial_balance must be a JSON array';
    END IF;

    INSERT INTO public.trial_balance (
      company_id,
      particulars,
      debit,
      credit,
      synced_at
    )
    SELECT
      p_company_id,
      TRIM(trial_balance_row->>'particulars'),
      COALESCE(NULLIF(TRIM(trial_balance_row->>'debit'), '')::NUMERIC, 0),
      COALESCE(NULLIF(TRIM(trial_balance_row->>'credit'), '')::NUMERIC, 0),
      v_synced_at
    FROM jsonb_array_elements(COALESCE(p_trial_balance, '[]'::jsonb)) AS trial_balance_row;

    UPDATE public.companies
      SET last_trial_balance_synced_at = v_synced_at
      WHERE id = p_company_id;

    DELETE FROM public.trial_balance
      WHERE company_id = p_company_id
        AND synced_at <> v_synced_at;

    v_result := v_result || jsonb_build_object(
      'trial_balance',
      jsonb_array_length(COALESCE(p_trial_balance, '[]'::jsonb))
    );
  END IF;

  RETURN v_result;
END;
$$;


CREATE OR REPLACE FUNCTION public.tb_ingest_phase3_hybrid(
  p_company_id UUID,
  p_synced_at TIMESTAMPTZ DEFAULT now(),
  p_groups JSONB DEFAULT NULL,
  p_ledgers JSONB DEFAULT NULL,
  p_stock_items JSONB DEFAULT NULL,
  p_outstanding JSONB DEFAULT NULL,
  p_profit_loss JSONB DEFAULT NULL,
  p_balance_sheet JSONB DEFAULT NULL,
  p_trial_balance JSONB DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
  v_result JSONB := '{}'::jsonb;
BEGIN
  v_result := v_result
    || COALESCE(
      public.tb_ingest_masters(
        p_company_id := p_company_id,
        p_synced_at := p_synced_at,
        p_groups := p_groups,
        p_ledgers := p_ledgers,
        p_stock_items := p_stock_items
      ),
      '{}'::jsonb
    )
    || COALESCE(
      public.tb_ingest_snapshots(
        p_company_id := p_company_id,
        p_synced_at := p_synced_at,
        p_outstanding := p_outstanding,
        p_profit_loss := p_profit_loss,
        p_balance_sheet := p_balance_sheet,
        p_trial_balance := p_trial_balance
      ),
      '{}'::jsonb
    );

  RETURN v_result;
END;
$$;
