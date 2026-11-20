#!/usr/bin/env python3
"""Opt-in xdev control/1: independent server, durable worker, explicit resources.

No legacy queue/cursor/config is read or written. Python 3.14, standard library.
Use --help and each subcommand's --help. This channel never detaches a command;
Windows commands and descendants belong to a dedicated native Job Object.
UNKNOWN retains its resource reservation and is never automatically replayed.
"""
import argparse
import base64
import contextlib
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


VERSION = "xdev-control/1"
MAX_FRAME = 1024 * 1024
MAX_COMMAND = 64 * 1024
MAX_OUTPUT = 128 * 1024
MAX_JOBS = 1000
TERMINAL = {"SUCCEEDED", "FAILED", "TIMED_OUT", "UNKNOWN"}
RESERVED = {"CLAIMED", "RUNNING", "UNKNOWN"}
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
RESOURCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")


class ProtocolError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def require(condition, message, code=400):
    if not condition:
        raise ProtocolError(message, code)


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def valid_key(value):
    return isinstance(value, str) and KEY.fullmatch(value) is not None


def read_secret(path):
    raw = Path(path).read_bytes()
    require(32 <= len(raw.strip()) <= 4096, "secret file must hold 32..4096 bytes")
    return raw.strip()


def signed(secret, value):
    return dict(value, mac=hmac.new(secret, canonical(value), hashlib.sha256).hexdigest())


def verified(secret, value):
    require(isinstance(value, dict), "invalid signed envelope", 401)
    plain = {k: v for k, v in value.items() if k != "mac"}
    mac = value.get("mac")
    require(isinstance(mac, str) and hmac.compare_digest(
        mac, hmac.new(secret, canonical(plain), hashlib.sha256).hexdigest()), "invalid signature", 401)
    return plain


def validate_spec(spec):
    require(isinstance(spec, dict) and set(spec) == {
        "version", "op", "key", "lane", "resource_keys", "body", "timeout", "output_limit"},
        "invalid job fields")
    require(spec["version"] == VERSION and spec["op"] == "exec", "unsupported protocol or operation")
    require(valid_key(spec["key"]), "invalid job key")
    require(spec["lane"] in {"serial", "independent"}, "invalid lane")
    keys = spec["resource_keys"]
    require(isinstance(keys, list) and len(keys) <= 8 and all(
        isinstance(k, str) and RESOURCE.fullmatch(k) for k in keys), "invalid resource keys")
    require(keys == sorted(set(keys)), "resource keys must be sorted and unique")
    require((spec["lane"] == "serial" and not keys) or
            (spec["lane"] == "independent" and bool(keys)), "independent jobs require explicit resource keys")
    body = spec["body"]
    require(isinstance(body, dict) and set(body) == {"argv"}, "body requires argv only")
    argv = body["argv"]
    require(isinstance(argv, list) and 1 <= len(argv) <= 128 and all(
        isinstance(a, str) and len(a) <= 8192 and "\0" not in a for a in argv) and argv[0], "invalid argv")
    require(len(canonical(spec)) <= MAX_COMMAND, "command too large", 413)
    require(integer(spec["timeout"], 1, 3600), "timeout must be 1..3600 seconds")
    require(integer(spec["output_limit"], 1, MAX_OUTPUT), "invalid output limit")
    return spec


def incompatible(left, right):
    return (left["lane"] == "serial" or right["lane"] == "serial" or
            bool(set(left["resource_keys"]) & set(right["resource_keys"])))


def independent_root(root):
    root = Path(root).resolve()
    require(not any((root / name).exists() for name in (
        "last_id", "queue", "results", "claims", "agent2.json")), "legacy state directory refused")
    return root


class StateLock:
    """持有操作系统锁直到退出；不删除锁文件、不按旧 PID 杀进程。"""
    def __init__(self, root):
        root = independent_root(root)
        root.mkdir(parents=True, exist_ok=True)
        self.stream = os.fdopen(os.open(root / "dispatcher.lock", os.O_RDWR | os.O_CREAT, 0o600), "r+b")
        if os.fstat(self.stream.fileno()).st_size == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise ProtocolError("state already has an active dispatcher", 409) from None

    def close(self):
        if not self.stream.closed:
            if os.name == "nt":
                import msvcrt
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class Store:
    def __init__(self, root, role, channel_id):
        self.root = independent_root(root)
        # 独立目录为硬约束，不能把当前旧队列目录当成新状态目录。
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "control.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            for key, value in {"version": VERSION, "role": role, "channel_id": channel_id}.items():
                db.execute("INSERT OR IGNORE INTO meta VALUES (?, ?)", (key, value))
                require(db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()[0] == value,
                        "state protocol, role or channel mismatch")
        if os.name != "nt":
            os.chmod(self.root, 0o700)
            os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=3, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    @contextlib.contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise


class ControllerStore(Store):
    def __init__(self, root, channel_id):
        super().__init__(root, "controller", channel_id)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE NOT NULL,
                digest TEXT NOT NULL, spec TEXT NOT NULL, state TEXT NOT NULL,
                claim TEXT, result TEXT, updated REAL NOT NULL)""")

    def submit(self, spec):
        validate_spec(spec)
        sha = digest(spec)
        with self.transaction() as db:
            old = db.execute("SELECT * FROM jobs WHERE key=?", (spec["key"],)).fetchone()
            if old:
                require(old["digest"] == sha, "job key already has different content", 409)
                return {"key": old["key"], "seq": old["seq"], "duplicate": True, "digest": sha}
            require(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] < MAX_JOBS,
                    "bounded channel capacity reached; preserve this channel", 507)
            cursor = db.execute("INSERT INTO jobs (key,digest,spec,state,updated) VALUES (?,?,?,?,?)",
                (spec["key"], sha, canonical(spec).decode(), "QUEUED", time.time()))
            return {"key": spec["key"], "seq": cursor.lastrowid, "duplicate": False, "digest": sha}

    def fetch(self, after):
        require(integer(after, 0, 2**63 - 1), "invalid cursor")
        with self.connect() as db:
            # 最多八个 64 KiB 命令，响应体始终低于一 MiB 上限。
            rows = db.execute("SELECT seq,digest,spec,state,claim FROM jobs WHERE seq>? ORDER BY seq LIMIT 8", (after,))
            return [{"seq": r["seq"], "digest": r["digest"], "spec": json.loads(r["spec"]),
                     "state": r["state"], "claim": r["claim"]} for r in rows]

    def status(self, key):
        require(valid_key(key), "invalid job key")
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()
        require(row is not None, "unknown job key", 404)
        spec = json.loads(row["spec"])
        return {"key": key, "seq": row["seq"], "digest": row["digest"], "state": row["state"],
                "claim": row["claim"],
                "lane": spec["lane"], "resource_keys": spec["resource_keys"], "updated": row["updated"],
                "result": json.loads(row["result"]) if row["result"] else None}

    def claim(self, key, sha, token):
        require(valid_key(key) and valid_key(token), "invalid claim")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()
            require(row is not None and row["digest"] == sha, "claim content mismatch", 409)
            if row["claim"]:
                if row["claim"] != token:
                    return {"claimed": False, "state": "REMOTE"}
                return {"claimed": row["state"] == "CLAIMED", "state": row["state"]}
            require(row["state"] == "QUEUED", "job is not queued", 409)
            spec = json.loads(row["spec"])
            if db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('CLAIMED','RUNNING','UNKNOWN')").fetchone()[0] >= 16:
                return {"claimed": False, "state": "BUSY"}
            # 中央 CAS 同时检查资源；即使误开另一状态目录，也不能重领或突破互斥。
            blockers = db.execute("SELECT spec FROM jobs WHERE key<>? AND "
                "(state IN ('CLAIMED','RUNNING','UNKNOWN') OR (state='QUEUED' AND seq<?))",
                (key, row["seq"]))
            if any(incompatible(spec, json.loads(r[0])) for r in blockers):
                return {"claimed": False, "state": "BUSY"}
            db.execute("UPDATE jobs SET state='CLAIMED',claim=?,updated=? WHERE key=?", (token, time.time(), key))
        return {"claimed": True, "state": "CLAIMED"}

    def publish(self, result):
        require(isinstance(result, dict) and set(result) == {
            "key", "digest", "claim", "state", "returncode", "stdout_b64", "stderr_b64",
            "truncated", "started_at", "finished_at", "reason"}, "invalid result fields")
        require(valid_key(result["key"]) and valid_key(result["claim"]), "invalid result identity")
        require(result["state"] in TERMINAL and type(result["truncated"]) is bool, "invalid result state")
        require(result["returncode"] is None or integer(result["returncode"], -(2**63), 2**63 - 1),
                "invalid return code")
        require(isinstance(result["reason"], str) and len(result["reason"]) <= 256, "invalid result reason")
        for field in ("started_at", "finished_at"):
            require(type(result[field]) in (int, float) and math.isfinite(result[field]) and result[field] >= 0,
                    "invalid result timestamp")
        try:
            size = sum(len(base64.b64decode(result[f], validate=True)) for f in ("stdout_b64", "stderr_b64"))
        except (ValueError, TypeError):
            raise ProtocolError("invalid result output") from None
        encoded = canonical(result).decode()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE key=?", (result["key"],)).fetchone()
            require(row is not None and row["digest"] == result["digest"], "result content mismatch", 409)
            require(size <= json.loads(row["spec"])["output_limit"], "result output limit exceeded", 413)
            if row["result"]:
                require(row["result"] == encoded, "conflicting terminal result preserved", 409)
                return {"accepted": True, "duplicate": True}
            # 没收到 claim 回应时只能保存 UNKNOWN；不得假定已执行或释放资源。
            require(row["claim"] == result["claim"] or
                    (row["claim"] is None and result["state"] == "UNKNOWN"), "result claim mismatch", 409)
            db.execute("UPDATE jobs SET state=?,claim=?,result=?,updated=? WHERE key=?",
                (result["state"], result["claim"], encoded, time.time(), result["key"]))
        return {"accepted": True, "duplicate": False}


def empty_result(spec, claim, reason, state="UNKNOWN", started=0):
    return {"key": spec["key"], "digest": digest(spec), "claim": claim, "state": state,
            "returncode": None, "stdout_b64": "", "stderr_b64": "", "truncated": False,
            "started_at": started, "finished_at": time.time(), "reason": reason}


class WorkerStore(Store):
    def __init__(self, root, channel_id):
        super().__init__(root, "worker", channel_id)
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO meta VALUES ('cursor','0')")
            db.execute("""CREATE TABLE IF NOT EXISTS inbox (
                seq INTEGER UNIQUE NOT NULL, key TEXT PRIMARY KEY, digest TEXT NOT NULL,
                spec TEXT NOT NULL, state TEXT NOT NULL, claim TEXT, result TEXT,
                sent INTEGER NOT NULL DEFAULT 0)""")
            db.execute("""CREATE TABLE IF NOT EXISTS remote_observations (
                key TEXT PRIMARY KEY, local_claim TEXT, observation TEXT NOT NULL, updated REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS publication_conflicts (
                key TEXT PRIMARY KEY, reason TEXT NOT NULL, observed REAL NOT NULL)""")

    def cursor(self):
        with self.connect() as db:
            return int(db.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()[0])

    def ingest(self, records):
        require(isinstance(records, list) and len(records) <= 8, "invalid inbox batch")
        with self.transaction() as db:
            cursor = int(db.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()[0])
            for row in records:
                require(isinstance(row, dict) and set(row) == {"seq", "digest", "spec", "state", "claim"}, "invalid inbox record")
                spec = validate_spec(row["spec"])
                require(integer(row["seq"], 1, 2**63 - 1) and row["digest"] == digest(spec), "inbox digest mismatch")
                require(row["state"] in TERMINAL | RESERVED | {"QUEUED"}, "invalid inbox state")
                require(row["claim"] is None or valid_key(row["claim"]), "invalid inbox claim")
                old = db.execute("SELECT seq,digest FROM inbox WHERE key=?", (spec["key"],)).fetchone()
                if old:
                    require(old["seq"] == row["seq"] and old["digest"] == row["digest"], "inbox conflict", 409)
                    continue
                require(row["seq"] == cursor + 1, "inbox gap; cursor preserved", 409)
                # 新收件目录也不能重领中央已有终态或执行意向；不代替原 worker 发布回执。
                state = "REMOTE" if row["state"] in {"CLAIMED", "RUNNING"} else row["state"]
                db.execute("INSERT INTO inbox (seq,key,digest,spec,state,claim,sent) VALUES (?,?,?,?,?,?,?)",
                    (row["seq"], spec["key"], row["digest"], canonical(spec).decode(), state,
                     row["claim"], int(state != "QUEUED")))
                cursor = row["seq"]
            # inbox 与 cursor 同一 FULL 同步事务；任意故障只会全部提交或全部回退。
            db.execute("UPDATE meta SET value=? WHERE key='cursor'", (str(cursor),))

    def rows(self, clause, params=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM inbox WHERE " + clause + " ORDER BY seq", params)]

    def claim(self, key):
        token = secrets.token_hex(16)
        with self.transaction() as db:
            require(db.execute("UPDATE inbox SET state='CLAIMED',claim=? WHERE key=? AND state='QUEUED'",
                               (token, key)).rowcount == 1, "local claim conflict", 409)
        return token

    def unclaim_busy(self, key, token):
        # 只有中央明确 BUSY 且未启动，才可回到排队态。
        with self.transaction() as db:
            require(db.execute("UPDATE inbox SET state='QUEUED',claim=NULL WHERE key=? AND claim=? AND state='CLAIMED'",
                               (key, token)).rowcount == 1, "local busy transition conflict", 409)

    def running(self, key, token):
        with self.transaction() as db:
            require(db.execute("UPDATE inbox SET state='RUNNING' WHERE key=? AND claim=? AND state='CLAIMED'",
                               (key, token)).rowcount == 1, "local start transition conflict", 409)

    def foreign_claim(self, key, token):
        # 中央明确原任务已归他方，不依赖紧接的 status 一定联网成功。
        with self.transaction() as db:
            require(db.execute("UPDATE inbox SET state='REMOTE',claim=NULL,sent=1 "
                               "WHERE key=? AND claim=? AND state='CLAIMED' AND result IS NULL",
                               (key, token)).rowcount == 1, "foreign claim transition conflict", 409)
            db.execute("INSERT OR IGNORE INTO remote_observations VALUES (?,?,?,?)",
                       (key, token, canonical({"key": key, "state": "REMOTE"}).decode(), time.time()))

    def observe_remote(self, key, status, rejected_claim=None):
        """只观察中央已属其他意向的任务；不制造本地执行结果或释放本地未知执行。"""
        require(isinstance(status, dict) and status.get("key") == key and
                status.get("state") in TERMINAL | {"CLAIMED", "RUNNING"} and
                valid_key(status.get("claim")), "invalid remote observation", 409)
        observation = {name: status[name] for name in ("key", "seq", "digest", "state", "claim", "updated")}
        with self.transaction() as db:
            row = db.execute("SELECT * FROM inbox WHERE key=?", (key,)).fetchone()
            require(row is not None and row["digest"] == status["digest"] and row["seq"] == status["seq"],
                    "remote observation identity mismatch", 409)
            require(row["result"] is None and (row["state"] == "REMOTE" or
                    (row["state"] == "CLAIMED" and rejected_claim == row["claim"] and
                     rejected_claim != status["claim"])), "cannot replace a local execution intent", 409)
            state = status["state"] if status["state"] in TERMINAL else "REMOTE"
            db.execute("UPDATE inbox SET state=?,claim=?,sent=1 WHERE key=?", (state, status["claim"], key))
            db.execute("INSERT INTO remote_observations VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                       "observation=excluded.observation,updated=excluded.updated",
                       (key, rejected_claim, canonical(observation).decode(), time.time()))

    def complete(self, result):
        value = canonical(result).decode()
        with self.transaction() as db:
            row = db.execute("SELECT claim,result FROM inbox WHERE key=?", (result["key"],)).fetchone()
            require(row is not None and row["claim"] == result["claim"], "local result claim mismatch", 409)
            if row["result"]:
                require(row["result"] == value, "local result conflict", 409)
                return
            db.execute("UPDATE inbox SET state=?,result=? WHERE key=?", (result["state"], value, result["key"]))

    def recover(self):
        recovered = []
        for row in self.rows("state IN ('CLAIMED','RUNNING')"):
            result = empty_result(json.loads(row["spec"]), row["claim"], "dispatcher restarted after durable claim; never replay")
            self.complete(result)
            recovered.append(result["key"])
        return recovered

    def mark_sent(self, key):
        with self.transaction() as db:
            db.execute("UPDATE inbox SET sent=1 WHERE key=? AND result IS NOT NULL", (key,))

    def quarantine_result(self, key, reason):
        # 原 result 永久保留；明确冲突单独落盘，不把拒收记成已交付。
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO publication_conflicts VALUES (?,?,?)", (key, reason[:256], time.time()))
            db.execute("UPDATE inbox SET sent=-1 WHERE key=? AND result IS NOT NULL AND sent=0", (key,))


class ControlServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, store, secret, channel_id, allowed=("127.0.0.1/32",)):
        self.store, self.secret, self.channel_id = store, secret, channel_id
        self.allowed = [ipaddress.ip_network(n, strict=False) for n in allowed]
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def dispatch(self, op, payload):
        require(isinstance(payload, dict), "invalid payload")
        if op == "capabilities":
            return {"version": VERSION, "channel_id": self.channel_id,
                    "capabilities": ["durable-inbox", "claim-before-start", "resource-keys", "serial-barrier",
                                     "direct-status", "idempotent-result", "restart-unknown", "windows-job-object"],
                    "max_output_bytes": MAX_OUTPUT, "max_jobs": MAX_JOBS, "max_claims": 16, "default_lane": "serial"}
        if op == "submit":
            return self.store.submit(payload)
        if op == "status":
            return self.store.status(payload.get("key"))
        if op in {"fetch", "claim", "result"}:
            require(payload.get("channel_id") == self.channel_id, "wrong worker channel", 403)
        if op == "fetch":
            return {"records": self.store.fetch(payload.get("after"))}
        if op == "claim":
            return self.store.claim(payload.get("key"), payload.get("digest"), payload.get("claim"))
        if op == "result":
            return self.store.publish(payload.get("result"))
        raise ProtocolError("unsupported operation")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def respond(self, status, value):
        raw = canonical(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        nonce = None
        try:
            require(any(ipaddress.ip_address(self.client_address[0]) in net for net in self.server.allowed),
                    "peer not allowed", 403)
            require(self.path == "/v1", "unknown endpoint", 404)
            require(self.headers.get("Transfer-Encoding") is None, "chunked body refused")
            size = int(self.headers.get("Content-Length", "0"))
            require(0 < size <= MAX_FRAME, "invalid request size", 413)
            envelope = verified(self.server.secret, json.loads(self.rfile.read(size)))
            require(set(envelope) == {"version", "op", "payload", "sent_at", "nonce"} and
                    envelope["version"] == VERSION, "unsupported envelope")
            require(integer(envelope["sent_at"], 0, 2**63 - 1) and
                    abs(time.time() - envelope["sent_at"]) <= 300, "stale request", 401)
            require(valid_key(envelope["nonce"]), "invalid nonce")
            nonce = envelope["nonce"]
            response, code = self.server.dispatch(envelope["op"], envelope["payload"]), 200
        except ProtocolError as exc:
            response, code = {"error": str(exc)}, exc.code
        except (ValueError, TypeError, KeyError, sqlite3.Error):
            response, code = {"error": "invalid request or unavailable durable state"}, 400
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        try:
            self.respond(code, signed(self.server.secret, {"version": VERSION, "nonce": nonce,
                                                         "status": code, "payload": response}))
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass


class Client:
    def __init__(self, url, secret, channel_id=None, timeout=5):
        parsed = urllib.parse.urlsplit(url)
        require(parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and
                not parsed.password and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment,
                "server must be an HTTP(S) origin")
        self.url, self.secret, self.channel_id, self.timeout = url.rstrip("/") + "/v1", secret, channel_id, timeout
        # 私有直达通道不沿系统 HTTP_PROXY，且不接受跨 origin 重定向。
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args, **_kwargs):
                return None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, op, payload):
        nonce = secrets.token_hex(16)
        envelope = signed(self.secret, {"version": VERSION, "op": op, "payload": payload,
                                       "sent_at": int(time.time()), "nonce": nonce})
        request = urllib.request.Request(self.url, data=canonical(envelope), headers={"Content-Type": "application/json"})
        try:
            response = self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            raw = response.read(MAX_FRAME + 1)
            require(len(raw) <= MAX_FRAME, "response too large", 413)
            value = verified(self.secret, json.loads(raw))
            require(value.get("version") == VERSION and value.get("nonce") == nonce and
                    value.get("status") == response.code, "response binding mismatch", 401)
            if response.code != 200:
                raise ProtocolError(value.get("payload", {}).get("error", "remote error"), response.code)
            return value["payload"]

    def worker_call(self, op, **payload):
        return self.call(op, dict(payload, channel_id=self.channel_id))


class WindowsJob:
    """启动门闩前加入 Job Object，禁止命令先逃出本次资源所有权。"""
    def __init__(self, process):
        import ctypes
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]
        self.ctypes, self.kernel = ctypes, ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes, self.kernel.CreateJobObjectW.restype = [ctypes.c_void_p, w.LPCWSTR], w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.kernel.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        require(bool(self.handle), "Windows Job Object creation failed")
        limits = Extended()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not (self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) and
                self.kernel.AssignProcessToJobObject(self.handle, w.HANDLE(int(process._handle)))):
            self.close()
            raise ProtocolError("Windows Job Object assignment failed before command start")

    def finish(self):
        self.kernel.TerminateJobObject(self.handle, 124)
        # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION 的四个 LARGE_INTEGER + 四个 DWORD。
        buf = self.ctypes.create_string_buffer(48)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not self.kernel.QueryInformationJobObject(self.handle, 1, buf, len(buf), None):
                return False
            active = int.from_bytes(buf.raw[40:44], "little")
            if active == 0:
                return True
            time.sleep(0.02)
        return False

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def execute(spec, claim, root):
    start = time.time()
    result = empty_result(spec, claim, "", state="FAILED", started=start)
    process, job = None, None
    buffers = [bytearray(), bytearray()]
    buffer_lock = threading.Lock()
    truncated = [False]
    readers = []

    def drain(pipe, target):
        try:
            while chunk := pipe.read(8192):
                with buffer_lock:
                    remaining = spec["output_limit"] - sum(map(len, buffers))
                    target.extend(chunk[:remaining])
                    truncated[0] |= len(chunk) > remaining
        except (OSError, ValueError):
            pass
        finally:
            pipe.close()

    try:
        work = Path(root) / "work" / spec["key"]
        scratch = work / "scratch"
        scratch.mkdir(parents=True, exist_ok=False)
        env = dict(os.environ, TMP=str(scratch), TEMP=str(scratch), TMPDIR=str(scratch),
                   XDEV_CONTROL_JOB_ID=spec["key"], PYTHONDONTWRITEBYTECODE="1")
        process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "_child", "--",
                                    *spec["body"]["argv"]], cwd=work, env=env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=os.name != "nt", close_fds=True)
        if os.name == "nt":
            job = WindowsJob(process)
        for pipe, target in zip((process.stdout, process.stderr), buffers):
            reader = threading.Thread(target=drain, args=(pipe, target), daemon=True)
            reader.start()
            readers.append(reader)
        process.stdin.write(b"G")
        process.stdin.close()
        try:
            result["returncode"] = process.wait(timeout=spec["timeout"])
            result["state"] = "SUCCEEDED" if result["returncode"] == 0 else "FAILED"
        except subprocess.TimeoutExpired:
            if job:
                clean = job.finish()
                result["state"] = "TIMED_OUT" if clean else "UNKNOWN"
            else:
                # 本机离线用自有进程组；跨平台候选不宣称能证明 Unix 逃逸后代已退出。
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                result["state"] = "UNKNOWN"
            result["reason"] = "execution timeout; only this invocation was terminated"
            result["returncode"] = process.wait(timeout=5)
        if job and not job.finish():
            result.update(state="UNKNOWN", reason="owned process tree termination not confirmed")
        for reader in readers:
            reader.join(timeout=2)
        if any(reader.is_alive() for reader in readers):
            result.update(state="UNKNOWN", reason="output pipe still owned by a descendant")
    except Exception as exc:
        result.update(state="UNKNOWN" if process else "FAILED", reason=type(exc).__name__ + ": supervised execution failed")
    finally:
        if job:
            job.close()
        if process and process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                result.update(state="UNKNOWN", reason="direct child termination not confirmed")
        for reader in readers:
            reader.join(timeout=0.1)
        if process and not any(reader.is_alive() for reader in readers):
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe and not pipe.closed:
                    pipe.close()
        with buffer_lock:
            result.update(stdout_b64=base64.b64encode(buffers[0]).decode(),
                          stderr_b64=base64.b64encode(buffers[1]).decode(), truncated=truncated[0], finished_at=time.time())
    return result


class Worker:
    def __init__(self, state, client, channel_id, max_workers=1):
        require(integer(max_workers, 1, 16), "max-workers must be 1..16")
        self.lock = StateLock(state)
        try:
            self.store = WorkerStore(state, channel_id)
            self.store.recover()
        except BaseException:
            self.lock.close()
            raise
        self.client, self.max_workers = client, max_workers
        self.threads = {}
        self.errors = []
        self.outbox_after = 0
        self.remote_after = 0

    @staticmethod
    def rotate(rows, after, limit=8):
        return ([row for row in rows if row["seq"] > after] +
                [row for row in rows if row["seq"] <= after])[:limit]

    def flush_results(self):
        for row in self.rotate(self.store.rows("result IS NOT NULL AND sent=0"), self.outbox_after):
            self.outbox_after = row["seq"]
            try:
                self.client.worker_call("result", result=json.loads(row["result"]))
                self.store.mark_sent(row["key"])
            except Exception as exc:
                self.record_error(exc)
                if isinstance(exc, ProtocolError) and exc.code == 409:
                    self.store.quarantine_result(row["key"], str(exc))

    def refresh_remote(self):
        for row in self.rotate(self.store.rows("state='REMOTE'"), self.remote_after):
            self.remote_after = row["seq"]
            try:
                status = self.client.call("status", {"key": row["key"]})
                self.store.observe_remote(row["key"], status)
            except Exception as exc:
                self.record_error(exc)

    def rejected_foreign_claim(self, key, token):
        try:
            status = self.client.call("status", {"key": key})
            self.store.observe_remote(key, status, rejected_claim=token)
            return True
        except Exception as exc:
            self.record_error(exc)
            return False

    def record_error(self, exc):
        self.errors = (self.errors + [type(exc).__name__ + ": " + str(exc)[:200]])[-8:]

    def step(self, dispatch=True):
        self.threads = {key: thread for key, thread in self.threads.items() if thread.is_alive()}
        self.flush_results()
        self.refresh_remote()
        if not dispatch:
            return
        try:
            records = self.client.worker_call("fetch", after=self.store.cursor())["records"]
            self.store.ingest(records)
        except Exception as exc:
            self.record_error(exc)
            return
        for row in self.store.rows("state='QUEUED'"):
            if len(self.threads) >= self.max_workers:
                break
            spec = json.loads(row["spec"])
            # 本地过滤减少无效 HTTP；中央仍是跨 dispatcher 的最终互斥点。
            if any(incompatible(spec, json.loads(other["spec"])) for other in self.store.rows(
                    "state IN ('CLAIMED','RUNNING','UNKNOWN','REMOTE')")):
                continue
            token = self.store.claim(row["key"])
            try:
                response = self.client.worker_call("claim", key=row["key"], digest=row["digest"], claim=token)
                if response == {"claimed": False, "state": "BUSY"}:
                    self.store.unclaim_busy(row["key"], token)
                    continue
                if response == {"claimed": False, "state": "REMOTE"}:
                    self.store.foreign_claim(row["key"], token)
                    continue
                require(response == {"claimed": True, "state": "CLAIMED"}, "claim not acknowledged", 409)
                self.store.running(row["key"], token)
                thread = threading.Thread(target=self.run_one, args=(spec, token), daemon=True)
                self.threads[row["key"]] = thread
                thread.start()
            except Exception as exc:
                self.record_error(exc)
                if isinstance(exc, ProtocolError) and exc.code == 409 and self.rejected_foreign_claim(row["key"], token):
                    continue
                self.store.complete(empty_result(spec, token, "claim/start uncertain; never replay"))

    def run_one(self, spec, token):
        try:
            self.store.complete(execute(spec, token, self.store.root))
        except Exception as exc:
            self.record_error(exc)
            # 持久化失败保留 RUNNING；重启只会成为 UNKNOWN。

    def close(self):
        # 正常停止只等待本 dispatcher 已领任务，不停止其他进程或重置游标。
        for thread in self.threads.values():
            thread.join()
        self.flush_results()
        self.lock.close()


def make_spec(args):
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    return validate_spec({"version": VERSION, "op": "exec", "key": args.id,
        "lane": "independent" if args.independent else "serial", "resource_keys": sorted(set(args.resource)),
        "body": {"argv": argv}, "timeout": args.timeout, "output_limit": args.output_limit})


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["_child"]:
        require(argv[1:2] == ["--"] and len(argv) > 2, "invalid gated child")
        if sys.stdin.buffer.read(1) != b"G":
            return 125
        # Windows 的 os.execvpe 会让门闩进程提前退出。等待实际 argv；Job Object
        # 已含当前父进程，真实命令及后代会继承归属，保留原 argv 和真实退出码。
        return subprocess.call(argv[2:], stdin=subprocess.DEVNULL, close_fds=True)
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "worker", "run", "status", "capabilities"):
        command = subs.add_parser(name)
        command.add_argument("--secret-file", required=True, type=Path)
        if name != "serve":
            command.add_argument("--url", required=True, help="independent server HTTP(S) origin")
        if name in {"serve", "worker"}:
            command.add_argument("--state", required=True, type=Path, help="new private directory, never legacy state")
            command.add_argument("--channel-id", required=True, help="same explicit identifier at both endpoints")
        if name == "serve":
            command.add_argument("--host", default="127.0.0.1")
            command.add_argument("--port", type=int, default=8771)
            command.add_argument("--allow-ip", action="append", default=[])
        elif name == "worker":
            command.add_argument("--max-workers", type=int, default=1)
            command.add_argument("--poll", type=float, default=0.5)
            command.add_argument("--seconds", type=float, default=3600, help="stop fetching after this budget; drain owned work")
            command.add_argument("--allow-local-test", action="store_true", help="explicitly allow non-Windows offline test worker")
        elif name == "run":
            command.add_argument("--id", required=True, help="persist and reuse this id on timeout; never submit a replacement")
            command.add_argument("--independent", action="store_true")
            command.add_argument("--resource", action="append", default=[])
            command.add_argument("--timeout", type=int, default=60)
            command.add_argument("--output-limit", type=int, default=64 * 1024)
            command.add_argument("--wait", type=float, default=0, help="observer budget only; timeout never resubmits")
            command.add_argument("argv", nargs=argparse.REMAINDER, help="-- executable arg ... (runs in a unique cwd)")
        elif name == "status":
            command.add_argument("--id", required=True)
    args = parser.parse_args(argv)
    secret = read_secret(args.secret_file)
    if args.command == "serve":
        require(valid_key(args.channel_id), "invalid channel id")
        with StateLock(args.state):
            store = ControllerStore(args.state, args.channel_id)
            server = ControlServer((args.host, args.port), store, secret, args.channel_id,
                                   args.allow_ip or ["127.0.0.1/32"])
            print(json.dumps({"version": VERSION, "state": str(args.state.resolve()),
                              "listen_port": server.server_port, "channel_id": args.channel_id}), flush=True)
            try:
                server.serve_forever(poll_interval=0.25)
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
        return 0
    client = Client(args.url, secret, getattr(args, "channel_id", None))
    if args.command == "capabilities":
        print(json.dumps(client.call("capabilities", {}), indent=2))
    elif args.command == "status":
        print(json.dumps(client.call("status", {"key": args.id}), indent=2))
    elif args.command == "run":
        require(math.isfinite(args.wait) and 0 <= args.wait <= 86400, "invalid wait budget")
        # 先验证新协议，绝不回退或写入旧服务。
        require(client.call("capabilities", {})["version"] == VERSION, "protocol negotiation failed")
        receipt = client.call("submit", make_spec(args))
        print(json.dumps(dict(receipt, version=VERSION)), flush=True)
        if args.wait:
            deadline = time.monotonic() + args.wait
            while time.monotonic() < deadline:
                result = client.call("status", {"key": args.id})
                if result["state"] in TERMINAL:
                    print(json.dumps(result), flush=True)
                    return 0 if result["state"] == "SUCCEEDED" else 1
                time.sleep(min(0.25, max(0, deadline - time.monotonic())))
            print(json.dumps({"key": args.id, "state": "OBSERVER_TIMEOUT", "action": "status original id"}))
            return 3
    elif args.command == "worker":
        require(os.name == "nt" or args.allow_local_test, "Windows worker required; non-Windows needs --allow-local-test")
        require(valid_key(args.channel_id), "invalid channel id")
        require(math.isfinite(args.poll) and 0.05 <= args.poll <= 30 and
                math.isfinite(args.seconds) and 0 < args.seconds <= 86400, "invalid worker budget")
        caps = client.call("capabilities", {})
        require(caps["version"] == VERSION and caps["channel_id"] == args.channel_id, "worker negotiation mismatch")
        worker = Worker(args.state, client, args.channel_id, args.max_workers)
        print(json.dumps({"version": VERSION, "worker": "started", "max_workers": args.max_workers}), flush=True)
        try:
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline:
                worker.step()
                time.sleep(args.poll)
        except KeyboardInterrupt:
            pass
        finally:
            worker.close()
        print(json.dumps({"worker": "stopped", "cursor": worker.store.cursor(), "errors": worker.errors}), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ProtocolError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"error": type(exc).__name__ + ": " + str(exc)[:200]}), file=sys.stderr)
        raise SystemExit(2)
