# Starts the stack. Use this rather than `docker compose up -d` directly.
#
# The reason it exists: after Docker Desktop shuts down (a reboot, a laptop
# sleeping, an update), the containers are stopped but still EXIST. Plain
# `docker compose up -d` then usually works, but a container left in a half-exited
# state produces
#
#     Error response from daemon: ... is not running
#
# and the fix is a `docker compose stop` first. This script just does that
# unconditionally, which is harmless when everything is already down.
#
# YOUR DATA IS SAFE across stop/start. It lives in the named volume
# `tallybridge_supabase_db_data`, not in the containers. Only `docker compose
# down -v` deletes it, and nothing here or in stop.ps1 passes -v.
#
# ASCII ONLY -- see the note in install.ps1.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

docker info --format '{{.ServerVersion}}' > $null 2>&1
if ($LASTEXITCODE -ne 0) {
  throw "Docker engine is not responding. Start Docker Desktop, wait for 'Engine running', then re-run."
}

docker compose stop 2>$null | Out-Null
docker compose up -d
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed" }

Write-Host "waiting for the database ..." -NoNewline
$deadline = (Get-Date).AddMinutes(3)
while ((Get-Date) -lt $deadline) {
  if ((docker inspect --format '{{.State.Health.Status}}' tb_supabase_db 2>$null) -eq 'healthy') { break }
  Start-Sleep -Seconds 2
  Write-Host "." -NoNewline
}
Write-Host ""

docker compose ps --format '{{.Service}}  {{.State}}  {{.Status}}'

$envFile = @{}
Get-Content (Join-Path $PSScriptRoot ".env") | ForEach-Object {
  if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $envFile[$Matches[1]] = $Matches[2].Trim() }
}
Write-Host ""
Write-Host "  API     http://127.0.0.1:$($envFile.KONG_HTTP_PORT)"
Write-Host "  Studio  http://127.0.0.1:$($envFile.STUDIO_PORT)"
Write-Host ""
