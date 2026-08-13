# Stages everything the TallyBridge desktop app needs to run the data stack
# itself, into one directory that electron-builder copies in as resources\stack.
#
#   .\stage-for-app.ps1
#
# This is build-client-package.ps1 plus the built Express backend. The split is
# deliberate: build-client-package.ps1 produces the standalone folder a human
# installs by hand, and it already solves the two hard parts -- flattening the
# schema in apply order and rewriting the shared-module imports so nothing
# reaches outside the package. Re-implementing either here is how the app ends up
# shipping a schema nobody tested.
#
# What the app adds on top is the backend, because in this design the app spawns
# it rather than the client running `npm run dev` in a checkout.
#
# NOT staged, and asserted at the end: config\ (this machine's Postgres password,
# JWT secret and both role keys) and data\ (the books). install.ps1 generates
# fresh secrets on the target machine, so no two installs share a key.
#
# ASCII ONLY -- see the note in fetch-vendor.ps1.

[CmdletBinding()]
param(
  [string]$OutDir,
  # The backend must be compiled first; this only copies the output.
  [switch]$SkipBackend
)

$ErrorActionPreference = "Stop"

# $PSScriptRoot is not populated while param() defaults are being evaluated when
# the script is launched as `powershell -File <relative path>`, which is how the
# npm script calls it. Resolve the script's own directory in the body instead.
$here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
Set-Location $here
if (-not $OutDir) { $OutDir = Join-Path $here "..\dist-client\tallybridge-native" }
$repo = Resolve-Path (Join-Path $here "..")

# ---- 1. the stack, schema and vendor binaries -------------------------------
& (Join-Path $here "build-client-package.ps1") -OutDir $OutDir -IncludeBinaries
if ($LASTEXITCODE -ne 0) { throw "build-client-package.ps1 failed" }
$OutDir = (Resolve-Path $OutDir).Path

# ---- 2. the built Express backend -------------------------------------------
if (-not $SkipBackend) {
  $backendSrc = Join-Path $repo "backend"
  $backendOut = Join-Path $OutDir "backend"

  if (-not (Test-Path (Join-Path $backendSrc "dist\index.js"))) {
    throw "backend\dist\index.js not found. Build it first: cd backend ; npm run build"
  }
  if (-not (Test-Path (Join-Path $backendSrc "node_modules"))) {
    throw "backend\node_modules not found. Run: cd backend ; npm install"
  }

  New-Item -ItemType Directory -Path $backendOut -Force | Out-Null
  Copy-Item (Join-Path $backendSrc "dist") -Destination $backendOut -Recurse -Force
  Copy-Item (Join-Path $backendSrc "package.json") -Destination $backendOut -Force

  # node_modules is copied whole. Pruning to production deps would save maybe
  # 40 MB, but firebase-admin and @google-cloud/storage are imported at module
  # load by routes this deployment does not use, and a missing transitive
  # dependency surfaces as the backend failing to boot with a stack trace the
  # client cannot act on. Correctness over size until size is a problem.
  Write-Host "  copying backend\node_modules (this takes a minute)..." -ForegroundColor DarkGray
  Copy-Item (Join-Path $backendSrc "node_modules") -Destination $backendOut -Recurse -Force

  # .env would carry the CLOUD service key onto the client's machine. The app
  # passes the local values as environment variables instead.
  $leakedEnv = Join-Path $backendOut ".env"
  if (Test-Path $leakedEnv) { Remove-Item $leakedEnv -Force }

  Write-Host "  backend staged" -ForegroundColor Green
}

# ---- 3. assert nothing secret got in ----------------------------------------
foreach ($secret in @("config", "config\.env", "data", "logs", "backend\.env")) {
  $leaked = Join-Path $OutDir $secret
  if (Test-Path $leaked) { throw "packaging bug: $secret is in the staged payload. Remove it before shipping." }
}

$size = "{0:n1} MB" -f ((Get-ChildItem $OutDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
Write-Host ""
Write-Host "staged for the app: $OutDir  ($size)" -ForegroundColor Green
Write-Host "electron-builder copies this to resources\stack" -ForegroundColor Cyan
Write-Host ""
