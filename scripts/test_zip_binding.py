"""精确ZIP绑定的36项离线回归；只mock传输，不发真实任务或写仓内证据。"""
import base64
import contextlib
import hashlib
import importlib
import io
import json
import os
from pathlib import Path, PureWindowsPath
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


def blocked(*args, **kwargs):
    raise AssertionError('REAL_REMOTE_OR_PROCESS_OPERATION_BLOCKED')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def emit(value):
    return 'XDEV_JSON ' + json.dumps(value) + '\n'


def archive_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    return buffer.getvalue()


GOOD_ENTRIES = {'model.out': b'local fixture output\n', 'nested/evidence.txt': b'verified\n'}
GOOD_ZIP = archive_bytes(GOOD_ENTRIES)
FIXED_CASES = (
    'download_timeout_rc124_retains_claim_and_remote_zip',
    'download_timeouterror_retains_claim_and_remote_zip',
    'download_sha_mismatch_does_not_cleanup', 'unpack_invalid_zip_does_not_cleanup',
    'archive_count_mismatch_does_not_cleanup',
    'verified_exact_owned_zip_cleans_up_after_members_evidence',
    'foreign_zip_receipt_must_not_authorize_cleanup', 'wrong_basename_receipt',
    'same_basename_different_parent_receipt', 'relative_receipt', 'drive_relative_receipt',
    'root_relative_receipt', 'unc_receipt', 'extended_drive_receipt', 'device_namespace_receipt',
    'parent_traversal_receipt', 'ads_receipt', 'trailing_dot_receipt',
    'mismatched_expanded_root_receipt', 'missing_binding_receipt', 'wrong_transfer_id_receipt',
    'wrong_requested_source_receipt', 'wrong_requested_root_receipt',
    'download_size_mismatch_does_not_cleanup', 'verified_case_separator_equivalence',
    'verified_dot_separator_equivalence',
)
ENV_CASES = (
    'verified_environment_root', 'same_basename_different_parent_receipt',
    'foreign_zip_receipt_must_not_authorize_cleanup', 'relative_receipt', 'unc_receipt',
    'wrong_requested_root_receipt', 'wrong_transfer_id_receipt', 'unexpanded_root_receipt',
    'unc_expanded_root_receipt', 'verified_case_separator_equivalence',
)


class Transport:

    def __init__(self, name, state, destination, files, core, environment_root):
        self.files, self.core, self.environment_root = files, core, environment_root
        self.name, self.state, self.destination = (name, state, destination)
        self.calls = []
        self.cleanup_calls = []
        self.payload = b'not a zip' if name.startswith('unpack_invalid') else GOOD_ZIP
        self.expected_members = GOOD_ENTRIES
        self.receipt_path = None
        self.tx_id = None
        self.verified_before_cleanup = False

    def __call__(self, script, **kwargs):
        files, core = self.files, self.core
        job = 1000 + len(self.calls)
        if '[IO.Compression.ZipFile]::CreateFromDirectory($D, $Z)' in script:
            assert not self.calls, 'Unexpected repeated ZIP creation'
            match = re.search('outbox\\\\pull-([0-9a-f]{32})\\.zip', script)
            assert match, 'Only original exact ZIP-generation script accepted'
            self.tx_id = match.group(1)
            resolved_root = 'C:\\Users\\fixture\\AppData\\Local\\xdev' if self.environment_root else 'C:\\xdev-fixture'
            self.expected_remote = str(PureWindowsPath(resolved_root) / 'outbox' / f'pull-{self.tx_id}.zip')
            self.receipt_path = 'C:\\xdev-fixture\\outbox\\pull-ffffffffffffffffffffffffffffffff.zip' if self.name.startswith('foreign_zip') else self.expected_remote
            variants = {'wrong_basename_receipt': str(PureWindowsPath(self.expected_remote).with_name('different.zip')), 'same_basename_different_parent_receipt': str(PureWindowsPath(resolved_root) / 'elsewhere' / PureWindowsPath(self.expected_remote).name), 'relative_receipt': f'outbox\\pull-{self.tx_id}.zip', 'drive_relative_receipt': f'C:outbox\\pull-{self.tx_id}.zip', 'root_relative_receipt': f'\\xdev-fixture\\outbox\\pull-{self.tx_id}.zip', 'unc_receipt': f'\\\\server\\share\\xdev-fixture\\outbox\\pull-{self.tx_id}.zip', 'extended_drive_receipt': '\\\\?\\' + self.expected_remote, 'device_namespace_receipt': '\\\\.\\' + self.expected_remote, 'parent_traversal_receipt': resolved_root + f'\\other\\..\\outbox\\pull-{self.tx_id}.zip', 'ads_receipt': self.expected_remote + ':stream', 'trailing_dot_receipt': self.expected_remote + '.', 'verified_case_separator_equivalence': self.expected_remote.upper().replace('\\', '/'), 'verified_dot_separator_equivalence': resolved_root + f'\\.\\outbox\\\\pull-{self.tx_id}.zip'}
            self.receipt_path = variants.get(self.name, self.receipt_path)
            count = len(GOOD_ENTRIES) + (1 if self.name.startswith('archive_count') else 0)
            row = {'ok': True, 'zip': self.receipt_path, 'files': count}
            emit_line = script.splitlines()[-1]
            assert 'root=$XRoot;' in emit_line
            for key, value in (('requested_root', core.WIN_ROOT), ('transfer_id', self.tx_id), ('requested_source', 'C:\\xdev-fixture\\work\\model')):
                assert key + '=' + files.ps_quote(value) in emit_line, emit_line
            row.update(root=resolved_root, requested_root=core.WIN_ROOT, transfer_id=self.tx_id, requested_source='C:\\xdev-fixture\\work\\model')
            if self.name == 'mismatched_expanded_root_receipt':
                row['root'] = 'C:\\foreign'
                row['zip'] = str(PureWindowsPath(row['root']) / 'outbox' / PureWindowsPath(self.expected_remote).name)
            elif self.name == 'missing_binding_receipt':
                row.pop('transfer_id')
            elif self.name == 'wrong_transfer_id_receipt':
                row['transfer_id'] = 'f' * 32
            elif self.name == 'wrong_requested_source_receipt':
                row['requested_source'] = 'C:\\different-source'
            elif self.name == 'wrong_requested_root_receipt':
                row['requested_root'] = 'C:\\foreign'
            elif self.name == 'unexpanded_root_receipt':
                row['root'] = core.WIN_ROOT
            elif self.name == 'unc_expanded_root_receipt':
                row['root'] = '\\\\server\\share\\xdev'
                row['zip'] = str(PureWindowsPath(row['root']) / 'outbox' / PureWindowsPath(self.expected_remote).name)
            self.calls.append({'stage': 'zip', 'script': script, 'options': kwargs})
            self.calls[-1]['emitted_receipt'] = row
            return (0, emit(row), '', job)
        if script.startswith('$P = Expand ') and '[Convert]::ToBase64String($buf,0,$n)' in script:
            assert len(self.calls) == 1, 'Unexpected extra or retried download'
            assert script == files._chunk_script(self.expected_remote, 0, True)
            self.calls.append({'stage': 'chunk', 'script': script, 'options': kwargs})
            if self.name.startswith('download_timeout_rc124'):
                return (124, '', 'fixture timeout waiting for original job', job)
            if self.name.startswith('download_timeouterror'):
                raise TimeoutError('fixture download transport timeout')
            digest = '0' * 64 if self.name.startswith('download_sha_mismatch') else sha(self.payload)
            size = len(self.payload) - 1 if self.name.startswith('download_size_mismatch') else len(self.payload)
            return (0, emit({'ok': True, 'size': size, 'sha256': digest, 'offset': 0, 'n': len(self.payload), 'b64': base64.b64encode(self.payload).decode()}), '', job)
        if script.startswith('Remove-Item '):
            assert len(self.calls) == 2, 'Cleanup attempted before ZIP/download or repeated'
            assert script == 'Remove-Item -LiteralPath ' + files.ps_quote(self.expected_remote) + ' -Force'
            assert kwargs == {'timeout': 60, 'quiet': True}
            members_path = self.state / 'transfers' / self.tx_id / 'MEMBERS.json'
            evidence = json.loads(members_path.read_text(encoding='utf-8'))
            expected = [{'path': str(Path(p)), 'bytes': len(v), 'sha256': sha(v)} for p, v in sorted(self.expected_members.items())]
            assert evidence == expected, 'Cleanup requires durable complete local member evidence'
            assert all(((self.destination / p).read_bytes() == v for p, v in self.expected_members.items()))
            assert (self.destination.parent / f'.xdev-pull-{self.tx_id}.zip').read_bytes() == GOOD_ZIP
            self.verified_before_cleanup = True
            record = {'stage': 'cleanup', 'script': script, 'options': kwargs, 'receipt_zip': self.receipt_path, 'generated_zip': self.expected_remote}
            self.calls.append(record)
            self.cleanup_calls.append(record)
            return (0, '', '', job)
        raise AssertionError('UNREVIEWED_REMOTE_SCRIPT: transport refuses it')

class ZipBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 先隔离配置及全局副作用，再直接导入同目录当前实现；不复制产品源。
        cls.stack = contextlib.ExitStack()
        cls.addClassCleanup(cls.stack.close)
        cls.test_root = Path(cls.stack.enter_context(tempfile.TemporaryDirectory(prefix='xdev-zip-binding-'))).resolve()
        cls.state = cls.test_root / 'state'; cls.state.mkdir()
        cls.config = cls.test_root / 'config.json'
        cls.config.write_text(json.dumps({'state_dir': str(cls.state),
            'windows': {'work_root': r'C:\xdev-fixture'}}), encoding='utf-8')
        cls.stack.enter_context(patch.dict(os.environ, XDEV_CONFIG=str(cls.config), WBAGENT_STATE=str(cls.state)))
        # 网络/子进程阻断在产品模块导入前生效。
        for owner, name in ((socket, 'create_connection'), (socket.socket, 'connect'),
                            (socket.socket, 'connect_ex'), (subprocess, 'Popen'), (os, 'system')):
            cls.stack.enter_context(patch.object(owner, name, blocked))
        cls.stack.enter_context(patch.object(sys, 'dont_write_bytecode', True))
        here = Path(__file__).resolve().parent
        old_path = list(sys.path)
        cls.stack.callback(lambda: sys.path.__setitem__(slice(None), old_path))
        sys.path.insert(0, str(here))
        # unittest被其他测试导入时也不继承它们的配置、STATE或已缓存模块。
        names = ('wbagent', 'xdev_core', 'xdev_files', 'xdev_tasks')
        saved = {n: sys.modules.pop(n) for n in names if n in sys.modules}
        def restore():
            for n in names: sys.modules.pop(n, None)
            sys.modules.update(saved)
        cls.stack.callback(restore)
        cls.core = importlib.import_module('xdev_core')
        cls.files = importlib.import_module('xdev_files')
        cls.tasks = importlib.import_module('xdev_tasks')
        for module in (cls.core, cls.files, cls.tasks, cls.core.wbagent):
            if Path(module.__file__).resolve().parent != here:
                raise AssertionError('SAME_DIRECTORY_IMPLEMENTATION_REQUIRED')
        for owner, name in ((cls.core, 'run_ps'), (cls.tasks, 'run_ps'), (cls.core.wbagent, 'enqueue')):
            cls.stack.enter_context(patch.object(owner, name, blocked))

    def check_case(self, case, environment_root):
        with tempfile.TemporaryDirectory(prefix='case-', dir=self.test_root) as directory:
            root = Path(directory)
            state = root / 'state'; (state / 'files').mkdir(parents=True)
            destination = root / 'collected'
            files, core, tasks = self.files, self.core, self.tasks
            win_root = r'%LOCALAPPDATA%\xdev' if environment_root else r'C:\xdev-fixture'
            transport = Transport(case, state, destination, files, core, environment_root)
            with contextlib.ExitStack() as stack:
                for module, name, value in ((files, 'STATE', state), (files, 'WIN_ROOT', win_root),
                                            (core, 'WIN_ROOT', win_root), (files, 'run_ps', transport)):
                    stack.enter_context(patch.object(module, name, value))
                self.assertIs(files.run_ps, transport)
                self.assertIs(core.run_ps, blocked); self.assertIs(tasks.run_ps, blocked)
                self.assertIs(core.wbagent.enqueue, blocked)
                self.assertIs(socket.create_connection, blocked); self.assertIs(socket.socket.connect, blocked)
                self.assertIs(socket.socket.connect_ex, blocked)
                self.assertIs(subprocess.Popen, blocked); self.assertIs(os.system, blocked)
                self.assertEqual(core.CONFIG, self.config); self.assertEqual(core.STATE, self.state)
                self.assertEqual(core.wbagent.ROOT, self.state)
                self.assertTrue(files.STATE.is_relative_to(self.test_root))
                self.assertEqual(files.pull_dir.__module__, 'xdev_files')
                self.assertEqual(files.pull.__module__, 'xdev_files')
                capture = io.StringIO()
                with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
                    if case.startswith('download_timeouterror'):
                        with self.assertRaises(TimeoutError):
                            files.pull_dir(r'C:\xdev-fixture\work\model', str(destination))
                        rc = None
                    else:
                        rc = files.pull_dir(r'C:\xdev-fixture\work\model', str(destination))
                tx_root = state / 'transfers' / transport.tx_id
                outcome_path = tx_root / 'OUTCOME.json'
                outcome = json.loads(outcome_path.read_text(encoding='utf-8')) if outcome_path.exists() else None
                claim_retained = destination.with_name('.collected.xdev-claim.json').exists()
                zip_claims = [p.name for p in root.glob('.*.xdev-claim.json') if p.name != '.collected.xdev-claim.json']
                if case.startswith('verified_'):
                    self.assertEqual(rc, 0); self.assertEqual(len(transport.cleanup_calls), 1)
                    self.assertEqual(PureWindowsPath(transport.receipt_path), PureWindowsPath(transport.expected_remote))
                    self.assertTrue(transport.verified_before_cleanup)
                    self.assertFalse(claim_retained)
                    self.assertFalse((root / f'.xdev-pull-{transport.tx_id}.zip').exists())
                else:
                    self.assertEqual(transport.cleanup_calls, [], 'CLEANUP_OF_NON_OWNED_ZIP_REPRODUCED')
                    binding = case.endswith('_receipt') or case.startswith('foreign_zip')
                    self.assertEqual([c['stage'] for c in transport.calls], ['zip'] if binding else ['zip', 'chunk'])
                    if binding:
                        self.assertEqual(outcome['status'], 'ZIP_RECEIPT_BINDING_MISMATCH')
                        self.assertFalse(destination.exists()); self.assertFalse((tx_root / 'ARCHIVE.json').exists())
                        self.assertFalse((root / f'.xdev-pull-{transport.tx_id}.zip').exists())
                        self.assertTrue((tx_root / '0001-zip' / 'RETURN.json').is_file())
                    if case.startswith('download_timeout'):
                        self.assertTrue(claim_retained); self.assertTrue(zip_claims)
                        if case.startswith('download_timeout_rc124'):
                            self.assertEqual(rc, 124); self.assertEqual(outcome['status'], 'UNKNOWN')
                        else:
                            self.assertIsNone(rc); self.assertIsNone(outcome)
                    else:
                        self.assertEqual(rc, 5); self.assertFalse(claim_retained)


def case_test(case, environment_root):
    def test(self):
        self.check_case(case, environment_root)
    return test


for environment, cases in ((False, FIXED_CASES), (True, ENV_CASES)):
    for case in cases:
        setattr(ZipBindingTests, 'test_' + ('environment_' if environment else 'fixed_') + case,
                case_test(case, environment))


if __name__ == '__main__':
    unittest.main()
