"""xdev find-tools / rscript / mplus：在 Windows 上找统计软件、跑 R 脚本和 Mplus 模型、取回结果。

路径以配置 windows.rscript / windows.mplus 为准；没配时先跑 `xdev find-tools --write`。
"""
import json
import re
from pathlib import Path

from xdev_core import CFG, CONFIG, emitted, fail, ps_quote, run_ps
from xdev_tasks import print_summary, task

DEFAULT_ROOTS = [r"%ProgramFiles%", r"%ProgramFiles(x86)%", r"%LOCALAPPDATA%\Programs", r"%USERPROFILE%\Downloads"]


def find_tools(write: bool = False) -> int:
    roots = CFG.get("windows", {}).get("search_roots", DEFAULT_ROOTS)
    arr = "@(" + ", ".join(ps_quote(r) for r in roots) + ")"
    script = f"""$found = @{{ rscript = @(); mplus = @(); stata = @(); rstudio = @() }}
foreach ($r in {arr}) {{
  $d = Expand $r; if (-not (Test-Path -LiteralPath $d)) {{ continue }}
  # PS 5.1 里 -Include 配 -LiteralPath 不生效，所以按文件名自己筛（只认这几个确切的可执行文件名）
  Get-ChildItem -LiteralPath $d -Recurse -Depth 5 -File -Force -ErrorAction SilentlyContinue -Filter '*.exe' | ForEach-Object {{
    $n = $_.Name.ToLower()
    if ($n -eq 'rscript.exe' -and $_.DirectoryName -like '*\\bin\\x64') {{ $found.rscript += $_.FullName }}
    elseif ($n -eq 'mplus.exe') {{ $found.mplus += $_.FullName }}
    elseif ($n -in 'statamp-64.exe', 'statase-64.exe', 'statabe-64.exe', 'stata-64.exe') {{ $found.stata += $_.FullName }}
    elseif ($n -eq 'rstudio.exe') {{ $found.rstudio += ($_.FullName + ' ' + $_.VersionInfo.ProductVersion) }}
  }}
}}
Emit $found
"""
    rc, out, err, i = run_ps(script, timeout=290)
    rows = emitted(out)
    if rc != 0 or not rows:
        return fail(f"XDEV_FIND_FAILED rc={rc} job={i} {(out + err)[-600:]}", rc or 1)
    found = rows[-1]

    def rver(p):  # ...\R-4.5.2\bin\x64\Rscript.exe → (4, 5, 2)
        m = re.search(r"R-(\d+)\.(\d+)\.(\d+)", p)
        return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)
    def sver(p):  # ...\Stata18\...\StataMP-64.exe → 18
        m = re.search(r"Stata(\d+)", p)
        return int(m.group(1)) if m else 0
    best = {"rscript": max(found["rscript"], key=rver, default=None), "mplus": (found["mplus"] or [None])[0],
            "stata": max(found["stata"], key=sver, default=None)}
    print(json.dumps({"found": found, "chosen": best}, ensure_ascii=False, indent=1))
    if write:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        win = cfg.setdefault("windows", {})
        for k, v in best.items():
            if v and not win.get(k):  # 已有的配置不覆盖
                win[k] = v
        CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"XDEV_CONFIG_UPDATED {CONFIG}")
    return 0


def _tool(key: str) -> str:
    p = CFG.get("windows", {}).get(key)
    if not p:
        raise SystemExit(f"XDEV_NO_TOOL windows.{key} is not set; run: xdev find-tools --write")
    return p


def rscript(script: str, inputs: list, out_dir: str, args: list, max_seconds: int) -> int:
    name = Path(script).name
    cmd = f"& {ps_quote(_tool('rscript'))} --vanilla {ps_quote(name)} " + " ".join(ps_quote(a) for a in args) + " 2>&1"
    return print_summary(task([script, *inputs], cmd, out_dir, max_seconds=max_seconds))


def mplus(inp: str, inputs: list, out_dir: str, max_seconds: int) -> int:
    name = Path(inp).name
    outname = re.sub(r"\.inp$", "", name, flags=re.I) + ".out"
    cmd = f"& {ps_quote(_tool('mplus'))} {ps_quote(name)} {ps_quote(outname)}"
    summary = task([inp, *inputs], cmd, out_dir, max_seconds=max_seconds)
    out_file = Path(out_dir).expanduser() / outname
    if summary.get("ok") and out_file.is_file():
        text = out_file.read_text(encoding="utf-8", errors="replace")
        errors = [ln.strip() for ln in text.splitlines() if "*** ERROR" in ln]
        summary.update(mplus_out=str(out_file), terminated_normally="THE MODEL ESTIMATION TERMINATED NORMALLY" in text,
                       mplus_errors=errors[:10])
        if errors or not summary["terminated_normally"]:
            summary["ok"] = False
    elif summary.get("ok"):
        summary.update(ok=False, mplus_out=None, reason="no .out file came back")
    return print_summary(summary)
