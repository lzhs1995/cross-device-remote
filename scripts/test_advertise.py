"""自测 --advertise-host：隔离 state 目录和回环临时端口；rendezvous 与 LAN 探测均替换，不碰真实网络配置。"""
import importlib.util, ipaddress, os, sys, tempfile
from pathlib import Path

state = Path(tempfile.mkdtemp(prefix="xdev-adv-"))
(state / "queue").mkdir(); (state / "results").mkdir()
(state / "secret").write_text("selftest-secret\n", encoding="utf-8")
os.environ["WBAGENT_STATE"] = str(state)
spec = importlib.util.spec_from_file_location("wbagent", Path(__file__).resolve().with_name("wbagent.py"))
wb = importlib.util.module_from_spec(spec); spec.loader.exec_module(wb)
assert wb.ROOT == state, wb.ROOT

published = []
wb.publish_rendezvous = lambda gist, server: published.append((gist, server)) or True
wb.lan_address = lambda: ("192.0.2.20", ipaddress.ip_network("192.0.2.0/24"))

def run(argv):
    sys.argv = ["wbagent.py", "serve", *argv]
    wb.main()

# 1) 给 --advertise-host：发布固定地址
run(["--host", "127.0.0.1", "--port", "0", "--allow-ip", "127.0.0.1/32,100.64.0.0/10",
     "--rendezvous-gist", "FAKE", "--advertise-host", "192.0.2.10", "--ttl", "1"])
assert published == [("FAKE", "http://192.0.2.10:0")], published
# 2) 不给：退回 en0 地址（en0 无地址时不发布）
published.clear()
run(["--host", "127.0.0.1", "--port", "0", "--allow-ip", "127.0.0.1/32", "--rendezvous-gist", "FAKE", "--ttl", "1"])
ip, _ = wb.lan_address()
assert published == ([("FAKE", f"http://{ip}:0")] if ip else []), (published, ip)
log = (state / "server.log").read_text(encoding="utf-8")
assert "advertise=192.0.2.10" in log and "advertise=en0" in log and log.count("SERVE_END") == 2, log
print("ADVERTISE_SELFTEST PASS", published, "state", state)
