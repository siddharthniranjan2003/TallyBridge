# Produces a standalone folder to hand to a client.
#
#   .\build-client-package.ps1                        # ~1 MB, pulls images on site
#   .\build-client-package.ps1 -IncludeImages         # ~4.9 GB, installs offline
#
# The working copy in this repo cannot be shipped as-is for two reasons:
#
#   1. apply-schema.mjs reads its SQL from ..\backend and ..\supabase\migrations,
#      which do not exist outside the repo. The packager flattens every SQL file,
#      in the exact order the installer applies them, into schema\bundled\ -- which
#      apply-schema.mjs prefers automatically when present.
#   2. .env and volumes\api\kong.yml hold THIS machine's secrets. They are never
#      copied. install.ps1 generates fresh ones on the client's machine, so no two
#      installs share a key.
#
# ASCII ONLY -- see the note in install.ps1.

[CmdletBinding()]
param(
  [string]$OutDir = (Join-Path $PSScriptRoot "..\dist-client\tallybridge-supabase"),
  [switch]$IncludeImages
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$repo = Resolve-Path (Join-Path $PSScriptRoot "..")

if (Test-Path $OutDir) { Remove-Item $OutDir -Recurse -Force }
New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
$OutDir = (Resolve-Path $OutDir).Path
Write-Host "building -> $OutDir" -ForegroundColor Cyan

# ---- 1. the stack itself ----------------------------------------------------
foreach ($f in @("docker-compose.yml", "install.ps1", "start.ps1", "stop.ps1", "README.md")) {
  $src = Join-Path $PSScriptRoot $f
  if (Test-Path $src) { Copy-Item $src -Destination $OutDir }
}

New-Item -ItemType Directory -Path (Join-Path $OutDir "scripts") -Force | Out-Null
Get-ChildItem (Join-Path $PSScriptRoot "scripts") -Filter '*.mjs' |
  Copy-Item -Destination (Join-Path $OutDir "scripts")
# The shared layer. In the repo these live one level up in local-stack-shared\ and
# are imported by relative path; a package has no such sibling, so they are
# flattened in beside the stack's own scripts and the imports rewritten below.
$sharedScripts = Join-Path $repo "local-stack-shared\scripts"
Get-ChildItem $sharedScripts -File | Copy-Item -Destination (Join-Path $OutDir "scripts")

# `../../local-stack-shared/scripts/x.mjs` -> `./x.mjs`. Done here rather than by
# making the scripts path-agnostic because the repo layout is the one that should
# stay readable; the package is a build artifact.
Get-ChildItem (Join-Path $OutDir "scripts") -Filter '*.mjs' | ForEach-Object {
  $text = [System.IO.File]::ReadAllText($_.FullName)
  $fixed = $text -replace '\.\./\.\./local-stack-shared/scripts/', './'
  if ($fixed -ne $text) {
    [System.IO.File]::WriteAllText($_.FullName, $fixed, (New-Object System.Text.UTF8Encoding($false)))
  }
}

# volumes\db holds the two init scripts; volumes\api holds only the TEMPLATE.
# kong.yml itself is generated on the client machine and must not be copied --
# it carries this machine's anon and service keys in plain text.
New-Item -ItemType Directory -Path (Join-Path $OutDir "volumes\db") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $OutDir "volumes\api") -Force | Out-Null
Copy-Item (Join-Path $PSScriptRoot "volumes\db\*.sql") -Destination (Join-Path $OutDir "volumes\db")
Copy-Item (Join-Path $PSScriptRoot "volumes\api\kong.template.yml") -Destination (Join-Path $OutDir "volumes\api")

# ---- 2. flatten the schema --------------------------------------------------
# The order comes from local-stack-shared\scripts\schema-sources.mjs, the same
# module apply-schema.mjs uses at runtime and the same one the native stack's
# packager uses. Asking Node for it rather than re-listing the files here is the
# point: a hand-copied list is exactly how a package ends up building a different
# schema than the one that was tested.
$bundled = Join-Path $OutDir "schema\bundled"
New-Item -ItemType Directory -Path $bundled -Force | Out-Null

# file:/// is required, not cosmetic. Node's ESM loader rejects a bare Windows
# absolute path with ERR_UNSUPPORTED_ESM_URL_SCHEME ("Received protocol 'c:'"),
# because it parses the drive letter as a URL scheme.
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
  # Read/write as raw UTF-8 without a BOM. PowerShell 5.1's Set-Content -Encoding
  # utf8 writes a BOM, and Postgres rejects the leading U+FEFF as a syntax error
  # on line 1 -- an error that points nowhere near the cause.
  $text = [System.IO.File]::ReadAllText($src)
  [System.IO.File]::WriteAllText((Join-Path $bundled $name), $text, (New-Object System.Text.UTF8Encoding($false)))
}
Write-Host "  schema: $i files flattened into schema\bundled" -ForegroundColor Green

# ---- 3. optional offline images --------------------------------------------
if ($IncludeImages) {
  $imgDir = Join-Path $OutDir "images"
  New-Item -ItemType Directory -Path $imgDir -Force | Out-Null
  # Read the tags out of the compose file so this cannot drift from what actually
  # runs.
  $tags = Select-String -Path (Join-Path $PSScriptRoot "docker-compose.yml") -Pattern '^\s*image:\s*(\S+)' |
    ForEach-Object { $_.Matches[0].Groups[1].Value } | Sort-Object -Unique
  foreach ($tag in $tags) {
    $file = ($tag -replace '[/:]', '_') + '.tar'
    Write-Host "  saving $tag ..." -ForegroundColor DarkGray
    docker save -o (Join-Path $imgDir $file) $tag
    if ($LASTEXITCODE -ne 0) { throw "docker save failed for $tag" }
  }
  Write-Host "  images: $($tags.Count) saved (install with: .\install.ps1 -OfflineImages)" -ForegroundColor Green
}

# ---- 4. what NOT to ship, asserted ------------------------------------------
# A packaging bug that leaks .env would put this machine's service_role key on a
# client PC, so it is checked rather than assumed.
foreach ($secret in @(".env", "volumes\api\kong.yml")) {
  $leaked = Join-Path $OutDir $secret
  if (Test-Path $leaked) { throw "packaging bug: $secret was copied into the package. Remove it before shipping." }
}

$size = "{0:n1} MB" -f ((Get-ChildItem $OutDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
Write-Host ""
Write-Host "package ready: $OutDir  ($size)" -ForegroundColor Green
Write-Host ""
Write-Host "On the client machine:" -ForegroundColor Cyan
Write-Host "  1. Install Docker Desktop and Node 18+, start Docker Desktop"
Write-Host "  2. Copy this folder anywhere, open PowerShell in it"
Write-Host "  3. .\install.ps1 -Seed -LiveUrl <project-url> -LiveKey <service-key> -Profile reports"
Write-Host ""
