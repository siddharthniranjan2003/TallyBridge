# Stops the native stack. Data is never touched.
#
#   .\stop.ps1              stop everything
#   .\stop.ps1 -KeepDb      stop PostgREST and Caddy, leave Postgres up
#
# Mirrors start.ps1: uses the Windows services if they are registered, otherwise
# the PIDs recorded under logs\.
#
# There is no -Destroy here, unlike the Docker stack's stop.ps1. Deleting a
# native cluster means deleting the data directory, which is a plain folder the
# operator can see and remove deliberately -- no wrapper should make that a flag.
#
# ASCII ONLY -- see the note in fetch-vendor.ps1.

[CmdletBinding()]
param([switch]$KeepDb)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$e = @{}
Get-Content (Join-Path $PSScriptRoot "config\.env") | ForEach-Object {
  if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $e[$Matches[1]] = $Matches[2].Trim() }
}
$logs = Join-Path $PSScriptRoot "logs"

if (Get-Service -Name 'tallybridge-postgres' -ErrorAction SilentlyContinue) {
  # Reverse dependency order: the gateway should stop answering before what it
  # proxies to disappears, otherwise in-flight requests get a connection reset
  # instead of a clean 502.
  foreach ($n in @('tallybridge-gateway', 'tallybridge-postgrest')) {
    Stop-Service -Name $n -ErrorAction SilentlyContinue
    Write-Host ("  stopped {0}" -f $n) -ForegroundColor Green
  }
  if (-not $KeepDb) {
    Stop-Service -Name 'tallybridge-postgres' -ErrorAction SilentlyContinue
    Write-Host "  stopped tallybridge-postgres" -ForegroundColor Green
  }
} else {
  foreach ($name in @('gateway', 'postgrest')) {
    $pidFile = Join-Path $logs "$name.pid"
    if (Test-Path $pidFile) {
      $procId = Get-Content $pidFile
      $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
      if ($p) { Stop-Process -Id $procId -Force; Write-Host ("  stopped {0} (pid {1})" -f $name, $procId) -ForegroundColor Green }
      Remove-Item $pidFile -Force
    }
  }
  if (-not $KeepDb) {
    $pgBin = Join-Path $e.VENDOR_DIR "pgsql\bin"
    # -m fast rolls back open transactions and checkpoints cleanly. The default
    # (smart) waits for every client to disconnect, which never happens while a
    # backend holds a pool open.
    & (Join-Path $pgBin "pg_ctl.exe") -D $e.PGDATA -m fast -w stop 2>$null | Out-Null
    Write-Host "  stopped postgres" -ForegroundColor Green
  }
}

Write-Host ""
Write-Host "stopped. data kept in $($e.PGDATA)" -ForegroundColor Green
