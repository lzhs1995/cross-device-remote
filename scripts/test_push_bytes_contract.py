"""Portable exact-byte journal tests; synthetic signed queue, no remote calls."""
import base64
from contextlib import contextmanager
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load_fixture():
    spec = importlib.util.spec_from_file_location('push_bytes_fixture', HERE / 'test_push_recovery.py')
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    return fixture.PushRecovery('runTest')


@contextmanager
def windows_request_text_seam():
    """Model Windows newline translation explicitly, even on a POSIX runner."""
    original = Path.write_text

    def write_text(path, data, encoding=None, errors=None, newline=None):
        if path.name != 'request.ps1':
            return original(path, data, encoding=encoding, errors=errors, newline=newline)
        with path.open('wb') as raw:
            with io.TextIOWrapper(raw, encoding=encoding or 'utf-8', errors=errors,
                                  newline='\r\n' if newline is None else newline) as stream:
                return stream.write(data)

    with patch.object(Path, 'write_text', write_text):
        yield


class PushBytesContract(unittest.TestCase):
    def setUp(self):
        self.case = load_fixture()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)

    def test_production_journal_preserves_original_lf_bytes_and_queue_identity(self):
        case = self.case
        data = b'complete uploaded bytes'
        destination = r'C:\work\input.txt'
        tx = case.mod._Transfer('push', source=str(case.root / 'input.txt'), destination=destination,
            sha256=hashlib.sha256(data).hexdigest(), bytes=len(data), overwrite=False)
        intent = json.loads((tx.root / 'INTENT.json').read_text())
        staged = case.mod._push_staged(intent)
        (case.state / 'files' / staged).write_bytes(data)
        script = case.mod._push_script(destination, intent['sha256'], tx.id, staged, False)
        sent = []

        def fake_send(body, **kwargs):
            sent.append(body)
            encoded = base64.b64encode((case.core.PREAMBLE + body).encode('utf-8')).decode('ascii')
            case.write(case.state / 'queue/77.json', dict(id=77, timeout=120, script=encoded,
                mac=case.sign('cmd', 77, 120, encoded)))
            case.save_result(dict(ok=True, dest=destination, requested_dest=destination,
                transfer_id=tx.id, sha256=intent['sha256'], bytes=intent['bytes']))
            return 124, '', '', 77

        with windows_request_text_seam(), patch.object(case.mod, 'run_ps', fake_send):
            tx.call('push', script, timeout=120)
        tx.finish('UNKNOWN', job_id=77, staged=staged)
        step = tx.root / '0001-push'
        self.assertGreater(script.count('\n'), 0)
        self.assertEqual((step / 'request.ps1').read_bytes(), script.encode('utf-8'))
        self.assertEqual(json.loads((step / 'INTENT.json').read_text())['script_sha256'],
                         hashlib.sha256(script.encode('utf-8')).hexdigest())
        self.assertEqual(sent, [script])
        with patch.object(case.mod, 'run_ps') as remote:
            result = case.mod.recover_push(tx.root)
            remote.assert_not_called()
        self.assertEqual((result['ok'], result['status'], result['job_id']), (True, 'COMPLETE', 77))
        self.assertFalse(Path(intent['source']).exists())

    def test_fixture_writes_original_lf_bytes_under_windows_text_seam(self):
        with windows_request_text_seam():
            root, intent, staged, payload = self.case.fixture()
        expected = self.case.mod._push_script(intent['destination'], intent['sha256'], root.name,
                                             staged, intent['overwrite']).encode('utf-8')
        self.assertEqual((root / '0001-push/request.ps1').read_bytes(), expected)
        self.assertEqual(self.case.mod.recover_push(root)['status'], 'COMPLETE')

    def test_existing_crlf_evidence_is_rejected_without_normalization_or_rewrite(self):
        root, intent, staged, payload = self.case.fixture()
        request = root / '0001-push/request.ps1'
        original = request.read_bytes()
        self.assertNotIn(b'\r\n', original)
        request.write_bytes(original.replace(b'\n', b'\r\n'))
        before = {str(p.relative_to(self.case.state)): p.read_bytes()
                  for p in self.case.state.rglob('*') if p.is_file()}
        with patch.object(self.case.mod, 'run_ps') as remote:
            result = self.case.mod.recover_push(root)
            remote.assert_not_called()
        after = {str(p.relative_to(self.case.state)): p.read_bytes()
                 for p in self.case.state.rglob('*') if p.is_file()}
        self.assertEqual((result['status'], result['reason']), ('REJECTED', 'ORIGINAL_SCRIPT_CHANGED'))
        self.assertEqual(before, after)

    def test_original_unicode_and_existing_crlf_strings_are_preserved_exactly(self):
        for script in ('\u7b2c\u4e00\u884c\n\u7b2c\u4e8c\u884c\n', 'first\r\nsecond\r\n', 'single line'):
            with self.subTest(script=script):
                tx = self.case.mod._Transfer('unit-test-only')
                with windows_request_text_seam(), patch.object(self.case.mod, 'run_ps', return_value=(124, '', '', 77)) as send:
                    tx.call('unit', script)
                step = tx.root / '0001-unit'
                self.assertEqual((step / 'request.ps1').read_bytes(), script.encode('utf-8'))
                self.assertEqual(json.loads((step / 'INTENT.json').read_text())['script_sha256'],
                                 hashlib.sha256(script.encode('utf-8')).hexdigest())
                send.assert_called_once_with(script)


if __name__ == '__main__':
    unittest.main(verbosity=2)
