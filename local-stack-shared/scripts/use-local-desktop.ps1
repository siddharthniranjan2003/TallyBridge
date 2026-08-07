# Points the TallyBridge desktop app (Electron + Python engine) at the local
# stack, by patching its electron-store config in place.
#
#   .\scripts\use-local-desktop.ps1              # switch to local
#   .\scripts\use-local-desktop.ps1 -Show        # print current target, change nothing
#   .\scripts\use-local-desktop.ps1 -Restore     # put the previous config back
#
# Unlike backend\use-local.ps1 this is NOT shell-scoped -- the desktop app reads a
# JSON file on disk, so the change persists until it is restored. The existing
# config is copied to tallybridge-config.cloud-backup.json before anything is
# written, because that file may hold the client's real cloud credentials and
# there is no other copy of them.
#
# ASCII ONLY IN THIS FILE. Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI,
# so a UTF-8 em-dash arrives as three characters ending in a double-quote, which
# terminates whatever string it sits in and produces a cascade of parser errors
# on lines that look fine. Keep every character 7-bit.
#
# ---- What actually has to change, and why -----------------------------------
#
# The desktop app has TWO paths to the database and they are configured
# separately (src/main/sync-engine.ts:445-468):
#
#   controlPlaneUrl / controlPlaneApiKey
#       -> BACKEND_URL / API_KEY for the Python engine. Used by `render` ingest
#          mode and by every control-plane call (run tracking, push queue).
#          Points at the Express backend, which is itself pointed at local
#          Supabase by backend\use-local.ps1.
#
#   syncIngestUrl / syncIngestKey
#       -> SYNC_INGEST_URL / SYNC_INGEST_KEY. Used by `hybrid` and `direct` ingest
#          modes, which write to PostgREST without going through the backend.
#          syncIngestMode defaults to "hybrid" (store.ts:78), so on a default
#          install this path IS live, and leaving it on the cloud means masters
#          keep syncing to the cloud while the reports read local.
#
# ---- The /functions/ trap in syncIngestUrl ----------------------------------
#
# syncIngestUrl must keep the edge-function shape even though this stack runs no
# edge functions. cloud_pusher.py:224 derives the PostgREST base like this:
#
#     if SYNC_INGEST_URL and "/functions/" in SYNC_INGEST_URL:
#         return SYNC_INGEST_URL.split("/functions/", 1)[0] + "/rest/v1"
#     return ""
#
# Set it to the bare REST URL and that returns "" -- direct ingest then fails with
# "Direct ingest needs SYNC_INGEST_URL to derive the Supabase REST endpoint"
# (cloud_pusher.py:319), which reads like a missing setting rather than a
# wrongly-shaped one. So we write .../functions/v1/ingest-sync and let the engine
# strip it back to .../rest/v1. Nothing ever requests the /functions/ path itself.

[CmdletBinding()]
param(
  [switch]$Show,
  [switch]$Restore,
  # Which local stack to point at. Both serve the identical /rest/v1 surface;
  # only the gateway port differs (Kong 54321 vs Caddy 8000).
  [ValidateSet("docker", "native")]
  [string]$Stack = "docker",
  [ValidateSet("render", "hybrid", "direct")]
  [string]$IngestMode,
  [string]$BackendUrl = "http://localhost:3001",
  [string]$ApiKey = "localdevkey"
)

$ErrorActionPreference = "Stop"

# electron-store writes <userData>\<name>.json. name = "tallybridge-config"
# (store.ts:70); userData is %APPDATA%\<app name>, which is "tallybridge" in dev
# and "TallyBridge" in a packaged build -- the same folder on Windows.
$configDir  = Join-Path $env:APPDATA "tallybridge"
$configPath = Join-Path $configDir "tallybridge-config.json"
$backupPath = Join-Path $configDir "tallybridge-config.cloud-backup.json"

# Candidates in order: the repo layout for the chosen stack, then a packaged
# stack sitting one level up from this script (both packagers flatten it into
# their own scripts\ directory).
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..") -ErrorAction SilentlyContinue
$candidates = @()
if ($repo) {
  $candidates += if ($Stack -eq "native") {
    Join-Path $repo "native-stack\config\.env"
  } else {
    Join-Path $repo "local-supabase\.env"
  }
}
$candidates += (Join-Path $PSScriptRoot "..\config\.env"), (Join-Path $PSScriptRoot "..\.env")

$envPath = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $envPath) {
  throw "No stack .env found for -Stack $Stack. Looked at: $($candidates -join '; ')"
}

$localEnv = @{}
Get-Content $envPath | ForEach-Object {
  if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $localEnv[$Matches[1]] = $Matches[2].Trim() }
}

# GATEWAY_PORT is the native stack's Caddy; KONG_HTTP_PORT is the Docker stack's
# Kong. Whichever is present is the thing serving /rest/v1.
$gwPort = if ($localEnv.GATEWAY_PORT) { $localEnv.GATEWAY_PORT }
          elseif ($localEnv.KONG_HTTP_PORT) { $localEnv.KONG_HTTP_PORT }
          else { "54321" }
$supabaseUrl = "http://127.0.0.1:$gwPort"
Write-Host "using $Stack stack config: $envPath" -ForegroundColor DarkGray

function Show-Target {
  if (-not (Test-Path $configPath)) {
    Write-Host "no config yet at $configPath" -ForegroundColor Yellow
    Write-Host "(the app writes it on first save; run this again afterwards)"
    return
  }
  $c = Get-Content $configPath -Raw | ConvertFrom-Json
  Write-Host ""
  Write-Host "  config          $configPath"
  Write-Host "  backendUrl      $($c.backendUrl)"
  Write-Host "  controlPlaneUrl $($c.controlPlaneUrl)"
  Write-Host "  syncIngestMode  $($c.syncIngestMode)"
  Write-Host "  syncIngestUrl   $($c.syncIngestUrl)"
  Write-Host "  companies       $(@($c.companies).Count)"
  Write-Host ""
}

if ($Show) { Show-Target; return }

if ($Restore) {
  if (-not (Test-Path $backupPath)) { throw "no backup at $backupPath" }
  Copy-Item $backupPath $configPath -Force
  Write-Host "restored from $backupPath" -ForegroundColor Green
  Show-Target
  return
}

if (-not (Test-Path $configPath)) {
  throw "No config at $configPath. The app has not saved settings yet, and this script will not invent one: it would have no companies in it and the app would come up looking freshly installed. Start TallyBridge, save settings once, then re-run this."
}

# Back up before the first local switch only. Re-running must not overwrite the
# cloud backup with an already-local config -- that would destroy the only copy of
# the cloud credentials and make -Restore a no-op.
if (-not (Test-Path $backupPath)) {
  Copy-Item $configPath $backupPath -Force
  Write-Host "backed up cloud config -> $backupPath" -ForegroundColor Yellow
} else {
  Write-Host "cloud backup already exists, left as-is: $backupPath" -ForegroundColor DarkGray
}

$config = Get-Content $configPath -Raw | ConvertFrom-Json

function Set-Field($obj, $name, $value) {
  if ($obj.PSObject.Properties.Name -contains $name) { $obj.$name = $value }
  else { $obj | Add-Member -NotePropertyName $name -NotePropertyValue $value }
}

Set-Field $config "backendUrl"          $BackendUrl
Set-Field $config "apiKey"              $ApiKey
Set-Field $config "controlPlaneUrl"     $BackendUrl
Set-Field $config "controlPlaneApiKey"  $ApiKey
# See the /functions/ note in the header -- this shape is required, not cosmetic.
Set-Field $config "syncIngestUrl"       "$supabaseUrl/functions/v1/ingest-sync"
Set-Field $config "syncIngestKey"       $localEnv.SERVICE_ROLE_KEY
if ($IngestMode) { Set-Field $config "syncIngestMode" $IngestMode }

$config | ConvertTo-Json -Depth 20 | Set-Content $configPath -Encoding utf8

Write-Host "desktop app -> LOCAL stack" -ForegroundColor Green
Show-Target
Write-Host "  Restart TallyBridge for this to take effect (config is read at spawn time)." -ForegroundColor Cyan
Write-Host "  Revert with: .\scripts\use-local-desktop.ps1 -Restore" -ForegroundColor Cyan
Write-Host ""
