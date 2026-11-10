# wbagent v2 Windows side (user level, no elevation, outbound HTTP only).
# Same job protocol as v1. Adds: secret read from a config file (not the command
# line), HMAC-signed polls, server rotation, and a signed rendezvous record so the
# agent follows the Mac when its address changes. -Hours 0 = no deadline (logon
# autostart). Stop: close this window, create <state>\STOP, or remove the
# user-level XdevAgentLogon task. Existing legacy installations are not migrated.
# Marker: WBAGENT_V2
param(
  [string]$Config = (Join-Path $env:LOCALAPPDATA 'XdevAgent\agent2.json'),
  [double]$Hours = 0
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$cfg = [IO.File]::ReadAllText($Config, [Text.Encoding]::UTF8) | ConvertFrom-Json
$State = if ($cfg.state) { [string]$cfg.state } else { Join-Path $env:LOCALAPPDATA 'XdevAgent' }
$Work = Join-Path $State 'work'
New-Item -ItemType Directory -Force -Path $Work | Out-Null
try { $Host.UI.RawUI.WindowTitle = 'Cross-device remote agent - close to stop' } catch { }

$MutexName = if ($cfg.mutex) { [string]$cfg.mutex } else { 'Local\XdevAgentV1' }
$mutex = New-Object System.Threading.Mutex($false, $MutexName)
$owned = $false
try { $owned = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $owned = $true }
if (-not $owned) { Write-Host 'WBAGENT_ALREADY_RUNNING'; exit 3 }

# 新安裝從私人密鑰檔讀取；兼容既有私有設定的 secret 欄位。
$SecretText = if ($cfg.secret_file) { [IO.File]::ReadAllText([string]$cfg.secret_file).Trim() } else { [string]$cfg.secret }
if ($SecretText.Length -lt 32) { throw 'Missing pairing secret (minimum 32 characters)' }
$Key = [Text.Encoding]::UTF8.GetBytes($SecretText)
$SecretText = $null
function Get-Mac([string]$Text) {
  $h = New-Object System.Security.Cryptography.HMACSHA256 -ArgumentList (,$Key)
  $b = $h.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text))
  ($b | ForEach-Object { $_.ToString('x2') }) -join ''
}
function Write-AgentLog([string]$Line) {
  $s = '{0} {1}' -f ([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')), $Line
  Add-Content -Path (Join-Path $State 'agent.log') -Value $s -Encoding UTF8
  Write-Host $s
}
function Limit-Text([string]$Text) {
  if ($Text.Length -gt 8000000) { return $Text.Substring(0, 8000000) + "`n[WBAGENT_TRUNCATED]" }
  return $Text
}
function ConvertTo-B64([string]$Text) { [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Text)) }

$LastFile = Join-Path $State 'last_id'
$Last = 0
$Claims = Join-Path $State 'claims'
New-Item -ItemType Directory -Force -Path $Claims | Out-Null
if (Test-Path $LastFile) {
  $savedLast = [IO.File]::ReadAllText($LastFile).Trim()
  if ($savedLast -notmatch '^\d+$' -or -not [int]::TryParse($savedLast, [ref]$Last)) {
    throw 'Invalid last_id; preserve state and reconcile the original jobs. Never reset the cursor.'
  }
}
# 每個簽名任務先獨占留一份意向；即使 cursor 寫到一半或舊值被恢復，重啟也不重跑。
foreach ($claim in @(Get-ChildItem -LiteralPath $Claims -Filter '*.json' -File)) {
  $claimId = 0
  if ($claim.BaseName -match '^\d+$' -and [int]::TryParse($claim.BaseName, [ref]$claimId)) {
    $Last = [Math]::Max($Last, $claimId)
  }
}
function Claim-Job([int]$Id, [string]$Sha) {
  $path = Join-Path $Claims ('{0}.json' -f $Id)
  if (Test-Path -LiteralPath $path) { return $false }
  $stream = [IO.File]::Open($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
  try {
    $record = @{id=$Id;sha256_prefix=$Sha;claimed_at=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($record)
    $stream.Write($bytes, 0, $bytes.Length)
    $stream.Flush($true)
  } finally { $stream.Dispose() }
  return $true
}
function Invoke-Job($Job) {
  $id = [int]$Job.id
  $timeout = [int]$Job.timeout
  $body = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String([string]$Job.script))
  $pre = '[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); $OutputEncoding = [Console]::OutputEncoding; $ProgressPreference = ''SilentlyContinue'''
  $file = Join-Path $Work ('job-{0}.ps1' -f $id)
  [IO.File]::WriteAllText($file, $pre + "`r`n" + $body, (New-Object System.Text.UTF8Encoding($true)))
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
  $psi.Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $file + '"'
  $psi.WorkingDirectory = $Work
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $true
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.StandardOutputEncoding = [Text.Encoding]::UTF8
  $psi.StandardErrorEncoding = [Text.Encoding]::UTF8
  $sw = [Diagnostics.Stopwatch]::StartNew()
  $p = [Diagnostics.Process]::Start($psi)
  $outTask = $p.StandardOutput.ReadToEndAsync()
  $errTask = $p.StandardError.ReadToEndAsync()
  if ($p.WaitForExit($timeout * 1000)) {
    $p.WaitForExit()
    $rc = $p.ExitCode
  } else {
    & taskkill.exe /PID $p.Id /T /F | Out-Null
    $rc = 124
  }
  [void]$outTask.Wait(10000); [void]$errTask.Wait(10000)
  $out = if ($outTask.IsCompleted) { $outTask.Result } else { '' }
  $err = if ($errTask.IsCompleted) { $errTask.Result } else { '' }
  if ($rc -eq 124) { $err += "`n[WBAGENT_TIMEOUT after $timeout s]" }
  $ms = $sw.ElapsedMilliseconds
  Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
  return @{ id = $id; rc = $rc; out = (ConvertTo-B64 (Limit-Text $out)); err = (ConvertTo-B64 (Limit-Text $err)); ms = $ms }
}

function Send-Result($R) {
  $mac = Get-Mac ('res|{0}|{1}|{2}|{3}' -f $R.id, $R.rc, $R.out, $R.err)
  $json = '{"id":' + $R.id + ',"rc":' + $R.rc + ',"ms":' + $R.ms + ',"out":"' + $R.out + '","err":"' + $R.err + '","mac":"' + $mac + '"}'
  $bytes = [Text.Encoding]::UTF8.GetBytes($json)
  for ($n = 1; $n -le 3; $n++) {
    try {
      Invoke-WebRequest -UseBasicParsing -Method Post -Uri ($script:Server + '/result') -Body $bytes -ContentType 'application/json' -TimeoutSec 20 | Out-Null
      return $true
    } catch {
      Write-AgentLog ('post id={0} try {1} failed: {2}' -f $R.id, $n, $_.Exception.Message)
      Select-NextServer
      if ($n -lt 3) { Start-Sleep -Seconds 2 }
    }
  }
  return $false
}

# Server choice: saved last-good server, then the signed rendezvous record, then the static list
$ServerFile = Join-Path $State 'server.txt'
$Static = @($cfg.servers | ForEach-Object { [string]$_ })
$script:Server = $Static[0]
if (Test-Path $ServerFile) { $saved = (Get-Content $ServerFile -Raw).Trim(); if ($saved) { $script:Server = $saved } }
$SavedServer = $script:Server
$script:StaticIx = -1
$script:RvTs = [long]0
$script:RvNext = [DateTime]::MinValue
$script:RvLogNext = [DateTime]::MinValue
function Get-Rendezvous {
  # Record published by the Mac: {"server":..., "ts":..., "mac":HMAC("rv|server|ts")}; at most one lookup per 2 minutes
  if ([DateTime]::UtcNow -lt $script:RvNext) { return $null }
  $script:RvNext = [DateTime]::UtcNow.AddSeconds(120)
  $errs = @()
  foreach ($u in @($cfg.rendezvous)) {
    try {
      $r = Invoke-WebRequest -UseBasicParsing -Uri ([string]$u) -TimeoutSec 20 -Headers @{ 'Cache-Control' = 'no-cache' }
      $o = ([string]$r.Content) | ConvertFrom-Json
      if ($o.files) { $o = ([string]$o.files.'wbagent-rendezvous.json'.content) | ConvertFrom-Json }
      $expect = Get-Mac ('rv|{0}|{1}' -f $o.server, $o.ts)
      if ($expect -ne [string]$o.mac) { $errs += ('bad mac ' + $u); continue }
      if ([long]$o.ts -lt $script:RvTs) { continue }
      $script:RvTs = [long]$o.ts
      return [string]$o.server
    } catch {
      $errs += ('{0}: {1}' -f $u, $_.Exception.Message)
    }
  }
  if ($errs.Count -gt 0 -and [DateTime]::UtcNow -ge $script:RvLogNext) {
    Write-AgentLog ('rendezvous failed: ' + ($errs -join ' | '))
    $script:RvLogNext = [DateTime]::UtcNow.AddMinutes(10)
  }
  return $null
}

function Select-NextServer {
  $rv = Get-Rendezvous
  if ($rv -and $rv -ne $script:Server) { $script:Server = $rv; return }
  if ($Static.Count -eq 0) { return }
  $script:StaticIx = ($script:StaticIx + 1) % $Static.Count
  $script:Server = $Static[$script:StaticIx]
}

$Deadline = if ($Hours -gt 0) { [DateTime]::UtcNow.AddHours($Hours) } else { [DateTime]::MaxValue }
Write-AgentLog ('WBAGENT_V2 start pid={0} server={1} last={2} until={3}' -f $PID, $script:Server, $Last, $Deadline.ToString('o'))
$fails = 0
while ([DateTime]::UtcNow -lt $Deadline -and -not (Test-Path (Join-Path $State 'STOP'))) {
  foreach ($pendingResult in @(Get-ChildItem -LiteralPath $State -Filter 'result-*.json' -File)) {
    if (Test-Path ($pendingResult.FullName + '.sent')) { continue }
    $prior = [IO.File]::ReadAllText($pendingResult.FullName) | ConvertFrom-Json
    if (Send-Result $prior) { [IO.File]::WriteAllText($pendingResult.FullName + '.sent', [DateTime]::UtcNow.ToString('o')) }
  }
  $t = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
  $pm = Get-Mac ('poll|{0}|{1}' -f $Last, $t)
  try {
    $r = Invoke-WebRequest -UseBasicParsing -Uri ('{0}/next?after={1}&t={2}&m={3}' -f $script:Server, $Last, $t, $pm) -TimeoutSec 40
    $fails = 0
    if (-not (Test-Path -LiteralPath $ServerFile) -or $script:Server -ne $SavedServer) {
      Set-Content -Path $ServerFile -Value $script:Server -Encoding ASCII
      $SavedServer = $script:Server
      Write-AgentLog ('server now ' + $script:Server)
    }
  } catch {
    $fails++
    if ($fails -le 3 -or $fails % 60 -eq 0) { Write-AgentLog ('poll error #{0} {1}: {2}' -f $fails, $script:Server, $_.Exception.Message) }
    if ($fails % 3 -eq 0) {
      $before = $script:Server
      Select-NextServer
      if ($script:Server -ne $before -and $fails -le 30) { Write-AgentLog ('try server ' + $script:Server) }
    }
    Start-Sleep -Seconds 5
    continue
  }
  if ($r.StatusCode -ne 200) { continue }
  $job = $r.Content | ConvertFrom-Json
  $expect = Get-Mac ('cmd|{0}|{1}|{2}' -f $job.id, $job.timeout, $job.script)
  if ($expect -ne [string]$job.mac -or [int]$job.id -le $Last) {
    Write-AgentLog ('REJECT id={0} (bad mac or replay)' -f $job.id)
    Start-Sleep -Seconds 30
    continue
  }
  $raw = [Convert]::FromBase64String([string]$job.script)
  $sha = (([Security.Cryptography.SHA256]::Create().ComputeHash($raw) | ForEach-Object { $_.ToString('x2') }) -join '').Substring(0, 16)
  if (-not (Claim-Job ([int]$job.id) $sha)) {
    $Last = [Math]::Max($Last, [int]$job.id)
    Write-AgentLog ('SKIP_CLAIMED id={0}; preserve original outcome' -f $job.id)
    continue
  }
  $Last = [int]$job.id
  Set-Content -Path $LastFile -Value $Last -Encoding ASCII
  if ([Text.Encoding]::UTF8.GetString($raw) -eq 'WBAGENT_EXIT') {
    [void](Send-Result @{ id = [int]$job.id; rc = 0; out = (ConvertTo-B64 'WBAGENT_EXIT_OK'); err = ''; ms = 0 })
    Write-AgentLog ('EXIT requested by id={0}' -f $job.id)
    break
  }
  Write-AgentLog ('RUN id={0} sha={1} timeout={2}' -f $job.id, $sha, $job.timeout)
  # 獨占意向及游標均已保存，之後才啟動子程序。
  $res = Invoke-Job $job
  $resultPath = Join-Path $State ('result-{0}.json' -f $res.id)
  [IO.File]::WriteAllText($resultPath, ($res | ConvertTo-Json -Compress), (New-Object Text.UTF8Encoding($false)))
  $ok = Send-Result $res
  if ($ok) { [IO.File]::WriteAllText($resultPath + '.sent', [DateTime]::UtcNow.ToString('o')) }
  Write-AgentLog ('DONE id={0} rc={1} ms={2} posted={3}' -f $res.id, $res.rc, $res.ms, $ok)
}
Write-AgentLog 'WBAGENT_V2 stop'
$mutex.ReleaseMutex()
