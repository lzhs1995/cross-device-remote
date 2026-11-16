"""离线任务生命周期回归；不使用真实配置、队列、Windows 或研究数据。"""
import argparse
import base64
import contextlib
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import uuid
from unittest.mock import Mock, patch


class TaskLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xdev-task-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.old_cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.old_cwd)
        for name in ("1.dat", "2.dat", "a.dat", "b.dat", "data.dat", "data"):
            (self.root / name).write_text("synthetic test input\n")
        self.state = self.root / "state"
        for name in ("queue", "results", "transfers"):
            (self.state / name).mkdir(parents=True, exist_ok=True)
        self.push_outcomes = {}
        self.events = []
        self.event_lock = threading.Lock()
        self.barrier = None
        self.next_id = 0
        core = types.ModuleType("xdev_core")
        core.STATE = self.state
        core.PREAMBLE = "# OFFLINE PREAMBLE\n"
        core.wbagent = types.SimpleNamespace(ROOT=self.state, _require_request=self.require_request,
                                            read_result=self.read_result)
        core.WIN_ROOT = r"%LOCALAPPDATA%\unit-test-root"
        core.ps_quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        core.emitted = lambda value: [json.loads(line[10:]) for line in value.split("\n") if line.startswith("XDEV_JSON ")]
        core.run_ps = Mock(side_effect=self.remote)
        core.fail = lambda message, rc=1: rc
        files = types.ModuleType("xdev_files")
        files.push = Mock(side_effect=self.upload)
        files.recover_push = Mock(side_effect=self.recover_upload)
        files.pull_dir = Mock(return_value=0)
        spec = importlib.util.spec_from_file_location("tasks_under_test", Path(__file__).with_name("xdev_tasks.py"))
        self.client = importlib.util.module_from_spec(spec)
        modules = patch.dict(sys.modules, {"xdev_core": core, "xdev_files": files})
        modules.start()
        self.addCleanup(modules.stop)
        spec.loader.exec_module(self.client)
        self.core = core
        self.client._win_root = lambda: core.WIN_ROOT
        self.client.emitted = core.emitted
        self.client.run_ps = core.run_ps
        self.client.push = files.push
        self.client.recover_push = files.recover_push
        self.client.pull_dir = files.pull_dir
        self.client.fail = core.fail
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def receipt(self, row, rc=0, job_id=41):
        out = "XDEV_JSON " + json.dumps(row) + "\n"
        raw = dict(id=job_id, rc=rc, out=base64.b64encode(out.encode()).decode(), err="")
        (self.state / "results" / f"{job_id}.json").write_text(json.dumps(raw))
        return rc, out, "", job_id

    def require_request(self, job_id):
        request = json.loads((self.state / "queue" / f"{job_id}.json").read_text())
        self.assertEqual(request["mac"], hashlib.sha256(request["script"].encode()).hexdigest())

    def read_result(self, job_id):
        path = self.state / "results" / f"{job_id}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def record(self, event):
        with self.event_lock:
            self.events.append(event)

    @staticmethod
    def expanded(path):
        return path.replace("%LOCALAPPDATA%", r"C:\SyntheticLocal")

    def bound_terminal(self, name, rc=0):
        intent = json.loads((self.client._task_root(name) / "INTENT.json").read_text())
        return dict(ok=True, state="DONE", rc=rc, finished="FINISHED", kind="task", legacy=False,
            remote_dir=self.expanded(intent["remote_dir"]), out="", err="", owner=intent["owner"],
            task_name=intent["name"], requested_remote_dir=intent["remote_dir"], binding_verified=True)

    def remote(self, script, **kwargs):
        """模拟 CreateNew 的竞争语义；PowerShell 真机执行另行验收。"""
        with self.event_lock:
            self.next_id += 1
            job_id = self.next_id
        script_b64 = base64.b64encode((self.core.PREAMBLE + script).encode()).decode()
        (self.state / "queue" / f"{job_id}.json").write_text(json.dumps(dict(
            id=job_id, timeout=kwargs["timeout"], script=script_b64,
            mac=hashlib.sha256(script_b64.encode()).hexdigest())))
        if "# XDEV_TASK_RESERVE" in script:
            name, owner = re.search(r"\$Name = '([^']+)'; \$Owner = '([^']+)'", script).groups()
            self.assertIn("[IO.FileMode]::CreateNew", script)
            self.assertNotIn("Remove-Item", script)
            if self.barrier:
                self.barrier.wait(timeout=5)
            claim = self.root / (name.casefold() + ".claim")
            try:
                fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                self.record(("refused", name, owner))
                return self.receipt({"ok": False, "state": "REFUSED", "reason": "NAME_EXISTS"}, 3, job_id)
            with os.fdopen(fd, "w") as stream:
                stream.write(owner)
            self.record(("reserve", name, owner))
            return self.receipt({"ok": True, "state": "RESERVED", "owner": owner,
                                 "dir": "REMOTE_JOB", "remote_dir": self.expanded(self.client._work_dir(name))}, job_id=job_id)
        if "# XDEV_TASK_START" in script:
            self.assertIn("[IO.FileMode]::CreateNew", script)
            self.assertIn("OWNER_MISMATCH", script)
            name = re.search(r"\$J = Join-Path \$XRoot 'jobs\\([^']+)'", script).group(1)
            self.record(("start", name))
            return self.receipt({"ok": True, "state": "STARTING", "dir": "REMOTE_JOB", "runner_pid": 123}, job_id=job_id)
        if "# XDEV_TASK_STATUS" in script:
            if "TASK_OWNER_BINDING_MISMATCH" in script:
                name = re.search(r"\$J = Join-Path \$XRoot 'jobs\\([^']+)'", script).group(1)
                return self.receipt(self.bound_terminal(name), job_id=job_id)
            return self.receipt({"ok": True, "state": "DONE", "rc": 0, "finished": "FINISHED",
                                 "kind": "task", "remote_dir": "REMOTE_WORK", "out": "", "err": ""}, job_id=job_id)
        self.fail("unexpected remote operation")

    def upload(self, local, remote, **kwargs):
        name = remote.split("\\")[-2]
        self.assertTrue((self.root / (name.casefold() + ".claim")).exists(), "inputs must follow namespace reservation")
        self.record(("push", name, local, remote))
        transfer_id = uuid.uuid4().hex
        receipt = self.state / "transfers" / transfer_id
        receipt.mkdir()
        data = Path(local).read_bytes()
        intent = dict(operation="push", source=str(Path(local).absolute()), destination=remote,
                      transfer_id=transfer_id, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        (receipt / "INTENT.json").write_text(json.dumps(intent))
        self.push_outcomes[str(receipt)] = dict(ok=True, status="COMPLETE", transfer_id=transfer_id,
            receipt=str(receipt), job_id=100 + len(self.push_outcomes), rc=0,
            remote=dict(sha256=intent["sha256"], bytes=intent["bytes"]))
        kwargs["receipt_callback"](dict(intent, receipt=str(receipt)))
        return 0

    def recover_upload(self, receipt):
        return self.push_outcomes[str(receipt)]

    def unknown_upload(self, local, remote, **kwargs):
        self.upload(local, remote, **kwargs)
        receipt = next(reversed(self.push_outcomes))
        self.upload_success = (receipt, self.push_outcomes[receipt])
        self.push_outcomes[receipt] = dict(ok=False, status="UNKNOWN", receipt=receipt,
            transfer_id=Path(receipt).name, job_id=self.upload_success[1]["job_id"])
        return 124

    def delay_operation(self, stage):
        def delayed(script, **kwargs):
            original = self.remote(script, **kwargs)
            if f"# XDEV_TASK_{stage.upper()}" in script:
                job = original[3]
                path = self.state / "results" / f"{job}.json"
                self.delayed_result = (path, path.read_bytes())
                path.unlink()
                return 124, "", "", job
            return original
        self.client.run_ps.side_effect = delayed

    def test_unknown_push_reconciles_original_before_explicit_resume(self):
        self.client.push.side_effect = self.unknown_upload
        submitted = self.client.task_submit(["data.dat", "b.dat"], "exit 0", "resume")
        self.assertEqual(submitted["rc"], 124)
        task_root = Path(submitted["task_receipt"])
        handle = json.loads((task_root / "input-0000" / "TRANSFER.json").read_text())
        self.assertEqual(handle["receipt"], self.upload_success[0])
        before = len(self.events)
        self.assertEqual(self.client.task_status("resume")["state"], "UNKNOWN")
        self.assertEqual(self.client.task_resume("resume")["state"], "UNKNOWN")
        self.assertEqual(len(self.events), before)
        receipt, proof = self.upload_success
        self.push_outcomes[receipt] = proof
        observed = self.client.task_status("resume")
        self.assertEqual(observed["upload_status"], "PENDING")
        self.assertEqual(observed["pending_inputs"], [1])
        self.assertEqual(len(self.events), before, "status must not upload or start")
        self.client.push.side_effect = self.upload
        resumed = self.client.task_resume("resume")
        self.assertTrue(resumed["ok"], resumed)
        self.assertEqual([event[0] for event in self.events], ["reserve", "push", "push", "start"])
        self.assertEqual(submitted["owner"], resumed["owner"])
        self.assertEqual(submitted["reserve_job_id"], resumed["reserve_job_id"])
        self.assertEqual(self.client.task_resume("resume")["state"], "DONE")
        self.assertEqual(sum(event[0] == "start" for event in self.events), 1)

    def test_resume_refuses_changed_local_input_before_other_upload_or_start(self):
        self.client.push.side_effect = self.unknown_upload
        self.client.task_submit(["data.dat", "b.dat"], "exit 0", "changed")
        receipt, proof = self.upload_success
        self.push_outcomes[receipt] = proof
        Path("b.dat").write_text("changed after frozen intent")
        before = len(self.events)
        result = self.client.task_resume("changed")
        self.assertIn("INPUT_CHANGED", result["reason"])
        self.assertEqual(len(self.events), before)

    def test_failed_original_upload_never_retried(self):
        self.client.push.side_effect = self.unknown_upload
        self.client.task_submit(["data.dat"], "exit 0", "failed")
        receipt, proof = self.upload_success
        self.push_outcomes[receipt] = dict(proof, ok=False, status="FAILED", rc=4)
        for _ in range(2):
            result = self.client.task_resume("failed")
            self.assertEqual(result["state"], "FAILED")
            self.assertEqual(result["rc"], 4)
        self.assertEqual([event[0] for event in self.events], ["reserve", "push"])

    def test_unknown_reservation_uses_only_original_receipt(self):
        self.delay_operation("reserve")
        submitted = self.client.task_submit(["data.dat"], "exit 0", "reserve-late")
        self.assertEqual(submitted["rc"], 124)
        self.assertEqual(self.client.task_resume("reserve-late")["rc"], 124)
        self.assertEqual([event[0] for event in self.events], ["reserve"])
        path, raw = self.delayed_result
        path.write_bytes(raw)
        self.client.run_ps.side_effect = self.remote
        self.assertTrue(self.client.task_resume("reserve-late")["ok"])
        self.assertEqual([event[0] for event in self.events], ["reserve", "push", "start"])

    def test_unknown_start_never_restarts_even_if_remote_would_say_reserved(self):
        self.delay_operation("start")
        submitted = self.client.task_submit([], "exit 0", "start-late")
        self.assertEqual(submitted["rc"], 124)
        self.assertEqual(self.client.task_status("start-late")["rc"], 124)
        self.assertEqual(self.client.task_resume("start-late")["rc"], 124)
        self.assertEqual([event[0] for event in self.events], ["reserve", "start"])
        path, raw = self.delayed_result
        path.write_bytes(raw)
        self.client.run_ps.side_effect = self.remote
        self.assertEqual(self.client.task_resume("start-late")["state"], "DONE")
        self.assertEqual(sum(event[0] == "start" for event in self.events), 1)

    def test_request_return_gap_preserves_unknown_and_does_not_guess_queue_id(self):
        def interrupt(script, **kwargs):
            self.remote(script, **kwargs)
            raise OSError("crash after enqueue before return")
        self.client.run_ps.side_effect = interrupt
        result = self.client.task_submit([], "exit 0", "gap")
        self.assertFalse(result["ok"])
        self.client.run_ps.reset_mock()
        resumed = self.client.task_resume("gap")
        self.assertEqual(resumed["reason"], "ORIGINAL_QUEUE_ID_UNAVAILABLE")
        self.client.run_ps.assert_not_called()

    def test_status_without_handle_does_not_search_other_transfers(self):
        self.client.push.side_effect = OSError("no transfer handle")
        self.client.task_submit(["data.dat"], "exit 0", "no-handle")
        before = len(self.events)
        result = self.client.task_status("no-handle")
        self.assertEqual(result["reason"], "ORIGINAL_TRANSFER_HANDLE_UNAVAILABLE")
        self.assertEqual(len(self.events), before)
        self.client.recover_push.assert_not_called()

    def test_callback_hash_mismatch_blocks_before_upload_transport(self):
        def changing(local, remote, **kwargs):
            transfer_id = uuid.uuid4().hex
            receipt = self.state / "transfers" / transfer_id
            receipt.mkdir()
            kwargs["receipt_callback"](dict(receipt=str(receipt), transfer_id=transfer_id,
                destination=remote, sha256="0" * 64, bytes=1))
            self.fail("callback should have refused before transport")
        self.client.push.side_effect = changing
        result = self.client.task_submit(["data.dat"], "exit 0", "callback")
        self.assertIn("INPUT_CHANGED_BEFORE_UPLOAD", result["reason"])
        self.assertEqual([event[0] for event in self.events], ["reserve"])

    def test_original_body_change_cannot_start_reserved_task(self):
        self.client.push.side_effect = self.unknown_upload
        result = self.client.task_submit(["data.dat"], "exit 0", "body")
        path = Path(result["task_receipt"]) / "BODY.ps1"
        path.write_text("changed")
        self.assertEqual(self.client.task_resume("body")["reason"], "ORIGINAL_TASK_CODE_CHANGED")
        self.assertEqual(sum(event[0] == "start" for event in self.events), 0)

    def test_original_request_archive_or_queue_change_refused(self):
        self.delay_operation("reserve")
        result = self.client.task_submit([], "exit 0", "archive")
        path, raw = self.delayed_result
        path.write_bytes(raw)
        request = Path(result["task_receipt"]) / "reserve" / "INTENT.json"
        record = json.loads(request.read_text(encoding="utf-8"))
        record["script"] += "# changed"
        request.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(self.client.task_resume("archive")["reason"], "ORIGINAL_REQUEST_CHANGED")
        self.assertEqual([event[0] for event in self.events], ["reserve"])

    def test_remote_hash_guards_run_before_claim_and_inside_frozen_body(self):
        self.client.task_submit(["data.dat"], "exit 0", "guard")
        script = self.client.run_ps.call_args.args[0]
        self.assertLess(script.index("Get-FileHash"), script.index("$startClaim"))
        body = (self.client._task_root("guard") / "BODY.ps1").read_text()
        self.assertLess(body.index("Get-FileHash"), body.index("exit 0"))

    def test_task_lock_blocks_competing_resume_without_remote_effects(self):
        self.client.push.side_effect = self.unknown_upload
        result = self.client.task_submit(["data.dat"], "exit 0", "locked")
        before = len(self.events)
        with self.client._task_lock(Path(result["task_receipt"])):
            contender = self.client.task_resume("locked")
        self.assertEqual(contender["rc"], 75)
        self.assertEqual(len(self.events), before)

    def test_resume_without_original_journal_does_not_adopt_legacy(self):
        result = self.client.task_resume("legacy")
        self.assertEqual(result["reason"], "ORIGINAL_TASK_JOURNAL_REQUIRED")
        self.client.run_ps.assert_not_called()

    def test_journal_status_refuses_foreign_owner_directory_name_and_unbound_echo(self):
        self.assertTrue(self.client.task_submit([], "exit 0", "owned-original")["ok"])
        accepted = self.bound_terminal("owned-original")
        cases = [dict(accepted, owner="foreign-owner"),
                 dict(accepted, remote_dir=r"C:\foreign-task\private-output"),
                 dict(accepted, task_name="foreign-task"), dict(accepted, kind="detach"),
                 dict(accepted, requested_remote_dir="different-request"),
                 dict(accepted, binding_verified=False), dict(accepted, legacy=True)]
        for row in cases:
            with self.subTest(row=row):
                self.client.run_ps.side_effect = lambda script, **kw: self.receipt(row, job_id=901)
                status = self.client.task_status("owned-original")
                self.assertFalse(status["ok"])
                self.assertEqual(status["reason"], "TASK_STATUS_BINDING_MISMATCH")
                collected = self.client.task_collect("owned-original", "local-result")
                self.assertFalse(collected["ok"])
        self.client.pull_dir.assert_not_called()

    def test_collector_revalidates_binding_even_when_status_adapter_supplies_foreign_path(self):
        self.assertTrue(self.client.task_submit([], "exit 0", "owned-original")["ok"])
        foreign = dict(self.bound_terminal("owned-original"), remote_dir=r"C:\foreign-task\private-output")
        with patch.object(self.client, "task_status", return_value=foreign):
            result = self.client.task_collect("owned-original", "local-result")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "TASK_COLLECT_BINDING_MISMATCH")
        self.client.pull_dir.assert_not_called()

    def test_bound_status_expands_raw_directory_and_matches_original_reservation(self):
        self.assertTrue(self.client.task_submit([], "exit 0", "expected")["ok"])
        result = self.client.task_status("expected")
        self.assertTrue(result["ok"], result)
        script = self.client.run_ps.call_args.args[0]
        self.assertIn("$expectedResolvedDir = Expand $requestedRemoteDir", script)
        self.assertIn("$meta.owner -cne", script)
        self.assertIn("$meta.name -cne 'expected'", script)
        self.assertIn("[IO.Path]::GetFullPath([string]$meta.remote_dir)", script)
        self.assertIn("C:\\SyntheticLocal\\unit-test-root\\work\\expected", script)
        self.assertIn("%LOCALAPPDATA%\\unit-test-root\\work\\expected", script)
        collected = self.client.task_collect("expected", "local-result")
        self.assertTrue(collected["ok"], collected)
        self.client.pull_dir.assert_called_once_with(r"C:\SyntheticLocal\unit-test-root\work\expected", "local-result")

    def test_wait_and_tail_for_new_journal_cannot_accept_foreign_terminal(self):
        self.assertTrue(self.client.task_submit([], "exit 0", "wait-owned")["ok"])
        foreign = dict(self.bound_terminal("wait-owned"), owner="foreign-owner")
        self.client.run_ps.side_effect = lambda script, **kw: self.receipt(foreign, job_id=902)
        self.assertEqual(self.client.wait("wait-owned", max_seconds=1), 7)
        self.assertEqual(self.client.tail("wait-owned"), 7)
        self.client.pull_dir.assert_not_called()

    def test_same_name_race_has_one_owner_one_upload_one_start(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.client.task_submit, [f"{n}.dat"], "exit 0", "shared") for n in (1, 2)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(sum(row["ok"] for row in results), 1)
        self.assertEqual([row["rc"] for row in results if not row["ok"]], [3])
        self.assertEqual(len([e for e in self.events if e[0] == "push"]), 1)
        self.assertEqual(len([e for e in self.events if e[0] == "start"]), 1)
        owner = next(e[2] for e in self.events if e[0] == "reserve")
        self.assertEqual((self.root / "shared.claim").read_text(), owner)

    def test_windows_case_alias_cannot_acquire_second_namespace(self):
        first = self.client.task_submit(["a.dat"], "exit 0", "Shared")
        second = self.client.task_submit(["b.dat"], "exit 0", "shared")
        self.assertTrue(first["ok"])
        self.assertEqual(second["reason"], "NAME_EXISTS")
        self.assertEqual(self.client.push.call_count, 1)

    def test_legacy_name_refuses_before_input_upload_and_preserves_bytes(self):
        claim = self.root / "legacy.claim"
        claim.write_bytes(b"previous-owner-and-evidence")
        result = self.client.task_submit(["data.dat"], "exit 0", "legacy")
        self.assertEqual(result["reason"], "NAME_EXISTS")
        self.assertEqual(claim.read_bytes(), b"previous-owner-and-evidence")
        self.client.push.assert_not_called()
        script = self.client.run_ps.call_args.args[0]
        self.assertIn("Test-Path -LiteralPath $J", script)
        self.assertIn("Test-Path -LiteralPath $W", script)

    def test_full_uuid_default_is_unique_even_in_same_second(self):
        with patch.object(self.client.time, "strftime", return_value="20261115-000000"):
            results = [self.client.task_submit([], "exit 0") for _ in range(8)]
        self.assertEqual(len({r["name"] for r in results}), 8)
        for result in results:
            self.assertRegex(result["name"], r"^t20261115-000000-[0-9a-f]{32}$")

    def test_invalid_names_never_query_reserve_or_upload(self):
        for name in ("", ".", "..", "a.", "a\n", "../other", "bad';&", "CON", "nul.txt", "LPT1", "x" * 81):
            with self.subTest(name=name):
                self.assertEqual(self.client.task_submit(["data"], "exit 0", name)["rc"], 2)
                self.assertEqual(self.client.status(name)["rc"], 2)
                self.assertEqual(self.client.detach(name, "exit 0"), 2)
        self.client.run_ps.assert_not_called()
        self.client.push.assert_not_called()

    def test_duplicate_windows_basenames_rejected_before_reservation(self):
        for names in (["one/data.dat", "two/data.dat"], ["one/Data.dat", "two/data.DAT"], [r"one\data.dat", "two/data.dat"]):
            self.assertEqual(self.client.task_submit(names, "exit 0", "test")["reason"], "DUPLICATE_INPUT_BASENAME")
        self.client.run_ps.assert_not_called()
        self.client.push.assert_not_called()

    def test_invalid_input_windows_aliases_rejected(self):
        for path in ("NUL.txt", "data.", "data ", "bad:file", "bad\nfile", "../"):
            self.assertEqual(self.client.task_submit([path], "exit 0", "test")["reason"], "BAD_INPUT_BASENAME")
        self.client.run_ps.assert_not_called()

    def test_reservation_failed_missing_or_unknown_never_uploads(self):
        replies = [self.receipt({"ok": False, "state": "REFUSED", "reason": "NAME_EXISTS"}, 3),
                   (124, "", "", 51), (1, "", "failed", 52), (0, "", "", 53),
                   (0, "XDEV_JSON malformed\n", "", 54)]
        for number, reply in enumerate(replies):
            self.client.run_ps.reset_mock()
            self.client.run_ps.side_effect = None
            self.client.run_ps.return_value = reply
            result = self.client.task_submit(["data"], "exit 0", f"test{number}")
            self.assertFalse(result["ok"])
            self.assertEqual(result["stage"], "reserve")
            self.assertEqual(result["reserve_job_id"], reply[3])
            self.client.push.assert_not_called()
            self.assertEqual(self.client.run_ps.call_count, 1)

    def test_wrong_reservation_owner_never_uploads(self):
        self.client.run_ps.side_effect = None
        self.client.run_ps.return_value = self.receipt({"ok": True, "owner": "someone-else"})
        result = self.client.task_submit(["data"], "exit 0", "test")
        self.assertEqual(result["state"], "UNKNOWN")
        self.client.push.assert_not_called()

    def test_upload_failure_or_unknown_never_starts_or_collects(self):
        for number, rc in enumerate((4, 124)):
            self.client.push.side_effect = None
            self.client.push.return_value = rc
            result = self.client.task(["data"], "exit 0", "out", f"test{number}")
            self.assertFalse(result["ok"])
            self.assertEqual(result["rc"], rc)
            self.assertEqual(result["stage"], "push")
            self.assertEqual(result["state"], "UNKNOWN" if rc == 124 else "FAILED")
        self.assertFalse(any(e[0] == "start" for e in self.events))
        self.client.pull_dir.assert_not_called()

    def test_upload_exception_preserves_reserved_name(self):
        self.client.push.side_effect = OSError("transport interrupted")
        result = self.client.task_submit(["data"], "exit 0", "test")
        self.assertEqual(result["state"], "UNKNOWN")
        self.assertEqual(result["reserve_job_id"], 1)
        self.assertTrue((self.root / "test.claim").exists())
        self.assertFalse(any(e[0] == "start" for e in self.events))

    def test_start_failed_or_unknown_never_waits_or_collects(self):
        for number, response in enumerate(((124, "", "", 81), (1, "", "error", 82),
                                          self.receipt({"ok": False, "state": "UNKNOWN", "reason": "START_ALREADY_CLAIMED"}, 7, 83))):
            def remote(script, **kwargs):
                return response if "# XDEV_TASK_START" in script else self.remote(script, **kwargs)
            self.client.run_ps.side_effect = remote
            with patch.object(self.client, "_wait_status") as wait:
                result = self.client.task(["data"], "exit 0", "out", f"test{number}")
            self.assertFalse(result["ok"])
            self.assertEqual(result["stage"], "start")
            self.assertEqual(result["start_job_id"], response[3])
            wait.assert_not_called()
        self.client.pull_dir.assert_not_called()

    def test_status_validates_and_quotes_queries(self):
        self.client.status("safe-name_1")
        script = self.client.run_ps.call_args.args[0]
        self.assertIn("$J = Join-Path $XRoot 'jobs\\safe-name_1'", script)
        self.assertIn("Get-Process -Id ([int]$identity.pid)", script)
        self.assertIn("$process.StartTime.ToUniversalTime().ToString('o') -ceq $identity.birth", script)
        self.assertIn("LEGACY_IDENTITY_UNAVAILABLE", script)
        self.assertNotIn("-like ('*'", script)
        self.client.run_ps.reset_mock()
        for size in (-1, "1; exit", 1_000_001, True):
            self.assertEqual(self.client.status("safe", size)["reason"], "BAD_TAIL_SIZE")
        self.client.run_ps.assert_not_called()

    def test_status_transport_timeout_cannot_become_done(self):
        self.client.run_ps.side_effect = None
        self.client.run_ps.return_value = self.receipt({"ok": True, "state": "DONE", "rc": 0}, 124, 99)
        result = self.client.task_status("test")
        self.assertEqual(result["state"], "UNKNOWN")
        self.assertEqual(result["job_id"], 99)

    def test_malformed_terminal_rc_fails_closed(self):
        for rc in (None, True, 1.5, "not-a-number"):
            self.client.run_ps.side_effect = None
            self.client.run_ps.return_value = self.receipt({"ok": True, "state": "DONE", "rc": rc})
            self.assertEqual(self.client.task_status("test")["state"], "UNKNOWN")

    def test_nonterminal_state_never_collects(self):
        for state in ("UNKNOWN", "LOST", "RESERVED", "STARTING", "RUNNING"):
            with patch.object(self.client, "task_status", return_value={"ok": True, "state": state, "rc": None}):
                result = self.client.task_collect("test", "out")
            self.assertFalse(result["ok"])
        self.client.pull_dir.assert_not_called()

    def test_unknown_or_lost_wait_never_attempts_collect(self):
        for number, state in enumerate(("UNKNOWN", "LOST", "RUNNING")):
            with patch.object(self.client, "_wait_status", return_value={"ok": False, "state": state, "rc": 124 if state == "RUNNING" else 7}), \
                    patch.object(self.client, "task_collect") as collect:
                result = self.client.task([], "exit 0", "out", f"test{number}")
            self.assertFalse(result["ok"])
            self.assertEqual(result["state"], state)
            collect.assert_not_called()

    def test_engine_rc_and_collection_rc_propagate(self):
        for engine_rc, pull_rc, want in ((0, 0, 0), (23, 0, 23), (0, 5, 5), (23, 5, 23), (124, 0, 124)):
            self.client.pull_dir.return_value = pull_rc
            with patch.object(self.client, "task_status", return_value={"ok": True, "state": "DONE", "rc": engine_rc, "remote_dir": "REMOTE"}):
                result = self.client.task_collect("test", "out")
            self.assertEqual(result["rc"], engine_rc)
            self.assertEqual(result["pull_rc"], pull_rc)
            self.assertEqual(self.client.print_summary(result), want)

    def test_convenience_preserves_nonzero_rc_without_automatic_collection(self):
        for number, rc in enumerate((0, 7, 124)):
            terminal = {"ok": True, "state": "DONE", "rc": rc, "remote_dir": "REMOTE"}
            with patch.object(self.client, "_wait_status", return_value=terminal), \
                    patch.object(self.client, "task_status", side_effect=lambda name: self.bound_terminal(name, rc)):
                result = self.client.task([], f"exit {rc}", "out", f"test{number}")
            self.assertEqual(self.client.print_summary(result), rc)
            self.assertEqual(result["state"], "DONE")
        self.assertEqual(self.client.pull_dir.call_count, 1, "only DONE rc=0 may be collected automatically")

    def test_legacy_terminal_collect_uses_original_workdir(self):
        terminal = {"ok": True, "state": "DONE", "rc": 0, "legacy": True, "kind": "legacy", "remote_dir": "LEGACY_WORK"}
        with patch.object(self.client, "task_status", return_value=terminal):
            self.assertTrue(self.client.task_collect("legacy", "out")["ok"])
        self.client.pull_dir.assert_called_once_with("LEGACY_WORK", "out")

    def test_detach_uses_reservation_and_does_not_upload(self):
        self.assertEqual(self.client.detach("task", "exit 0"), 0)
        self.assertEqual([e[0] for e in self.events], ["reserve", "start"])
        self.client.push.assert_not_called()
        self.assertEqual(self.client.detach("task", "exit 0"), 3)

    def test_body_and_runner_keep_native_exit_code_and_atomic_terminal_receipt(self):
        body = self.client._body_script("& tool.exe", r"C:\work\O'Brien")
        self.assertIn("$ErrorActionPreference = 'Stop'", body)
        self.assertIn("$global:LASTEXITCODE = 0", body)
        self.assertIn("exit $LASTEXITCODE", body)
        self.assertIn("C:\\work\\O''Brien", body)
        self.assertIn("$env:TEMP = Join-Path $PSScriptRoot 'temp'; $env:TMP = $env:TEMP", body)
        self.assertIn("body.identity.json", body)
        self.assertIn("runner.identity.json", self.client.RUNNER)
        self.assertIn("StartTime.ToUniversalTime().ToString('o')", body)
        self.assertIn("[IO.File]::Move(($identityFile + '.part'), $identityFile)", body)
        self.assertIn("-WorkingDirectory $cwd", self.client.RUNNER)
        self.assertIn("$p.WaitForExit()", self.client.RUNNER)
        self.assertIn("[IO.File]::Move($tmp, (Join-Path $J 'rc.txt'))", self.client.RUNNER)

    def test_max_zero_issues_no_status_request(self):
        result = self.client._wait_status("test", 0)
        self.assertEqual(result["rc"], 124)
        self.client.run_ps.assert_not_called()

    def test_wait_passes_remaining_budget_to_status(self):
        with patch.object(self.client.time, "monotonic", side_effect=[100, 100.25, 101]), \
                patch.object(self.client, "status", return_value={"ok": True, "state": "RUNNING", "rc": None}) as status:
            result = self.client._wait_status("test", 1)
        self.assertEqual(result["rc"], 124)
        status.assert_called_once_with("test", nbytes=400, wait_seconds=0.75)

    def test_status_default_and_explicit_wait_are_bounded(self):
        self.client.status("test")
        self.assertEqual(self.client.run_ps.call_args.kwargs["wait"], 45)
        self.client.task_status("test", wait_seconds=0.125)
        self.assertEqual(self.client.run_ps.call_args.kwargs["wait"], 0.125)
        self.client.run_ps.reset_mock()
        self.assertEqual(self.client.status("test", wait_seconds=0)["rc"], 124)
        for wait in (-1, float("nan"), float("inf"), "bad"):
            self.assertEqual(self.client.status("test", wait_seconds=wait)["rc"], 2)
        self.client.run_ps.assert_not_called()

    def test_parser_import_needs_no_configuration(self):
        script_dir = str(Path(__file__).resolve().parent)
        code = "import argparse, sys; sys.path.insert(0, sys.argv[1]); import xdev_tasks; p=argparse.ArgumentParser(); xdev_tasks.add_parser(p.add_subparsers(dest='cmd')); p.parse_args(['task-status','name'])"
        result = subprocess.run([sys.executable, "-B", "-c", code, script_dir],
                                env=dict(os.environ, XDEV_CONFIG=str(self.root / "no-config.json")),
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_parser_and_dispatch_support_separate_lifecycle(self):
        parser = argparse.ArgumentParser()
        self.client.add_parser(parser.add_subparsers(dest="cmd", required=True))
        args = parser.parse_args(["task-submit", "-c", "exit 0", "--input", "data.dat", "--name", "cli"])
        self.assertEqual(self.client.run(args), 0)
        args = parser.parse_args(["task-status", "cli", "--bytes", "0"])
        self.assertEqual(self.client.run(args), 0)
        args = parser.parse_args(["task-collect", "cli", "--out", "out"])
        self.assertEqual(self.client.run(args), 0)
        args = parser.parse_args(["task-submit", "body.ps1", "-c", "exit 0"])
        self.assertEqual(self.client.run(args), 2)


class CrossModuleRecoveryTests(unittest.TestCase):
    """真实本机签名 queue/core/files/task 联调；只模拟 Windows 执行结果。"""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xdev-task-cross-module-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "state"
        for name in ("queue", "results", "files"):
            (self.state / name).mkdir(parents=True, exist_ok=True)
        (self.state / "secret").write_text("synthetic-offline-key-" + "a" * 48)
        config = self.root / "config.json"
        config.write_text(json.dumps(dict(state_dir=str(self.state), windows=dict(work_root=r"C:\synthetic-xdev"))))
        env = patch.dict(os.environ, {"XDEV_CONFIG": str(config), "WBAGENT_STATE": str(self.state)})
        env.start(); self.addCleanup(env.stop)
        saved_modules = {key: sys.modules.get(key) for key in ("wbagent", "xdev_core", "xdev_files")}
        for key in saved_modules:
            sys.modules.pop(key, None)
        original_path = sys.path[:]
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), original_path))
        def restore():
            for key, value in saved_modules.items():
                if value is None:
                    sys.modules.pop(key, None)
                else:
                    sys.modules[key] = value
        self.addCleanup(restore)
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import xdev_core
        import xdev_files
        self.core, self.files, self.wb = xdev_core, xdev_files, xdev_core.wbagent
        spec = importlib.util.spec_from_file_location("task_cross_subject", Path(__file__).with_name("xdev_tasks.py"))
        self.tasks = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.tasks)
        self.original_enqueue = self.wb.enqueue
        enqueuer = patch.object(self.wb, "enqueue", side_effect=self.enqueue)
        enqueuer.start(); self.addCleanup(enqueuer.stop)
        original_run = self.core.run_ps
        uploader = patch.object(self.files, "run_ps", side_effect=lambda script, **kw: original_run(script, **dict(kw, wait=0)))
        uploader.start(); self.addCleanup(uploader.stop)
        self.events = []
        self.source = self.root / "input.txt"
        self.source.write_bytes(b"synthetic frozen input\n")
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__(); self.addCleanup(redirect.__exit__, None, None, None)

    def publish(self, job, payload, rc=0):
        out = base64.b64encode(("XDEV_JSON " + json.dumps(payload) + "\r\n").encode()).decode()
        record = dict(id=job, rc=rc, out=out, err="", ms=1)
        record["mac"] = self.wb.sign("res", job, rc, out, "")
        self.assertEqual(self.wb.publish_result(record), "published")

    def enqueue(self, encoded, timeout):
        job = self.original_enqueue(encoded, timeout)
        script = base64.b64decode(encoded).decode()
        if "# XDEV_TASK_RESERVE" in script:
            name, owner = re.search(r"\$Name = '([^']+)'; \$Owner = '([^']+)'", script).groups()
            self.publish(job, dict(ok=True, state="RESERVED", owner=owner, dir="TEST_JOB",
                                  remote_dir=self.tasks._work_dir(name)))
            self.events.append(("reserve", job))
        elif "$ClaimBytes =" in script:
            transfer_id = re.search(r";transfer_id='([0-9a-f]{32})'", script).group(1)
            handle = json.loads((self.tasks._task_root("cross") / "input-0000" / "TRANSFER.json").read_text())
            self.assertEqual(handle["transfer_id"], transfer_id, "task handle must predate enqueue")
            original = json.loads((Path(handle["receipt"]) / "INTENT.json").read_text())
            self.pending_push = (job, dict(ok=True, dest=original["destination"],
                requested_dest=original["destination"], transfer_id=transfer_id,
                sha256=original["sha256"], bytes=original["bytes"]))
            self.events.append(("push", job))
        elif "# XDEV_TASK_START" in script:
            self.publish(job, dict(ok=True, state="STARTING", runner_pid=1234, dir="TEST_JOB"))
            self.events.append(("start", job))
        elif "# XDEV_TASK_STATUS" in script:
            intent = json.loads((self.tasks._task_root("cross") / "INTENT.json").read_text())
            self.publish(job, dict(ok=True, state="DONE", rc=0, finished="TEST_FINISHED",
                                  remote_dir=self.tasks._work_dir("cross"), kind="task", out="", err="",
                                  legacy=False, owner=intent["owner"], task_name="cross", binding_verified=True,
                                  requested_remote_dir=intent["remote_dir"]))
            self.events.append(("status", job))
        else:
            self.fail("unexpected transport command")
        return job

    def test_signed_queue_late_push_recovery_then_original_task_resume(self):
        first = self.tasks.task_submit([self.source], "exit 0", "cross")
        self.assertEqual(first["rc"], 124, first)
        handle_path = Path(first["task_receipt"]) / "input-0000" / "TRANSFER.json"
        handle = json.loads(handle_path.read_text())
        outcome_path = Path(handle["receipt"]) / "OUTCOME.json"
        original_outcome = outcome_path.read_bytes()
        self.assertEqual(self.tasks.task_status("cross")["state"], "UNKNOWN")
        self.assertEqual([e[0] for e in self.events], ["reserve", "push"])
        self.publish(*self.pending_push)
        observed = self.tasks.task_status("cross")
        self.assertEqual(observed["upload_status"], "COMPLETE", observed)
        self.assertEqual(outcome_path.read_bytes(), original_outcome)
        self.assertTrue((Path(handle["receipt"]) / "RECOVERY.json").exists())
        self.assertEqual(list((self.state / "files").iterdir()), [])
        self.assertTrue(self.tasks.task_resume("cross")["ok"])
        self.assertEqual(self.tasks.task_resume("cross")["state"], "DONE")
        self.assertEqual([e[0] for e in self.events], ["reserve", "push", "start", "status"])

    def test_callback_failure_after_handle_preserves_no_enqueue_and_missing_return(self):
        original_write = self.tasks._write_new
        def fail_after_handle(path, value):
            original_write(path, value)
            if path.name == "TRANSFER.json":
                raise OSError("simulated disk failure after durable handle")
        with patch.object(self.tasks, "_write_new", side_effect=fail_after_handle):
            first = self.tasks.task_submit([self.source], "exit 0", "cross")
        self.assertFalse(first["ok"])
        self.assertEqual([e[0] for e in self.events], ["reserve"])
        handle = json.loads((Path(first["task_receipt"]) / "input-0000" / "TRANSFER.json").read_text())
        transfer_root = Path(handle["receipt"])
        self.assertEqual(json.loads((transfer_root / "OUTCOME.json").read_text())["status"], "ABORTED_BEFORE_ENQUEUE")
        self.assertFalse((transfer_root / "0001-push" / "RETURN.json").exists())
        self.assertFalse(self.tasks.task_resume("cross")["ok"])
        self.assertEqual([e[0] for e in self.events], ["reserve"])
        self.assertEqual(list((self.state / "files").iterdir()), [])

    def test_original_signed_queue_mutation_cannot_resume_upload(self):
        first = self.tasks.task_submit([self.source], "exit 0", "cross")
        path = self.state / "queue" / f"{first['reserve_job_id']}.json"
        original = json.loads(path.read_text())
        original["script"] = base64.b64encode(b"altered command").decode()
        path.write_text(json.dumps(original))
        resumed = self.tasks.task_resume("cross")
        self.assertFalse(resumed["ok"])
        self.assertEqual([e[0] for e in self.events], ["reserve", "push"])

    def test_original_result_archive_race_fails_closed(self):
        first = self.tasks.task_submit([self.source], "exit 0", "cross")
        reader = self.wb.read_result
        def change_after_read(job):
            value = reader(job)
            if job == first["reserve_job_id"]:
                (self.state / "results" / f"{job}.json").write_text("{}")
            return value
        with patch.object(self.wb, "read_result", side_effect=change_after_read):
            resumed = self.tasks.task_resume("cross")
        self.assertEqual(resumed["reason"], "ORIGINAL_RESULT_CHANGED_DURING_RECOVERY")
        self.assertEqual([e[0] for e in self.events], ["reserve", "push"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
