# Runs from the uninstaller, before Inno deletes the install directory.
#
# Stops and deregisters the four Windows services. If this is skipped, the
# service entries survive as orphans pointing at a path that no longer exists,
# and Windows reports them failed on every boot with no obvious cause.
#
# It does NOT touch the data directory. After cutover that directory is the
# client's entire books, and an uninstall must not be able to destroy it. Its
# location is printed instead.
#
# ASCII ONLY -- see the note in post-install.ps1.

[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$InstallDir)

$ErrorActionPreference = "Continue"

$log = Join-Path $InstallDir "logs\uninstall.log"
New-Item -ItemType Directory -Path (Split-Path $log -Parent) -Force -ErrorAction SilentlyContinue | Out-Null

$dataDir = ""
$envPath = Join-Path $InstallDir "config\.env"
if (Test-Path $envPath) {
  Get-Content $envPath | ForEach-Object {
    if ($_ -match '^\s*PGDATA\s*=\s*(.*)$') { $dataDir = $Matches[1].Trim() }
  }
}

$svcScript = Join-Path $InstallDir "scripts\services.ps1"
if (Test-Path $svcScript) {
  try {
    & $svcScript -Uninstall
  } catch {
    "services.ps1 -Uninstall failed: $($_.Exception.Message)" | Out-File $log -Append
  }
}

# Belt and braces: if services.ps1 could not run (a partial install, a missing
# config), remove whatever is registered by name so nothing is orphaned.
foreach ($id in @('tallybridge-backend', 'tallybridge-gateway', 'tallybridge-postgrest', 'tallybridge-postgres')) {
  $svc = Get-Service -Name $id -ErrorAction SilentlyContinue
  if ($svc) {
    Stop-Service -Name $id -Force -ErrorAction SilentlyContinue
    # sc.exe rather than Remove-Service: Remove-Service does not exist in
    # Windows PowerShell 5.1, which is what runs on a stock client machine.
    & sc.exe delete $id | Out-Null
    "removed orphaned service $id" | Out-File $log -Append
  }
}

if ($dataDir) {
  "Database NOT removed. It is still at: $dataDir" | Out-File $log -Append
}
exit 0
