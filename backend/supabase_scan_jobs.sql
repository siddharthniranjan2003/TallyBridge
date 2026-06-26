-- ============================================================
-- TallyBridge — scan_jobs (shared "scan in-flight" signal)
-- Run in your Supabase SQL Editor (Dashboard → SQL Editor).
-- Apply to every project the app reads from:
--   yynuuysvjeipawzfbeme  (testing / prod flavors)
--   ztugwhevemibdrzqafyw  (deployment flavor)
-- ============================================================
--
-- Why this table exists
-- ---------------------
-- The queue's "Processing…" badge must climb the instant a scan is SENT and
-- drain when its parsed invoice lands — and it must do so on EVERY logged-in
-- client (the scanning phone AND any open web session), not just the device
-- that did the scan. "Scan sent" has no existing row to observe (the push_queue
-- invoice row only appears ~105s later, when parsing finishes), so we broadcast
-- it through this tiny table over Supabase realtime.
--
-- Lifecycle
-- ---------
--   +1  Mobile INSERTs one row the moment a scan is sent. The row id IS the
--       job_id; mobile passes it to the parsing service as ?job_id=<id>. The
--       realtime INSERT echo makes the badge climb on every client.
--   -1  The parsing service DELETEs the row by id (service key, bypasses RLS)
--       right after it inserts the matching push_queue invoice row. The realtime
--       DELETE echo drains the badge on every client. No client decrements.
--  TTL  Clients ignore rows older than 300s, so a missed DELETE can't wedge it.
--
-- Single-tenant per Supabase project (same model as push_queue, whose RLS is a
-- single all-roles policy and whose client reads are unscoped), so no company_id
-- is needed here. Add company_id scoping if a project ever serves >1 company.

CREATE TABLE IF NOT EXISTS scan_jobs (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  type       TEXT NOT NULL CHECK (type IN ('sale', 'purchase')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_scan_jobs_created_at
  ON scan_jobs(created_at DESC);

ALTER TABLE scan_jobs ENABLE ROW LEVEL SECURITY;

-- Mirror push_queue's permissive single-policy access: the anon client (mobile +
-- web) reads/inserts; the service-keyed parsing service deletes. One policy, all
-- roles — consistent with the existing push_queue policy.
DROP POLICY IF EXISTS "scan_jobs full access" ON scan_jobs;
CREATE POLICY "scan_jobs full access" ON scan_jobs
  FOR ALL USING (true) WITH CHECK (true);

-- Realtime so every client's badge climbs/drains live. (No-op if already added.)
DO $$
BEGIN
  ALTER PUBLICATION supabase_realtime ADD TABLE scan_jobs;
EXCEPTION
  WHEN duplicate_object THEN NULL;
END
$$;
