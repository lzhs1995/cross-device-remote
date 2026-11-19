#!/usr/bin/env python3
"""Offline control/1 acceptance in disposable state. No existing worker or data.

On macOS/POSIX this exercises protocol/scheduler/child supervision, NOT Windows
Job Object acceptance. Run the same suite with native Windows Python for that.
"""
import base64
import copy
import contextlib
import io
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
from unittest.mock import patch
import urllib.error
import urllib.request

import xdev_control as control


def spec(key, seconds=0.1, resources=None, code=None, output_limit=65536, timeout=10):
    code = code or ("import json,time; a=time.time_ns(); time.sleep(" + str(seconds) +
                    "); print(json.dumps({'start':a,'end':time.time_ns()}))")
    return {"version": control.VERSION, "op": "exec", "key": key,
            "lane": "serial" if resources is None else "independent", "resource_keys": sorted(resources or []),
            "body": {"argv": [sys.executable, "-B", "-c", code]}, "timeout": timeout, "output_limit": output_limit}


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xdev-control-test-")
        self.root = Path(self.tmp.name)
        self.secret = secrets.token_bytes(32)
        self.channel = "offline-test"
        self.controller = control.ControllerStore(self.root / "controller", self.channel)
        self.server = control.ControlServer(("127.0.0.1", 0), self.controller, self.secret, self.channel)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.client = control.Client(self.url, self.secret, self.channel)
        self.worker = None
        self.worker_thread = None
        self.stop = threading.Event()

    def tearDown(self):
        self.stop_worker()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)
        self.tmp.cleanup()

    def start_worker(self, max_workers=4, state=None):
        self.stop.clear()
        self.worker = control.Worker(state or self.root / "worker", self.client, self.channel, max_workers)
        def loop():
            while not self.stop.is_set():
                self.worker.step()
                self.stop.wait(0.02)
        self.worker_thread = threading.Thread(target=loop, daemon=True)
        self.worker_thread.start()

    def stop_worker(self):
        if self.worker is not None:
            self.stop.set()
            if self.worker_thread:
                self.worker_thread.join(timeout=8)
                self.assertFalse(self.worker_thread.is_alive())
            self.worker.close()
            self.worker = None
            self.worker_thread = None

    def until(self, predicate, seconds=12):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.02)
        self.fail("bounded wait expired; worker errors=" + str(self.worker.errors if self.worker else []))

    def terminal(self, key):
        return self.until(lambda: (r if (r := self.client.call("status", {"key": key}))["state"] in control.TERMINAL else None))

    def interval(self, key):
        result = self.terminal(key)
        self.assertEqual(result["state"], "SUCCEEDED", result)
        return json.loads(base64.b64decode(result["result"]["stdout_b64"]))

    def test_different_explicit_resources_really_overlap_and_own_cwd(self):
        release = self.root / "overlap-release"
        ready = [self.root / ("overlap-ready-" + key) for key in ("a", "b")]
        # Hold both real child bodies until both have started; fixed sleeps can
        # finish before a slow second launch. A serial worker cannot reach both markers.
        for key, marker in zip(("a", "b"), ready):
            body = ("from pathlib import Path\nimport json,time\n"
                    "start = time.monotonic_ns()\n"
                    "release = Path(" + repr(str(release)) + ")\n"
                    "deadline = time.monotonic() + 60\n"
                    "Path(" + repr(str(marker)) + ").write_text('ready')\n"
                    "while not release.exists():\n"
                    "    if time.monotonic() >= deadline:\n"
                    "        raise SystemExit('OVERLAP_FIXTURE_RELEASE_TIMEOUT')\n"
                    "    time.sleep(0.02)\n"
                    "print(json.dumps({'start':start,'end':time.monotonic_ns()}))\n")
            self.client.call("submit", spec(key, resources=["sandbox:" + key], code=body, timeout=75))
        try:
            self.start_worker()
            self.until(lambda: all(marker.is_file() for marker in ready))
            for key in ("a", "b"):
                self.assertEqual(self.client.call("status", {"key": key})["state"], "CLAIMED")
        finally:
            # Preserve a readiness failure, release only our children, and collect
            # both original keys even if collecting the first one fails.
            original_error = sys.exc_info()[1]
            release.touch()
            results, cleanup_errors = {}, []
            for key in ("a", "b"):
                try:
                    results[key] = self.terminal(key)
                except Exception as exc:
                    cleanup_errors.append((key, exc))
            if cleanup_errors:
                detail = "overlap fixture cleanup failed: " + repr(cleanup_errors)
                if original_error is not None:
                    original_error.add_note(detail)
                else:
                    self.fail(detail)
        for result in results.values():
            self.assertEqual(result["state"], "SUCCEEDED", result)
        a, b = (json.loads(base64.b64decode(results[key]["result"]["stdout_b64"])) for key in ("a", "b"))
        self.assertLess(max(a["start"], b["start"]), min(a["end"], b["end"]))
        self.assertTrue((self.root / "worker/work/a/scratch").is_dir())
        self.assertTrue((self.root / "worker/work/b/scratch").is_dir())

    def test_same_resource_is_serialized(self):
        self.client.call("submit", spec("a", 0.25, ["rstudio:one"]))
        self.client.call("submit", spec("b", 0.25, ["rstudio:one"]))
        self.start_worker()
        a, b = self.interval("a"), self.interval("b")
        self.assertLessEqual(a["end"], b["start"])

    def test_default_arbitrary_job_is_global_serial_barrier(self):
        self.client.call("submit", spec("before", 0.2, ["a"]))
        self.client.call("submit", spec("default", 0.2))
        self.client.call("submit", spec("after", 0.2, ["b"]))
        self.start_worker()
        a, b, c = self.interval("before"), self.interval("default"), self.interval("after")
        self.assertLessEqual(a["end"], b["start"])
        self.assertLessEqual(b["end"], c["start"])

    def test_status_and_short_control_complete_while_long_job_runs(self):
        marker = self.root / "long-started"
        release = self.root / "long-release"
        # 用显式释放保持长任务；机器负载不应令固定睡眠提前结束。
        body = ("from pathlib import Path\nimport time\n"
                "release = Path(" + repr(str(release)) + ")\n"
                "deadline = time.monotonic() + 60\n"
                "Path(" + repr(str(marker)) + ").write_text('started')\n"
                "while not release.exists():\n"
                "    if time.monotonic() >= deadline:\n"
                "        raise SystemExit('LONG_FIXTURE_RELEASE_TIMEOUT')\n"
                "    time.sleep(0.02)\n"
                "print('released')\n")
        self.client.call("submit", spec("long", resources=["long"], code=body, timeout=75))
        try:
            self.start_worker()
            self.until(marker.exists)
            start = time.monotonic()
            status = self.client.call("status", {"key": "long"})
            self.assertEqual(status["state"], "CLAIMED")
            self.assertLess(time.monotonic() - start, 0.5)
            self.client.call("submit", spec("short", resources=["short"], code="print('control')"))
            self.assertEqual(self.terminal("short")["state"], "SUCCEEDED")
            self.assertFalse(release.exists())
            self.assertEqual(self.client.call("status", {"key": "long"})["state"], "CLAIMED")
        finally:
            # 即使断言失败，也释放本测试孩子并按原 key 核收终态。
            release.touch()
            long_result = self.terminal("long")
        self.assertEqual(long_result["state"], "SUCCEEDED", long_result)
        self.assertEqual(base64.b64decode(long_result["result"]["stdout_b64"]).strip(), b"released")

    def test_submit_duplicate_is_idempotent_conflicting_id_preserves_original(self):
        original = spec("stable", resources=["a"])
        first = self.client.call("submit", original)
        second = self.client.call("submit", original)
        self.assertEqual(first["seq"], second["seq"])
        self.assertTrue(second["duplicate"])
        altered = copy.deepcopy(original)
        altered["timeout"] += 1
        with self.assertRaises(control.ProtocolError) as context:
            self.client.call("submit", altered)
        self.assertEqual(context.exception.code, 409)
        self.assertEqual(self.controller.fetch(0)[0]["spec"], original)

    def test_result_duplicate_is_atomic_conflicting_result_rejected(self):
        self.client.call("submit", spec("done"))
        self.start_worker()
        result = self.terminal("done")["result"]
        self.assertTrue(self.client.worker_call("result", result=result)["duplicate"])
        altered = dict(result, returncode=99)
        with self.assertRaises(control.ProtocolError) as context:
            self.client.worker_call("result", result=altered)
        self.assertEqual(context.exception.code, 409)
        self.assertEqual(self.controller.status("done")["result"], result)

    def test_restart_after_claim_unknown_never_replayed_and_resource_retained(self):
        marker = self.root / "must-not-run"
        original = spec("claimed", resources=["protected"], code="from pathlib import Path; Path(" + repr(str(marker)) + ").touch()")
        self.client.call("submit", original)
        local = control.WorkerStore(self.root / "worker", self.channel)
        local.ingest(self.controller.fetch(0))
        token = local.claim("claimed")
        self.client.worker_call("claim", key="claimed", digest=control.digest(original), claim=token)
        self.start_worker()
        recovered = self.terminal("claimed")
        self.assertEqual(recovered["state"], "UNKNOWN")
        self.assertFalse(marker.exists())
        self.client.call("submit", spec("same", resources=["protected"]))
        self.client.call("submit", spec("other", resources=["free"]))
        self.assertEqual(self.terminal("other")["state"], "SUCCEEDED")
        self.assertEqual(self.controller.status("same")["state"], "QUEUED")
        self.assertFalse(marker.exists())

    def test_inbox_transaction_rolls_back_cursor_and_records_on_gap(self):
        self.controller.submit(spec("one"))
        self.controller.submit(spec("two"))
        local = control.WorkerStore(self.root / "worker", self.channel)
        rows = self.controller.fetch(0)
        wrong = copy.deepcopy(rows)
        wrong[1]["seq"] = 3
        with self.assertRaises(control.ProtocolError):
            local.ingest(wrong)
        self.assertEqual(local.cursor(), 0)
        self.assertEqual(local.rows("1=1"), [])
        local.ingest(rows)
        self.assertEqual(local.cursor(), 2)
        self.assertEqual(len(local.rows("1=1")), 2)
        local.ingest(rows)
        self.assertEqual(local.cursor(), 2)
        restarted = control.WorkerStore(self.root / "worker", self.channel)
        self.assertEqual(restarted.cursor(), 2)
        self.assertEqual([r["key"] for r in restarted.rows("state='QUEUED'")], ["one", "two"])

    def test_actual_dispatcher_process_crash_recovers_running_without_replay(self):
        marker = self.root / "execution-count"
        finished = self.root / "original-finished"
        code = ("from pathlib import Path; import time; p=Path(" + repr(str(marker)) +
                "); p.open('a').write('once\\n'); time.sleep(1.2); Path(" + repr(str(finished)) + ").touch()")
        printable = secrets.token_hex(24).encode()
        key_file = self.root / "worker-key"
        key_file.write_bytes(printable)
        self.server.secret = printable
        self.client = control.Client(self.url, printable, self.channel)
        self.client.call("submit", spec("crashed", resources=["crash-key"], code=code))
        process = subprocess.Popen([sys.executable, "-B", str(Path(control.__file__).resolve()), "worker",
            "--url", self.url, "--secret-file", str(key_file), "--state", str(self.root / "worker"),
            "--channel-id", self.channel, "--seconds", "10", "--poll", "0.05", "--allow-local-test"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.until(lambda: marker.exists() and marker.read_text() == "once\n")
            process.terminate()  # 仅结束本测试 Popen 的 dispatcher，模拟真实崩溃。
            process.wait(timeout=5)
            self.start_worker()
            self.assertEqual(self.terminal("crashed")["state"], "UNKNOWN")
            self.client.call("submit", spec("after-crash", resources=["free-key"]))
            self.assertEqual(self.terminal("after-crash")["state"], "SUCCEEDED")
            if os.name != "nt":
                self.until(finished.exists)
            self.assertEqual(marker.read_text(), "once\n")
            self.assertEqual(self.worker.store.cursor(), 2)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)

    def test_durable_unsent_result_republishes_without_execution(self):
        marker = self.root / "once"
        original = spec("outbox", code="from pathlib import Path; Path(" + repr(str(marker)) + ").write_text('once')")
        self.controller.submit(original)
        worker = control.Worker(self.root / "worker", self.client, self.channel)
        worker.store.ingest(self.controller.fetch(0))
        token = worker.store.claim("outbox")
        self.client.worker_call("claim", key="outbox", digest=control.digest(original), claim=token)
        worker.store.running("outbox", token)
        result = control.execute(original, token, worker.store.root)
        worker.store.complete(result)
        before = worker.store.rows("key='outbox'")[0]["result"]
        worker.lock.close()  # 模拟终态落盘后、发布前退出；不运行 close() 的发布步骤。
        self.assertIsNone(self.controller.status("outbox")["result"])
        self.start_worker()
        self.assertEqual(self.terminal("outbox")["result"], result)
        self.assertEqual(self.worker.store.rows("key='outbox'")[0]["result"], before)
        self.assertEqual(marker.read_text(), "once")

    def test_single_dispatcher_lock_and_central_claim_fence(self):
        self.start_worker()
        with self.assertRaises(control.ProtocolError):
            control.Worker(self.root / "worker", self.client, self.channel)
        self.stop_worker()
        original = spec("single", resources=["a"])
        self.controller.submit(original)
        self.assertTrue(self.controller.claim("single", control.digest(original), "intent-one")["claimed"])
        self.assertEqual(self.controller.claim("single", control.digest(original), "intent-two"),
                         {"claimed": False, "state": "REMOTE"})

    def test_fresh_inbox_does_not_replay_controller_terminal(self):
        self.controller.submit(spec("past", resources=["a"]))
        self.start_worker()
        first = self.terminal("past")["result"]
        self.stop_worker()
        self.start_worker(state=self.root / "fresh-worker")
        self.until(lambda: self.worker.store.cursor() == 1)
        self.controller.submit(spec("new", resources=["a"]))
        self.assertEqual(self.terminal("new")["state"], "SUCCEEDED")
        self.assertEqual(self.controller.status("past")["result"], first)
        self.assertFalse((self.root / "fresh-worker/work/past").exists())

    def test_two_worker_states_refresh_foreign_claim_then_run_same_resource(self):
        self.controller.submit(spec("owner", seconds=0.6, resources=["shared"]))
        first = control.Worker(self.root / "worker-one", self.client, self.channel, 1)
        second = None
        try:
            first.step()
            self.assertEqual(self.controller.status("owner")["state"], "CLAIMED")
            second = control.Worker(self.root / "worker-two", self.client, self.channel, 1)
            self.controller.submit(spec("follower", seconds=0.05, resources=["shared"]))
            second.step()
            self.assertEqual(second.store.rows("key='owner'")[0]["state"], "REMOTE")
            self.assertIsNone(second.store.rows("key='owner'")[0]["result"])
            def finished():
                first.step(dispatch=False)
                second.step()
                return self.controller.status("follower")["state"] == "SUCCEEDED"
            self.until(finished)
            owner = self.interval("owner")
            follower = self.interval("follower")
            self.assertLessEqual(owner["end"], follower["start"])
            self.assertEqual(second.store.rows("key='owner'")[0]["state"], "SUCCEEDED")
            self.assertFalse((second.store.root / "work/owner").exists())
            self.assertTrue((second.store.root / "work/follower").exists())
        finally:
            first.close()
            if second:
                second.close()

    def test_two_worker_claim_race_different_resources_has_no_false_unknown(self):
        for key in ("one", "two"):
            marker = self.root / (key + "-count")
            code = ("from pathlib import Path; import time; Path(" + repr(str(marker)) +
                    ").open('a').write('once\\n'); time.sleep(0.3)")
            self.controller.submit(spec(key, resources=[key], code=code))
        first = control.Worker(self.root / "worker-one", self.client, self.channel, 1)
        second = control.Worker(self.root / "worker-two", self.client, self.channel, 1)
        try:
            # 两个目录都先缓存 QUEUED，再让一个中央 CAS 获胜。
            records = self.controller.fetch(0)
            first.store.ingest(records)
            second.store.ingest(records)
            first.step()
            second.step()
            self.assertEqual(second.store.rows("key='one'")[0]["state"], "REMOTE")
            self.assertIsNone(second.store.rows("key='one'")[0]["result"])
            def finished():
                first.step(dispatch=False)
                second.step()
                return all(self.controller.status(key)["state"] == "SUCCEEDED" for key in ("one", "two"))
            self.until(finished)
            second.step()
            self.assertEqual([r["state"] for r in second.store.rows("1=1")], ["SUCCEEDED", "SUCCEEDED"])
            self.assertEqual((self.root / "one-count").read_text(), "once\n")
            self.assertEqual((self.root / "two-count").read_text(), "once\n")
            self.assertFalse((second.store.root / "work/one").exists())
            self.assertTrue((second.store.root / "work/two").exists())
        finally:
            first.close()
            second.close()

    def test_conflicting_and_temporarily_failed_outbox_do_not_block_later_results(self):
        worker = control.Worker(self.root / "worker", self.client, self.channel)
        original_call = self.client.worker_call
        try:
            submitted = {}
            for key in ("conflict", "transient", "later"):
                original = spec(key, resources=[key])
                self.controller.submit(original)
                submitted[key] = original
            worker.store.ingest(self.controller.fetch(0))
            saved = {}
            for key, original in submitted.items():
                token = worker.store.claim(key)
                self.controller.claim(key, control.digest(original), token)
                result = control.empty_result(original, token, "offline fixture", state="FAILED")
                worker.store.complete(result)
                saved[key] = result
            server_result = dict(saved["conflict"], reason="first authoritative receipt")
            self.controller.publish(server_result)
            def fail_one(op, **payload):
                if op == "result" and payload["result"]["key"] == "transient":
                    raise OSError("simulated temporary transport failure")
                return original_call(op, **payload)
            self.client.worker_call = fail_one
            worker.flush_results()
            self.assertEqual(worker.store.rows("key='conflict'")[0]["sent"], -1)
            self.assertEqual(worker.store.rows("key='transient'")[0]["sent"], 0)
            self.assertEqual(worker.store.rows("key='later'")[0]["sent"], 1)
            self.assertEqual(self.controller.status("later")["result"], saved["later"])
            self.assertEqual(json.loads(worker.store.rows("key='conflict'")[0]["result"]), saved["conflict"])
            self.assertEqual(self.controller.status("conflict")["result"], server_result)
            with worker.store.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM publication_conflicts").fetchone()[0], 1)
            self.client.worker_call = original_call
            worker.flush_results()
            self.assertEqual(worker.store.rows("key='transient'")[0]["sent"], 1)
        finally:
            self.client.worker_call = original_call
            worker.close()

    def test_gated_child_waits_for_real_exit_and_preserves_argv(self):
        args = ["space value", 'embedded"quote', "trailing\\", "中文"]
        original = spec("argv", code="import json,sys,time; time.sleep(0.25); print(json.dumps(sys.argv[1:])); sys.exit(7)")
        original["body"]["argv"].extend(args)
        start = time.monotonic()
        result = control.execute(original, "fixture-intent", self.root / "execution")
        self.assertGreaterEqual(time.monotonic() - start, 0.25)
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(result["returncode"], 7)
        self.assertEqual(json.loads(base64.b64decode(result["stdout_b64"])), args)

    def test_hmac_covers_operation_lane_resources_key_body_and_timeout(self):
        original = spec("signed", resources=["a"])
        envelope = control.signed(self.secret, {"version": control.VERSION, "op": "submit", "payload": original,
                                              "sent_at": int(time.time()), "nonce": secrets.token_hex(16)})
        changes = [("lane", "serial"), ("resource_keys", ["other"]), ("key", "different"),
                   ("body", {"argv": ["different"]}), ("timeout", 99), ("output_limit", 1)]
        for field, value in changes:
            tampered = copy.deepcopy(envelope)
            tampered["payload"][field] = value
            with self.assertRaises(control.ProtocolError):
                control.verified(self.secret, tampered)
        tampered = dict(envelope, op="status")
        with self.assertRaises(control.ProtocolError):
            control.verified(self.secret, tampered)
        request = urllib.request.Request(self.url + "/v1", data=control.canonical(tampered))
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(context.exception.code, 401)
        context.exception.close()
        self.assertEqual(self.controller.fetch(0), [])

    def test_bound_output_own_cwd_and_scratch(self):
        code = "import os,json; print(json.dumps({'cwd':os.getcwd(),'scratch':os.environ['TMP']})); print('x'*200000)"
        self.controller.submit(spec("bounded", code=code, output_limit=2048))
        self.start_worker()
        result = self.terminal("bounded")["result"]
        data = base64.b64decode(result["stdout_b64"])
        self.assertLessEqual(len(data) + len(base64.b64decode(result["stderr_b64"])), 2048)
        self.assertTrue(result["truncated"])
        paths = json.loads(data.splitlines()[0])
        self.assertEqual(Path(paths["cwd"]).resolve(), (self.root / "worker/work/bounded").resolve())
        self.assertEqual(Path(paths["scratch"]).resolve(), (self.root / "worker/work/bounded/scratch").resolve())

    def test_cli_run_status_and_capabilities_use_new_server(self):
        secret_file = self.root / "key"
        # CLI 的文本密钥和内存测试密钥使用同一组可打印内容。
        printable = secrets.token_hex(24).encode()
        secret_file.write_bytes(printable)
        self.server.secret = printable
        self.client = control.Client(self.url, printable, self.channel)
        prefix = [sys.executable, "-B", str(Path(control.__file__).resolve())]
        common = ["--url", self.url, "--secret-file", str(secret_file)]
        # 尚无worker：确定性覆盖观察截止，不假定子进程能在5秒内完成启动。
        marker = self.root / "cli-executed-once"
        code = ("from pathlib import Path; import sys; "
                "p=Path(" + repr(str(marker)) + "); "
                "p.open('x').close(); sys.stdout.buffer.write(b'cli-ok\\n')")
        command = prefix + ["run", *common, "--id", "cli", "--wait", "0.001", "--", sys.executable,
                            "-B", "-c", code]
        run = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 3, run.stdout + run.stderr)
        messages = [json.loads(line) for line in run.stdout.splitlines()]
        receipt = messages[0]
        self.assertFalse(receipt["duplicate"])
        self.assertEqual(messages[-1], {"key": "cli", "state": "OBSERVER_TIMEOUT", "action": "status original id"})
        queued = self.client.call("status", {"key": "cli"})
        self.assertEqual(queued["state"], "QUEUED")
        self.assertEqual(queued["digest"], receipt["digest"])
        self.assertFalse(marker.exists())
        self.start_worker()
        terminal = self.terminal("cli")
        self.assertEqual(terminal["state"], "SUCCEEDED", terminal)
        self.assertEqual(terminal["digest"], receipt["digest"])
        self.assertEqual(base64.b64decode(terminal["result"]["stdout_b64"]), b"cli-ok\n")
        executed = marker.stat()
        status = subprocess.run(prefix + ["status", *common, "--id", "cli"], capture_output=True, text=True, timeout=10)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout), terminal)
        # 原id和载荷只做幂等读取；同一排他marker使第二次真实执行无法成功。
        command[command.index("--wait") + 1] = "5"
        resumed = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        messages = [json.loads(line) for line in resumed.stdout.splitlines()]
        self.assertTrue(messages[0]["duplicate"])
        self.assertEqual(messages[0]["digest"], receipt["digest"])
        self.assertEqual(messages[-1], terminal)
        self.assertEqual(len(self.controller.fetch(0)), 1)
        self.assertEqual(marker.stat(), executed)
        caps = subprocess.run(prefix + ["capabilities", *common], capture_output=True, text=True, timeout=10)
        self.assertIn("direct-status", json.loads(caps.stdout)["capabilities"])

    def test_cli_wait_observes_running_then_original_terminal(self):
        # 独立于OS调度的CLI等待协议测试；真实进程/HTTP由上例覆盖。
        key_file = self.root / "wait-key"
        key_file.write_text("wait-protocol-synthetic-secret-32-bytes")
        calls, elapsed = [], [0.0]
        states = iter(({"key": "wait-original", "state": "QUEUED"},
                       {"key": "wait-original", "state": "CLAIMED"},
                       {"key": "wait-original", "state": "SUCCEEDED", "result": {"returncode": 0}}))

        class ScriptedClient:
            def call(self, operation, payload):
                calls.append((operation, copy.deepcopy(payload)))
                if operation == "capabilities":
                    return {"version": control.VERSION}
                if operation == "submit":
                    return {"key": "wait-original", "duplicate": False}
                if operation == "status":
                    return next(states)
                raise AssertionError(operation)

        def advance(seconds):
            self.assertGreater(seconds, 0)
            elapsed[0] += seconds

        output = io.StringIO()
        with patch.object(control, "Client", return_value=ScriptedClient()), \
                patch.object(control.time, "monotonic", side_effect=lambda: elapsed[0]), \
                patch.object(control.time, "sleep", side_effect=advance), contextlib.redirect_stdout(output):
            rc = control.main(["run", "--url", self.url, "--secret-file", str(key_file),
                               "--id", "wait-original", "--wait", "5", "--", sys.executable, "-c", "pass"])
        self.assertEqual(rc, 0)
        self.assertEqual([op for op, _ in calls], ["capabilities", "submit", "status", "status", "status"])
        self.assertTrue(all(payload == {"key": "wait-original"} for op, payload in calls if op == "status"))
        self.assertEqual(json.loads(output.getvalue().splitlines()[-1])["state"], "SUCCEEDED")
        self.assertGreater(elapsed[0], 0)
        self.assertLess(elapsed[0], 5)

    def test_legacy_directory_is_refused_without_mutation(self):
        legacy = self.root / "legacy"
        legacy.mkdir()
        (legacy / "last_id").write_text("123\n")
        with self.assertRaises(control.ProtocolError):
            control.Worker(legacy, self.client, self.channel)
        self.assertEqual({p.name for p in legacy.iterdir()}, {"last_id"})
        self.assertEqual((legacy / "last_id").read_text(), "123\n")

    @unittest.skipUnless(os.name == "nt", "requires Windows native Job Object")
    def test_windows_timeout_kills_only_owned_descendants(self):
        marker = self.root / "descendant-must-not-survive"
        child = "import time; from pathlib import Path; time.sleep(3); Path(" + repr(str(marker)) + ").touch()"
        parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); time.sleep(10)"
        self.controller.submit(spec("timeout", resources=["timed"], code=parent, timeout=1))
        self.controller.submit(spec("unrelated", resources=["other"], seconds=2))
        self.start_worker()
        self.assertEqual(self.terminal("timeout")["state"], "TIMED_OUT")
        self.assertEqual(self.terminal("unrelated")["state"], "SUCCEEDED")
        time.sleep(1.3)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
