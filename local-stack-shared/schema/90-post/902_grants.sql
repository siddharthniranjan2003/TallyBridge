-- Grants, then a PostgREST schema-cache reload.
--
-- The supabase/postgres image sets default privileges for anon/authenticated/
-- service_role, but only for objects created by the role that ran ALTER DEFAULT
-- PRIVILEGES. Our schema is applied over psql as `postgres`, and views and
-- functions added later by hand would miss out entirely. Granting explicitly at
-- the end of every apply removes the whole class of "table exists in Studio but
-- PostgREST 404s / permission denied" problems.
--
-- NOTE ON RLS: no table here enables row-level security, which mirrors both
-- backend/full_schema.sql and the live project. It is defensible only because of
-- how this stack is deployed — every port binds to 127.0.0.1, and Kong requires a
-- valid anon or service key before a request reaches PostgREST. If this stack is
-- ever exposed beyond loopback, RLS becomes mandatory, because `anon` can read
-- every table as written.

GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;

GRANT ALL ON ALL TABLES    IN SCHEMA public TO anon, authenticated, service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO anon, authenticated, service_role;
GRANT ALL ON ALL FUNCTIONS IN SCHEMA public TO anon, authenticated, service_role;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT ALL ON TABLES    TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT ALL ON SEQUENCES TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT ALL ON FUNCTIONS TO anon, authenticated, service_role;

-- PostgREST caches the schema on connect. Without this, a table created seconds
-- ago answers 404 until the container restarts.
NOTIFY pgrst, 'reload schema';
