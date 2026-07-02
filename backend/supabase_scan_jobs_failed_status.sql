-- ============================================================
-- TallyBridge — scan_jobs: "garbage invoice" (failed scan) support
-- Run in your Supabase SQL Editor (Dashboard → SQL Editor).
-- Apply to EVERY project the app reads from:
--   yynuuysvjeipawzfbeme  (testing / prod flavors)
--   ztugwhevemibdrzqafyw  (client production)
-- Idempotent — safe to re-run.
-- ============================================================
--
-- Why these columns
-- -----------------
-- A scan that FAILS to parse into a voucher (no party/stock match, unreadable,
-- or backend ingest down) used to leave its scan_jobs row to silently expire at
-- the 300s badge TTL — the scanned invoice vanished with no user signal.
--
-- Instead of orphaning the row, the parsing service now marks it:
--   status     'failed'  (was implicitly 'processing')
--   reason     the failure reason (for support/debugging)
--   page_count how many scanned page images were stored (in GCS, keyed by this
--              row id) so the app's "Garbage invoice" sheet knows how many to show
--
-- The app surfaces failed rows as a "Garbage invoice" entry the user can open
-- (image-only bottom sheet) and delete. Successful scans are unchanged: the
-- parser still DELETEs the row, draining the badge.
--
-- Additive only: existing rows default to status='processing', page_count=0.
-- No REPLICA IDENTITY change is needed — the app reads the full new row on the
-- realtime UPDATE (replica identity only affects the OLD record).

ALTER TABLE public.scan_jobs
  ADD COLUMN IF NOT EXISTS status     text    NOT NULL DEFAULT 'processing',
  ADD COLUMN IF NOT EXISTS reason     text,
  ADD COLUMN IF NOT EXISTS page_count integer NOT NULL DEFAULT 0;

-- Constrain status to the known values (guarded so re-runs don't error).
DO $$
BEGIN
  ALTER TABLE public.scan_jobs
    ADD CONSTRAINT scan_jobs_status_chk CHECK (status IN ('processing', 'failed'));
EXCEPTION
  WHEN duplicate_object THEN NULL;
END
$$;
