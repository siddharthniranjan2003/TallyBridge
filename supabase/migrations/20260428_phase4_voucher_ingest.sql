CREATE OR REPLACE FUNCTION public.tb_compact_date_to_date(p_value TEXT)
RETURNS DATE
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT CASE
    WHEN p_value IS NULL OR btrim(p_value) = '' THEN NULL
    WHEN btrim(p_value) ~ '^\d{8}$' THEN to_date(btrim(p_value), 'YYYYMMDD')
    WHEN btrim(p_value) ~ '^\d{4}-\d{2}-\d{2}$' THEN btrim(p_value)::DATE
    ELSE NULL
  END
$$;


CREATE OR REPLACE FUNCTION public.tb_is_purchase_voucher_type(p_value TEXT)
RETURNS BOOLEAN
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT CASE
    WHEN p_value IS NULL THEN FALSE
    WHEN lower(btrim(p_value)) IN ('purchase', 'local purchase', 'import purchase') THEN TRUE
    WHEN lower(btrim(p_value)) LIKE '%purchase%' AND lower(btrim(p_value)) NOT LIKE '%order%' THEN TRUE
    ELSE FALSE
  END
$$;


CREATE OR REPLACE FUNCTION public.tb_ingest_vouchers(
  p_company_id UUID,
  p_synced_at TIMESTAMPTZ DEFAULT now(),
  p_vouchers JSONB DEFAULT '[]'::jsonb,
  p_sync_meta JSONB DEFAULT '{}'::jsonb,
  p_alter_ids JSONB DEFAULT '{}'::jsonb,
  p_is_final_chunk BOOLEAN DEFAULT FALSE,
  p_record_counts JSONB DEFAULT '{}'::jsonb
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
  v_result JSONB := '{}'::jsonb;
  v_synced_at TIMESTAMPTZ := COALESCE(p_synced_at, now());
  v_vouchers JSONB := COALESCE(p_vouchers, '[]'::jsonb);
  v_sync_meta JSONB := COALESCE(p_sync_meta, '{}'::jsonb);
  v_alter_ids JSONB := COALESCE(p_alter_ids, '{}'::jsonb);
  v_record_counts JSONB := COALESCE(p_record_counts, '{}'::jsonb);
  v_item_count INTEGER := 0;
  v_entry_count INTEGER := 0;
  v_purchase_count INTEGER := 0;
  v_voucher_sync_mode TEXT := lower(COALESCE(NULLIF(btrim(v_sync_meta->>'voucher_sync_mode'), ''), 'full'));
  v_voucher_from_date DATE := public.tb_compact_date_to_date(v_sync_meta->>'voucher_from_date');
  v_voucher_to_date DATE := public.tb_compact_date_to_date(v_sync_meta->>'voucher_to_date');
  v_log_sync_meta JSONB := '{}'::jsonb;
  v_voucher_count INTEGER := 0;
BEGIN
  IF p_company_id IS NULL THEN
    RAISE EXCEPTION 'p_company_id is required';
  END IF;

  IF jsonb_typeof(v_vouchers) <> 'array' THEN
    RAISE EXCEPTION 'p_vouchers must be a JSON array';
  END IF;

  v_voucher_count := jsonb_array_length(v_vouchers);

  CREATE TEMP TABLE IF NOT EXISTS pg_temp.tb_phase4_upserted_vouchers (
    id UUID PRIMARY KEY,
    tally_guid TEXT NOT NULL UNIQUE
  ) ON COMMIT DROP;
  TRUNCATE TABLE pg_temp.tb_phase4_upserted_vouchers;

  WITH voucher_source AS (
    SELECT
      voucher_row,
      NULLIF(btrim(voucher_row->>'tally_guid'), '') AS tally_guid
    FROM jsonb_array_elements(v_vouchers) AS voucher_row
  ),
  upserted AS (
    INSERT INTO public.vouchers (
      company_id,
      tally_guid,
      alter_id,
      master_id,
      voucher_number,
      voucher_type,
      date,
      party_name,
      amount,
      narration,
      is_cancelled,
      is_optional,
      reference,
      is_invoice,
      view,
      synced_at
    )
    SELECT
      p_company_id,
      tally_guid,
      NULLIF(btrim(voucher_row->>'alter_id'), '')::INTEGER,
      NULLIF(btrim(voucher_row->>'master_id'), '')::INTEGER,
      NULLIF(btrim(voucher_row->>'voucher_number'), ''),
      NULLIF(btrim(voucher_row->>'voucher_type'), ''),
      public.tb_compact_date_to_date(voucher_row->>'date'),
      NULLIF(btrim(voucher_row->>'party_name'), ''),
      COALESCE(NULLIF(btrim(voucher_row->>'amount'), '')::NUMERIC, 0),
      NULLIF(btrim(voucher_row->>'narration'), ''),
      COALESCE(NULLIF(btrim(voucher_row->>'is_cancelled'), '')::BOOLEAN, FALSE),
      COALESCE(NULLIF(btrim(voucher_row->>'is_optional'), '')::BOOLEAN, FALSE),
      NULLIF(btrim(voucher_row->>'reference'), ''),
      COALESCE(NULLIF(btrim(voucher_row->>'is_invoice'), '')::BOOLEAN, FALSE),
      NULLIF(btrim(voucher_row->>'view'), ''),
      v_synced_at
    FROM voucher_source
    WHERE tally_guid IS NOT NULL
    ON CONFLICT (company_id, tally_guid) DO UPDATE
      SET alter_id = EXCLUDED.alter_id,
          master_id = EXCLUDED.master_id,
          voucher_number = EXCLUDED.voucher_number,
          voucher_type = EXCLUDED.voucher_type,
          date = EXCLUDED.date,
          party_name = EXCLUDED.party_name,
          amount = EXCLUDED.amount,
          narration = EXCLUDED.narration,
          is_cancelled = EXCLUDED.is_cancelled,
          is_optional = EXCLUDED.is_optional,
          reference = EXCLUDED.reference,
          is_invoice = EXCLUDED.is_invoice,
          view = EXCLUDED.view,
          synced_at = EXCLUDED.synced_at
    RETURNING id, tally_guid
  )
  INSERT INTO pg_temp.tb_phase4_upserted_vouchers (id, tally_guid)
  SELECT id, tally_guid
  FROM upserted;

  DELETE FROM public.voucher_items
  WHERE voucher_id IN (SELECT id FROM pg_temp.tb_phase4_upserted_vouchers);

  DELETE FROM public.voucher_ledger_entries
  WHERE voucher_id IN (SELECT id FROM pg_temp.tb_phase4_upserted_vouchers);

  WITH voucher_source AS (
    SELECT
      voucher_row,
      NULLIF(btrim(voucher_row->>'tally_guid'), '') AS tally_guid
    FROM jsonb_array_elements(v_vouchers) AS voucher_row
  )
  INSERT INTO public.voucher_items (
    voucher_id,
    stock_item_name,
    quantity,
    unit,
    rate,
    discount_pct,
    amount
  )
  SELECT
    upserted.id,
    btrim(item_row->>'stock_item_name'),
    COALESCE(NULLIF(btrim(item_row->>'quantity'), '')::NUMERIC, 0),
    NULLIF(btrim(item_row->>'unit'), ''),
    COALESCE(NULLIF(btrim(item_row->>'rate'), '')::NUMERIC, 0),
    COALESCE(NULLIF(btrim(item_row->>'discount_pct'), '')::NUMERIC, 0),
    COALESCE(NULLIF(btrim(item_row->>'amount'), '')::NUMERIC, 0)
  FROM voucher_source
  JOIN pg_temp.tb_phase4_upserted_vouchers AS upserted
    ON upserted.tally_guid = voucher_source.tally_guid
  CROSS JOIN LATERAL jsonb_array_elements(
    CASE
      WHEN jsonb_typeof(voucher_row->'items') = 'array' THEN voucher_row->'items'
      ELSE '[]'::jsonb
    END
  ) AS item_row
  WHERE NULLIF(btrim(item_row->>'stock_item_name'), '') IS NOT NULL;
  GET DIAGNOSTICS v_item_count = ROW_COUNT;

  WITH voucher_source AS (
    SELECT
      voucher_row,
      NULLIF(btrim(voucher_row->>'tally_guid'), '') AS tally_guid
    FROM jsonb_array_elements(v_vouchers) AS voucher_row
  )
  INSERT INTO public.voucher_ledger_entries (
    voucher_id,
    ledger_name,
    amount,
    is_party_ledger,
    is_deemed_positive,
    bill_allocations
  )
  SELECT
    upserted.id,
    btrim(entry_row->>'ledger_name'),
    COALESCE(NULLIF(btrim(entry_row->>'amount'), '')::NUMERIC, 0),
    COALESCE(NULLIF(btrim(entry_row->>'is_party_ledger'), '')::BOOLEAN, FALSE),
    COALESCE(NULLIF(btrim(entry_row->>'is_deemed_positive'), '')::BOOLEAN, FALSE),
    CASE
      WHEN jsonb_typeof(entry_row->'bill_allocations') = 'array' THEN entry_row->'bill_allocations'
      ELSE '[]'::jsonb
    END
  FROM voucher_source
  JOIN pg_temp.tb_phase4_upserted_vouchers AS upserted
    ON upserted.tally_guid = voucher_source.tally_guid
  CROSS JOIN LATERAL jsonb_array_elements(
    CASE
      WHEN jsonb_typeof(voucher_row->'ledger_entries') = 'array' THEN voucher_row->'ledger_entries'
      ELSE '[]'::jsonb
    END
  ) AS entry_row
  WHERE NULLIF(btrim(entry_row->>'ledger_name'), '') IS NOT NULL;
  GET DIAGNOSTICS v_entry_count = ROW_COUNT;

  WITH voucher_source AS (
    SELECT
      voucher_row,
      NULLIF(btrim(voucher_row->>'tally_guid'), '') AS tally_guid
    FROM jsonb_array_elements(v_vouchers) AS voucher_row
  )
  INSERT INTO public.purchases (
    company_id,
    voucher_id,
    tally_guid,
    voucher_number,
    voucher_type,
    date,
    party_name,
    amount,
    narration,
    reference,
    is_cancelled,
    is_invoice,
    synced_at
  )
  SELECT
    p_company_id,
    upserted.id,
    upserted.tally_guid,
    NULLIF(btrim(voucher_row->>'voucher_number'), ''),
    NULLIF(btrim(voucher_row->>'voucher_type'), ''),
    public.tb_compact_date_to_date(voucher_row->>'date'),
    NULLIF(btrim(voucher_row->>'party_name'), ''),
    COALESCE(NULLIF(btrim(voucher_row->>'amount'), '')::NUMERIC, 0),
    NULLIF(btrim(voucher_row->>'narration'), ''),
    NULLIF(btrim(voucher_row->>'reference'), ''),
    COALESCE(NULLIF(btrim(voucher_row->>'is_cancelled'), '')::BOOLEAN, FALSE),
    COALESCE(NULLIF(btrim(voucher_row->>'is_invoice'), '')::BOOLEAN, FALSE),
    v_synced_at
  FROM voucher_source
  JOIN pg_temp.tb_phase4_upserted_vouchers AS upserted
    ON upserted.tally_guid = voucher_source.tally_guid
  WHERE public.tb_is_purchase_voucher_type(voucher_row->>'voucher_type')
  ON CONFLICT (company_id, tally_guid) DO UPDATE
    SET voucher_id = EXCLUDED.voucher_id,
        voucher_number = EXCLUDED.voucher_number,
        voucher_type = EXCLUDED.voucher_type,
        date = EXCLUDED.date,
        party_name = EXCLUDED.party_name,
        amount = EXCLUDED.amount,
        narration = EXCLUDED.narration,
        reference = EXCLUDED.reference,
        is_cancelled = EXCLUDED.is_cancelled,
        is_invoice = EXCLUDED.is_invoice,
        synced_at = EXCLUDED.synced_at;
  GET DIAGNOSTICS v_purchase_count = ROW_COUNT;

  IF p_is_final_chunk THEN
    CREATE TEMP TABLE IF NOT EXISTS pg_temp.tb_phase4_stale_voucher_ids (
      id UUID PRIMARY KEY
    ) ON COMMIT DROP;
    TRUNCATE TABLE pg_temp.tb_phase4_stale_voucher_ids;

    IF v_voucher_sync_mode = 'full' THEN
      INSERT INTO pg_temp.tb_phase4_stale_voucher_ids (id)
      SELECT id
      FROM public.vouchers
      WHERE company_id = p_company_id
        AND synced_at <> v_synced_at;

      DELETE FROM public.purchases
      WHERE company_id = p_company_id
        AND synced_at <> v_synced_at;
    ELSIF v_voucher_sync_mode = 'incremental'
      AND v_voucher_from_date IS NOT NULL
      AND v_voucher_to_date IS NOT NULL THEN
      INSERT INTO pg_temp.tb_phase4_stale_voucher_ids (id)
      SELECT id
      FROM public.vouchers
      WHERE company_id = p_company_id
        AND date >= v_voucher_from_date
        AND date <= v_voucher_to_date
        AND synced_at <> v_synced_at;

      DELETE FROM public.purchases
      WHERE company_id = p_company_id
        AND date >= v_voucher_from_date
        AND date <= v_voucher_to_date
        AND synced_at <> v_synced_at;
    END IF;

    DELETE FROM public.voucher_items
    WHERE voucher_id IN (SELECT id FROM pg_temp.tb_phase4_stale_voucher_ids);

    DELETE FROM public.voucher_ledger_entries
    WHERE voucher_id IN (SELECT id FROM pg_temp.tb_phase4_stale_voucher_ids);

    DELETE FROM public.vouchers
    WHERE id IN (SELECT id FROM pg_temp.tb_phase4_stale_voucher_ids);

    UPDATE public.companies
    SET last_synced_at = v_synced_at,
        alter_id = COALESCE(NULLIF(btrim(v_alter_ids->>'alter_id'), ''), alter_id),
        alt_vch_id = COALESCE(NULLIF(btrim(v_alter_ids->>'alt_vch_id'), ''), alt_vch_id),
        alt_mst_id = COALESCE(NULLIF(btrim(v_alter_ids->>'alt_mst_id'), ''), alt_mst_id),
        last_voucher_date = COALESCE(
          public.tb_compact_date_to_date(v_alter_ids->>'last_voucher_date'),
          last_voucher_date
        )
    WHERE id = p_company_id;

    v_log_sync_meta := v_sync_meta
      - ARRAY[
        'record_counts',
        'chunk_index',
        'chunk_count',
        'is_final_chunk',
        'voucher_chunk_index',
        'voucher_chunk_count',
        'voucher_chunk_size'
      ];

    BEGIN
      INSERT INTO public.sync_log (
        company_id,
        status,
        records_synced,
        sync_meta
      )
      VALUES (
        p_company_id,
        'success',
        COALESCE(
          NULLIF(v_record_counts, '{}'::jsonb),
          jsonb_build_object(
            'vouchers', v_voucher_count,
            'purchases', v_purchase_count,
            'voucher_items', v_item_count,
            'voucher_ledger_entries', v_entry_count
          )
        ),
        v_log_sync_meta
      );
    EXCEPTION
      WHEN OTHERS THEN
        BEGIN
          INSERT INTO public.sync_log (
            company_id,
            status,
            records_synced
          )
          VALUES (
            p_company_id,
            'success',
            COALESCE(
              NULLIF(v_record_counts, '{}'::jsonb),
              jsonb_build_object(
                'vouchers', v_voucher_count,
                'purchases', v_purchase_count,
                'voucher_items', v_item_count,
                'voucher_ledger_entries', v_entry_count
              )
            )
          );
        EXCEPTION
          WHEN OTHERS THEN
            NULL;
        END;
    END;
  END IF;

  v_result := jsonb_build_object(
    'vouchers', v_voucher_count,
    'voucher_items', v_item_count,
    'voucher_ledger_entries', v_entry_count,
    'purchases', v_purchase_count,
    'finalized', p_is_final_chunk
  );

  IF p_is_final_chunk THEN
    v_result := COALESCE(NULLIF(v_record_counts, '{}'::jsonb), '{}'::jsonb)
      || jsonb_build_object(
        'purchases',
        COALESCE(NULLIF(v_record_counts->>'purchases', '')::INTEGER, v_purchase_count),
        'voucher_items',
        COALESCE(NULLIF(v_record_counts->>'voucher_items', '')::INTEGER, v_item_count),
        'voucher_ledger_entries',
        COALESCE(NULLIF(v_record_counts->>'voucher_ledger_entries', '')::INTEGER, v_entry_count),
        'finalized',
        TRUE
      );
  END IF;

  RETURN v_result;
END;
$$;


CREATE OR REPLACE FUNCTION public.tb_ingest_phase4_full(
  p_company_id UUID,
  p_synced_at TIMESTAMPTZ DEFAULT now(),
  p_groups JSONB DEFAULT NULL,
  p_ledgers JSONB DEFAULT NULL,
  p_stock_items JSONB DEFAULT NULL,
  p_outstanding JSONB DEFAULT NULL,
  p_profit_loss JSONB DEFAULT NULL,
  p_balance_sheet JSONB DEFAULT NULL,
  p_trial_balance JSONB DEFAULT NULL,
  p_vouchers JSONB DEFAULT '[]'::jsonb,
  p_sync_meta JSONB DEFAULT '{}'::jsonb,
  p_alter_ids JSONB DEFAULT '{}'::jsonb,
  p_record_counts JSONB DEFAULT '{}'::jsonb
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
  v_result JSONB := '{}'::jsonb;
BEGIN
  v_result := v_result
    || COALESCE(
      public.tb_ingest_phase3_hybrid(
        p_company_id := p_company_id,
        p_synced_at := p_synced_at,
        p_groups := p_groups,
        p_ledgers := p_ledgers,
        p_stock_items := p_stock_items,
        p_outstanding := p_outstanding,
        p_profit_loss := p_profit_loss,
        p_balance_sheet := p_balance_sheet,
        p_trial_balance := p_trial_balance
      ),
      '{}'::jsonb
    )
    || COALESCE(
      public.tb_ingest_vouchers(
        p_company_id := p_company_id,
        p_synced_at := p_synced_at,
        p_vouchers := p_vouchers,
        p_sync_meta := p_sync_meta,
        p_alter_ids := p_alter_ids,
        p_is_final_chunk := TRUE,
        p_record_counts := p_record_counts
      ),
      '{}'::jsonb
    );

  RETURN v_result;
END;
$$;
