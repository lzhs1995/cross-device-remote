#!/usr/bin/env python3
"""离线检查 xdev mplus --native：不连 Windows、不跑 driver 或 Mplus。绑定、driver plan/config 字段、任务正文、
状态作业和收尾都用替身；覆盖 journal 缺失/半截/外批、未决成员、SHA 不符、.out 失败、pull 124 不重取、不重提。"""
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

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


BINDING = {"schema": n.BINDING_SCHEMA, "work_root": WORK, "python": {"path": "C:\\XP\\python.exe"},
           "driver": {"path": "C:\\XN\\native_batch_driver.py", "sha256": n.PINS["driver"]},
           "adapter": {"path": "C:\\XN\\general_batch_admission.py", "sha256": n.PINS["adapter"]},
           "backend": {"path": "C:\\XN\\win_process.py", "sha256": n.PINS["backend"]},
           "engine": {"exe_path": "C:\\XM\\Mplus.exe", "exe_sha256": "e" * 64},
           "admission": {"helper_module": "C:\\XN\\host_admission_adapter.py", "provider_directory": "C:\\XN\\providers",
                         "registry": "C:\\XR\\reservations.sqlite", "original_controller": "controller-1", "policy_id": "policy-1",
                         "policy": {"schema": "host-measured-policy-v1", "volume_reserve_bytes": {"C:\\": 20_000_000_000}},
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
bad_bindings = {
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
    assert set(r) == REQUEST_FIELDS and r["attempt_id"] == m["attempt_id"] == a and r["route"] == "DIRECT_MPLUS"
    assert r["original_controller"] == "controller-1" and r["model_id"] == a
    assert r["working_directory"] == f"{W}\\models\\{a}\\cwd" and r["temp_directory"] == f"{W}\\models\\{a}\\tmp"
    assert row["input_source"] == f"{W}\\{m['inp']}" and row["input_name"] == m["inp"] and row["deadline_seconds"] == 60
    assert row["data_sources"] == {"d.dat": f"{W}\\d.dat"} and r["data_sha256"] == {"d.dat": xdev_tasks._input_digest(P / "d.dat")[1]}
    assert r["input_sha256"] == xdev_tasks._input_digest(Path(m["local_inp"]))[1] == m["input_sha256"]
    assert r["processors"] == 2 and r["exe_path"] == "C:\\XM\\Mplus.exe" and r["exe_sha256"] == "e" * 64
    assert r["physical_bytes"] == 2 ** 30 and r["output_budget_bytes"] == 2 ** 20
for f in ("attempt_id", "request_id", "job_id"):
    assert len({row["request"][f] for row in nplan["models"]}) == 2, f
assert set(nconfig) == CONFIG_FIELDS and nconfig["schema"] == n.CONFIG_SCHEMA and nconfig["max_parallel"] == 2
assert all(nconfig[k] == BINDING["admission"][k] for k in n.ADMISSION_KEYS)  # 策略、窗口、provider 原样转交 adapter 核验
assert pl["capacity_scope"] == "GENERAL_BATCH_ADMISSION" and pl["max_parallel"] == 2 and pl["deadline_seconds"] == 60
assert "external_host_admission_enforced_by_xdev" not in pl and "host_reservations" not in pl
assert n.native_plan([M1, M2], [D], good, 60, jobs=1)[2]["max_parallel"] == 1
assert refused(n.native_plan, [M1, M2], [D], good, 60, jobs=3)            # K <= 批大小
assert refused(n.native_plan, [M1], [D], good, None) and refused(n.native_plan, [M1], [D], good, 181)
assert refused(n.native_plan, [str(P / "m 3.inp")], [], good, 60)          # driver 的 input_name 规则
assert refused(n.native_plan, [str(P / "_m4.inp")], [], good, 60)
for bad_name in ("d d.dat", "m1.out", "_xdev_native_plan.json"):        # 共用输入名：ASCII、不与 .inp/.out/控制文件同名
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
for marker in ("WORK_DIR_MISMATCH", "TASK_NAME_IS_NOT_BATCH_ID", "SHA_MISMATCH", "PYTHON_MISSING", "JOURNAL_EXISTS_NO_REPLAY",
               "MODELS_DIR_EXISTS_NO_REPLAY", "[NullString]::Value", "$p.WaitForExit()", "exit ([int]$code)", "DRIVER_EXIT_UNKNOWN"):
    assert marker in body, marker
for banned in ("sqlite", "reservation", "priorityclass", "ecoqos", "xdev_pool", "xdev_state", "taskkill", "stop-process"):
    assert banned not in body.lower(), banned
assert max(body.index(m) for m in ("SHA_MISMATCH", "JOURNAL_EXISTS_NO_REPLAY", "MODELS_DIR_EXISTS_NO_REPLAY")) < body.index("Start-Process")
assert "$W" not in body  # 正文的桥接回执叫 $w；PS 变量不分大小写，不能再有 $W

# ---- 收尾负控：状态作业、pull_dir、W1 task_status 全用替身 ----
b.time.sleep = lambda s: None
REAL_POLL = n.poll_once
pulls = []
pull_mode = {"rc": {}, "text": {}}


def fake_pull(remote, local):
    pulls.append((remote, local))
    local = Path(local)
    if remote.endswith("\\" + n.JOURNAL):
        local.mkdir(parents=True)
        (local / "RESULT.json").write_text("{}", encoding="utf-8")
        return 0
    if pull_mode["rc"].get(local.name) == 124:  # 原次结果未知：认领文件留着，远端 zip 不动
        local.with_name("." + local.name + ".xdev-claim.json").write_text(json.dumps({"transfer_id": "9" * 32}), encoding="utf-8")
        return 124
    local.mkdir(parents=True)
    (local / (local.name + ".out")).write_text(pull_mode["text"].get(local.name, NORMAL), encoding="utf-8")
    return 0


b.pull_dir = fake_pull
task_rc = {"rc": 0, "state": "DONE"}
statuses = []
xdev_tasks.task_status = lambda name, nbytes=4000, wait_seconds=45: statuses.append(name) or {
    "ok": True, "state": task_rc["state"], "rc": task_rc["rc"], "binding_verified": True, "job_id": 77}


def member(a, **kw):
    m = {"attempt_id": a, "pid": 100, "exit_code": 0, "terminal": "EXITED", "descendants_complete": True,
         "output_sha256": sha(NORMAL), "validation_errors": [], "processes_ok": True,
         "scientific_validation": "NOT_PERFORMED", "engine_interval_confirmed": False}
    m.update(kw)
    return m


def terminal(a, birth, end):
    return {"attempt_id": a, "terminal": "EXITED", "exit_code": 0, "birth_filetime_100ns": birth, "native_exit_filetime_100ns": end}


def result_doc(name, members, **kw):
    doc = {"schema": n.RESULT_SCHEMA, "batch_id": name, "admission_state": "RELEASED", "evidence_class": "NATIVE_COLLECTION",
           "processes_ok": all(m["processes_ok"] for m in members), "scientific_validation": "NOT_PERFORMED",
           "members": members, "errors": [], "unresolved_attempt_ids": []}
    doc.update(kw)
    return doc


def make_man(name, k=2):
    out_dir = tmp / name
    out_dir.mkdir()
    man = {"schema": n.SCHEMA, "batch": name, "remote_dir": xdev_tasks._work_dir(name), "work_dir": WORK + "\\" + name,
           "shared": [], "plan_sha256": "a" * 64, "config_sha256": "b" * 64,
           "models": [{"key": f"m{i}", "attempt_id": f"m{i}", "local_inp": f"/x/m{i}.inp", "inp": f"m{i}.inp",
                       "out": f"m{i}.out", "processors": 1} for i in range(1, k + 1)],
           "task": {"name": name, "remote_dir": xdev_tasks._work_dir(name), "owner": OWNER}, "results": {}}
    return man, out_dir


def wrapper(man, **kw):
    w = {"schema": "xdev-mplus-native-wrapper-v1", "owner": OWNER, "name": man["task"]["name"], "batch_id": man["batch"],
         "plan_sha256": man["plan_sha256"], "config_sha256": man["config_sha256"], "state": "DRIVER_EXITED", "driver_exit_code": 0}
    w.update(kw)
    return w


def row(man, full=False, result=None, members=None, files=None, runner="0 2026-11-18T00:00:06Z", journal=True, **kw):
    """files: attempt -> (terminal, result) 原文；缺省按 RESULT 成员生成且与之一致。"""
    members = members if members is not None else [member(m["attempt_id"]) for m in man["models"]]
    result = result if result is not None else result_doc(man["batch"], members)
    if files is None:
        files = {x["attempt_id"]: (json.dumps(terminal(x["attempt_id"], 10 ** 7 * i, 10 ** 7 * (i + 5))), json.dumps(x))
                 for i, x in enumerate(members)}
    r = {"ok": True, "job": 5, "now": 1, "runner": runner, "wrapper": wrapper(man), "journal_exists": journal,
         "result_exists": journal and result != "MISSING", "events": 9, "last_event": {"event": "DRIVER_RESULT_PERSISTED"},
         "driver_stderr": None,
         "result": (None if result == "MISSING" else result) if full else None,
         "members": ({a: {"attempt_id": a, "terminal": t, "result": res} for a, (t, res) in files.items()} if full
                     else {a: {"attempt_id": a, "settled": True} for a in files})}
    r.update(kw)
    return r


def polls(man, light=None, **kw):
    """首轮轻量状态（runner 已出现），收尾一轮全量证据。"""
    n.poll_once = lambda m, full=False: row(man, full=full, **kw) if full else (light or row(man, **kw))


# 正对照：两模型区间重叠；按 plan 的 working_directory 取回、.out SHA 等于 driver 记录、文本判定通过
man, out_dir = make_man("good")
polls(man)
assert n.follow(man, out_dir, 60) == 0 and man["ok"] is True and man["stage"] == "RESULT" and man["exit_code"] == 0
assert [p[0] for p in pulls] == [man["remote_dir"] + "\\models\\m1\\cwd", man["remote_dir"] + "\\models\\m2\\cwd",
                                 man["remote_dir"] + "\\driver-evidence"]
assert man["timing"] == {"kernel_span_s": 6.0, "kernel_sum_s": 10.0, "kernel_interval_peak": 2}
for k in ("m1", "m2"):
    rec = man["results"][k]
    assert rec["ok"] and rec["output_sha_ok"] and rec["attempt_result_consistent"] and rec["check"]["ok"] and not rec["evidence_mismatch"]
    assert rec["scientific_validation"] == "NOT_PERFORMED"
assert man["journal_collect"]["status"] == "COMPLETE" and statuses == ["good"]


def case(name, k=2, **kw):
    man, out_dir = make_man(name, k)
    polls(man, **kw)
    start = len(pulls)
    rc = n.follow(man, out_dir, 60)
    return rc, man, pulls[start:]


# driver 没建 journal（正文拒绝或依赖不符）：没起进程、不取回，记 NOT_STARTED 和原因
man0, _ = make_man("tmpl")
rc, man, got = case("nojournal", journal=False, wrapper=wrapper(man0, state="REFUSED", reason="SHA_MISMATCH driver"))
assert rc == 1 and got == [] and man["stage"] == "NOT_STARTED" and man["results"]["m1"]["reason"] == "SHA_MISMATCH driver"
# journal 在但 RESULT 缺失、半截、外批或别的 schema：原次未知，不取回、不重调
for label, res in (("resmissing", "MISSING"), ("respartial", "PARTIAL"),
                   ("resforeign", result_doc("other-batch", [member("m1"), member("m2")])),
                   ("resschema", result_doc("resschema", [member("m1"), member("m2")], schema="native-general-batch-result-v0"))):
    rc, man, got = case(label, result=res)
    assert rc == 7 and got == [] and man["stage"] == "RESULT_UNKNOWN" and man["ok"] is False, label
# 未决成员（driver 关不上的原进程）：只取已结算的，未决的记 UNRESOLVED，整批 7
rc, man, got = case("unresolved", members=[member("m1")],
                    result=result_doc("unresolved", [member("m1")], admission_state="UNKNOWN", processes_ok=False,
                                      unresolved_attempt_ids=["m2"]))
assert rc == 7 and [p[0].rsplit("\\", 2)[-2] for p in got[:1]] == ["m1"] and len(got) == 2
assert man["results"]["m2"]["state"] == "UNRESOLVED" and man["results"]["m1"]["ok"] is True and man["ok"] is False
# driver 记录的 .out SHA 与取回的不同 → 证据不一致，7，不重取
rc, man, got = case("shabad", members=[member("m1", output_sha256="0" * 64), member("m2")])
assert rc == 7 and man["results"]["m1"]["evidence_mismatch"] is True and man["results"]["m1"]["ok"] is False and len(got) == 3
# attempt 结果文件与 RESULT 成员不同 → 证据不一致，7
ms = [member("m1"), member("m2")]
rc, man, got = case("attemptbad", members=ms, files={"m1": (json.dumps(terminal("m1", 0, 1)), json.dumps(member("m1", pid=999))),
                                                      "m2": (json.dumps(terminal("m2", 0, 1)), json.dumps(ms[1]))})
assert rc == 7 and man["results"]["m1"]["attempt_result_consistent"] is False and man["results"]["m1"]["evidence_mismatch"]
assert man["results"]["m1"]["ok"] is False and man["results"]["m2"]["ok"] is True
# processes_ok 不是科学验收：SHA 一致但 .out 带错误行 → 失败 1（不是未知）
pull_mode["text"]["m1"] = BAD_OUT
rc, man, got = case("outbad", members=[member("m1", output_sha256=sha(BAD_OUT)), member("m2")])
assert rc == 1 and man["results"]["m1"]["output_sha_ok"] and man["results"]["m1"]["check"]["ok"] is False
assert man["results"]["m1"]["ok"] is False and man["results"]["m2"]["ok"] is True
del pull_mode["text"]["m1"]
# 非零退出（rc9 但 .out 文字正常）→ 失败
rc, man, got = case("rc9", members=[member("m1", exit_code=9, processes_ok=False), member("m2")])
assert rc == 1 and man["results"]["m1"]["ok"] is False and man["results"]["m1"]["check"]["ok"] is True
# 原任务 rc≠0 或预约没有释放 → 批次不成功
task_rc["rc"] = 1
rc, man, got = case("taskrc1")
assert rc == 1 and man["ok"] is False and all(man["results"][k]["ok"] for k in ("m1", "m2"))
task_rc["rc"] = 0
rc, man, got = case("notreleased", result=result_doc("notreleased", [member("m1"), member("m2")], admission_state="UNKNOWN"))
assert rc != 0 and man["ok"] is False
# W1 task_status 没核到 DONE → 7，什么都不取
task_rc["state"] = "UNKNOWN"
rc, man, got = case("taskunknown")
assert rc == 7 and got == [] and "stage" not in man
task_rc["state"] = "DONE"
# pull 124 → 该模型 UNKNOWN，认领留着；整批 7；--resume 只重报原结论，不再取、不再查
pull_mode["rc"]["m2"] = 124
rc, man, got = case("pull124")
assert rc == 7 and man["results"]["m2"]["collect"]["status"] == "UNKNOWN" and man["results"]["m1"]["ok"] is True
del pull_mode["rc"]["m2"]
b._journal_owner = lambda name: OWNER
n.poll_once = lambda m, full=False: (_ for _ in ()).throw(AssertionError("resume of a concluded batch must not poll"))
start, n_status = len(pulls), len(statuses)
assert b.resume(str(tmp / "pull124"), 60) == 7 and len(pulls) == start and len(statuses) == n_status
# 状态作业报桥接回执不属于本批 → 致命，不查 task_status、不取回
man, out_dir = make_man("wrapbad")
n.poll_once = lambda m, full=False: {"ok": False, "fatal": True, "reason": "WRAPPER_BINDING_MISMATCH job=5 {}"}
start, n_status = len(pulls), len(statuses)
assert n.follow(man, out_dir, 60) == 7 and len(pulls) == start and len(statuses) == n_status
# runner 回执一直没出现 → 124，原任务在 Windows 上继续；收尾全量读三次都失败 → 7，不取回
man, out_dir = make_man("still")
polls(man, light=row(man, runner=None))
assert n.follow(man, out_dir, 0) == 124 and "final" not in man["task"]
man, out_dir = make_man("unread")
light = row(man)
n.poll_once = lambda m, full=False: {"ok": False, "fatal": False, "reason": "rc=124"} if full else light
start = len(pulls)
assert n.follow(man, out_dir, 60) == 7 and len(pulls) == start

# poll_once：先读 runner 回执，再读桥接回执和 journal；桥接回执里的批次/SHA/owner 不符 → 致命
n.poll_once = REAL_POLL
man, out_dir = make_man("pollonce")


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
                 members=[{"attempt_id": "m1", "terminal": None, "result": json.dumps(member("m1"))}])
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
xdev_tasks.task_submit = lambda inputs, command, name: submits.append((inputs, command, name)) or dict(sub_reply["r"], name=name)
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
    return row(m, full=full)


n.poll_once = e2e_poll
out2 = tmp / "r2"
assert n.run([M1, M2], [D], str(out2), 60, GOOD, deadline_s=90) == 0 and len(submits) == 2 and polls_seen == [False, True]
final = json.loads((out2 / b.MANIFEST).read_text(encoding="utf-8"))
assert final["ok"] is True and final["task"]["owner"] == OWNER and final["capacity_scope"] == "GENERAL_BATCH_ADMISSION"
assert sorted(final["results"]) == ["m1", "m2"] and final["native_result"]["admission_state"] == "RELEASED"

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
