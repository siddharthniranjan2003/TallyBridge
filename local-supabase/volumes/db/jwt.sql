-- Mounted as /docker-entrypoint-initdb.d/init-scripts/99-tb-jwt.sql.
--
-- Exposes the JWT secret to SQL as app.settings.jwt_secret, the same way the
-- hosted project does. Nothing in TallyBridge's own SQL reads it today, but
-- Supabase's bundled helper functions do, and Studio surfaces an error in the
-- SQL editor when the setting is missing.

\set jwt_secret `echo "$JWT_SECRET"`
\set jwt_exp `echo "$JWT_EXP"`
-- migrate.sh exports PGDATABASE; POSTGRES_DB is not guaranteed to be in scope here.
\set dbname `echo "$PGDATABASE"`

ALTER DATABASE :"dbname" SET "app.settings.jwt_secret" TO :'jwt_secret';
ALTER DATABASE :"dbname" SET "app.settings.jwt_exp" TO :'jwt_exp';
