"""Isolated concurrency/recovery tests. No installed module or remote call."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch
import zipfile

HERE = Path(__file__).resolve().parent


def wire(row, job=1, rc=0):
    return rc, 'XDEV_JSON ' + json.dumps(row) + '\n', '', job


def chunk(data, offset=0, size=None, digest=None):
    return dict(ok=True, size=len(data) if size is None else size,
                sha256=digest or hashlib.sha256(data).hexdigest(), offset=offset,
                n=len(data), b64=base64.b64encode(data).decode())


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        state = self.root / 'state'; (state / 'files').mkdir(parents=True)
        core = types.ModuleType('xdev_core')
        core.STATE = state
        core.emitted = lambda out: [json.loads(line[10:]) for line in out.split('\n') if line.startswith('XDEV_JSON ')]
        core.fail = lambda message, rc=1: rc
        core.ps_quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        core.run_ps = lambda *a, **kw: self.fail('unexpected remote call')
        with patch.dict(sys.modules, {'xdev_core': core}):
            spec = importlib.util.spec_from_file_location('isolated_transfer', HERE / 'xdev_files.py')
            self.mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.mod)
        self.state = state

    def tearDown(self):
        self.temp.cleanup()

    def outcomes(self):
        return [json.loads(p.read_text()) for p in (self.state / 'transfers').glob('*/OUTCOME.json')]

    def test_simultaneous_identical_pushes_have_distinct_staging_and_remote_temporaries(self):
        src = self.root / 'same data.txt'; src.write_bytes(b'same')
        barrier = threading.Barrier(2); staged = []; lock = threading.Lock()
        def run(script, **kwargs):
            name = re.search(r"/file/([^']+)'", script)[1]
            with lock: staged.append(name)
            self.assertTrue((self.state / 'files' / name).exists())
            barrier.wait(timeout=5)
            self.assertIn('[IO.FileMode]::CreateNew', script)
            self.assertIn('DEST_BUSY', script)
            self.assertIn('[IO.File]::Move($Tmp, $Dest)', script)
            dest = re.search(r"\$Dest = Expand '([^']+)'", script)[1]
            transfer_id = re.search(r"transfer_id='([^']+)'", script)[1]
            return wire(dict(ok=True, dest=dest, requested_dest=dest, transfer_id=transfer_id,
                             sha256=hashlib.sha256(b'same').hexdigest(), bytes=4))
        with patch.object(self.mod, 'run_ps', side_effect=run):
            with ThreadPoolExecutor(2) as pool:
                jobs = [pool.submit(self.mod.push, str(src), f'C:\\dest{i}') for i in range(2)]
                self.assertEqual([f.result(timeout=10) for f in jobs], [0, 0])
        self.assertEqual(len(set(staged)), 2)
        self.assertEqual(list((self.state / 'files').iterdir()), [])
        self.assertEqual([o['status'] for o in self.outcomes()], ['COMPLETE', 'COMPLETE'])

    def test_unknown_push_retains_original_id_staged_bytes_and_never_retries(self):
        src = self.root / 'input'; src.write_bytes(b'input')
        with patch.object(self.mod, 'run_ps', return_value=(124, '', '', 77)) as run:
            self.assertEqual(self.mod.push(str(src), 'C:\\input'), 124)
            run.assert_called_once()
        staged = list((self.state / 'files').iterdir())
        self.assertEqual(len(staged), 1); self.assertEqual(staged[0].read_bytes(), b'input')
        outcome = self.outcomes()[0]
        self.assertEqual((outcome['status'], outcome['job_id']), ('UNKNOWN', 77))

    def test_different_pull_targets_progress_concurrently_and_keep_distinct_part_names(self):
        barrier = threading.Barrier(2)
        def run(script, **kwargs):
            barrier.wait(timeout=5)
            self.assertEqual(len(list(self.root.glob('*.xdev-part-*'))), 2)
            barrier.wait(timeout=5)
            return wire(chunk(b'hello'))
        with patch.object(self.mod, 'run_ps', side_effect=run):
            with ThreadPoolExecutor(2) as pool:
                futures = [pool.submit(self.mod.pull, 'C:\\source', str(self.root / f'out{i}')) for i in range(2)]
                self.assertEqual([f.result(timeout=10) for f in futures], [0, 0])
        self.assertEqual((self.root / 'out0').read_bytes(), b'hello')
        self.assertEqual((self.root / 'out1').read_bytes(), b'hello')

    def test_same_pull_target_is_rejected_before_second_remote_read(self):
        entered = threading.Event(); release = threading.Event()
        def run(*args, **kwargs):
            entered.set(); self.assertTrue(release.wait(5)); return wire(chunk(b'original'))
        dst = self.root / 'same'
        with patch.object(self.mod, 'run_ps', side_effect=run) as remote:
            with ThreadPoolExecutor(1) as pool:
                first = pool.submit(self.mod.pull, 'C:\\source', str(dst))
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.mod.pull('C:\\second', str(dst)), 3)
                release.set(); self.assertEqual(first.result(10), 0)
            remote.assert_called_once()
        self.assertEqual(dst.read_bytes(), b'original')

    def test_unknown_second_chunk_keeps_partial_claim_and_original_queue_id(self):
        dst = self.root / 'partial'
        digest = hashlib.sha256(b'abcdef').hexdigest()
        replies = [wire(chunk(b'abc', size=6, digest=digest), job=11), (124, '', '', 12)]
        with patch.object(self.mod, 'CHUNK', 3), patch.object(self.mod, 'run_ps', side_effect=replies) as run:
            self.assertEqual(self.mod.pull('C:\\source', str(dst)), 124)
            self.assertEqual(self.mod.pull('C:\\source', str(dst)), 3)
            self.assertEqual(run.call_count, 2)
        self.assertFalse(dst.exists())
        parts = list(self.root.glob('partial.xdev-part-*'))
        self.assertEqual(len(parts), 1); self.assertEqual(parts[0].read_bytes(), b'abc')
        self.assertTrue((self.root / '.partial.xdev-claim.json').exists())
        unknown = next(o for o in self.outcomes() if o['status'] == 'UNKNOWN')
        self.assertEqual((unknown['job_id'], unknown['offset']), (12, 3))

    def test_bad_offsets_lengths_hashes_and_short_reads_never_publish(self):
        bad = [dict(chunk(b'abc'), offset=1), dict(chunk(b'abc'), n=2),
               dict(chunk(b''), size=3), dict(chunk(b'abc'), sha256='0' * 64),
               dict(chunk(b'abc'), b64='not base64!')]
        for index, row in enumerate(bad):
            with self.subTest(index=index):
                dst = self.root / f'bad{index}'
                with patch.object(self.mod, 'run_ps', return_value=wire(row)) as run:
                    self.assertEqual(self.mod.pull('C:\\source', str(dst)), 5)
                    run.assert_called_once()
                self.assertFalse(dst.exists())
                self.assertFalse((self.root / f'.bad{index}.xdev-claim.json').exists())

    def test_existing_and_legacy_partial_are_not_overwritten_or_replayed(self):
        dst = self.root / 'existing'; dst.write_bytes(b'keep')
        legacy = self.root / 'legacy.xdev-part'; legacy.write_bytes(b'keep partial')
        with patch.object(self.mod, 'run_ps') as run:
            self.assertEqual(self.mod.pull('remote', str(dst)), 3)
            self.assertEqual(self.mod.pull('remote', str(self.root / 'legacy')), 3)
            run.assert_not_called()
        self.assertEqual(dst.read_bytes(), b'keep'); self.assertEqual(legacy.read_bytes(), b'keep partial')

    def test_explicit_overwrite_preserves_each_old_file_under_unique_name(self):
        dst = self.root / 'replace'; dst.write_bytes(b'old')
        with patch.object(self.mod, 'run_ps', side_effect=[wire(chunk(b'new')), wire(chunk(b'newer'))]):
            self.assertEqual(self.mod.pull('remote', str(dst), overwrite=True), 0)
            self.assertEqual(self.mod.pull('remote', str(dst), overwrite=True), 0)
        backups = list(self.root.glob('replace.xdev-old-*'))
        self.assertEqual(len(backups), 2)
        self.assertEqual({p.read_bytes() for p in backups}, {b'old', b'new'})
        self.assertEqual(dst.read_bytes(), b'newer')

    def _zip_reply(self, script, job=40, count=1, **kwargs):
        name = re.search(r"outbox\\(pull-[a-f0-9]+\.zip)'", script)[1]
        return wire(dict(ok=True, zip='C:\\xdev\\outbox\\' + name, files=count), job=job)

    def _archive_pull(self, entries):
        def pull(remote, local, quiet=False):
            with zipfile.ZipFile(local, 'w') as archive:
                for name, value in entries:
                    archive.writestr(name, value)
            return 0
        return pull

    def test_simultaneous_directory_pulls_use_unique_remote_and_local_zip_names(self):
        barrier = threading.Barrier(2); remote_names = []; local_names = []
        def run(script, **kwargs):
            if 'CreateFromDirectory' in script:
                reply = self._zip_reply(script)
                remote_names.append(json.loads(reply[1][10:])['zip'])
                barrier.wait(timeout=5)
                return reply
            return 0, '', '', 41
        archive_pull = self._archive_pull([('result.txt', b'data')])
        def pull(remote, local, **kwargs):
            local_names.append(local); return archive_pull(remote, local, **kwargs)
        with patch.object(self.mod, 'run_ps', side_effect=run), patch.object(self.mod, 'pull', side_effect=pull):
            with ThreadPoolExecutor(2) as pool:
                jobs = [pool.submit(self.mod.pull_dir, 'C:\\work', str(self.root / f'dir{i}')) for i in range(2)]
                self.assertEqual([f.result(10) for f in jobs], [0, 0])
        self.assertEqual(len(set(remote_names)), 2); self.assertEqual(len(set(local_names)), 2)
        self.assertTrue(all((self.root / f'dir{i}' / 'result.txt').read_bytes() == b'data' for i in range(2)))

    def test_unknown_archive_download_keeps_remote_zip_and_no_cleanup_or_replay(self):
        with patch.object(self.mod, 'run_ps', side_effect=self._zip_reply) as run, \
             patch.object(self.mod, 'pull', return_value=124) as pull:
            dst = self.root / 'unfinished'
            self.assertEqual(self.mod.pull_dir('C:\\work', str(dst)), 124)
            self.assertEqual(self.mod.pull_dir('C:\\work', str(dst)), 3)
            run.assert_called_once(); pull.assert_called_once()
        self.assertTrue((self.root / '.unfinished.xdev-claim.json').exists())
        outcome = next(o for o in self.outcomes() if o['status'] == 'UNKNOWN')
        self.assertIn('pull-', outcome['remote_zip'])

    def test_unsafe_duplicate_or_count_mismatched_archives_never_publish_or_cleanup(self):
        cases = [([('../escape', b'x')], 1), ([('A.txt', b'a'), ('a.txt', b'b')], 2),
                 ([('a.txt', b'a')], 2)]
        for index, (entries, count) in enumerate(cases):
            with self.subTest(index=index):
                def run(script, **kwargs): return self._zip_reply(script, count=count)
                with patch.object(self.mod, 'run_ps', side_effect=run) as remote, \
                     patch.object(self.mod, 'pull', side_effect=self._archive_pull(entries)):
                    dst = self.root / f'badzip{index}'
                    self.assertEqual(self.mod.pull_dir('C:\\work', str(dst)), 5)
                    remote.assert_called_once()
                self.assertFalse(dst.exists()); self.assertFalse((self.root / 'escape').exists())

    def test_unknown_cleanup_preserves_verified_local_archive_and_original_cleanup_id(self):
        def run(script, **kwargs):
            return self._zip_reply(script) if 'CreateFromDirectory' in script else (124, '', '', 55)
        dst = self.root / 'verified'
        with patch.object(self.mod, 'run_ps', side_effect=run), \
             patch.object(self.mod, 'pull', side_effect=self._archive_pull([('a.txt', b'good')])):
            self.assertEqual(self.mod.pull_dir('C:\\work', str(dst)), 0)
        outcome = self.outcomes()[0]
        self.assertEqual(outcome['status'], 'COMPLETE_CLEANUP_PENDING')
        self.assertEqual(outcome['cleanup_job_id'], 55)
        self.assertTrue(Path(outcome['local_zip']).exists())
        self.assertEqual((dst / 'a.txt').read_bytes(), b'good')

    def test_push_rechecks_legacy_partial_and_late_destination_without_cleanup(self):
        src = self.root / 'input'; src.write_bytes(b'input')
        captured = []
        def run(script, **kwargs):
            captured.append(script)
            return wire(dict(ok=False, reason='DEST_APPEARED'), rc=3)
        with patch.object(self.mod, 'run_ps', side_effect=run):
            self.assertEqual(self.mod.push(str(src), 'C:\\input'), 3)
        script = captured[0]
        legacy = script.index('LEGACY_PARTIAL_REQUIRES_ORIGINAL_RECOVERY')
        download = script.index('Invoke-WebRequest')
        late = script.index('DEST_APPEARED')
        backup = script.index('[IO.File]::Move($Dest,')
        self.assertLess(legacy, download); self.assertLess(download, late); self.assertLess(late, backup)
        self.assertIn("if (-not $false) { Emit @{ok=$false;reason='DEST_APPEARED'", script)
        self.assertEqual(len(list((self.state / 'files').iterdir())), 1)

    def test_push_overwrite_flag_only_opens_explicit_late_replacement(self):
        src = self.root / 'input'; src.write_bytes(b'input')
        captured = []
        def run(script, **kwargs):
            captured.append(script)
            return 124, '', '', 70
        with patch.object(self.mod, 'run_ps', side_effect=run):
            self.assertEqual(self.mod.push(str(src), 'C:\\input', overwrite=True), 124)
        self.assertIn("if (-not $true) { Emit @{ok=$false;reason='DEST_APPEARED'", captured[0])
        self.assertIn('LEGACY_PARTIAL_REQUIRES_ORIGINAL_RECOVERY', captured[0])


if __name__ == '__main__':
    unittest.main()
