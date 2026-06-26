-- Direct ingest now runs via PostgREST under the publishable/anon role, whose
-- default statement_timeout is too short for a full voucher chunk (error 57014,
-- "canceling statement due to statement timeout"). Give the ingest RPCs their own
-- generous timeout, applied for the duration of each function call regardless of
-- the caller's role/session setting. (The edge function used the service role,
-- which has no such limit — that's why this only surfaced after the PostgREST cutover.)

DO $$
DECLARE
  r record;
BEGIN
  FOR r IN
    SELECT oid::regprocedure AS sig
    FROM pg_proc
    WHERE pronamespace = 'public'::regnamespace
      AND proname IN (
        'tb_ingest_vouchers',
        'tb_ingest_phase3_hybrid',
        'tb_ingest_phase4_full',
        'tb_ingest_masters',
        'tb_ingest_snapshots'
      )
  LOOP
    EXECUTE format('ALTER FUNCTION %s SET statement_timeout = %L', r.sig, '120s');
  END LOOP;
END $$;
