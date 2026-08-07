# Registers or removes the three Windows services. REQUIRES ADMINISTRATOR.
#
#   .\scripts\services.ps1 -Install
#   .\scripts\services.ps1 -Uninstall
#   .\scripts\services.ps1 -Status
#
# This is the step that makes the native stack worth building: registered
# services start with the machine, before and without anyone logging in. Docker
# Desktop cannot do that, so an unattended reboot silently stops the client's
# reports until someone signs in.
#
# ASCII ONLY -- see the note in fetch-vendor.ps1.

[CmdletBinding()]
param([switch]$Install, [switch]$Uninstall, [switch]$Status)

$ErrorActionPreference = "Stop"
$pkg = Split-Path $PSScriptRoot -Parent

# Dependency order. tallybridge-backend only exists when the installer staged the
# Express backend, so it is included only if bootstrap generated its XML.
$ids = @('tallybridge-postgres', 'tallybridge-postgrest', 'tallybridge-gateway')
if (Test-Path (Join-Path (Split-Path $PSScriptRoot -Parent) "config\services\tallybridge-backend.xml")) {
  $ids += 'tallybridge-backend'
}

function Test-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
}

if ($Status) {
  foreach ($id in $ids) {
    $s = Get-Service -Name $id -ErrorAction SilentlyContinue
    if ($s) { "  {0,-24} {1,-9} start={2}" -f $id, $s.Status, $s.StartType }
    else    { "  {0,-24} not registered" -f $id }
  }
  return
}

if (-not (Test-Admin)) {
  throw @"
Administrator required to register Windows services.

Right-click PowerShell -> Run as administrator, then:
    cd $pkg
    .\scripts\services.ps1 $(if ($Uninstall) { '-Uninstall' } else { '-Install' })

Everything else in this stack -- initdb, schema, data copy, and running the same
binaries as ordinary processes via .\start.ps1 -- works without elevation.
"@
}

$svcDir = Join-Path $pkg "config\services"
if (-not (Test-Path $svcDir)) { throw "config\services not found. Run: node scripts\bootstrap.mjs" }

$e = @{}
Get-Content (Join-Path $pkg "config\.env") | ForEach-Object {
  if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $e[$Matches[1]] = $Matches[2].Trim() }
}
$winsw = Join-Path $e.VENDOR_DIR "winsw\WinSW.exe"
if (-not (Test-Path $winsw)) { throw "WinSW.exe not found at $winsw. Run: .\fetch-vendor.ps1" }

if ($Uninstall) {
  # Reverse order so dependents go before what they depend on.
  foreach ($id in ($ids | Sort-Object -Descending)) {
    $exe = Join-Path $svcDir "$id.exe"
    if (-not (Test-Path $exe)) { Write-Host "  $id not installed" -ForegroundColor DarkGray; continue }
    & $exe stop 2>$null | Out-Null
    & $exe uninstall
    Write-Host "  removed $id" -ForegroundColor Yellow
  }
  Write-Host ""
  Write-Host "Services removed. The database directory was NOT touched: $($e.PGDATA)" -ForegroundColor Green
  return
}

# Install.
#
# WinSW identifies its config by FILENAME: <name>.exe reads <name>.xml sitting
# beside it. So each service gets its own copy of the executable named after it.
# That is WinSW's documented convention, not a workaround -- one shared WinSW.exe
# would have no way to tell which of the three it is being started as.
foreach ($id in $ids) {
  $xml = Join-Path $svcDir "$id.xml"
  if (-not (Test-Path $xml)) { throw "missing $xml" }
  $exe = Join-Path $svcDir "$id.exe"
  Copy-Item $winsw $exe -Force

  if (Get-Service -Name $id -ErrorAction SilentlyContinue) {
    Write-Host "  $id already registered, refreshing config" -ForegroundColor DarkGray
    & $exe stop 2>$null | Out-Null
    & $exe uninstall | Out-Null
    Start-Sleep -Seconds 2
  }
  & $exe install
  if ($LASTEXITCODE -ne 0) { throw "failed to install $id" }
  Write-Host "  registered $id" -ForegroundColor Green
}

Write-Host ""
Write-Host "Registered. Start them with .\start.ps1, or reboot -- they are set to Automatic." -ForegroundColor Green
Write-Host ""
Write-Host "If antivirus later quarantines one of these unsigned binaries the service" -ForegroundColor Yellow
Write-Host "will fail to start WEEKS after a clean install, presenting as 'reports stopped" -ForegroundColor Yellow
Write-Host "working'. Add an AV exclusion for $pkg now." -ForegroundColor Yellow
