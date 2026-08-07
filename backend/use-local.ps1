# Points this shell's backend at the local Supabase stack instead of the cloud.
#
#   DOT-SOURCE IT. Do not just run it:
#       . .\use-local.ps1 ; npm run dev
#   Running it as `.\use-local.ps1` sets the variables in a child scope that dies
#   the instant the script ends, and `npm run dev` then quietly starts against the
#   cloud with every health check passing.
#
# Nothing here edits backend\.env. dotenv.config() does not overwrite variables
# already present in the process environment, so these exports win for this shell
# only -- the cloud configuration stays intact and a fresh terminal goes back to
# the cloud with no cleanup step.
#
# ASCII ONLY IN THIS FILE. Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI,
# so a UTF-8 em-dash arrives as the three characters 'a', '€' and '"' -- and that
# trailing double-quote terminates whatever string it is sitting in. The failure
# is a pile of "Unexpected token" / "string is missing the terminator" errors
# pointing at lines that look perfectly fine. Keep every character 7-bit.
#
# ---- THE ONE TRAP IN THIS FILE ---------------------------------------------
#
# There are TWO Supabase clients inside the same backend process, and setting one
# pair leaves the other half of the app silently reading the cloud:
#
#   SUPABASE_URL / SUPABASE_SERVICE_KEY   -> db/supabase.ts, the global client.
#                                            /api/sync/stock, /vouchers,
#                                            /outstanding, /parties, /pnl,
#                                            /balance-sheet -- the React dashboard.
#   SUPABASE_URL_Client / *_SERVICE_KEY_CLIENT -> sync.ts:19, reorderSupabase.
#                                            /api/sync/reorder-levels -- the MRP
#                                            report, and nothing else.
#
# SUPABASE_URL_Client has a CAPITAL C and everything else lowercase. That is read
# literally at sync.ts:20. Spell it SUPABASE_URL_CLIENT and the override does not
# throw and does not log -- `reorderSupabase` just falls back to the global
# client, and the MRP report serves CLOUD data while every health check passes.

$ErrorActionPreference = "Stop"

$envPath = Join-Path $PSScriptRoot "..\local-supabase\.env"
if (-not (Test-Path $envPath)) {
  throw "local-supabase\.env not found. Run: cd ..\local-supabase; node scripts\bootstrap.mjs"
}

# Read the generated values rather than hardcoding them -- the keys are unique per
# machine, so a copy pasted from another install authenticates against nothing.
$local = @{}
Get-Content $envPath | ForEach-Object {
  if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $local[$Matches[1]] = $Matches[2].Trim() }
}

$kongPort = if ($local.KONG_HTTP_PORT) { $local.KONG_HTTP_PORT } else { "54321" }
$url = "http://127.0.0.1:$kongPort"
$key = $local.SERVICE_ROLE_KEY
if (-not $key) {
  throw "SERVICE_ROLE_KEY missing from $envPath. Run: node scripts\bootstrap.mjs"
}

# Both clients, both keys. Set one pair only and half the app still reads cloud.
$env:SUPABASE_URL          = $url
$env:SUPABASE_SERVICE_KEY  = $key
$env:API_KEY               = "localdevkey"

$env:SUPABASE_URL_Client         = $url      # capital C -- see above
$env:SUPABASE_SERVICE_KEY_CLIENT = $key
$env:API_KEY_CLIENT              = "localdevkey"

$env:PORT = if ($local.BACKEND_PORT) { $local.BACKEND_PORT } else { "3001" }

Write-Host ""
Write-Host "backend -> LOCAL Supabase" -ForegroundColor Green
Write-Host "  SUPABASE_URL          $url"
Write-Host "  SUPABASE_URL_Client   $url   (capital C)"
Write-Host "  service key           $($key.Substring(0, 12))...  ($($key.Length) chars)"
Write-Host "  x-api-key             localdevkey   (both API_KEY and API_KEY_CLIENT)"
Write-Host "  PORT                  $($env:PORT)"
Write-Host ""
Write-Host "  FIREBASE_SERVICE_ACCOUNT_B64 is left to backend\.env -- Firebase JWT login"
Write-Host "  is unchanged by this script."
Write-Host ""
Write-Host "  now start it with:  npm run dev" -ForegroundColor Cyan
Write-Host ""
