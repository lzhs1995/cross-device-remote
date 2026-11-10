"""xdev detach / tail / wait / task：Windows 上的长任务。

detach：正文写到 Windows 本地文件，隐藏 powershell 跑，stdout/stderr 进日志，结束时写 NAME.rc；本次调用立即返回，
        不占单线程的 agent（agent 一次只执行一个 job，长任务不 detach 会挡住其它人）。
task：建远端工作目录 → push 输入 → detach 跑命令 → wait → pull-dir 取回整个工作目录。
"""
import json
import re
import sys
import time

from xdev_core import WIN_ROOT, emitted, fail, ps_quote, run_ps
from xdev_files import pull_dir, push

NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def detach(name: str, body: str) -> int:
    if not NAME_RE.match(name):
        return fail("XDEV_BAD_NAME use letters, digits, . _ -", 2)
    # 后台正文另起 powershell，拿不到 run 的前导；补上 Expand/$XRoot，否则 Expand 会落到 Windows 自带的 expand.exe
    body = ("[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)\n"
            "function Expand([string]$p) { [Environment]::ExpandEnvironmentVariables($p) }\n"
            f"$XRoot = Expand {ps_quote(WIN_ROOT)}\n" + body)
    script = f"""$J = Join-Path $XRoot 'jobs\\{name}'
if (Test-Path -LiteralPath $J) {{ Emit @{{ ok = $false; reason = 'NAME_EXISTS'; dir = $J }}; exit 3 }}
New-Item -ItemType Directory -Force -Path $J | Out-Null
$Body = Join-Path $J 'body.ps1'; $Run = Join-Path $J 'runner.ps1'
[IO.File]::WriteAllText($Body, {ps_quote(body)}, (New-Object System.Text.UTF8Encoding($true)))
$r = "`$p = Start-Process -FilePath powershell.exe -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File ""$Body""' -WindowStyle Hidden -PassThru -RedirectStandardOutput ""$J\\out.log"" -RedirectStandardError ""$J\\err.log""`r`n`$null = `$p.Handle; `$p.WaitForExit()`r`n[IO.File]::WriteAllText(""$J\\rc.txt"", [string]`$p.ExitCode + ' ' + [DateTime]::UtcNow.ToString('o'))"
[IO.File]::WriteAllText($Run, $r, (New-Object System.Text.UTF8Encoding($true)))
$p = Start-Process -FilePath powershell.exe -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "' + $Run + '"') -WindowStyle Hidden -PassThru
Emit @{{ ok = $true; name = '{name}'; dir = $J; runner_pid = $p.Id; at = [DateTime]::UtcNow.ToString('o') }}
"""
    rc, out, err, i = run_ps(script, timeout=60)
    rows = emitted(out)
    if rc == 0 and rows and rows[-1].get("ok"):
        print(f"XDEV_DETACHED {name} dir={rows[-1]['dir']} pid={rows[-1]['runner_pid']} job={i}")
        return 0
    return fail(f"XDEV_DETACH_FAILED rc={rc} job={i} {rows[-1] if rows else (out + err)[-800:]}", rc or 1)


def status(name: str, nbytes: int = 4000) -> dict | None:
    script = f"""$J = Join-Path $XRoot 'jobs\\{name}'
if (-not (Test-Path -LiteralPath $J)) {{ Emit @{{ ok = $false; reason = 'NO_SUCH_JOB' }}; exit 3 }}
function TailOf([string]$f) {{
  if (-not (Test-Path -LiteralPath $f)) {{ return '' }}
  $fs = New-Object IO.FileStream($f, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
  try {{ $n = [Math]::Min($fs.Length, {nbytes}); [void]$fs.Seek(-$n, [IO.SeekOrigin]::End); $b = New-Object byte[] $n; [void]$fs.Read($b, 0, $n) }} finally {{ $fs.Dispose() }}
  [Text.Encoding]::UTF8.GetString($b)
}}
$rcf = Join-Path $J 'rc.txt'
if (Test-Path -LiteralPath $rcf) {{ $parts = (Get-Content -LiteralPath $rcf -Raw).Trim() -split ' '; $state = 'DONE'; $rc = $parts[0]; $fin = $parts[1] }}
elseif (@(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object {{ $_.CommandLine -like ('*' + $J + '\\runner.ps1*') }}).Count) {{ $state = 'RUNNING'; $rc = $null; $fin = $null }}
else {{
  # runner 刚退出、rc.txt 可能晚一点才写好：再看一次，避免把刚结束的任务报成 LOST
  Start-Sleep -Seconds 2
  if (Test-Path -LiteralPath $rcf) {{ $parts = (Get-Content -LiteralPath $rcf -Raw).Trim() -split ' '; $state = 'DONE'; $rc = $parts[0]; $fin = $parts[1] }}
  else {{ $state = 'LOST'; $rc = $null; $fin = $null }}
}}
Emit @{{ ok = $true; state = $state; rc = $rc; finished = $fin; out = (TailOf (Join-Path $J 'out.log')); err = (TailOf (Join-Path $J 'err.log')) }}
"""
    rc, out, err, i = run_ps(script, timeout=60)
    rows = emitted(out)
    return rows[-1] if rows else {"ok": False, "reason": f"rc={rc} job={i} {(out + err)[-400:]}"}


def tail(name: str) -> int:
    s = status(name)
    if not s.get("ok"):
        return fail(f"XDEV_TAIL_FAILED {s}", 3)
    print(s["out"].rstrip())
    if s["err"].strip():
        print("--- stderr ---\n" + s["err"].rstrip())
    print(f"XDEV_STATE {name} {s['state']} rc={s['rc']} finished={s['finished']}")
    return 0


def wait(name: str, max_seconds: int = 3600) -> int:
    # 首查 3 s 再按 6/12/24/48/60 s 退避：短任务少等 5–15 s，长任务每分钟最多一个 status 作业
    end, gap = time.time() + max_seconds, 3
    while True:
        s = status(name, nbytes=400)
        if not s.get("ok"):
            return fail(f"XDEV_WAIT_FAILED {s}", 3)
        if s["state"] != "RUNNING":
            print(f"XDEV_STATE {name} {s['state']} rc={s['rc']} finished={s['finished']}")
            return int(s["rc"]) if s["state"] == "DONE" else 7
        if time.time() + gap > end:
            print(f"XDEV_STILL_RUNNING {name} (later: xdev wait {name})")
            return 124
        print(f"  {time.strftime('%H:%M:%S')} {name} running; last: {s['out'].strip().splitlines()[-1:] }", file=sys.stderr)
        time.sleep(gap)
        gap = min(gap * 2, 60)


def task(inputs: list, command: str, out_dir: str, name: str | None = None, max_seconds: int = 3600) -> dict:
    """push 输入到新的远端工作目录，在里面 detach 跑 command（PowerShell），等完再把整个目录取回 out_dir。"""
    name = name or "t" + time.strftime("%Y%m%d-%H%M%S")
    wd = rf"%LOCALAPPDATA%\xdev\work\{name}"
    for f in inputs:
        if push(f, wd + "\\" + f.replace("\\", "/").rsplit("/", 1)[-1]):
            return {"ok": False, "stage": "push", "input": f}
    body = f"$ErrorActionPreference = 'Continue'\nSet-Location -LiteralPath ([Environment]::ExpandEnvironmentVariables({ps_quote(wd)}))\n{command}\nexit $LASTEXITCODE\n"
    if detach(name, body):
        return {"ok": False, "stage": "detach"}
    rc = wait(name, max_seconds)
    if rc == 124:
        return {"ok": False, "stage": "wait", "name": name, "hint": f"xdev wait {name}, then xdev pull-dir {wd} {out_dir}"}
    tail(name)
    prc = pull_dir(wd, out_dir)
    return {"ok": prc == 0, "name": name, "rc": rc, "remote_dir": wd, "local_dir": out_dir, "pull_rc": prc}


def print_summary(summary: dict) -> int:
    print("XDEV_TASK " + json.dumps(summary, ensure_ascii=False))
    return 0 if summary.get("ok") and summary.get("rc") == 0 else (summary.get("rc") or 1)
