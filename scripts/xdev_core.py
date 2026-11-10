"""xdev 公共部分：配置、入队、等回执、PowerShell 字符串引用。只用标准库。

配置文件：$XDEV_CONFIG，默认 ~/.config/cross-device-remote/config.json（机器相关，不在 skill 目录里）。
密钥：只从 <state_dir>/secret 读（wbagent init 生成，0600），不写进配置，也不打印。
"""
import base64
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get("XDEV_CONFIG", "~/.config/cross-device-remote/config.json")).expanduser()


def load_config() -> dict:
    if not CONFIG.is_file():
        sys.exit(f"XDEV_NO_CONFIG {CONFIG}: copy {HERE.parent / 'config.example.json'} there and edit state_dir")
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    cfg["state_dir"] = str(Path(cfg["state_dir"]).expanduser())
    return cfg


CFG = load_config()
os.environ["WBAGENT_STATE"] = CFG["state_dir"]  # 让同目录的 wbagent 用同一个状态目录（同一个密钥、队列）
sys.path.insert(0, str(HERE))
import wbagent  # noqa: E402

STATE = Path(CFG["state_dir"])
WIN_ROOT = CFG.get("windows", {}).get("work_root", r"%LOCALAPPDATA%\xdev")
AGENT_STATE = CFG.get("windows", {}).get("agent_state", r"%LOCALAPPDATA%\StataWorkbenchAgent")


def ps_quote(s: str) -> str:
    """PowerShell 单引号字面量：内部单引号写两遍；不展开变量，适合中文和带空格的路径。"""
    return "'" + str(s).replace("'", "''") + "'"


# 每个 xdev 任务开头都带这一段：UTF-8 输出；$XRoot 是 Windows 上的工作根目录；Expand 展开 %LOCALAPPDATA% 这类写法
PREAMBLE = f"""$ErrorActionPreference = 'Stop'
$env:HTTPS_PROXY = ''; $env:HTTP_PROXY = ''
function Expand([string]$p) {{ [Environment]::ExpandEnvironmentVariables($p) }}
$XRoot = Expand {ps_quote(WIN_ROOT)}
New-Item -ItemType Directory -Force -Path $XRoot | Out-Null
$MacServer = (Get-Content -LiteralPath (Join-Path (Expand {ps_quote(AGENT_STATE)}) 'server.txt') -Raw).Trim()
function Emit($o) {{ 'XDEV_JSON ' + ($o | ConvertTo-Json -Compress -Depth 6) }}
"""


def run_ps(script: str, timeout: int = 300, wait: int | None = None, quiet: bool = False):
    """入队一段 PowerShell，等回执。返回 (rc, stdout, stderr, job_id)；等不到时 rc=124。"""
    b64 = base64.b64encode((PREAMBLE + script).encode("utf-8")).decode()
    i = wbagent.enqueue(b64, timeout)
    res = STATE / "results" / f"{i}.json"
    if not quiet:
        # 先记录原队列ID；观察中断后仍可核原次，不能另行入队。
        print(f"XDEV_QUEUED id={i}", file=sys.stderr, flush=True)
    # 默认多等 10 分钟：agent 一次只跑一个 job，别的客户端排在前面的任务会占住它
    end = time.monotonic() + (wait if wait is not None else timeout + 600)
    r = None
    while True:
        try:
            # 现役server可能刚创建文件而尚未写完；只重读原回执，不重提任务。
            r = json.loads(res.read_text(encoding="utf-8"))
            break
        except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError):
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.5, remaining))
    if r is None:
        if not quiet:
            print(f"XDEV_WAITING id={i} (later: xdev result {i})", file=sys.stderr)
        return 124, "", "", i
    # Windows 输出是 CRLF；统一成 LF，免得调用方按行比对时多出 \r
    out = base64.b64decode(r["out"]).decode("utf-8", "replace").replace("\r\n", "\n")
    err = base64.b64decode(r["err"]).decode("utf-8", "replace").replace("\r\n", "\n")
    return int(r["rc"]), out, err, i


def emitted(out: str) -> list:
    """取出脚本里 Emit 打印的 JSON 行。"""
    rows = []
    # JSON 中的 Unicode NEL/LS/PS 是字符串内容，不是协议换行。
    for line in out.split("\n"):
        if line.startswith("XDEV_JSON "):
            rows.append(json.loads(line[len("XDEV_JSON "):]))
    return rows


def fail(msg: str, rc: int = 1):
    print(msg, file=sys.stderr)
    return rc
