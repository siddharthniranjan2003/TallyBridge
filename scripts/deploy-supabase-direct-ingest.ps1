$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

param(
  [Parameter(Mandatory = $true)]
  [string]$ProjectRef,

  [Parameter(Mandatory = $true)]
  [string]$DbPassword,

  [Parameter(Mandatory = $true)]
  [string]$SyncIngestKey
)

$repoRoot = Split-Path -Parent $PSScriptRoot
$supabaseDir = Join-Path $repoRoot "supabase"
$tempDir = Join-Path $supabaseDir ".temp"
$backupRoot = Join-Path $env:TEMP ("tallybridge-supabase-temp-" + [guid]::NewGuid().ToString("N"))
$hadBackup = $false

function Invoke-SupabaseCommand {
  param(
    [Parameter(Mandatory = $true)]
    [string[]]$Arguments
  )

  Write-Host ("`n> supabase " + ($Arguments -join " "))
  & supabase @Arguments --yes --workdir $repoRoot
  if ($LASTEXITCODE -ne 0) {
    throw "supabase command failed with exit code $LASTEXITCODE"
  }
}

function Invoke-JsonRequest {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Url,

    [Parameter(Mandatory = $true)]
    [hashtable]$Headers,

    [Parameter(Mandatory = $true)]
    [string]$Body
  )

  return Invoke-WebRequest `
    -UseBasicParsing `
    -Method Post `
    -Uri $Url `
    -Headers $Headers `
    -Body $Body `
    -TimeoutSec 60
}

try {
  if (Test-Path -LiteralPath $tempDir) {
    New-Item -ItemType Directory -Path $backupRoot | Out-Null
    Copy-Item -LiteralPath $tempDir -Destination $backupRoot -Recurse
    $hadBackup = $true
  }

  Invoke-SupabaseCommand @(
    "link",
    "--project-ref", $ProjectRef,
    "--password", $DbPassword
  )

  Invoke-SupabaseCommand @(
    "db", "push",
    "--include-all",
    "--linked"
  )

  Invoke-SupabaseCommand @(
    "functions", "deploy", "ingest-sync",
    "--project-ref", $ProjectRef,
    "--use-api"
  )

  Invoke-SupabaseCommand @(
    "secrets", "set",
    "SYNC_INGEST_KEY=$SyncIngestKey",
    "--project-ref", $ProjectRef
  )

  $healthUrl = "https://$ProjectRef.supabase.co/functions/v1/ingest-sync"
  $health = Invoke-WebRequest -UseBasicParsing -Uri $healthUrl -TimeoutSec 30

  $dryRunPayload = @{
    sync_contract_version = 1
    company_name = "TallyBridge Validation"
    sync_meta = @{}
    groups = @()
    ledgers = @()
    stock_items = @()
  } | ConvertTo-Json -Compress

  $dryRunHeaders = @{
    "content-type" = "application/json"
    "x-sync-key" = $SyncIngestKey
    "x-sync-contract-version" = "1"
    "x-sync-dry-run" = "1"
  }

  $dryRun = Invoke-JsonRequest `
    -Url $healthUrl `
    -Headers $dryRunHeaders `
    -Body $dryRunPayload

  Write-Host ""
  Write-Host "Supabase direct-ingest deployment completed."
  Write-Host ("Project: " + $ProjectRef)
  Write-Host ("Health status: " + $health.StatusCode)
  Write-Host ("Dry-run status: " + $dryRun.StatusCode)
}
finally {
  if (Test-Path -LiteralPath $tempDir) {
    Remove-Item -LiteralPath $tempDir -Recurse -Force
  }

  if ($hadBackup) {
    Copy-Item -LiteralPath (Join-Path $backupRoot ".temp") -Destination $supabaseDir -Recurse
  }

  if (Test-Path -LiteralPath $backupRoot) {
    Remove-Item -LiteralPath $backupRoot -Recurse -Force
  }
}
