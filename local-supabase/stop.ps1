# Stops the stack and KEEPS the data.
#
#   .\stop.ps1            stop containers, keep the database   <- what you want
#   .\stop.ps1 -Destroy   ALSO DELETE THE DATABASE VOLUME
#
# -Destroy runs `docker compose down -v`, which removes the named volume
# `tallybridge_supabase_db_data`. On a client machine that volume IS the client's
# books once they are off the cloud, and there is no undo, so it asks for typed
# confirmation.
#
# ASCII ONLY -- see the note in install.ps1.

[CmdletBinding()]
param([switch]$Destroy)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not $Destroy) {
  docker compose stop
  Write-Host "stopped. data kept. start again with: .\start.ps1" -ForegroundColor Green
  return
}

Write-Host ""
Write-Host "THIS DELETES THE DATABASE." -ForegroundColor Red
Write-Host "Volume tallybridge_supabase_db_data and everything in it will be removed." -ForegroundColor Red
Write-Host "If this machine is the only copy of the data, it is gone for good." -ForegroundColor Red
Write-Host ""
$answer = Read-Host "Type DELETE to confirm"
if ($answer -cne "DELETE") {
  Write-Host "cancelled -- nothing was removed." -ForegroundColor Yellow
  return
}

docker compose down -v
Write-Host "stack and data volume removed." -ForegroundColor Yellow
Write-Host "A fresh .\install.ps1 will rebuild it empty." -ForegroundColor Yellow
