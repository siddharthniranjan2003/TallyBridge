# Produces a standalone folder to hand to a client. No Docker anywhere in it.
#
#   .\build-client-package.ps1                    ~1 MB, downloads binaries on site
#   .\build-client-package.ps1 -IncludeBinaries   ~450 MB, installs with no internet
#
# The working copy cannot be shipped as-is for two reasons:
#
#   1. Its scripts import from ..\local-stack-shared and read SQL out of
#      ..\backend and ..\supabase\migrations. None of that exists beside the
#      package, so the shared modules are flattened in and the imports rewritten,
#      and the SQL is flattened into schema\bundled in apply order.
#   2. config\ holds THIS machine's Postgres password, JWT secret and both role
#      keys. It is never copied; install.ps1 generates fresh ones on the client's
#      machine, so no two installs share a key.
#
# ASCII ONLY -- see the note in fetch-vendor.ps1.

[CmdletBinding()]
param(
  [string]$OutDir = (Join-Path $PSScriptRoot "..\dist-client\tallybridge-native"),
  [switch]$IncludeBinaries
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$repo = Resolve-Path (Join-Path $PSScriptRoot "..")

if (Test-Path $OutDir) { Remove-Item $OutDir -Recurse -Force }
New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
$OutDir = (Resolve-Path $OutDir).Path
Write-Host "building -> $OutDir" -ForegroundColor Cyan

# ---- 1. the stack itself ----------------------------------------------------
foreach ($f in @("install.ps1", "start.ps1", "stop.ps1", "fetch-vendor.ps1", "README.md")) {
  $src = Join-Path $PSScriptRoot $f
  if (Test-Path $src) { Copy-Item $src -Destination $OutDir }
}
New-Item -ItemType Directory -Path (Join-Path $OutDir "templates") -Force | Out-Null
Copy-Item (Join-Path $PSScriptRoot "templates\*") -Destination (Join-Path $OutDir "templates")

$outScripts = Join-Path $OutDir "scripts"
New-Item -ItemType Directory -Path $outScripts -Force | Out-Null
Copy-Item (Join-Path $PSScriptRoot "scripts\*") -Destination $outScripts

# The shared layer, flattened in beside the stack's own scripts. -File not
# -Filter '*.mjs': use-local-desktop.ps1 lives there too.
Get-ChildItem (Join-Path $repo "local-stack-shared\scripts") -File |
  Copy-Item -Destination $outScripts

# `../../local-stack-shared/scripts/x.mjs` -> `./x.mjs`. Rewritten here rather
# than making the scripts path-agnostic, because the repo layout is the one that
# should stay readable; the package is a build artifact.
Get-ChildItem $outScripts -Filter '*.mjs' | ForEach-Object {
  $text = [System.IO.File]::ReadAllText($_.FullName)
  $fixed = $text -replace '\.\./\.\./local-stack-shared/scripts/', './'
  if ($fixed -ne $text) {
    [System.IO.File]::WriteAllText($_.FullName, $fixed, (New-Object System.Text.UTF8Encoding($false)))
  }
}

# ---- 2. flatten the schema --------------------------------------------------
# The order comes from local-stack-shared\scripts\schema-sources.mjs -- the same
# module apply-schema.mjs uses at runtime, and the same one the Docker stack's
# packager uses. Asking Node for it rather than re-listing here is the point: a
# hand-copied list is how a package ends up building a schema nobody tested.
$bundled = Join-Path $OutDir "schema\bundled"
New-Item -ItemType Directory -Path $bundled -Force | Out-Null

# file:/// is required: Node's ESM loader parses a bare Windows drive letter as a
# URL scheme and fails with ERR_UNSUPPORTED_ESM_URL_SCHEME.
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
} finally {
  if (Test-Path $tmpList) { Remove-Item $tmpList -Force }
}

$i = 0
foreach ($src in $order) {
  if (-not (Test-Path $src)) { throw "missing schema source: $src" }
  $i++
  $name = "{0:d3}_{1}" -f $i, (Split-Path $src -Leaf)
  # Read/write raw UTF-8 without a BOM. PowerShell 5.1's Set-Content -Encoding
  # utf8 writes one, and Postgres rejects the leading U+FEFF as a syntax error on
  # line 1 -- an error that points nowhere near the cause.
  $text = [System.IO.File]::ReadAllText($src)
  [System.IO.File]::WriteAllText((Join-Path $bundled $name), $text, (New-Object System.Text.UTF8Encoding($false)))
}
Write-Host "  schema: $i files flattened into schema\bundled" -ForegroundColor Green

# ---- 3. optional offline binaries -------------------------------------------
if ($IncludeBinaries) {
  $vendor = Join-Path $PSScriptRoot "vendor"
  if (-not (Test-Path (Join-Path $vendor "pgsql\bin\psql.exe"))) {
    throw "vendor\pgsql is missing. Run: .\fetch-vendor.ps1 -IncludePostgres"
  }
  Write-Host "  copying vendor binaries (this takes a minute)..." -ForegroundColor DarkGray
  Copy-Item $vendor -Destination (Join-Path $OutDir "vendor") -Recurse -Force
  Write-Host "  binaries included (install with: .\install.ps1 -SkipVendor)" -ForegroundColor Green
}

# ---- 4. what NOT to ship, asserted ------------------------------------------
# A packaging bug that leaks config\ would put this machine's service_role key and
# Postgres password on a client PC, so it is checked rather than assumed.
foreach ($secret in @("config", "config\.env", "data", "logs")) {
  $leaked = Join-Path $OutDir $secret
  if (Test-Path $leaked) { throw "packaging bug: $secret was copied into the package. Remove it before shipping." }
}

$size = "{0:n1} MB" -f ((Get-ChildItem $OutDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
Write-Host ""
Write-Host "package ready: $OutDir  ($size)" -ForegroundColor Green
Write-Host ""
Write-Host "On the client machine:" -ForegroundColor Cyan
Write-Host "  1. Install Node 18+ (nothing else -- no Docker, no PostgreSQL installer)"
Write-Host "  2. Copy this folder anywhere, open PowerShell AS ADMINISTRATOR in it"
Write-Host "  3. .\install.ps1 -Seed -LiveUrl <project-url> -LiveKey <service-key>"
Write-Host ""
