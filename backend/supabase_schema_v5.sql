DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'sync_log'
      AND column_name = 'created_at'
  ) AND NOT EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'sync_log'
      AND column_name = 'synced_at'
  ) THEN
    EXECUTE 'ALTER TABLE public.sync_log RENAME COLUMN created_at TO synced_at';
  END IF;
END $$;

ALTER TABLE public.sync_log
ADD COLUMN IF NOT EXISTS synced_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE public.sync_log
ADD COLUMN IF NOT EXISTS error_message TEXT;

ALTER TABLE public.sync_log
ADD COLUMN IF NOT EXISTS sync_meta JSONB;

DROP INDEX IF EXISTS idx_sync_log_company_created_at;

CREATE INDEX IF NOT EXISTS idx_sync_log_company_synced_at
  ON public.sync_log(company_id, synced_at DESC);
