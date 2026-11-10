#!/usr/bin/env python3
"""wbagent Mac 端：签名命令队列 + 结果收件（无提权的 Windows 文本通道）。
状态目录由环境变量 WBAGENT_STATE 指定；入队用 os.link 独占取号（多个客户端同时入队不会互相覆盖）。

serve：只接受 --allow-ip 的连接；GET /next 长轮询取下一条已签名命令，POST /result 收回执并验签。
run：把一个 PowerShell 脚本签名入队，等回执，打印 stdout/stderr，退出码同远端。
签名：HMAC-SHA256(secret, "cmd|id|timeout|script_b64") / ("res|id|rc|out_b64|err_b64")。
"""
import argparse, base64, hashlib, hmac, json, os, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(os.environ["WBAGENT_STATE"]).expanduser() if os.environ.get("WBAGENT_STATE") else Path(__file__).resolve().parent / "wbagent-state"


def secret() -> bytes:
    return (ROOT / "secret").read_text().strip().encode()


def sign(*parts) -> str:
    return hmac.new(secret(), "|".join(str(p) for p in parts).encode(), hashlib.sha256).hexdigest()


def log(msg: str) -> None:
    with open(ROOT / "server.log", "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + msg + "\n")


def pending_after(last: int):
    ids = sorted(int(p.stem) for p in (ROOT / "queue").glob("*.json"))
    for i in ids:
        if i > last and not (ROOT / "results" / f"{i}.json").exists():
            return json.loads((ROOT / "queue" / f"{i}.json").read_text())
    return None


class Handler(BaseHTTPRequestHandler):
    allow_ip = ""
    last_peer = ""
    require_signed_poll = False
    own_net = None

    def log_message(self, fmt, *args):  # 默认访问日志关掉，只写 server.log
        pass

    def _send(self, code: int, body: bytes = b"") -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _allowed(self) -> bool:
        import ipaddress
        # --allow-ip 可以是单个地址、网段或逗号分隔的多个网段（Mac 会换网）；命令仍靠 HMAC 验签
        nets = [ipaddress.ip_network(n.strip(), strict=False) for n in self.allow_ip.split(",") if n.strip()]
        if Handler.own_net:  # --allow-own-subnet：Mac 当前所在网段，换网后自动跟着变
            nets.append(Handler.own_net)
        if not any(ipaddress.ip_address(self.client_address[0]) in n for n in nets):
            # 拒绝时不把带签名的查询串写进日志
            log(f"REFUSED {self.client_address[0]} {self.command} {self.path.split('?', 1)[0]}")
            self._send(403)
            return False
        return True

    def do_GET(self):
        if not self._allowed():
            return
        if self.path.startswith("/file/"):
            # 只发 files/ 目录里的单层文件名；接收端按 sha256 自己核
            name = self.path[len("/file/"):]
            p = ROOT / "files" / name
            if "/" in name or name.startswith(".") or not p.is_file():
                return self._send(404)
            log(f"FILE {name} {p.stat().st_size}")
            return self._send(200, p.read_bytes())
        if not self.path.startswith("/next"):
            return self._send(404)
        from urllib.parse import parse_qs, urlsplit
        q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        try:
            last = int(q.get("after", "0"))
        except ValueError:
            return self._send(400)
        if "m" in q or Handler.require_signed_poll:
            # v2 取件带签名：HMAC("poll|after|t")，t 与本机相差不超过 10 分钟；v1 不带签名，只在未开 --require-signed-poll 时放行
            try:
                fresh = abs(time.time() - int(q.get("t", "0"))) <= 600
            except ValueError:
                fresh = False
            good = hmac.compare_digest(sign("poll", q.get("after", ""), q.get("t", "")), q.get("m", ""))
            if not (fresh and good):
                log(f"BAD_POLL {self.client_address[0]} fresh={fresh}")
                return self._send(401)
        (ROOT / "last_seen").write_text(f"{time.time():.0f} after={last} peer={self.client_address[0]}\n")
        if self.client_address[0] != Handler.last_peer:
            # 只在取件方地址变化时记一行，用来知道 Windows 现在的地址
            Handler.last_peer = self.client_address[0]
            log(f"PEER {self.client_address[0]} after={last}")
        deadline = time.time() + 25
        while time.time() < deadline:
            job = pending_after(last)
            if job:
                log(f"DISPATCH id={job['id']}")
                return self._send(200, json.dumps(job).encode())
            time.sleep(0.5)
        self._send(204)

    def do_POST(self):
        if not self._allowed():
            return
        if self.path != "/result":
            return self._send(404)
        n = int(self.headers.get("Content-Length", "0"))
        if n <= 0 or n > 64_000_000:
            return self._send(413)
        try:
            r = json.loads(self.rfile.read(n).decode("utf-8"))
            expect = sign("res", r["id"], r["rc"], r["out"], r["err"])
        except Exception as e:  # 格式坏就拒收
            log(f"BAD_RESULT {e!r}")
            return self._send(400)
        if not hmac.compare_digest(expect, str(r.get("mac", ""))):
            log(f"BAD_MAC id={r.get('id')}")
            return self._send(401)
        (ROOT / "results" / f"{int(r['id'])}.json").write_text(json.dumps(r))
        log(f"RESULT id={r['id']} rc={r['rc']}")
        self._send(200, b"{}")


def cmd_init(a):
    import secrets as _s
    for d in ("queue", "results", "files"):
        (ROOT / d).mkdir(parents=True, exist_ok=True)
    p = ROOT / "secret"
    try:
        # 独占创建，防止同时初始化覆盖已配对密钥；创建时就限制权限。
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(_s.token_hex(16) + "\n")
    if getattr(a, "show_secret", False):
        print(p.read_text(encoding="utf-8").strip())
    else:
        print(f"WBAGENT_INITIALIZED state={ROOT}")


def lan_address():
    """Mac 当前局域网地址与网段（en0 优先，再 en1）；都没有返回 (None, None)。"""
    import ipaddress, subprocess
    for ifc in ("en0", "en1"):
        ip = subprocess.run(["ipconfig", "getifaddr", ifc], capture_output=True, text=True).stdout.strip()
        if ip:
            mask = subprocess.run(["ipconfig", "getoption", ifc, "subnet_mask"], capture_output=True, text=True).stdout.strip()
            return ip, ipaddress.ip_network(f"{ip}/{mask or '255.255.255.0'}", strict=False)
    return None, None


def publish_rendezvous(gist: str, server: str) -> bool:
    """把签名的当前地址写进 secret gist 的 wbagent-rendezvous.json；Windows 端验签后才采用。"""
    import subprocess, tempfile
    ts = int(time.time())
    rec = {"server": server, "ts": ts, "mac": sign("rv", server, ts)}
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "wbagent-rendezvous.json"
        p.write_text(json.dumps(rec))
        r = subprocess.run(["gh", "gist", "edit", gist, "--filename", "wbagent-rendezvous.json", str(p)],
                           capture_output=True, text=True, timeout=60)
    log(f"RENDEZVOUS {server} ts={ts} rc={r.returncode} {r.stderr.strip()[:200]}")
    return r.returncode == 0


def cmd_serve(a):
    import threading
    Handler.allow_ip = a.allow_ip
    Handler.require_signed_poll = a.require_signed_poll
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"SERVE {a.host}:{a.port} allow={a.allow_ip} own_subnet={a.allow_own_subnet} "
        f"signed_poll={a.require_signed_poll} rendezvous={'yes' if a.rendezvous_gist else 'no'} "
        f"advertise={a.advertise_host or 'en0'} ttl={a.ttl}")
    end = time.time() + a.ttl
    published, published_at, next_check = None, 0.0, 0.0
    while time.time() < end and not (ROOT / "STOP").exists():
        if time.time() >= next_check and (a.allow_own_subnet or a.rendezvous_gist):
            next_check = time.time() + 30
            ip, net = lan_address()
            if a.allow_own_subnet and net != Handler.own_net:
                Handler.own_net = net
                log(f"OWN_SUBNET {net}")
            # --advertise-host 给定时（例如 Tailscale 地址）发布固定地址，不随 en0 换网变化
            host = a.advertise_host or ip
            if a.rendezvous_gist and host:
                server = f"http://{host}:{a.port}"
                # 地址变了马上发；不变也每 6 小时刷新一次；发失败下一轮（30 秒后）重试
                if server != published or time.time() - published_at > 6 * 3600:
                    if publish_rendezvous(a.rendezvous_gist, server):
                        published, published_at = server, time.time()
        time.sleep(1)
    srv.shutdown()
    log("SERVE_END")


def cmd_run(a):
    script = Path(a.file).read_text(encoding="utf-8") if a.file else a.command
    b64 = base64.b64encode(script.encode("utf-8")).decode()
    return await_result(enqueue(b64, a.timeout), a.wait)


def enqueue(b64: str, timeout: int) -> int:
    # os.link 在目标已存在时失败：两个客户端抢到同一个号时，后到的换下一个号，不会覆盖别人的任务
    while True:
        ids = [int(p.stem) for p in (ROOT / "queue").glob("*.json")]
        i = max(ids, default=0) + 1
        job = {"id": i, "timeout": timeout, "script": b64, "mac": sign("cmd", i, timeout, b64)}
        tmp = ROOT / "queue" / f".{i}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(job))
        try:
            os.link(tmp, ROOT / "queue" / f"{i}.json")
            return i
        except FileExistsError:
            continue
        finally:
            tmp.unlink()


def await_result(i: int, wait: int) -> int:
    res = ROOT / "results" / f"{i}.json"
    end = time.time() + wait
    while time.time() < end and not res.exists():
        time.sleep(0.5)
    if not res.exists():
        print(f"WBAGENT_WAITING id={i}", file=sys.stderr)
        return 124
    r = json.loads(res.read_text())
    sys.stdout.write(base64.b64decode(r["out"]).decode("utf-8", "replace"))
    err = base64.b64decode(r["err"]).decode("utf-8", "replace")
    if err:
        sys.stderr.write(err)
    print(f"\nWBAGENT_RC id={i} rc={r['rc']} ms={r.get('ms')}", file=sys.stderr)
    return int(r["rc"])


def cmd_result(a):
    # run 等超时后，用 id 取迟到的回执
    return await_result(a.id, a.wait)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    init = sub.add_parser("init")
    init.add_argument("--show-secret", action="store_true", help="explicitly print the pairing secret; keep this output private")
    g = sub.add_parser("result")
    g.add_argument("id", type=int)
    g.add_argument("--wait", type=int, default=110)
    s = sub.add_parser("serve")
    s.add_argument("--host", required=True)
    s.add_argument("--port", type=int, default=18092)
    s.add_argument("--allow-ip", required=True)
    s.add_argument("--ttl", type=int, default=43200)
    s.add_argument("--allow-own-subnet", action="store_true", help="另放行 Mac 当前所在网段（每 30 秒重测）")
    s.add_argument("--require-signed-poll", action="store_true", help="拒收不带签名的取件（v1 agent 会被拒）")
    s.add_argument("--rendezvous-gist", default="", help="把签名的当前地址写进这个 gist")
    s.add_argument("--advertise-host", default="", help="rendezvous 发布这个固定地址（如 Tailscale 100.x）；不给则用 en0 地址")
    r = sub.add_parser("run")
    r.add_argument("-f", "--file")
    r.add_argument("-c", "--command")
    r.add_argument("--timeout", type=int, default=600)
    r.add_argument("--wait", type=int, default=110)
    a = ap.parse_args()
    if a.cmd == "run" and not (a.file or a.command):
        ap.error("run needs -f or -c")
    return {"init": cmd_init, "serve": cmd_serve, "run": cmd_run, "result": cmd_result}[a.cmd](a) or 0


if __name__ == "__main__":
    sys.exit(main())
