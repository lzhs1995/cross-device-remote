# Fresh user-level installation. Never replaces an existing worker or private state.
param(
  [Parameter(Mandatory=$true)][string[]]$Server,
  [Parameter(Mandatory=$true)][string]$SecretFile,
  [string]$State = (Join-Path $env:LOCALAPPDATA 'XdevAgent'),
  [switch]$AutoStart,
  [switch]$Start
)
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Run this installer on Windows.' }
$origins = @()
foreach ($url in $Server) {
  $u = [Uri]$url
  if (-not $u.IsAbsoluteUri -or $u.Scheme -notin @('http','https') -or -not $u.Host -or $u.UserInfo -or $u.Query -or $u.Fragment -or $u.AbsolutePath -ne '/') {
    throw 'Server must be an HTTP(S) origin with no credentials, query or fragment.'
  }
  $origins += $u.GetLeftPart([UriPartial]::Authority)
}
$State = [IO.Path]::GetFullPath($State)
$pairing = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $SecretFile)).Trim()
if ($pairing.Length -lt 32) { throw 'Pairing secret must have at least 32 characters.' }
if (Test-Path -LiteralPath $State) { throw 'State already exists; preserve its jobs and use the documented maintenance procedure.' }
# 防止同一帳號建立兩個讀取同一佇列的 worker。
$legacy = Join-Path $env:LOCALAPPDATA 'StataWorkbenchAgent\agent2.json'
if (Test-Path -LiteralPath $legacy) { throw 'Legacy worker configuration found; do not install a second queue consumer.' }
if ($AutoStart) {
  foreach ($name in @('XdevAgentLogon','XdevAgentWatchdog')) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) { throw "Task already exists: $name" }
  }
}
New-Item -ItemType Directory -Path $State | Out-Null
# 從空 DACL 建立，不能只停用繼承而保留既有的 OWNER RIGHTS/Admin 顯式規則。
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$principals = @($sid, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'))
$directoryAcl = [Security.AccessControl.DirectorySecurity]::new()
$directoryAcl.SetAccessRuleProtection($true, $false)
$inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit
foreach ($identity in $principals) {
  $rule = [Security.AccessControl.FileSystemAccessRule]::new($identity,
    [Security.AccessControl.FileSystemRights]::FullControl, $inheritance,
    [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow)
  $directoryAcl.AddAccessRule($rule)
}
[IO.Directory]::SetAccessControl($State, $directoryAcl)
$secretTarget = Join-Path $State 'secret'
[IO.File]::WriteAllText($secretTarget, $pairing, (New-Object Text.UTF8Encoding($false)))
$pairing = $null
foreach ($file in @('wbagent-agent2.ps1','watchdog.ps1','start.vbs')) {
  Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination (Join-Path $State $file)
}
$cfg = @{state=$State;secret_file=$secretTarget;servers=@($origins);rendezvous=@();mutex='Local\XdevAgentV1'}
[IO.File]::WriteAllText((Join-Path $State 'agent2.json'), ($cfg | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding($false)))
# 複製來源可能附帶顯式 ACE；所有新檔也重建為兩個 SID 的受保護 DACL。
foreach ($file in @('secret','agent2.json','wbagent-agent2.ps1','watchdog.ps1','start.vbs')) {
  $fileAcl = [Security.AccessControl.FileSecurity]::new()
  $fileAcl.SetAccessRuleProtection($true, $false)
  foreach ($identity in $principals) {
    $rule = [Security.AccessControl.FileSystemAccessRule]::new($identity,
      [Security.AccessControl.FileSystemRights]::FullControl, [Security.AccessControl.AccessControlType]::Allow)
    $fileAcl.AddAccessRule($rule)
  }
  [IO.File]::SetAccessControl((Join-Path $State $file), $fileAcl)
}
if ($AutoStart) {
  $wscript = Join-Path $env:SystemRoot 'System32\wscript.exe'
  $vbs = Join-Path $State 'start.vbs'
  $action = New-ScheduledTaskAction -Execute $wscript -Argument ('"' + $vbs + '"')
  $who = [Security.Principal.WindowsIdentity]::GetCurrent().Name
  $principal = New-ScheduledTaskPrincipal -UserId $who -LogonType Interactive -RunLevel Limited
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
  Register-ScheduledTask -TaskName XdevAgentLogon -Action $action -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $who) -Settings $settings -Principal $principal | Out-Null
  $watch = New-ScheduledTaskAction -Execute $wscript -Argument ('"' + $vbs + '" watchdog')
  $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
  $bounded = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
  Register-ScheduledTask -TaskName XdevAgentWatchdog -Action $watch -Trigger $trigger -Settings $bounded -Principal $principal | Out-Null
}
if ($Start) { & (Join-Path $env:SystemRoot 'System32\wscript.exe') (Join-Path $State 'start.vbs') }
@{status='XDEV_WINDOWS_INSTALLED';state=$State;auto_start=[bool]$AutoStart;started=[bool]$Start;secret_printed=$false} | ConvertTo-Json -Compress
