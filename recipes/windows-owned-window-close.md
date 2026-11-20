# Recipe: close one authorized Windows window

[Project](../README.md) · [Concurrency and evidence](../docs/concurrency.md)

Use this only for a window the user has authorized you to close. Preserve its exact PID, process birth time and executable path from a fresh read-only observation. A window title, old PID, process name or stale `running` record is insufficient. Other sessions keep their ownership.

1. Read the target's PID, UTC birth time, full executable path, `Responding` and main-window handle; inspect the visible target for an existing dialog. Save the observation privately. If identity or responsiveness cannot be established, or a save/confirmation dialog is already present, preserve the window and stop this close attempt.
2. Run the following script once with those exact values and a fresh, private intent-file path. The exclusive intent file preserves the one-call budget across interruption. Do not remove it to retry.
3. Inspect the receipt. A remaining window or save dialog stays open for the user; do not send keys, answer dialogs, force termination or repeat the close. If the transport response is lost, recover the original request before any further action.

Save as `close-owned-window.ps1` and invoke through the already authorized Windows execution route. Its parameters contain observed values, not guessed examples:

```powershell
param(
  [Parameter(Mandatory=$true)][int]$ExpectedPid,
  [Parameter(Mandatory=$true)][DateTimeOffset]$ExpectedBirthUtc,
  [Parameter(Mandatory=$true)][string]$ExpectedExe,
  [Parameter(Mandatory=$true)][string]$IntentFile
)
$ErrorActionPreference = 'Stop'
$p = Get-Process -Id $ExpectedPid -ErrorAction Stop
$birth = $p.StartTime.ToUniversalTime()
$exe = $p.MainModule.FileName
if ($birth.Ticks -ne $ExpectedBirthUtc.UtcDateTime.Ticks -or
    -not [string]::Equals($exe, $ExpectedExe, [StringComparison]::OrdinalIgnoreCase)) {
  throw 'Target identity differs from the authorized observation'
}
$p.Refresh()
if ($p.HasExited -or -not $p.Responding -or $p.MainWindowHandle -eq [IntPtr]::Zero) {
  throw 'Target is not an observed responding window'
}
$intent = [ordered]@{
  pid = $ExpectedPid; birth_utc = $birth.ToString('o'); exe = $exe
  responding = $true; action = 'CloseMainWindowOnce'; wait_ms = 10000
}
$f = [IO.File]::Open($IntentFile, [IO.FileMode]::CreateNew,
                    [IO.FileAccess]::Write, [IO.FileShare]::None)
try {
  $bytes = [Text.Encoding]::UTF8.GetBytes(($intent | ConvertTo-Json -Compress))
  $f.Write($bytes, 0, $bytes.Length)
  $f.Flush($true)
} finally { $f.Dispose() }
$requested = $p.CloseMainWindow()
$exited = $p.WaitForExit(10000)
[ordered]@{
  pid = $ExpectedPid; birth_utc = $birth.ToString('o'); exe = $exe
  close_requested = $requested; exited_within_10_seconds = $exited
  outcome = $(if ($exited) { 'EXIT_OBSERVED' } else { 'WINDOW_OR_DIALOG_PRESERVED' })
} | ConvertTo-Json -Compress
```

Use a transport budget longer than the 10-second observation and save its original receipt. The script calls `CloseMainWindow()` once and waits at most ten seconds; it contains no kill or retry. Read-only verification after a timeout is permitted. A close request returning true is not proof of exit, and observed exit is not proof that unsaved content was saved.

Process inspection can race with a short asynchronous process exiting. An unavailable `MainModule` after exit is not evidence that the process remains live. For a model, use its original output/exit receipt; for this action, retain the exact close intent and observation without inventing a result.
