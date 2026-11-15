"""SHA-checked file transfers with per-attempt names and same-target exclusion.

Public push/pull/pull_dir signatures and the original signed queue are retained.
An UNKNOWN result keeps its original queue ID, claim and temporary artifacts;
this module never resubmits it or deletes another transfer's temporary files.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import uuid
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from xdev_core import STATE, emitted, fail, ps_quote, run_ps

CHUNK = 3 * 1024 * 1024


def _write_new(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())


class _Transfer:
    """Small original-attempt journal, not a second queue or retry controller.

    run_ps prints the original queue ID at submission; we persist it on return.
    A crash before return may leave only request.ps1 and the caller's queue log:
    this is not crash-proof queue binding and never authorizes a new submission.
    """
    def __init__(self, operation, **inputs):
        self.id = uuid.uuid4().hex
        self.root = STATE / 'transfers' / self.id
        self.root.mkdir(parents=True, exist_ok=False)
        self.sequence = 0
        _write_new(self.root / 'INTENT.json', dict(operation=operation, transfer_id=self.id,
                   pid=os.getpid(), **inputs))

    def call(self, label, script, **kwargs):
        self.sequence += 1
        root = self.root / f'{self.sequence:04d}-{label}'
        root.mkdir()
        (root / 'request.ps1').write_text(script, encoding='utf-8')
        _write_new(root / 'INTENT.json', dict(script_sha256=hashlib.sha256(script.encode()).hexdigest(),
                                           options=kwargs, no_automatic_retry=True))
        result = run_ps(script, **kwargs)
        rc, out, err, job_id = result
        # Full signed replies already live in the original queue. Do not copy
        # every 3-MiB data chunk into a second large journal on a constrained disk.
        _write_new(root / 'RETURN.json', dict(rc=rc, job_id=job_id,
                   original_result=str(STATE / 'results' / f'{job_id}.json'),
                   stdout_bytes=len(out.encode()), stdout_sha256=hashlib.sha256(out.encode()).hexdigest(),
                   stderr_bytes=len(err.encode()), stderr_sha256=hashlib.sha256(err.encode()).hexdigest(),
                   status='UNKNOWN' if rc == 124 else 'RETURNED'))
        return result

    def finish(self, status, **details):
        _write_new(self.root / 'OUTCOME.json', dict(status=status, **details))


def _destination(local):
    raw = Path(local).expanduser().absolute()
    raw.parent.mkdir(parents=True, exist_ok=True)
    dst = raw.parent.resolve() / raw.name
    if dst.is_symlink():
        raise ValueError('Destination must not be a symlink')
    return dst


def _claim(dst, transfer):
    claim = dst.with_name('.' + dst.name + '.xdev-claim.json')
    _write_new(claim, dict(transfer_id=transfer.id, receipt=str(transfer.root), destination=str(dst)))
    return claim


def _release(claim, transfer):
    # A changed/replaced claim is not ours to remove.
    value = json.loads(claim.read_text(encoding='utf-8'))
    if value.get('transfer_id') != transfer.id:
        raise RuntimeError('TRANSFER_CLAIM_CHANGED')
    claim.unlink()


def _push_staged(intent):
    name = Path(intent['source']).name
    safe = re.sub(r'[^A-Za-z0-9._-]', '_', name)[:80] or 'file'
    return f"xdev-{intent['sha256'][:12]}-{intent['transfer_id']}-{safe}"


def _push_script(dest, sha, transfer_id, staged, overwrite, recovery_binding=True):
    binding = f';requested_dest={ps_quote(dest)};transfer_id={ps_quote(transfer_id)}' if recovery_binding else ''
    return f"""$Dest = Expand {ps_quote(dest)}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Dest) | Out-Null
$ClaimPath = $Dest + '.xdev-claim'
try {{ $Claim = [IO.File]::Open($ClaimPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read) }}
catch [IO.IOException] {{
  if (Test-Path -LiteralPath $ClaimPath) {{ Emit @{{ok=$false;reason='DEST_BUSY';dest=$Dest}}; exit 3 }}
  throw
}}
try {{
  $ClaimBytes = [Text.Encoding]::UTF8.GetBytes('{transfer_id}')
  $Claim.Write($ClaimBytes, 0, $ClaimBytes.Length); $Claim.Flush($true)
  if (Test-Path -LiteralPath ($Dest + '.xdev-part')) {{ Emit @{{ok=$false;reason='LEGACY_PARTIAL_REQUIRES_ORIGINAL_RECOVERY';dest=$Dest}}; exit 3 }}
  if ((Test-Path -LiteralPath $Dest) -and -not {'$true' if overwrite else '$false'}) {{ Emit @{{ok=$false;reason='DEST_EXISTS';dest=$Dest}}; exit 3 }}
  $Tmp = $Dest + '.xdev-part-{transfer_id}'
  Invoke-WebRequest -UseBasicParsing -Uri ($MacServer + '/file/{staged}') -OutFile $Tmp -TimeoutSec 600
  $h = (Get-FileHash -LiteralPath $Tmp -Algorithm SHA256).Hash.ToLower()
  if ($h -ne '{sha}') {{ Emit @{{ok=$false;reason='HASH_MISMATCH';got=$h;temporary=$Tmp}}; exit 4 }}
  if (Test-Path -LiteralPath $Dest) {{
    if (-not {'$true' if overwrite else '$false'}) {{ Emit @{{ok=$false;reason='DEST_APPEARED';dest=$Dest;temporary=$Tmp}}; exit 3 }}
    [IO.File]::Move($Dest, $Dest + '.xdev-old-{transfer_id}')
  }}
  [IO.File]::Move($Tmp, $Dest)
  Emit @{{ok=$true;dest=$Dest;sha256=$h;bytes=(Get-Item -LiteralPath $Dest).Length{binding}}}
}} finally {{
  $Claim.Dispose()
  if ((Test-Path -LiteralPath $ClaimPath) -and [IO.File]::ReadAllText($ClaimPath) -eq '{transfer_id}') {{ Remove-Item -LiteralPath $ClaimPath -Force }}
}}
"""


def push(local: str, remote: str | None = None, overwrite: bool = False, receipt_callback=None) -> int:
    """Upload once, optionally publishing the original receipt before enqueue.

    The synchronous callback receives an independent dict of frozen input and
    receipt fields after INTENT is durable, before staging or calling run_ps.
    Callback failure aborts this attempt and propagates unchanged; no upload is
    submitted. The callback must persist its handle before returning. The int
    return and behavior without a callback remain unchanged.
    """
    src = Path(local).expanduser()
    data = src.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    dest = remote or rf'%LOCALAPPDATA%\xdev\inbox\{src.name}'
    tx = _Transfer('push', source=str(src), destination=dest, sha256=sha, bytes=len(data), overwrite=overwrite)
    if receipt_callback is not None:
        try:
            receipt_callback(dict(receipt=str(tx.root), transfer_id=tx.id,
                                  sha256=sha, bytes=len(data), destination=dest, source=str(src)))
        except BaseException:
            try:
                tx.finish('ABORTED_BEFORE_ENQUEUE', reason='RECEIPT_CALLBACK_FAILED')
            except Exception:
                # A full/unwritable disk must not hide the original callback
                # failure. INTENT is retained; staging/enqueue never occurred.
                pass
            raise
    staged = _push_staged(dict(source=str(src), sha256=sha, transfer_id=tx.id))
    with (STATE / 'files' / staged).open('xb') as stream:
        stream.write(data)
    script = _push_script(dest, sha, tx.id, staged, overwrite)
    rc, out, err, job = tx.call('push', script, timeout=max(120, len(data) // 200_000))
    rows = emitted(out)
    if (rc == 0 and len(rows) == 1 and isinstance(rows[0], dict) and rows[0].get('ok') is True
            and rows[0].get('sha256') == sha and type(rows[0].get('bytes')) is int
            and rows[0]['bytes'] == len(data) and rows[0].get('requested_dest') == dest
            and rows[0].get('transfer_id') == tx.id and isinstance(rows[0].get('dest'), str)
            and PureWindowsPath(rows[0]['dest']).is_absolute()
            and ('%' in dest or PureWindowsPath(rows[0]['dest']) == PureWindowsPath(dest))):
        tx.finish('COMPLETE', job_id=job, remote=rows[0], staged=staged)
        (STATE / 'files' / staged).unlink()
        print(f"XDEV_PUSHED {rows[0]['dest']} sha256={sha} bytes={len(data)} job={job}")
        return 0
    tx.finish('UNKNOWN' if rc == 124 else 'FAILED', job_id=job, staged=staged)
    return fail(f'XDEV_PUSH_FAILED rc={rc} job={job} receipt={tx.root} {rows[-1] if rows else (out + err)[-800:]}', rc or 1)


def _regular_json(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('REGULAR_ORIGINAL_RECEIPT_REQUIRED')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('ORIGINAL_RECEIPT_OBJECT_REQUIRED')
    return value


def _push_recovery_context(transfer):
    """Require the original generated script and signed queue, never an ID guess."""
    base = (STATE / 'transfers').resolve()
    value = str(transfer)
    root = base / value if re.fullmatch('[0-9a-f]{32}', value) else Path(value).expanduser()
    if root.name in ('INTENT.json', 'OUTCOME.json'):
        root = root.parent
    if (not root.is_absolute() or root.is_symlink() or root.resolve().parent != base
            or re.fullmatch('[0-9a-f]{32}', root.name) is None):
        raise ValueError('ORIGINAL_TRANSFER_PATH_REQUIRED')
    root = root.resolve()
    intent = _regular_json(root / 'INTENT.json')
    if (intent.get('operation') != 'push' or intent.get('transfer_id') != root.name
            or type(intent.get('bytes')) is not int or intent['bytes'] < 0
            or type(intent.get('overwrite')) is not bool
            or not isinstance(intent.get('sha256'), str)
            or re.fullmatch('[0-9a-f]{64}', intent['sha256']) is None
            or not isinstance(intent.get('destination'), str)
            or not isinstance(intent.get('source'), str)):
        raise ValueError('ORIGINAL_PUSH_INTENT_INVALID')
    step = root / '0001-push'
    if [p.name for p in root.iterdir() if p.is_dir()] != ['0001-push'] or step.is_symlink():
        raise ValueError('ORIGINAL_SINGLE_PUSH_STEP_REQUIRED')
    request_intent = _regular_json(step / 'INTENT.json')
    returned = _regular_json(step / 'RETURN.json')
    outcome_path = root / 'OUTCOME.json'
    outcome = _regular_json(outcome_path) if outcome_path.exists() else None
    script_path = step / 'request.ps1'
    if script_path.is_symlink():
        raise ValueError('ORIGINAL_SCRIPT_CHANGED')
    script = script_path.read_bytes()
    staged = _push_staged(intent)
    current = _push_script(intent['destination'], intent['sha256'], root.name, staged, intent['overwrite']).encode()
    legacy = _push_script(intent['destination'], intent['sha256'], root.name, staged, intent['overwrite'], False).encode()
    if (script not in (current, legacy)
            or request_intent.get('script_sha256') != hashlib.sha256(script).hexdigest()
            or request_intent.get('no_automatic_retry') is not True):
        raise ValueError('ORIGINAL_SCRIPT_CHANGED')
    job = returned.get('job_id')
    if type(job) is not int or job <= 0:
        raise ValueError('ORIGINAL_QUEUE_ID_UNAVAILABLE')
    if (returned.get('status') not in ('UNKNOWN', 'RETURNED')
            or type(returned.get('rc')) is not int
            or (returned['status'] == 'UNKNOWN' and returned['rc'] != 124)
            or (outcome is not None and outcome.get('status') not in ('UNKNOWN', 'FAILED', 'COMPLETE'))):
        raise ValueError('ORIGINAL_PUSH_RETURN_INVALID')
    if (Path(returned.get('original_result', '')).resolve() != (STATE / 'results' / f'{job}.json').resolve()
            or (outcome is not None and outcome.get('job_id') != job)
            or (outcome is not None and outcome.get('staged') != staged)):
        raise ValueError('ORIGINAL_PUSH_BINDING_CHANGED')
    import xdev_core as core
    reader = getattr(core.wbagent, 'read_result', None)
    require_request = getattr(core.wbagent, '_require_request', None)
    if not callable(reader) or not callable(require_request):
        raise ValueError('VALIDATED_ORIGINAL_RESULT_READER_REQUIRED')
    if Path(core.wbagent.ROOT).resolve() != STATE.resolve():
        raise ValueError('ORIGINAL_QUEUE_ROOT_CHANGED')
    require_request(job)  # Existing queue validates the original command signature.
    queue_path = STATE / 'queue' / f'{job}.json'
    queued = _regular_json(queue_path)
    body = base64.b64decode(queued['script'], validate=True)
    if (queued.get('id') != job or queued.get('timeout') != request_intent.get('options', {}).get('timeout')
            or not isinstance(queued.get('mac'), str)
            or not hmac.compare_digest(core.wbagent.sign('cmd', job, queued['timeout'], queued['script']), queued['mac'])
            or body != (core.PREAMBLE + script.decode('utf-8')).encode('utf-8')):
        raise ValueError('ORIGINAL_QUEUE_SCRIPT_MISMATCH')
    result = reader(job)
    return root, intent, returned, outcome, staged, result, script == current


def _push_result_payload(intent, transfer_id, result, bound):
    if type(result.get('rc')) is not int:
        raise ValueError('ORIGINAL_RESULT_RC_INVALID')
    out = base64.b64decode(result['out'], validate=True).decode('utf-8', 'replace').replace('\r\n', '\n')
    err = base64.b64decode(result['err'], validate=True).decode('utf-8', 'replace').replace('\r\n', '\n')
    if result['rc'] != 0:
        return None, out, err
    rows = emitted(out)
    if len(rows) != 1 or not isinstance(rows[0], dict) or rows[0].get('ok') is not True:
        raise ValueError('ORIGINAL_PUSH_SUCCESS_PAYLOAD_MISSING')
    row = rows[0]
    if (row.get('sha256') != intent['sha256'] or type(row.get('bytes')) is not int
            or row['bytes'] != intent['bytes'] or not isinstance(row.get('dest'), str)
            or not PureWindowsPath(row['dest']).is_absolute()):
        raise ValueError('ORIGINAL_PUSH_HASH_SIZE_OR_DEST_CHANGED')
    requested = intent['destination']
    if bound and (row.get('requested_dest') != requested or row.get('transfer_id') != transfer_id):
        raise ValueError('ORIGINAL_PUSH_DESTINATION_BINDING_CHANGED')
    # Old replies cannot attest how an environment-variable destination expanded.
    if '%' in requested:
        if not bound:
            raise ValueError('LEGACY_DESTINATION_EXPANSION_UNATTESTED')
    elif not PureWindowsPath(requested).is_absolute() or PureWindowsPath(requested) != PureWindowsPath(row['dest']):
        raise ValueError('ORIGINAL_PUSH_DESTINATION_CHANGED')
    return row, out, err


def _remove_recovered_push_stage(stage, intent):
    """Remove only this completed transfer's unchanged, regular staged file.

    Another verified reconciliation may have removed it at any path lookup.
    Keep descriptor identity through the read, then recheck the pathname before
    unlink. The final check/unlink is not an atomic defense against an arbitrary
    outside writer; staging remains a private transfer-owned namespace.
    """
    changed = 'OWNED_STAGED_FILE_CHANGED'
    try:
        try:
            before = os.lstat(stage)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(changed)
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
        try:
            fd = os.open(stage, flags)
        except FileNotFoundError:
            return
        try:
            opened = os.fstat(fd)
            identity = (opened.st_dev, opened.st_ino)
            if (not stat.S_ISREG(opened.st_mode) or identity != (before.st_dev, before.st_ino)
                    or opened.st_size != intent['bytes']):
                raise ValueError(changed)
            digest = hashlib.sha256()
            size = 0
            while size <= intent['bytes']:
                data = os.read(fd, min(CHUNK, intent['bytes'] + 1 - size))
                if not data:
                    break
                digest.update(data)
                size += len(data)
            if size != intent['bytes'] or digest.hexdigest() != intent['sha256']:
                raise ValueError(changed)
        finally:
            os.close(fd)
        try:
            current = os.lstat(stage)
        except FileNotFoundError:
            return
        if (not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != identity
                or (current.st_size, current.st_mtime_ns, current.st_ctime_ns)
                != (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)):
            raise ValueError(changed)
        try:
            stage.unlink()
        except FileNotFoundError:
            return
    except OSError as error:
        raise ValueError(changed) from error


def recover_push(transfer) -> dict:
    """Reconcile a recorded push using only its original validated queue result.

    No enqueue, remote probe, upload or source-file reread occurs. Publication
    proves the original upload result, not that another task never changed the
    remote destination afterward. Caller must retain normal pre-execution input
    hash checks. Local staged cleanup is limited to this transfer after SUCCESS.
    """
    try:
        root, intent, returned, outcome, staged, result, bound = _push_recovery_context(transfer)
        job = returned['job_id']
        if result is None:
            return dict(ok=False, status='UNKNOWN', job_id=job, receipt=str(root), resubmitted=False)
        if result.get('id') != job:
            raise ValueError('ORIGINAL_RESULT_ID_CHANGED')
        row, out, err = _push_result_payload(intent, root.name, result, bound)
        # A prior complete return is immutable; late UNKNOWN returns deliberately
        # carried empty output, so only RETURNED output hashes are comparable.
        if returned.get('status') == 'RETURNED':
            if returned.get('rc') != result['rc']:
                raise ValueError('ORIGINAL_RESULT_CHANGED')
            for label, text in (('stdout', out), ('stderr', err)):
                if (returned.get(label + '_bytes') != len(text.encode())
                        or returned.get(label + '_sha256') != hashlib.sha256(text.encode()).hexdigest()):
                    raise ValueError('ORIGINAL_RESULT_CHANGED')
        original = STATE / 'results' / f'{job}.json'
        raw = original.read_bytes()
        if original.is_symlink() or json.loads(raw) != result:
            raise ValueError('ORIGINAL_RESULT_CHANGED_DURING_RECOVERY')
        proof = dict(schema='xdev-push-recovery-v1', transfer_id=root.name, job_id=job,
                     status='COMPLETE' if row else 'FAILED', ok=row is not None,
                     original_result=dict(path=str(original), bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()),
                     original_outcome=outcome.get('status') if outcome else None,
                     remote=row, rc=result['rc'], resubmitted=False, remote_operations=0)
        recovery = root / 'RECOVERY.json'
        if recovery.exists():
            if _regular_json(recovery) != proof:
                raise ValueError('ORIGINAL_RECOVERY_PROOF_CHANGED')
        else:
            temporary = root / ('.recovery-' + uuid.uuid4().hex + '.tmp')
            try:
                _write_new(temporary, proof)
                try:
                    os.link(temporary, recovery)
                except FileExistsError:
                    if _regular_json(recovery) != proof:
                        raise ValueError('ORIGINAL_RECOVERY_PROOF_CHANGED')
            finally:
                temporary.unlink()
        if row is not None:
            _remove_recovered_push_stage(STATE / 'files' / staged, intent)
        return dict(proof, receipt=str(root), recovery_receipt=str(recovery))
    except (OSError, ValueError, KeyError, TypeError) as error:
        return dict(ok=False, status='REJECTED', reason=str(error), resubmitted=False, remote_operations=0)


def _chunk_script(remote: str, offset: int, with_stat: bool) -> str:
    digest = "$h = (Get-FileHash -LiteralPath $P -Algorithm SHA256).Hash.ToLower()\n" if with_stat else "$h = ''\n"
    return f"""$P = Expand {ps_quote(remote)}
if (-not (Test-Path -LiteralPath $P -PathType Leaf)) {{ Emit @{{ok=$false;reason='NOT_FOUND';path=$P}}; exit 3 }}
{digest}$fs = New-Object IO.FileStream($P, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
try {{
  $size = $fs.Length; [void]$fs.Seek({offset}, [IO.SeekOrigin]::Begin)
  $buf = New-Object byte[] {CHUNK}; $n = $fs.Read($buf, 0, {CHUNK})
}} finally {{ $fs.Dispose() }}
Emit @{{ok=$true;size=$size;sha256=$h;offset={offset};n=$n;b64=[Convert]::ToBase64String($buf,0,$n)}}
"""


def pull(remote: str, local: str, overwrite: bool = False, quiet: bool = False) -> int:
    dst = _destination(local)
    if dst.exists() and not overwrite:
        return fail(f'XDEV_PULL_REFUSED local exists: {dst}', 3)
    if dst.with_name(dst.name + '.xdev-part').exists():
        return fail(f'XDEV_PULL_REFUSED legacy partial requires original recovery: {dst}', 3)
    tx = _Transfer('pull', source=remote, destination=str(dst), overwrite=overwrite)
    try:
        claim = _claim(dst, tx)
    except FileExistsError:
        tx.finish('REFUSED_TARGET_BUSY')
        return fail(f'XDEV_PULL_REFUSED target busy: {dst}', 3)
    settled = False
    part = dst.with_name(dst.name + '.xdev-part-' + tx.id)
    offset, size, want = 0, None, None
    try:
        # Repeat the publication check after acquiring the target claim.
        if dst.exists() and not overwrite:
            settled = True; tx.finish('REFUSED_DEST_EXISTS')
            return fail(f'XDEV_PULL_REFUSED local exists: {dst}', 3)
        with part.open('xb') as stream:
            while size is None or offset < size:
                rc, out, err, job = tx.call('chunk', _chunk_script(remote, offset, size is None), timeout=300)
                rows = emitted(out)
                if rc != 0 or len(rows) != 1 or rows[0].get('ok') is not True:
                    settled = rc != 124
                    tx.finish('FAILED' if settled else 'UNKNOWN', job_id=job, part=str(part), offset=offset)
                    return fail(f'XDEV_PULL_FAILED rc={rc} job={job} receipt={tx.root} {(out + err)[-800:]}', rc or 1)
                row = rows[0]
                try:
                    block = base64.b64decode(row['b64'], validate=True)
                    valid = (type(row['size']) is int and row['size'] >= 0
                             and type(row['offset']) is int and row['offset'] == offset
                             and type(row['n']) is int and 0 <= row['n'] <= CHUNK
                             and len(block) == row['n'] and offset + row['n'] <= row['size']
                             and (row['n'] > 0 or row['size'] == 0))
                    if size is None:
                        size, want = row['size'], row['sha256']
                        valid = valid and isinstance(want, str) and re.fullmatch('[0-9a-f]{64}', want) is not None
                    valid = valid and row['size'] == size
                except (KeyError, TypeError, ValueError):
                    valid = False
                if not valid:
                    settled = True; tx.finish('INVALID_CHUNK', job_id=job, part=str(part), offset=offset)
                    return fail(f'XDEV_PULL_INVALID_CHUNK job={job} receipt={tx.root}', 5)
                stream.write(block); stream.flush(); os.fsync(stream.fileno())
                offset += len(block)
        got = hashlib.sha256(part.read_bytes()).hexdigest()
        if got != want:
            settled = True; tx.finish('HASH_MISMATCH', part=str(part), wanted=want, got=got)
            return fail(f'XDEV_PULL_CHANGED want={want} got={got} receipt={tx.root}', 5)
        if dst.exists():
            if not overwrite:
                settled = True; tx.finish('REFUSED_DEST_APPEARED', part=str(part))
                return fail(f'XDEV_PULL_REFUSED local appeared: {dst}', 3)
            dst.rename(dst.with_name(dst.name + '.xdev-old-' + tx.id))
        # link is atomic and refuses an existing target, unlike rename on POSIX.
        os.link(part, dst)
        part.unlink()
        tx.finish('COMPLETE', sha256=got, bytes=size, destination=str(dst))
        settled = True
        if not quiet:
            print(f'XDEV_PULLED {dst} sha256={got} bytes={size}')
        return 0
    finally:
        if settled:
            _release(claim, tx)


def _zip_members(archive, root):
    members, seen = [], set()
    for info in archive.infolist():
        name = info.filename.replace('\\', '/')
        parts = PurePosixPath(name).parts
        if (not parts or name.startswith('/') or '..' in parts or ':' in parts[0]
                or stat.S_ISLNK(info.external_attr >> 16)):
            raise ValueError('UNSAFE_ZIP_ENTRY:' + info.filename)
        if name.endswith('/'):
            continue
        key = '/'.join(parts).casefold()
        target = root.joinpath(*parts)
        if key in seen or root not in target.resolve().parents:
            raise ValueError('DUPLICATE_OR_UNSAFE_ZIP_ENTRY:' + info.filename)
        seen.add(key); members.append((info, target))
    return members


def pull_dir(remote_dir: str, local_dir: str) -> int:
    dst = _destination(local_dir)
    if dst.exists() and (not dst.is_dir() or any(dst.iterdir())):
        return fail(f'XDEV_PULL_REFUSED local dir not empty: {dst}', 3)
    tx = _Transfer('pull-dir', source=remote_dir, destination=str(dst))
    try:
        claim = _claim(dst, tx)
    except FileExistsError:
        tx.finish('REFUSED_TARGET_BUSY')
        return fail(f'XDEV_PULL_REFUSED target busy: {dst}', 3)
    settled = False
    lzip = dst.parent / f'.xdev-pull-{tx.id}.zip'
    unpack = dst.parent / f'.{dst.name}.xdev-unpack-{tx.id}'
    try:
        zscript = f"""Add-Type -AssemblyName System.IO.Compression.FileSystem
$D = Expand {ps_quote(remote_dir)}
if (-not (Test-Path -LiteralPath $D -PathType Container)) {{ Emit @{{ok=$false;reason='NOT_FOUND';path=$D}}; exit 3 }}
$Z = Join-Path $XRoot 'outbox\\pull-{tx.id}.zip'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Z) | Out-Null
[IO.Compression.ZipFile]::CreateFromDirectory($D, $Z)
Emit @{{ok=$true;zip=$Z;files=@(Get-ChildItem -LiteralPath $D -Recurse -File).Count}}
"""
        rc, out, err, job = tx.call('zip', zscript, timeout=600)
        rows = emitted(out)
        if rc != 0 or len(rows) != 1 or rows[0].get('ok') is not True:
            settled = rc != 124
            tx.finish('FAILED' if settled else 'UNKNOWN', job_id=job, local_zip=str(lzip))
            return fail(f'XDEV_ZIP_FAILED rc={rc} job={job} receipt={tx.root} {(out + err)[-800:]}', rc or 1)
        rzip, nfiles = rows[0]['zip'], rows[0]['files']
        if not isinstance(rzip, str) or type(nfiles) is not int or nfiles < 0:
            settled = True; tx.finish('INVALID_ZIP_RECEIPT', job_id=job)
            return fail(f'XDEV_ZIP_FAILED invalid receipt job={job}', 5)
        _write_new(tx.root / 'ARCHIVE.json', dict(remote_zip=rzip, local_zip=str(lzip), files=nfiles, job_id=job))
        rc = pull(rzip, str(lzip), quiet=True)
        if rc:
            settled = rc != 124
            tx.finish('PULL_FAILED' if settled else 'UNKNOWN', remote_zip=rzip, local_zip=str(lzip), rc=rc)
            return rc
        unpack.mkdir(mode=0o700)
        try:
            with zipfile.ZipFile(lzip) as archive:
                members = _zip_members(archive, unpack)
                if len(members) != nfiles:
                    raise ValueError('ZIP_FILE_COUNT_MISMATCH')
                for info, target in members:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, target.open('xb') as output:
                        shutil.copyfileobj(source, output)
        except (ValueError, zipfile.BadZipFile, OSError) as error:
            settled = True; tx.finish('ARCHIVE_INVALID', error=str(error), remote_zip=rzip,
                                      local_zip=str(lzip), unpack=str(unpack))
            return fail(f'XDEV_UNSAFE_OR_INVALID_ZIP {error} receipt={tx.root}', 5)
        if dst.exists():
            if not dst.is_dir() or any(dst.iterdir()):
                settled = True; tx.finish('REFUSED_DEST_APPEARED', unpack=str(unpack))
                return fail(f'XDEV_PULL_REFUSED local dir changed: {dst}', 3)
            dst.rmdir()
        unpack.rename(dst)
        root = dst.resolve()
        got = sorted(p for p in root.rglob('*') if p.is_file())
        evidence = [dict(path=str(p.relative_to(root)), bytes=p.stat().st_size,
                         sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in got]
        _write_new(tx.root / 'MEMBERS.json', evidence)
        # Only a fully verified local archive can authorize cleanup of our exact remote ZIP.
        crc, _, cerr, cjob = tx.call('cleanup', f'Remove-Item -LiteralPath {ps_quote(rzip)} -Force', timeout=60, quiet=True)
        if crc == 0:
            lzip.unlink()
        tx.finish('COMPLETE' if crc == 0 else 'COMPLETE_CLEANUP_PENDING', destination=str(root),
                  remote_zip=rzip, local_zip=str(lzip), cleanup_rc=crc, cleanup_job_id=cjob)
        settled = True
        for item in evidence:
            print(f"  {item['sha256']}  {item['path']}")
        print(f'XDEV_PULLED_DIR {root} files={len(got)} remote_files={nfiles}')
        if crc:
            fail(f'XDEV_CLEANUP_PENDING job={cjob} receipt={tx.root} {cerr[-400:]}', crc)
        return 0
    finally:
        if settled:
            _release(claim, tx)
