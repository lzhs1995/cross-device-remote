"""xdev health / serve：30 秒看清通道状态；Mac 端服务没在跑时按配置原样启动。

health 退出码：0 服务存在且取件新鲜；2 Mac 端服务没在跑；3 Windows agent 取件过期或无法核实；1 其它。
缺回执只能说明结果未知，不能证明远端进程仍在执行，也不能作为在线证据。
"""
import calendar
import math
import os
import re
import subprocess
import sys
import time

from xdev_core import CFG, HERE, STATE


class ProcessUnverified(RuntimeError):
    """检查受限不等于服务已停，尤其不能据此再启动一个服务。"""


def _server_pids() -> list:
    port = str(CFG.get("port", 18092))
    try:
        result = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True,
                                text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProcessUnverified(type(exc).__name__) from exc
    if result.returncode:
        raise ProcessUnverified(f"ps exit {result.returncode}")
    out = result.stdout
    # 只算 python 进程本身（不算启动它的 sh -c 外壳）
    return [ln.split(None, 1)[0] for ln in out.splitlines()
            if "wbagent.py serve" in ln and f"--port {port}" in ln and not ln.split(None, 1)[1].startswith(("sh ", "/bin/sh "))]


def _serve_window():
    """最近一次 SERVE 的开始时间与 TTL；之后有 SERVE_END 就返回 None。"""
    log = STATE / "server.log"
    if not log.is_file():
        return None
    for ln in reversed(log.read_text(encoding="utf-8", errors="replace").splitlines()):
        if " SERVE_END" in ln:
            return None
        if " SERVE " in ln:
            m = re.search(r"ttl=(\d+)", ln)
            start = calendar.timegm(time.strptime(ln[:20], "%Y-%m-%dT%H:%M:%SZ"))  # 日志是 UTC
            return start, int(m.group(1)) if m else 0
    return None


def health() -> int:
    process_error = None
    try:
        pids = _server_pids()
    except ProcessUnverified as exc:
        pids, process_error = [], str(exc)
    win = _serve_window()
    seen = (STATE / "last_seen").read_text().split() if (STATE / "last_seen").exists() else []
    try:
        age = time.time() - float(seen[0])
        if not math.isfinite(age) or age < 0:
            age = None
    except (ValueError, IndexError):
        age = None
    try:
        cursor = int(next(s.split("=", 1)[1] for s in seen if s.startswith("after=")))
    except (ValueError, StopIteration):
        cursor = None
    peer = next((s.split("=", 1)[1] for s in seen if s.startswith("peer=")), "?")
    raw_inflight = next((s.split("=", 1)[1] for s in seen if s.startswith("inflight=")), "")
    inflight = {int(x) for x in raw_inflight.split(",") if x.isdecimal()}
    done = {int(p.stem) for p in (STATE / "results").glob("*.json")}
    pending = sorted(int(p.stem) for p in (STATE / "queue").glob("*.json") if int(p.stem) not in done)
    log = (STATE / "server.log").read_text(encoding="utf-8", errors="replace") if (STATE / "server.log").exists() else ""
    dispatched = {int(x) for x in re.findall(r"DISPATCH id=(\d+)", log)}
    # cursor 之前的缺回执保留 UNKNOWN，不能误当成当前还在跑的任务。
    active_reported = sorted(i for i in pending if i in inflight)
    historical = sorted(i for i in pending if cursor is not None and i <= cursor and i not in inflight)
    unconfirmed = sorted(i for i in pending if i in dispatched and i not in historical and i not in inflight)
    waiting = sorted(i for i in pending if i not in dispatched and i not in historical and i not in inflight)
    print(f"server   : {'process_unverified (' + process_error + ')' if process_error else 'running pid ' + ','.join(pids) if pids else 'NOT RUNNING'}"
          + (f", TTL left {int((win[0] + win[1] - time.time()) / 60)} min" if pids and win else ""))
    print(f"agent    : last poll {int(age)} s ago from {peer}" if age is not None else "agent    : no valid poll timestamp")
    print(f"queue    : dispatched_unconfirmed {unconfirmed or '-'}, waiting {waiting or '-'}, historical_unknown {historical or '-'}")
    if active_reported:
        print(f"inflight : reported_unconfirmed {active_reported} (poll report; not process liveness proof)")
    if process_error:
        print("XDEV_HEALTH PROCESS_UNVERIFIED (preserve service; do not start a duplicate)")
        return 3
    if not pids:
        print("XDEV_HEALTH SERVER_DOWN (start it: xdev serve)")
        return 2
    if age is None or age > 90:
        print("XDEV_HEALTH AGENT_STALE (no fresh poll; a synchronous job may still be running. Inspect its original receipt/process evidence; do not resubmit)")
        return 3
    print("XDEV_HEALTH OK (fresh poll; queue status does not prove background jobs are idle)")
    return 0


def serve() -> int:
    try:
        pids = _server_pids()
    except ProcessUnverified as exc:
        print(f"XDEV_SERVE_REFUSED process_unverified ({exc}); preserve the existing service")
        return 3
    if pids:
        print("XDEV_SERVE already running")
        return health()
    if (STATE / "STOP").exists():
        print(f"XDEV_SERVE_REFUSED {STATE / 'STOP'} exists (someone asked the server to stop); remove it on purpose first")
        return 2
    args = CFG.get("serve_args")
    if not args:
        print("XDEV_SERVE_REFUSED config has no serve_args")
        return 2
    env = dict(os.environ, WBAGENT_STATE=str(STATE))
    with open(STATE / f"serve-{time.strftime('%Y%m%dT%H%M%S')}.out", "ab") as out:
        p = subprocess.Popen([sys.executable, "-B", str(HERE / "wbagent.py"), "serve", *args], env=env,
                             stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    time.sleep(3)
    if p.poll() is not None:
        print(f"XDEV_SERVE_FAILED exited rc={p.returncode}; see {STATE}/serve-*.out")
        return 1
    print(f"XDEV_SERVE started pid {p.pid}; confirm a fresh Windows poll with xdev health")
    return 0
