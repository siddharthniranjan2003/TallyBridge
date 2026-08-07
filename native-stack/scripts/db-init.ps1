# Creates the Postgres cluster and the Supabase role set. Runs once.
#
#   .\scripts\db-init.ps1              create if absent, then start
#   .\scripts\db-init.ps1 -StartOnly   just start an existing cluster
#
# Does NOT need administrator: initdb and `pg_ctl start` run as the current user.
# Only registering the Windows *service* needs elevation, and that is a separate
# step (scripts\services.ps1).
#
# ASCII ONLY -- see the note in fetch-vendor.ps1.

[CmdletBinding()]
param([switch]$StartOnly)

$ErrorActionPreference = "Stop"
$pkg = Split-Path $PSScriptRoot -Parent

$envPath = Join-Path $pkg "config\.env"
if (-not (Test-Path $envPath)) { throw "config\.env not found. Run: node scripts\bootstrap.mjs" }
$e = @{}
Get-Content $envPath | ForEach-Object {
  if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $e[$Matches[1]] = $Matches[2].Trim() }
}

$pgBin = Join-Path $e.VENDOR_DIR "pgsql\bin"
if (-not (Test-Path (Join-Path $pgBin "initdb.exe"))) {
  throw "PostgreSQL binaries not found at $pgBin. Run: .\fetch-vendor.ps1 -IncludePostgres"
}
$env:Path = "$pgBin;$env:Path"

$data = $e.PGDATA
$log  = Join-Path $pkg "logs\postgres.log"
New-Item -ItemType Directory -Path (Split-Path $log -Parent) -Force | Out-Null

function Test-Running {
  & (Join-Path $pgBin "pg_isready.exe") -h 127.0.0.1 -p $e.PGPORT -q 2>$null | Out-Null
  return ($LASTEXITCODE -eq 0)
}

if (-not $StartOnly -and -not (Test-Path (Join-Path $data "PG_VERSION"))) {
  Write-Host "creating cluster at $data ..." -ForegroundColor Cyan
  New-Item -ItemType Directory -Path $data -Force | Out-Null

  # The superuser password goes via a file, not --pwprompt (interactive, would
  # hang a service install) and not on the command line (visible to any user in
  # the process list for as long as initdb runs).
  $pwFile = Join-Path $env:TEMP ("tb-pg-{0}.txt" -f [guid]::NewGuid())
  try {
    Set-Content -Path $pwFile -Value $e.POSTGRES_PASSWORD -NoNewline -Encoding ascii
    # scram-sha-256 for host connections; PostgREST, psql and pg_ctl all speak it.
    & (Join-Path $pgBin "initdb.exe") -D $data -U postgres --pwfile=$pwFile `
        --auth-host=scram-sha-256 --auth-local=trust --encoding=UTF8 `
        --locale=C --lc-messages=C | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "initdb failed" }
  } finally {
    if (Test-Path $pwFile) { Remove-Item $pwFile -Force }
  }

  # Loopback only. The default listen_addresses is already localhost, but state it
  # so a later edit cannot widen it by accident -- this is the client's ledger.
  Add-Content (Join-Path $data "postgresql.conf") @"

# --- TallyBridge ---
listen_addresses = '127.0.0.1'
port = $($e.PGPORT)
"@
  Write-Host "  cluster created" -ForegroundColor Green
}

if (-not (Test-Running)) {
  Write-Host "starting postgres on 127.0.0.1:$($e.PGPORT) ..." -ForegroundColor Cyan
  & (Join-Path $pgBin "pg_ctl.exe") -D $data -l $log -w -o "-p $($e.PGPORT)" start
  if ($LASTEXITCODE -ne 0) { Get-Content $log -Tail 20; throw "pg_ctl start failed" }
}
Write-Host "  postgres up" -ForegroundColor Green

# ---- roles -------------------------------------------------------------------
# The Supabase images create these; a stock PostgreSQL does not, so they are made
# here. PostgREST logs in as `authenticator`, which has NO privileges of its own
# and can only SET ROLE to anon / authenticated / service_role -- that separation
# is what makes the JWT role claim meaningful rather than decorative.
$env:PGPASSWORD = $e.POSTGRES_PASSWORD

# SINGLE-quoted here-string. In a double-quoted @"..."@ PowerShell expands '$',
# and backslash is not its escape character (backtick is), so a dollar-quoted
# plpgsql block written as \$\$ reaches psql literally and dies with
# `invalid command \$`. Nothing is interpolated here; the password is substituted
# afterwards through -replace.
$rolesSql = @'
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    CREATE ROLE anon NOLOGIN NOINHERIT;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    CREATE ROLE authenticated NOLOGIN NOINHERIT;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    CREATE ROLE service_role NOLOGIN NOINHERIT BYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticator') THEN
    CREATE ROLE authenticator LOGIN NOINHERIT;
  END IF;
END
$$;

ALTER ROLE authenticator WITH PASSWORD '__TB_PW__';
GRANT anon, authenticated, service_role TO authenticator;

-- pgcrypto goes in its OWN schema, exactly as Supabase does it, and NOT in public.
--
-- PostgREST exposes every function in an exposed schema as an RPC. Installed into
-- `public` -- which is what backend/full_schema.sql:1 asks for -- pgcrypto puts
-- armor, dearmor, gen_salt, gen_random_uuid, pgp_armor_headers and pgp_key_id on
-- the REST API. Measured: schema-parity reported "rpcs live=13 local=19" and
-- named all six. Cosmetically that is drift; practically it is crypto helpers
-- published on the client's ledger API for no reason.
--
-- full_schema.sql's `CREATE EXTENSION IF NOT EXISTS pgcrypto` then becomes a
-- no-op, because the extension already exists -- schema irrelevant. So the shared
-- SQL needs no edit.
--
-- Safe for gen_random_uuid(): it is a CORE builtin in pg_catalog from PG13 on,
-- not pgcrypto's, so the 14 column defaults in full_schema.sql resolve with
-- `extensions` nowhere near search_path. This is the same arrangement the live
-- project runs.
CREATE SCHEMA IF NOT EXISTS extensions;
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_extension e
    JOIN pg_namespace n ON n.oid = e.extnamespace
    WHERE e.extname = 'pgcrypto' AND n.nspname = 'public'
  ) THEN
    ALTER EXTENSION pgcrypto SET SCHEMA extensions;
  ELSIF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pgcrypto') THEN
    CREATE EXTENSION pgcrypto WITH SCHEMA extensions;
  END IF;
END
$$;

-- An EMPTY publication that nothing publishes to, created only so the schema
-- applies. supabase/postgres ships `supabase_realtime` as part of its image, and
-- backend/supabase_scan_jobs.sql:53 does
--     ALTER PUBLICATION supabase_realtime ADD TABLE scan_jobs;
-- which is a hard ERROR on stock PostgreSQL, aborting the whole schema apply at
-- the second file.
--
-- Creating it is the right fix rather than editing that .sql: the file is shared
-- with the Docker stack and with the live cloud project, and a publication with
-- no subscriber costs nothing. This stack runs no Realtime service, so it stays
-- inert -- it exists purely so one schema definition serves all three.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'supabase_realtime') THEN
    CREATE PUBLICATION supabase_realtime;
  END IF;
END
$$;
'@ -replace '__TB_PW__', $e.POSTGRES_PASSWORD

$tmpSql = Join-Path $env:TEMP ("tb-roles-{0}.sql" -f [guid]::NewGuid())
try {
  Set-Content -Path $tmpSql -Value $rolesSql -Encoding ascii
  & (Join-Path $pgBin "psql.exe") -h 127.0.0.1 -p $e.PGPORT -U postgres -d $e.POSTGRES_DB `
      -v ON_ERROR_STOP=1 --quiet --no-psqlrc -f $tmpSql
  if ($LASTEXITCODE -ne 0) { throw "role setup failed" }
} finally {
  if (Test-Path $tmpSql) { Remove-Item $tmpSql -Force }
  Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
}

Write-Host "  roles ready: anon, authenticated, service_role, authenticator" -ForegroundColor Green
