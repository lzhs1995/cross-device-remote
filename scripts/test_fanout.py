"""离线验证持久 fanout；禁止使用真实配置、队列、Windows 或研究数据。"""

import argparse
import base64
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import xdev_clauder as client
import xdev_fanout as fanout


class FanoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fake_core = SimpleNamespace(STATE=self.root / "offline-queue")
        core_patch = patch.dict(sys.modules, {"xdev_core": self.fake_core})
        core_patch.start()
        self.addCleanup(core_patch.stop)
        self.state = self.root / "state"
        self.state.mkdir()
        self.binding = {"session_name": "offline", "port": 8787, "r_pid": 101,
                        "rstudio_pid": 100, "r_birth_utc": "2026-11-15T00:00:00Z", "binding_id": "offlinebinding"}
        client.write_new(self.state / "BINDING.json", self.binding)
        self.grant = {"schema_version": 1, "status": "GRANTED", "grant_id": "offline-grant",
                      "run_id": "run1", "binding_id": self.binding["binding_id"], "max_concurrency": 3,
                      "expires_at_utc": "2100-01-01T00:00:00Z"}
        self.grant_path = self.root / "external-admission.json"
        self.grant_path.write_text(json.dumps(self.grant), encoding="utf-8")
        self.contract = {"schema_version": 1, "batch_concurrency": 2,
                         "external_host_admission": {"receipt_file": self.grant_path.name,
                                                     "receipt_sha256": fanout.digest(self.grant_path.read_bytes())},
                         "members": []}
        for index in range(3):
            code = f'cat("worker-{index} 中文 $HOME `literal`", "\\n")\r\n'
            path = self.root / f"worker-{index}.R"
            path.write_bytes(code.encode("utf-8"))
            self.contract["members"].append({"member_id": f"m{index}", "code_file": path.name,
                                             "code_sha256": fanout.digest(code.encode("utf-8")), "independent": True,
                                             "remote_work_dir": f"C:\\offline\\m{index}\\work",
                                             "remote_temp_dir": f"C:\\offline\\m{index}\\tmp"})
        self.contract_path = self.root / "contract.json"
        self.write_contract()
        self.calls = []
        self.poll_status = "running"
        self.poll_success = True
        self.custom = None

    def write_contract(self):
        self.contract_path.write_text(json.dumps(self.contract), encoding="utf-8")

    def args(self, action="fanout-submit", **extra):
        values = {"state": str(self.state), "contract": str(self.contract_path), "run_id": "run1",
                  "action": action, "agent_id": "offline", "http_timeout": 25, "wait": 0, "max_polls": 8}
        values.update(extra)
        return argparse.Namespace(**values)

    @property
    def directory(self):
        return self.state / "fanout" / "run1"

    def intent(self):
        return client.read(self.directory / "INTENT.json")

    def invoke(self, directory, script, http_timeout, wait):
        saved = client.read(directory / "INTENT.json")
        payload, request_id = saved["payload"], saved["request_id"]
        self.assertEqual(script, client.request_script(self.binding, request_id, payload, http_timeout))
        self.calls.append(saved)
        if payload.get("async"):
            response = {"success": True, "job_id": payload["job_id"]}
        else:
            response = {"status": self.poll_status, "metadata": {"job_id": payload["check_job"]}}
            if self.poll_status == "complete":
                response["success"] = self.poll_success
        row = {"status": "HTTP_RESPONSE", "request_id": request_id, "payload_sha256": fanout.payload_sha(payload),
               "transport": fanout.TRANSPORT, "native_mcp_verified": False, "response": response}
        if self.custom:
            row = self.custom(saved, row)
        if row is None:
            raise TimeoutError("offline interruption after original queue submission")
        return client.save_response(directory, 0, "XDEV_JSON " + json.dumps(row), "", len(self.calls), 0.01)

    def run_action(self, action="fanout-submit", **extra):
        with patch.object(client, "invoke", side_effect=self.invoke), contextlib.redirect_stdout(io.StringIO()):
            return client.run(self.args(action, **extra))

    def statuses(self):
        return [fanout.member_status(self.directory, member)["status"] for member in self.intent()["members"]]

    def all_complete(self):
        self.assertEqual(self.run_action(), 0)
        self.poll_status = "complete"
        self.assertEqual(self.run_action("fanout-poll"), 0)
        self.assertEqual(self.run_action(), 0)
        self.assertEqual(self.run_action("fanout-poll"), 0)
        self.assertEqual(self.statuses(), ["SUCCEEDED"] * 3)

    def test_batch_cap_and_repeat_submission_keep_original_ids(self):
        self.assertEqual(self.run_action(), 0)
        self.assertEqual(self.run_action(), 0)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.statuses(), ["SUBMITTED", "SUBMITTED", "PENDING"])
        for call, member in zip(self.calls, self.intent()["members"]):
            self.assertEqual(call["request_id"], member["request_id"])
            self.assertEqual(call["payload"]["job_id"], member["job_id"])
            self.assertTrue(call["payload"]["async"])
        summary = client.read(self.directory / "SUMMARY.json")
        self.assertEqual(summary["capacity_scope"], "BATCH_ONLY")
        self.assertFalse(summary["external_host_admission_enforced_by_xdev"])

    def test_code_bytes_preserved_and_hash_bound(self):
        self.run_action()
        for call, member in zip(self.calls, self.intent()["members"]):
            raw = (self.root / member["code_file"]).read_bytes()
            submitted = call["payload"]["code"]
            self.assertTrue(submitted.encode("utf-8").endswith(raw))
            self.assertEqual(fanout.digest(raw), member["code_sha256"])
            self.assertEqual(fanout.digest(submitted.encode("utf-8")), member["submitted_code_sha256"])
            self.assertIn("Sys.setenv(TMP = .xdev_temp, TEMP = .xdev_temp, TMPDIR = .xdev_temp)", submitted)
            self.assertIn("setwd(.xdev_work)", submitted)
            self.assertNotIn("dir.create", submitted)

    def test_contract_and_code_mismatches_do_not_contact_transport(self):
        self.run_action()
        calls = len(self.calls)
        self.contract["batch_concurrency"] = 3
        self.write_contract()
        with self.assertRaisesRegex(ValueError, "contract"):
            self.run_action()
        self.contract["batch_concurrency"] = 2
        self.write_contract()
        (self.root / "worker-0.R").write_text("stop('changed')", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "code_sha256"):
            self.run_action()
        self.assertEqual(len(self.calls), calls)

    def test_binding_change_rejected_before_transport(self):
        self.run_action()
        changed = {**self.binding, "r_pid": 999}
        (self.state / "BINDING.json").write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "binding"):
            self.run_action("fanout-poll")
        self.assertEqual(len(self.calls), 2)

    def test_poll_and_merge_use_original_snapshots_after_source_removed(self):
        self.contract["batch_concurrency"] = 3
        self.write_contract()
        self.run_action()
        for member in self.contract["members"]:
            (self.root / member["code_file"]).unlink()
        self.poll_status = "complete"
        self.assertEqual(self.run_action("fanout-poll"), 0)
        self.assertEqual(self.run_action("fanout-merge"), 0)

    def test_partial_unknown_freezes_new_members_and_never_replays(self):
        self.contract["batch_concurrency"] = 3
        self.write_contract()
        self.custom = lambda call, row: {"status": "UNKNOWN_HTTP_OUTCOME"} if len(self.calls) == 2 else row
        self.assertEqual(self.run_action(), 124)
        self.assertEqual(self.run_action(), 124)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.statuses(), ["SUBMITTED", "UNKNOWN", "PENDING"])
        self.custom = None
        self.assertEqual(self.run_action("fanout-poll"), 0)
        self.assertEqual(self.run_action(), 0)
        submissions = [call for call in self.calls if call["payload"].get("async")]
        self.assertEqual(len(submissions), 3)
        self.assertEqual(len({call["request_id"] for call in submissions}), 3)
        self.assertEqual(len({call["payload"]["job_id"] for call in submissions}), 3)

    def test_exception_preserves_intent_and_does_not_poll_before_submit_reconciled(self):
        self.custom = lambda call, row: None
        self.assertEqual(self.run_action(), 124)
        self.assertEqual(self.run_action(), 124)
        self.assertEqual(len(self.calls), 1)
        self.custom = None
        self.run_action("fanout-poll")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.statuses(), ["UNKNOWN", "PENDING", "PENDING"])

    def test_running_is_normal_and_poll_never_submits(self):
        self.run_action()
        self.assertEqual(self.run_action("fanout-poll"), 0)
        self.assertEqual(self.statuses(), ["RUNNING", "RUNNING", "PENDING"])
        self.assertTrue(all("check_job" in call["payload"] for call in self.calls[2:]))
        self.assertEqual(self.run_action("fanout-merge"), 4)
        self.assertFalse((self.directory / "MERGED.json").exists())

    def test_bounded_polls_rotate_fairly_across_repeat_invocations(self):
        self.contract["batch_concurrency"] = 3
        self.write_contract()
        self.run_action()
        for index in range(4):
            previous = len(self.calls)
            self.run_action("fanout-poll", max_polls=1)
            self.assertEqual(len(self.calls) - previous, 1)
            self.assertEqual(self.calls[-1]["payload"]["check_job"], self.intent()["members"][index % 3]["job_id"])

    def test_terminal_error_stops_pending_dispatch_and_merge(self):
        self.run_action()
        self.poll_status, self.poll_success = "complete", False
        self.assertEqual(self.run_action("fanout-poll"), 3)
        calls = len(self.calls)
        self.assertEqual(self.run_action(), 3)
        self.assertEqual(self.run_action("fanout-poll"), 3)
        self.assertEqual(self.run_action("fanout-merge"), 3)
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(self.statuses(), ["FAILED", "FAILED", "PENDING"])

    def test_complete_lifecycle_merges_idempotently_without_transfers_or_science_claim(self):
        self.all_complete()
        calls = len(self.calls)
        self.assertEqual(self.run_action("fanout-poll"), 0)
        self.assertEqual(self.run_action("fanout-merge"), 0)
        original = (self.directory / "MERGED.json").read_bytes()
        self.assertEqual(self.run_action("fanout-merge"), 0)
        self.assertEqual((self.directory / "MERGED.json").read_bytes(), original)
        self.assertEqual(len(self.calls), calls)
        result = client.read(self.directory / "MERGED.json")
        self.assertEqual(result["scientific_acceptance"], "NOT_EVALUATED")
        self.assertEqual(result["files_transferred"], [])
        self.assertFalse(result["merge_code_executed"])
        self.assertFalse(result["native_mcp_verified"])
        self.assertEqual(result["transport"], fanout.TRANSPORT)
        self.assertEqual(len(result["members"]), 3)

    def test_not_found_remains_unknown_and_no_submission_replayed(self):
        self.run_action()
        self.poll_status = "not_found"
        self.assertEqual(self.run_action("fanout-poll"), 124)
        calls = len(self.calls)
        self.assertEqual(self.run_action(), 124)
        self.assertEqual(len(self.calls), calls)

    def test_late_original_poll_receipt_is_reconciled_before_fresh_query(self):
        self.run_action()
        saved = []

        def pending(directory, script, http_timeout, wait):
            saved.append(client.read(directory / "INTENT.json"))
            client.write_new(directory / "QUEUE.json", {"job_id": 300})
            return {"returncode": 124, "rows": [], "queue_id": 300}

        with patch.object(client, "invoke", side_effect=pending), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(client.run(self.args("fanout-poll", max_polls=1)), 124)
        request = saved[0]
        row = {"status": "HTTP_RESPONSE", "request_id": request["request_id"],
               "payload_sha256": fanout.payload_sha(request["payload"]), "transport": fanout.TRANSPORT,
               "native_mcp_verified": False, "response": {"status": "complete", "success": True}}
        results = self.fake_core.STATE / "results"
        results.mkdir(parents=True)
        client.write_new(results / "300.json", {"rc": 0, "out": base64.b64encode(("XDEV_JSON " + json.dumps(row)).encode()).decode(), "err": ""})
        # 游标先处理第二个成员，然后下一轮接回第一个成员的原迟到回执。
        self.run_action("fanout-poll", max_polls=1)
        self.run_action("fanout-poll", max_polls=1)
        self.assertEqual(self.statuses()[0], "SUCCEEDED")
        self.assertFalse(any(call["payload"].get("check_job") == request["job_id"] for call in self.calls))
        self.assertTrue((self.state / "requests" / request["request_id"] / "late-result" / "RESPONSE.json").exists())

    def test_explicit_submit_failure_does_not_dispatch_other_members(self):
        self.custom = lambda call, row: {**row, "response": {"success": False, "error": "offline denied"}}
        self.assertEqual(self.run_action(), 3)
        self.assertEqual(self.run_action(), 3)
        self.assertEqual(self.statuses(), ["FAILED", "PENDING", "PENDING"])
        self.assertEqual(len(self.calls), 1)

    def test_aggregate_progress_is_bounded_and_original_receipt_preserved(self):
        self.run_action()
        self.custom = lambda call, row: {**row, "response": {**row["response"],
                         "progress": {"stage": "running", "message": "中文" * 10000, "updated_at": "2026-11-15T00:00:00Z"}}}
        self.run_action("fanout-poll")
        summary = client.read(self.directory / "SUMMARY.json")
        self.assertLessEqual(len(json.dumps(summary, ensure_ascii=False).encode("utf-8")) + 12, 65536)
        self.assertLessEqual(len(summary["members"][0]["progress"]["message"].encode("utf-8")), 128)
        self.assertTrue(summary["members"][0]["progress_truncated"])
        report_path = Path(summary["full_progress_report"]["path"])
        self.assertEqual(summary["full_progress_report"]["sha256"], fanout.digest(report_path.read_bytes()))
        report = client.read(report_path)
        self.assertEqual(report["members"][0]["progress"]["message"], "中文" * 10000)
        self.assertEqual(report["members"][0]["progress"]["stage"], "running")
        self.assertEqual(report["members"][0]["progress"]["updated_at"], "2026-11-15T00:00:00Z")
        self.assertEqual(report["members"][0]["job_id"], self.intent()["members"][0]["job_id"])
        self.assertIsNone(report["members"][0]["elapsed_seconds"])
        self.assertIsNone(report["members"][0]["observed_poll_interval_seconds"])
        latest = fanout.member_status(self.directory, self.intent()["members"][0])
        self.assertEqual(len(latest["receipt"]["response"]["progress"]["message"]), 20000)

    def test_wrong_request_payload_transport_or_job_receipt_is_not_accepted(self):
        self.run_action()
        member = self.intent()["members"][0]
        payload = {"check_job": member["job_id"]}
        row = {"status": "HTTP_RESPONSE", "request_id": "original-poll", "payload_sha256": fanout.payload_sha(payload),
               "transport": fanout.TRANSPORT, "native_mcp_verified": False,
               "response": {"status": "complete", "success": True, "metadata": {"job_id": member["job_id"]}}}
        changes = [{"request_id": "other"}, {"payload_sha256": "0" * 64}, {"transport": "MCP"},
                   {"native_mcp_verified": True}, {"response": {"status": "complete", "success": True, "job_id": "other"}},
                   {"response": {"status": "complete"}}]
        for change in changes:
            with self.subTest(change=change):
                result = {"returncode": 0, "rows": [{**row, **change}]}
                self.assertEqual(fanout.classify(result, "original-poll", payload, member["job_id"])[0], "UNKNOWN")

    def test_merge_revalidates_actual_terminal_receipt(self):
        self.all_complete()
        member = self.intent()["members"][0]
        current = fanout.member_status(self.directory, member)
        current["receipt"]["response"]["success"] = False
        fanout.event(self.directory, member, "SUCCEEDED", request_id=current["request_id"],
                     payload=current["payload"], receipt=current["receipt"])
        with self.assertRaisesRegex(ValueError, "exact original"):
            self.run_action("fanout-merge")
        self.assertFalse((self.directory / "MERGED.json").exists())

    def test_foreign_submit_claim_cannot_be_polled_or_merged(self):
        self.run_action()
        member = self.intent()["members"][0]
        path = self.state / "jobs" / member["job_id"] / "SUBMIT_INTENT.json"
        path.write_text(json.dumps({"request_id": "foreign", "code_sha256": "0" * 64}), encoding="utf-8")
        self.assertEqual(self.run_action("fanout-poll"), 124)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[-1]["payload"]["check_job"], self.intent()["members"][1]["job_id"])

    def test_pretransport_crash_retains_unknown_identity(self):
        original_send = fanout.send
        with patch.object(fanout, "send", side_effect=OSError("offline crash before request intent")):
            self.assertEqual(self.run_action(), 124)
        self.assertEqual(self.run_action(), 124)
        self.assertEqual(self.run_action("fanout-poll"), 124)
        self.assertEqual(len(self.calls), 0)
        self.assertEqual(fanout.send, original_send)

    def test_same_run_concurrent_submission_is_exclusive(self):
        entered, release = threading.Event(), threading.Event()
        result, failure = [], []
        original_invoke = self.invoke

        def blocking(*args):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test synchronization timed out")
            return original_invoke(*args)

        def first():
            try:
                result.append(client.run(self.args()))
            except BaseException as exc:
                failure.append(exc)

        with patch.object(client, "invoke", side_effect=blocking), contextlib.redirect_stdout(io.StringIO()):
            thread = threading.Thread(target=first)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(client.run(self.args()), 75)
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failure, [])
        self.assertEqual(result, [0])
        self.assertEqual(len(self.calls), 2)

    def test_process_global_stderr_redirect_cannot_mix_queue_ids_across_runs(self):
        entered, release = threading.Event(), threading.Event()
        active, peak, failures, results = [0], [0], [], []
        paths = [self.root / "journal-a", self.root / "journal-b"]
        for path in paths:
            path.mkdir()

        def run_ps(script, timeout, wait):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            entered.set()
            if not release.wait(5):
                raise TimeoutError("offline queue synchronization")
            sys.stderr.write(f"XDEV_QUEUED id={script}\n")
            active[0] -= 1
            return 0, 'XDEV_JSON {"status":"offline"}', "", int(script)

        def invoke(index):
            try:
                results.append(client.invoke(paths[index], str(100 + index), 25, 0))
            except BaseException as exc:
                failures.append(exc)

        with patch.dict(sys.modules, {"xdev_core": SimpleNamespace(run_ps=run_ps)}):
            threads = [threading.Thread(target=invoke, args=(index,)) for index in range(2)]
            threads[0].start()
            self.assertTrue(entered.wait(5))
            threads[1].start()
            release.set()
            for thread in threads:
                thread.join(5)
                self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(peak[0], 1)
        for index, path in enumerate(paths):
            self.assertEqual(client.read(path / "QUEUE.json")["job_id"], 100 + index)

    def test_original_member_set_and_admission_bytes_are_validated_again(self):
        self.run_action()
        original = self.intent()
        changed = copy.deepcopy(original)
        changed["members"].pop()
        (self.directory / "INTENT.json").write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "member set"):
            self.run_action("fanout-poll")
        changed = copy.deepcopy(original)
        changed["external_host_admission_receipt"]["original_utf8"] += " "
        (self.directory / "INTENT.json").write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "admission receipt"):
            self.run_action("fanout-poll")
        self.assertEqual(len(self.calls), 2)

    def test_strict_contract_rejects_unapproved_or_dependent_members_and_arbitrary_merge(self):
        base = copy.deepcopy(self.contract)
        variants = []
        for mutation in (lambda c: c.pop("external_host_admission"),
                         lambda c: c["external_host_admission"].update(approved=False),
                         lambda c: c["members"][0].update(independent=False),
                         lambda c: c.update(batch_concurrency=True),
                         lambda c: c.update(batch_concurrency=33),
                         lambda c: c.update(merge_code="system('arbitrary')"),
                         lambda c: c["members"].append(copy.deepcopy(c["members"][0]))):
            variant = copy.deepcopy(base)
            mutation(variant)
            variants.append(variant)
        for variant in variants:
            with self.subTest(variant=variant):
                self.contract = variant
                self.write_contract()
                with self.assertRaises(ValueError):
                    self.run_action()
        self.assertEqual(self.calls, [])

    def test_external_admission_is_pinned_and_scoped_to_original_run_binding_capacity_and_expiry(self):
        original = copy.deepcopy(self.grant)
        variants = [{"run_id": "another-run"}, {"binding_id": "another-binding"},
                    {"max_concurrency": 1}, {"expires_at_utc": "2000-01-01T00:00:00Z"}, {"status": "REVOKED"}]
        for change in variants:
            with self.subTest(change=change):
                self.grant_path.write_text(json.dumps({**original, **change}), encoding="utf-8")
                self.contract["external_host_admission"]["receipt_sha256"] = fanout.digest(self.grant_path.read_bytes())
                self.write_contract()
                with self.assertRaises(ValueError):
                    self.run_action()
        self.grant_path.write_text(json.dumps(original), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "receipt_sha256"):
            self.run_action()
        self.assertEqual(self.calls, [])

    def test_member_remote_directories_must_be_isolated_and_absolute(self):
        for path in ("relative/work", "C:\\offline\\m0\\work", "C:\\offline\\m0", "C:\\offline\\m0\\work\\child"):
            with self.subTest(path=path):
                self.contract["members"][1]["remote_work_dir"] = path
                self.write_contract()
                with self.assertRaises(ValueError):
                    self.run_action()
        self.assertEqual(self.calls, [])

    def test_case_alias_members_are_rejected_before_any_write_even_with_same_code(self):
        first, second = self.contract["members"][:2]
        first["member_id"], second["member_id"] = "m", "M"
        second["code_file"], second["code_sha256"] = first["code_file"], first["code_sha256"]
        self.write_contract()
        with self.assertRaisesRegex(ValueError, "ignoring case"):
            self.run_action()
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.calls, [])

    def test_run_id_and_state_directory_case_aliases_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "lowercase"):
            self.run_action(run_id="Run1")
        self.assertFalse((self.state / "fanout").exists())
        alias = self.root / "STATE"
        with self.assertRaisesRegex(ValueError, "Case alias"):
            self.run_action(state=str(alias))
        self.assertEqual(self.calls, [])

    def test_windows_extended_device_unc_and_drive_aliases_are_rejected(self):
        aliases = [r"\\?\C:\offline\m0\work", r"\\.\C:\offline\m0\work", r"\??\C:\offline\m0\work",
                   r"\\server\share\member", r"\\?\UNC\server\share\member", r"C:relative"]
        for alias in aliases:
            with self.subTest(alias=alias):
                self.contract["members"][1]["remote_work_dir"] = alias
                self.write_contract()
                with self.assertRaises(ValueError):
                    self.run_action()
        self.assertEqual(self.calls, [])

    def test_repeated_poll_waits_on_one_unresolved_original_query_without_stacking(self):
        self.contract["members"] = self.contract["members"][:1]
        self.write_contract()
        self.run_action()

        def pending(directory, script, http_timeout, wait):
            client.write_new(directory / "QUEUE.json", {"job_id": 400})
            return {"returncode": 124, "queue_id": 400, "rows": []}

        with patch.object(client, "invoke", side_effect=pending) as transport, contextlib.redirect_stdout(io.StringIO()):
            for _ in range(4):
                self.assertEqual(client.run(self.args("fanout-poll", max_polls=1)), 124)
            self.assertEqual(transport.call_count, 1)
        requests = list((self.state / "requests").glob("fo-poll-*"))
        self.assertEqual(len(requests), 1)

    def test_earlier_late_terminal_dominates_later_not_found_from_old_stacked_polls(self):
        self.contract["members"] = self.contract["members"][:1]
        self.write_contract()
        self.run_action()
        member = self.intent()["members"][0]
        payload = {"check_job": member["job_id"]}
        original_id = "fo-poll-original"

        def pending(directory, script, http_timeout, wait):
            client.write_new(directory / "QUEUE.json", {"job_id": 401})
            return {"returncode": 124, "queue_id": 401, "rows": []}

        with patch.object(client, "invoke", side_effect=pending):
            fanout.send(self.directory, self.state, self.binding, member, original_id, payload, self.args())
        # 模拟升级前已经追加、并返回 not_found 的第二个原请求；不能删除这些历史证据。
        self.poll_status = "not_found"
        with patch.object(client, "invoke", side_effect=self.invoke):
            fanout.send(self.directory, self.state, self.binding, member, "fo-poll-later", payload, self.args())
        row = {"status": "HTTP_RESPONSE", "request_id": original_id, "payload_sha256": fanout.payload_sha(payload),
               "transport": fanout.TRANSPORT, "native_mcp_verified": False,
               "response": {"status": "complete", "success": True}}
        results = self.fake_core.STATE / "results"
        results.mkdir(parents=True)
        client.write_new(results / "401.json", {"rc": 0, "out": base64.b64encode(("XDEV_JSON " + json.dumps(row)).encode()).decode(), "err": ""})
        calls = len(self.calls)
        self.assertEqual(self.run_action("fanout-poll"), 0)
        self.assertEqual(len(self.calls), calls)
        current = fanout.member_status(self.directory, member)
        self.assertEqual(current["status"], "SUCCEEDED")
        self.assertEqual(current["request_id"], original_id)
        self.assertEqual(self.run_action("fanout-merge"), 0)
        self.assertTrue((self.state / "requests" / "fo-poll-later" / "RESPONSE.json").exists())

    def test_conflicting_exact_original_terminals_stay_unknown_and_block_merge(self):
        self.contract["members"] = self.contract["members"][:1]
        self.write_contract()
        self.run_action()
        member = self.intent()["members"][0]
        payload = {"check_job": member["job_id"]}
        self.poll_status = "complete"
        with patch.object(client, "invoke", side_effect=self.invoke):
            fanout.send(self.directory, self.state, self.binding, member, "fo-poll-first", payload, self.args())
            self.poll_success = False
            fanout.send(self.directory, self.state, self.binding, member, "fo-poll-second", payload, self.args())
        self.assertEqual(self.run_action("fanout-poll"), 124)
        self.assertEqual(len(self.calls), 3)
        with self.assertRaisesRegex(ValueError, "conflicting exact original"):
            self.run_action("fanout-merge")
        self.assertFalse((self.directory / "MERGED.json").exists())

    def test_success_event_cannot_substitute_for_original_running_archive(self):
        self.contract["members"] = self.contract["members"][:1]
        self.write_contract()
        self.run_action()
        self.run_action("fanout-poll")
        member = self.intent()["members"][0]
        current = fanout.member_status(self.directory, member)
        raw_path = self.state / "requests" / current["request_id"] / "RESPONSE.json"
        altered = copy.deepcopy(current["receipt"])
        altered["response"].update(status="complete", success=True)
        fanout.event(self.directory, member, "SUCCEEDED", request_id=current["request_id"],
                     payload=current["payload"], receipt=altered)
        with self.assertRaisesRegex(ValueError, "conflicting exact original"):
            self.run_action("fanout-merge")
        self.assertEqual(client.read(raw_path)["response"]["status"], "running")
        self.assertEqual(self.statuses(), ["UNKNOWN"])
        self.assertFalse((self.directory / "MERGED.json").exists())

    def test_terminal_request_intent_must_exist_and_bind_original_job_and_session(self):
        self.contract["members"] = self.contract["members"][:1]
        self.write_contract()
        self.run_action()
        self.poll_status = "complete"
        self.run_action("fanout-poll")
        member = self.intent()["members"][0]
        current = fanout.member_status(self.directory, member)
        path = self.state / "requests" / current["request_id"] / "INTENT.json"
        original = path.read_bytes()
        path.unlink()
        with self.assertRaisesRegex(ValueError, "exact original"):
            self.run_action("fanout-merge")
        for update in ({"job_id": "foreign"}, {"binding_sha256": "0" * 64}, {"payload": {"check_job": "foreign"}}):
            with self.subTest(update=update):
                path.write_text(json.dumps({**json.loads(original), **update}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "exact original"):
                    self.run_action("fanout-merge")
        path.write_bytes(original)
        self.assertEqual(self.run_action("fanout-merge"), 0)

    def test_existing_merge_rechecks_original_raw_hash_and_returncode(self):
        self.contract["members"] = self.contract["members"][:1]
        self.write_contract()
        self.run_action()
        self.poll_status = "complete"
        self.run_action("fanout-poll")
        self.assertEqual(self.run_action("fanout-merge"), 0)
        merged = (self.directory / "MERGED.json").read_bytes()
        current = fanout.member_status(self.directory, self.intent()["members"][0])
        path = self.state / "requests" / current["request_id"] / "RAW_TRANSPORT.json"
        original = path.read_bytes()
        for changed in (original + b" ", json.dumps({**json.loads(original), "returncode": 3}).encode("utf-8")):
            path.write_bytes(changed)
            with self.assertRaisesRegex(ValueError, "exact original"):
                self.run_action("fanout-merge")
        self.assertEqual((self.directory / "MERGED.json").read_bytes(), merged)

    def test_late_terminal_archived_hash_change_blocks_merge(self):
        self.contract["members"] = self.contract["members"][:1]
        self.write_contract()
        self.run_action()
        member = self.intent()["members"][0]
        payload = {"check_job": member["job_id"]}
        request_id = "fo-poll-late-hash"

        def pending(directory, script, http_timeout, wait):
            client.write_new(directory / "QUEUE.json", {"job_id": 402})
            return {"returncode": 124, "queue_id": 402, "rows": []}

        with patch.object(client, "invoke", side_effect=pending):
            fanout.send(self.directory, self.state, self.binding, member, request_id, payload, self.args())
        row = {"status": "HTTP_RESPONSE", "request_id": request_id, "payload_sha256": fanout.payload_sha(payload),
               "transport": fanout.TRANSPORT, "native_mcp_verified": False,
               "response": {"status": "complete", "success": True}}
        results = self.fake_core.STATE / "results"
        results.mkdir(parents=True)
        client.write_new(results / "402.json", {"rc": 0, "out": base64.b64encode(("XDEV_JSON " + json.dumps(row)).encode()).decode(), "err": ""})
        self.run_action("fanout-poll")
        current = fanout.member_status(self.directory, member)
        self.assertEqual(current["status"], "SUCCEEDED")
        path = self.state / current["evidence"]["response"]["path"]
        self.assertIn("late-result", path.parts)
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "exact original"):
            self.run_action("fanout-merge")
        self.assertFalse((self.directory / "MERGED.json").exists())

    def test_pending_original_submit_blocks_new_polls_and_capacity_despite_later_observation(self):
        saved = []

        def pending(directory, script, http_timeout, wait):
            saved.append(client.read(directory / "INTENT.json"))
            client.write_new(directory / "QUEUE.json", {"job_id": 403})
            return {"returncode": 124, "queue_id": 403, "rows": []}

        with patch.object(client, "invoke", side_effect=pending) as transport, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(client.run(self.args()), 124)
            for _ in range(3):
                self.assertEqual(client.run(self.args("fanout-poll", max_polls=1)), 124)
                self.assertEqual(client.run(self.args()), 124)
            self.assertEqual(transport.call_count, 1)
        member = self.intent()["members"][0]
        self.poll_status = "complete"
        # 旧客户端/手动查询产生终态也不允许跳过尚未结清的原提交队列。
        with patch.object(client, "invoke", side_effect=self.invoke):
            fanout.send(self.directory, self.state, self.binding, member, "fo-poll-legacy", {"check_job": member["job_id"]}, self.args())
        self.assertEqual(self.run_action(), 124)
        self.assertEqual(self.run_action("fanout-poll", max_polls=1), 124)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.statuses(), ["UNKNOWN", "PENDING", "PENDING"])
        submit = saved[0]
        row = {"status": "HTTP_RESPONSE", "request_id": submit["request_id"],
               "payload_sha256": fanout.payload_sha(submit["payload"]), "transport": fanout.TRANSPORT,
               "native_mcp_verified": False, "response": {"success": True, "job_id": member["job_id"]}}
        results = self.fake_core.STATE / "results"
        results.mkdir(parents=True)
        client.write_new(results / "403.json", {"rc": 0, "out": base64.b64encode(("XDEV_JSON " + json.dumps(row)).encode()).decode(), "err": ""})
        self.assertEqual(self.run_action("fanout-poll", max_polls=1), 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.statuses(), ["SUCCEEDED", "PENDING", "PENDING"])

    def test_full_progress_retains_elapsed_and_observed_interval_without_extra_polls(self):
        self.contract["members"] = self.contract["members"][:1]
        self.write_contract()
        self.run_action()
        self.custom = lambda call, row: {**row, "response": {**row["response"], "elapsed_seconds": 17.25,
                         "progress": {"stage": "拟合模型" * 100, "message": "仍在原任务中" * 1000,
                                      "updated_at": "2026-11-15T08:00:00Z", "detail": "full local metadata"}}}
        before = len(self.calls)
        self.run_action("fanout-poll", max_polls=1)
        first_summary = client.read(self.directory / "SUMMARY.json")
        self.assertIsNone(first_summary["members"][0]["observed_poll_interval_seconds"])
        self.run_action("fanout-poll", max_polls=1)
        self.assertEqual(len(self.calls) - before, 2)
        summary = client.read(self.directory / "SUMMARY.json")
        full = client.read(summary["full_progress_report"]["path"])["members"][0]
        self.assertEqual(full["elapsed_seconds"], 17.25)
        self.assertIsInstance(full["observed_poll_interval_seconds"], float)
        self.assertGreaterEqual(full["observed_poll_interval_seconds"], 0)
        self.assertEqual(full["progress"]["stage"], "拟合模型" * 100)
        self.assertEqual(full["progress"]["message"], "仍在原任务中" * 1000)
        self.assertEqual(full["progress"]["detail"], "full local metadata")
        self.assertTrue(summary["members"][0]["progress_truncated"])
        self.assertLessEqual(len(json.dumps(summary, ensure_ascii=False).encode("utf-8")) + 12, 65536)

    def test_poll_interval_ignores_forward_and_backward_wall_clock_adjustments(self):
        self.contract["members"] = self.contract["members"][:1]
        self.write_contract()
        self.run_action()
        clocks = [10_000_000_000, 10_750_000_000, 12_000_000_000]
        walls = ["2026-11-15T00:00:00+00:00", "2026-11-16T00:00:00+00:00", "2026-11-14T00:00:00+00:00"]
        expected = [None, 0.75, 1.25]
        before = len(self.calls)
        with patch.object(fanout.time, "monotonic_ns", side_effect=clocks):
            for wall, interval in zip(walls, expected):
                with patch.object(fanout, "now", return_value=wall):
                    self.assertEqual(self.run_action("fanout-poll", max_polls=1), 0)
                summary = client.read(self.directory / "SUMMARY.json")
                full = client.read(summary["full_progress_report"]["path"])["members"][0]
                self.assertEqual(summary["members"][0]["observed_poll_interval_seconds"], interval)
                self.assertEqual(full["observed_poll_interval_seconds"], interval)
        self.assertEqual(len(self.calls) - before, 3)
        member = self.intent()["members"][0]
        original_polls = [record for record in fanout.member_events(self.directory, member) if "poll_clock" in record]
        self.assertEqual([record["at_utc"] for record in original_polls], walls)
        for record in original_polls:
            intent = client.read(self.state / "requests" / record["request_id"] / "INTENT.json")
            self.assertEqual(intent["poll_clock"], record["poll_clock"])

    def test_different_clock_domains_and_legacy_poll_records_report_null(self):
        self.contract["members"] = self.contract["members"][:1]
        self.write_contract()
        self.run_action()
        for domain, tick in (("process-a", 1_000_000_000), ("process-b", 2_000_000_000)):
            clock = {"kind": "PROCESS_MONOTONIC_NS", "domain": domain, "value_ns": tick}
            with patch.object(fanout, "poll_clock", return_value=clock):
                self.run_action("fanout-poll", max_polls=1)
        summary = client.read(self.directory / "SUMMARY.json")
        full = client.read(summary["full_progress_report"]["path"])["members"][0]
        self.assertIsNone(full["observed_poll_interval_seconds"])
        self.assertEqual(full["poll_interval_source"], "UNKNOWN")
        self.assertIsNone(full["poll_interval_clock_domain"])
        member = self.intent()["members"][0]
        fanout.event(self.directory, member, "RUNNING", request_id="legacy-poll", payload={"check_job": member["job_id"]})
        legacy = fanout.full_progress(self.directory, member, fanout.member_status(self.directory, member))
        self.assertIsNone(legacy["observed_poll_interval_seconds"])
        self.assertEqual(legacy["poll_interval_source"], "UNKNOWN")
        self.assertEqual(len(self.calls), 3)

    def test_monotonic_domain_refreshes_after_process_change(self):
        process, session = fanout._CLOCK_PROCESS, fanout._CLOCK_SESSION
        try:
            first = fanout.poll_clock()
            with patch.object(fanout.os, "getpid", return_value=process + 1):
                second = fanout.poll_clock()
            self.assertNotEqual(first["domain"], second["domain"])
            self.assertIsNone(fanout.poll_interval(first, second))
        finally:
            fanout._CLOCK_PROCESS, fanout._CLOCK_SESSION = process, session

    def test_invalid_or_regressing_monotonic_clock_never_uses_wall_time(self):
        valid = {"kind": "PROCESS_MONOTONIC_NS", "domain": "same-process", "value_ns": 2_000_000_000}
        for invalid in (None, {}, {**valid, "value_ns": 1_000_000_000},
                        {**valid, "value_ns": True}, {**valid, "value_ns": -1},
                        {**valid, "value_ns": "3000000000"}, {**valid, "kind": "WALL_CLOCK"}):
            with self.subTest(invalid=invalid):
                self.assertIsNone(fanout.poll_interval(valid, invalid))

    def test_locally_complete_progress_does_not_claim_remote_bounded_receipt_is_complete(self):
        self.contract["members"] = self.contract["members"][:1]
        self.write_contract()
        self.run_action()
        reference = {"path": "C:\\offline\\original.json", "sha256": "a" * 64, "location": "WINDOWS_ONLY"}
        self.custom = lambda call, row: {**row, "receipt_bounded": True, "full_receipt": reference,
                         "response": {**row["response"], "progress": {"stage": "running", "message": "bounded remote text"}}}
        self.run_action("fanout-poll", max_polls=1)
        summary = client.read(self.directory / "SUMMARY.json")
        self.assertTrue(summary["full_progress_report"]["complete_local_record"])
        self.assertFalse(summary["full_progress_report"]["remote_progress_complete"])
        local = client.read(summary["full_progress_report"]["path"])["members"][0]
        self.assertTrue(local["source_receipt_bounded"])
        self.assertEqual(local["source_full_receipt"], reference)

    def test_unknown_run_and_invalid_poll_budget_do_not_contact_transport(self):
        with self.assertRaisesRegex(ValueError, "Unknown run"):
            self.run_action("fanout-poll")
        with self.assertRaisesRegex(ValueError, "max-polls"):
            self.run_action(max_polls=0)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
