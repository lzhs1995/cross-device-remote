#!/usr/bin/env python3
"""xdev：在 Mac 上让 Windows 实体机执行命令、跑长任务、搬文件、跑 R / Mplus。

所有子命令都只打印文字结果，最后一行是 XDEV_<状态> 开头的摘要，退出码与远端一致（124 = 还没等到）。
用法见 ../SKILL.md。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="xdev", description="Mac → Windows remote execution over the wbagent channel")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health", help="server / agent / queue state in one screen")
    sub.add_parser("serve", help="start the Mac server with config serve_args if it is not running")
    p = sub.add_parser("run", help="run a short PowerShell job (<= a few minutes) and print its output")
    p.add_argument("file", nargs="?")
    p.add_argument("-c", "--command")
    p.add_argument("--timeout", type=int, default=300, help="kill the job on Windows after this many seconds")
    p.add_argument("--wait", type=int, help="seconds to wait here (default timeout+600; queued jobs ahead count)")
    p = sub.add_parser("result", help="fetch a late result by job id")
    p.add_argument("id", type=int)
    p.add_argument("--wait", type=int, default=110)
    p = sub.add_parser("detach", help="start a long PowerShell job in the background on Windows")
    p.add_argument("name")
    p.add_argument("file", nargs="?")
    p.add_argument("-c", "--command")
    for n in ("tail", "wait"):
        p = sub.add_parser(n, help=f"{n} a detached job")
        p.add_argument("name")
        if n == "wait":
            p.add_argument("--max", type=int, default=3600)
    p = sub.add_parser("push", help="copy a Mac file to Windows (sha256 checked)")
    p.add_argument("local")
    p.add_argument("remote", nargs="?")
    p.add_argument("--overwrite", action="store_true")
    for n in ("pull", "pull-dir"):
        what = "one Windows file" if n == "pull" else "a whole Windows directory (zipped, then unpacked)"
        p = sub.add_parser(n, help=f"copy {what} back to the Mac (sha256 checked)")
        p.add_argument("remote")
        p.add_argument("local")
        if n == "pull":
            p.add_argument("--overwrite", action="store_true")
    p = sub.add_parser("find-tools", help="locate Rscript / Mplus / Stata on Windows")
    p.add_argument("--write", action="store_true", help="save the chosen paths into the config (existing keys kept)")
    p = sub.add_parser("rscript", help="push an R script (+inputs), run it on Windows, pull the work dir back")
    p.add_argument("script")
    p.add_argument("--input", action="append", default=[])
    p.add_argument("--out", required=True)
    p.add_argument("--arg", action="append", default=[])
    p.add_argument("--max", type=int, default=3600)
    p = sub.add_parser("mplus", help="push an .inp (+data), run Mplus on Windows, pull the work dir back, check the .out")
    p.add_argument("inp")
    p.add_argument("--input", action="append", default=[])
    p.add_argument("--out", required=True)
    p.add_argument("--max", type=int, default=3600)
    import xdev_clauder
    xdev_clauder.add_parser(sub)
    a = ap.parse_args(argv)

    if a.cmd == "rstudio":
        try:
            return xdev_clauder.run(a)
        except (FileExistsError, FileNotFoundError, ValueError) as exc:
            print(f"XDEV_RSTUDIO_REFUSED {exc}; preserve the original state and request id", file=sys.stderr)
            return 3
    if a.cmd in ("health", "serve"):
        import xdev_health
        return getattr(xdev_health, a.cmd)()
    if a.cmd in ("run", "detach"):
        if not (a.file or a.command):
            ap.error(f"{a.cmd} needs a file or -c")
        body = Path(a.file).read_text(encoding="utf-8") if a.file else a.command
        if a.cmd == "detach":
            import xdev_tasks
            return xdev_tasks.detach(a.name, body)
        import xdev_core
        rc, out, err, i = xdev_core.run_ps(body, timeout=a.timeout, wait=a.wait)
        sys.stdout.write(out)
        sys.stderr.write(err)
        print(f"XDEV_RC job={i} rc={rc}", file=sys.stderr)
        return rc
    if a.cmd == "result":
        import xdev_core
        return xdev_core.wbagent.await_result(a.id, a.wait)
    if a.cmd in ("tail", "wait"):
        import xdev_tasks
        return xdev_tasks.tail(a.name) if a.cmd == "tail" else xdev_tasks.wait(a.name, a.max)
    import xdev_files
    if a.cmd == "push":
        return xdev_files.push(a.local, a.remote, a.overwrite)
    if a.cmd == "pull":
        return xdev_files.pull(a.remote, a.local, a.overwrite)
    if a.cmd == "pull-dir":
        return xdev_files.pull_dir(a.remote, a.local)
    import xdev_tools
    if a.cmd == "find-tools":
        return xdev_tools.find_tools(a.write)
    if a.cmd == "rscript":
        return xdev_tools.rscript(a.script, a.input, a.out, a.arg, a.max)
    return xdev_tools.mplus(a.inp, a.input, a.out, a.max)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PermissionError as e:
        # Codex 默认 workspace-write 沙箱禁 ps 与网络：说清原因，不要让 agent 当成通道故障去「修」
        print(f"XDEV_SANDBOX_BLOCKED {e}; xdev needs process listing, loopback network and its state_dir."
              " Run the agent with full access (Codex: /permissions -> Full Access), then retry.")
        sys.exit(1)
