# Restart only an absent owned worker. The worker itself retries network failures.
# Never kill a live process based on old logs, and never reset the queue cursor.
$ErrorActionPreference = 'Stop'
if (Test-Path (Join-Path $PSScriptRoot 'STOP')) { exit 0 }
$agent = Join-Path $PSScriptRoot 'wbagent-agent2.ps1'
$procs = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object {
  $_.CommandLine -and $_.CommandLine.IndexOf($agent, [StringComparison]::OrdinalIgnoreCase) -ge 0
})
if ($procs.Count -eq 0) {
  Add-Content -Path (Join-Path $PSScriptRoot 'watchdog.log') -Value ([DateTime]::UtcNow.ToString('o') + ' AGENT_ABSENT_START')
  & (Join-Path $env:SystemRoot 'System32\wscript.exe') (Join-Path $PSScriptRoot 'start.vbs')
}
