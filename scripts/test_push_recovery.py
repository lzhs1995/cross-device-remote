"""Read-only original-result recovery tests; synthetic queue, no live requests."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


class PushRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / 'state'
        for name in ('files', 'queue', 'results'):
            (self.state / name).mkdir(parents=True)
        self.core = types.ModuleType('xdev_core')
        self.core.STATE = self.state
        self.core.PREAMBLE = '# synthetic preamble\n'
        self.core.emitted = lambda out: [json.loads(x[10:]) for x in out.split('\n') if x.startswith('XDEV_JSON ')]
        self.core.fail = lambda message, rc=1: rc
        self.core.ps_quote = lambda text: "'" + str(text).replace("'", "''") + "'"
        self.core.run_ps = lambda *a, **kw: self.fail('Recovery must not call run_ps')
        self.secret = b'synthetic-test-key-not-real'
        self.wb = types.SimpleNamespace(ROOT=self.state, read_result=self.read_result,
                                        _require_request=self.require_request, sign=self.sign)
        self.core.wbagent = self.wb
        self.patch = patch.dict(sys.modules, {'xdev_core': self.core})
        self.patch.start()
        spec = importlib.util.spec_from_file_location('push_recovery_candidate', HERE / 'xdev_files.py')
        self.mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.mod)

    def tearDown(self):
        self.patch.stop(); self.temp.cleanup()

    def sign(self, *parts):
        return hmac.new(self.secret, '|'.join(str(x) for x in parts).encode(), hashlib.sha256).hexdigest()

    def require_request(self, job):
        row = json.loads((self.state / 'queue' / f'{job}.json').read_text())
        if row['id'] != job or row['mac'] != self.sign('cmd', job, row['timeout'], row['script']):
            raise ValueError('original request signature mismatch')

    def read_result(self, job):
        try:
            row = json.loads((self.state / 'results' / f'{job}.json').read_text())
        except FileNotFoundError:
            return None
        if row['id'] != job or row['mac'] != self.sign('res', job, row['rc'], row['out'], row['err']):
            return None
        return row

    def fixture(self, dest='C:\\work\\input.txt', current=True, result=True, identity='a' * 32):
        root = self.state / 'transfers' / identity
        step = root / '0001-push'; step.mkdir(parents=True)
        data = b'complete uploaded bytes'
        intent = dict(operation='push', transfer_id=identity, pid=11, source=str(self.root / 'input.txt'),
                      destination=dest, sha256=hashlib.sha256(data).hexdigest(), bytes=len(data), overwrite=False)
        staged = self.mod._push_staged(intent)
        (self.state / 'files' / staged).write_bytes(data)
        script = self.mod._push_script(dest, intent['sha256'], identity, staged, False, current)
        (step / 'request.ps1').write_bytes(script.encode('utf-8'))
        self.write(root / 'INTENT.json', intent)
        self.write(step / 'INTENT.json', dict(script_sha256=hashlib.sha256(script.encode()).hexdigest(),
                                             options=dict(timeout=120), no_automatic_retry=True))
        self.write(step / 'RETURN.json', dict(rc=124, status='UNKNOWN', job_id=77,
                   original_result=str(self.state / 'results/77.json'), stdout_bytes=0, stderr_bytes=0,
                   stdout_sha256=hashlib.sha256(b'').hexdigest(), stderr_sha256=hashlib.sha256(b'').hexdigest()))
        self.write(root / 'OUTCOME.json', dict(status='UNKNOWN', job_id=77, staged=staged))
        encoded = base64.b64encode((self.core.PREAMBLE + script).encode()).decode()
        self.write(self.state / 'queue/77.json', dict(id=77, timeout=120, script=encoded,
                                                   mac=self.sign('cmd', 77, 120, encoded)))
        expanded = 'C:\\Users\\example\\AppData\\Local\\xdev\\input.txt' if '%' in dest else dest
        payload = dict(ok=True, dest=expanded, sha256=intent['sha256'], bytes=intent['bytes'])
        if current:
            payload.update(requested_dest=dest, transfer_id=identity)
        if result:
            self.save_result(payload)
        return root, intent, staged, payload

    def write(self, path, value):
        path.write_text(json.dumps(value))

    def save_result(self, payload, rc=0, job=77):
        out = base64.b64encode(('XDEV_JSON ' + json.dumps(payload) + '\r\n').encode()).decode()
        row = dict(id=job, rc=rc, out=out, err='', mac=self.sign('res', job, rc, out, ''))
        self.write(self.state / 'results/77.json', row)

    def test_unknown_without_original_result_stays_unknown_and_preserves_stage(self):
        root, intent, stage, payload = self.fixture(result=False)
        before = {p.relative_to(self.state): p.read_bytes() for p in self.state.rglob('*') if p.is_file()}
        with patch.object(self.mod, 'run_ps') as remote:
            result = self.mod.recover_push(root)
            remote.assert_not_called()
        self.assertEqual(result['status'], 'UNKNOWN')
        after = {p.relative_to(self.state): p.read_bytes() for p in self.state.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_complete_original_result_reconciles_once_without_enqueue_or_source_reread(self):
        root, intent, stage, payload = self.fixture()
        old = (root / 'OUTCOME.json').read_bytes()
        self.assertFalse(Path(intent['source']).exists())
        with patch.object(self.mod, 'run_ps') as remote:
            first = self.mod.recover_push(root / 'OUTCOME.json')
            second = self.mod.recover_push(root.name)
            remote.assert_not_called()
        self.assertEqual(first, second); self.assertTrue(first['ok'])
        self.assertEqual(first['job_id'], 77)
        self.assertFalse((self.state / 'files' / stage).exists())
        self.assertEqual(old, (root / 'OUTCOME.json').read_bytes())
        self.assertTrue((root / 'RECOVERY.json').exists())

    def test_concurrent_reconciliation_only_removes_own_stage_once(self):
        from push_cleanup_test_trace import StageCleanupTrace
        root, intent, stage, payload = self.fixture()
        unrelated = self.state / 'files/keep'; unrelated.write_bytes(b'other transfer')
        with StageCleanupTrace(self.mod) as trace, ThreadPoolExecutor(2) as pool:
            def recover(original_root):
                trace.begin(self.state / 'files' / stage)
                result = self.mod.recover_push(original_root)
                return result, trace.snapshot()
            observed = list(pool.map(recover, [root, root]))
        results = [item[0] for item in observed]
        failure_context = {'case': 'two_worker_reconciliation', 'results': results,
                           'stage_io': [item[1] for item in observed]}
        self.assertTrue(all(x['ok'] for x in results), failure_context)
        self.assertEqual(unrelated.read_bytes(), b'other transfer')

    def test_stage_disappears_after_lagging_observation_both_recoveries_complete(self):
        root, intent, name, payload = self.fixture()
        stage = self.state / 'files' / name
        unrelated = self.state / 'files/keep'; unrelated.write_bytes(b'other transfer')
        observed = threading.Barrier(2)
        cleaned = threading.Event()
        role = threading.local()
        original_stat, original_lstat = Path.stat, os.lstat

        def pause_after_observation(path, value):
            if Path(path) == stage and getattr(role, 'lagging', False) and not getattr(role, 'paused', False):
                role.paused = True
                observed.wait(timeout=10)
                if not cleaned.wait(timeout=10):
                    raise AssertionError('first recovery did not finish cleanup')
            return value

        def path_stat(path, *args, **kwargs):
            return pause_after_observation(path, original_stat(path, *args, **kwargs))

        def lstat(path, *args, **kwargs):
            return pause_after_observation(path, original_lstat(path, *args, **kwargs))

        def lagging():
            role.lagging = True
            return self.mod.recover_push(root)

        def leading():
            observed.wait(timeout=10)
            try:
                return self.mod.recover_push(root)
            finally:
                cleaned.set()

        # Path.stat captures the old exists()/is_file() gap; os.lstat captures
        # the same disappearance between the replacement's observation/open.
        with patch.object(Path, 'stat', path_stat), patch.object(self.mod.os, 'lstat', lstat), \
                patch.object(self.mod, 'run_ps') as remote, ThreadPoolExecutor(2) as pool:
            slow = pool.submit(lagging); fast = pool.submit(leading)
            results = [slow.result(timeout=15), fast.result(timeout=15)]
            remote.assert_not_called()
        self.assertTrue(all(x.get('ok') is True and x['status'] == 'COMPLETE' for x in results), results)
        self.assertFalse(stage.exists())
        self.assertEqual(unrelated.read_bytes(), b'other transfer')

    def test_recovery_cleanup_concurrency_24_rounds_four_workers(self):
        from push_cleanup_test_trace import StageCleanupTrace
        unrelated = self.state / 'files/keep'; unrelated.write_bytes(b'other transfer')
        with StageCleanupTrace(self.mod) as trace, patch.object(self.mod, 'run_ps') as remote, ThreadPoolExecutor(4) as pool:
            for index in range(24):
                with self.subTest(round=index):
                    root, intent, name, payload = self.fixture(identity=f'{index:032x}')
                    original_outcome = (root / 'OUTCOME.json').read_bytes()
                    start = threading.Barrier(4)

                    def recover():
                        trace.begin(self.state / 'files' / name)
                        start.wait(timeout=10)
                        result = self.mod.recover_push(root)
                        return result, trace.snapshot()

                    futures = [pool.submit(recover) for _ in range(4)]
                    observed = [f.result(timeout=15) for f in futures]
                    results = [item[0] for item in observed]
                    failure_context = {'round': index, 'results': results,
                                       'stage_io': [item[1] for item in observed]}
                    self.assertTrue(all(x.get('ok') is True and x['status'] == 'COMPLETE' for x in results), failure_context)
                    self.assertTrue(all(x == results[0] for x in results), failure_context)
                    self.assertFalse((self.state / 'files' / name).exists())
                    self.assertEqual((root / 'OUTCOME.json').read_bytes(), original_outcome)
                    self.assertEqual(unrelated.read_bytes(), b'other transfer')
            remote.assert_not_called()

    def test_stage_symlink_directory_and_mutated_bytes_are_preserved(self):
        for index, kind in enumerate(('symlink', 'dangling_symlink', 'directory', 'same_length', 'length')):
            with self.subTest(kind=kind):
                root, intent, name, payload = self.fixture(identity=f'{index:032x}')
                stage = self.state / 'files' / name
                valid = stage.read_bytes(); stage.unlink()
                external = self.root / f'external-{index}'
                external.write_bytes(valid)
                if kind == 'symlink':
                    stage.symlink_to(external)
                elif kind == 'dangling_symlink':
                    stage.symlink_to(self.root / 'absent-external')
                elif kind == 'directory':
                    stage.mkdir(); (stage / 'keep').write_bytes(b'keep')
                else:
                    stage.write_bytes(b'x' * len(valid) if kind == 'same_length' else b'changed length')
                before = os.lstat(stage)
                result = self.mod.recover_push(root)
                self.assertEqual((result['status'], result['reason']), ('REJECTED', 'OWNED_STAGED_FILE_CHANGED'))
                self.assertEqual((os.lstat(stage).st_dev, os.lstat(stage).st_ino), (before.st_dev, before.st_ino))
                self.assertEqual(external.read_bytes(), valid)
                if kind in ('symlink', 'dangling_symlink'):
                    self.assertTrue(stage.is_symlink())
                elif kind == 'directory':
                    self.assertEqual((stage / 'keep').read_bytes(), b'keep')
                else:
                    self.assertEqual(stage.read_bytes(), b'x' * len(valid) if kind == 'same_length' else b'changed length')

    def test_stage_swapped_to_symlink_between_lstat_and_open_is_preserved(self):
        root, intent, name, payload = self.fixture()
        stage = self.state / 'files' / name
        external = self.root / 'external'; external.write_bytes(stage.read_bytes())
        original_open = self.mod._open_recovered_stage

        def replace_before_open(path, flags, *args, **kwargs):
            if Path(path) == stage:
                stage.unlink(); stage.symlink_to(external)
            return original_open(path, flags, *args, **kwargs)

        with patch.object(self.mod, '_open_recovered_stage', replace_before_open):
            result = self.mod.recover_push(root)
        self.assertEqual((result['status'], result['reason']), ('REJECTED', 'OWNED_STAGED_FILE_CHANGED'))
        self.assertTrue(stage.is_symlink())
        self.assertEqual(hashlib.sha256(external.read_bytes()).hexdigest(), intent['sha256'])

    def test_stage_swapped_inode_at_open_is_rejected_without_nofollow(self):
        root, intent, name, payload = self.fixture()
        stage = self.state / 'files' / name
        replacement = self.root / 'replacement'; replacement.write_bytes(stage.read_bytes())
        replacement_identity = os.lstat(replacement).st_ino
        original_open = self.mod._open_recovered_stage

        def replace_before_open(path, flags, *args, **kwargs):
            if Path(path) == stage:
                os.replace(replacement, stage)
            return original_open(path, flags, *args, **kwargs)

        with patch.object(self.mod.os, 'O_NOFOLLOW', 0, create=True), patch.object(self.mod, '_open_recovered_stage', replace_before_open):
            result = self.mod.recover_push(root)
        self.assertEqual((result['status'], result['reason']), ('REJECTED', 'OWNED_STAGED_FILE_CHANGED'))
        self.assertEqual(os.lstat(stage).st_ino, replacement_identity)
        self.assertEqual(hashlib.sha256(stage.read_bytes()).hexdigest(), intent['sha256'])

    def test_stage_replaced_before_unlink_is_preserved(self):
        for index, kind in enumerate(('regular', 'symlink', 'directory')):
            with self.subTest(kind=kind):
                root, intent, name, payload = self.fixture(identity=f'{index:032x}')
                stage = self.state / 'files' / name
                replacement = self.root / f'replacement-{index}'; replacement.write_bytes(stage.read_bytes())
                original_lstat = os.lstat
                calls = 0

                def replace_before_recheck(path, *args, **kwargs):
                    nonlocal calls
                    if Path(path) == stage:
                        calls += 1
                        if calls == 2:
                            if kind == 'regular':
                                os.replace(replacement, stage)
                            else:
                                stage.unlink()
                                if kind == 'symlink':
                                    stage.symlink_to(replacement)
                                else:
                                    stage.mkdir()
                    return original_lstat(path, *args, **kwargs)

                with patch.object(self.mod.os, 'lstat', replace_before_recheck):
                    result = self.mod.recover_push(root)
                self.assertEqual((result['status'], result['reason']), ('REJECTED', 'OWNED_STAGED_FILE_CHANGED'))
                self.assertGreaterEqual(calls, 2)
                if kind == 'symlink':
                    self.assertTrue(stage.is_symlink())
                    self.assertEqual(hashlib.sha256(replacement.read_bytes()).hexdigest(), intent['sha256'])
                elif kind == 'directory':
                    self.assertTrue(stage.is_dir())
                else:
                    self.assertEqual(hashlib.sha256(stage.read_bytes()).hexdigest(), intent['sha256'])

    def test_stage_missing_at_final_lstat_or_unlink_is_already_clean(self):
        for index, point in enumerate(('lstat', 'unlink')):
            with self.subTest(point=point):
                root, intent, name, payload = self.fixture(identity=f'{index:032x}')
                stage = self.state / 'files' / name
                original_lstat, original_unlink = os.lstat, Path.unlink
                calls = 0

                def missing_at_recheck(path, *args, **kwargs):
                    nonlocal calls
                    if Path(path) == stage:
                        calls += 1
                        if point == 'lstat' and calls == 2:
                            original_unlink(stage)
                    return original_lstat(path, *args, **kwargs)

                def concurrent_unlink(path, *args, **kwargs):
                    if path == stage and point == 'unlink':
                        original_unlink(stage)
                    return original_unlink(path, *args, **kwargs)

                with patch.object(self.mod.os, 'lstat', missing_at_recheck), patch.object(Path, 'unlink', concurrent_unlink):
                    result = self.mod.recover_push(root)
                self.assertEqual((result['ok'], result['status']), (True, 'COMPLETE'), result)
                self.assertFalse(stage.exists())

    def test_stage_open_non_enoent_error_is_rejected_and_preserved(self):
        root, intent, name, payload = self.fixture()
        stage = self.state / 'files' / name; original = stage.read_bytes()
        original_open = self.mod._open_recovered_stage

        def deny_stage(path, *args, **kwargs):
            if Path(path) == stage:
                raise PermissionError('synthetic access denial')
            return original_open(path, *args, **kwargs)

        with patch.object(self.mod, '_open_recovered_stage', deny_stage):
            result = self.mod.recover_push(root)
        self.assertEqual((result['status'], result['reason']), ('REJECTED', 'OWNED_STAGED_FILE_CHANGED'))
        self.assertEqual(stage.read_bytes(), original)

    def test_legacy_absolute_reply_can_reconcile(self):
        root, intent, stage, payload = self.fixture(current=False)
        self.assertTrue(self.mod.recover_push(root)['ok'])

    def test_legacy_env_destination_cannot_reconcile_without_mapping(self):
        root, intent, stage, payload = self.fixture('%LOCALAPPDATA%\\xdev\\input.txt', current=False)
        result = self.mod.recover_push(root)
        self.assertEqual(result['status'], 'REJECTED')
        self.assertIn('EXPANSION_UNATTESTED', result['reason'])
        self.assertTrue((self.state / 'files' / stage).exists())

    def test_current_env_reply_requires_exact_requested_destination_and_transfer_id(self):
        root, intent, stage, payload = self.fixture('%LOCALAPPDATA%\\xdev\\input.txt')
        payload['transfer_id'] = 'b' * 32; self.save_result(payload)
        self.assertEqual(self.mod.recover_push(root)['status'], 'REJECTED')
        payload['transfer_id'] = root.name; self.save_result(payload)
        self.assertTrue(self.mod.recover_push(root)['ok'])

    def test_changed_queue_id_script_or_unsigned_result_does_not_recover(self):
        root, intent, stage, payload = self.fixture()
        queue = self.state / 'queue/77.json'; original = queue.read_bytes()
        bad = json.loads(original); bad['script'] = base64.b64encode(b'other upload').decode()
        bad['mac'] = self.sign('cmd', 77, 120, bad['script']); self.write(queue, bad)
        self.assertEqual(self.mod.recover_push(root)['status'], 'REJECTED')
        queue.write_bytes(original)
        returned = root / '0001-push/RETURN.json'; original_return = returned.read_bytes()
        bad = json.loads(original_return); bad['job_id'] = 78; self.write(returned, bad)
        self.assertEqual(self.mod.recover_push(root)['status'], 'REJECTED')
        returned.write_bytes(original_return)
        path = self.state / 'results/77.json'; bad = json.loads(path.read_text()); bad['mac'] = 'bad'
        self.write(path, bad)
        self.assertEqual(self.mod.recover_push(root)['status'], 'UNKNOWN')
        self.assertTrue((self.state / 'files' / stage).exists())

    def test_failed_and_hash_size_destination_mismatches_preserve_stage(self):
        root, intent, stage, payload = self.fixture()
        for field, value in [('sha256', '0' * 64), ('bytes', intent['bytes'] + 1), ('dest', 'C:\\wrong')]:
            with self.subTest(field=field):
                self.save_result(dict(payload, **{field: value}))
                self.assertEqual(self.mod.recover_push(root)['status'], 'REJECTED')
                self.assertFalse((root / 'RECOVERY.json').exists())
        self.save_result(dict(ok=False, reason='HASH_MISMATCH'), rc=4)
        recovered = self.mod.recover_push(root)
        self.assertEqual((recovered['status'], recovered['rc']), ('FAILED', 4))
        self.assertFalse(recovered['ok']); self.assertTrue((self.state / 'files' / stage).exists())

    def test_changed_stage_wrong_operation_and_external_receipt_path_are_rejected(self):
        root, intent, stage, payload = self.fixture()
        staged = self.state / 'files' / stage; staged.write_bytes(b'changed')
        self.assertEqual(self.mod.recover_push(root)['status'], 'REJECTED')
        self.assertEqual(staged.read_bytes(), b'changed')
        self.write(root / 'INTENT.json', dict(intent, operation='pull'))
        self.assertEqual(self.mod.recover_push(root)['status'], 'REJECTED')
        self.assertEqual(self.mod.recover_push(self.root / 'outside')['status'], 'REJECTED')

    def test_malformed_receipt_object_is_rejected_without_mutation(self):
        root, intent, stage, payload = self.fixture()
        path = root / 'INTENT.json'; path.write_text('[]')
        before = path.read_bytes()
        result = self.mod.recover_push(root)
        self.assertEqual(result['status'], 'REJECTED')
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue((self.state / 'files' / stage).exists())


if __name__ == '__main__':
    unittest.main()
