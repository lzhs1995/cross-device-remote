"""Offline callback ordering and original push recovery; no real queue imports."""
import base64
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import types
import unittest
from unittest.mock import Mock, patch

import test_push_recovery as recovery

HERE = Path(__file__).resolve().parent


class PushReceiptCallback(unittest.TestCase):
    # Reuse only the existing isolated signed-queue fixture, not its test cases.
    setUp = recovery.PushRecovery.setUp
    tearDown = recovery.PushRecovery.tearDown
    sign = recovery.PushRecovery.sign
    require_request = recovery.PushRecovery.require_request
    read_result = recovery.PushRecovery.read_result
    write = recovery.PushRecovery.write
    save_result = recovery.PushRecovery.save_result

    def test_callback_handle_is_persisted_before_enqueue_and_recovers_original_unknown(self):
        src = self.root / 'model input.txt'
        data = b'frozen input bytes'; src.write_bytes(data)
        destination = r'%LOCALAPPDATA%\xdev\test\model input.txt'
        stored = self.root / 'task-input-transfer.json'
        events = []

        def persist(info):
            events.append('callback')
            self.assertIs(type(info), dict)
            self.assertEqual(set(info), {'receipt', 'transfer_id', 'sha256', 'bytes', 'destination', 'source'})
            self.assertRegex(info['transfer_id'], r'^[0-9a-f]{32}$')
            expected = dict(receipt=str(self.state / 'transfers' / info['transfer_id']),
                            transfer_id=info['transfer_id'], sha256=hashlib.sha256(data).hexdigest(),
                            bytes=len(data), destination=destination, source=str(src))
            self.assertEqual(info, expected)
            self.assertIs(type(info['bytes']), int)
            intent = json.loads((Path(info['receipt']) / 'INTENT.json').read_text())
            self.assertEqual({k: intent[k] for k in expected if k != 'receipt'},
                             {k: v for k, v in expected.items() if k != 'receipt'})
            self.assertEqual(list((self.state / 'files').iterdir()), [])
            self.assertEqual(list((self.state / 'queue').iterdir()), [])
            self.write(stored, info)

        def submit_once(script, timeout):
            events.append('run_ps')
            info = json.loads(stored.read_text())
            stage = self.mod._push_staged(info)
            self.assertEqual((self.state / 'files' / stage).read_bytes(), data)
            encoded = base64.b64encode((self.core.PREAMBLE + script).encode()).decode()
            self.write(self.state / 'queue/77.json', dict(id=77, timeout=timeout, script=encoded,
                       mac=self.sign('cmd', 77, timeout, encoded)))
            return 124, '', '', 77

        with patch.object(self.mod, 'run_ps', side_effect=submit_once) as remote:
            self.assertEqual(self.mod.push(str(src), destination, receipt_callback=persist), 124)
            remote.assert_called_once()
        self.assertEqual(events, ['callback', 'run_ps'])
        info = json.loads(stored.read_text())
        receipt = Path(info['receipt'])
        outcome = (receipt / 'OUTCOME.json').read_bytes()
        self.assertEqual(json.loads(outcome)['status'], 'UNKNOWN')
        queue_before = (self.state / 'queue/77.json').read_bytes()
        payload = dict(ok=True, dest=r'C:\Users\example\AppData\Local\xdev\test\model input.txt',
                       requested_dest=destination, transfer_id=info['transfer_id'],
                       sha256=info['sha256'], bytes=info['bytes'])
        self.save_result(payload)
        src.unlink()
        with patch.object(self.mod, 'run_ps') as remote:
            first = self.mod.recover_push(info['receipt'])
            repeated = self.mod.recover_push(info['transfer_id'])
            remote.assert_not_called()
        self.assertTrue(first['ok'], first)
        self.assertEqual(first, repeated)
        self.assertEqual((first['job_id'], first['transfer_id']), (77, info['transfer_id']))
        self.assertFalse(first['resubmitted'])
        self.assertEqual((receipt / 'OUTCOME.json').read_bytes(), outcome)
        self.assertEqual((self.state / 'queue/77.json').read_bytes(), queue_before)
        self.assertEqual(list((self.state / 'files').iterdir()), [])

    def test_callback_exception_preserves_exception_and_never_stages_or_enqueues(self):
        src = self.root / 'input.txt'; src.write_bytes(b'input')
        keep = self.state / 'files/other-transfer'; keep.write_bytes(b'keep')
        old = self.state / 'queue/9.json'; old.write_bytes(b'original queue bytes')
        queue_before = {p.name: p.read_bytes() for p in (self.state / 'queue').iterdir()}
        error = OSError('task receipt persistence failed')
        seen = []

        def cannot_persist(info):
            seen.append(dict(info))
            self.assertTrue((Path(info['receipt']) / 'INTENT.json').is_file())
            self.assertEqual(list((self.state / 'files').iterdir()), [keep])
            raise error

        enqueue = Mock(side_effect=AssertionError('must never enqueue'))
        with patch.object(self.mod, 'run_ps') as remote, patch.object(self.wb, 'enqueue', enqueue, create=True):
            with self.assertRaises(OSError) as caught:
                self.mod.push(str(src), r'C:\input.txt', receipt_callback=cannot_persist)
            self.assertIs(caught.exception, error)
            remote.assert_not_called(); enqueue.assert_not_called()
        self.assertEqual(len(seen), 1)
        self.assertEqual(queue_before, {p.name: p.read_bytes() for p in (self.state / 'queue').iterdir()})
        self.assertEqual(list((self.state / 'files').iterdir()), [keep])
        self.assertEqual(keep.read_bytes(), b'keep')
        receipt = Path(seen[0]['receipt'])
        self.assertEqual(json.loads((receipt / 'OUTCOME.json').read_text()),
                         dict(status='ABORTED_BEFORE_ENQUEUE', reason='RECEIPT_CALLBACK_FAILED'))
        self.assertFalse((receipt / '0001-push').exists())

    def test_abort_record_failure_does_not_mask_callback_error_or_enqueue(self):
        src = self.root / 'input.txt'; src.write_bytes(b'input')
        callback_error = OSError('caller state is full')
        callback = Mock(side_effect=callback_error)
        with patch.object(self.mod._Transfer, 'finish', side_effect=OSError('journal disk is full')), \
                patch.object(self.mod, 'run_ps') as remote:
            with self.assertRaises(OSError) as caught:
                self.mod.push(str(src), receipt_callback=callback)
            self.assertIs(caught.exception, callback_error)
            remote.assert_not_called()
        self.assertEqual(list((self.state / 'files').iterdir()), [])
        self.assertEqual(list((self.state / 'queue').iterdir()), [])
        receipt = Path(callback.call_args.args[0]['receipt'])
        self.assertTrue((receipt / 'INTENT.json').is_file())
        self.assertFalse((receipt / '0001-push').exists())

    def test_callback_cannot_change_frozen_upload_fields_by_mutating_its_payload(self):
        src = self.root / 'input.txt'; data = b'original'; src.write_bytes(data)
        destination = r'C:\fixed\input.txt'
        captured = []

        def mutate(info):
            captured.append(dict(info))
            info.update(receipt='wrong', transfer_id='b' * 32, sha256='0' * 64,
                        bytes=0, destination=r'C:\wrong', source='wrong')

        with patch.object(self.mod, 'run_ps', return_value=(124, '', '', 77)) as remote:
            self.assertEqual(self.mod.push(str(src), destination, receipt_callback=mutate), 124)
        info = captured[0]
        script = remote.call_args.args[0]
        self.assertIn("$Dest = Expand 'C:\\fixed\\input.txt'", script)
        self.assertIn("transfer_id='" + info['transfer_id'] + "'", script)
        self.assertIn(info['sha256'], script)
        intent = json.loads((Path(info['receipt']) / 'INTENT.json').read_text())
        self.assertEqual(intent['destination'], destination)
        self.assertEqual(intent['transfer_id'], info['transfer_id'])
        self.assertEqual((self.state / 'files' / self.mod._push_staged(info)).read_bytes(), data)

    def test_no_callback_preserves_existing_call_forms_and_receipt_invariants(self):
        src = self.root / 'input.txt'; data = b'original'; src.write_bytes(data)
        identity = 'a' * 32
        sha = hashlib.sha256(data).hexdigest()
        empty_sha = hashlib.sha256(b'').hexdigest()
        staged = f'xdev-{sha[:12]}-{identity}-input.txt'
        call_forms = [(str(src),), (str(src), r'C:\input.txt'),
                      (str(src), r'C:\input.txt', True)]
        for index, args in enumerate(call_forms):
            for rc, status in [(0, 'COMPLETE'), (124, 'UNKNOWN'), (3, 'FAILED')]:
                with self.subTest(args=args, rc=rc):
                    state = self.root / f'{index}-{rc}'
                    (state / 'files').mkdir(parents=True)
                    keep = state / 'files/other-transfer'
                    keep.write_bytes(b'keep')
                    requested = args[1] if len(args) > 1 else r'%LOCALAPPDATA%\xdev\inbox\input.txt'
                    dest = requested if '%' not in requested else r'C:\Users\example\AppData\Local\xdev\inbox\input.txt'
                    payload = dict(ok=True, dest=dest, requested_dest=requested, transfer_id=identity,
                                   sha256=sha, bytes=len(data))
                    out = 'XDEV_JSON ' + json.dumps(payload) if rc == 0 else ''
                    output = io.StringIO()
                    with patch.object(self.mod, 'STATE', state), \
                            patch.object(self.mod.uuid, 'uuid4', return_value=types.SimpleNamespace(hex=identity)), \
                            patch.object(self.mod, 'run_ps', return_value=(rc, out, '', 77)) as remote, \
                            redirect_stdout(output):
                        returned = self.mod.push(*args)
                        remote.assert_called_once()
                    self.assertEqual(returned, rc)
                    self.assertEqual(output.getvalue(),
                                     f'XDEV_PUSHED {dest} sha256={sha} bytes={len(data)} job=77\n'
                                     if rc == 0 else '')
                    receipt = state / 'transfers' / identity
                    self.assertEqual(json.loads((receipt / 'INTENT.json').read_text()),
                                     dict(operation='push', transfer_id=identity, pid=os.getpid(),
                                          source=str(src), destination=requested, sha256=sha,
                                          bytes=len(data), overwrite=len(args) == 3))
                    outcome = dict(status=status, job_id=77, staged=staged)
                    if rc == 0:
                        outcome['remote'] = payload
                    self.assertEqual(json.loads((receipt / 'OUTCOME.json').read_text()), outcome)
                    step = receipt / '0001-push'
                    script = (step / 'request.ps1').read_bytes()
                    self.assertEqual(script, remote.call_args.args[0].encode())
                    self.assertEqual(remote.call_args.kwargs, dict(timeout=120))
                    self.assertEqual(json.loads((step / 'INTENT.json').read_text()),
                                     dict(script_sha256=hashlib.sha256(script).hexdigest(),
                                          options=dict(timeout=120), no_automatic_retry=True))
                    self.assertEqual(json.loads((step / 'RETURN.json').read_text()),
                                     dict(rc=rc, job_id=77, original_result=str(state / 'results/77.json'),
                                          stdout_bytes=len(out.encode()),
                                          stdout_sha256=hashlib.sha256(out.encode()).hexdigest(),
                                          stderr_bytes=0, stderr_sha256=empty_sha,
                                          status='UNKNOWN' if rc == 124 else 'RETURNED'))
                    expected = {f'transfers/{identity}/{name}' for name in
                                ('INTENT.json', 'OUTCOME.json', '0001-push/INTENT.json',
                                 '0001-push/RETURN.json', '0001-push/request.ps1')}
                    expected.add('files/other-transfer')
                    if rc != 0:
                        expected.add(f'files/{staged}')
                        self.assertEqual((state / 'files' / staged).read_bytes(), data)
                    self.assertEqual({p.relative_to(state).as_posix() for p in state.rglob('*') if p.is_file()},
                                     expected)
                    self.assertEqual(keep.read_bytes(), b'keep')


if __name__ == '__main__':
    unittest.main()
