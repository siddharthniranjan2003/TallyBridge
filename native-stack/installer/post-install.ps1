# Runs once, from inside the installer, elevated. Does the entire setup.
#
# Kept as a script rather than a chain of Inno [Run] entries so the exact same
# sequence can be run by hand on a machine that is already installed:
#
#   powershell -ExecutionPolicy Bypass -File post-install.ps1 -InstallDir "C:\Program Files\TallyBridge Server" -DataDir "C:\ProgramData\TallyBridge\data"
#
# Every step is idempotent, so re-running it is a repair, not a reinstall.
#
# ASCII ONLY -- Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI, so a UTF-8
# dash arrives as three characters ending in a double-quote.

[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$InstallDir,
  [Parameter(Mandatory = $true)][string]$DataDir,
  [string]$LiveUrl = "",
  [string]$LiveKey = "",
  # Skip service registration and run the same binaries as processes instead.
  # Used to exercise the full install sequence without administrator, which is
  # how the staged payload gets tested before it is compiled into the exe.
  [switch]$NoServices
)

$ErrorActionPreference = "Stop"

$log = Join-Path $InstallDir "logs\install.log"
New-Item -ItemType Directory -Path (Split-Path $log -Parent) -Force | Out-Null
Start-Transcript -Path $log -Append | Out-Null

function Step($msg) { Write-Host ""; Write-Host "=== $msg ===" }

try {
  Set-Location $InstallDir
  $node = Join-Path $InstallDir "vendor\node\node.exe"
  if (-not (Test-Path $node)) { throw "bundled node.exe missing at $node" }

  Step "Generating this machine's secrets and config"
  # --backend-dir is what makes bootstrap emit the fourth service definition.
  & $node "scripts\bootstrap.mjs" --data-dir $DataDir --backend-dir (Join-Path $InstallDir "backend")
  if ($LASTEXITCODE -ne 0) { throw "bootstrap failed" }

  Step "Creating the database cluster"
  & (Join-Path $InstallDir "scripts\db-init.ps1")

  Step "Applying the schema"
  & $node "scripts\apply-schema.mjs"
  if ($LASTEXITCODE -ne 0) { throw "schema apply failed" }

  if ($NoServices) {
    Step "Skipping service registration (-NoServices)"
  } else {
    Step "Registering Windows services"
    & (Join-Path $InstallDir "scripts\services.ps1") -Install
  }

  Step "Starting"
  # start.ps1 picks service mode automatically when the services exist, and falls
  # back to processes when they do not, so it needs no flag here.
  & (Join-Path $InstallDir "start.ps1")

  # PostgREST caches the schema on connect, and the services were started after
  # the schema was applied, so it is current. Give them a moment to bind before
  # anything talks to them.
  Start-Sleep -Seconds 6

  if ($LiveUrl -and $LiveKey) {
    Step "Copying data from $LiveUrl"
    $env:TB_ENV_FILE = Join-Path $InstallDir "config\.env"
    $env:LIVE_SUPABASE_URL = $LiveUrl
    $env:LIVE_SUPABASE_KEY = $LiveKey
    # 'reports' profile: the tables the MRP and stock endpoints read. The
    # Sale/Purchase tables (push_queue, Purchase_Matching, Audit_Trail_Purchase,
    # scan_jobs) are created but left empty -- nothing on this machine writes to
    # them, because the parsing service and push poller are not installed.
    & $node "scripts\copy-live-to-local.mjs" --profile reports
    if ($LASTEXITCODE -ne 0) { throw "data copy failed" }
  } else {
    Step "No project details given -- installing an empty database"
  }

  Step "Verifying"
  # --allow-empty when nothing was copied: an empty database is the correct
  # outcome of an install that was given no project, and without this the
  # installer ends a good install by reporting seven failures.
  $hcArgs = @("scripts\healthcheck.mjs")
  if (-not ($LiveUrl -and $LiveKey)) { $hcArgs += "--allow-empty" }
  & $node @hcArgs
  $health = $LASTEXITCODE

  Write-Host ""
  if ($health -eq 0) { Write-Host "INSTALL OK" } else { Write-Host "INSTALL COMPLETED WITH FAILURES -- see above" }
  Write-Host "  data directory: $DataDir"
  Write-Host "  log:            $log"
  exit $health
}
catch {
  Write-Host ""
  Write-Host "INSTALL FAILED: $($_.Exception.Message)"
  Write-Host $_.ScriptStackTrace
  exit 1
}
finally {
  Stop-Transcript | Out-Null
}
