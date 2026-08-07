# One-shot installer for the TallyBridge local Supabase stack.
#
#   .\install.ps1                                  # install, no data
#   .\install.ps1 -Seed                            # install + copy data from the
#                                                  #   project in backend\.env
#   .\install.ps1 -Seed -LiveUrl https://xxx.supabase.co -LiveKey eyJ...
#   .\install.ps1 -Seed -Profile reports           # reports + stock tables only
#
# Safe to re-run. It generates secrets only if they are missing, applies a schema
# made entirely of idempotent statements, and upserts data rather than replacing
# it. Nothing here drops or truncates anything.
#
# ASCII ONLY IN THIS FILE -- Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI,
# and a UTF-8 dash arrives as three characters ending in a double-quote, which
# terminates whatever string it lands in.

[CmdletBinding()]
param(
  [switch]$Seed,
  [string]$LiveUrl,
  [string]$LiveKey,
  # Shadows PowerShell's automatic $PROFILE inside this script only. Nothing here
  # reads $PROFILE, and the name matches copy-live-to-local.mjs's --profile flag,
  # which is worth more than avoiding the shadow.
  [ValidateSet("reports", "full")]
  [string]$Profile = "full",
  # Load images from .\images\*.tar instead of pulling from Docker Hub. Produced
  # by build-client-package.ps1 -IncludeImages, for a client PC with no usable
  # internet (the images are ~4.9 GB to pull).
  [switch]$OfflineImages
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Say($msg, $color = "White") { Write-Host $msg -ForegroundColor $color }
function Step($n, $msg) { Write-Host ""; Write-Host "[$n] $msg" -ForegroundColor Cyan }

# ---- 0. prerequisites -------------------------------------------------------
Step 0 "Checking prerequisites"

$docker = Get-Command docker -ErrorAction SilentlyContinue
if (-not $docker) {
  throw "docker not found on PATH. Install Docker Desktop and start it, then re-run."
}
# `docker info` is the check that matters. The CLI exists even when the engine is
# stopped, and every later step would then fail with a different, less obvious
# error.
docker info --format '{{.ServerVersion}}' > $null 2>&1
if ($LASTEXITCODE -ne 0) {
  throw "Docker is installed but the engine is not responding. Start Docker Desktop and wait for it to say 'Engine running', then re-run."
}
Say "  docker engine ok" Green

$node = Get-Command node -ErrorAction SilentlyContinue
if (-not $node) {
  throw "node not found on PATH. Node 18+ is required (the install scripts and the TallyBridge backend both need it)."
}
$nodeMajor = [int](((node --version) -replace '^v', '') -split '\.')[0]
if ($nodeMajor -lt 18) { throw "node $((node --version)) is too old; the scripts use fetch() and need Node 18+." }
Say "  node $(node --version) ok" Green

# ---- 1. secrets -------------------------------------------------------------
Step 1 "Generating this machine's secrets"
node scripts\bootstrap.mjs
if ($LASTEXITCODE -ne 0) { throw "bootstrap failed" }

# ---- 2. images --------------------------------------------------------------
if ($OfflineImages) {
  Step 2 "Loading images from .\images"
  $tars = Get-ChildItem -Path (Join-Path $PSScriptRoot "images") -Filter '*.tar' -ErrorAction SilentlyContinue
  if (-not $tars) { throw "no .tar files in .\images -- build the package with -IncludeImages, or drop -OfflineImages to pull from Docker Hub." }
  foreach ($t in $tars) {
    Say "  loading $($t.Name) ..."
    docker load -i $t.FullName | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "docker load failed for $($t.Name)" }
  }
  Say "  images loaded" Green
} else {
  Step 2 "Pulling images (about 4.9 GB on a first install; cached afterwards)"
  docker compose pull
  if ($LASTEXITCODE -ne 0) { throw "docker compose pull failed" }
}

# ---- 3. start ---------------------------------------------------------------
Step 3 "Starting the stack"
docker compose up -d
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed" }

# `docker compose up -d` returns as soon as the containers are created. The db is
# only usable once its healthcheck passes, and on a FIRST boot that includes
# running the image's own init plus our role/jwt scripts, which takes far longer
# than a restart. Applying the schema before then fails with a connection error.
Say "  waiting for the database to accept connections ..."
$deadline = (Get-Date).AddMinutes(5)
$ready = $false
while ((Get-Date) -lt $deadline) {
  $state = (docker inspect --format '{{.State.Health.Status}}' tb_supabase_db 2>$null)
  if ($state -eq 'healthy') { $ready = $true; break }
  if ($state -eq 'unhealthy') {
    docker compose logs --tail 40 db
    throw "database reported unhealthy -- see the log above."
  }
  Start-Sleep -Seconds 3
}
if (-not $ready) {
  docker compose logs --tail 40 db
  throw "database did not become healthy within 5 minutes -- see the log above."
}
Say "  database ready" Green

# ---- 4. schema --------------------------------------------------------------
Step 4 "Applying the schema"
node scripts\apply-schema.mjs
if ($LASTEXITCODE -ne 0) { throw "schema apply failed" }

# ---- 5. data ----------------------------------------------------------------
if ($Seed) {
  Step 5 "Copying data"
  $copyArgs = @()
  if ($Profile -eq "reports") { $copyArgs += @("--profile", "reports") }
  if ($LiveUrl) { $env:LIVE_SUPABASE_URL = $LiveUrl }
  if ($LiveKey) { $env:LIVE_SUPABASE_KEY = $LiveKey }
  node scripts\copy-live-to-local.mjs @copyArgs
  if ($LASTEXITCODE -ne 0) { throw "data copy failed" }
} else {
  Step 5 "Skipping data copy (pass -Seed to copy from an existing project)"
}

# ---- 6. verify --------------------------------------------------------------
Step 6 "Health check"
node scripts\healthcheck.mjs
$healthExit = $LASTEXITCODE

# ---- summary ----------------------------------------------------------------
$envFile = @{}
Get-Content (Join-Path $PSScriptRoot ".env") | ForEach-Object {
  if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $envFile[$Matches[1]] = $Matches[2].Trim() }
}

Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host " TallyBridge local Supabase is installed" -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  API (PostgREST via Kong)  http://127.0.0.1:$($envFile.KONG_HTTP_PORT)"
Write-Host "  Studio (the web UI)       http://127.0.0.1:$($envFile.STUDIO_PORT)"
Write-Host "  Postgres                  127.0.0.1:$($envFile.POSTGRES_PORT)  user postgres"
Write-Host ""
Write-Host "  All three are bound to 127.0.0.1 and are not reachable from the network."
Write-Host "  Keys and passwords are unique to this machine and live in .\.env"
Write-Host ""
Write-Host "  Next:" -ForegroundColor Cyan
Write-Host "    backend       cd ..\backend ; . .\use-local.ps1 ; npm run dev"
Write-Host "    desktop app   .\scripts\use-local-desktop.ps1"
Write-Host "    re-check      node scripts\healthcheck.mjs"
Write-Host ""

exit $healthExit
