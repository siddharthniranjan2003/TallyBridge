# Points this shell's backend at the NATIVE stack (Postgres + PostgREST + Caddy,
# no Docker) instead of the cloud.
#
#   DOT-SOURCE IT. Do not just run it:
#       . .\use-native.ps1 ; npm run dev
#   Running it as `.\use-native.ps1` sets the variables in a child scope that dies
#   the instant the script ends, and `npm run dev` then quietly starts against the
#   cloud with every health check passing.
#
# The sibling script use-local.ps1 does the same for the DOCKER stack. The only
# difference between them is the port -- Caddy on 8000 versus Kong on 54321 --
# because both gateways serve the identical /rest/v1 surface. That is the whole
# design: SUPABASE_URL is the one thing that changes, and no application code
# knows which stack it is talking to.
#
# ASCII ONLY IN THIS FILE. Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI,
# so a UTF-8 em-dash arrives as three characters ending in a double-quote, which
# terminates whatever string it is sitting in.
#
# ---- THE ONE TRAP ----------------------------------------------------------
#
# There are TWO Supabase clients inside the same backend process, and setting one
# pair leaves the other half of the app silently reading the cloud:
#
#   SUPABASE_URL / SUPABASE_SERVICE_KEY        -> db/supabase.ts, the global
#       client: /api/sync/stock, /vouchers, /outstanding, /parties, /pnl,
#       /balance-sheet -- the React dashboard.
#   SUPABASE_URL_Client / *_SERVICE_KEY_CLIENT -> sync.ts:19, reorderSupabase:
#       /api/sync/reorder-levels -- the MRP report, and nothing else.
#
# SUPABASE_URL_Client has a CAPITAL C and everything else lowercase. That is read
# literally at sync.ts:20. Spell it SUPABASE_URL_CLIENT and the override does not
# throw and does not log -- reorderSupabase falls back to the global client, and
# the MRP report serves CLOUD data while every health check passes.

$ErrorActionPreference = "Stop"

$envPath = Join-Path $PSScriptRoot "..\native-stack\config\.env"
if (-not (Test-Path $envPath)) {
  throw "native-stack\config\.env not found. Run: cd ..\native-stack; .\install.ps1"
}

# Read the generated values rather than hardcoding them -- the keys are unique per
# machine, so a copy pasted from another install authenticates against nothing.
$n = @{}
Get-Content $envPath | ForEach-Object {
  if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $n[$Matches[1]] = $Matches[2].Trim() }
}

$port = if ($n.GATEWAY_PORT) { $n.GATEWAY_PORT } else { "8000" }
$url = "http://127.0.0.1:$port"
$key = $n.SERVICE_ROLE_KEY
if (-not $key) { throw "SERVICE_ROLE_KEY missing from $envPath. Run: node scripts\bootstrap.mjs" }

# Both clients, both keys. Set one pair only and half the app still reads cloud.
$env:SUPABASE_URL          = $url
$env:SUPABASE_SERVICE_KEY  = $key
$env:API_KEY               = "localdevkey"

$env:SUPABASE_URL_Client         = $url      # capital C -- see above
$env:SUPABASE_SERVICE_KEY_CLIENT = $key
$env:API_KEY_CLIENT              = "localdevkey"

$env:PORT = if ($n.BACKEND_PORT) { $n.BACKEND_PORT } else { "3001" }

Write-Host ""
Write-Host "backend -> NATIVE stack (no Docker)" -ForegroundColor Green
Write-Host "  SUPABASE_URL          $url   (Caddy)"
Write-Host "  SUPABASE_URL_Client   $url   (capital C)"
Write-Host "  service key           $($key.Substring(0, 12))...  ($($key.Length) chars)"
Write-Host "  x-api-key             localdevkey   (both API_KEY and API_KEY_CLIENT)"
Write-Host "  PORT                  $($env:PORT)"
Write-Host ""
Write-Host "  now start it with:  npm run dev" -ForegroundColor Cyan
Write-Host ""
