"""经现有 xdev 队列连接 Windows ClaudeR；科学代码仍由 RStudio 执行。"""

import base64
import contextlib
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import uuid


def add_parser(sub):
    parser = sub.add_parser("rstudio", help="bind Windows ClaudeR, execute R, submit once and poll the same async job")
    parser.add_argument("action", choices=("bind", "execute", "submit", "poll", "result"))
    parser.add_argument("--state", required=True, help="durable local state directory for one Windows RStudio identity")
    parser.add_argument("--session-name")
    parser.add_argument("--port", type=int)
    parser.add_argument("--r-pid", type=int)
    parser.add_argument("--rstudio-pid", type=int)
    parser.add_argument("--request-id")
    parser.add_argument("--job-id")
    parser.add_argument("--code-file")
    parser.add_argument("--agent-id", default="xdev-rstudio-agent")
    parser.add_argument("--http-timeout", type=int, default=25)
    parser.add_argument("--wait", type=int, default=55)


def name(value, label):
    if not value or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ValueError(f"{label}: use 1-80 letters, digits, underscores or hyphens")
    return value


def write_new(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def ps(value):
    return "'" + str(value).replace("'", "''") + "'"


def receipt_wire_script():
    """状态回执在 Windows 限长；完整 HTTP/JSON 原件不离开 Windows。"""
    return r'''
function Send-ClaudeReceipt([string]$FullPath) {
    $Info = Get-Item -LiteralPath $FullPath
    $WirePath = $FullPath + '.wire.json'
    $Text = [IO.File]::ReadAllText($FullPath)
    if ($Info.Length + 12 -le 65536) { 'XDEV_JSON ' + $Text; return }
    if (Test-Path -LiteralPath $WirePath) {
        if ((Get-Item -LiteralPath $WirePath).Length + 12 -gt 65536) { throw 'CLAUDER_WIRE_RECEIPT_TOO_LARGE' }
        'XDEV_JSON ' + [IO.File]::ReadAllText($WirePath); return
    }
    $Original = $Text | ConvertFrom-Json
    $Small = @{}
    foreach ($Key in @('status','request_id','payload_sha256','at_utc','transport','response_decoding','response_content_type')) {
        if ($null -ne $Original.$Key) { $Small[$Key] = ([string]$Original.$Key).Substring(0,[Math]::Min(512,([string]$Original.$Key).Length)) }
    }
    $Small.native_mcp_verified = $false
    $Small.http_seconds = $Original.http_seconds
    $Small.response = @{}
    if ($null -ne $Original.response) {
        foreach ($Key in @('status','job_id','success')) {
            $Value = $Original.response.$Key
            if ($Value -is [bool]) { $Small.response[$Key] = [bool]$Value }
            elseif ($null -ne $Value) { $Small.response[$Key] = ([string]$Value).Substring(0,[Math]::Min(512,([string]$Value).Length)) }
        }
        if ($null -ne $Original.response.progress) {
            $Small.response.progress = @{}
            foreach ($Key in @('stage','message','updated_at')) {
                $Value = [string]$Original.response.progress.$Key
                $Small.response.progress[$Key] = $Value.Substring(0,[Math]::Min(512,$Value.Length))
            }
        }
    }
    # ConvertTo-Json -Compress 的正文一行；不把巨大输出再拆成内存中的行数组。
    $Lines = 1
    for ($Position = 0; ($Position = $Text.IndexOf("`n", $Position)) -ge 0; $Position++) { $Lines++ }
    $Small.full_receipt = @{path=$FullPath; bytes=[long]$Info.Length; sha256=(Get-FileHash -LiteralPath $FullPath -Algorithm SHA256).Hash.ToLower(); lines=$Lines; location='WINDOWS_ONLY'}
    $Small.receipt_bounded = $true
    $Small.response.output_omitted = $true
    $Wire = $Small | ConvertTo-Json -Compress -Depth 8
    $Encoding = New-Object Text.UTF8Encoding($false)
    if ($Encoding.GetByteCount($Wire) + 12 -gt 65536) { throw 'CLAUDER_WIRE_RECEIPT_TOO_LARGE' }
    [IO.File]::WriteAllText(($WirePath + '.part'), $Wire, $Encoding)
    [IO.File]::Move(($WirePath + '.part'), $WirePath)
    'XDEV_JSON ' + $Wire
}
'''


def binding_script(binding):
    return r'''
$ErrorActionPreference = 'Stop'
$Port = PORT_VALUE
$ExpectedR = RPID_VALUE
$ExpectedStudio = SPID_VALUE
$ExpectedName = NAME_VALUE
$ExpectedBirth = BIRTH_VALUE
$DiscoveryFile = Join-Path $env:USERPROFILE ('.claude_r_sessions\' + $ExpectedName + '.json')
$Discovery = [IO.File]::ReadAllText($DiscoveryFile) | ConvertFrom-Json
$Headers = @{}
if ($Discovery.token) { $Headers['X-Clauder-Token'] = [string]$Discovery.token }
if ($Discovery.session_name -ne $ExpectedName -or [int]$Discovery.port -ne $Port -or [int]$Discovery.pid -ne $ExpectedR) { throw 'CLAUDER_DISCOVERY_IDENTITY_CHANGED' }
$IdentityStarted = [DateTime]::UtcNow
$PortPattern = '^\s*TCP\s+127\.0\.0\.1:' + $Port + '\s+\S+\s+LISTENING\s+(\d+)\s*$'
$Listeners = @(& "$env:WINDIR\System32\netstat.exe" -ano -p tcp | ForEach-Object { if ($_ -match $PortPattern) { [int]$Matches[1] } })
if ($Listeners.Count -ne 1 -or $Listeners[0] -ne $ExpectedR) { throw 'CLAUDER_LISTENER_IDENTITY_CHANGED' }
$RProcess = Get-Process -Id $ExpectedR -ErrorAction Stop
$Studio = Get-Process -Id $ExpectedStudio -ErrorAction Stop
if ($RProcess.ProcessName -notmatch '^rsession' -or $Studio.ProcessName -ne 'rstudio') { throw 'CLAUDER_RSTUDIO_PROCESS_CHANGED' }
$Birth = $RProcess.StartTime.ToUniversalTime().ToString('o')
if ($ExpectedBirth) {
    if ([Math]::Abs(([DateTime]::Parse($Birth)-[DateTime]::Parse($ExpectedBirth)).TotalMilliseconds) -gt 1) { throw 'CLAUDER_R_PID_REUSED' }
} else {
    # Windows 的创建父 PID 不变；首次核父子链，后续核同一 R 生命周期与监听归属。
    $Ancestry = Get-CimInstance Win32_Process -Filter ("ProcessId = " + $ExpectedR)
    if ($Ancestry.ParentProcessId -ne $ExpectedStudio) { throw 'CLAUDER_RSTUDIO_PARENT_CHANGED' }
}
$Url = 'http://127.0.0.1:' + $Port + '/'
# PS5.1 的 .Content 在缺 charset 时可能按 Latin-1 解码 JSON；原始 UTF-8 字节才是合同。
$ResponseUtf8 = New-Object Text.UTF8Encoding($false, $true)
$StatusResponse = Invoke-WebRequest -UseBasicParsing -Uri $Url -Headers $Headers -TimeoutSec 8
$Status = $ResponseUtf8.GetString($StatusResponse.RawContentStream.ToArray()) | ConvertFrom-Json
if (-not $Status.running -or $Status.session_name -ne $ExpectedName) { throw 'CLAUDER_HTTP_IDENTITY_CHANGED' }
$Identity = @{session_name=$ExpectedName; port=$Port; r_pid=$ExpectedR; rstudio_pid=$ExpectedStudio; r_birth_utc=$Birth; computer=$env:COMPUTERNAME; r_executable=$RProcess.Path; rstudio_executable=$Studio.Path; identity_seconds=([DateTime]::UtcNow-$IdentityStarted).TotalSeconds}
'''.replace("PORT_VALUE", str(binding["port"])).replace("RPID_VALUE", str(binding["r_pid"])).replace(
        "SPID_VALUE", str(binding["rstudio_pid"])).replace("NAME_VALUE", ps(binding["session_name"])).replace(
        "BIRTH_VALUE", ps(binding.get("r_birth_utc", "")))


def request_script(binding, request_id, payload, http_timeout):
    encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
    digest = hashlib.sha256(base64.b64decode(encoded)).hexdigest()
    return binding_script(binding) + receipt_wire_script() + r'''
$RequestRoot = Join-Path $XRoot ('clauder-requests\' + BINDING_VALUE + '\' + REQUEST_VALUE)
$ReplyFile = Join-Path $RequestRoot 'RESPONSE.json'
$IntentFile = Join-Path $RequestRoot 'INTENT.json'
$PayloadBytes = [Convert]::FromBase64String(PAYLOAD_VALUE)
$Payload = [Text.Encoding]::UTF8.GetString($PayloadBytes) | ConvertFrom-Json
$Digest = DIGEST_VALUE
$Utf8 = New-Object Text.UTF8Encoding($false)
if (Test-Path -LiteralPath $IntentFile) {
    $Old = [IO.File]::ReadAllText($IntentFile) | ConvertFrom-Json
    if ($Old.payload_sha256 -ne $Digest) { throw 'CLAUDER_REQUEST_ID_REUSED_FOR_DIFFERENT_CODE' }
    if (Test-Path -LiteralPath $ReplyFile) {
        Send-ClaudeReceipt $ReplyFile
    } else {
        Emit @{status='UNKNOWN_PRIOR_HTTP_REQUEST'; request_id=REQUEST_VALUE; job_id=$Payload.job_id; resubmitted=$false}
    }
    exit 0
}
[IO.Directory]::CreateDirectory($RequestRoot) | Out-Null
$Intent = @{request_id=REQUEST_VALUE; payload_sha256=$Digest; identity=$Identity; at_utc=[DateTime]::UtcNow.ToString('o'); job_id=$Payload.job_id; check_job=$Payload.check_job}
$IntentBytes = $Utf8.GetBytes(($Intent | ConvertTo-Json -Compress -Depth 8))
$Claim = [IO.File]::Open($IntentFile, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
try { $Claim.Write($IntentBytes, 0, $IntentBytes.Length); $Claim.Flush() } finally { $Claim.Dispose() }
$Started = [DateTime]::UtcNow
try {
    $Response = Invoke-WebRequest -UseBasicParsing -Method Post -Uri $Url -Headers $Headers -Body $PayloadBytes -ContentType 'application/json; charset=utf-8' -TimeoutSec TIMEOUT_VALUE
    $ResponseBytes = $Response.RawContentStream.ToArray()
    [IO.File]::WriteAllBytes((Join-Path $RequestRoot 'HTTP_RESPONSE.body'), $ResponseBytes)
    $ResponseText = $ResponseUtf8.GetString($ResponseBytes)
    $Reply = $ResponseText | ConvertFrom-Json
    $Receipt = @{status='HTTP_RESPONSE'; request_id=REQUEST_VALUE; payload_sha256=$Digest; identity=$Identity; at_utc=[DateTime]::UtcNow.ToString('o'); http_seconds=([DateTime]::UtcNow-$Started).TotalSeconds; response=$Reply; transport='MAC_XDEV_WINDOWS_LOOPBACK_HTTP'; native_mcp_verified=$false; response_decoding='UTF8_RAW_CONTENT_STREAM'; response_content_type=[string]$Response.Headers['Content-Type']; legacy_content_differs=($ResponseText -cne $Response.Content)}
} catch {
    $Receipt = @{status='UNKNOWN_HTTP_OUTCOME'; request_id=REQUEST_VALUE; payload_sha256=$Digest; identity=$Identity; at_utc=[DateTime]::UtcNow.ToString('o'); http_seconds=([DateTime]::UtcNow-$Started).TotalSeconds; error=$_.Exception.Message; job_id=$Payload.job_id; resubmitted=$false}
}
$Saved = $Receipt | ConvertTo-Json -Compress -Depth 24
[IO.File]::WriteAllText(($ReplyFile + '.part'), $Saved, $Utf8)
[IO.File]::Move(($ReplyFile + '.part'), $ReplyFile)
Send-ClaudeReceipt $ReplyFile
'''.replace("BINDING_VALUE", ps(binding["binding_id"])).replace("REQUEST_VALUE", ps(request_id)).replace(
        "PAYLOAD_VALUE", ps(encoded)).replace("DIGEST_VALUE", ps(digest)).replace("TIMEOUT_VALUE", str(http_timeout))


class QueueJournal:
    """在 run_ps 打印原队列 ID 时即落盘；中断后不重新入队。"""

    def __init__(self, directory):
        self.directory = directory
        self.stream = (directory / "queue.stderr").open("x", encoding="utf-8")
        self.buffer = ""

    def write(self, value):
        self.stream.write(value)
        self.stream.flush()
        sys.__stderr__.write(value)
        sys.__stderr__.flush()
        self.buffer += value
        found = re.search(r"XDEV_QUEUED id=(\d+)", self.buffer)
        if found and not (self.directory / "QUEUE.json").exists():
            write_new(self.directory / "QUEUE.json", {"job_id": int(found[1])})
        return len(value)

    def flush(self):
        self.stream.flush()

    def close(self):
        self.stream.close()


def save_response(directory, rc, stdout, stderr, queue_id, elapsed):
    # 即使外层 JSON 有误也先保全原回执；Unicode 分隔符不能按 splitlines 拆开。
    write_new(directory / "RAW_TRANSPORT.json", {
        "returncode": rc, "stdout": stdout, "stderr": stderr, "queue_id": queue_id,
        "elapsed_seconds": elapsed, "at_utc": datetime.now(timezone.utc).isoformat()})
    rows = [json.loads(line[10:]) for line in stdout.split("\n") if line.startswith("XDEV_JSON ")]
    result = {"returncode": rc, "stdout": stdout, "stderr": stderr, "queue_id": queue_id,
              "elapsed_seconds": elapsed, "at_utc": datetime.now(timezone.utc).isoformat(), "rows": rows}
    write_new(directory / "TRANSPORT.json", result)
    if len(rows) == 1:
        write_new(directory / "RESPONSE.json", rows[0])
    return result


def invoke(directory, script, http_timeout, wait):
    import xdev_core as core
    (directory / "request.ps1").write_text(script, encoding="utf-8")
    journal = QueueJournal(directory)
    began = time.monotonic()
    try:
        with contextlib.redirect_stderr(journal):
            rc, out, err, queue_id = core.run_ps(script, timeout=http_timeout + 20, wait=wait)
    finally:
        journal.close()
    return save_response(directory, rc, out, err, queue_id, time.monotonic() - began)


def reconcile(directory):
    """只读取原队列回执；不会把原 HTTP 请求再次入队。"""
    import xdev_core as core
    response = directory / "RESPONSE.json"
    if response.exists():
        return {"returncode": 0, "rows": [read(response)]}
    q = read(directory / "QUEUE.json")["job_id"]
    try:
        result = read(core.STATE / "results" / f"{q}.json")
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError):
        return {"returncode": 124, "queue_id": q, "rows": []}
    # 原 WAITING 回执保持；迟到结果另存，不覆盖未知状态。
    late = directory / "late-result"
    if late.exists():
        return read(late / "TRANSPORT.json")
    late.mkdir()
    output = base64.b64decode(result["out"]).decode("utf-8").replace("\r\n", "\n")
    error = base64.b64decode(result["err"]).decode("utf-8").replace("\r\n", "\n")
    return save_response(late, int(result["rc"]), output, error, q, 0.0)


def report(state, action, result, job_id=None):
    if len(result.get("rows", [])) == 1:
        row = result["rows"][0]
        response = row.get("response", {})
        if action in ("poll", "result") and job_id and response.get("status") == "complete":
            terminal = state / "jobs" / job_id / "TERMINAL.json"
            if not terminal.exists():
                write_new(terminal, row)
        print("XDEV_JSON " + json.dumps(row, ensure_ascii=False))
        status = response.get("status", row.get("status", "UNKNOWN"))
        print(f"XDEV_RSTUDIO {status} job_id={job_id or '-'}")
        if row.get("status", "").startswith("UNKNOWN"):
            return 124
        if response.get("success") is False or response.get("status") == "not_found":
            return 3
    else:
        print(f"XDEV_RSTUDIO WAITING_OR_FAILED queue_id={result.get('queue_id')} rc={result['returncode']}")
        if result.get("stderr"):
            print(result["stderr"], file=sys.stderr)
    return result["returncode"]


def run(args):
    state = Path(args.state).expanduser().resolve()
    if not 1 <= args.http_timeout <= 35 or not 0 <= args.wait <= 55:
        raise ValueError("HTTP timeout must be 1-35 seconds and local wait 0-55 seconds")
    if args.action == "bind":
        for key in ("session_name", "port", "r_pid", "rstudio_pid"):
            if not getattr(args, key):
                raise ValueError(f"bind requires --{key.replace('_', '-')}")
        name(args.session_name, "session-name")
        if not 1024 <= args.port <= 65535 or min(args.r_pid, args.rstudio_pid) <= 0:
            raise ValueError("invalid target port or PID")
        state.mkdir(parents=True, exist_ok=False)
        binding = {"session_name": args.session_name, "port": args.port, "r_pid": args.r_pid,
                   "rstudio_pid": args.rstudio_pid, "binding_id": uuid.uuid4().hex}
        write_new(state / "BIND_INTENT.json", binding)
        directory = state / "requests" / "bind"
        directory.mkdir(parents=True)
        result = invoke(directory, binding_script(binding) + "\nEmit @{status='BOUND'; identity=$Identity}\n", args.http_timeout, args.wait)
        if result["returncode"] == 0 and len(result["rows"]) == 1 and result["rows"][0]["status"] == "BOUND":
            binding.update(result["rows"][0]["identity"])
            write_new(state / "BINDING.json", binding)
        return report(state, args.action, result)
    if args.action == "result":
        request_id = name(args.request_id, "request-id")
        directory = state / "requests" / request_id
        result = reconcile(directory)
        if request_id == "bind" and not (state / "BINDING.json").exists() and len(result.get("rows", [])) == 1 and result["rows"][0].get("status") == "BOUND":
            write_new(state / "BINDING.json", {**read(state / "BIND_INTENT.json"), **result["rows"][0]["identity"]})
        intent = directory / "INTENT.json"
        job_id = read(intent).get("job_id") if intent.exists() else None
        return report(state, args.action, result, job_id)
    binding = read(state / "BINDING.json")
    if args.action == "poll":
        job_id = name(args.job_id, "job-id")
        if not (state / "jobs" / job_id / "SUBMIT_INTENT.json").exists():
            raise ValueError("Unknown job for this binding; preserve the original submit state")
        terminal = state / "jobs" / job_id / "TERMINAL.json"
        if terminal.exists():
            return report(state, args.action, {"returncode": 0, "rows": [read(terminal)]}, job_id)
        payload = {"check_job": job_id}
        request_id = name(args.request_id or ("poll-" + uuid.uuid4().hex), "request-id")
    else:
        request_id = name(args.request_id, "request-id")
        if not args.code_file:
            raise ValueError("execute/submit require --code-file")
        code = Path(args.code_file).read_text(encoding="utf-8")
        if not code.strip():
            raise ValueError("Empty R script")
        payload = {"code": code, "agent_id": args.agent_id}
        job_id = None
        if args.action == "submit":
            job_id = name(args.job_id, "job-id")
            payload.update({"async": True, "job_id": job_id})
            job_dir = state / "jobs" / job_id
            job_dir.mkdir(parents=True, exist_ok=False)
            write_new(job_dir / "SUBMIT_INTENT.json", {"request_id": request_id,
                      "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest()})
    directory = state / "requests" / request_id
    directory.mkdir(parents=True, exist_ok=False)
    write_new(directory / "INTENT.json", {"action": args.action, "job_id": job_id,
              "request_id": request_id, "payload": payload, "at_utc": datetime.now(timezone.utc).isoformat()})
    result = invoke(directory, request_script(binding, request_id, payload, args.http_timeout), args.http_timeout, args.wait)
    return report(state, args.action, result, job_id)
