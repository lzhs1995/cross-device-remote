Option Explicit
Dim fso, base, script, shell, command
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
script = "wbagent-agent2.ps1"
If WScript.Arguments.Count > 0 Then
  If WScript.Arguments(0) = "watchdog" Then script = "watchdog.ps1"
End If
Set shell = CreateObject("WScript.Shell")
command = """" & shell.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\WindowsPowerShell\v1.0\powershell.exe"" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & base & "\" & script & """"
If script = "wbagent-agent2.ps1" Then command = command & " -Config """ & base & "\agent2.json"""
shell.Run command, 0, False
