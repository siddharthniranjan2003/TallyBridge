# Builds ONE setup.exe containing the whole on-premise server.
#
#   .\build-installer.ps1
#   .\build-installer.ps1 -Version 1.2.0
#
# Stages the payload, then compiles it with Inno Setup. The staging is where the
# size comes from: PostgreSQL is trimmed 848 MB -> 117 MB, and the backend ships
# built with production dependencies only.
#
# Prerequisites on the BUILD machine (not the client's):
#   * Inno Setup 6      winget install JRSoftware.InnoSetup
#   * Node + npm        to build the backend
#   * .\fetch-vendor.ps1 -IncludePostgres   then   .\scripts\trim-vendor.ps1
#
# ASCII ONLY -- see the note in fetch-vendor.ps1.

[CmdletBinding()]
param(
  [string]$Version = "1.0.0",
  [string]$OutDir = (Join-Path $PSScriptRoot "..\dist-client"),
  [switch]$KeepStaging,
  # Stage the payload and stop, without the several-minute LZMA2 compress. The
  # staged tree is byte-for-byte what ends up inside the exe, so it is what to
  # point a test install at.
  [switch]$StageOnly
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$repo = Resolve-Path (Join-Path $PSScriptRoot "..")

function Say($m, $c = "Green") { Write-Host "  $m" -ForegroundColor $c }
function Step($m) { Write-Host ""; Write-Host $m -ForegroundColor Cyan }

# ---- 0. tools ---------------------------------------------------------------
Step "[0] Locating tools"
$iscc = @(
  "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
  "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
  "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup not found. Install it: winget install JRSoftware.InnoSetup" }
Say "ISCC  $iscc"

$vendor = Join-Path $PSScriptRoot "vendor"
foreach ($probe in @("pgsql\bin\psql.exe", "postgrest\postgrest.exe", "caddy\caddy.exe", "winsw\WinSW.exe", "node\node.exe")) {
  if (-not (Test-Path (Join-Path $vendor $probe))) {
    throw "vendor\$probe missing. Run: .\fetch-vendor.ps1 -IncludePostgres"
  }
}
# Refuse to ship the 848 MB version by accident: pgAdmin is the tell.
if (Test-Path (Join-Path $vendor "pgsql\pgAdmin 4")) {
  throw "vendor\pgsql still contains pgAdmin 4 (673 MB of GUI the client never opens). Run: .\scripts\trim-vendor.ps1"
}
Say "vendor binaries present and trimmed"

# ---- 1. backend -------------------------------------------------------------
Step "[1] Building the Express backend"
$backendSrc = Join-Path $repo "backend"
Push-Location $backendSrc
try {
  npm run build
  if ($LASTEXITCODE -ne 0) { throw "backend build failed" }
} finally { Pop-Location }
Say "dist\index.js built"

# ---- 2. stage ---------------------------------------------------------------
Step "[2] Staging the payload"
$staging = Join-Path $env:TEMP ("tb-installer-payload-{0}" -f $Version)
if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
New-Item -ItemType Directory -Path $staging -Force | Out-Null

Copy-Item (Join-Path $PSScriptRoot "vendor")    -Destination (Join-Path $staging "vendor") -Recurse
Copy-Item (Join-Path $PSScriptRoot "templates") -Destination (Join-Path $staging "templates") -Recurse
Copy-Item (Join-Path $PSScriptRoot "installer") -Destination (Join-Path $staging "installer") -Recurse
foreach ($f in @("start.ps1", "stop.ps1", "README.md")) {
  Copy-Item (Join-Path $PSScriptRoot $f) -Destination $staging
}

$stagedScripts = Join-Path $staging "scripts"
New-Item -ItemType Directory -Path $stagedScripts -Force | Out-Null
Copy-Item (Join-Path $PSScriptRoot "scripts\*") -Destination $stagedScripts
# The shared layer, flattened in. -File not '*.mjs': use-local-desktop.ps1 too.
Get-ChildItem (Join-Path $repo "local-stack-shared\scripts") -File | Copy-Item -Destination $stagedScripts
# `../../local-stack-shared/scripts/x.mjs` -> `./x.mjs`
Get-ChildItem $stagedScripts -Filter '*.mjs' | ForEach-Object {
  $text = [System.IO.File]::ReadAllText($_.FullName)
  $fixed = $text -replace '\.\./\.\./local-stack-shared/scripts/', './'
  if ($fixed -ne $text) { [System.IO.File]::WriteAllText($_.FullName, $fixed, (New-Object System.Text.UTF8Encoding($false))) }
}

# Schema, flattened in apply order. The order comes from schema-sources.mjs --
# the same module apply-schema.mjs uses at runtime -- so the shipped schema
# cannot diverge from the tested one.
$bundled = Join-Path $staging "schema\bundled"
New-Item -ItemType Directory -Path $bundled -Force | Out-Null
$repoUrl = 'file:///' + ($repo -replace '\\', '/')
$repoFwd = $repo -replace '\\', '/'
$listScript = @"
import { collectSchemaFiles } from '$repoUrl/local-stack-shared/scripts/schema-sources.mjs';
console.log(collectSchemaFiles('$repoFwd').join('\n'));
"@
$tmpList = Join-Path $env:TEMP ("tb-schema-list-{0}.mjs" -f [guid]::NewGuid())
try {
  [System.IO.File]::WriteAllText($tmpList, $listScript, (New-Object System.Text.UTF8Encoding($false)))
  $order = @(node $tmpList)
  if ($LASTEXITCODE -ne 0 -or -not $order) { throw "could not resolve the schema file list" }
} finally { if (Test-Path $tmpList) { Remove-Item $tmpList -Force } }
$i = 0
foreach ($src in $order) {
  $i++
  $name = "{0:d3}_{1}" -f $i, (Split-Path $src -Leaf)
  # Raw UTF-8, no BOM: Postgres rejects a leading U+FEFF as a syntax error on
  # line 1, pointing nowhere near the cause.
  [System.IO.File]::WriteAllText((Join-Path $bundled $name), [System.IO.File]::ReadAllText($src), (New-Object System.Text.UTF8Encoding($false)))
}
Say "schema: $i files"

# Backend: built output plus PRODUCTION dependencies only. `npm ci --omit=dev`
# into the staging copy drops typescript, tsx, esbuild and nodemon -- around
# 40 MB of build tooling that has no business on a client machine.
Step "[3] Installing production dependencies for the backend"
$stagedBackend = Join-Path $staging "backend"
New-Item -ItemType Directory -Path $stagedBackend -Force | Out-Null
Copy-Item (Join-Path $backendSrc "dist") -Destination (Join-Path $stagedBackend "dist") -Recurse
Copy-Item (Join-Path $backendSrc "package.json") -Destination $stagedBackend
Copy-Item (Join-Path $backendSrc "package-lock.json") -Destination $stagedBackend -ErrorAction SilentlyContinue
Push-Location $stagedBackend
try {
  npm ci --omit=dev --no-audit --no-fund
  if ($LASTEXITCODE -ne 0) { throw "npm ci --omit=dev failed" }
} finally { Pop-Location }

# backend\.env must NOT ship: it holds the cloud service_role key and the Firebase
# service-account private key. The service gets its configuration from the WinSW
# XML instead, which bootstrap generates per machine.
foreach ($leak in @(".env", ".env.testing", "use-local.ps1", "use-native.ps1")) {
  $p = Join-Path $stagedBackend $leak
  if (Test-Path $p) { Remove-Item $p -Force }
}
Say "backend staged"

$payloadMB = [math]::Round((Get-ChildItem $staging -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 0)
Say "payload: $payloadMB MB uncompressed"

if ($StageOnly) {
  Write-Host ""
  Write-Host "staged (not compiled): $staging" -ForegroundColor Green
  Write-Host "test it with:" -ForegroundColor Cyan
  Write-Host "  .\installer\post-install.ps1 -InstallDir <copy-of-staging> -DataDir <dir> -NoServices"
  return
}

# ---- 4. compile -------------------------------------------------------------
Step "[4] Compiling the installer (LZMA2, this takes a few minutes)"
New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
$OutDir = (Resolve-Path $OutDir).Path
$env:TB_INSTALLER_VERSION = $Version
$env:TB_PAYLOAD_DIR = $staging
$env:TB_OUTPUT_DIR = $OutDir

& $iscc /Q (Join-Path $PSScriptRoot "installer\TallyBridgeServer.iss")
if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }

$exe = Join-Path $OutDir ("TallyBridgeServer-Setup-{0}.exe" -f $Version)
if (-not (Test-Path $exe)) { throw "expected $exe" }

if (-not $KeepStaging) { Remove-Item $staging -Recurse -Force }

$mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host " $exe" -ForegroundColor Green
Write-Host " $mb MB  (from $payloadMB MB of payload)" -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Hand this one file to the client. On their PC:" -ForegroundColor Cyan
Write-Host "    double-click, accept the UAC prompt, optionally paste the project"
Write-Host "    URL + service key to copy existing data, and wait."
Write-Host ""
Write-Host "  IT IS NOT CODE-SIGNED. SmartScreen will warn, and antivirus may" -ForegroundColor Yellow
Write-Host "  quarantine postgrest.exe or caddy.exe WEEKS later, which presents as" -ForegroundColor Yellow
Write-Host "  'reports stopped working'. Sign it, or add an AV exclusion at setup." -ForegroundColor Yellow
Write-Host ""
