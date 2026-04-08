$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName System.Data

function Write-JsonLine($Value) {
  $json = $Value | ConvertTo-Json -Compress -Depth 12
  [Console]::Out.WriteLine($json)
  [Console]::Out.Flush()
}

function Get-CandidateDsns($Override, $Port) {
  $candidates = New-Object System.Collections.Generic.List[string]
  if ($Override) {
    $candidates.Add($Override)
  }

  if ($Port) {
    $candidates.Add("TallyODBC64_$Port")
    $candidates.Add("TallyODBC_$Port")
  }

  return $candidates | Select-Object -Unique
}

function Test-OdbcConnection($Dsn, $TimeoutSeconds) {
  $connection = New-Object System.Data.Odbc.OdbcConnection("DSN=$Dsn;")
  $connection.ConnectionTimeout = $TimeoutSeconds
  try {
    $connection.Open()
    return $connection
  } catch {
    if ($connection) {
      $connection.Dispose()
    }
    throw
  }
}

function Execute-OdbcQuery($Connection, $Sql, $TimeoutSeconds) {
  $command = $Connection.CreateCommand()
  $command.CommandText = $Sql
  $command.CommandTimeout = $TimeoutSeconds

  $reader = $null
  try {
    $reader = $command.ExecuteReader()
    $rows = New-Object System.Collections.Generic.List[object]

    while ($reader.Read()) {
      $row = [ordered]@{}
      for ($i = 0; $i -lt $reader.FieldCount; $i++) {
        $name = $reader.GetName($i)
        $value = if ($reader.IsDBNull($i)) { $null } else { $reader.GetValue($i) }
        $row[$name] = $value
      }
      $rows.Add([pscustomobject]$row)
    }

    return $rows
  } finally {
    if ($reader) {
      $reader.Dispose()
    }
    $command.Dispose()
  }
}

function Resolve-Connection($Override, $Port, $TimeoutSeconds) {
  $errors = @()

  foreach ($dsn in (Get-CandidateDsns $Override $Port)) {
    try {
      $connection = Test-OdbcConnection $dsn $TimeoutSeconds
      return @{
        state = "ok"
        dsn = $dsn
        connection = $connection
      }
    } catch {
      $errors += @{
        dsn = $dsn
        message = $_.Exception.Message
      }
    }
  }

  return @{
    state = "not_configured"
    dsn = $null
    connection = $null
    errors = $errors
  }
}

function Invoke-Probe($Request) {
  $timeoutSeconds = 10
  if ($null -ne $Request.timeout_seconds) {
    $timeoutSeconds = [int]$Request.timeout_seconds
  }
  $timeoutSeconds = [Math]::Max(5, $timeoutSeconds)
  $resolved = Resolve-Connection $Request.dsn_override $Request.port $timeoutSeconds
  if ($resolved.state -ne "ok") {
    return @{
      state = "not_configured"
      dsn = $null
      supported_sections = @()
      message = "No working Tally ODBC DSN was found."
      errors = $resolved.errors
    }
  }

  $supported = New-Object System.Collections.Generic.List[string]
  try {
    $sections = @()
    if ($null -ne $Request.sections) {
      $sections = $Request.sections
    }
    foreach ($section in $sections) {
      $sql = $null
      if ($Request.queries) {
        $sql = $Request.queries.$section
      }
      if (-not $sql) {
        continue
      }

      try {
        $null = Execute-OdbcQuery $resolved.connection $sql $timeoutSeconds
        $supported.Add($section)
      } catch {
      }
    }

    return @{
      state = "ok"
      dsn = $resolved.dsn
      supported_sections = $supported.ToArray()
      message = "ODBC probe succeeded."
    }
  } finally {
    if ($resolved.connection) {
      $resolved.connection.Dispose()
    }
  }
}

function Invoke-Query($Request) {
  $timeoutSeconds = 15
  if ($null -ne $Request.timeout_seconds) {
    $timeoutSeconds = [int]$Request.timeout_seconds
  }
  $timeoutSeconds = [Math]::Max(5, $timeoutSeconds)
  $resolved = Resolve-Connection $Request.dsn_override $Request.port $timeoutSeconds
  if ($resolved.state -ne "ok") {
    return @{
      state = "not_configured"
      dsn = $null
      rows = @()
      message = "No working Tally ODBC DSN was found."
      errors = $resolved.errors
    }
  }

  try {
    $rows = Execute-OdbcQuery $resolved.connection $Request.sql $timeoutSeconds
    if ($rows.Count -eq 0) {
      return @{
        state = "empty"
        dsn = $resolved.dsn
        rows = @()
      }
    }

    return @{
      state = "ok"
      dsn = $resolved.dsn
      rows = $rows
    }
  } finally {
    if ($resolved.connection) {
      $resolved.connection.Dispose()
    }
  }
}

while (($line = [Console]::In.ReadLine()) -ne $null) {
  if ([string]::IsNullOrWhiteSpace($line)) {
    continue
  }

  try {
    $request = $line | ConvertFrom-Json
    switch ($request.cmd) {
      "probe" {
        Write-JsonLine (Invoke-Probe $request)
      }
      "query" {
        Write-JsonLine (Invoke-Query $request)
      }
      "quit" {
        Write-JsonLine @{ state = "ok"; message = "bye" }
        break
      }
      default {
        Write-JsonLine @{ state = "error"; message = "Unknown command: $($request.cmd)" }
      }
    }
  } catch {
    Write-JsonLine @{
      state = "error"
      message = $_.Exception.Message
    }
  }
}
