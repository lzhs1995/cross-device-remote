"""Windows 5.1 集成测试：临时私有状态、回环签名协议；绝不调用 Mplus 或现役 worker。"""
import base64
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid


SOURCE = Path(__file__).resolve().parent.parent
POWERSHELL = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"


@unittest.skipUnless(os.name == "nt", "requires real Windows PowerShell 5.1; no cross-platform simulation")
class WindowsWorkerTests(unittest.TestCase):
    def setUp(self):
        if not POWERSHELL.is_file():
            self.skipTest("Windows PowerShell 5.1 executable unavailable")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.server_state = self.root / "controller"
        self.worker_state = self.root / "worker 測試"
        for path in (self.server_state / "queue", self.server_state / "results", self.server_state / "files", self.worker_state):
            path.mkdir(parents=True, exist_ok=True)
        self.key = secrets.token_hex(16)
        (self.server_state / "secret").write_text(self.key, encoding="utf-8")
        (self.worker_state / "secret").write_text(self.key, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("test_server_" + uuid.uuid4().hex, SOURCE / "scripts/wbagent.py")
        self.wb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.wb)
        self.wb.ROOT = self.server_state
        self.wb.Handler.allow_ip = "127.0.0.1/32"
        self.wb.Handler.require_signed_poll = True
        self.accept_results = threading.Event()
        self.accept_results.set()
        accept_results = self.accept_results

        class TestHandler(self.wb.Handler):
            def do_POST(handler):
                if not accept_results.is_set():
                    handler.rfile.read(int(handler.headers.get("Content-Length", "0")))
                    return handler._send(503, b'{}')
                return super().do_POST()

        self.server = self.wb.ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.worker_config = self.worker_state / "agent2.json"
        self.worker_config.write_text(json.dumps({"state": str(self.worker_state),
            "secret_file": str(self.worker_state / "secret"), "servers": [self.url], "rendezvous": [],
            "mutex": "Local\\XdevWorkerTest_" + uuid.uuid4().hex}), encoding="utf-8")
        self.client_config = self.root / "client-config.json"
        self.client_config.write_text(json.dumps({"state_dir": str(self.server_state),
            "windows": {"agent_state": str(self.worker_state), "work_root": str(self.root / "client-work")}}), encoding="utf-8")
        self.worker = None
        self.logs = []
        self.start_worker()

    def tearDown(self):
        try:
            self.stop_worker()
        finally:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
            for stream in self.logs:
                stream.close()
            # Windows 的后台长轮询线程可能仍持有文件；先终止所有已知自有进程再清理。
            self.tmp.cleanup()

    def start_worker(self):
        log = (self.root / f"worker-{len(self.logs)}.log").open("ab")
        self.logs.append(log)
        self.worker = subprocess.Popen([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(SOURCE / "windows/wbagent-agent2.ps1"), "-Config", str(self.worker_config), "-Hours", "0.05"],
            stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)

    def stop_worker(self, force=False):
        if self.worker is None or self.worker.poll() is not None:
            return
        if not force:
            self.accept_results.set()
            self.enqueue("WBAGENT_EXIT")
            try:
                self.worker.wait(timeout=15)
                return
            except subprocess.TimeoutExpired:
                pass
        # 仅结束由本测试 Popen 建立并仍存活的进程树。
        if self.worker.poll() is None:
            subprocess.run(["taskkill.exe", "/PID", str(self.worker.pid), "/T", "/F"], capture_output=True, timeout=15)
            self.worker.wait(timeout=15)

    def diagnostics(self):
        parts = []
        for file in sorted(self.root.glob("worker-*.log")):
            parts.append(file.read_text(encoding="utf-8", errors="replace")[-6000:])
        return "\n".join(parts)

    def until(self, predicate, seconds=40):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            if self.worker.poll() is not None:
                self.fail(f"worker exited {self.worker.returncode}: {self.diagnostics()}")
            time.sleep(0.1)
        self.fail("worker timed out: " + self.diagnostics())

    def enqueue(self, body, timeout=10):
        return self.wb.enqueue(base64.b64encode(body.encode("utf-8")).decode("ascii"), timeout)

    def result(self, job):
        path = self.server_state / "results" / f"{job}.json"

        def read():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                return None

        return self.until(read)

    @staticmethod
    def quote(path):
        return "'" + str(path).replace("'", "''") + "'"

    def test_first_xdev_wrapper_job_unicode_nonzero_and_saved_server(self):
        # 真正使用 xdev 的 PREAMBLE；缺少首次 server.txt 时此调用必失败。
        command = "[Console]::WriteLine('中文 — pairing'); [Console]::Error.WriteLine('錯誤'); exit 7"
        result = subprocess.run([sys.executable, "-B", str(SOURCE / "scripts/xdev.py"), "run", "-c", command,
            "--timeout", "10", "--wait", "35"], env=dict(os.environ, XDEV_CONFIG=str(self.client_config), PYTHONUTF8="1"),
            capture_output=True, text=True, encoding="utf-8", timeout=45)
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr + self.diagnostics())
        self.assertIn("中文 — pairing", result.stdout)
        self.assertIn("錯誤", result.stderr)
        self.assertEqual((self.worker_state / "server.txt").read_text().strip(), self.url)
        self.assertNotIn(self.key, result.stdout + result.stderr + self.diagnostics())
        local = self.until(lambda: next(iter(self.worker_state.glob("result-*.json")), None))
        self.assertEqual(json.loads(local.read_text())["rc"], 7)

    def test_timeout_returns_124_and_leaves_result_record(self):
        job = self.enqueue("Start-Sleep -Seconds 30", timeout=1)
        result = self.result(job)
        self.assertEqual(result["rc"], 124)
        self.assertIn("WBAGENT_TIMEOUT", base64.b64decode(result["err"]).decode("utf-8"))
        self.assertTrue((self.worker_state / f"result-{job}.json").is_file())

    def test_crash_restart_uses_claim_even_if_cursor_is_restored_to_old_value(self):
        marker = self.root / "one-execution.txt"
        job = self.enqueue(f"[IO.File]::AppendAllText({self.quote(marker)}, 'once'); Start-Sleep -Seconds 30", timeout=60)
        self.until(lambda: marker.exists() and marker.read_text() == "once")
        self.assertTrue((self.worker_state / "claims" / f"{job}.json").is_file())
        self.stop_worker(force=True)
        # 只在临时测试状态模拟旧 cursor 恢复，证明独占意向阻止重放。
        (self.worker_state / "last_id").write_text("0\n", encoding="ascii")
        self.start_worker()
        next_job = self.enqueue("[Console]::WriteLine('resumed')")
        self.assertEqual(self.result(next_job)["rc"], 0)
        self.assertEqual(marker.read_text(), "once")
        self.assertFalse((self.server_state / "results" / f"{job}.json").exists())

    def test_unsent_result_is_retained_and_reposted_after_restart_without_rerun(self):
        marker = self.root / "side-effect.txt"
        self.accept_results.clear()
        job = self.enqueue(f"[IO.File]::AppendAllText({self.quote(marker)}, 'once'); [Console]::WriteLine('saved')")
        local = self.worker_state / f"result-{job}.json"
        self.until(lambda: local.exists() and local.stat().st_size > 0)
        self.stop_worker(force=True)
        before = local.read_bytes()
        self.assertFalse((self.server_state / "results" / f"{job}.json").exists())
        self.accept_results.set()
        self.start_worker()
        result = self.result(job)
        self.assertEqual(result["rc"], 0)
        self.assertEqual(marker.read_text(), "once")
        self.assertEqual(local.read_bytes(), before)
        self.until(lambda: Path(str(local) + ".sent").exists())

    def test_windows_installer_rejects_non_origin_before_creating_state(self):
        destination = self.root / "must-not-be-created"
        result = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(SOURCE / "windows/install.ps1"), "-Server", self.url + "/not-an-origin",
            "-SecretFile", str(self.worker_state / "secret"), "-State", str(destination)], capture_output=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(destination.exists())

    def test_malformed_legacy_cursor_fails_closed_without_dispatch(self):
        self.stop_worker()
        (self.worker_state / "last_id").write_text("", encoding="ascii")
        marker = self.root / "must-not-run.txt"
        job = self.enqueue(f"[IO.File]::WriteAllText({self.quote(marker)}, 'unexpected')")
        self.start_worker()
        self.worker.wait(timeout=20)
        self.assertNotEqual(self.worker.returncode, 0)
        self.assertFalse(marker.exists())
        self.assertFalse((self.server_state / "results" / f"{job}.json").exists())
        self.assertIn("Invalid last_id", self.diagnostics())


if __name__ == "__main__":
    unittest.main(verbosity=2)
