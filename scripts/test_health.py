"""离线核健康判断：历史缺回执不冒充在线，检查不改变原队列。"""
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


class HealthTests(unittest.TestCase):
    NOW = 1800000000.0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "queue").mkdir()
        (self.root / "results").mkdir()
        core = types.ModuleType("xdev_core")
        core.CFG = {"port": 18092}
        core.HERE = Path(__file__).resolve().parent
        core.STATE = self.root
        spec = importlib.util.spec_from_file_location("health_under_test", core.HERE / "xdev_health.py")
        self.client = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"xdev_core": core}):
            spec.loader.exec_module(self.client)

    def state(self, age=5, cursor=11, jobs=(10, 12, 13), dispatched=(10, 12), completed=()):
        (self.root / "last_seen").write_text(f"{self.NOW - age} after={cursor} peer=192.0.2.30\n", encoding="utf-8")
        for job in jobs:
            (self.root / "queue" / f"{job}.json").write_text('{}\n', encoding="utf-8")
        for job in completed:
            (self.root / "results" / f"{job}.json").write_text('{"rc":0}\n', encoding="utf-8")
        (self.root / "server.log").write_text("".join(f"2000-01-01T00:00:00Z DISPATCH id={job}\n" for job in dispatched), encoding="utf-8")

    def call(self, pids=("123",)):
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        output = io.StringIO()
        with patch.object(self.client, "_server_pids", return_value=list(pids)), \
                patch.object(self.client.time, "time", return_value=self.NOW), \
                contextlib.redirect_stdout(output):
            rc = self.client.health()
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(after, before, "health must preserve original queue, receipts, and cursor")
        return rc, output.getvalue()

    def test_stale_historical_dispatch_cannot_report_online(self):
        self.state(age=7200, jobs=(10,), dispatched=(10,))
        rc, out = self.call()
        self.assertEqual(rc, 3)
        self.assertIn("historical_unknown [10]", out)
        self.assertNotIn("XDEV_HEALTH OK", out)

    def test_stale_current_dispatch_is_unconfirmed_not_online(self):
        self.state(age=120)
        rc, out = self.call()
        self.assertEqual(rc, 3)
        self.assertIn("dispatched_unconfirmed [12]", out)
        self.assertIn("do not resubmit", out)

    def test_fresh_poll_classifies_history_current_and_waiting(self):
        self.state()
        rc, out = self.call()
        self.assertEqual(rc, 0)
        for marker in ("historical_unknown [10]", "dispatched_unconfirmed [12]", "waiting [13]"):
            self.assertIn(marker, out)
        self.assertNotIn("agent busy with a job", out)
        self.assertIn("does not prove background jobs are idle", out)

    def test_completed_receipt_is_excluded_from_unknown(self):
        self.state(completed=(10, 12))
        rc, out = self.call()
        self.assertEqual(rc, 0)
        self.assertIn("dispatched_unconfirmed -, waiting [13], historical_unknown -", out)

    def test_missing_or_partial_timestamp_fails_closed(self):
        self.state()
        for value in (None, "", "invalid after=11", "nan after=11", "inf after=11"):
            with self.subTest(value=value):
                seen = self.root / "last_seen"
                if value is None:
                    seen.unlink(missing_ok=True)
                else:
                    seen.write_text(value, encoding="utf-8")
                rc, out = self.call()
                self.assertEqual(rc, 3)
                self.assertIn("AGENT_STALE", out)

    def test_future_timestamp_is_not_fresh(self):
        self.state(age=-60)
        self.assertEqual(self.call()[0], 3)

    def test_legacy_poll_without_cursor_does_not_guess_history(self):
        self.state()
        (self.root / "last_seen").write_text(f"{self.NOW} peer=192.0.2.30\n", encoding="utf-8")
        rc, out = self.call()
        self.assertEqual(rc, 0)
        self.assertIn("historical_unknown -", out)
        self.assertIn("dispatched_unconfirmed [10, 12]", out)

    def test_server_down_takes_precedence_over_fresh_poll(self):
        self.state()
        rc, out = self.call(pids=())
        self.assertEqual(rc, 2)
        self.assertIn("SERVER_DOWN", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
