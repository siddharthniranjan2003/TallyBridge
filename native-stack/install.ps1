# One-shot installer for the TallyBridge native stack. No Docker.
#
#   .\install.ps1                                    install, no data
#   .\install.ps1 -Seed                              + copy data from backend\.env's project
#   .\install.ps1 -Seed -LiveUrl https://xxx.supabase.co -LiveKey eyJ... -Profile reports
#   .\install.ps1 -NoServices                        skip service registration (no admin needed)
#
# Run it from an ELEVATED PowerShell. Only the last step needs admin -- registering
# the three Windows services, which is the whole point: they start with the
# machine, before anyone logs in. Without elevation everything else still
# completes and the stack runs as ordinary processes via .\start.ps1.
#
# Safe to re-run: secrets are generated only if missing, every SQL statement is
# idempotent, and the data copy upserts. Nothing here drops or truncates.
#
# ASCII ONLY -- see the note in fetch-vendor.ps1.

[CmdletBinding()]
param(
  [switch]$Seed,
  [string]$LiveUrl,
  [string]$LiveKey,
  # Shadows PowerShell's automatic $PROFILE inside this script only; nothing here
  # reads it, and the name matches copy-live-to-local.mjs's --profile flag.
  [ValidateSet("reports", "full")]
  [string]$Profile = "reports",
  [string]$DataDir,
  [switch]$NoServices,
  [switch]$SkipVendor
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$repo = Resolve-Path (Join-Path $PSScriptRoot "..")

function Step($n, $msg) { Write-Host ""; Write-Host "[$n] $msg" -ForegroundColor Cyan }
function Say($msg) { Write-Host "  $msg" -ForegroundColor Green }

# ---- 0. prerequisites -------------------------------------------------------
Step 0 "Checking prerequisites"
$node = Get-Command node -ErrorAction SilentlyContinue
if (-not $node) { throw "node not found on PATH. Node 18+ is required (install scripts and the TallyBridge backend both need it)." }
$nodeMajor = [int](((node --version) -replace '^v', '') -split '\.')[0]
if ($nodeMajor -lt 18) { throw "node $((node --version)) is too old; the scripts use fetch() and need Node 18+." }
Say "node $(node --version)"

$isAdmin = (New-Object Security.Principal.WindowsPrincipal(
  [Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) { Say "running elevated -- services can be registered" }
else { Write-Host "  NOT elevated -- will install everything except the Windows services" -ForegroundColor Yellow }

# ---- 1. binaries ------------------------------------------------------------
if ($SkipVendor) {
  Step 1 "Skipping vendor fetch (-SkipVendor)"
} else {
  Step 1 "Fetching binaries into .\vendor"
  .\fetch-vendor.ps1 -IncludePostgres
}

# ---- 2. secrets and config --------------------------------------------------
Step 2 "Generating this machine's secrets and config"
$bootstrapArgs = @()
if ($DataDir) { $bootstrapArgs += @("--data-dir", $DataDir) }
node scripts\bootstrap.mjs @bootstrapArgs
if ($LASTEXITCODE -ne 0) { throw "bootstrap failed" }

# ---- 3. database ------------------------------------------------------------
Step 3 "Creating the database cluster and roles"
.\scripts\db-init.ps1

# ---- 4. schema --------------------------------------------------------------
Step 4 "Applying the schema"
node scripts\apply-schema.mjs
if ($LASTEXITCODE -ne 0) { throw "schema apply failed" }

# ---- 5. services (or processes) ---------------------------------------------
if ($NoServices -or -not $isAdmin) {
  Step 5 "Starting as processes (services skipped)"
  .\start.ps1
} else {
  Step 5 "Registering Windows services"
  .\scripts\services.ps1 -Install
  .\start.ps1
}

# PostgREST caches the schema on connect. It was started after the schema was
# applied above, so it is current -- but say so, because a reader who reorders
# these steps will get 404s on every table and no clue why.
Start-Sleep -Seconds 3

# ---- 6. data ----------------------------------------------------------------
if ($Seed) {
  Step 6 "Copying data"
  $env:TB_ENV_FILE = Join-Path $PSScriptRoot "config\.env"
  if ($LiveUrl) { $env:LIVE_SUPABASE_URL = $LiveUrl }
  if ($LiveKey) { $env:LIVE_SUPABASE_KEY = $LiveKey }
  $copyScript = Join-Path $repo "local-stack-shared\scripts\copy-live-to-local.mjs"
  if (-not (Test-Path $copyScript)) { $copyScript = Join-Path $PSScriptRoot "scripts\copy-live-to-local.mjs" }
  node $copyScript --profile $Profile
  if ($LASTEXITCODE -ne 0) { throw "data copy failed" }
} else {
  Step 6 "Skipping data copy (pass -Seed to copy from an existing project)"
}

# ---- 7. verify --------------------------------------------------------------
Step 7 "Health check"
node scripts\healthcheck.mjs
$healthExit = $LASTEXITCODE

$e = @{}
Get-Content (Join-Path $PSScriptRoot "config\.env") | ForEach-Object {
  if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $e[$Matches[1]] = $Matches[2].Trim() }
}

Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host " TallyBridge native stack installed -- no Docker" -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  API (PostgREST via Caddy)  http://127.0.0.1:$($e.GATEWAY_PORT)"
Write-Host "  PostgreSQL                 127.0.0.1:$($e.PGPORT)   user postgres"
Write-Host "  Data directory             $($e.PGDATA)"
Write-Host ""
Write-Host "  Both bind to 127.0.0.1 and are unreachable from the network."
Write-Host "  Keys and passwords are unique to this machine, in .\config\.env"
Write-Host ""
if (-not $isAdmin -and -not $NoServices) {
  Write-Host "  SERVICES NOT REGISTERED -- the stack is tied to this login session." -ForegroundColor Yellow
  Write-Host "  Re-run from an elevated PowerShell, or: .\scripts\services.ps1 -Install" -ForegroundColor Yellow
  Write-Host ""
}
Write-Host "  Point the backend at it:" -ForegroundColor Cyan
Write-Host "    cd ..\backend ; . .\use-native.ps1 ; npm run dev"
Write-Host ""

exit $healthExit
