"""检查真实请求边界：重复提交、未知结果、终态复用和完整 UTF-8 载荷。"""

import argparse
import base64
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import xdev_clauder as client


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.binding = {"session_name": "study", "port": 8787, "r_pid": 101,
                        "rstudio_pid": 100, "r_birth_utc": "2026-11-02T00:00:00Z", "binding_id": "testbinding"}
        client.write_new(self.state / "BINDING.json", self.binding)
        self.code = self.root / "worker.R"
        self.code.write_text('cat("中文 \' $HOME `literal`", "\\n")\n', encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def args(self, action="submit", **extra):
        value = dict(action=action, state=str(self.state), http_timeout=25, wait=55,
                     request_id="request1", job_id="job1", code_file=str(self.code), agent_id="test")
        value.update(extra)
        return argparse.Namespace(**value)

    def result(self, status="submitted", **extra):
        return {"returncode": 0, "rows": [{"status": "HTTP_RESPONSE",
                "response": {"success": True, "status": status, **extra}}]}

    def test_same_async_job_cannot_be_submitted_under_a_new_request_id(self):
        with patch.object(client, "invoke", return_value=self.result()) as transport:
            self.assertEqual(client.run(self.args()), 0)
            with self.assertRaises(FileExistsError):
                client.run(self.args(request_id="request2"))
            self.assertEqual(transport.call_count, 1)

    def test_unknown_http_outcome_keeps_permanent_submit_claim(self):
        result = {"returncode": 0, "rows": [{"status": "UNKNOWN_HTTP_OUTCOME"}]}
        with patch.object(client, "invoke", return_value=result) as transport:
            self.assertEqual(client.run(self.args()), 124)
            with self.assertRaises(FileExistsError):
                client.run(self.args(request_id="replay"))
            self.assertEqual(transport.call_count, 1)

    def test_complete_poll_is_cached_before_next_poll(self):
        with patch.object(client, "invoke", return_value=self.result()):
            client.run(self.args())
        with patch.object(client, "invoke", return_value=self.result("complete", output="done")) as transport:
            self.assertEqual(client.run(self.args("poll", request_id="poll1")), 0)
            self.assertEqual(client.run(self.args("poll", request_id="poll2")), 0)
            self.assertEqual(transport.call_count, 1)

    def test_failed_async_is_not_success_or_resubmitted(self):
        result = {"returncode": 0, "rows": [{"status": "HTTP_RESPONSE",
                  "response": {"status": "complete", "success": False, "error": "model failed"}}]}
        with patch.object(client, "invoke", return_value=self.result()):
            client.run(self.args())
        with patch.object(client, "invoke", return_value=result) as transport:
            self.assertEqual(client.run(self.args("poll", request_id="poll1")), 3)
            self.assertEqual(client.run(self.args("poll", request_id="poll2")), 3)
            self.assertEqual(transport.call_count, 1)

    def test_sync_request_id_is_exclusive(self):
        with patch.object(client, "invoke", return_value=self.result()) as transport:
            self.assertEqual(client.run(self.args("execute")), 0)
            self.code.write_text('stop("different code")', encoding="utf-8")
            with self.assertRaises(FileExistsError):
                client.run(self.args("execute"))
            self.assertEqual(transport.call_count, 1)

    def test_arbitrary_r_text_is_preserved_without_shell_interpolation(self):
        payload = {"code": self.code.read_text(encoding="utf-8"), "async": True, "job_id": "job1"}
        script = client.request_script(self.binding, "r1", payload, 25)
        encoded = re.search(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", script)[1]
        self.assertEqual(json.loads(base64.b64decode(encoded)), payload)

    def test_unbound_poll_does_not_contact_windows(self):
        with patch.object(client, "invoke") as transport:
            with self.assertRaises(ValueError):
                client.run(self.args("poll"))
            transport.assert_not_called()

    def test_unicode_line_separators_inside_json_are_not_record_boundaries(self):
        row = {"status": "HTTP_RESPONSE", "response": {"output": "中文\u0085\u2028\u2029done"}}
        output = "diagnostic\nXDEV_JSON " + json.dumps(row, ensure_ascii=False) + "\n"
        result = client.save_response(self.root, 0, output, "", 42, 0.1)
        self.assertEqual(result["rows"], [row])
        self.assertEqual(client.read(self.root / "RAW_TRANSPORT.json")["stdout"], output)

    def test_malformed_json_preserves_raw_original_reply(self):
        output = 'XDEV_JSON {"incomplete":\n'
        with self.assertRaises(json.JSONDecodeError):
            client.save_response(self.root, 0, output, "diagnostic", 42, 0.1)
        raw = client.read(self.root / "RAW_TRANSPORT.json")
        self.assertEqual(raw["stdout"], output)
        self.assertEqual(raw["queue_id"], 42)
        self.assertFalse((self.root / "RESPONSE.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
