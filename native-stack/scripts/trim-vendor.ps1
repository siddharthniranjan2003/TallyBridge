# Strips everything from .\vendor that a headless database server does not run.
#
#   .\scripts\trim-vendor.ps1 -WhatIf   show what would go, change nothing
#   .\scripts\trim-vendor.ps1           do it
#
# The EnterpriseDB "binaries" zip is 848 MB extracted, and 673 MB of that is
# pgAdmin 4 -- a desktop GUI the client will never open. The remaining cuts are
# documentation, C headers and the wxWidgets DLLs that exist only to draw
# pgAdmin's window. What is left is a complete PostgreSQL server and client.
#
# This is what makes a single-exe installer realistic: ~130 MB instead of 848 MB,
# before compression.
#
# Safe to re-run. Verify afterwards with .\scripts\db-init.ps1 -StartOnly and
# node scripts\healthcheck.mjs -- both exercise initdb, psql, pg_ctl and
# pg_isready, which are the four binaries the stack actually depends on.
#
# ASCII ONLY -- see the note in fetch-vendor.ps1.

[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = "Stop"
$pkg = Split-Path $PSScriptRoot -Parent
$pg = Join-Path $pkg "vendor\pgsql"
if (-not (Test-Path $pg)) { throw "vendor\pgsql not found. Run: .\fetch-vendor.ps1 -IncludePostgres" }

function Size($path) {
  if (-not (Test-Path $path)) { return 0 }
  (Get-ChildItem $path -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
}

$before = Size $pg

# Whole directories that have no role in running a server.
$dirs = @(
  "pgAdmin 4",     # 673 MB desktop GUI. Studio covers this need on the dev box.
  "doc",           # HTML manual
  "include",       # C headers, for building extensions against this server
  "StackBuilder",  # optional-component downloader, interactive
  "symbols"        # debug symbols, present in some builds
)

# pgAdmin's UI toolkit, which lives in bin\ rather than its own folder.
$binPatterns = @("wxmsw*.dll", "wxbase*.dll")

foreach ($d in $dirs) {
  $path = Join-Path $pg $d
  if (Test-Path $path) {
    $mb = [math]::Round((Size $path) / 1MB, 1)
    if ($PSCmdlet.ShouldProcess("$d ($mb MB)", "remove")) {
      Remove-Item $path -Recurse -Force
    }
    Write-Host ("  - {0,-16} {1,7:n1} MB" -f $d, $mb) -ForegroundColor DarkGray
  }
}

foreach ($pat in $binPatterns) {
  Get-ChildItem (Join-Path $pg "bin") -Filter $pat -ErrorAction SilentlyContinue | ForEach-Object {
    $mb = [math]::Round($_.Length / 1MB, 1)
    if ($PSCmdlet.ShouldProcess("bin\$($_.Name) ($mb MB)", "remove")) { Remove-Item $_.FullName -Force }
    Write-Host ("  - bin\{0,-11} {1,7:n1} MB" -f $_.Name, $mb) -ForegroundColor DarkGray
  }
}

# Guard: everything the stack calls by name must survive. A trim that quietly
# removed one of these would not fail here -- it would fail on the client's
# machine, during install, with a path error.
$required = @("postgres.exe", "initdb.exe", "pg_ctl.exe", "psql.exe", "pg_isready.exe", "libpq.dll")
$missing = $required | Where-Object { -not (Test-Path (Join-Path $pg "bin\$_")) }
if ($missing) { throw "trim removed something required: $($missing -join ', ')" }

$after = Size $pg
Write-Host ""
Write-Host ("pgsql: {0:n0} MB -> {1:n0} MB  (saved {2:n0} MB)" -f ($before/1MB), ($after/1MB), (($before-$after)/1MB)) -ForegroundColor Green
Write-Host "verify with: .\scripts\db-init.ps1 -StartOnly ; node scripts\healthcheck.mjs" -ForegroundColor Cyan
