"""Windows 长任务：先原子保留名字，再上传/启动；未知原次不得重提。"""
import base64
import contextlib
import hashlib
import json
import math
import os
import re
import sys
import time
import uuid
from pathlib import Path, PureWindowsPath

NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,80}")
DEVICE_RE = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", re.I)
ACTIVE_STATES = {"RUNNING", "STARTING", "RESERVED"}


def _state() -> Path:
    from xdev_core import STATE
    return Path(STATE).resolve()


def _preamble() -> str:
    from xdev_core import PREAMBLE
    return PREAMBLE


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_new(path: Path, value) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _read_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("ORIGINAL_REGULAR_RECEIPT_REQUIRED")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("ORIGINAL_RECEIPT_OBJECT_REQUIRED")
    return value


def _task_root(name: str) -> Path:
    return _state() / "task-attempts" / name.casefold()


@contextlib.contextmanager
def _task_lock(root: Path):
    # 同原任务独占；不删除锁、不把任务锁扩大为主机计算锁。
    if root.is_symlink():
        raise ValueError("TASK_JOURNAL_SYMLINK_REFUSED")
    with (root / "operation.lock").open("a+b") as stream:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"0"); stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise BlockingIOError("TASK_BUSY") from None
            try:
                yield
            finally:
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise BlockingIOError("TASK_BUSY") from None
            yield


def _input_digest(path: Path) -> tuple[int, str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("REGULAR_INPUT_REQUIRED")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk); digest.update(chunk)
    return size, digest.hexdigest()


def _check_local_inputs(intent: dict) -> None:
    for item in intent["inputs"]:
        if _input_digest(Path(item["source"])) != (item["bytes"], item["sha256"]):
            raise ValueError("INPUT_CHANGED: " + item["source"])


def _input_guard(inputs: list) -> str:
    # 上传证明只证明当时字节；启动前及模型命令前再次核远端原输入。
    lines = ["# XDEV_TASK_INPUT_HASH_GUARD"]
    for item in inputs:
        lines += [f"$inputPath = Expand {ps_quote(item['destination'])}",
                  "if (-not (Test-Path -LiteralPath $inputPath -PathType Leaf)) { throw 'XDEV_INPUT_MISSING' }",
                  f"if ((Get-Item -LiteralPath $inputPath).Length -ne {item['bytes']}) {{ throw 'XDEV_INPUT_SIZE_CHANGED' }}",
                  f"if ((Get-FileHash -LiteralPath $inputPath -Algorithm SHA256).Hash.ToLower() -cne {ps_quote(item['sha256'])}) {{ throw 'XDEV_INPUT_HASH_CHANGED' }}"]
    return "\n".join(lines) + "\n"


def recover_push(*args, **kwargs):
    from xdev_files import recover_push as recover
    return recover(*args, **kwargs)


# CLI 帮助只需 parser，不能因导入而读取真实配置或队列。
def _win_root() -> str:
    from xdev_core import WIN_ROOT
    return WIN_ROOT


def ps_quote(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def emitted(out: str) -> list:
    from xdev_core import emitted as parse
    return parse(out)


def run_ps(*args, **kwargs):
    from xdev_core import run_ps as execute
    return execute(*args, **kwargs)


def push(*args, **kwargs):
    from xdev_files import push as transfer
    return transfer(*args, **kwargs)


def pull_dir(*args, **kwargs):
    from xdev_files import pull_dir as transfer
    return transfer(*args, **kwargs)


def fail(message, rc=1):
    print(message, file=sys.stderr)
    return rc


def _valid_name(name: str) -> bool:
    # Windows 会折叠尾随句点及设备名，不能让别名指向其他任务。
    return (isinstance(name, str) and NAME_RE.fullmatch(name) is not None
            and name not in (".", "..") and not name.endswith(".")
            and DEVICE_RE.fullmatch(name) is None)


def _work_dir(name: str) -> str:
    return _win_root().rstrip("\\/") + "\\work\\" + name


def _bad_name(name) -> dict:
    return {"ok": False, "stage": "validate", "state": "REFUSED", "name": name,
            "reason": "BAD_NAME: use 1-80 letters, digits, . _ -; no Windows aliases", "rc": 2}


def _remote_outcome(rc, out, err, job_id, name, stage) -> dict:
    try:
        rows = emitted(out)
        row = rows[0] if len(rows) == 1 and isinstance(rows[0], dict) else None
    except (ValueError, TypeError):
        row = None
    if rc == 124 or not row or (rc != 0 and row.get("ok")):
        return {"ok": False, "name": name, "stage": stage, "state": "UNKNOWN",
                "rc": rc or 7, "job_id": job_id, "reason": (out + err)[-800:] or "NO_CONFIRMED_RECEIPT",
                "hint": f"xdev result {job_id}; xdev task-status {name}; do not resubmit"}
    result = dict(row, name=name, stage=stage, job_id=job_id, transport_rc=rc)
    # status 的 rc 属于引擎，其他阶段的 rc 属于该操作。
    if stage != "status" or not row.get("ok"):
        result["rc"] = rc or (0 if row.get("ok") else 7)
    return result


def _remote(script: str, name: str, stage: str, wait_seconds: float = 45,
            request_path: Path | None = None) -> dict:
    """原次先落意图，返回后绑定队列 ID；缺失 ID 不扫描队列猜测。"""
    if request_path is not None:
        request_path.mkdir(exist_ok=False)
        _write_new(request_path / "INTENT.json", dict(
            stage=stage, name=name, script=script, timeout=60,
            script_sha256=_sha(script.encode()), full_script_sha256=_sha((_preamble() + script).encode())))
    try:
        rc, out, err, job_id = run_ps(script, timeout=60, wait=wait_seconds)
    except (OSError, ValueError) as exc:
        return {"ok": False, "name": name, "stage": stage, "state": "UNKNOWN",
                "rc": 7, "reason": str(exc), "hint": f"xdev task-status {name}; do not resubmit"}
    result = _remote_outcome(rc, out, err, job_id, name, stage)
    if request_path is not None:
        _write_new(request_path / "RETURN.json", dict(job_id=job_id, rc=rc,
            stdout_sha256=_sha(out.encode()), stderr_sha256=_sha(err.encode()),
            request_sha256=_sha((request_path / "INTENT.json").read_bytes()), outcome=result))
    return result


def _read_original_operation(request_path: Path, name: str, stage: str) -> dict:
    """只接原请求；必须同时匹配冻结脚本、原签名 queue 和原 result。"""
    from xdev_core import wbagent
    pending = dict(ok=False, name=name, stage=stage, state="UNKNOWN", rc=124,
                   reason="ORIGINAL_REQUEST_UNRESOLVED")
    if not (request_path / "RETURN.json").exists():
        return dict(pending, reason="ORIGINAL_QUEUE_ID_UNAVAILABLE")
    request = _read_json(request_path / "INTENT.json")
    returned = _read_json(request_path / "RETURN.json")
    script = request.get("script")
    if (request.get("name") != name or request.get("stage") != stage
            or not isinstance(script, str) or request.get("script_sha256") != _sha(script.encode())
            or returned.get("request_sha256") != _sha((request_path / "INTENT.json").read_bytes())
            or request.get("full_script_sha256") != _sha((_preamble() + script).encode())):
        raise ValueError("ORIGINAL_REQUEST_CHANGED")
    job_id = returned.get("job_id")
    if type(job_id) is not int or job_id <= 0:
        return dict(pending, reason="ORIGINAL_QUEUE_ID_UNAVAILABLE")
    pending.update(job_id=job_id, **{stage + "_job_id": job_id})
    if Path(wbagent.ROOT).resolve() != _state():
        raise ValueError("ORIGINAL_QUEUE_ROOT_CHANGED")
    wbagent._require_request(job_id)
    queued = _read_json(_state() / "queue" / f"{job_id}.json")
    body = base64.b64decode(queued["script"], validate=True)
    if (queued.get("id") != job_id or queued.get("timeout") != request["timeout"]
            or _sha(body) != request["full_script_sha256"]):
        raise ValueError("ORIGINAL_QUEUE_SCRIPT_CHANGED")
    raw = wbagent.read_result(job_id)
    if raw is None:
        return pending
    result_path = _state() / "results" / f"{job_id}.json"
    original_bytes = result_path.read_bytes()
    if result_path.is_symlink() or json.loads(original_bytes) != raw:
        raise ValueError("ORIGINAL_RESULT_CHANGED_DURING_RECOVERY")
    out = base64.b64decode(raw["out"], validate=True).decode("utf-8", "replace").replace("\r\n", "\n")
    err = base64.b64decode(raw["err"], validate=True).decode("utf-8", "replace").replace("\r\n", "\n")
    # 已同步返回的结果不可被迟到记录覆盖；124 的空输出不作为终态。
    if returned["rc"] != 124 and (returned["rc"] != raw["rc"]
            or returned["stdout_sha256"] != _sha(out.encode())
            or returned["stderr_sha256"] != _sha(err.encode())):
        raise ValueError("ORIGINAL_RESULT_CHANGED")
    result = _remote_outcome(raw["rc"], out, err, job_id, name, stage)
    result[stage + "_job_id"] = job_id
    proof = dict(result=result, original_result_sha256=_sha(original_bytes))
    proof_path = request_path / "RECONCILED.json"
    if proof_path.exists():
        if _read_json(proof_path) != proof:
            raise ValueError("ORIGINAL_RECONCILIATION_CHANGED")
    else:
        _write_new(proof_path, proof)
    return result


def _reserve(name: str, kind: str, owner: str | None = None,
             request_path: Path | None = None) -> dict:
    owner = owner or uuid.uuid4().hex
    script = f"""# XDEV_TASK_RESERVE
$Name = {ps_quote(name)}; $Owner = {ps_quote(owner)}
$J = Join-Path $XRoot {ps_quote('jobs' + chr(92) + name)}
$W = Join-Path $XRoot {ps_quote('work' + chr(92) + name)}
$Claims = Join-Path $XRoot 'task-claims'; $Claim = Join-Path $Claims ($Name + '.json')
# 旧命名任务及旧输入目录归原次，不补写、清理或替换。
if ((Test-Path -LiteralPath $J) -or (Test-Path -LiteralPath $W)) {{
  Emit @{{ ok = $false; state = 'REFUSED'; reason = 'NAME_EXISTS' }}; exit 3
}}
[IO.Directory]::CreateDirectory($Claims) | Out-Null
try {{
  # CreateNew 是跨进程原子竞争点；绝不能改成 Test-Path 加 Force。
  $claimStream = [IO.File]::Open($Claim, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
}} catch [IO.IOException] {{
  if (Test-Path -LiteralPath $Claim) {{ Emit @{{ ok = $false; state = 'REFUSED'; reason = 'NAME_EXISTS' }}; exit 3 }}
  throw
}}
try {{
  $bytes = [Text.Encoding]::UTF8.GetBytes((@{{ name = $Name; owner = $Owner; kind = {ps_quote(kind)} }} | ConvertTo-Json -Compress))
  $claimStream.Write($bytes, 0, $bytes.Length); $claimStream.Flush()
}} finally {{ $claimStream.Dispose() }}
# 再核遗留目录；失败也保留 claim，不释放可能已归属的名字。
if ((Test-Path -LiteralPath $J) -or (Test-Path -LiteralPath $W)) {{
  Emit @{{ ok = $false; state = 'REFUSED'; reason = 'NAME_EXISTS' }}; exit 3
}}
[IO.Directory]::CreateDirectory($J) | Out-Null
if ({ps_quote(kind)} -eq 'task') {{ [IO.Directory]::CreateDirectory($W) | Out-Null }}
$T = Join-Path $J 'temp'; [IO.Directory]::CreateDirectory($T) | Out-Null
$meta = @{{ name = $Name; owner = $Owner; kind = {ps_quote(kind)}; state = 'RESERVED'; remote_dir = $W; temp_dir = $T; at = [DateTime]::UtcNow.ToString('o') }}
[IO.File]::WriteAllText((Join-Path $J 'task.json'), ($meta | ConvertTo-Json -Compress), (New-Object Text.UTF8Encoding($false)))
Emit @{{ ok = $true; state = 'RESERVED'; owner = $Owner; dir = $J; remote_dir = $W }}
"""
    result = _remote(script, name, "reserve", request_path=request_path)
    result["reserve_job_id"] = result.get("job_id")
    if result.get("ok") and result.get("owner") != owner:
        result.update(ok=False, state="UNKNOWN", rc=7, reason="RESERVATION_OWNER_MISMATCH")
    return result


def _identity_script(role: str) -> str:
    return f"""$identityFile = Join-Path $PSScriptRoot {ps_quote(role + '.identity.json')}
$selfProcess = Get-Process -Id $PID -ErrorAction Stop
$identity = @{{ pid = $PID; birth = $selfProcess.StartTime.ToUniversalTime().ToString('o'); script_path = $PSCommandPath }}
[IO.File]::WriteAllText(($identityFile + '.part'), ($identity | ConvertTo-Json -Compress), (New-Object Text.UTF8Encoding($false)))
[IO.File]::Move(($identityFile + '.part'), $identityFile)
"""


def _body_script(body: str, wd: str | None = None) -> str:
    location = f"Set-Location -LiteralPath (Expand {ps_quote(wd)})\n" if wd else ""
    return ("[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)\n"
            "$ErrorActionPreference = 'Stop'\n"
            "function Expand([string]$p) { [Environment]::ExpandEnvironmentVariables($p) }\n"
            f"$XRoot = Expand {ps_quote(_win_root())}\n"
            "$env:TEMP = Join-Path $PSScriptRoot 'temp'; $env:TMP = $env:TEMP\n"
            + _identity_script("body") +
            "$global:LASTEXITCODE = 0\ntry {\n" + location + body + "\n"
            "$xdevCommandSucceeded = $?\n"
            "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
            "if (-not $xdevCommandSucceeded) { exit 1 }\nexit 0\n"
            "} catch { [Console]::Error.WriteLine([string]$_); exit 1 }\n")


RUNNER = "$ErrorActionPreference = 'Stop'\n" + _identity_script("runner") + r"""
$J = $PSScriptRoot; $Body = Join-Path $J 'body.ps1'
try {
  $meta = Get-Content -LiteralPath (Join-Path $J 'task.json') -Raw | ConvertFrom-Json
  $cwd = if ($meta.kind -eq 'task') { $meta.remote_dir } else { $J }
  $env:TEMP = $meta.temp_dir; $env:TMP = $meta.temp_dir
  $p = Start-Process -FilePath powershell.exe -ArgumentList ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $Body + '"') -WorkingDirectory $cwd -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $J 'out.log') -RedirectStandardError (Join-Path $J 'err.log')
  $null = $p.Handle; $p.WaitForExit()
  if ($null -eq $p.ExitCode) { throw 'MISSING_CHILD_EXIT_CODE' }
  $receipt = [string]$p.ExitCode + ' ' + [DateTime]::UtcNow.ToString('o')
  $tmp = Join-Path $J 'rc.txt.part'
  [IO.File]::WriteAllText($tmp, $receipt, (New-Object Text.UTF8Encoding($false)))
  [IO.File]::Move($tmp, (Join-Path $J 'rc.txt'))
} catch {
  # 子进程终止未被证实时不伪造退出码，不补跑。
  [IO.File]::WriteAllText((Join-Path $J 'runner-error.txt'), [string]$_)
  exit 1
}
"""


def _start(name: str, owner: str, body: str, request_path: Path | None = None,
           inputs: list | None = None, runner: str = RUNNER) -> dict:
    script = f"""# XDEV_TASK_START
$J = Join-Path $XRoot {ps_quote('jobs' + chr(92) + name)}
$meta = Get-Content -LiteralPath (Join-Path $J 'task.json') -Raw | ConvertFrom-Json
if ($meta.owner -cne {ps_quote(owner)}) {{ Emit @{{ ok = $false; state = 'REFUSED'; reason = 'OWNER_MISMATCH' }}; exit 3 }}
{_input_guard(inputs or [])}
try {{
  $startClaim = [IO.File]::Open((Join-Path $J 'start.claim'), [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
  $startClaim.Dispose()
}} catch [IO.IOException] {{
  Emit @{{ ok = $false; state = 'UNKNOWN'; reason = 'START_ALREADY_CLAIMED' }}; exit 7
}}
$Body = Join-Path $J 'body.ps1'; $Run = Join-Path $J 'runner.ps1'
[IO.File]::WriteAllText($Body, {ps_quote(body)}, (New-Object Text.UTF8Encoding($true)))
[IO.File]::WriteAllText($Run, {ps_quote(runner)}, (New-Object Text.UTF8Encoding($true)))
$env:TEMP = $meta.temp_dir; $env:TMP = $meta.temp_dir
$cwd = if ($meta.kind -eq 'task') {{ $meta.remote_dir }} else {{ $J }}
$p = Start-Process -FilePath powershell.exe -ArgumentList ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $Run + '"') -WorkingDirectory $cwd -WindowStyle Hidden -PassThru
Emit @{{ ok = $true; state = 'STARTING'; dir = $J; runner_pid = $p.Id; at = [DateTime]::UtcNow.ToString('o') }}
"""
    result = _remote(script, name, "start", request_path=request_path)
    result["start_job_id"] = result.get("job_id")
    return result


def detach(name: str, body: str) -> int:
    if not _valid_name(name):
        return fail(f"XDEV_BAD_NAME {_bad_name(name)['reason']}", 2)
    reservation = _reserve(name, "detach")
    if not reservation.get("ok"):
        return fail("XDEV_DETACH_FAILED " + json.dumps(reservation), reservation["rc"])
    result = _start(name, reservation["owner"], _body_script(body))
    if not result.get("ok"):
        return fail("XDEV_DETACH_FAILED " + json.dumps(result), result["rc"])
    print(f"XDEV_DETACHED {name} dir={result['dir']} pid={result['runner_pid']} job={result['job_id']}")
    return 0


def _status_binding_matches(result: dict, name: str, owner: str,
                            raw_dir: str, resolved_dir: str) -> bool:
    remote_dir = result.get("remote_dir")
    return (result.get("binding_verified") is True and result.get("owner") == owner
            and result.get("task_name") == name and result.get("kind") == "task"
            and result.get("legacy") is False and result.get("requested_remote_dir") == raw_dir
            and isinstance(remote_dir, str) and "%" not in remote_dir
            and PureWindowsPath(remote_dir).is_absolute()
            and PureWindowsPath(remote_dir) == PureWindowsPath(resolved_dir))


def status(name: str, nbytes: int = 4000, wait_seconds: float = 45, *,
           expected_owner: str | None = None, expected_remote_dir: str | None = None,
           expected_resolved_remote_dir: str | None = None) -> dict:
    if not _valid_name(name):
        return _bad_name(name)
    if type(nbytes) is not int or not 0 <= nbytes <= 1_000_000:
        return {"ok": False, "name": name, "state": "REFUSED", "reason": "BAD_TAIL_SIZE", "rc": 2}
    if type(wait_seconds) not in (int, float) or not math.isfinite(wait_seconds) or wait_seconds < 0:
        return {"ok": False, "name": name, "state": "REFUSED", "reason": "BAD_WAIT_LIMIT", "rc": 2}
    if wait_seconds == 0:
        return {"ok": False, "name": name, "state": "UNKNOWN", "reason": "WAIT_LIMIT", "rc": 124}
    bound = expected_owner is not None
    if (any(value is not None for value in (expected_owner, expected_remote_dir, expected_resolved_remote_dir))
            and (not isinstance(expected_owner, str) or re.fullmatch(r"[0-9a-f]{32}", expected_owner) is None
                 or expected_remote_dir != _work_dir(name) or not isinstance(expected_resolved_remote_dir, str)
                 or "%" in expected_resolved_remote_dir or not PureWindowsPath(expected_resolved_remote_dir).is_absolute())):
        return _task_error(name, "INVALID_EXPECTED_TASK_BINDING", 2, "status")
    binding_guard = ""
    if bound:
        binding_guard = f"""$requestedRemoteDir = {ps_quote(expected_remote_dir)}
$expectedResolvedDir = Expand $requestedRemoteDir
if (-not $meta -or $meta.owner -cne {ps_quote(expected_owner)} -or $meta.name -cne {ps_quote(name)} -or $meta.kind -cne 'task') {{
  Emit @{{ ok = $false; state = 'UNKNOWN'; reason = 'TASK_OWNER_BINDING_MISMATCH' }}; exit 7
}}
if ([IO.Path]::GetFullPath([string]$meta.remote_dir) -ine [IO.Path]::GetFullPath($expectedResolvedDir) -or
    [IO.Path]::GetFullPath([string]$meta.remote_dir) -ine [IO.Path]::GetFullPath({ps_quote(expected_resolved_remote_dir)})) {{
  Emit @{{ ok = $false; state = 'UNKNOWN'; reason = 'TASK_DIRECTORY_BINDING_MISMATCH' }}; exit 7
}}
$bindingVerified = $true
"""
    script = f"""# XDEV_TASK_STATUS
$J = Join-Path $XRoot {ps_quote('jobs' + chr(92) + name)}
if (-not (Test-Path -LiteralPath $J)) {{
  $claim = Join-Path (Join-Path $XRoot 'task-claims') {ps_quote(name + '.json')}
  if (Test-Path -LiteralPath $claim) {{ Emit @{{ ok = $false; state = 'UNKNOWN'; reason = 'RESERVATION_INCOMPLETE' }}; exit 7 }}
  Emit @{{ ok = $false; state = 'UNKNOWN'; reason = 'NO_SUCH_JOB' }}; exit 3
}}
function TailOf([string]$f) {{
  if (-not (Test-Path -LiteralPath $f)) {{ return '' }}
  $fs = New-Object IO.FileStream($f, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
  try {{ $n = [Math]::Min($fs.Length, {nbytes}); [void]$fs.Seek(-$n, [IO.SeekOrigin]::End); $b = New-Object byte[] $n; [void]$fs.Read($b, 0, $n) }} finally {{ $fs.Dispose() }}
  [Text.Encoding]::UTF8.GetString($b)
}}
$meta = $null; $mf = Join-Path $J 'task.json'
if (Test-Path -LiteralPath $mf) {{ $meta = Get-Content -LiteralPath $mf -Raw | ConvertFrom-Json }}
$bindingVerified = $false; $requestedRemoteDir = $null
{binding_guard}
function IdentityAlive([string]$role) {{
  $f = Join-Path $J ($role + '.identity.json')
  if (-not (Test-Path -LiteralPath $f)) {{ return $false }}
  $identity = Get-Content -LiteralPath $f -Raw | ConvertFrom-Json
  if ($identity.script_path -ine (Join-Path $J ($role + '.ps1'))) {{ return $false }}
  $process = Get-Process -Id ([int]$identity.pid) -ErrorAction SilentlyContinue
  if (-not $process -or $process.ProcessName -ine 'powershell') {{ return $false }}
  try {{ return $process.StartTime.ToUniversalTime().ToString('o') -ceq $identity.birth }}
  catch {{ return $false }}
}}
$rcf = Join-Path $J 'rc.txt'; $Run = Join-Path $J 'runner.ps1'
$state = 'UNKNOWN'; $rc = $null; $fin = $null; $verified = $false; $reason = $null
if (-not (Test-Path -LiteralPath $rcf)) {{
  $verified = (IdentityAlive 'runner') -or (IdentityAlive 'body')
  if ($verified) {{ $state = 'RUNNING' }}
  elseif ($meta -and -not (Test-Path -LiteralPath (Join-Path $J 'start.claim'))) {{ $state = 'RESERVED' }}
  elseif (-not $meta) {{ $state = 'UNKNOWN'; $reason = 'LEGACY_IDENTITY_UNAVAILABLE' }}
  else {{ Start-Sleep -Seconds 2; $state = 'LOST' }}
}}
if (Test-Path -LiteralPath $rcf) {{
  $parts = (Get-Content -LiteralPath $rcf -Raw).Trim() -split ' ', 2; $parsed = 0
  if ($parts.Count -ne 2 -or -not [int]::TryParse($parts[0], [ref]$parsed)) {{
    Emit @{{ ok = $false; state = 'UNKNOWN'; reason = 'INVALID_EXIT_RECEIPT' }}; exit 7
  }}
  $state = 'DONE'; $rc = $parsed; $fin = $parts[1]
}}
$legacy = $null -eq $meta
# 旧 task() 曾固定工作根；不迁移旧命名任务。
$wd = if ($legacy) {{ Expand {ps_quote('%LOCALAPPDATA%' + chr(92) + 'xdev' + chr(92) + 'work' + chr(92) + name)} }} else {{ $meta.remote_dir }}
$kind = if ($legacy) {{ 'legacy' }} else {{ $meta.kind }}
$observedOwner = if ($meta) {{ $meta.owner }} else {{ $null }}
$observedName = if ($meta) {{ $meta.name }} else {{ $null }}
Emit @{{ ok = $true; state = $state; rc = $rc; finished = $fin; remote_dir = $wd; kind = $kind; legacy = $legacy; owner = $observedOwner; task_name = $observedName; binding_verified = $bindingVerified; requested_remote_dir = $requestedRemoteDir; identity_verified = $verified; reason = $reason; out = (TailOf (Join-Path $J 'out.log')); err = (TailOf (Join-Path $J 'err.log')); runner_error = (TailOf (Join-Path $J 'runner-error.txt')) }}
"""
    result = _remote(script, name, "status", wait_seconds)
    if result.get("ok") and bound and not _status_binding_matches(
            result, name, expected_owner, expected_remote_dir, expected_resolved_remote_dir):
        result.update(ok=False, state="UNKNOWN", rc=7, reason="TASK_STATUS_BINDING_MISMATCH")
    if result.get("ok") and result.get("state") == "DONE":
        try:
            value = result.get("rc")
            if type(value) is not int and (not isinstance(value, str) or not re.fullmatch(r"-?\d+", value)):
                raise ValueError("missing exit code")
            result["rc"] = int(result["rc"])
        except (ValueError, TypeError):
            result.update(ok=False, state="UNKNOWN", rc=7, reason="INVALID_EXIT_RECEIPT")
    return result


def task_status(name: str, nbytes: int = 4000, wait_seconds: float = 45) -> dict:
    if not _valid_name(name):
        return _bad_name(name)
    if type(nbytes) is not int or not 0 <= nbytes <= 1_000_000:
        return _task_error(name, "BAD_TAIL_SIZE", 2, "status")
    if type(wait_seconds) not in (int, float) or not math.isfinite(wait_seconds) or wait_seconds < 0:
        return _task_error(name, "BAD_WAIT_LIMIT", 2, "status")
    root = _task_root(name)
    if not root.exists():
        return status(name, nbytes=nbytes, wait_seconds=wait_seconds)
    try:
        with _task_lock(root):
            return _continue_task(root, _load_task(root, name), allow_new=False,
                                  nbytes=nbytes, wait_seconds=wait_seconds)
    except BlockingIOError:
        return _task_error(name, "TASK_BUSY", 75, "status")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return _task_error(name, exc, stage="status")


def _print_tail(name: str, s: dict) -> None:
    print(s.get("out", "").rstrip())
    if s.get("err", "").strip():
        print("--- stderr ---\n" + s["err"].rstrip())
    print(f"XDEV_STATE {name} {s['state']} rc={s.get('rc')} finished={s.get('finished')}")


def tail(name: str) -> int:
    s = task_status(name)
    if not s.get("ok"):
        return fail(f"XDEV_TAIL_FAILED {s}", s.get("rc") or 3)
    _print_tail(name, s)
    return 7 if s["state"] in ("UNKNOWN", "LOST") else 0


def _wait_status(name: str, max_seconds: int = 3600) -> dict:
    # 退避只读原 name；等待上限不是引擎预算。
    if not _valid_name(name):
        return _bad_name(name)
    if type(max_seconds) not in (int, float) or not math.isfinite(max_seconds) or max_seconds < 0:
        return {"ok": False, "name": name, "state": "REFUSED", "reason": "BAD_WAIT_LIMIT", "rc": 2}
    end, gap = time.monotonic() + max_seconds, 3
    s = {"ok": False, "name": name, "state": "UNKNOWN", "rc": 124, "reason": "WAIT_LIMIT"}
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return dict(s, ok=False, stage="wait", rc=124)
        s = task_status(name, nbytes=400, wait_seconds=min(45, remaining))
        if not s.get("ok"):
            return dict(s, stage="wait", rc=s.get("rc") or 3)
        if s["state"] == "DONE":
            _print_tail(name, s)
            return s
        if s["state"] not in ACTIVE_STATES:
            _print_tail(name, s)
            return dict(s, ok=False, stage="wait", rc=7)
        if time.monotonic() + gap > end:
            print(f"XDEV_STILL_RUNNING {name} state={s['state']} (later: xdev wait {name})")
            return dict(s, ok=False, stage="wait", rc=124)
        print(f"  {time.strftime('%H:%M:%S')} {name} {s['state']}; last: {s.get('out', '').strip().splitlines()[-1:]}", file=sys.stderr)
        time.sleep(gap)
        gap = min(gap * 2, 60)


def wait(name: str, max_seconds: int = 3600) -> int:
    s = _wait_status(name, max_seconds)
    if not s.get("ok"):
        return fail(f"XDEV_WAIT_FAILED {s}", s.get("rc") or 3)
    return int(s["rc"])


def _task_error(name, reason, rc=7, stage="resume") -> dict:
    return dict(ok=False, name=name, stage=stage, state="UNKNOWN" if rc in (7, 124) else "REFUSED",
                rc=rc, reason=str(reason), hint=f"xdev task-status {name}; preserve the original attempt")


def _load_task(root: Path, name: str) -> dict:
    intent = _read_json(root / "INTENT.json")
    if (intent.get("schema") != "xdev-task-attempt-v1" or intent.get("name", "").casefold() != name.casefold()
            or intent.get("queue_root") != str(_state()) or intent.get("windows_root") != _win_root()
            or intent.get("remote_dir") != _work_dir(intent["name"])
            or re.fullmatch(r"[0-9a-f]{32}", intent.get("owner", "")) is None
            or not isinstance(intent.get("inputs"), list)):
        raise ValueError("ORIGINAL_TASK_BINDING_CHANGED")
    for file, key in (("BODY.ps1", "body_sha256"), ("RUNNER.ps1", "runner_sha256")):
        path = root / file
        if path.is_symlink() or _sha(path.read_bytes()) != intent.get(key):
            raise ValueError("ORIGINAL_TASK_CODE_CHANGED")
    names = set()
    for item in intent["inputs"]:
        if (not isinstance(item, dict) or not isinstance(item.get("source"), str)
                or not Path(item["source"]).is_absolute() or type(item.get("bytes")) is not int
                or item["bytes"] < 0 or re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", "")) is None
                or item.get("destination") != intent["remote_dir"] + "\\" + item.get("basename", "")):
            raise ValueError("ORIGINAL_TASK_INPUT_CHANGED")
        basename = item.get("basename", "")
        if (not basename or basename in (".", "..") or basename.endswith((".", " "))
                or re.search(r'[<>:"/\\|?*\x00-\x1f]', basename) or DEVICE_RE.fullmatch(basename)
                or basename.casefold() in names):
            raise ValueError("ORIGINAL_TASK_INPUT_BASENAME_CHANGED")
        names.add(basename.casefold())
    return intent


def _upload_result(intent, index, proof) -> dict:
    item = intent["inputs"][index]
    result = dict(ok=proof.get("ok") is True, name=intent["name"], stage="push",
                  state="RESERVED" if proof.get("ok") else ("FAILED" if proof.get("status") == "FAILED" else "UNKNOWN"),
                  rc=0 if proof.get("ok") else proof.get("rc", 124 if proof.get("status") == "UNKNOWN" else 7),
                  input=item["source"], transfer_id=proof.get("transfer_id"),
                  transfer_receipt=proof.get("receipt"), job_id=proof.get("job_id"),
                  reason=proof.get("reason"), no_resubmission=True)
    return result


def _input_recovery(root: Path, intent: dict, index: int) -> dict:
    step = root / f"input-{index:04d}"
    item = intent["inputs"][index]
    if not step.exists():
        return dict(ok=True, status="NOT_ATTEMPTED")
    if not (step / "TRANSFER.json").exists():
        return dict(ok=False, status="UNKNOWN", rc=7, reason="ORIGINAL_TRANSFER_HANDLE_UNAVAILABLE")
    handle = _read_json(step / "TRANSFER.json")
    if any(handle.get(k) != item[k] for k in ("destination", "sha256", "bytes")):
        raise ValueError("ORIGINAL_TRANSFER_INPUT_CHANGED")
    receipt = Path(handle["receipt"])
    if (receipt.is_symlink() or receipt.resolve().parent != (_state() / "transfers").resolve()
            or receipt.name != handle.get("transfer_id")):
        raise ValueError("ORIGINAL_TRANSFER_PATH_CHANGED")
    original = _read_json(receipt / "INTENT.json")
    if (original.get("operation") != "push" or original.get("transfer_id") != handle["transfer_id"]
            or Path(original.get("source", "")).absolute() != Path(item["source"])
            or any(original.get(k) != item[k] for k in ("destination", "sha256", "bytes"))):
        raise ValueError("ORIGINAL_TRANSFER_BINDING_CHANGED")
    proof = recover_push(str(receipt))
    if proof.get("ok") and (proof.get("status") != "COMPLETE"
            or proof.get("transfer_id") != handle["transfer_id"]
            or proof.get("remote", {}).get("sha256") != item["sha256"]
            or proof.get("remote", {}).get("bytes") != item["bytes"]):
        raise ValueError("ORIGINAL_TRANSFER_PROOF_CHANGED")
    return proof


def _upload_one(root: Path, intent: dict, index: int) -> dict:
    item = intent["inputs"][index]
    step = root / f"input-{index:04d}"
    step.mkdir(exist_ok=False)
    _write_new(step / "ATTEMPT.json", dict(input_index=index, input=item, no_automatic_retry=True))

    def record_handle(handle):
        # 由文件层在入队前回调；不扫描共享目录、不解析全局 stderr 猜测 ID。
        if not isinstance(handle, dict):
            raise ValueError("STRUCTURED_TRANSFER_HANDLE_REQUIRED")
        if any(handle.get(k) != item[k] for k in ("destination", "sha256", "bytes")):
            raise ValueError("INPUT_CHANGED_BEFORE_UPLOAD")
        receipt = Path(handle["receipt"])
        transfer_id = handle.get("transfer_id", "")
        if (re.fullmatch(r"[0-9a-f]{32}", transfer_id) is None or not receipt.is_absolute()
                or receipt.is_symlink() or receipt.resolve().parent != (_state() / "transfers").resolve()
                or receipt.name != transfer_id):
            raise ValueError("INVALID_TRANSFER_HANDLE")
        _write_new(step / "TRANSFER.json", {k: handle[k] for k in
            ("receipt", "transfer_id", "destination", "sha256", "bytes")})

    try:
        rc = push(item["source"], item["destination"], receipt_callback=record_handle)
    except (OSError, ValueError, TypeError) as exc:
        _write_new(step / "RETURN.json", dict(rc=7, exception=str(exc), no_automatic_retry=True))
        return _upload_result(intent, index, dict(ok=False, status="UNKNOWN", rc=7, reason=str(exc)))
    _write_new(step / "RETURN.json", dict(rc=rc, no_automatic_retry=True))
    if rc:
        handle = _read_json(step / "TRANSFER.json") if (step / "TRANSFER.json").exists() else {}
        return _upload_result(intent, index, dict(ok=False, status="UNKNOWN" if rc == 124 else "FAILED",
                              rc=rc, receipt=handle.get("receipt"), transfer_id=handle.get("transfer_id")))
    return _upload_result(intent, index, _input_recovery(root, intent, index))


def _validate_reservation(intent, result):
    if result.get("ok"):
        if result.get("state") != "RESERVED" or result.get("owner") != intent["owner"]:
            raise ValueError("ORIGINAL_RESERVATION_OWNER_CHANGED")
        actual = result.get("remote_dir")
        if (not isinstance(actual, str) or "%" in actual or not PureWindowsPath(actual).is_absolute()
                or ("%" not in intent["remote_dir"] and PureWindowsPath(actual) != PureWindowsPath(intent["remote_dir"]))):
            raise ValueError("ORIGINAL_RESERVATION_DIRECTORY_CHANGED")
    return result


def _continue_task(root: Path, intent: dict, *, allow_new: bool,
                   reservation: dict | None = None, nbytes=4000, wait_seconds=45) -> dict:
    name = intent["name"]
    reservation = _validate_reservation(intent, reservation if reservation is not None else
        _read_original_operation(root / "reserve", name, "reserve"))
    evidence = dict(name=name, owner=intent["owner"], remote_dir=intent["remote_dir"],
                    task_receipt=str(root), reserve_job_id=reservation.get("job_id"))
    if not reservation.get("ok"):
        return dict(reservation, **{k: v for k, v in evidence.items() if k not in reservation})
    # start 意图一旦存在，只观察原请求；不能因 RESERVED/LOST 再起一次。
    if (root / "start").exists():
        started = _read_original_operation(root / "start", name, "start")
        if not started.get("ok"):
            return dict(evidence, **{k: v for k, v in started.items() if k not in evidence})
        if started.get("state") != "STARTING" or type(started.get("runner_pid")) is not int:
            raise ValueError("ORIGINAL_START_RECEIPT_INVALID")
        return dict(status(name, nbytes, wait_seconds, expected_owner=intent["owner"],
                           expected_remote_dir=intent["remote_dir"],
                           expected_resolved_remote_dir=reservation["remote_dir"]), task_receipt=str(root),
                    reserve_job_id=reservation.get("job_id"), start_job_id=started.get("job_id"))
    if allow_new:
        _check_local_inputs(intent)
    pending = []
    for index, item in enumerate(intent["inputs"]):
        proof = _input_recovery(root, intent, index)
        if proof.get("status") == "NOT_ATTEMPTED":
            if not allow_new:
                pending.append(index)
                continue
            uploaded = _upload_one(root, intent, index)
            if not uploaded.get("ok"):
                return dict(evidence, **{k: v for k, v in uploaded.items() if k not in evidence})
        elif not proof.get("ok"):
            result = _upload_result(intent, index, proof)
            return dict(evidence, **{k: v for k, v in result.items() if k not in evidence})
    if not allow_new:
        return dict(evidence, ok=True, stage="status", state="RESERVED", rc=0,
                    upload_status="PENDING" if pending else "COMPLETE", pending_inputs=pending,
                    hint=f"xdev task-resume {name}; status never starts execution")
    _check_local_inputs(intent)
    result = _start(name, intent["owner"], (root / "BODY.ps1").read_text(encoding="utf-8"),
                    request_path=root / "start", inputs=intent["inputs"],
                    runner=(root / "RUNNER.ps1").read_text(encoding="utf-8"))
    result.update(evidence)
    result["hint"] = f"xdev task-status {name}; never resubmit after UNKNOWN"
    return result


def task_resume(name: str) -> dict:
    """显式接续原预约；只首次上传尚未尝试项，不重传/重启原 UNKNOWN。"""
    if not _valid_name(name):
        return _bad_name(name)
    root = _task_root(name)
    if not root.exists():
        return _task_error(name, "ORIGINAL_TASK_JOURNAL_REQUIRED", 3)
    try:
        with _task_lock(root):
            return _continue_task(root, _load_task(root, name), allow_new=True)
    except BlockingIOError:
        return _task_error(name, "TASK_BUSY", 75)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return _task_error(name, exc)


def task_submit(inputs: list, command: str, name: str | None = None) -> dict:
    """先固定本机原意图，再原子保留远端名字；每阶段只提交一次。"""
    name = name if name is not None else "t" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex
    if not _valid_name(name):
        return _bad_name(name)
    basenames, normalized_inputs = set(), []
    for raw in inputs:
        path = str(raw)
        basename = path.replace("\\", "/").rsplit("/", 1)[-1]
        if (not basename or basename in (".", "..") or basename.endswith((".", " "))
                or re.search(r'[<>:"|?*\x00-\x1f]', basename) or DEVICE_RE.fullmatch(basename)):
            return dict(_bad_name(name), reason="BAD_INPUT_BASENAME", input=path)
        if basename.casefold() in basenames:
            return dict(_bad_name(name), reason="DUPLICATE_INPUT_BASENAME", input=path)
        basenames.add(basename.casefold())
        normalized_inputs.append((path, basename))
    try:
        if not isinstance(command, str) or len(command.encode()) > 64 * 1024 or len(inputs) > 128:
            raise ValueError("TASK_INPUT_LIMIT")
        frozen = []
        for path, basename in normalized_inputs:
            source = Path(path).expanduser().absolute()
            size, sha = _input_digest(source)
            frozen.append(dict(source=str(source), basename=basename, destination=_work_dir(name) + "\\" + basename,
                               bytes=size, sha256=sha))
        root = _task_root(name)
        root.parent.mkdir(parents=True, exist_ok=True)
        try:
            root.mkdir(exist_ok=False)
        except FileExistsError:
            return _task_error(name, "NAME_EXISTS", 3, "reserve")
        with _task_lock(root):
            body = _body_script(_input_guard(frozen) + command, _work_dir(name))
            for filename, text in (("BODY.ps1", body), ("RUNNER.ps1", RUNNER)):
                with (root / filename).open("x", encoding="utf-8", newline="") as stream:
                    stream.write(text); stream.flush(); os.fsync(stream.fileno())
            intent = dict(schema="xdev-task-attempt-v1", name=name, owner=uuid.uuid4().hex,
                queue_root=str(_state()), windows_root=_win_root(), remote_dir=_work_dir(name), inputs=frozen,
                body_sha256=_sha(body.encode()), runner_sha256=_sha(RUNNER.encode()))
            _write_new(root / "INTENT.json", intent)
            reservation = _reserve(name, "task", owner=intent["owner"], request_path=root / "reserve")
            return _continue_task(root, intent, allow_new=True, reservation=reservation)
    except BlockingIOError:
        return _task_error(name, "TASK_BUSY", 75, "submit")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return _task_error(name, exc, 7, "submit")


def task_collect(name: str, out_dir: str) -> dict:
    """只有原收据证实 DONE 才收集；非零退出也收集并保留原 rc。"""
    s = task_status(name)
    if not s.get("ok") or s.get("state") != "DONE":
        return dict(s, ok=False, stage="collect", rc=(s.get("rc") or (124 if s.get("state") in ACTIVE_STATES else 7)),
                    reason=s.get("reason", "NOT_TERMINAL"), local_dir=out_dir)
    # 新 journal 的收集再次绑定原预约；不能仅信任可变 status.remote_dir。
    root = _task_root(name)
    if root.exists():
        try:
            with _task_lock(root):
                intent = _load_task(root, name)
                reservation = _validate_reservation(intent, _read_original_operation(
                    root / "reserve", intent["name"], "reserve"))
                if (not reservation.get("ok") or not _status_binding_matches(s, intent["name"],
                        intent["owner"], intent["remote_dir"], reservation["remote_dir"])):
                    return dict(s, ok=False, stage="collect", rc=7, reason="TASK_COLLECT_BINDING_MISMATCH", local_dir=out_dir)
        except BlockingIOError:
            return _task_error(name, "TASK_BUSY", 75, "collect")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return _task_error(name, exc, stage="collect")
    if s.get("kind") == "detach":
        return dict(s, ok=False, stage="collect", rc=3, reason="DETACH_HAS_NO_TASK_WORKDIR", local_dir=out_dir)
    wd = s.get("remote_dir")
    if not wd:
        return dict(s, ok=False, stage="collect", rc=7, reason="MISSING_WORKDIR", local_dir=out_dir)
    remote_rc = int(s["rc"])
    try:
        prc = pull_dir(wd, out_dir)
    except (OSError, ValueError) as exc:
        return dict(s, ok=False, stage="collect", rc=remote_rc, pull_rc=7, reason=str(exc), local_dir=out_dir)
    return {"ok": prc == 0, "stage": "collect", "state": "DONE", "name": name, "rc": remote_rc,
            "remote_dir": wd, "local_dir": out_dir, "pull_rc": prc, "status_job_id": s.get("job_id")}


def task(inputs: list, command: str, out_dir: str, name: str | None = None, max_seconds: int = 3600) -> dict:
    """原便利接口走安全提交路径；UNKNOWN/LOST 不收集、不重提。"""
    submitted = task_submit(inputs, command, name)
    if not submitted.get("ok"):
        return submitted
    name = submitted["name"]
    waited = _wait_status(name, max_seconds)
    if not waited.get("ok") or waited.get("state") != "DONE":
        result = dict(submitted)
        result.update(waited)
        result.update(ok=False, stage="wait", hint=f"xdev task-status {name}; xdev task-collect {name} --out {out_dir} after DONE")
        return result
    if waited["rc"] != 0:
        result = dict(submitted)
        result.update(waited)
        result.update(ok=False, stage="wait", hint=f"xdev task-collect {name} --out {out_dir} to explicitly collect failed execution")
        return result
    # 自动取回只接受原 DONE rc=0；非零引擎由显式 collect 取回。
    result = task_collect(name, out_dir)
    result["reserve_job_id"] = submitted.get("reserve_job_id")
    result["start_job_id"] = submitted.get("start_job_id")
    return result


def print_summary(summary: dict) -> int:
    print("XDEV_TASK " + json.dumps(summary, ensure_ascii=False))
    return int(summary.get("rc") or summary.get("pull_rc") or (0 if summary.get("ok") else 1))


def add_parser(sub) -> None:
    p = sub.add_parser("task-submit", help="reserve a name, upload inputs, start once; return without waiting")
    p.add_argument("file", nargs="?", help="local PowerShell body file")
    p.add_argument("-c", "--command", help="PowerShell body instead of a file")
    p.add_argument("--input", action="append", default=[])
    p.add_argument("--name", help="explicit unused name; default timestamp plus full UUID")
    p = sub.add_parser("task-status", help="observe the original task name; never resubmit")
    p.add_argument("name")
    p.add_argument("--bytes", type=int, default=4000, help="maximum tail bytes per stream")
    p.add_argument("--wait", type=float, default=45, help="local receipt wait limit in seconds; never resubmit on timeout")
    p = sub.add_parser("task-resume", help="resume the original reservation and uploads; never replay unknown requests")
    p.add_argument("name")
    p = sub.add_parser("task-collect", help="collect a DONE task directory, retaining its exit code")
    p.add_argument("name")
    p.add_argument("--out", required=True)


def run(args) -> int:
    if args.cmd == "task-submit":
        if bool(args.file) == bool(args.command):
            return fail("XDEV_TASK_REFUSED task-submit needs exactly one file or -c/--command", 2)
        try:
            body = Path(args.file).expanduser().read_text(encoding="utf-8").removeprefix("\ufeff") if args.file else args.command
        except OSError as exc:
            return fail(f"XDEV_TASK_REFUSED {exc}", 2)
        return print_summary(task_submit(args.input, body, args.name))
    if args.cmd == "task-status":
        s = task_status(args.name, args.bytes, args.wait)
        if s.get("ok") and s.get("state") in ("UNKNOWN", "LOST"):
            s = dict(s, ok=False, rc=7)
        return print_summary(s)
    if args.cmd == "task-collect":
        return print_summary(task_collect(args.name, args.out))
    if args.cmd == "task-resume":
        return print_summary(task_resume(args.name))
    return fail(f"XDEV_TASK_REFUSED unknown command: {args.cmd}", 2)
