# Downloads the three (or four) binaries the native stack runs on into .\vendor.
#
#   .\fetch-vendor.ps1                    postgrest + caddy + winsw   (~45 MB)
#   .\fetch-vendor.ps1 -IncludePostgres   + PostgreSQL binaries       (~360 MB)
#
# Versions are pinned. Nothing here auto-updates: a client PC that silently moved
# to a new PostgREST minor between two installs is a support call nobody can
# reproduce.
#
# ASCII ONLY -- Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI, so a UTF-8
# dash arrives as three characters ending in a double-quote and terminates
# whatever string it lands in.

[CmdletBinding()]
param([switch]$IncludePostgres, [switch]$Force)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$vendor = Join-Path $PSScriptRoot "vendor"
New-Item -ItemType Directory -Path $vendor -Force | Out-Null

# PostgREST 12.2.12 matches the Docker stack's postgrest image tag exactly, so
# both deployments run identical query semantics.
$items = @(
  @{ name = "postgrest"; url = "https://github.com/PostgREST/postgrest/releases/download/v12.2.12/postgrest-v12.2.12-windows-x86-64.zip"; probe = "postgrest.exe" }
  @{ name = "caddy";     url = "https://github.com/caddyserver/caddy/releases/download/v2.10.2/caddy_2.10.2_windows_amd64.zip";           probe = "caddy.exe" }
  # node.exe runs the install scripts AND the Express backend as a service, so
  # bundling it means the client machine needs nothing pre-installed at all.
  # 22 LTS rather than the 24 on the dev box: a shop PC should be on the
  # long-term line.
  @{ name = "node"; url = "https://nodejs.org/dist/v22.21.1/node-v22.21.1-win-x64.zip"; probe = "node.exe"; single = "node-v22.21.1-win-x64\node.exe" }
)
if ($IncludePostgres) {
  # 17.6 matches the live Supabase server version (17.6), so anything that works
  # against the cloud project behaves the same here.
  $items += @{ name = "pgsql"; url = "https://get.enterprisedb.com/postgresql/postgresql-17.6-1-windows-x64-binaries.zip"; probe = "pgsql\bin\psql.exe"; flat = $true }
}

foreach ($it in $items) {
  $dest = Join-Path $vendor $it.name
  $probe = Join-Path $(if ($it.flat) { $vendor } else { $dest }) $it.probe
  if ((Test-Path $probe) -and -not $Force) {
    Write-Host ("  have  {0}" -f $it.name) -ForegroundColor DarkGray
    continue
  }
  $zip = Join-Path $vendor ("{0}.zip" -f $it.name)
  Write-Host ("  fetch {0} ..." -f $it.name) -ForegroundColor Cyan
  Invoke-WebRequest -UseBasicParsing -Uri $it.url -OutFile $zip

  if ($it.single) {
    # Node ships a whole distribution (npm, headers, docs) inside a versioned
    # folder; we want exactly one file out of it. Extracting to a staging dir and
    # keeping node.exe drops ~60 MB and, more importantly, keeps the vendored
    # path free of the version number so nothing has to be re-pinned in two
    # places when it moves.
    $stage = Join-Path $vendor ("_stage_{0}" -f $it.name)
    if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
    Expand-Archive $zip -DestinationPath $stage -Force
    New-Item -ItemType Directory -Path $dest -Force | Out-Null
    Copy-Item (Join-Path $stage $it.single) -Destination $dest -Force
    Remove-Item $stage -Recurse -Force
  } else {
    # The PostgreSQL zip already contains a pgsql\ directory, so it expands into
    # vendor\ directly; the others are bare and get their own folder.
    Expand-Archive $zip -DestinationPath $(if ($it.flat) { $vendor } else { $dest }) -Force
  }
  Remove-Item $zip -Force
  if (-not (Test-Path $probe)) { throw "expected $probe after extracting $($it.name)" }
  Write-Host ("  ok    {0}" -f $it.name) -ForegroundColor Green
}

# WinSW is a single exe, not a zip.
$winsw = Join-Path $vendor "winsw"
New-Item -ItemType Directory -Path $winsw -Force | Out-Null
$winswExe = Join-Path $winsw "WinSW.exe"
if ((-not (Test-Path $winswExe)) -or $Force) {
  Write-Host "  fetch winsw ..." -ForegroundColor Cyan
  # net461 build: .NET Framework 4.6.1+ is present on every Windows 10/11, so
  # this needs no runtime install on the client machine.
  Invoke-WebRequest -UseBasicParsing `
    -Uri "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW.NET461.exe" `
    -OutFile $winswExe
  Write-Host "  ok    winsw" -ForegroundColor Green
} else {
  Write-Host "  have  winsw" -ForegroundColor DarkGray
}

Write-Host ""
$size = "{0:n0} MB" -f ((Get-ChildItem $vendor -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
Write-Host "vendor ready: $vendor  ($size)" -ForegroundColor Green
if (-not $IncludePostgres) {
  Write-Host "PostgreSQL not fetched. Either re-run with -IncludePostgres, or point" -ForegroundColor Yellow
  Write-Host "VENDOR_DIR at an existing PostgreSQL install that has bin\psql.exe." -ForegroundColor Yellow
}
