"""绑定原始异步请求的批次编排；不实现主机准入、科学验收或文件传输。"""

import contextlib
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import tempfile
import time
import uuid

import xdev_clauder as client


TRANSPORT = "MAC_XDEV_WINDOWS_LOOPBACK_HTTP"
MAX_MEMBERS = 128
TERMINAL = {"SUCCEEDED", "FAILED"}
_CLOCK_PROCESS = os.getpid()
_CLOCK_SESSION = uuid.uuid4().hex


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def poll_clock():
    """没有可靠启动身份时只在同一进程会话内比较单调时钟；fork 不继承比较域。"""
    global _CLOCK_PROCESS, _CLOCK_SESSION
    pid = os.getpid()
    if pid != _CLOCK_PROCESS:
        _CLOCK_PROCESS, _CLOCK_SESSION = pid, uuid.uuid4().hex
    return {"kind": "PROCESS_MONOTONIC_NS", "domain": f"{pid}:{_CLOCK_SESSION}",
            "value_ns": time.monotonic_ns()}


def poll_interval(previous, current):
    """旧记录、跨进程会话或无效时钟值不能回退到墙钟延迟。"""
    for value in (previous, current):
        if (not isinstance(value, dict) or value.get("kind") != "PROCESS_MONOTONIC_NS"
                or not isinstance(value.get("domain"), str) or not value["domain"]
                or type(value.get("value_ns")) is not int or value["value_ns"] < 0):
            return None
    if previous["domain"] != current["domain"] or current["value_ns"] < previous["value_ns"]:
        return None
    return (current["value_ns"] - previous["value_ns"]) / 1_000_000_000


def save(path, value, *, exclusive=False):
    """临时文件落盘后原子发布；独占意图不覆盖旧证据。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".fanout-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Busy(Exception):
    pass


@contextlib.contextmanager
def run_lock(directory):
    """锁只覆盖一个绑定下的一个 run；这不是主机执行槽租约。"""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "LOCK").open("a+b") as stream:
        try:
            if os.name == "nt":
                import msvcrt
                if stream.seek(0, os.SEEK_END) == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise Busy("This original run is already being operated on") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def keys(value, allowed, required, label):
    if not isinstance(value, dict) or set(value) - set(allowed) or not set(required) <= set(value):
        raise ValueError(f"Invalid {label}: required={sorted(required)}, allowed={sorted(allowed)}")


def sha256_field(value, label):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"{label} must be a lowercase SHA256 digest")
    return value


def windows_directory(value):
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise ValueError("Member work/temp directories must be absolute Windows paths")
    path = PureWindowsPath(value)
    # 扩展、设备及 UNC 名称可绕过普通盘符路径的字符串隔离检查。
    if len(path.drive) != 2 or path.drive[1] != ":" or not path.drive[0].isascii() or not path.drive[0].isalpha():
        raise ValueError("Member directories require ordinary drive-letter paths; UNC/device/extended namespaces are unsupported")
    if not path.is_absolute() or len(path.parts) < 2 or any(part == ".." for part in path.parts):
        raise ValueError("Member work/temp directories must be absolute, non-root Windows paths without '..'")
    for part in path.parts[1:]:
        if part.endswith((".", " ")) or any(char in '<>:"|?*' for char in part):
            raise ValueError("Ambiguous Windows directory component")
    return str(path)


def validate_admission(receipt, run_id, binding, capacity):
    required = {"schema_version", "status", "grant_id", "run_id", "binding_id", "max_concurrency", "expires_at_utc"}
    if not isinstance(receipt, dict) or not required <= receipt.keys():
        raise ValueError("External admission receipt lacks original grant, run, binding, capacity or expiry")
    if (type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1 or receipt["status"] != "GRANTED"
            or not isinstance(receipt["grant_id"], str) or not receipt["grant_id"].strip()
            or receipt["run_id"] != run_id or receipt["binding_id"] != binding["binding_id"]
            or type(receipt["max_concurrency"]) is not int or receipt["max_concurrency"] < capacity):
        raise ValueError("Pinned external host grant does not authorize this run, binding and batch capacity")
    try:
        expires = datetime.fromisoformat(receipt["expires_at_utc"].replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("External grant expiry must be an ISO8601 timestamp with timezone") from exc
    if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
        raise ValueError("External host admission grant expired or lacks timezone; original jobs may still be polled")


def wrapped_code(code, member):
    # 仅设置异步 worker 的 cwd 和子进程环境；不修改主 RStudio，不重写科学脚本。
    # callr 在执行此代码前已启动；既有 R tempdir() 仍由 callr/R 自行管理。
    work = json.dumps(member["remote_work_dir"], ensure_ascii=False)
    temp = json.dumps(member["remote_temp_dir"], ensure_ascii=False)
    return ("# xdev fanout: approved worker-local directory/environment setup\n"
            "local({\n"
            f"  .xdev_work <- {work}\n  .xdev_temp <- {temp}\n"
            '  if (!dir.exists(.xdev_work) || !dir.exists(.xdev_temp)) stop("XDEV_PREPROVISIONED_DIRECTORY_MISSING")\n'
            "  setwd(.xdev_work)\n"
            "  Sys.setenv(TMP = .xdev_temp, TEMP = .xdev_temp, TMPDIR = .xdev_temp)\n"
            "})\n"
            f"# Original approved scientific code SHA256: {digest(code.encode('utf-8'))}\n" + code)


def contract_file(path, *, load_code):
    if not path:
        raise ValueError("fanout actions require --contract")
    path = Path(path).expanduser().resolve()
    value = client.read(path)
    keys(value, {"schema_version", "batch_concurrency", "external_host_admission", "members"},
         {"schema_version", "batch_concurrency", "external_host_admission", "members"}, "fanout contract")
    if value["schema_version"] != 1 or isinstance(value["schema_version"], bool):
        raise ValueError("fanout schema_version must be 1")
    capacity = value["batch_concurrency"]
    if type(capacity) is not int or not 1 <= capacity <= 32:
        raise ValueError("batch_concurrency must be 1-32; it is not host capacity")
    admission = value["external_host_admission"]
    keys(admission, {"receipt_file", "receipt_sha256"}, {"receipt_file", "receipt_sha256"}, "external_host_admission")
    sha256_field(admission["receipt_sha256"], "external admission receipt_sha256")
    if not isinstance(admission["receipt_file"], str) or not admission["receipt_file"]:
        raise ValueError("External admission receipt_file must pin a separately granted host admission receipt")
    admission_path = (path.parent / admission["receipt_file"]).resolve()
    receipt = None
    if load_code:
        raw = admission_path.read_bytes()
        if digest(raw) != admission["receipt_sha256"]:
            raise ValueError("External host admission receipt_sha256 mismatch")
        receipt = {"document": json.loads(raw.decode("utf-8")), "original_utf8": raw.decode("utf-8")}
    members = value["members"]
    if not isinstance(members, list) or not 1 <= len(members) <= MAX_MEMBERS:
        raise ValueError(f"members must contain 1-{MAX_MEMBERS} independent R members")
    seen, codes, normalized, directories = set(), {}, [], []
    for member in members:
        required = {"member_id", "code_file", "code_sha256", "independent", "remote_work_dir", "remote_temp_dir"}
        keys(member, required, required, "member")
        if not isinstance(member["member_id"], str):
            raise ValueError("member_id must be a string")
        mid = client.name(member["member_id"], "member_id")
        if mid.casefold() in seen or member["independent"] is not True:
            raise ValueError("Every member must be unique ignoring case and explicitly approved independent=true")
        seen.add(mid.casefold())
        sha = sha256_field(member["code_sha256"], f"{mid}: code_sha256")
        work = windows_directory(member["remote_work_dir"])
        temp = windows_directory(member["remote_temp_dir"])
        if work.casefold() == temp.casefold():
            raise ValueError("A member's work and temp directory must be distinct")
        for previous_mid, previous in directories:
            for current in (work, temp):
                a, b = PureWindowsPath(previous.casefold()), PureWindowsPath(current.casefold())
                if a == b or a in b.parents or b in a.parents:
                    raise ValueError(f"Member directories overlap across {previous_mid} and {mid}")
        directories.extend([(mid, work), (mid, temp)])
        if not isinstance(member["code_file"], str) or not member["code_file"]:
            raise ValueError(f"{mid}: code_file must name an approved R script")
        code_path = (path.parent / member["code_file"]).resolve()
        if load_code:
            raw = code_path.read_bytes()
            code = raw.decode("utf-8")
            if not code.strip() or digest(raw) != sha:
                raise ValueError(f"{mid}: empty code or code_sha256 mismatch")
            codes[mid] = code
        normalized.append({**member, "code_file": str(code_path), "remote_work_dir": work, "remote_temp_dir": temp})
    normalized_contract = {**value, "members": normalized,
                           "external_host_admission": {**admission, "receipt_file": str(admission_path)}}
    return normalized_contract, digest(canonical(normalized_contract)), codes, receipt


def load_run(directory, binding, contract, sha, codes, receipt, args):
    intent_path = directory / "INTENT.json"
    if intent_path.exists():
        intent = client.read(intent_path)
        if (intent["run_id"] != args.run_id or intent["contract_sha256"] != sha or intent["contract"] != contract
                or intent["binding_sha256"] != digest(canonical(binding))):
            raise ValueError("Original run contract or RStudio binding changed; preserve the original run")
        admission = intent["external_host_admission_receipt"]
        if (digest(admission["original_utf8"].encode("utf-8")) != contract["external_host_admission"]["receipt_sha256"]
                or json.loads(admission["original_utf8"]) != admission["document"]):
            raise ValueError("Original external admission receipt was changed")
        if len(intent["members"]) != len(contract["members"]):
            raise ValueError("Original member set changed")
        # 不再读取源脚本也可轮询；实际提交始终使用原代码快照。
        for member, approved in zip(intent["members"], contract["members"]):
            snapshot = client.read(directory / "code" / (member["member_id"] + ".json"))["code"]
            token = digest(canonical([binding["binding_id"], args.run_id, approved["member_id"]]))[:40]
            expected = {**approved, "job_id": "fo-" + token, "request_id": "fo-" + token + "-submit",
                        "submitted_code_sha256": digest(wrapped_code(snapshot, approved).encode("utf-8"))}
            if member != expected:
                raise ValueError("Original member identity, paths or code hash changed")
            if (digest(snapshot.encode("utf-8")) != member["code_sha256"]
                    or digest(wrapped_code(snapshot, member).encode("utf-8")) != member["submitted_code_sha256"]):
                raise ValueError("Original code snapshot was changed")
        return intent
    if args.action != "fanout-submit":
        raise ValueError("Unknown run; first use fanout-submit with the approved contract")
    members = []
    for member in contract["members"]:
        mid = member["member_id"]
        token = digest(canonical([binding["binding_id"], args.run_id, mid]))[:40]
        members.append({**member, "job_id": "fo-" + token, "request_id": "fo-" + token + "-submit",
                        "submitted_code_sha256": digest(wrapped_code(codes[mid], member).encode("utf-8"))})
        code_path = directory / "code" / (mid + ".json")
        if code_path.exists():
            if client.read(code_path) != {"code": codes[mid]}:
                raise ValueError("Interrupted run preparation has a different code snapshot")
        else:
            save(code_path, {"code": codes[mid]}, exclusive=True)
    intent = {"schema_version": 1, "run_id": args.run_id, "at_utc": now(),
              "contract": contract, "contract_sha256": sha,
              "binding_sha256": digest(canonical(binding)), "binding_id": binding["binding_id"],
              "members": members, "agent_id": args.agent_id, "transport": TRANSPORT,
              "external_host_admission_receipt": receipt,
              "native_mcp_verified": False, "capacity_scope": "BATCH_ONLY",
              "external_host_admission_enforced_by_xdev": False}
    save(intent_path, intent, exclusive=True)
    return intent


def member_events(directory, member):
    events = directory / "members" / member["member_id"] / "events"
    records = sorted(events.glob("*.json")) if events.exists() else []
    return [client.read(record) for record in records]


def member_status(directory, member):
    records = member_events(directory, member)
    request_order, latest_requests, terminals = {}, {}, []
    for index, record in enumerate(records):
        request_id = record.get("request_id")
        request_order.setdefault(request_id, index)
        if request_id and isinstance(record.get("payload"), dict):
            latest_requests[request_id] = record
        if record["status"] not in TERMINAL:
            continue
        result = {"returncode": 0, "rows": [record.get("receipt")]}
        verified = classify(result, request_id, record.get("payload", {}), member["job_id"])[0]
        if verified != record["status"]:
            return {"member_id": member["member_id"], "job_id": member["job_id"], "status": "UNKNOWN",
                    "terminal_conflict": True, "message": "Recorded terminal does not match exact original receipt"}
        try:
            archived, reference = archived_receipt(directory.parent.parent, request_id,
                                                    record.get("payload", {}), member["job_id"])
            if archived != record.get("receipt") or reference != record.get("evidence"):
                raise ValueError("Event differs from the archived original receipt or pinned evidence hashes")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {"member_id": member["member_id"], "job_id": member["job_id"], "status": "UNKNOWN",
                    "terminal_conflict": True, "message": f"Original terminal archive is missing or inconsistent: {exc}"}
        terminals.append(record)
    if len({record["status"] for record in terminals}) > 1:
        return {"member_id": member["member_id"], "job_id": member["job_id"], "status": "UNKNOWN",
                "terminal_conflict": True, "message": "Conflicting exact original async terminal outcomes"}
    pending_submits = [record for record in latest_requests.values()
                       if record["payload"].get("async") is True and not transport_completed(record)]
    if pending_submits:
        return {**pending_submits[0], "status": "UNKNOWN", "pending_submission_transport": True,
                "message": "Original submission transport is unresolved; keep batch capacity occupied"}
    if terminals:
        # 原请求顺序决定终态证据，不让后来的 not_found/UNKNOWN 覆盖已保存原终态。
        return min(terminals, key=lambda record: request_order[record["request_id"]])
    if records:
        return records[-1]
    return {"status": "PENDING", "job_id": member["job_id"], "member_id": member["member_id"]}


def event(directory, member, status, **extra):
    events = directory / "members" / member["member_id"] / "events"
    events.mkdir(parents=True, exist_ok=True)
    sequence = len(list(events.glob("*.json"))) + 1
    value = {"status": status, "at_utc": now(), "job_id": member["job_id"],
             "member_id": member["member_id"], **extra}
    save(events / f"{sequence:08d}.json", value, exclusive=True)
    return value


def payload_sha(payload):
    # 必须与现有 request_script 的 UTF-8 编码一致。
    return digest(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8"))


def archived_receipt(state, request_id, payload, job_id):
    """终态必须绑定原请求、实际返回码和原始传输字节；事件副本不能自证。"""
    client.name(request_id, "original request-id")
    request_dir = state / "requests" / request_id
    intent_path = request_dir / "INTENT.json"
    intent = client.read(intent_path)
    binding = client.read(state / "BINDING.json")
    if (intent.get("request_id") != request_id or intent.get("job_id") != job_id
            or intent.get("payload") != payload
            or intent.get("action") != ("submit" if payload.get("async") else "poll")
            or intent.get("binding_sha256") != digest(canonical(binding))):
        raise ValueError("Original request intent does not bind this payload, job and RStudio identity")
    archives = [path for path in (request_dir, request_dir / "late-result") if (path / "RESPONSE.json").is_file()]
    if len(archives) != 1:
        raise ValueError("Expected one original response archive")
    archive = archives[0]
    response_path, transport_path, raw_path = (archive / name for name in ("RESPONSE.json", "TRANSPORT.json", "RAW_TRANSPORT.json"))
    response, transport, raw = (client.read(path) for path in (response_path, transport_path, raw_path))
    rows = [json.loads(line[10:]) for line in raw["stdout"].split("\n") if line.startswith("XDEV_JSON ")]
    if (raw.get("returncode") != 0 or transport.get("returncode") != 0
            or rows != [response] or transport.get("rows") != [response]
            or any(raw.get(key) != transport.get(key) for key in ("stdout", "stderr", "queue_id"))):
        raise ValueError("Original raw output, returncode, transport and response disagree")
    if (response.get("request_id") != request_id or response.get("payload_sha256") != payload_sha(payload)
            or response.get("transport") != TRANSPORT or response.get("native_mcp_verified") is not False):
        raise ValueError("Archived receipt does not bind the original request and route")
    identity = response.get("identity")
    if identity is not None:
        if not isinstance(identity, dict) or any(identity.get(key) != binding.get(key) for key in
                                               ("session_name", "port", "r_pid", "rstudio_pid", "r_birth_utc")):
            raise ValueError("Archived receipt RStudio identity differs from the original binding")
    reference = {key: {"path": str(path.relative_to(state)), "sha256": digest(path.read_bytes())}
                 for key, path in (("request_intent", intent_path), ("response", response_path),
                                   ("transport", transport_path), ("raw_transport", raw_path))}
    return response, reference


def classify(result, request_id, payload, job_id):
    rows = result.get("rows", [])
    if result.get("returncode") != 0 or len(rows) != 1:
        return "UNKNOWN", "No original successful transport receipt"
    row = rows[0]
    if not isinstance(row, dict) or row.get("status") != "HTTP_RESPONSE":
        return "UNKNOWN", "Original HTTP outcome is unknown"
    if (row.get("request_id") != request_id or row.get("payload_sha256") != payload_sha(payload)
            or row.get("transport") != TRANSPORT or row.get("native_mcp_verified") is not False):
        return "UNKNOWN", "Receipt does not bind the original request and transport"
    response = row.get("response")
    if not isinstance(response, dict):
        return "UNKNOWN", "Missing ClaudeR response"
    metadata = response.get("metadata")
    echoed = [response.get("job_id")]
    if isinstance(metadata, dict):
        echoed.append(metadata.get("job_id"))
    if any(value is not None and value != job_id for value in echoed):
        return "UNKNOWN", "Response belongs to a different async job"
    status = response.get("status")
    if status == "not_found":
        return "UNKNOWN", "not_found does not establish that the original job never ran"
    if status == "complete":
        if response.get("success") is True and payload.get("check_job") == job_id:
            return "SUCCEEDED", "Original async terminal succeeded; scientific acceptance is separate"
        if response.get("success") is False:
            return "FAILED", "Original async terminal failed"
        return "UNKNOWN", "No explicit original async terminal success"
    if status in ("error", "failed", "cancelled") or response.get("success") is False:
        return "FAILED", "ClaudeR reported failure or cancellation"
    if status == "running":
        return "RUNNING", "Original async job is running normally"
    if payload.get("async") is True and response.get("job_id") == job_id and response.get("success") is True:
        return "SUBMITTED", "Original async job accepted"
    return "UNKNOWN", "Unrecognized response; preserve original identity"


def observe(directory, member, request_id, payload, result):
    status, message = classify(result, request_id, payload, member["job_id"])
    # 原回执已由 invoke/reconcile 保存；这里额外保存验证所用原行，便于离线合并。
    row = result.get("rows", [])
    extra = {"request_id": request_id, "payload": payload, "message": message,
             "queue_id": result.get("queue_id"), "receipt": row[0] if len(row) == 1 else None,
             "transport_completed": result.get("returncode") not in (None, 124) or len(row) == 1}
    if status in TERMINAL:
        try:
            archived, reference = archived_receipt(directory.parent.parent, request_id, payload, member["job_id"])
            if archived != extra["receipt"]:
                raise ValueError("Observed terminal differs from original archived response")
            extra["evidence"] = reference
        except (OSError, ValueError, KeyError, TypeError) as exc:
            status = "UNKNOWN"
            extra["message"] = f"Original terminal archive is missing or inconsistent: {exc}"
    return event(directory, member, status, **extra)


def transport_completed(record):
    completed = record.get("transport_completed")
    if completed is None:  # 升级前原证据的兼容读取，不因此重发请求。
        completed = isinstance(record.get("receipt"), dict) or record["status"] != "UNKNOWN"
    return completed


def send(directory, state, binding, member, request_id, payload, args):
    payload = json.loads(canonical(payload))
    clock = {"poll_clock": poll_clock()} if payload == {"check_job": member["job_id"]} else {}
    request_dir = state / "requests" / request_id
    # 独占请求目录与持久意图先于网络；异常后只接回原 ID。
    request_dir.mkdir(parents=True, exist_ok=False)
    save(request_dir / "INTENT.json", {"action": "submit" if payload.get("async") else "poll",
         "request_id": request_id, "job_id": member["job_id"], "payload": payload,
         "binding_sha256": digest(canonical(binding)),
         "at_utc": now(), **clock}, exclusive=True)
    event(directory, member, "UNKNOWN", request_id=request_id, payload=payload,
          message="Original request intent persisted before transport", **clock)
    try:
        result = client.invoke(request_dir, client.request_script(binding, request_id, payload, args.http_timeout),
                               args.http_timeout, args.wait)
    except Exception as exc:
        return event(directory, member, "UNKNOWN", request_id=request_id, payload=payload,
                     message=f"Original request requires reconciliation: {type(exc).__name__}: {exc}")
    return observe(directory, member, request_id, payload, result)


def reconcile_member(directory, state, member, current):
    request_id = current.get("request_id")
    if not request_id:
        return current
    request_dir = state / "requests" / request_id
    try:
        result = client.reconcile(request_dir)
    except (OSError, ValueError, KeyError):
        return current
    return observe(directory, member, request_id, current["payload"], result)


def reconcile_outstanding(directory, state, member):
    """接回所有未决原请求；一个成员最多保留一个仍待回执的轮询。"""
    latest = {}
    for record in member_events(directory, member):
        request_id, payload = record.get("request_id"), record.get("payload")
        if request_id and isinstance(payload, dict):
            latest[request_id] = record
    pending = []
    for request_id, record in latest.items():
        if record["status"] == "UNKNOWN":
            record = reconcile_member(directory, state, member, record)
        payload = record.get("payload", {})
        if payload == {"check_job": member["job_id"]} or payload.get("async") is True:
            # 原提交和原轮询都占据在途身份；尚无原命令回执不能追加任何查询。
            if not transport_completed(record):
                pending.append(request_id)
    return member_status(directory, member), pending


def original_submission(directory, state, intent, member):
    """原作业和原 HTTP 意图都必须属于本次 run，防止接回同名他方作业。"""
    job = client.read(state / "jobs" / member["job_id"] / "SUBMIT_INTENT.json")
    expected = {"request_id": member["request_id"], "code_sha256": member["code_sha256"],
                "submitted_code_sha256": member["submitted_code_sha256"],
                "run_id": intent["run_id"], "member_id": member["member_id"]}
    if job != expected:
        raise ValueError("Original submit claim does not belong to this run and code")
    request = client.read(state / "requests" / member["request_id"] / "INTENT.json")
    code = client.read(directory / "code" / (member["member_id"] + ".json"))["code"]
    expected_payload = {"code": wrapped_code(code, member), "agent_id": intent["agent_id"], "async": True, "job_id": member["job_id"]}
    if (request.get("action") != "submit" or request.get("request_id") != member["request_id"]
            or request.get("job_id") != member["job_id"] or request.get("payload") != expected_payload
            or request.get("binding_sha256") != intent["binding_sha256"]):
        raise ValueError("Original HTTP submit intent does not bind this member and code")


def submit(directory, state, binding, intent, args):
    current = [member_status(directory, member) for member in intent["members"]]
    if any(item["status"] in ("UNKNOWN", "FAILED") for item in current):
        return
    available = intent["contract"]["batch_concurrency"] - sum(item["status"] in ("SUBMITTED", "RUNNING") for item in current)
    for member, status in zip(intent["members"], current):
        if available <= 0:
            break
        if status["status"] != "PENDING":
            continue
        # 长批次不能沿提交开始时的旧时间检查继续跨过外部执行窗口。
        validate_admission(intent["external_host_admission_receipt"]["document"], intent["run_id"],
                           binding, intent["contract"]["batch_concurrency"])
        # 提交身份先落盘；即使其后中断也不自动重放该成员。
        event(directory, member, "UNKNOWN", request_id=member["request_id"],
              message="Original submit identity reserved")
        job_dir = state / "jobs" / member["job_id"]
        try:
            job_dir.mkdir(parents=True, exist_ok=False)
            save(job_dir / "SUBMIT_INTENT.json", {"request_id": member["request_id"],
                 "code_sha256": member["code_sha256"], "run_id": intent["run_id"],
                 "submitted_code_sha256": member["submitted_code_sha256"],
                 "member_id": member["member_id"]}, exclusive=True)
            code = client.read(directory / "code" / (member["member_id"] + ".json"))["code"]
            payload = {"code": wrapped_code(code, member), "agent_id": intent["agent_id"], "async": True, "job_id": member["job_id"]}
            result = send(directory, state, binding, member, member["request_id"], payload, args)
        except (OSError, ValueError) as exc:
            event(directory, member, "UNKNOWN", request_id=member["request_id"],
                  message=f"Original submit claim needs reconciliation: {exc}")
            break
        available -= 1
        if result["status"] in ("UNKNOWN", "FAILED"):
            break


def poll(directory, state, binding, intent, args):
    cursor_path = directory / "POLL_CURSOR.json"
    cursor = client.read(cursor_path)["next"] if cursor_path.exists() else 0
    members = intent["members"]
    queried = 0
    reconciled = {}
    # 本地接回不消耗新查询预算；扫描全体原成员，避免游标或旧 UNKNOWN 隐藏迟到终态。
    for member in members:
        current = member_status(directory, member)
        if current["status"] == "PENDING":
            reconciled[member["member_id"]] = (current, [])
            continue
        try:
            original_submission(directory, state, intent, member)
        except (OSError, ValueError, KeyError) as exc:
            current = event(directory, member, "UNKNOWN", message=f"Cannot prove original submit ownership: {exc}")
            reconciled[member["member_id"]] = (current, ["UNKNOWN_ORIGINAL_OWNERSHIP"])
            continue
        reconciled[member["member_id"]] = reconcile_outstanding(directory, state, member)
    for offset in range(len(members)):
        index = (cursor + offset) % len(members)
        member = members[index]
        current, outstanding = reconciled[member["member_id"]]
        if current["status"] == "PENDING" or current["status"] in TERMINAL or current.get("terminal_conflict") or outstanding:
            continue
        if queried >= args.max_polls:
            break
        request_id = "fo-poll-" + uuid.uuid4().hex
        send(directory, state, binding, member, request_id, {"check_job": member["job_id"]}, args)
        queried += 1
        # 有界轮询公平轮转；UNKNOWN 不释放批次容量，也不阻止接回其他原任务。
        save(cursor_path, {"next": (index + 1) % len(members), "at_utc": now()})
    return queried


def merge(directory, state, intent):
    rows = []
    for member in intent["members"]:
        current = member_status(directory, member)
        if current.get("terminal_conflict"):
            raise ValueError("Merge rejected: conflicting exact original async terminal evidence")
        if current["status"] != "SUCCEEDED":
            return False
        original_submission(directory, state, intent, member)
        payload = current.get("payload", {})
        result = {"returncode": 0, "rows": [current.get("receipt")]}
        if (payload != {"check_job": member["job_id"]}
                or classify(result, current.get("request_id"), payload, member["job_id"])[0] != "SUCCEEDED"):
            raise ValueError("Merge rejected: missing exact original async success evidence")
        rows.append({"member_id": member["member_id"], "job_id": member["job_id"],
                     "submit_request_id": member["request_id"], "terminal_request_id": current["request_id"],
                     "code_sha256": member["code_sha256"], "submitted_code_sha256": member["submitted_code_sha256"],
                     "remote_work_dir": member["remote_work_dir"], "remote_temp_dir": member["remote_temp_dir"],
                     "terminal_evidence": current["evidence"],
                     "terminal_receipt_sha256": digest(canonical(current["receipt"]))})
    manifest = {"schema_version": 1, "run_id": intent["run_id"], "contract_sha256": intent["contract_sha256"],
                "binding_id": intent["binding_id"], "status": "LIFECYCLE_MERGED", "members": rows,
                "transport": TRANSPORT, "native_mcp_verified": False, "scientific_acceptance": "NOT_EVALUATED",
                "external_host_admission_receipt_sha256": intent["contract"]["external_host_admission"]["receipt_sha256"],
                "external_host_admission_independently_verified": False,
                "files_transferred": [], "merge_code_executed": False}
    target = directory / "MERGED.json"
    if target.exists():
        if client.read(target) != manifest:
            raise ValueError("Existing merge differs from original terminal evidence")
    else:
        save(target, manifest, exclusive=True)
    return True


def full_progress(directory, member, current):
    """本地完整进度独立于有界 wire；未知耗时和首次轮询间隔保持 null。"""
    progress, elapsed, source_request = None, None, None
    source_bounded, source_full_receipt = False, None
    poll_times = {}
    for record in member_events(directory, member):
        payload, request_id = record.get("payload", {}), record.get("request_id")
        if payload == {"check_job": member["job_id"]} and request_id not in poll_times:
            # 首个原意图缺元数据时保留 None，不能借后续接回时间伪造原间隔。
            poll_times[request_id] = record.get("poll_clock")
        receipt = record.get("receipt")
        response = receipt.get("response") if isinstance(receipt, dict) else None
        if isinstance(response, dict):
            if isinstance(response.get("progress"), dict):
                progress = response["progress"]
                source_request = request_id
                source_bounded = receipt.get("receipt_bounded") is True
                source_full_receipt = receipt.get("full_receipt")
            candidate = response.get("elapsed_seconds")
            if type(candidate) in (int, float) and math.isfinite(candidate) and candidate >= 0:
                elapsed = candidate
    observed = list(poll_times.values())
    interval = poll_interval(observed[-2], observed[-1]) if len(observed) >= 2 else None
    return {"member_id": member["member_id"], "job_id": member["job_id"], "status": current["status"],
            "progress": progress, "progress_request_id": source_request,
            "source_receipt_bounded": source_bounded, "source_full_receipt": source_full_receipt,
            "elapsed_seconds": elapsed, "observed_poll_interval_seconds": interval,
            "elapsed_source": "CLAUDER_ORIGINAL_RESPONSE" if elapsed is not None else "UNKNOWN",
            "poll_interval_source": "LOCAL_ORIGINAL_POLL_INTENTS_MONOTONIC_SAME_PROCESS_SESSION" if interval is not None else "UNKNOWN",
            "poll_interval_clock_domain": observed[-1]["domain"] if interval is not None else None}


def summary(directory, intent, *, queried=0):
    members = [member_status(directory, member) for member in intent["members"]]
    statuses = [member["status"] for member in members]
    status = ("FAILED" if "FAILED" in statuses else "UNKNOWN" if "UNKNOWN" in statuses
              else "LIFECYCLE_SUCCEEDED" if all(s == "SUCCEEDED" for s in statuses)
              else "RUNNING" if any(s in ("RUNNING", "SUBMITTED") for s in statuses) else "PENDING")
    value = {"run_id": intent["run_id"], "status": status, "contract_sha256": intent["contract_sha256"],
             "transport": TRANSPORT, "native_mcp_verified": False, "capacity_scope": "BATCH_ONLY",
             "batch_concurrency": intent["contract"]["batch_concurrency"],
             "external_host_admission_required": True, "external_host_admission_enforced_by_xdev": False,
             "external_host_admission_independently_verified": False,
             "worker_environment_scope": "ASYNC_WORKER_TMP_TEMP_TMPDIR_AND_CWD",
             "r_tempdir_scope": "EXISTING_CALLR_R_PROCESS_MANAGED",
             "scientific_acceptance": "NOT_EVALUATED", "queries_this_invocation": queried,
             "merged": (directory / "MERGED.json").exists(),
             "members": [{key: member[key] for key in ("member_id", "job_id", "status")} for member in members]}
    complete_progress = [full_progress(directory, member, current) for member, current in zip(intent["members"], members)]
    progress_path = directory / "PROGRESS.json"
    save(progress_path, {"run_id": intent["run_id"], "status": status, "updated_at_utc": now(),
                         "transport": TRANSPORT, "native_mcp_verified": False, "members": complete_progress})
    value["full_progress_report"] = {"path": str(progress_path), "sha256": digest(progress_path.read_bytes()),
                                     "location": "LOCAL_ONLY", "complete_local_record": True,
                                     "remote_progress_complete": not any(member["source_receipt_bounded"] for member in complete_progress)}
    for visible, member in zip(value["members"], complete_progress):
        visible["elapsed_seconds"] = member["elapsed_seconds"]
        visible["observed_poll_interval_seconds"] = member["observed_poll_interval_seconds"]
        progress = member["progress"]
        if isinstance(progress, dict):
            visible["progress"] = {key: str(progress[key]).encode("utf-8")[:limit].decode("utf-8", errors="ignore")
                                   for key, limit in (("stage", 64), ("message", 128), ("updated_at", 64)) if key in progress}
            visible["progress_truncated"] = (set(visible["progress"]) != set(progress)
                                              or any(visible["progress"][key] != progress[key] for key in visible["progress"]))
    value["progress_omitted_for_wire_limit"] = 0
    for visible in reversed(value["members"]):
        if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) + 12 <= 65536:
            break
        if "progress" in visible:
            visible.pop("progress")
            visible["progress_truncated"] = True
            value["progress_omitted_for_wire_limit"] += 1
    # 128 个成员的 64 KiB 有界清单，不嵌入 R 输出；原进度和回执仍留在请求目录。
    save(directory / "SUMMARY.json", value)
    print("XDEV_JSON " + json.dumps(value, ensure_ascii=False))
    return 3 if "FAILED" in statuses else 124 if "UNKNOWN" in statuses else 0


def reject_case_alias(path):
    if path.parent.is_dir():
        for sibling in path.parent.iterdir():
            if sibling.name.casefold() == path.name.casefold() and sibling.name != path.name:
                raise ValueError("Case alias in fanout state/run path; use the original directory spelling")


def run(args):
    run_id = client.name(args.run_id, "run-id")
    if run_id != run_id.casefold():
        raise ValueError("fanout run-id must be lowercase to avoid case-insensitive filesystem aliases")
    if not 1 <= args.max_polls <= 32:
        raise ValueError("--max-polls must be 1-32; each query also uses --http-timeout/--wait")
    state = Path(args.state).expanduser().resolve()
    reject_case_alias(state)
    binding = client.read(state / "BINDING.json")
    contract, sha, codes, receipt = contract_file(args.contract, load_code=args.action == "fanout-submit")
    if args.action == "fanout-submit":
        validate_admission(receipt["document"], run_id, binding, contract["batch_concurrency"])
    directory = state / "fanout" / run_id
    reject_case_alias(directory)
    try:
        with run_lock(directory):
            intent = load_run(directory, binding, contract, sha, codes, receipt, args)
            queried = 0
            merge_ready = True
            if args.action == "fanout-submit":
                submit(directory, state, binding, intent, args)
            elif args.action == "fanout-poll":
                queried = poll(directory, state, binding, intent, args)
            elif args.action == "fanout-merge":
                merge_ready = merge(directory, state, intent)
            result = summary(directory, intent, queried=queried)
            return result if result else (0 if merge_ready else 4)
    except Busy:
        print("XDEV_JSON " + json.dumps({"run_id": run_id, "status": "BUSY", "resubmitted": False}))
        return 75
