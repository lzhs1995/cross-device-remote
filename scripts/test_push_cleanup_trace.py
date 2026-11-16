"""Deterministic diagnostic controls; fault models are not native Windows proof."""
import errno
import importlib.util
import os
from pathlib import Path
import types
import unittest
from unittest.mock import patch

from push_cleanup_test_trace import StageCleanupTrace, STAT_FIELDS


HERE = Path(__file__).resolve().parent


class CleanupTraceTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('cleanup_trace_fixture', HERE / 'test_push_recovery.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        self.case = module.PushRecovery('runTest'); self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.root, self.intent, name, self.payload = self.case.fixture()
        self.stage = self.case.state / 'files' / name

    def recover(self):
        with StageCleanupTrace(self.case.mod) as trace:
            trace.begin(self.stage)
            result = self.case.mod.recover_push(self.root)
            observed = trace.snapshot()
        return result, observed

    def assert_rejected(self, result):
        self.assertEqual((result['status'], result['reason']), ('REJECTED', 'OWNED_STAGED_FILE_CHANGED'))
        self.assertNotIn('stage_cleanup_diagnostic', result)
        self.assertTrue(self.stage.exists())

    def test_unlink_sharing_violation_preserves_errno_and_is_not_accepted(self):
        original = Path.unlink
        failure = PermissionError(errno.EACCES, 'UNIT_TEST_ONLY_SHARING_VIOLATION')
        failure.winerror = 32
        def unlink(path, *args, **kwargs):
            if path == self.stage: raise failure
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', unlink): result, trace = self.recover()
        self.assert_rejected(result)
        event = trace['events'][-1]
        self.assertEqual((event['operation'], event['errno'], event['winerror']), ('unlink', errno.EACCES, 32))

    def test_open_access_denied_is_distinct_from_unlink_sharing(self):
        original = os.open
        failure = PermissionError(errno.EACCES, 'UNIT_TEST_ONLY_ACCESS_DENIED'); failure.winerror = 5
        def open_file(path, *args, **kwargs):
            if Path(path) == self.stage: raise failure
            return original(path, *args, **kwargs)
        with patch.object(self.case.mod.os, 'open', open_file): result, trace = self.recover()
        self.assert_rejected(result)
        event = trace['events'][-1]
        self.assertEqual((event['operation'], event['errno'], event['winerror']), ('open', errno.EACCES, 5))

    def test_fstat_identity_mismatch_records_both_identities(self):
        original = os.fstat
        def fstat(fd):
            value = original(fd)
            fields = {key:getattr(value, key, None) for key in STAT_FIELDS}
            fields['st_ino'] += 1
            return types.SimpleNamespace(**fields)
        with patch.object(self.case.mod.os, 'fstat', fstat): result, trace = self.recover()
        self.assert_rejected(result)
        stats = [e['stat'] for e in trace['events'] if 'stat' in e]
        self.assertEqual(stats[1]['st_ino'], stats[0]['st_ino'] + 1)
        self.assertEqual(trace['events'][-1]['operation'], 'close')

    def test_final_metadata_mismatch_is_not_mislabeled_as_os_permission(self):
        original = os.lstat; count = 0
        def lstat(path, *args, **kwargs):
            nonlocal count
            value = original(path, *args, **kwargs)
            if Path(path) == self.stage:
                count += 1
                if count >= 2:
                    fields = {key:getattr(value, key, None) for key in STAT_FIELDS}
                    fields['st_ctime_ns'] += 1
                    return types.SimpleNamespace(**fields)
            return value
        with patch.object(self.case.mod.os, 'lstat', lstat): result, trace = self.recover()
        self.assert_rejected(result)
        self.assertFalse(any(e['outcome'] == 'ERROR' for e in trace['events']))
        records = [e['stat'] for e in trace['events'] if e['operation'] == 'lstat']
        self.assertEqual(records[-1]['st_ctime_ns'], records[0]['st_ctime_ns'] + 1)

    def test_read_error_keeps_read_phase_and_closes_descriptor(self):
        original = os.read
        def read(fd, count): raise OSError(errno.EIO, 'UNIT_TEST_ONLY_READ_ERROR')
        with patch.object(self.case.mod.os, 'read', read): result, trace = self.recover()
        self.assert_rejected(result)
        errors = [e for e in trace['events'] if e['outcome'] == 'ERROR']
        self.assertEqual((errors[0]['operation'], errors[0]['errno']), ('read', errno.EIO))
        self.assertEqual(trace['events'][-1]['operation'], 'close')

    def test_success_and_missing_stage_keep_original_results(self):
        result, trace = self.recover()
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertFalse(self.stage.exists())
        again, absent = self.recover()
        self.assertEqual(result, again)
        self.assertEqual(absent['events'][0]['operation'], 'lstat')
        self.assertEqual(absent['events'][0]['error_type'], 'FileNotFoundError')
        self.assertLessEqual(len(trace['events']), 48)
        self.assertNotIn(str(self.case.root), str(trace))

    def test_event_budget_is_bounded_without_affecting_operations(self):
        with StageCleanupTrace(self.case.mod) as trace:
            trace.begin(self.stage)
            for _ in range(60): os.lstat(self.stage)
            observed = trace.snapshot()
        self.assertEqual(len(observed['events']), 48)
        self.assertEqual(observed['dropped'], 12)


if __name__ == '__main__':
    unittest.main(verbosity=2)
