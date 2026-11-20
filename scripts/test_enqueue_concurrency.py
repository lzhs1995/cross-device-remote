#!/usr/bin/env python3
"""enqueue 同进程双线程回归；仅临时合成队列，禁网络和子进程。"""
import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
import contextlib
import datetime
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
import uuid

SOURCE = None
DENIED = []


def audit(event, args):
    if event.startswith("socket.") or event in ("subprocess.Popen", "os.system"):
        DENIED.append(event)
        raise RuntimeError("Offline enqueue tests cannot start processes or network")


sys.addaudithook(audit)


def function(file, name, **env):
    # 只编译待测函数；不导入 wbagent，不读取真实配置，也不启动服务器。
    if file != "wbagent.py" or name != "enqueue":
        raise ValueError("Only the enqueue function is in this regression scope")
    path = SOURCE / file
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    if len(nodes) != 1:
        raise ValueError("Exactly one enqueue function is required")
    scope = {"Path": Path, "json": json, "uuid": uuid, **env}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), scope)
    return scope[name]


def concurrent_two(a, b):
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(a), pool.submit(b)]
        return [j.result(timeout=8) for j in jobs]


class EnqueueConcurrencyTests(unittest.TestCase):
    def test_enqueue_same_process_threads_preserve_payload_and_signature(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/"queue").mkdir()
            (root/"results").mkdir()
            old = b'{"id":7,"original":"unchanged"}'
            (root/"queue/7.json").write_bytes(old)
            (root/"results/7.json").write_bytes(old)
            ready = threading.Barrier(2)
            first_done = threading.Event()
            seen = threading.local()
            def link(a, b):
                if not getattr(seen, "entered", False):
                    seen.entered = True
                    ready.wait(timeout=5)
                    # 两者都已写暂存；第一条取号/清理完，第二条才尝试原编号。
                    if seen.payload == "second":
                        self.assertTrue(first_done.wait(5))
                os.link(a, b)
            def sign(*parts):
                return hmac.new(b"synthetic-test-only", "|".join(map(str,parts)).encode(), hashlib.sha256).hexdigest()
            enqueue = function("wbagent.py", "enqueue", ROOT=root, sign=sign,
                               os=types.SimpleNamespace(getpid=os.getpid, link=link))
            def invoke(text):
                seen.payload = text
                try:
                    return {"id": enqueue(text, 17), "expected": text}
                except Exception as error:
                    return {"error": type(error).__name__ + ": " + str(error)}
                finally:
                    if text == "first":
                        first_done.set()
            results = concurrent_two(lambda: invoke("first"), lambda: invoke("second"))
            self.assertFalse([r for r in results if "error" in r], results)
            self.assertEqual({r["id"] for r in results}, {8,9})
            for r in results:
                job = json.loads((root/"queue"/(str(r["id"])+".json")).read_text())
                self.assertEqual(job["script"], r["expected"])
                self.assertEqual(job["timeout"], 17)
                self.assertEqual(job["mac"], sign("cmd", job["id"], 17, r["expected"]))
            self.assertEqual((root/"queue/7.json").read_bytes(), old)
            self.assertEqual((root/"results/7.json").read_bytes(), old)
            self.assertFalse(list((root/"queue").glob(".*.tmp")))


TEST_NAME = "test_enqueue_same_process_threads_preserve_payload_and_signature"


def run(source):
    global SOURCE
    SOURCE = source
    stream = io.StringIO()
    suite = unittest.TestSuite([EnqueueConcurrencyTests(TEST_NAME)])
    with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return {"tests": result.testsRun, "failures": len(result.failures),
            "errors": len(result.errors),
            "failed_names": [test._testMethodName for test, _ in result.failures],
            "log": stream.getvalue()}


def source_pin(directory):
    path = directory / "wbagent.py"
    raw = path.read_bytes()
    return {"path": str(path), "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent,
                        help="directory containing the candidate wbagent.py")
    parser.add_argument("--baseline", type=Path,
                        help="optional historical source directory; this regression must fail there")
    parser.add_argument("--output", type=Path,
                        help="optional new JSON evidence file; existing files are never replaced")
    args = parser.parse_args()
    source = args.source.resolve()
    baseline_dir = args.baseline.resolve() if args.baseline else None
    before = {"candidate": source_pin(source)}
    if baseline_dir:
        before["baseline"] = source_pin(baseline_dir)
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    baseline = run(baseline_dir) if baseline_dir else None
    candidate = run(source)
    after = {"candidate": source_pin(source)}
    if baseline_dir:
        after["baseline"] = source_pin(baseline_dir)
    baseline_ok = baseline is None or (baseline["tests"] == 1 and baseline["failures"] == 1
                    and baseline["errors"] == 0 and baseline["failed_names"] == [TEST_NAME])
    passed = (candidate["tests"] == 1 and candidate["failures"] == candidate["errors"] == 0
              and baseline_ok and not DENIED and before == after)
    result = {"schema": "enqueue-thread-regression/1", "status": "PASS" if passed else "FAIL",
              "started_at_utc": started,
              "finished_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "baseline": baseline, "candidate": candidate,
              "source_before": before, "source_after": after,
              "source_unchanged_during_test": before == after,
              "test_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "denied_network_or_process_attempts": list(DENIED),
              "real_queue_submissions": 0, "real_windows_calls": 0}
    if args.output:
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    if baseline:
        print("BASELINE_EXPECTED_FAILURE")
        print(baseline["log"], end="")
    print("CANDIDATE")
    print(candidate["log"], end="")
    print(json.dumps({key: result[key] for key in
        ("status", "test_sha256", "source_unchanged_during_test", "denied_network_or_process_attempts",
         "real_queue_submissions", "real_windows_calls")}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
