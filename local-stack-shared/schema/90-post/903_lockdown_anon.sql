-- Take every table privilege away from `anon`.
--
-- ── The hole this closes ─────────────────────────────────────────────────────
--
-- 902_grants.sql grants ALL on every table to anon, authenticated and
-- service_role, and no table enables RLS. On the Docker stack that is survivable
-- only because Kong sits in front of PostgREST and rejects any request without a
-- registered key (verified: 401). Kong is the sole thing standing between a
-- request and the whole ledger.
--
-- The native Windows stack has no Kong. Caddy rewrites /rest/v1 and proxies; it
-- does not authenticate. Measured on this machine before this file existed:
--
--     GET http://127.0.0.1:3000/stock_items?select=name   (no headers at all)
--     -> 200  [{"name":"SOLID CARBIDE DRILL 8.6 YG"}, ...]
--
-- and `has_table_privilege('anon','stock_items','DELETE')` was true. So anything
-- that could reach the port could read and delete the client's books.
--
-- ── Why revoking is safe here ────────────────────────────────────────────────
--
-- Nothing in this deployment authenticates as anon. The Express backend uses the
-- service_role key (both of its clients), and the Python engine sends the same
-- key as SYNC_INGEST_KEY. The anon key is issued and left unused.
--
-- PostgREST still needs the ROLE to exist and `authenticator` must still be able
-- to SET ROLE to it — that is a role membership grant, not a table privilege, and
-- is untouched below. Unauthenticated requests now get a permission error from
-- Postgres instead of data.
--
-- ── If Sale/Purchase ever come back ──────────────────────────────────────────
--
-- The Flutter app talks to Supabase directly with the anon key
-- (history_screen.dart:75, queue_screen.dart:190, stock_info_screen.dart:138).
-- Those paths are out of scope while scanning is paused, and they run against the
-- CLOUD project, not this one. If they are ever pointed here, the answer is RLS
-- policies plus targeted grants — not re-granting ALL to anon.
--
-- Idempotent: REVOKE on an already-revoked privilege is a no-op.

REVOKE ALL ON ALL TABLES    IN SCHEMA public FROM anon;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM anon;

-- 902 set default privileges for anon as well, so new tables would silently get
-- the grant back. Cancel that too, or the next migration re-opens the hole.
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES    FROM anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM anon;

-- USAGE on the schema itself stays: without it PostgREST cannot even build its
-- schema cache for the anon role, and it fails to start rather than serving 401s.
GRANT USAGE ON SCHEMA public TO anon;

NOTIFY pgrst, 'reload schema';
