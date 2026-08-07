-- Mounted as /docker-entrypoint-initdb.d/init-scripts/99-tb-roles.sql.
-- Runs once, on first boot of an empty data volume, after the image's own
-- init-scripts have created the Supabase role set.
--
-- The image creates the roles but leaves them without the password this install
-- generated, so PostgREST cannot log in as `authenticator` and postgres-meta
-- cannot log in as `supabase_admin`.
--
-- Symptom when this does not run: every /rest/v1 request returns
-- {"message":"Database connection error"} and the rest container log repeats
-- `password authentication failed for user "authenticator"`.

\set pgpass `echo "$POSTGRES_PASSWORD"`

-- Stash the password in a session GUC rather than interpolating it into the DO
-- block below: psql does NOT substitute :variables inside dollar-quoted strings,
-- so :'pgpass' there would be written to the role as the literal text.
SELECT set_config('tb.pgpass', :'pgpass', false);

-- Skip roles the image did not create, instead of listing them and hoping. The
-- exact role set moves between supabase/postgres versions (pgbouncer and
-- supabase_functions_admin are not in every build), and because migrate.sh runs
-- these files with ON_ERROR_STOP=1, one missing role would otherwise abort the
-- entire database init and leave a stack that never comes up.
DO $$
DECLARE
  target text;
  pw text := current_setting('tb.pgpass');
BEGIN
  FOREACH target IN ARRAY ARRAY[
    'authenticator',
    'supabase_admin',
    'supabase_auth_admin',
    'supabase_storage_admin',
    'supabase_functions_admin',
    'pgbouncer'
  ] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = target) THEN
      EXECUTE format('ALTER USER %I WITH PASSWORD %L', target, pw);
      RAISE NOTICE 'tb: password set for role %', target;
    ELSE
      RAISE NOTICE 'tb: role % not present in this image, skipped', target;
    END IF;
  END LOOP;
END $$;

SELECT set_config('tb.pgpass', '', false);
