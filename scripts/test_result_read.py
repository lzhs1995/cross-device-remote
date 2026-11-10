"""重现回执存在但尚未写完的窗口；只用临时合成回执，不加载真实配置或入队。"""
import argparse
import ast
import base64
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest

SOURCE = None


class ReaderTests(unittest.TestCase):
    def test_json_string_line_separators_preserved_in_emitted_rows(self):
        tree = ast.parse(SOURCE.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "emitted")
        environment = {"json": json}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), "exec"), environment)
        value = {"text": "中文\u0085\u2028\u2029done"}
        wire = "diagnostic\nXDEV_JSON " + json.dumps(value, ensure_ascii=False) + "\r\n"
        self.assertEqual(environment["emitted"](wire), [value])

    def invoke(self, initial, updates, wait=2):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "results").mkdir()
            path = root / "results/42.json"
            state = {"clock": 0.0, "enqueues": 0, "sleeps": 0}

            def enqueue(*_):
                state["enqueues"] += 1
                if initial is not None:
                    path.write_bytes(initial)
                return 42

            def sleep(seconds):
                state["clock"] += seconds
                state["sleeps"] += 1
                if updates:
                    value = updates.pop(0)
                    if value is not None:
                        path.write_bytes(value)

            environment = {"STATE": root, "PREAMBLE": "SYNTHETIC\n", "base64": base64,
                           "json": json, "sys": sys,
                           "time": types.SimpleNamespace(time=lambda: state["clock"],
                                                         monotonic=lambda: state["clock"], sleep=sleep),
                           "wbagent": types.SimpleNamespace(enqueue=enqueue)}
            tree = ast.parse(SOURCE.read_text())
            function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run_ps")
            exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), "exec"), environment)
            capture = io.StringIO()
            with contextlib.redirect_stderr(capture):
                result = environment["run_ps"]("SYNTHETIC", timeout=1, wait=wait)
            return result, state, capture.getvalue(), path.read_bytes() if path.exists() else None

    @staticmethod
    def valid():
        return json.dumps({"id": 42, "rc": 7, "out": base64.b64encode("中文\r\n".encode()).decode(),
                           "err": base64.b64encode(b"diagnostic\r\n").decode()}).encode()

    def test_completed_receipt_and_negative_exit_passthrough(self):
        result, state, _, _ = self.invoke(self.valid(), [], wait=0)
        self.assertEqual(result, (7, "中文\n", "diagnostic\n", 42))
        self.assertEqual(state["enqueues"], 1)
        self.assertEqual(state["sleeps"], 0)

    def test_empty_created_receipt_waits_for_same_completion(self):
        result, state, _, _ = self.invoke(b"", [self.valid()])
        self.assertEqual(result[0], 7)
        self.assertEqual(state["enqueues"], 1)

    def test_truncated_receipt_waits_through_multiple_writes(self):
        result, state, _, _ = self.invoke(b'{"id":', [b'{"id":42,"out":', self.valid()])
        self.assertEqual(result[0], 7)
        self.assertEqual(state["enqueues"], 1)

    def test_incomplete_utf8_does_not_lose_original_job(self):
        result, state, _, _ = self.invoke(b'{"message":"\xe4\xb8', [self.valid()])
        self.assertEqual(result[-1], 42)
        self.assertEqual(state["enqueues"], 1)

    def test_permanently_partial_file_preserves_original_and_budget(self):
        broken = b'{"id":'
        result, state, log, final = self.invoke(broken, [], wait=1.2)
        self.assertEqual(result, (124, "", "", 42))
        self.assertAlmostEqual(state["clock"], 1.2)
        self.assertEqual(state["enqueues"], 1)
        self.assertIn("id=42", log)
        self.assertEqual(final, broken)

    def test_missing_receipt_never_creates_second_job(self):
        result, state, log, final = self.invoke(None, [], wait=1)
        self.assertEqual(result[0], 124)
        self.assertEqual(state["enqueues"], 1)
        self.assertIn("id=42", log)
        self.assertIsNone(final)

    def test_original_id_is_observable_before_result(self):
        _, _, log, _ = self.invoke(b"", [self.valid()])
        self.assertIn("XDEV_QUEUED id=42", log)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    SOURCE = args.source.resolve()
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ReaderTests))
    report = {"status": "PASS" if result.wasSuccessful() else "FAIL", "tests": result.testsRun,
              "errors": len(result.errors), "failures": len(result.failures),
              "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
              "real_queue_submissions": 0, "real_mplus_calls": 0, "log": output.getvalue()}
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({key: value for key, value in report.items() if key != "log"}))
    raise SystemExit(0 if result.wasSuccessful() else 2)
