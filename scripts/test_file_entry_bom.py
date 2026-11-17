#!/usr/bin/env python3
"""实际 CLI 文件读取与载荷包装回归；仅合成文件和内存传输。"""
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
import time
import types
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parent
BOM = b"\xef\xbb\xbf"
DENIED = []


def audit(event, args):
    if event.startswith("socket.") or event in ("subprocess.Popen", "os.system"):
        DENIED.append(event)
        raise RuntimeError("Offline file-entry tests cannot use network or subprocesses")


sys.addaudithook(audit)


def definitions(file, names, namespace):
    # 只提取当前源码的函数/前缀，不导入真实配置、secret或队列。
    tree = ast.parse(file.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if
             (isinstance(node, ast.FunctionDef) and node.name in names) or
             (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
              and node.targets[0].id in names)]
    if len(nodes) != len(names):
        raise AssertionError((file.name, names))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(file), "exec"), namespace)


class OfflineCLI:
    def __init__(self):
        self.bodies = []
        self.payloads = []
        self.enqueue_calls = []
        core = types.ModuleType("xdev_core")
        core.__dict__.update(base64=base64, sys=sys, time=time,
                             STATE=Path("SYNTHETIC_ONLY_NO_READ"),
                             WIN_ROOT=r"%LOCALAPPDATA%\xdev",
                             AGENT_STATE=r"%LOCALAPPDATA%\StataWorkbenchAgent")

        def enqueue(encoded, timeout):
            self.enqueue_calls.append((encoded, timeout))
            self.payloads.append(base64.b64decode(encoded))
            return 999001

        core.wbagent = types.SimpleNamespace(enqueue=enqueue, read_result=lambda job: {
            "rc": 0, "out": base64.b64encode(b"OFFLINE\n").decode(), "err": ""})
        definitions(SOURCE / "xdev_core.py", {"ps_quote", "PREAMBLE", "run_ps"}, core.__dict__)
        self.core = core
        tasks = types.ModuleType("xdev_tasks")
        tasks.__dict__.update(Path=Path, ps_quote=core.ps_quote,
                              _win_root=lambda: core.WIN_ROOT,
                              fail=lambda message, rc=1: rc,
                              print_summary=lambda result: result["rc"])
        definitions(SOURCE / "xdev_tasks.py",
                    {"add_parser", "run", "_body_script", "_identity_script"}, tasks.__dict__)

        def detach(name, body):
            self.bodies.append(body)
            self.payloads.append(tasks._body_script(body).encode("utf-8"))
            return 0

        def submit(inputs, body, name):
            self.bodies.append(body)
            self.payloads.append(tasks._body_script(body, r"C:\SYNTHETIC_ONLY").encode("utf-8"))
            return {"rc": 0}

        tasks.detach = detach
        tasks.task_submit = submit
        clauder = types.ModuleType("xdev_clauder")
        clauder.add_parser = lambda sub: None
        self.modules = {"xdev_core": core, "xdev_tasks": tasks, "xdev_clauder": clauder}
        entry = {"argparse": argparse, "sys": sys, "Path": Path}
        definitions(SOURCE / "xdev.py", {"main"}, entry)
        self.main = entry["main"]

    def invoke(self, route, file=None, command=None):
        argv = [route]
        if route == "detach":
            argv.append("SYNTHETIC_ONLY")
        if route == "task-submit":
            argv += ["--name", "SYNTHETIC_ONLY"]
        argv += [str(file)] if file is not None else ["-c", command]
        with mock.patch.dict(sys.modules, self.modules), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = self.main(argv)
        if rc != 0:
            raise AssertionError((route, rc))
        if route == "run":
            self.bodies.append(self.payloads[-1].decode("utf-8")[len(self.core.PREAMBLE):])
        return self.bodies[-1], self.payloads[-1]


class FileEntryTests(unittest.TestCase):
    def files(self, raw, expected):
        for route in ("run", "detach", "task-submit"):
            with self.subTest(route=route), tempfile.TemporaryDirectory(prefix="xdev-bom-offline-") as folder:
                file = Path(folder) / "body.ps1"
                file.write_bytes(raw)
                cli = OfflineCLI()
                body, payload = cli.invoke(route, file=file)
                self.assertEqual(body, expected)
                self.assertIn(expected.encode("utf-8"), payload)
                if route == "run":
                    self.assertEqual(payload, (cli.core.PREAMBLE + expected).encode("utf-8"))
                self.assertEqual(file.read_bytes(), raw)
                self.assertEqual(len(cli.payloads), 1)
                self.assertEqual(len(cli.enqueue_calls), 1 if route == "run" else 0)

    def test_bom_file_consumed_before_wrapping(self):
        body = "$x = '中文'; Write-Output $x\n"
        self.files(BOM + body.encode("utf-8"), body)

    def test_non_bom_unicode_and_existing_newline_behavior_preserved(self):
        self.files("$x='中文'\r\nWrite-Output $x\r\n".encode("utf-8"), "$x='中文'\nWrite-Output $x\n")

    def test_interior_bom_is_content_not_removed(self):
        body = "$x='\ufeffliteral'; Write-Output $x\n"
        self.files(body.encode("utf-8"), body)

    def test_only_one_leading_bom_consumed(self):
        self.files(BOM + BOM + b"Write-Output 1\n", "\ufeffWrite-Output 1\n")

    def test_nonleading_bom_after_whitespace_preserved(self):
        self.files(b" " + BOM + b"Write-Output 1\n", " \ufeffWrite-Output 1\n")

    def test_inline_command_bom_and_crlf_preserved(self):
        for route in ("run", "detach", "task-submit"):
            with self.subTest(route=route):
                text = "\ufeff$x='\ufeff';\r\nWrite-Output $x"
                cli = OfflineCLI()
                body, payload = cli.invoke(route, command=text)
                self.assertEqual(body, text)
                self.assertIn(text.encode("utf-8"), payload)
                self.assertEqual(len(cli.payloads), 1)

    def rejected_before_submission(self, raw):
        for route in ("run", "detach", "task-submit"):
            with self.subTest(route=route, raw_hex=raw.hex()), \
                    tempfile.TemporaryDirectory(prefix="xdev-bom-invalid-") as folder:
                file = Path(folder) / "body.ps1"
                file.write_bytes(raw)
                cli = OfflineCLI()
                with self.assertRaises(UnicodeDecodeError):
                    cli.invoke(route, file=file)
                self.assertEqual(cli.bodies, [])
                self.assertEqual(cli.payloads, [])
                self.assertEqual(cli.enqueue_calls, [])
                self.assertEqual(file.read_bytes(), raw)

    def test_invalid_utf8_rejected_before_fake_submission(self):
        self.rejected_before_submission(BOM + b"\xff")

    def test_truncated_bom_one_byte_rejected_before_transport(self):
        self.rejected_before_submission(b"\xef")

    def test_truncated_bom_two_bytes_rejected_before_transport(self):
        self.rejected_before_submission(b"\xef\xbb")

    def test_synthetic_payload_only_initial_bom_removed(self):
        # 完全合成载荷，保留正文BOM；不引用真实任务或交接目录。
        body = "$value='合成\ufeff正文'; Write-Output $value\n"
        raw = BOM + body.encode("utf-8")
        with tempfile.TemporaryDirectory(prefix="xdev-bom-payload-") as folder:
            source = Path(folder) / "body.ps1"
            source.write_bytes(raw)
            cli = OfflineCLI()
            original_payload = cli.core.PREAMBLE.encode("utf-8") + raw
            decoded, payload = cli.invoke("run", file=source)
            self.assertEqual(decoded, body)
            self.assertEqual(payload, cli.core.PREAMBLE.encode("utf-8") + raw[len(BOM):])
            self.assertEqual(original_payload.replace(BOM, b"", 1), payload)
            self.assertEqual(payload.count(BOM), 1)
            self.assertEqual(source.read_bytes(), raw)
            self.assertEqual(len(cli.enqueue_calls), 1)


def main():
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(FileEntryTests))
    passed = result.wasSuccessful() and not DENIED
    print(json.dumps({"tests_run": result.testsRun, "failure_records": len(result.failures),
                      "error_records": len(result.errors), "passed": passed,
                      "output": output.getvalue(),
                      "scope": "actual CLI/file reads/payload functions; synthetic fixtures and stub transport",
                      "source_pins": {name: hashlib.sha256((SOURCE / name).read_bytes()).hexdigest()
                                      for name in ("xdev.py", "xdev_tasks.py", "xdev_core.py")},
                      "denied_network_or_process_attempts": DENIED,
                      "real_queue_writes": 0, "windows_execution": False}, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
