"""Portable deterministic model of lookup/getattr racing a peer cleanup.

Synthetic signed fixtures, no live queue. The final lstat is modeled in two
phases: lookup observes an inode; peer recovery unlinks it; getattr then returns
that inode with unlink-updated ctime. No descriptor stays open across unlink,
so Windows sharing modes do not become an unrelated test prerequisite.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_push_recovery as fixtures


class CleanupInterleaving(unittest.TestCase):
    def run_schedule(self, rebound=None, deny_recheck=False):
        fixture = fixtures.PushRecovery()
        fixture.setUp()
        try:
            return self._run_schedule(fixture, rebound, deny_recheck)
        finally:
            fixture.tearDown()

    def _run_schedule(self, fixture, rebound, deny_recheck):
        root, intent, name, payload = fixture.fixture()
        stage = fixture.state / 'files' / name
        original_bytes = stage.read_bytes()
        original_outcome = (root / 'OUTCOME.json').read_bytes()
        unrelated = fixture.state / 'files/other-transfer'
        unrelated.write_bytes(b'preserve other owner')
        looked_up, cleaned = threading.Event(), threading.Event()
        role = threading.local()
        native_lstat = os.lstat
        modeled = []

        def split_final_lstat(path, *args, **kwargs):
            if Path(path) != stage or not getattr(role, 'lagging', False):
                return native_lstat(path, *args, **kwargs)
            role.calls = getattr(role, 'calls', 0) + 1
            if deny_recheck and role.calls == 3:
                raise PermissionError('synthetic fresh lookup denial')
            if role.calls != 2:
                return native_lstat(path, *args, **kwargs)
            before = native_lstat(stage)
            looked_up.set()
            if not cleaned.wait(10):
                raise AssertionError('leading recovery did not finish')
            # Explicit synthetic getattr result, not a native kernel claim.
            # Keep identity, mode, size and mtime, model unlink's ctime/nlink change.
            values = {key: getattr(before, key) for key in
                      ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns')}
            values.update(st_nlink=0, st_ctime_ns=before.st_ctime_ns + 1)
            modeled.append(values)
            return SimpleNamespace(**values)

        def lagging():
            role.lagging = True
            return fixture.mod.recover_push(root)

        def leading():
            if not looked_up.wait(10):
                raise AssertionError('lagging recovery did not reach final lookup')
            try:
                result = fixture.mod.recover_push(root)
                self.assertTrue(result.get('ok'), result)
                self.assertFalse(stage.exists())
                if rebound == 'regular':
                    stage.write_bytes(original_bytes)
                elif rebound == 'symlink':
                    stage.symlink_to(unrelated)
                elif rebound == 'directory':
                    stage.mkdir(); (stage / 'keep').write_bytes(b'keep')
                return result
            finally:
                cleaned.set()

        if rebound == 'symlink':
            probe = fixture.root / 'symlink-capability-probe'
            try:
                probe.symlink_to(unrelated)
            except OSError as error:
                self.skipTest('host cannot create test symlink: ' + str(error))
            else:
                probe.unlink()
        with patch.object(fixture.mod.os, 'lstat', split_final_lstat), \
                patch.object(fixture.mod, 'run_ps') as remote, ThreadPoolExecutor(2) as pool:
            slow = pool.submit(lagging)
            fast = pool.submit(leading)
            results = [slow.result(timeout=15), fast.result(timeout=15)]
            remote.assert_not_called()
        self.assertEqual(len(modeled), 1, 'required interleaving was not reached')
        self.assertEqual((root / 'OUTCOME.json').read_bytes(), original_outcome)
        self.assertEqual(unrelated.read_bytes(), b'preserve other owner')
        self.assertEqual(json.loads((root / 'RECOVERY.json').read_text())['job_id'], 77)
        self.assertTrue(all(r.get('resubmitted') is False and r.get('remote_operations') == 0 for r in results))
        if rebound == 'regular':
            self.assertEqual(stage.read_bytes(), original_bytes)
        elif rebound == 'symlink':
            self.assertTrue(stage.is_symlink())
        elif rebound == 'directory':
            self.assertEqual((stage / 'keep').read_bytes(), b'keep')
        else:
            self.assertFalse(stage.exists())
        return results

    def test_peer_cleanup_ctime_change_both_complete(self):
        results = self.run_schedule()
        self.assertTrue(all(r.get('ok') is True and r.get('status') == 'COMPLETE' for r in results), results)
        self.assertEqual(results[0], results[1])

    def assert_rebound_rejected(self, kind):
        slow, fast = self.run_schedule(rebound=kind)
        self.assertEqual((slow['status'], slow['reason']),
                         ('REJECTED', 'OWNED_STAGED_FILE_CHANGED'))
        self.assertTrue(fast['ok'])

    def test_rebound_regular_preserved_and_rejected(self):
        self.assert_rebound_rejected('regular')

    def test_rebound_symlink_preserved_and_rejected(self):
        self.assert_rebound_rejected('symlink')

    def test_rebound_directory_preserved_and_rejected(self):
        self.assert_rebound_rejected('directory')

    def test_fresh_lookup_permission_error_rejected(self):
        slow, fast = self.run_schedule(deny_recheck=True)
        self.assertEqual((slow['status'], slow['reason']),
                         ('REJECTED', 'OWNED_STAGED_FILE_CHANGED'))
        self.assertTrue(fast['ok'])

    def test_surviving_path_ctime_change_preserved_and_rejected(self):
        fixture = fixtures.PushRecovery()
        fixture.setUp()
        try:
            root, intent, name, payload = fixture.fixture()
            stage = fixture.state / 'files' / name
            original_bytes = stage.read_bytes()
            native_lstat = os.lstat
            calls = 0

            def changed_ctime(path, *args, **kwargs):
                nonlocal calls
                result = native_lstat(path, *args, **kwargs)
                if Path(path) == stage:
                    calls += 1
                    if calls == 2:
                        values = {key: getattr(result, key) for key in
                                  ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_nlink')}
                        values['st_ctime_ns'] = result.st_ctime_ns + 1
                        return SimpleNamespace(**values)
                return result

            with patch.object(fixture.mod.os, 'lstat', changed_ctime), patch.object(fixture.mod, 'run_ps') as remote:
                result = fixture.mod.recover_push(root)
                remote.assert_not_called()
            self.assertEqual((result['status'], result['reason']),
                             ('REJECTED', 'OWNED_STAGED_FILE_CHANGED'))
            self.assertEqual(stage.read_bytes(), original_bytes)
            self.assertGreaterEqual(calls, 2)
        finally:
            fixture.tearDown()


if __name__ == '__main__':
    unittest.main()
