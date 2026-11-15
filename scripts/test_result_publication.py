"""Isolated result publication/read tests; no enqueue, live state, or Windows.

HTTP binds only 127.0.0.1 on an ephemeral port. Process workers use this source
file and the test's temporary state; no installed xdev module is imported.
"""
import base64
import concurrent.futures
import contextlib
import copy
import hashlib
import hmac
import http.client
from http.server import ThreadingHTTPServer
import importlib.util
import io
import json
import multiprocessing
import os
from pathlib import Path
import secrets
import tempfile
import threading
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().with_name('wbagent.py')


def load_subject(state):
    spec = importlib.util.spec_from_file_location('isolated_result_publication', SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = Path(state)
    return module


def signature(key, *parts):
    raw = '|'.join(str(part) for part in parts).encode()
    return hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()


def publish_in_process(state, record, barrier, output):
    """Top-level target so multiprocessing spawn never inherits installed state."""
    try:
        subject = load_subject(state)
        barrier.wait(timeout=15)
        output.put({'status': subject.publish_result(record)})
    except BaseException as error:
        output.put({'error': type(error).__name__ + ': ' + str(error)})


class PublicationFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='xdev-result-publication-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for directory in ('queue', 'results', 'files'):
            (self.root / directory).mkdir()
        self.key = secrets.token_hex(32)
        (self.root / 'secret').write_text(self.key + '\n')
        self.subject = load_subject(self.root)
        self.request(7)

    def request(self, number):
        encoded = base64.b64encode(b'fixture only; never executed').decode()
        record = {'id': number, 'timeout': 15, 'script': encoded,
                  'mac': signature(self.key, 'cmd', number, 15, encoded)}
        (self.root / 'queue' / f'{number}.json').write_text(json.dumps(record))
        return record

    def result(self, number=7, text='synthetic 中文\r\n', **extra):
        record = {'id': number, 'rc': 0, 'out': base64.b64encode(text.encode()).decode(),
                  'err': base64.b64encode(b'synthetic stderr').decode(), 'ms': 23,
                  'metadata': {'stage': 'fixture', 'items': [1, None, '文'], 'value': 1}}
        record.update(extra)
        record['mac'] = signature(self.key, 'res', record['id'], record['rc'], record['out'], record['err'])
        return record

    def path(self, number=7):
        return self.root / 'results' / f'{number}.json'

    def conflicts(self, number=7):
        return sorted((self.root / 'conflicts' / str(number)).glob('*.json'))

    def assert_conflict_record(self, candidate, number=7):
        records = [json.loads(path.read_bytes()) for path in self.conflicts(number)]
        self.assertIn(candidate, records)
        return records


class PublicationTests(PublicationFixture):
    def test_first_publish_then_semantic_duplicate_keeps_all_fields_and_bytes(self):
        record = self.result(extra={'alpha': 1, 'beta': ['two', False]})
        self.assertEqual(self.subject.publish_result(record), 'published')
        original = self.path().read_bytes()
        self.assertEqual(self.subject.read_result(7), record)
        reordered = dict(reversed(list(record.items())))
        reordered['extra'] = {'beta': ['two', False], 'alpha': 1}
        self.assertEqual(self.subject.publish_result(reordered), 'duplicate')
        self.assertEqual(self.path().read_bytes(), original)
        self.assertEqual(self.conflicts(), [])

    def test_existing_valid_legacy_formatting_is_not_rewritten(self):
        record = self.result()
        original = (' \n' + json.dumps(record, ensure_ascii=False, indent=3) + '\n').encode()
        self.path().write_bytes(original)
        self.assertEqual(self.subject.read_result(7), record)
        self.assertEqual(self.subject.publish_result(record), 'duplicate')
        self.assertEqual(self.path().read_bytes(), original)

    def test_signed_fields_and_unsigned_metadata_changes_are_conflicts(self):
        winner = self.result()
        self.assertEqual(self.subject.publish_result(winner), 'published')
        original = self.path().read_bytes()
        candidates = [self.result(text='different stdout'), self.result(rc=9), self.result(ms=24)]
        changed = copy.deepcopy(winner)
        changed['metadata']['items'].reverse()
        candidates.append(changed)
        changed = copy.deepcopy(winner)
        changed['metadata']['value'] = True  # JSON true is distinct from JSON 1.
        candidates.append(changed)
        changed = copy.deepcopy(winner)
        changed['new_field'] = {'retained': 'whole candidate'}
        candidates.append(changed)
        changed = copy.deepcopy(winner)
        del changed['metadata']
        candidates.append(changed)
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.assertEqual(self.subject.publish_result(candidate), 'conflict')
                self.assertEqual(self.path().read_bytes(), original)
                self.assert_conflict_record(candidate)
        self.assertEqual(len(self.conflicts()), len(candidates))

    def test_bad_shape_base64_signature_and_nonfinite_never_publish(self):
        valid = self.result()
        candidates = [None, [], dict(valid, id=True), dict(valid, id='7'),
                      dict(valid, rc=False), dict(valid, rc='0'), dict(valid, out=[]),
                      dict(valid, mac='0' * 64), self.result(out='not valid base64!'),
                      dict(valid, metadata={'value': float('nan')}),
                      dict(valid, metadata={'value': float('inf')})]
        missing = dict(valid)
        del missing['err']
        candidates.append(missing)
        for candidate in candidates:
            with self.subTest(kind=repr(candidate)):
                with self.assertRaises(ValueError):
                    self.subject.publish_result(candidate)
                self.assertFalse(self.path().exists())
                self.assertEqual(self.conflicts(), [])

    def test_existing_result_cannot_bypass_result_or_original_queue_validation(self):
        record = self.result()
        self.assertEqual(self.subject.publish_result(record), 'published')
        original = self.path().read_bytes()
        with self.assertRaises(ValueError):
            self.subject.publish_result(dict(record, mac='0' * 64))
        queue_path = self.root / 'queue/7.json'
        queue_path.unlink()
        with self.assertRaises(ValueError):
            self.subject.publish_result(record)
        request = self.request(7)
        request['script'] = base64.b64encode(b'changed request').decode()
        queue_path.write_text(json.dumps(request))
        with self.assertRaises(ValueError):
            self.subject.publish_result(record)
        wrong_id = self.request(8)
        queue_path.write_text(json.dumps(wrong_id))
        with self.assertRaises(ValueError):
            self.subject.publish_result(record)
        self.assertEqual(self.path().read_bytes(), original)
        self.assertEqual(self.conflicts(), [])

    def test_unknown_request_and_malformed_queue_create_no_result(self):
        with self.assertRaises(ValueError):
            self.subject.publish_result(self.result(number=999))
        self.assertFalse(self.path(999).exists())
        queue_path = self.root / 'queue/7.json'
        for raw in (b'{"id":', b'[]', b'{"id":7,"id":7}', b'null'):
            with self.subTest(raw=raw):
                queue_path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    self.subject.publish_result(self.result())
                self.assertFalse(self.path().exists())

    def test_invalid_legacy_result_is_not_repaired_and_new_candidate_is_retained(self):
        record = self.result()
        invalids = [b'{"id":', b'[]', b'{}', b'\xff',
                    json.dumps(dict(record, mac='0' * 64)).encode(),
                    json.dumps(self.result(number=8)).encode(),
                    json.dumps(self.result(out='!invalid!')).encode(),
                    json.dumps(dict(record, metadata={'value': float('nan')})).encode(),
                    ('{"id":7,' + json.dumps(record)[1:]).encode()]
        for raw in invalids:
            with self.subTest(raw=raw):
                self.path().write_bytes(raw)
                self.assertIsNone(self.subject.read_result(7))
                self.assertEqual(self.subject.publish_result(record), 'conflict')
                self.assertEqual(self.path().read_bytes(), raw)
                self.assertEqual(len(self.conflicts()), 1)
                self.assert_conflict_record(record)

    def test_missing_reader_returns_none_without_creating_a_receipt(self):
        self.assertIsNone(self.subject.read_result(7))
        self.assertFalse(self.path().exists())

    def test_results_storage_error_remains_oserror_for_publish_and_read(self):
        results = self.root / 'results'
        results.rmdir()
        results.write_bytes(b'fixture obstruction')
        with self.assertRaises(OSError):
            self.subject.publish_result(self.result())
        with self.assertRaises(OSError):
            self.subject.read_result(7)
        self.assertEqual(results.read_bytes(), b'fixture obstruction')

    def test_conflict_storage_error_does_not_modify_winner_or_claim_success(self):
        winner = self.result()
        self.assertEqual(self.subject.publish_result(winner), 'published')
        original = self.path().read_bytes()
        (self.root / 'conflicts').write_bytes(b'fixture obstruction')
        self.assertEqual(self.subject.publish_result(winner), 'duplicate')
        with self.assertRaises(OSError):
            self.subject.publish_result(self.result(ms=24))
        self.assertEqual(self.path().read_bytes(), original)

    def test_concurrent_identical_threads_publish_once_without_conflicts(self):
        record = self.result()
        barrier = threading.Barrier(8)

        def publish():
            barrier.wait(timeout=10)
            return self.subject.publish_result(copy.deepcopy(record))

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(publish) for _ in range(8)]
            statuses = [future.result(timeout=15) for future in futures]
        self.assertEqual(statuses.count('published'), 1)
        self.assertEqual(statuses.count('duplicate'), 7)
        self.assertEqual(self.subject.read_result(7), record)
        self.assertEqual(self.conflicts(), [])

    def test_concurrent_different_threads_keep_every_losing_candidate(self):
        records = [self.result(text=f'candidate {index}', ordinal=index) for index in range(8)]
        barrier = threading.Barrier(len(records))

        def publish(record):
            barrier.wait(timeout=10)
            return self.subject.publish_result(record)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(publish, record) for record in records]
            statuses = [future.result(timeout=15) for future in futures]
        self.assertEqual(statuses.count('published'), 1)
        self.assertEqual(statuses.count('conflict'), 7)
        winner = self.subject.read_result(7)
        self.assertIn(winner, records)
        losers = [record for record in records if record != winner]
        self.assertEqual(len(self.conflicts()), len(losers))
        self.assertCountEqual([json.loads(path.read_bytes()) for path in self.conflicts()], losers)

    def test_repeated_and_concurrent_same_conflict_keeps_one_evidence_file(self):
        winner, candidate = self.result(), self.result(ms=24)
        self.assertEqual(self.subject.publish_result(winner), 'published')
        original = self.path().read_bytes()
        self.assertEqual(self.subject.publish_result(candidate), 'conflict')
        evidence = self.conflicts()[0]
        evidence_raw = evidence.read_bytes()
        barrier = threading.Barrier(8)

        def publish():
            barrier.wait(timeout=10)
            return self.subject.publish_result(copy.deepcopy(candidate))

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(publish) for _ in range(8)]
            self.assertEqual([future.result(timeout=15) for future in futures], ['conflict'] * 8)
        self.assertEqual(self.conflicts(), [evidence])
        self.assertEqual(evidence.read_bytes(), evidence_raw)
        self.assertEqual(self.path().read_bytes(), original)

    def test_corrupted_existing_conflict_evidence_is_not_repaired(self):
        winner, candidate = self.result(), self.result(ms=24)
        self.assertEqual(self.subject.publish_result(winner), 'published')
        original = self.path().read_bytes()
        self.assertEqual(self.subject.publish_result(candidate), 'conflict')
        evidence = self.conflicts()[0]
        damaged = b'{"damaged original conflict'
        evidence.write_bytes(damaged)
        with self.assertRaises(OSError):
            self.subject.publish_result(candidate)
        self.assertEqual(evidence.read_bytes(), damaged)
        self.assertEqual(self.conflicts(), [evidence])
        self.assertEqual(self.path().read_bytes(), original)

    def test_different_processes_have_one_winner_and_one_conflict(self):
        context = multiprocessing.get_context('spawn')
        barrier, output = context.Barrier(2), context.Queue()
        records = [self.result(text='process A'), self.result(text='process B')]
        processes = [context.Process(target=publish_in_process,
                     args=(str(self.root), record, barrier, output)) for record in records]
        try:
            for process in processes:
                process.start()
            reports = [output.get(timeout=25) for _ in processes]
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)
            self.assertCountEqual(reports, [{'status': 'published'}, {'status': 'conflict'}])
            winner = self.subject.read_result(7)
            self.assertIn(winner, records)
            self.assertEqual(len(self.conflicts()), 1)
            self.assertEqual(json.loads(self.conflicts()[0].read_bytes()), next(row for row in records if row != winner))
        finally:
            for process in processes:
                if process.pid is not None and process.is_alive():
                    process.terminate()  # Only this test's own temporary publisher process.
                    process.join(timeout=5)
            output.close()
            output.join_thread()

    def test_readers_observe_none_before_atomic_commit_and_complete_record_after(self):
        record = self.result(text='large synthetic output\n' * 10000)
        before_commit, allow_commit = threading.Event(), threading.Event()
        committed, allow_return = threading.Event(), threading.Event()
        canonical = self.path()

        def gated(operation):
            def call(source, destination, *args, **kwargs):
                if Path(destination).absolute() != canonical:
                    return operation(source, destination, *args, **kwargs)
                before_commit.set()
                if not allow_commit.wait(timeout=10):
                    raise TimeoutError('test gate before atomic publication')
                value = operation(source, destination, *args, **kwargs)
                committed.set()
                if not allow_return.wait(timeout=10):
                    raise TimeoutError('test gate after atomic publication')
                return value
            return call

        # Gate public filesystem commit operations, not a private helper or temp name.
        originals = {name: getattr(os, name) for name in ('link', 'rename', 'replace')}
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            with mock.patch.object(self.subject.os, 'link', gated(originals['link'])), \
                 mock.patch.object(self.subject.os, 'rename', gated(originals['rename'])), \
                 mock.patch.object(self.subject.os, 'replace', gated(originals['replace'])):
                future = pool.submit(self.subject.publish_result, record)
                try:
                    self.assertTrue(before_commit.wait(timeout=10), 'No atomic filename commit was reached')
                    self.assertFalse(canonical.exists())
                    self.assertIsNone(self.subject.read_result(7))
                    allow_commit.set()
                    self.assertTrue(committed.wait(timeout=10))
                    self.assertEqual(json.loads(canonical.read_bytes()), record)
                    self.assertEqual(self.subject.read_result(7), record)
                    self.assertFalse(future.done())
                finally:
                    allow_commit.set()
                    allow_return.set()
                self.assertEqual(future.result(timeout=10), 'published')


class HttpPublicationTests(PublicationFixture):
    def setUp(self):
        super().setUp()
        self.subject.Handler.allow_ip = '127.0.0.1/32'
        self.subject.Handler.own_net = None
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self.subject.Handler)
        self.server.daemon_threads = True
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)
        self.assertFalse(self.server_thread.is_alive())

    def post(self, record=None, raw=None):
        if raw is None:
            raw = json.dumps(record, ensure_ascii=False).encode()
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            connection.request('POST', '/result', body=raw, headers={'Content-Type': 'application/json'})
            response = connection.getresponse()
            body = response.read()
            return response.status, body
        finally:
            connection.close()

    def test_v1_http_publish_duplicate_and_metadata_conflict(self):
        record = self.result()
        self.assertEqual(self.post(record)[0], 200)
        original = self.path().read_bytes()
        self.assertEqual(self.post(dict(reversed(list(record.items()))))[0], 200)
        candidate = self.result(ms=24)
        self.assertEqual(self.post(candidate)[0], 409)
        self.assertEqual(self.post(candidate)[0], 409)
        self.assertEqual(len(self.conflicts()), 1)
        self.assertEqual(self.path().read_bytes(), original)
        self.assertEqual(self.subject.read_result(7), record)
        self.assert_conflict_record(candidate)

    def test_http_unknown_request_bad_queue_and_bad_result_mac_are_distinct(self):
        self.assertEqual(self.post(self.result(number=999))[0], 400)
        self.assertFalse(self.path(999).exists())
        self.assertEqual(self.post(dict(self.result(), mac='0' * 64))[0], 401)
        request = self.request(7)
        request['mac'] = '0' * 64
        (self.root / 'queue/7.json').write_text(json.dumps(request))
        self.assertEqual(self.post(self.result())[0], 400)
        self.assertFalse(self.path().exists())

    def test_http_malformed_shape_duplicate_keys_and_nonfinite_are_rejected(self):
        record = self.result()
        malformed = [b'{broken', b'[]', b'\xff',
                     json.dumps(dict(record, id=True)).encode(),
                     json.dumps(dict(record, metadata={'n': float('nan')})).encode(),
                     ('{"id":7,' + json.dumps(record)[1:]).encode()]
        for raw in malformed:
            with self.subTest(raw=raw):
                self.assertEqual(self.post(raw=raw)[0], 400)
                self.assertFalse(self.path().exists())
        self.assertEqual(self.conflicts(), [])

    def test_http_legacy_partial_bytes_are_preserved_on_conflict(self):
        raw = b'{"id":7,"out":"unfinished'
        self.path().write_bytes(raw)
        candidate = self.result()
        self.assertEqual(self.post(candidate)[0], 409)
        self.assertEqual(self.path().read_bytes(), raw)
        self.assertIsNone(self.subject.read_result(7))
        self.assert_conflict_record(candidate)

    def test_http_storage_failure_is_503_and_keeps_original_result(self):
        winner = self.result()
        self.assertEqual(self.post(winner)[0], 200)
        original = self.path().read_bytes()
        (self.root / 'conflicts').write_bytes(b'fixture obstruction')
        self.assertEqual(self.post(self.result(ms=24))[0], 503)
        self.assertEqual(self.path().read_bytes(), original)

    def test_http_corrupted_digest_evidence_returns_503_without_overwrite(self):
        winner, candidate = self.result(), self.result(ms=24)
        self.assertEqual(self.post(winner)[0], 200)
        original = self.path().read_bytes()
        self.assertEqual(self.post(candidate)[0], 409)
        evidence = self.conflicts()[0]
        damaged = b'legacy conflict evidence is malformed'
        evidence.write_bytes(damaged)
        self.assertEqual(self.post(candidate)[0], 503)
        self.assertEqual(self.conflicts(), [evidence])
        self.assertEqual(evidence.read_bytes(), damaged)
        self.assertEqual(self.path().read_bytes(), original)

    def test_missing_secret_propagates_to_read_await_and_http_503(self):
        record = self.result()
        self.assertEqual(self.post(record)[0], 200)
        original = self.path().read_bytes()
        (self.root / 'secret').unlink()
        with self.assertRaises(FileNotFoundError):
            self.subject.read_result(7)
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            with self.assertRaises(FileNotFoundError):
                self.subject.await_result(7, 0)
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(self.post(record)[0], 503)
        self.assertEqual(self.path().read_bytes(), original)
        self.assertEqual(self.conflicts(), [])

    def test_concurrent_http_candidates_get_one_200_and_one_409(self):
        records = [self.result(text='HTTP A'), self.result(text='HTTP B')]
        barrier = threading.Barrier(2)

        def post(record):
            barrier.wait(timeout=10)
            return self.post(record)[0]

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(post, record) for record in records]
            statuses = [future.result(timeout=15) for future in futures]
        self.assertCountEqual(statuses, [200, 409])
        winner = self.subject.read_result(7)
        self.assertIn(winner, records)
        self.assertEqual(len(self.conflicts()), 1)
        self.assertEqual(json.loads(self.conflicts()[0].read_bytes()), next(row for row in records if row != winner))


if __name__ == '__main__':
    unittest.main(verbosity=2)
