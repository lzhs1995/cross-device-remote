#!/usr/bin/env python3
"""离线检查 xdev mplus --native：不连 Windows、不跑 driver 或 Mplus。绑定（含 Python/psutil 身份）、driver plan/config
字段、批次限定的 attempt_id、任务正文、状态作业和收尾都用替身；收尾用按 successor03 driver 字节格式模拟的 journal，
覆盖 terminal 缺失/外批/pid 不符、journal 取回失败或外批、成员重复、多余文件、事件篡改、rc 不符、未决成员、SHA 不符、
.out 失败、pull 124 不重取、不重提。"""
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path, PureWindowsPath

HERE = Path(__file__).resolve().parent
tmp = Path(tempfile.mkdtemp(prefix="xdev-native-test-"))
(tmp / "state" / "files").mkdir(parents=True)
cfg = tmp / "config.json"
cfg.write_text(json.dumps({"state_dir": str(tmp / "state"), "windows": {"mplus": "C:\\M\\Mplus.exe"}}), encoding="utf-8")
os.environ["XDEV_CONFIG"] = str(cfg)
sys.path.insert(0, str(HERE))
import xdev_mplus_batch as b  # noqa: E402
import xdev_mplus_native as n  # noqa: E402
import xdev_tasks  # noqa: E402

OWNER = "0123456789abcdef0123456789abcdef"
NORMAL = "INPUT READING TERMINATED NORMALLY\nTHE MODEL ESTIMATION TERMINATED NORMALLY\n"
BAD_OUT = NORMAL + "*** ERROR in MODEL command\n"
WORK = "C:\\XW\\work"
REQUEST_FIELDS = {"original_controller", "attempt_id", "request_id", "job_id", "model_id", "route", "working_directory",
                  "temp_directory", "input_sha256", "data_sha256", "exe_path", "exe_sha256", "physical_bytes", "commit_bytes",
                  "processors", "output_budget_bytes", "temp_budget_bytes"}  # 与 W1 adapter _requests 的字段集相同
CONFIG_FIELDS = {"schema", "helper_module", "provider_directory", "registry", "original_controller", "batch_id", "policy_id",
                 "policy", "external_evidence", "max_parallel", "overhead"}  # 与 W1 adapter 构造器的字段集相同


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def refused(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except ValueError:
        return True
    return False


PY = {"path": "C:\\XP\\python.exe", "sha256": "d" * 64, "version": "3.12.7", "architecture": "AMD64",
      "psutil": {"version": "6.1.0", "path": "C:\\XP\\Lib\\site-packages\\psutil"}}
BINDING = {"schema": n.BINDING_SCHEMA, "work_root": WORK, "python": PY,
           "driver": {"path": "C:\\XN\\native_batch_driver.py", "sha256": n.PINS["driver"]},
           "adapter": {"path": "C:\\XN\\general_batch_admission.py", "sha256": n.PINS["adapter"]},
           "backend": {"path": "C:\\XN\\win_process.py", "sha256": n.PINS["backend"]},
           "engine": {"exe_path": "C:\\XM\\Mplus.exe", "exe_sha256": "e" * 64},
           "admission": {"helper_module": "C:\\XN\\host_admission_adapter.py", "provider_directory": "C:\\XN\\providers",
                         "registry": "C:\\XR\\reservations.sqlite", "original_controller": "controller-1", "policy_id": "policy-1",
                         "policy": {"schema": "host-measured-policy-v1", "host_id": "host-1",
                                    "volume_reserve_bytes": {"C:\\": 20_000_000_000}},
                         "external_evidence": {"path": "C:\\XN\\window.json", "sha256": "f" * 64},
                         "overhead": {"physical_bytes": 1, "commit_bytes": 1, "processors": 1, "volume_bytes": {"C:\\": 1}}},
           "model_budget": {"physical_bytes": 2 ** 30, "commit_bytes": 2 ** 30, "output_budget_bytes": 2 ** 20,
                            "temp_budget_bytes": 2 ** 20}}


def write_binding(name, mutate=None):
    d = copy.deepcopy(BINDING)
    if mutate:
        mutate(d)
    p = tmp / name
    p.write_text(json.dumps(d), encoding="utf-8")
    return str(p)


# 绑定：私有部署 JSON 的结构、W1 审过的 SHA 配对、本地绝对路径、C: 保留量不低于 20e9 B
GOOD = write_binding("good.json")
good = n.load_binding(GOOD)
assert good["binding"]["work_root"] == WORK and good["sha256"] == hashlib.sha256(Path(GOOD).read_bytes()).hexdigest()
assert good["binding"]["python"] == "C:\\XP\\python.exe" and good["binding"]["python_pin"] == {
    "sha256": "d" * 64, "version": "3.12.7", "architecture": "AMD64", "psutil_version": "6.1.0",
    "psutil_path": "C:\\XP\\Lib\\site-packages\\psutil"}
bad_bindings = {
    "pyold": lambda d: d.update(python={"path": "C:\\XP\\python.exe"}),          # 只有路径的旧绑定不再接受
    "pynosha": lambda d: d["python"].pop("sha256"),
    "pysha": lambda d: d["python"].update(sha256="D" * 64),
    "pyver": lambda d: d["python"].update(version="3.12"),
    "pyarch": lambda d: d["python"].update(architecture="AMD 64"),
    "pyextra": lambda d: d["python"].update(prefix="C:\\XP"),
    "psutilnover": lambda d: d["python"]["psutil"].pop("version"),
    "psutilver": lambda d: d["python"]["psutil"].update(version="6"),
    "psutilpath": lambda d: d["python"]["psutil"].update(path="psutil"),
    "psutilextra": lambda d: d["python"]["psutil"].update(file="C:\\XP\\psutil\\__init__.py"),
    "pin": lambda d: d["driver"].update(sha256="0" * 64),
    "oldadapter": lambda d: d["adapter"].update(sha256="33c4b27dc654f0ba250f9c2abbaa9aafa7114884b4240ccc20b4e40032ea86c8"),
    "envvar": lambda d: d.update(work_root="%LOCALAPPDATA%\\xdev\\work"),
    "unc": lambda d: d["python"].update(path="\\\\host\\share\\python.exe"),
    "dotdot": lambda d: d["adapter"].update(path="C:\\XN\\..\\x.py"),
    "relative": lambda d: d["backend"].update(path="win_process.py"),
    "stream": lambda d: d["admission"].update(registry="C:\\XR\\r.sqlite:alt"),
    "quote": lambda d: d.update(work_root='C:\\X"W'),
    "extra": lambda d: d.update(priority="Normal"),
    "missing": lambda d: d.pop("model_budget"),
    "schema": lambda d: d.update(schema="xdev-mplus-native-binding-v0"),
    "engine": lambda d: d["engine"].update(exe_path="C:\\XM\\mpdemo.exe"),
    "enginesha": lambda d: d["engine"].update(exe_sha256="E" * 64),
    "floor": lambda d: d["admission"]["policy"]["volume_reserve_bytes"].update({"C:\\": 19_999_999_999}),
    "nofloor": lambda d: d["admission"]["policy"].pop("volume_reserve_bytes"),
    "budget0": lambda d: d["model_budget"].update(physical_bytes=0),
    "budgetbool": lambda d: d["model_budget"].update(commit_bytes=True),
    "budgetfloat": lambda d: d["model_budget"].update(temp_budget_bytes=1.5),
    "evidence": lambda d: d["admission"]["external_evidence"].update(sha256="F" * 64),
    "controller": lambda d: d["admission"].update(original_controller=""),
    "admextra": lambda d: d["admission"].update(max_parallel=2),
}
for label, mutate in bad_bindings.items():
    assert refused(n.load_binding, write_binding(label + ".json", mutate)), label
(tmp / "notjson.json").write_text("{", encoding="utf-8")
os.symlink(tmp / "good.json", tmp / "link.json")
assert refused(n.load_binding, str(tmp / "notjson.json")) and refused(n.load_binding, str(tmp / "link.json"))
assert refused(n.load_binding, str(tmp / "absent.json"))

# deadline：必须显式给、0 < S <= 180（driver 上限），不默认、不静默截断
for bad in (None, 0, -1, 180.001, 181, float("nan"), float("inf"), True, "60"):
    assert refused(n.deadline, bad), bad
assert n.deadline(180) == 180 and n.deadline(0.5) == 0.5

# driver plan 与 admission config：字段集与 W1 driver/adapter 完全一致；每模型独占 cwd/tmp 兄弟目录
P = tmp / "p"
P.mkdir()
for name in ("m1.inp", "m2.inp", "m 3.inp", "_m4.inp"):
    (P / name).write_text("TITLE: t;\nDATA: FILE = d.dat;\nANALYSIS: PROCESSORS = 2;\n", encoding="ascii")
(P / "d.dat").write_text("1 2\n", encoding="ascii")
M1, M2, D = str(P / "m1.inp"), str(P / "m2.inp"), str(P / "d.dat")
pl, nplan, nconfig = n.native_plan([M1, M2], [D], good, 60)
W = WORK + "\\" + pl["batch"]
assert set(nplan) == {"schema", "batch_id", "journal_directory", "models"} and nplan["schema"] == n.PLAN_SCHEMA
assert nplan["batch_id"] == pl["batch"] == nconfig["batch_id"] and xdev_tasks._valid_name(pl["batch"])
assert pl["work_dir"] == W and nplan["journal_directory"] == W + "\\driver-evidence" and pl["remote_dir"] == xdev_tasks._work_dir(pl["batch"])
for row, m in zip(nplan["models"], pl["models"]):
    assert set(row) == {"request", "input_source", "input_name", "data_sources", "deadline_seconds"}
    r, a = row["request"], m["key"]
    assert set(r) == REQUEST_FIELDS and r["attempt_id"] == m["attempt_id"] == f"{pl['batch']}.{a}" and r["route"] == "DIRECT_MPLUS"
    assert r["original_controller"] == "controller-1" and r["model_id"] == a
    assert r["request_id"] == f"{pl['batch']}.{a}" and r["job_id"] == f"{pl['batch']}/{a}"
    assert r["working_directory"] == f"{W}\\models\\{a}\\cwd" == m["working_directory"] and r["temp_directory"] == f"{W}\\models\\{a}\\tmp"
    assert row["input_source"] == f"{W}\\{m['inp']}" and row["input_name"] == m["inp"] and row["deadline_seconds"] == 60
    assert row["data_sources"] == {"d.dat": f"{W}\\d.dat"} and r["data_sha256"] == {"d.dat": xdev_tasks._input_digest(P / "d.dat")[1]}
    assert r["input_sha256"] == xdev_tasks._input_digest(Path(m["local_inp"]))[1] == m["input_sha256"]
    assert r["processors"] == 2 and r["exe_path"] == "C:\\XM\\Mplus.exe" and r["exe_sha256"] == "e" * 64
    assert r["physical_bytes"] == 2 ** 30 and r["output_budget_bytes"] == 2 ** 20
for f in ("attempt_id", "request_id", "job_id"):
    assert len({row["request"][f] for row in nplan["models"]}) == 2, f
# attempt_id 带批次名：adapter 以 (controller, attempt_id) 为永久主键，同一模型在下一批必须换 id；driver 限 101 字符
pl_next, nplan_next, _ = n.native_plan([M1, M2], [D], good, 60)
ids_first = [row["request"]["attempt_id"] for row in nplan["models"]]
ids_next = [row["request"]["attempt_id"] for row in nplan_next["models"]]
assert pl_next["batch"] != pl["batch"] and not set(ids_first) & set(ids_next)
assert [i.split(".", 1)[1] for i in ids_first] == [i.split(".", 1)[1] for i in ids_next] == ["m1", "m2"]
long_key = "k" * 60
long_id = n.attempt_id(pl["batch"], long_key)
assert len(pl["batch"]) == 50 and len(long_id) == 101 and n.SAFE_ATTEMPT.fullmatch(long_id) and long_id.startswith(pl["batch"] + ".k")
assert long_id.endswith("-" + hashlib.sha256(long_key.encode()).hexdigest()[:8])
assert n.attempt_id(pl["batch"], "k" * 59 + "j") != long_id                  # 截短后按全名 SHA 仍能区分
assert n.attempt_id(pl["batch"], "k" * 50) == pl["batch"] + "." + "k" * 50   # 放得下就不截
assert refused(n.attempt_id, "b" * 81, "m1") and refused(n.attempt_id, pl["batch"], "bad key")
(P / "long").mkdir()
(P / "long" / (long_key + ".inp")).write_text("TITLE: t;\n", encoding="ascii")
pl_long, nplan_long, _ = n.native_plan([str(P / "long" / (long_key + ".inp"))], [], good, 60)
assert nplan_long["models"][0]["request"]["attempt_id"] == n.attempt_id(pl_long["batch"], long_key) == pl_long["models"][0]["attempt_id"]
assert set(nconfig) == CONFIG_FIELDS and nconfig["schema"] == n.CONFIG_SCHEMA and nconfig["max_parallel"] == 2
assert all(nconfig[k] == BINDING["admission"][k] for k in n.ADMISSION_KEYS)  # 策略、窗口、provider 原样转交 adapter 核验
assert pl["capacity_scope"] == "GENERAL_BATCH_ADMISSION" and pl["max_parallel"] == 2 and pl["deadline_seconds"] == 60
assert "external_host_admission_enforced_by_xdev" not in pl and "host_reservations" not in pl
assert n.native_plan([M1, M2], [D], good, 60, jobs=1)[2]["max_parallel"] == 1
assert refused(n.native_plan, [M1, M2], [D], good, 60, jobs=3)            # K <= 批大小
assert refused(n.native_plan, [M1], [D], good, None) and refused(n.native_plan, [M1], [D], good, 181)
assert refused(n.native_plan, [str(P / "m 3.inp")], [], good, 60)          # driver 的 input_name 规则
assert refused(n.native_plan, [str(P / "_m4.inp")], [], good, 60)
# 共用输入名：ASCII、不与模型输出或正文的桥接回执/临时文件/stdout/stderr 同名。
for bad_name in ("d d.dat", "m1.out", "_xdev_native_plan.json", "xdev_native_wrapper.json",
                 "xdev_native_wrapper.json.part", "driver.stdout", "driver.stderr"):
    (P / "x").mkdir(exist_ok=True)
    (P / "x" / bad_name).write_text("1\n", encoding="ascii")
    assert refused(n.native_plan, [M1], [str(P / "x" / bad_name)], good, 60), bad_name
small = n.load_binding(write_binding("small.json", lambda d: d["model_budget"].update(output_budget_bytes=10)))
assert refused(n.native_plan, [M1], [D], small, 60)                     # 暂存输入超出每模型输出预算
many = []
for i in range(100):
    (P / f"k{i}.inp").write_text("TITLE: t;\n", encoding="ascii")
    many.append(str(P / f"k{i}.inp"))
extra = []
for i in range(27):
    (P / f"x{i}.dat").write_text("1\n", encoding="ascii")
    extra.append(str(P / f"x{i}.dat"))
assert refused(n.native_plan, many, extra, good, 60)                    # 两个控制文件也算 W1 task 的 128 个输入
assert n.native_plan(many, extra[:26], good, 60)

# 任务正文：只核身份/依赖/SHA 后同步调用 driver 一次；不起 Mplus、不碰预约、不设优先级
body = n.native_command(pl, good, "a" * 64, "b" * 64)
assert body.startswith("# XDEV_MPLUS_NATIVE_BODY") and "__CFG__" not in body and len(body.encode()) < 60 * 1024
assert body.count("Start-Process") == 1 and "Start-Process -FilePath $cfg.python" in body and "mplus.exe" not in body.lower()
cfg_json = json.loads(body.split("$cfg = (", 1)[1].split(" | ConvertFrom-Json)", 1)[0][1:-1].replace("''", "'"))
assert [p["role"] for p in cfg_json["pins"]] == ["driver", "adapter", "backend", "plan", "config"]
assert [p["sha256"] for p in cfg_json["pins"]] == [n.PINS["driver"], n.PINS["adapter"], n.PINS["backend"], "a" * 64, "b" * 64]
assert cfg_json["argline"] == (f'-B "C:\\XN\\native_batch_driver.py" --plan "{W}\\_xdev_native_plan.json"'
                               f' --admission-config "{W}\\_xdev_native_admission.json"'
                               ' --adapter-module "C:\\XN\\general_batch_admission.py" --backend-module "C:\\XN\\win_process.py"')
assert cfg_json["work_dir"] == W and cfg_json["journal"] == W + "\\driver-evidence" and cfg_json["batch_id"] == pl["batch"]
assert cfg_json["python"] == "C:\\XP\\python.exe" and cfg_json["python_pin"] == good["binding"]["python_pin"]
assert cfg_json["probe"] == n.PYTHON_PROBE and cfg_json["driver_path"] == "C:\\XN\\native_batch_driver.py" and '"' not in n.PYTHON_PROBE
for marker in ("WORK_DIR_MISMATCH", "TASK_NAME_IS_NOT_BATCH_ID", "SHA_MISMATCH", "PYTHON_MISSING", "JOURNAL_EXISTS_NO_REPLAY",
               "MODELS_DIR_EXISTS_NO_REPLAY", "[NullString]::Value", "$p.WaitForExit()", "exit ([int]$code)", "DRIVER_EXIT_UNKNOWN",
               "PYTHON_SHA_MISMATCH", "PYTHON_PROBE_FAILED", "PYTHON_PROBE_UNREADABLE", "PYTHON_OR_PSUTIL_MISMATCH",
               "& $cfg.python -B -c $cfg.probe $cfg.driver_path"):
    assert marker in body, marker
assert body.index("PYTHON_SHA_MISMATCH") < body.index("$cfg.probe") < body.index("PYTHON_OR_PSUTIL_MISMATCH") < body.index("Start-Process")
# 探针与 driver 同样以 driver 所在目录为 sys.path[0]：放在 driver 旁边的 psutil 会被它报出来（这里用替身包）
probe_dir = tmp / "probe"
(probe_dir / "psutil").mkdir(parents=True)
(probe_dir / "psutil" / "__init__.py").write_text("__version__ = '9.8.7'\n", encoding="ascii")
(probe_dir / "native_batch_driver.py").write_text("raise SystemExit(3)\n", encoding="ascii")
p = subprocess.run([sys.executable, "-B", "-c", n.PYTHON_PROBE, str(probe_dir / "native_batch_driver.py")], cwd=tmp,
                   capture_output=True, text=True, timeout=60)
probe = json.loads(p.stdout.splitlines()[-1])
assert p.returncode == 0 and set(probe) == {"version", "architecture", "psutil_version", "psutil_path"}, p.stderr
assert probe["psutil_version"] == "9.8.7" and Path(probe["psutil_path"]).resolve() == (probe_dir / "psutil").resolve(), probe
assert probe["version"] == ".".join(map(str, sys.version_info[:3])) and probe["architecture"]
# safe-path 下 driver 脚本也不会把自己的目录放进 sys.path：探针此时不动 sys.path，替身包不应被导入
p = subprocess.run([sys.executable, "-B", "-c", n.PYTHON_PROBE, str(probe_dir / "native_batch_driver.py")], cwd=tmp,
                   capture_output=True, text=True, timeout=60, env=dict(os.environ, PYTHONSAFEPATH="1"))
assert p.returncode != 0 or json.loads(p.stdout.splitlines()[-1])["psutil_version"] != "9.8.7", p.stdout
for banned in ("sqlite", "reservation", "priorityclass", "ecoqos", "xdev_pool", "xdev_state", "taskkill", "stop-process"):
    assert banned not in body.lower(), banned
assert max(body.index(m) for m in ("SHA_MISMATCH", "JOURNAL_EXISTS_NO_REPLAY", "MODELS_DIR_EXISTS_NO_REPLAY")) < body.index("Start-Process")
assert "$W" not in body  # 正文的桥接回执叫 $w；PS 变量不分大小写，不能再有 $W

# ---- 收尾负控：状态作业、pull_dir、W1 task_status 全用替身；journal 按 successor03 driver 的写法逐字节模拟 ----
b.time.sleep = lambda s: None
REAL_POLL = n.poll_once
PIN = good["binding"]["python_pin"]
pulls = []
pull_mode = {"rc": {}, "text": {}}
JOURNALS = {}  # 批次名（轮询所见）与 journal 远端目录（取回所得）-> 模拟的 driver journal
INPS = tmp / "inps"
INPS.mkdir()
for i in range(1, 4):
    (INPS / f"m{i}.inp").write_text("TITLE: t;\nANALYSIS: PROCESSORS = 1;\n", encoding="ascii")


def canon(value) -> bytes:
    return (n.canonical(value) + "\n").encode()


def make_man(label, k=2):
    """与 run() 相同：真 native_plan，冻结 plan/config 落在 OUT/_xdev_native，清单带两份 SHA 与绑定的 Python 身份。"""
    out_dir = tmp / label
    pl, nplan, nconfig = n.native_plan([str(INPS / f"m{i}.inp") for i in range(1, k + 1)], [], good, 60)
    ctl = out_dir / n.CONTROL_DIR
    ctl.mkdir(parents=True)
    (ctl / n.PLAN_FILE).write_bytes(canon(nplan))
    (ctl / n.CONFIG_FILE).write_bytes(canon(nconfig))
    man = {**pl, "binding": {"path": good["path"], "sha256": good["sha256"]}, "plan_sha256": hashlib.sha256(canon(nplan)).hexdigest(),
           "config_sha256": hashlib.sha256(canon(nconfig)).hexdigest(), "pins": dict(n.PINS), "python_pin": PIN,
           "task": {"name": pl["batch"], "remote_dir": pl["remote_dir"], "owner": OWNER}, "results": {}}
    return man, out_dir, nplan, nconfig


def digest(value):
    """W1 adapter 的 digest 原式（独立于被测代码）：规范 JSON 的 SHA256。"""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def filetime_utc(ticks):
    """W1 backend _filetime_utc 原式：FILETIME（1601 起 100ns）显示到微秒的 UTC isoformat。"""
    from datetime import datetime, timedelta, timezone
    return (datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ticks // 10)).isoformat()


def receipt(nplan, nconfig):
    """adapter prepare 成功后 receipt() 的形状：本批 lease、config SHA、RESERVED、每个 attempt 一份未启动的冻结 grant。"""
    lease, cfg_sha = "generalbatch:" + digest([nconfig["original_controller"], nconfig["batch_id"]]), digest(nconfig)
    children = {}
    for x in nplan["models"]:
        g = {"schema": "general-batch-grant-v1", "lease_key": lease, "configuration_sha256": cfg_sha,
             "attempt": copy.deepcopy(x["request"]), "execution_authorized": False}
        children[x["request"]["attempt_id"]] = {"grant": dict(g, sha256=digest(g)), "state": "PREPARED", "identity": None,
                                                "terminal": None}
    # Frozen prepare budget; owner_created is psutil Unix epoch seconds.
    rows, overhead, count = [x["request"] for x in nplan["models"]], nconfig["overhead"], nconfig["max_parallel"]
    budget = {f: overhead[f] + sum(sorted((r[f] for r in rows), reverse=True)[:count])
              for f in ("physical_bytes", "commit_bytes", "processors")}
    volumes = {root.upper(): amount for root, amount in overhead["volume_bytes"].items()}
    for r in rows:
        for pf, bf in (("working_directory", "output_budget_bytes"), ("temp_directory", "temp_budget_bytes")):
            root = PureWindowsPath(r[pf]).anchor.upper()
            volumes[root] = volumes.get(root, 0) + r[bf]
    budget["volume_bytes"] = volumes
    return {"schema": "general-batch-journal-v1", "lease_key": lease, "owner_pid": 4321, "owner_created": 1794960000.25,
            "configuration_sha256": cfg_sha, "original_controller": nconfig["original_controller"], "batch_id": nconfig["batch_id"],
            "state": "RESERVED", "budget": budget,
            "children": children, "events": [{"event": "MEASURED_RESERVATION_ACQUIRED", "monotonic_ns": 1, "utc_epoch": 1.0}],
            "admission": {"admitted": True, "new_reservation": True, "key": lease}}


def want_rc(res):
    """driver main() 的退出码：processes_ok 为 0，有未决成员为 7，否则 1。"""
    if not isinstance(res, dict):
        return 1
    return 0 if res.get("processes_ok") is True else (7 if res.get("unresolved_attempt_ids") else 1)


def drive(nplan, nconfig, settle=None, exit_codes=None, out_sha=None):
    """按 successor03 driver 的顺序写本批 journal：PLAN、事件（STAGED/SPAWN/IDENTITY/TERMINAL/结算）、每个已结算成员的
    terminal/result、RESULT。与 driver 一样，NATIVE_TERMINAL 与 terminal 文件、RESULT 成员与 result 文件、RESULT 与
    DRIVER_RESULT_PERSISTED 共用同一个对象；settle 之外的 attempt 记未决（driver 关不上的原进程）。"""
    rows = [x["request"] for x in nplan["models"]]
    settle = [r["model_id"] for r in rows] if settle is None else settle  # 按模型名给
    events, members, terminals = [], [], {}
    files = {"PLAN.json": {"plan": copy.deepcopy(nplan), "evidence_class": "NATIVE_COLLECTION"}}

    def add(event, **fields):
        events.append({"sequence": len(events) + 1, "event": event, "monotonic_ns": 1000 * (len(events) + 1), **fields})

    add("ORIGINAL_BATCH_INTENT", configuration_sha256=hashlib.sha256(n.canonical(nconfig).encode()).hexdigest())
    rec = receipt(nplan, nconfig)
    add("MEASURED_BATCH_RESERVED", admission=rec)
    for r in rows:
        add("ORIGINAL_FILES_STAGED", attempt_id=r["attempt_id"], input_sha256=r["input_sha256"], data_sha256=dict(r["data_sha256"]))
    for i, r in enumerate(rows):
        birth = 134394000000000000 + 10 ** 7 * i
        ident = {k: r[k] for k in ("model_id", "route", "input_sha256", "request_id", "job_id", "attempt_id")}
        ident.update(pid=100 + i, birth_utc=filetime_utc(birth), birth_filetime_100ns=birth, birth_filetime=birth,
                     exe_path=r["exe_path"].upper(), exe_sha256=r["exe_sha256"], host_id="host-1",  # 镜像名大小写可与绑定不同
                     clock_domain="windows-monotonic-ns:host-1:driver:1:1", identity_source="NATIVE_PROCESS_HANDLE",
                     engine_interval_confirmed=False, start_monotonic_ns=None)
        add("SPAWN_INTENT", request=copy.deepcopy(r), grant=copy.deepcopy(rec["children"][r["attempt_id"]]["grant"]))
        add("IDENTITY_CAPTURED_SUSPENDED", identity=ident)
        add("IDENTITY_BOUND_BEFORE_RESUME", attempt_id=r["attempt_id"])
        returned = dict(ident, start_monotonic_ns=5, deadline_seconds=60)
        add("SPAWN_RETURNED", identity=returned)
        terminals[r["attempt_id"]] = dict(returned, terminal="EXITED", exit_code=(exit_codes or {}).get(r["model_id"], 0),
                                          descendants_complete=True, job_active_processes=0,
                                          native_exit_filetime_100ns=birth + 5 * 10 ** 7)
    for r in rows:
        a = r["attempt_id"]
        if r["model_id"] not in settle:
            continue
        t = terminals[a]
        add("NATIVE_TERMINAL", attempt_id=a, terminal=t)
        res = {"attempt_id": a, "pid": t["pid"], "exit_code": t["exit_code"], "terminal": t["terminal"], "descendants_complete": True,
               "output_sha256": (out_sha or {}).get(r["model_id"]) or sha(pull_mode["text"].get(r["model_id"], NORMAL)),
               "validation_errors": [], "processes_ok": t["exit_code"] == 0, "scientific_validation": "NOT_PERFORMED",
               "engine_interval_confirmed": False}
        files[a + ".terminal.json"], files[a + ".result.json"] = t, res
        members.append(res)
        add("ADMISSION_TERMINAL_ACCEPTED", attempt_id=a)
        add("ORIGINAL_HANDLES_CLOSED", attempt_id=a)
    unresolved = [r["attempt_id"] for r in rows if r["model_id"] not in settle]
    state = "UNKNOWN" if unresolved else "RELEASED"
    files["RESULT.json"] = {"schema": n.RESULT_SCHEMA, "batch_id": nplan["batch_id"], "admission_state": state,
                            "evidence_class": "NATIVE_COLLECTION", "scientific_validation": "NOT_PERFORMED",
                            "processes_ok": not unresolved and all(m["processes_ok"] for m in members),
                            "members": members, "errors": [], "unresolved_attempt_ids": unresolved}
    add("DRIVER_RESULT_PERSISTED", summary=files["RESULT.json"])
    return {"files": files, "events": events, "raw": {}}


def render(J):
    """journal 落盘字节：记录是规范 JSON 加换行（driver write_new），events.jsonl 每行一条；raw 覆盖字节，None = 没有这个文件。"""
    out = {k: canon(v) for k, v in J["files"].items()}
    out["events.jsonl"] = b"".join(canon(e) for e in J["events"])
    out.update(J["raw"])
    return {k: v for k, v in out.items() if v is not None}


def verified_python(**kw):
    return {"version": PIN["version"], "architecture": PIN["architecture"], "psutil_version": PIN["psutil_version"],
            "psutil_path": PIN["psutil_path"], **kw}


def wrapper(man, J=None, **kw):
    """正文的桥接回执：driver 退出码与 J 的 RESULT 相符，带探针核过的 Python/psutil 身份。"""
    w = {"schema": "xdev-mplus-native-wrapper-v1", "owner": OWNER, "name": man["task"]["name"], "batch_id": man["batch"],
         "plan_sha256": man["plan_sha256"], "config_sha256": man["config_sha256"], "state": "DRIVER_EXITED",
         "driver_exit_code": want_rc(J["files"].get("RESULT.json")) if J else 0, "python_verified": verified_python()}
    w.update(kw)
    return {k: v for k, v in w.items() if v is not None}


def row(man, J, full=False, runner="0 2026-11-18T00:00:06Z", journal=True, wrap_kw=None, **kw):
    """状态作业回件：按 J 的落盘字节给 RESULT 原文、事件数、末条事件、各 attempt 的 terminal/result 原文。"""
    disk = {k: v.decode() for k, v in render(J).items()} if journal else {}
    lines = [x for x in disk.get("events.jsonl", "").split("\n") if x.strip()]
    attempts = [m["attempt_id"] for m in man["models"]]
    r = {"ok": True, "job": 5, "now": 1, "runner": runner, "wrapper": wrapper(man, J, **(wrap_kw or {})), "journal_exists": journal,
         "result_exists": "RESULT.json" in disk, "events": len(lines), "last_event": n._parse(lines[-1]) if lines else None,
         "driver_stderr": None, "result": n._parse(disk.get("RESULT.json")) if full else None,
         "members": ({a: {"attempt_id": a, "terminal": disk.get(a + ".terminal.json"), "result": disk.get(a + ".result.json")}
                      for a in attempts} if full else {a: {"attempt_id": a, "settled": a + ".result.json" in disk} for a in attempts})}
    r.update(kw)
    return r


def polls(man, J, light=None, **kw):
    """首轮轻量状态（runner 已出现），收尾一轮全量证据。"""
    n.poll_once = lambda m, full=False: row(man, J, full=full, **kw) if full else (light or row(man, J, **kw))


def fake_pull(remote, local):
    pulls.append((remote, local))
    local = Path(local)
    if remote.endswith("\\" + n.JOURNAL):
        if pull_mode["rc"].get(n.JOURNAL):  # journal 原次取回失败或未知：本机什么都没落
            return pull_mode["rc"][n.JOURNAL]
        local.mkdir(parents=True)
        for name, raw in render(JOURNALS[remote]).items():
            (local / name).write_bytes(raw)
        return 0
    if pull_mode["rc"].get(local.name) == 124:  # 原次结果未知：认领文件留着，远端 zip 不动
        local.with_name("." + local.name + ".xdev-claim.json").write_text(json.dumps({"transfer_id": "9" * 32}), encoding="utf-8")
        return 124
    local.mkdir(parents=True)
    (local / (local.name + ".out")).write_bytes(pull_mode["text"].get(local.name, NORMAL).encode("utf-8"))
    return 0


b.pull_dir = fake_pull
task_rc = {"rc": None, "state": "DONE"}  # rc None = 原任务 rc 等于 driver 按轮询所见 RESULT 的退出码
statuses = []


def fake_status(name, nbytes=4000, wait_seconds=45):
    statuses.append(name)
    rc = want_rc(JOURNALS[name]["files"].get("RESULT.json")) if task_rc["rc"] is None else task_rc["rc"]
    return {"ok": True, "state": task_rc["state"], "rc": rc, "binding_verified": True, "job_id": 77}


xdev_tasks.task_status = fake_status


def case(label, k=2, mutate=None, collected=None, polled=None, local=None, journal_rc=None, drive_kw=None, **kw):
    """一批：真 plan → 模拟 driver journal。mutate 两边都改（driver 本身就这么写）；collected/polled 只改取回或轮询的一边；
    local 改本机冻结文件；journal_rc 让 journal 的 pull_dir 原次返回该码。"""
    man, out_dir, nplan, nconfig = make_man(label, k)
    J = drive(nplan, nconfig, **(drive_kw or {}))
    if mutate:
        mutate(J)
    Jc, Jp = copy.deepcopy(J), copy.deepcopy(J)
    if collected:
        collected(Jc)
    if polled:
        polled(Jp)
    JOURNALS[man["batch"]], JOURNALS[nplan["journal_directory"]] = Jp, Jc
    if local:
        local(out_dir)
    polls(man, Jp, **kw)
    pull_mode["rc"][n.JOURNAL] = journal_rc
    start = len(pulls)
    try:
        rc = n.follow(man, out_dir, 60)
    finally:
        pull_mode["rc"].pop(n.JOURNAL)
    return rc, man, pulls[start:]


def first(J):
    return J["files"]["PLAN.json"]["plan"]["models"][0]["request"]["attempt_id"]


def ev(J, event, i=0):
    return [e for e in J["events"] if e["event"] == event][i]


def renumber(J):
    for i, e in enumerate(J["events"], 1):
        e.update(sequence=i, monotonic_ns=1000 * i)


def insert_before_result(J, event):
    J["events"].insert(len(J["events"]) - 1, event)
    renumber(J)


# 正对照：两模型区间重叠；先取回并核 journal，再按 plan 的 working_directory 取回、.out SHA 等于 driver 记录、文本判定通过
rc, man, got = case("c_good")
WD = man["work_dir"]
assert rc == 0 and man["ok"] is True and man["stage"] == "RESULT" and man["exit_code"] == 0
assert [p[0] for p in got] == [WD + "\\driver-evidence", WD + "\\models\\m1\\cwd", WD + "\\models\\m2\\cwd"]
assert man["journal_verification"] == {"ok": True, "errors": []} and man["journal_collect"]["status"] == "COMPLETE"
assert man["timing"] == {"kernel_span_s": 6.0, "kernel_sum_s": 10.0, "kernel_interval_peak": 2}
for k in ("m1", "m2"):
    rec = man["results"][k]
    assert rec["ok"] and rec["output_sha_ok"] and rec["attempt_result_consistent"] and rec["check"]["ok"] and not rec["evidence_mismatch"]
    assert rec["scientific_validation"] == "NOT_PERFORMED" and rec["attempt_id"] == f"{man['batch']}.{k}" and rec["state"] == "SETTLED"
assert statuses == [man["batch"]]
# provider 的成功 admission 不要求 key；真实 journal 核验仍通过并取回全部模型。
rc, man, got = case("c_receipt_no_admission_key", mutate=lambda J:
                    ev(J, "MEASURED_BATCH_RESERVED")["admission"]["admission"].pop("key"))
assert rc == 0 and man["ok"] is True and len(got) == 3
assert man["journal_verification"] == {"ok": True, "errors": []}
assert "key" not in ev(JOURNALS[man["batch"]], "MEASURED_BATCH_RESERVED")["admission"]["admission"]
# 三个模型、psutil 目录大小写不同、安全结算重试留下第二条同样的 NATIVE_TERMINAL：仍是同一原次，通过
rc, man, got = case("c_three", k=3, wrap_kw={"python_verified": verified_python(psutil_path=PIN["psutil_path"].upper())},
                    mutate=lambda J: (J["events"].insert(J["events"].index(ev(J, "NATIVE_TERMINAL")) + 1,
                                                         copy.deepcopy(ev(J, "NATIVE_TERMINAL"))), renumber(J)))
assert rc == 0 and man["ok"] is True and len(got) == 4 and man["timing"]["kernel_interval_peak"] == 3
# driver 没建 journal（正文拒绝或依赖不符）：没起进程、不取回，记 NOT_STARTED 和原因
rc, man, got = case("c_nojournal", journal=False, wrapper={"state": "REFUSED", "reason": "PYTHON_OR_PSUTIL_MISMATCH"})
assert rc == 1 and got == [] and man["stage"] == "NOT_STARTED" and man["results"]["m1"]["reason"] == "PYTHON_OR_PSUTIL_MISMATCH"
# journal 在但轮询所见 RESULT 缺失、半截、外批或别的 schema：原次未知，不取回、不重调
for label, kw in (("c_resmissing", {"polled": lambda J: J["raw"].update({"RESULT.json": None})}),
                  ("c_respartial", {"polled": lambda J: J["raw"].update({"RESULT.json": b'{"schema": "native-gen'})}),
                  ("c_resforeign", {"mutate": lambda J: J["files"]["RESULT.json"].update(batch_id="other-batch")}),
                  ("c_resschema", {"mutate": lambda J: J["files"]["RESULT.json"].update(schema="native-general-batch-result-v0")})):
    rc, man, got = case(label, **kw)
    assert rc == 7 and got == [] and man["stage"] == "RESULT_UNKNOWN" and man["ok"] is False, label


def unverified(label, expect, **kw):
    """journal 没取回或核不过：整批 7，只试过一次 journal 取回，模型目录一个都不取，结果全记 EVIDENCE_UNVERIFIED；
    expect 是本例针对的那一项检查报出的错误片段（防止由别的检查顺带拦下而掩盖该项失效）。"""
    rc, man, got = case(label, **kw)
    assert rc == 7 and man["ok"] is False and man["stage"] == "RESULT" and man["exit_code"] == 7, (label, rc)
    errors = man["journal_verification"]["errors"]
    assert man["journal_verification"]["ok"] is False and any(expect in e for e in errors), (label, errors)
    assert [p[0] for p in got] == [man["work_dir"] + "\\driver-evidence"], (label, got)
    assert all(man["results"][m["key"]]["state"] == "EVIDENCE_UNVERIFIED" for m in man["models"]), label
    return man


def term(J):
    return J["files"][first(J) + ".terminal.json"]


# 完整预约回执的预算必须等于冻结 prepare：数值相等的 float 也不是整数预算。
for label, change in (
    ("c_receipt_budget_wrong", lambda d: d.update(physical_bytes=d["physical_bytes"] - 1)),
    ("c_receipt_budget_float", lambda d: d.update(commit_bytes=float(d["commit_bytes"]))),
    ("c_receipt_budget_bool", lambda d: d.update(processors=True)),
):
    unverified(label, "MEASURED_BATCH_RESERVED budget differs from frozen config/requests",
               mutate=lambda J, change=change: change(ev(J, "MEASURED_BATCH_RESERVED")["admission"]["budget"]))
# owner 是 provider 的严格 PID / epoch-seconds 值域；bool 不能冒充整数。
for label, field, value in (
    ("c_receipt_pid_low", "owner_pid", 0),
    ("c_receipt_pid_high", "owner_pid", 2**31),
    ("c_receipt_pid_bool", "owner_pid", True),
    ("c_receipt_created_low", "owner_created", .0009),
    ("c_receipt_created_high", "owner_created", 2**53 + 1),
    ("c_receipt_created_bool", "owner_created", True),
):
    unverified(label, "MEASURED_BATCH_RESERVED owner is outside the frozen provider domain",
               mutate=lambda J, field=field, value=value:
               ev(J, "MEASURED_BATCH_RESERVED")["admission"].update({field: value}))


OTHER_MAN, _, OTHER_PLAN, OTHER_CONFIG = make_man("c_otherbatch")
OTHER = drive(OTHER_PLAN, OTHER_CONFIG)
# W1 独审反例：terminal 缺失（轮询原文为 None / driver 没写 / 取回为空文件）或属于外批/外 pid，都不能 rc0
DIFF, ONCE = "collected file differs from the polled original", "are not exactly the plan's attempts once each"
unverified("c_term_none_polled", DIFF, polled=lambda J: J["raw"].update({first(J) + ".terminal.json": None}))
unverified("c_term_none_both", "terminal file missing", mutate=lambda J: J["raw"].update({first(J) + ".terminal.json": None}))
unverified("c_term_empty_collected", "terminal.json: unreadable", collected=lambda J: J["raw"].update({first(J) + ".terminal.json": b""}))
unverified("c_term_foreign", "events name attempts outside this batch: ['MIXED:", mutate=lambda J: term(J).update(
    attempt_id="other", request_id="other-batch.m1", job_id="other-batch/m1", pid=999))
unverified("c_term_pid999", "identity differs from the terminal record", mutate=lambda J: term(J).update(pid=999))
# W1 独审反例：journal 取回 FAILED（rc5）或未知（124）都不成功，也不再取模型目录
man = unverified("c_journal_rc5", "collection FAILED (rc=5); not repeated", journal_rc=5)
assert man["journal_collect"]["status"] == "FAILED" and man["journal_collect"]["rc"] == 5
assert unverified("c_journal_124", "collection UNKNOWN (rc=124)", journal_rc=124)["journal_collect"]["status"] == "UNKNOWN"
# W1 独审反例：取回的 RESULT 属于外批且成员为空；或整个取回的 journal 是另一批的
unverified("c_collected_foreign", "RESULT.json is not a native result of this batch", collected=lambda J: J["files"].update(
    {"RESULT.json": {"schema": n.RESULT_SCHEMA, "batch_id": "FOREIGN", "members": []}}))
unverified("c_collected_other_batch", "PLAN.json is not this batch's frozen plan", collected=lambda J: J.update(copy.deepcopy(OTHER)))
unverified("c_polled_result_differs", "collected RESULT.json differs from the polled original",
           polled=lambda J: J["files"]["RESULT.json"]["errors"].append("late"))
# 成员集：重复、缺一个（又不在未决里）、多一个外批成员、既是成员又未决
unverified("c_member_twice", ONCE, mutate=lambda J: J["files"]["RESULT.json"]["members"].append(J["files"]["RESULT.json"]["members"][0]))
unverified("c_member_dropped", ONCE, mutate=lambda J: J["files"]["RESULT.json"]["members"].pop())
unverified("c_member_foreign", ONCE, mutate=lambda J: J["files"]["RESULT.json"]["members"].append(
    dict(J["files"]["RESULT.json"]["members"][0], attempt_id=OTHER_MAN["models"][0]["attempt_id"])))
unverified("c_unresolved_and_member", ONCE, mutate=lambda J: J["files"]["RESULT.json"]["unresolved_attempt_ids"].append(first(J)))
# 文件：多出的文件、result 文件与 RESULT 成员不同、字节不规范、PLAN/RESULT 是测试模式或别的 plan、本机冻结 plan 提交后被改
unverified("c_extra_file", "files this batch's driver does not write: ['other.terminal.json']",
           collected=lambda J: J["raw"].update({"other.terminal.json": b"{}\n"}))
unverified("c_result_file_differs", "member result does not match its terminal/result files", mutate=lambda J: J["files"].update(
    {first(J) + ".result.json": dict(J["files"][first(J) + ".result.json"], pid=999)}))
unverified("c_result_not_canonical", "RESULT.json: bytes are not the driver's canonical record", collected=lambda J: J["raw"].update(
    {"RESULT.json": (json.dumps(J["files"]["RESULT.json"]) + "\n").encode()}))
unverified("c_plan_test_only", "PLAN.json is not this batch's frozen plan",
           mutate=lambda J: J["files"]["PLAN.json"].update(evidence_class="UNIT_TEST_ONLY"))
unverified("c_plan_other", "PLAN.json is not this batch's frozen plan",
           mutate=lambda J: J["files"]["PLAN.json"]["plan"].update(journal_directory="C:\\elsewhere"))
unverified("c_result_test_only", "RESULT.json is not a native result of this batch",
           mutate=lambda J: J["files"]["RESULT.json"].update(evidence_class="UNIT_TEST_ONLY"))
unverified("c_frozen_plan_edited", "local frozen plan file missing or changed",
           local=lambda o: (o / n.CONTROL_DIR / n.PLAN_FILE).write_bytes(b"{}\n"))
# 事件：不认识的名字、乱序、少一条、多一条、SPAWN_UNKNOWN、外批 attempt、顶层与内层 attempt 不一致、字节不规范、末行没换行
unverified("c_event_unknown", "event 'SPAWN_REPLAYED'", mutate=lambda J: J["events"][3].update(event="SPAWN_REPLAYED"))
unverified("c_event_sequence_gap", "line 5: sequence 99", mutate=lambda J: J["events"][4].update(sequence=99))


def spawn_after_terminal(J):
    e = ev(J, "SPAWN_RETURNED")
    J["events"].remove(e)
    J["events"].insert(J["events"].index(ev(J, "NATIVE_TERMINAL")) + 1, e)
    renumber(J)


ORDER, IDENT, TERM, BRIDGE, PYID = ("settlement events out of order", "identity differs from the terminal record",
                                   "terminal record incomplete", "bridge/task exit code does not match RESULT",
                                   "bridge receipt lacks the bound Python/psutil identity")
unverified("c_event_order", ORDER, mutate=spawn_after_terminal)
unverified("c_event_missing", "not exactly once each ['IDENTITY_BOUND_BEFORE_RESUME']",
           mutate=lambda J: (J["events"].remove(ev(J, "IDENTITY_BOUND_BEFORE_RESUME")), renumber(J)))
unverified("c_event_twice", "not exactly once each ['IDENTITY_BOUND_BEFORE_RESUME']",
           mutate=lambda J: insert_before_result(J, dict(ev(J, "IDENTITY_BOUND_BEFORE_RESUME"))))
unverified("c_event_spawn_unknown", "SPAWN_UNKNOWN 1", mutate=lambda J: insert_before_result(
    J, {"event": "SPAWN_UNKNOWN", "attempt_id": first(J), "error_type": "X", "identity": None}))
unverified("c_event_foreign", "outside this batch: ['other.m1']", mutate=lambda J: insert_before_result(
    J, {"event": "ORIGINAL_FILES_STAGED", "attempt_id": "other.m1", "input_sha256": "0" * 64, "data_sha256": {}}))
unverified("c_event_mixed", "outside this batch: ['MIXED:",
           mutate=lambda J: ev(J, "NATIVE_TERMINAL").update(attempt_id=OTHER_MAN["models"][0]["attempt_id"]))
unverified("c_event_count", "polled original had 99", events=99)
unverified("c_event_not_last", "end with exactly one DRIVER_RESULT_PERSISTED",
           mutate=lambda J: (J["events"].insert(len(J["events"]) - 2, J["events"].pop()), renumber(J)))
unverified("c_event_config", "ORIGINAL_BATCH_INTENT", mutate=lambda J: J["events"][0].update(configuration_sha256="0" * 64))
unverified("c_events_empty", "events.jsonl: no driver events", collected=lambda J: J["raw"].update({"events.jsonl": b""}))
unverified("c_events_not_canonical", "line 1: not a driver record", collected=lambda J: J["raw"].update(
    {"events.jsonl": b"".join((json.dumps(e) + "\n").encode() for e in J["events"])}))
unverified("c_events_unterminated", "last line not terminated",
           collected=lambda J: J["raw"].update({"events.jsonl": render(J)["events.jsonl"][:-1]}))
# 成员事件内容：暂存 SHA、SPAWN_INTENT 不是 plan 的请求、身份与 terminal 不符、引擎不是绑定的、子进程未关完、退出码不是整数
unverified("c_staged_sha", "staged input/data SHA", mutate=lambda J: ev(J, "ORIGINAL_FILES_STAGED").update(input_sha256="0" * 64))
unverified("c_spawn_request", "SPAWN_INTENT request", mutate=lambda J: ev(J, "SPAWN_INTENT")["request"].update(exe_sha256="0" * 64))
unverified("c_identity_pid", IDENT, mutate=lambda J: ev(J, "IDENTITY_CAPTURED_SUSPENDED")["identity"].update(pid=999))
unverified("c_identity_clock", IDENT, mutate=lambda J: ev(J, "SPAWN_RETURNED")["identity"].update(clock_domain="other-boot"))
unverified("c_engine_other", "not the plan's request/engine", mutate=lambda J: [d.update(exe_path="C:\\XM\\Other\\Mplus.exe") for d in (
    ev(J, "IDENTITY_CAPTURED_SUSPENDED")["identity"], ev(J, "SPAWN_RETURNED")["identity"], term(J))])
unverified("c_descendants_open", TERM, mutate=lambda J: term(J).update(descendants_complete=False))
unverified("c_exit_before_birth", TERM, mutate=lambda J: term(J).update(native_exit_filetime_100ns=1))
unverified("c_exit_code_text", TERM, mutate=lambda J: term(J).update(exit_code="0"))
# 桥接回执与原任务：driver 退出码与 RESULT 不符、原任务 rc 不符、没有或不符的 Python/psutil 身份
unverified("c_wrapper_exit", "(wrapper 1, task 0, expected 0)", wrap_kw={"driver_exit_code": 1})
unverified("c_wrapper_state", BRIDGE, wrap_kw={"state": "DRIVER_EXIT_UNKNOWN"})
task_rc["rc"] = 1
unverified("c_task_rc1", "(wrapper 0, task 1, expected 0)")
task_rc["rc"] = None
unverified("c_wrapper_no_python", PYID, wrap_kw={"python_verified": None})
unverified("c_wrapper_psutil", PYID, wrap_kw={"python_verified": verified_python(psutil_version="5.9.0")})
unverified("c_wrapper_psutil_path", PYID, wrap_kw={"python_verified": verified_python(psutil_path="C:\\Other\\psutil")})
unverified("c_wrapper_arch", PYID, wrap_kw={"python_verified": verified_python(architecture="ARM64")})
# 已中断的 journal 取回（STARTED）只记 UNKNOWN，不再取
man, out_dir, _, _ = make_man("c_journal_started")
man["journal_collect"] = {"status": "STARTED", "local": str(out_dir / "_native_journal")}
start = len(pulls)
assert n._collect_journal(man, out_dir)["status"] == "UNKNOWN" and len(pulls) == start
# 未决成员（driver 关不上的原进程）：journal 核过后只取已结算的，未决的记 UNRESOLVED，整批 7
rc, man, got = case("c_unresolved", drive_kw={"settle": ["m1"]})
assert rc == 7 and [p[0] for p in got] == [man["work_dir"] + "\\driver-evidence", man["work_dir"] + "\\models\\m1\\cwd"]
assert man["journal_verification"]["ok"] is True and man["native_result"]["unresolved_attempt_ids"] == [man["models"][1]["attempt_id"]]
assert man["results"]["m2"]["state"] == "UNRESOLVED" and man["results"]["m1"]["ok"] is True and man["ok"] is False
# driver 记录的 .out SHA 与取回的不同 → 证据不一致，7，不重取
rc, man, got = case("c_shabad", drive_kw={"out_sha": {"m1": "0" * 64}})
assert rc == 7 and man["results"]["m1"]["evidence_mismatch"] is True and man["results"]["m1"]["ok"] is False and len(got) == 3
# processes_ok 不是科学验收：SHA 一致但 .out 带错误行 → 失败 1（不是未知）
pull_mode["text"]["m1"] = BAD_OUT
rc, man, got = case("c_outbad")
assert rc == 1 and man["results"]["m1"]["output_sha_ok"] and man["results"]["m1"]["check"]["ok"] is False
assert man["results"]["m1"]["ok"] is False and man["results"]["m2"]["ok"] is True
del pull_mode["text"]["m1"]
# 非零退出（rc9 但 .out 文字正常）→ driver 退出 1，journal 核过，该模型失败
rc, man, got = case("c_rc9", drive_kw={"exit_codes": {"m1": 9}})
assert rc == 1 and man["journal_verification"]["ok"] is True and man["task"]["final"]["rc"] == 1
assert man["results"]["m1"]["ok"] is False and man["results"]["m1"]["check"]["ok"] is True and man["results"]["m2"]["ok"] is True
# 预约没有释放 → 批次不成功
rc, man, got = case("c_notreleased", mutate=lambda J: J["files"]["RESULT.json"].update(admission_state="UNKNOWN"))
assert rc != 0 and man["ok"] is False
# W1 task_status 没核到 DONE → 7，什么都不取
task_rc["state"] = "UNKNOWN"
rc, man, got = case("c_taskunknown")
assert rc == 7 and got == [] and "stage" not in man
task_rc["state"] = "DONE"
# pull 124 → 该模型 UNKNOWN，认领留着；整批 7；--resume 只重报原结论，不再取、不再查
pull_mode["rc"]["m2"] = 124
rc, man, got = case("c_pull124")
assert rc == 7 and man["results"]["m2"]["collect"]["status"] == "UNKNOWN" and man["results"]["m1"]["ok"] is True
del pull_mode["rc"]["m2"]
b._journal_owner = lambda name: OWNER
n.poll_once = lambda m, full=False: (_ for _ in ()).throw(AssertionError("resume of a concluded batch must not poll"))
start, n_status = len(pulls), len(statuses)
assert b.resume(str(tmp / "c_pull124"), 60) == 7 and len(pulls) == start and len(statuses) == n_status
assert b.resume(str(tmp / "c_journal_rc5"), 60) == 7 and len(pulls) == start and len(statuses) == n_status
# 状态作业报桥接回执不属于本批 → 致命，不查 task_status、不取回
man, out_dir, _, _ = make_man("c_wrapbad")
n.poll_once = lambda m, full=False: {"ok": False, "fatal": True, "reason": "WRAPPER_BINDING_MISMATCH job=5 {}"}
start, n_status = len(pulls), len(statuses)
assert n.follow(man, out_dir, 60) == 7 and len(pulls) == start and len(statuses) == n_status
# runner 回执一直没出现 → 124，原任务在 Windows 上继续；收尾全量读三次都失败 → 7，不取回
man, out_dir, nplan_s, nconfig_s = make_man("c_still")
J_s = drive(nplan_s, nconfig_s)
polls(man, J_s, light=row(man, J_s, runner=None))
assert n.follow(man, out_dir, 0) == 124 and "final" not in man["task"]
man, out_dir, nplan_s, nconfig_s = make_man("c_unread")
JOURNALS[man["batch"]] = J_s = drive(nplan_s, nconfig_s)
light = row(man, J_s)
n.poll_once = lambda m, full=False: {"ok": False, "fatal": False, "reason": "rc=124"} if full else light
start = len(pulls)
assert n.follow(man, out_dir, 60) == 7 and len(pulls) == start



# poll_once：先读 runner 回执，再读桥接回执和 journal；桥接回执里的批次/SHA/owner 不符 → 致命
n.poll_once = REAL_POLL
man, out_dir, _, _ = make_man("c_pollonce")


def reply(**kw):
    r = {"ok": True, "now": 1, "runner": None, "wrapper": None, "journal_exists": False, "result": None, "result_exists": False,
         "events": 0, "last_event": None, "members": [], "driver_stderr": None}
    r.update(kw)
    return lambda *a, **k: (0, "XDEV_JSON " + json.dumps(r) + "\n", "", 9)


for field, value in (("batch_id", "other"), ("plan_sha256", "c" * 64), ("owner", "f" * 32), ("name", "other")):
    n.run_ps = reply(wrapper=json.dumps(wrapper(man, **{field: value})))
    r = n.poll_once(man)
    assert r["ok"] is False and r["fatal"] is True and "WRAPPER_BINDING_MISMATCH" in r["reason"], field
n.run_ps = reply(wrapper='{"schema": "xdev-mplus-native-wrapper-v1", "own')  # 半截回执：下一轮再读
assert n.poll_once(man)["ok"] and n.poll_once(man)["wrapper"] == "PARTIAL"
n.run_ps = reply(journal_exists=True, result_exists=True, result='{"schema": "native-general', events=3,
                 last_event=json.dumps({"event": "SPAWN_RETURNED"}),
                 members=[{"attempt_id": "m1", "terminal": None, "result": json.dumps({"attempt_id": "m1", "pid": 100})}])
r = n.poll_once(man, full=True)
assert r["ok"] and r["result"] == "PARTIAL" and r["last_event"]["event"] == "SPAWN_RETURNED" and r["members"]["m1"]["terminal"] is None
n.run_ps = lambda *a, **k: (0, 'XDEV_JSON {"ok": false, "reason": "TASK_BINDING_MISMATCH"}\n', "", 9)
assert n.poll_once(man)["fatal"] is True
seen = []
n.run_ps = lambda script, **k: seen.append(script) or (124, "", "", 9)
assert n.poll_once(man)["ok"] is False and n.poll_once(man, full=True).get("fatal") is False
assert ("$meta.owner -cne '" + OWNER + "'") in seen[0] and seen[0].index("rc.txt") < seen[0].index("events.jsonl")
assert "$full = $false" in seen[0] and "$full = $true" in seen[1] and "Remove-Item" not in seen[0] + seen[1]

# run()：池参数在本路线没有语义 → 显式给了整批拒绝；deadline/绑定不合格本地拒，不碰远端
submits = []
sub_reply = {"r": {"ok": False, "stage": "push", "state": "UNKNOWN", "rc": 124, "reason": "NO_CONFIRMED_RECEIPT"}}


def fake_submit(inputs, command, name):
    """W1 task_submit 替身；并按上传的冻结 plan/config 预备这一批 driver 将写出的 journal（轮询与取回同源）。"""
    submits.append((inputs, command, name))
    nplan_r, nconfig_r = (json.loads(Path(p).read_bytes()) for p in inputs[-2:])
    JOURNALS[name] = JOURNALS[nplan_r["journal_directory"]] = drive(nplan_r, nconfig_r)
    return dict(sub_reply["r"], name=name)


xdev_tasks.task_submit = fake_submit
for kw in ({"priority": "Normal"}, {"cpu_budget": 4}, {"model_timeout_s": 60}, {"admission_wait_s": 10}, {"mem_reserve_gib": 1.5}):
    assert n.run([M1], [D], str(tmp / "r0"), 60, GOOD, deadline_s=60, **kw) == 2, kw
assert n.run([M1], [D], str(tmp / "r0"), 60, GOOD) == 2 and n.run([M1], [D], str(tmp / "r0"), 60, GOOD, deadline_s=181) == 2
assert n.run([M1], [D], str(tmp / "r0"), 60, str(tmp / "pin.json"), deadline_s=60) == 2
assert n.run([M1], [D], str(tmp / "r0"), -1, GOOD, deadline_s=60) == 2
assert submits == [] and not (tmp / "r0").exists()
# 整批交给 W1 task_submit 一次：全部 .inp + 共用数据 + plan + config；原次未知不重提；同一 OUT 不再提交
out1 = tmp / "r1"
assert n.run([M1, M2], [D], str(out1), 60, GOOD, deadline_s=90, jobs=2) == 7 and len(submits) == 1
inputs, command, name = submits[0]
ctl = out1 / n.CONTROL_DIR
assert inputs == [str(Path(M1).resolve()), str(Path(M2).resolve()), str(Path(D).resolve()),
                  str(ctl / n.PLAN_FILE), str(ctl / n.CONFIG_FILE)]
saved = json.loads((out1 / b.MANIFEST).read_text(encoding="utf-8"))
assert saved["schema"] == n.SCHEMA and saved["task"]["state"] == "UNKNOWN" and saved["task"]["name"] == name == saved["batch"]
assert saved["plan_sha256"] == hashlib.sha256((ctl / n.PLAN_FILE).read_bytes()).hexdigest()
assert saved["config_sha256"] == hashlib.sha256((ctl / n.CONFIG_FILE).read_bytes()).hexdigest()
assert saved["binding"] == {"path": str(Path(GOOD).resolve()), "sha256": good["sha256"]} and saved["pins"] == n.PINS
assert saved["python_pin"] == PIN and [m["attempt_id"] for m in saved["models"]] == [f"{name}.m1", f"{name}.m2"]
assert command.startswith("# XDEV_MPLUS_NATIVE_BODY") and saved["plan_sha256"] in command and saved["config_sha256"] in command
disk_plan = json.loads((ctl / n.PLAN_FILE).read_text(encoding="utf-8"))
assert disk_plan["batch_id"] == name and [m["deadline_seconds"] for m in disk_plan["models"]] == [90, 90]
assert json.loads((ctl / n.CONFIG_FILE).read_text(encoding="utf-8"))["max_parallel"] == 2
assert n.run([M1, M2], [D], str(out1), 60, GOOD, deadline_s=90) == 3 and len(submits) == 1
# 端到端正对照：提交成功 → 跟到原任务 DONE → 按 journal 收尾
sub_reply["r"] = {"ok": True, "stage": "start", "state": "STARTED", "rc": 0}
polls_seen = []


def e2e_poll(m, full=False):
    polls_seen.append(full)
    return row(m, JOURNALS[m["batch"]], full=full)


n.poll_once = e2e_poll
out2 = tmp / "r2"
assert n.run([M1, M2], [D], str(out2), 60, GOOD, deadline_s=90) == 0 and len(submits) == 2 and polls_seen == [False, True]
final = json.loads((out2 / b.MANIFEST).read_text(encoding="utf-8"))
assert final["ok"] is True and final["task"]["owner"] == OWNER and final["capacity_scope"] == "GENERAL_BATCH_ADMISSION"
assert sorted(final["results"]) == ["m1", "m2"] and final["native_result"]["admission_state"] == "RELEASED"
assert final["journal_verification"] == {"ok": True, "errors": []} and final["python_pin"] == PIN

# resume：经 xdev_mplus_batch.resume 按 schema 分派；状态未知不重放；RESERVED 只调一次 W1 task_resume；从不再提交
resumes = []
xdev_tasks.task_resume = lambda name: resumes.append(name) or {"ok": True, "state": "STARTING", "stage": "start", "rc": 0}
xdev_tasks.task_status = lambda name, nbytes=4000, wait_seconds=45: {"ok": False, "state": "UNKNOWN", "rc": 7,
                                                                      "reason": "ORIGINAL_QUEUE_ID_UNAVAILABLE"}
assert b.resume(str(out1), 60) == 7 and resumes == [] and len(submits) == 2
status_rows = iter([{"ok": True, "state": "RESERVED", "rc": 0, "upload_status": "PENDING"},
                    {"ok": True, "state": "DONE", "rc": 0, "binding_verified": True}])
xdev_tasks.task_status = lambda name, nbytes=4000, wait_seconds=45: next(status_rows)
assert b.resume(str(out1), 60) == 0 and resumes == [name] and len(submits) == 2
assert json.loads((out1 / b.MANIFEST).read_text(encoding="utf-8"))["ok"] is True
b._journal_owner = lambda name: None  # 本机 W1 journal 丢了或换了 → 不续
assert b.resume(str(out2), 60) == 7

# CLI：--deadline 只属于 --native；--native 下给池参数整批拒绝（不静默映射）
env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
cli = [sys.executable, "-B", str(HERE / "xdev.py"), "mplus", M1, "--out", str(tmp / "cli")]
p = subprocess.run(cli + ["--deadline", "60"], env=env, capture_output=True, text=True, timeout=60)
assert p.returncode == 2 and "--deadline belongs to --native" in p.stderr, p.stderr
p = subprocess.run(cli + ["--native", GOOD, "--deadline", "60", "--priority", "Normal"], env=env, capture_output=True, text=True, timeout=60)
assert p.returncode == 2 and "--priority not supported on the --native route" in p.stderr, p.stderr
p = subprocess.run(cli + ["--native", GOOD], env=env, capture_output=True, text=True, timeout=60)
assert p.returncode == 2 and "--native needs --deadline" in p.stderr, p.stderr
assert not (tmp / "cli").exists()
print("XDEV_MPLUS_NATIVE_TEST_PASS")
