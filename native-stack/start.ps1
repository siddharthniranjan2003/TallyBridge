# Starts the native stack.
#
# Two modes, chosen automatically:
#
#   SERVICE mode  - if the Windows services are registered (install.ps1 with
#                   admin), start those. This is the real deployment: they come up
#                   with the machine, before anyone logs in, which is the entire
#                   reason for the native stack.
#   PROCESS mode  - otherwise run the same three binaries with the same config as
#                   ordinary processes, and record their PIDs under logs\.
#                   Identical behaviour, tied to this login session. Useful on a
#                   dev box without admin, and for verifying a build before
#                   handing it over.
#
# ASCII ONLY -- see the note in fetch-vendor.ps1.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$e = @{}
$envPath = Join-Path $PSScriptRoot "config\.env"
if (-not (Test-Path $envPath)) { throw "config\.env not found. Run .\install.ps1 first." }
Get-Content $envPath | ForEach-Object {
  if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $e[$Matches[1]] = $Matches[2].Trim() }
}

$logs = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Path $logs -Force | Out-Null

$svc = Get-Service -Name 'tallybridge-postgres' -ErrorAction SilentlyContinue
if ($svc) {
  Write-Host "service mode" -ForegroundColor Cyan
  foreach ($n in @('tallybridge-postgres', 'tallybridge-postgrest', 'tallybridge-gateway')) {
    Start-Service -Name $n
    Write-Host ("  started {0}" -f $n) -ForegroundColor Green
  }
} else {
  Write-Host "process mode (services not registered -- run install.ps1 as admin for the real thing)" -ForegroundColor Yellow

  $pgBin = Join-Path $e.VENDOR_DIR "pgsql\bin"
  $env:Path = "$pgBin;$env:Path"

  # Postgres first and SYNCHRONOUSLY (-w): PostgREST exits if it cannot reach the
  # database on startup, so racing them means PostgREST dies and the stack looks
  # half-broken for reasons that are not in its log.
  & (Join-Path $pgBin "pg_isready.exe") -h 127.0.0.1 -p $e.PGPORT -q 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) {
    & (Join-Path $pgBin "pg_ctl.exe") -D $e.PGDATA -l (Join-Path $logs "postgres.log") -w -o "-p $($e.PGPORT)" start | Out-Null
  }
  Write-Host "  postgres  127.0.0.1:$($e.PGPORT)" -ForegroundColor Green

  function Start-Tracked($name, $exe, $argList) {
    $pidFile = Join-Path $logs "$name.pid"
    if (Test-Path $pidFile) {
      $old = Get-Process -Id (Get-Content $pidFile) -ErrorAction SilentlyContinue
      if ($old) { Write-Host ("  {0} already running (pid {1})" -f $name, $old.Id) -ForegroundColor DarkGray; return }
    }
    # -WindowStyle Hidden, NOT -NoNewWindow. -NoNewWindow makes the child share
    # this shell's console, with two consequences: the child inherits the
    # console's stdout handle and keeps it open, so anything piping this script's
    # output blocks forever waiting for EOF; and killing the shell kills the
    # child, so the "stack" evaporates when the terminal closes. Hidden gives the
    # child its own console and detaches it.
    #
    # None of this applies in service mode -- WinSW owns the process there. This
    # only matters for process mode, which is what a dev box without admin uses.
    $p = Start-Process -FilePath $exe -ArgumentList $argList -WindowStyle Hidden -PassThru `
           -RedirectStandardOutput (Join-Path $logs "$name.out.log") `
           -RedirectStandardError  (Join-Path $logs "$name.err.log")
    Set-Content -Path $pidFile -Value $p.Id
    Write-Host ("  {0}  pid {1}" -f $name.PadRight(9), $p.Id) -ForegroundColor Green
  }

  # PATH already carries pgsql\bin above -- postgrest.exe needs libpq.dll and
  # exits 0xC0000135 silently without it.
  Start-Tracked "postgrest" (Join-Path $e.VENDOR_DIR "postgrest\postgrest.exe") @("`"$(Join-Path $PSScriptRoot 'config\postgrest.conf')`"")
  Start-Tracked "gateway"   (Join-Path $e.VENDOR_DIR "caddy\caddy.exe")         @("run", "--config", "`"$(Join-Path $PSScriptRoot 'config\Caddyfile')`"", "--adapter", "caddyfile")
  Start-Sleep -Seconds 4
}

Write-Host ""
Write-Host "  API      http://127.0.0.1:$($e.GATEWAY_PORT)          (SUPABASE_URL points here)"
Write-Host "  Postgres 127.0.0.1:$($e.PGPORT)"
Write-Host ""
Write-Host "  check:   node scripts\healthcheck.mjs"
Write-Host ""
