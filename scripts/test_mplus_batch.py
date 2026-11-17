#!/usr/bin/env python3
"""离线检查 xdev mplus 多模型批次：不连 Windows、不跑 Mplus。传输、W1 任务与状态作业都用替身，
覆盖 W1 独审复现的三项（暂存互删、rc9 假成功、pull 124 后删源）及冻结 runner、准入、共用输入保护。"""
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
tmp = Path(tempfile.mkdtemp(prefix="xdev-batch-test-"))
(tmp / "state" / "files").mkdir(parents=True)
cfg = tmp / "config.json"
cfg.write_text(json.dumps({"state_dir": str(tmp / "state"), "windows": {"mplus": "C:\\M\\Mplus.exe"}}), encoding="utf-8")
os.environ["XDEV_CONFIG"] = str(cfg)
sys.path.insert(0, str(HERE))
import xdev_files  # noqa: E402
import xdev_mplus_batch as b  # noqa: E402
import xdev_tasks  # noqa: E402

OWNER = "0123456789abcdef0123456789abcdef"
NORMAL = "INPUT READING TERMINATED NORMALLY\nTHE MODEL ESTIMATION TERMINATED NORMALLY\n"


def refused(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except ValueError:
        return True
    return False


# .out 判定：普通模型看 TERMINATED NORMALLY；Monte Carlo 看完成的重复次数；任何错误行都失败
assert b.check_out(NORMAL)["ok"]
mc = "Number of replications\n    Requested       300\n    Completed       300\n"
assert b.check_out(mc)["ok"] and b.check_out(mc)["monte_carlo"] == {"requested": 300, "completed": 300}
assert not b.check_out(mc.replace("Completed       300", "Completed       299"))["ok"]
assert not b.check_out(NORMAL + "*** ERROR in MODEL command")["ok"]
assert not b.check_out("*** FATAL ERROR\n" + NORMAL)["ok"]
assert not b.check_out("   1 ERROR(S) FOUND IN THE INPUT INSTRUCTIONS\n" + NORMAL)["ok"]  # agy N4：只有汇总行
assert not b.check_out("THE MODEL ESTIMATION DID NOT TERMINATE NORMALLY due to an error\n")["ok"]
assert b.check_out("0 ERROR(S) FOUND IN THE INPUT INSTRUCTIONS\n" + NORMAL)["ok"]

# PROCESSORS：取第一个数；注释里的不算；缺省 1
assert b.inp_processors("ANALYSIS:\n  PROCESSORS = 4 2;") == 4
assert b.inp_processors("analysis: type=general; processors are 3;") == 3
assert b.inp_processors("! PROCESSORS = 8;\nPROC = 2;") == 2
assert b.inp_processors("TITLE: process IS fine;") == 1

# 准入参数：有限数字，越界/非数/布尔/小数作业数一律拒绝；省略 jobs/cpu_budget 交 Windows 端按模型数与 CPU 定
lim = b.limits()
assert lim["jobs"] is None and lim["cpu_budget"] is None and lim["admission_wait_s"] == 1800 and lim["priority"] == "Normal"
for bad in ({"jobs": 0}, {"jobs": -1}, {"jobs": 2.5}, {"jobs": True}, {"jobs": 101}, {"cpu_budget": 0},
            {"mem_reserve_gib": float("nan")}, {"mem_reserve_gib": -1}, {"model_mem_gib": 0}, {"model_mem_gib": float("inf")},
            {"commit_reserve_gib": float("-inf")}, {"disk_reserve_gib": "2"}, {"model_timeout_s": 0}, {"admission_wait_s": 0},
            {"priority": "High"}):
    assert refused(b.limits, **bad), bad

# 计划
(tmp / "a").mkdir()
(tmp / "b").mkdir()
for p in (tmp / "a" / "m1.inp", tmp / "b" / "M1.inp", tmp / "a" / "m2.inp"):
    p.write_text("TITLE: t;\nDATA: FILE = d.dat;\nANALYSIS: PROCESSORS = 2;\n", encoding="ascii")
(tmp / "a" / "d.dat").write_text("1 2\n", encoding="ascii")
assert refused(b.plan, [str(tmp / "a" / "m1.inp"), str(tmp / "b" / "M1.inp")], [], lim)  # 同名模型（不分大小写）
assert refused(b.plan, [str(tmp / "a" / "d.dat")], [], lim)                               # 不是 .inp
assert refused(b.plan, [], [], lim)
pl = b.plan([str(tmp / "a" / "m1.inp"), str(tmp / "a" / "m2.inp")], [str(tmp / "a" / "d.dat")], lim)
assert [m["key"] for m in pl["models"]] == ["m1", "m2"] and pl["models"][0]["processors"] == 2
assert xdev_tasks._valid_name(pl["batch"]) and pl["remote_dir"] == xdev_tasks._work_dir(pl["batch"])
assert pl["batch"] != b.plan([str(tmp / "a" / "m1.inp")], [], lim)["batch"]  # 同一秒两次批次名也不同
assert pl["capacity_scope"] == "BATCH_ONLY" and pl["external_host_admission_enforced_by_xdev"] is False
assert pl["shared"][0]["sha256"] == xdev_tasks._input_digest(tmp / "a" / "d.dat")[1]
# 首个模型也不能超线程预算：给了 --cpu-budget 时本地就拒；没给时池里记 REFUSED（见下方池正文检查）
assert refused(b.plan, [str(tmp / "a" / "m1.inp")], [], b.limits(cpu_budget=1))
assert b.plan([str(tmp / "a" / "m1.inp")], [], b.limits(cpu_budget=2))
# agy N2：模型写出与共用输入同名的文件（SAVEDATA / MONTECARLO SAVE / DATA IMPUTATION SAVE）整批拒绝；只读不拒
(tmp / "w").mkdir()
writers = {"s1.inp": "DATA: FILE = d.dat;\nSAVEDATA:\n  FILE IS D.DAT;\n",
           "s2.inp": "MONTECARLO: NAMES = y; NOBSERVATIONS = 10; NREPS = 2;\n  SAVE = d*.dat;\n",
           "s3.inp": "DATA: FILE = x.dat;\nDATA IMPUTATION: IMPUTE = y;\n  SAVE = ..\\d.dat;\n",
           "s4.inp": "DATA: FILE = d.dat;\nSAVEDATA: FILE = fscores.dat; SAVE = FSCORES;\n"}
for fname, text in writers.items():
    (tmp / "w" / fname).write_text(text, encoding="ascii")
for fname in ("s1.inp", "s2.inp", "s3.inp"):
    assert refused(b.plan, [str(tmp / "w" / fname)], [str(tmp / "a" / "d.dat")], lim), fname
assert b.plan([str(tmp / "w" / "s4.inp")], [str(tmp / "a" / "d.dat")], lim)
# W1 task 每个任务最多 128 个输入；批次最多 100 个模型
many = []
for i in range(101):
    (tmp / "w" / f"k{i}.inp").write_text("TITLE: t;\n", encoding="ascii")
    many.append(str(tmp / "w" / f"k{i}.inp"))
extra = []
for i in range(29):
    (tmp / "w" / f"x{i}.dat").write_text("1\n", encoding="ascii")
    extra.append(str(tmp / "w" / f"x{i}.dat"))
assert refused(b.plan, many, [], lim) and b.plan(many[:100], [], lim)
assert refused(b.plan, many[:100], extra, lim) and b.plan(many[:100], extra[:28], lim)

# W1 独审 P1-5：RUNNER 是冻结模板，不按当前配置改写；_start 原样提交 journal 里的 runner 字节
assert "__PRIORITY__" not in xdev_tasks.RUNNER and not hasattr(xdev_tasks, "_with_priority")
assert "PriorityClass" not in xdev_tasks.RUNNER and "SetProcessInformation" not in xdev_tasks.RUNNER
seen_scripts = []
real_remote = xdev_tasks._remote
xdev_tasks._remote = lambda script, *a, **k: seen_scripts.append(script) or {"ok": True, "job_id": 1}
xdev_tasks._start("n1", OWNER, "body", runner=xdev_tasks.RUNNER)
xdev_tasks._remote = real_remote
assert xdev_tasks.ps_quote(xdev_tasks.RUNNER) in seen_scripts[0]

# W1 独审 P1-2：批次不再自建按内容共享的暂存；上传走 W1 push（暂存名含 transfer_id），同输入两次不会同名
module =(HERE / "xdev_mplus_batch.py").read_text(encoding="utf-8")
assert not hasattr(b, "_stage") and not hasattr(b, "upload") and '"files"' not in module and "Invoke-WebRequest" not in module
s1 = xdev_files._push_staged({"source": "/x/d.dat", "sha256": "a" * 64, "transfer_id": "1" * 32})
s2 = xdev_files._push_staged({"source": "/x/d.dat", "sha256": "a" * 64, "transfer_id": "2" * 32})
assert s1 != s2

# 池正文：首个模型也过线程预算；共用输入只读；每子 TEMP/TMP；链接按文件号/SHA 认，不按长度/时间；内核起止时间 + 单调计时
body = b.pool_command(pl, lim, "C:\\M\\Mplus.exe")
assert body.startswith("# XDEV_MPLUS_POOL") and "__CFG__" not in body
assert "$running.Count -gt 0 -and" not in body and "Final $m 'REFUSED'" in body and "function BatchAdmit" in body
assert ".IsReadOnly = $true" in body and "$env:TEMP = $tdir; $env:TMP = $tdir" in body
assert "LastWriteTimeUtc" not in body and ".Length -eq" not in body and "FileId(" in body
assert "$p.ExitTime" in body and "Stopwatch" in body and "max_alive_observed" in body
assert "$R " not in body and "$R)" not in body and "[NullString]::Value" in body  # $Root 不能叫 $R：PS 变量不分大小写
cfg_json = json.loads(body.split("$cfg = ", 1)[1].split(" | ConvertFrom-Json", 1)[0][1:-1].replace("''", "'"))
assert cfg_json["priority"] == "Normal" and cfg_json["shared"] == [{"name": "d.dat", "sha256": pl["shared"][0]["sha256"]}]
assert len(body.encode()) < 60 * 1024 and len(b.pool_command(b.plan(many[:100], extra[:28], lim), lim, "C:\\M\\Mplus.exe").encode()) < 60 * 1024

# 计时：内核起止区间；首尾相接不算重叠
t = b.timing({"x": {"start_utc": "2026-11-16T08:48:28.7613939Z", "exit_utc": "2026-11-16T08:49:00.7613939Z"},
              "y": {"start_utc": "2026-11-16T08:48:29Z", "exit_utc": "2026-11-16T08:49:01Z"},
              "z": {"start_utc": "2026-11-16T08:49:01Z", "exit_utc": "2026-11-16T08:49:11Z"}},
             {"max_alive_observed": 2, "elapsed_ms": 42500})
assert t["kernel_interval_peak"] == 2 and t["kernel_span_s"] == 42.239 and t["max_alive_observed"] == 2 and t["pool_elapsed_s"] == 42.5, t

# ---- follow() 负控：状态作业、pull_dir、W1 task_status 全用替身；不许出现任何别的远端调用 ----
b.time.sleep = lambda s: None
REAL_POLL = b.poll_once
remote_calls = []
b.run_ps = lambda *a, **k: remote_calls.append(a) or (1, "", "unexpected", 0)
pulls = []
pull_mode = {"rc": 0}


def fake_pull(remote, local):
    pulls.append((remote, local))
    local = Path(local)
    if pull_mode["rc"] == 124:  # 原次结果未知：认领文件留着，远端 zip 不动
        local.with_name("." + local.name + ".xdev-claim.json").write_text(json.dumps({"transfer_id": "9" * 32}), encoding="utf-8")
        return 124
    local.mkdir(parents=True)
    (local / (local.name + ".out")).write_text(NORMAL, encoding="utf-8")
    return 0


b.pull_dir = fake_pull
task_rc = {"rc": 0}
xdev_tasks.task_status = lambda name, nbytes=4000, wait_seconds=45: {"ok": True, "state": "DONE", "rc": task_rc["rc"],
                                                                      "binding_verified": True, "job_id": 77}
POOL = {"owner": OWNER, "at": 100, "finished": True, "shared_verified": True, "max_alive_observed": 1,
        "elapsed_ms": 6000, "running": [], "pending": 0, "done": 1, "hold": ""}


def done_state(key, rc, pid=10, state="DONE"):
    return {"owner": OWNER, "key": key, "state": state, "rc": rc, "pid": pid, "start_utc": "2026-11-17T08:00:00.0000000Z",
            "exit_utc": "2026-11-17T08:00:05.0000000Z", "start_ms": 100, "observed_exit_ms": 5200}


def make_man(name):
    out_dir = tmp / name
    out_dir.mkdir()
    man = {"schema": b.SCHEMA, "batch": name, "remote_dir": xdev_tasks._work_dir(name), "shared": [],
           "models": [{"key": "m1", "local_inp": "/x/m1.inp", "inp": "m1.inp", "out": "m1.out", "processors": 1}],
           "task": {"name": name, "remote_dir": xdev_tasks._work_dir(name), "owner": OWNER},
           "states": {}, "seen": {}, "results": {}}
    return man, out_dir


def polls(*rows):
    it = iter(rows)
    b.poll_once = lambda man: next(it)


def poll_row(states, runner="0 2026-11-17T08:00:06Z", pool=None):
    return {"ok": True, "now": 100, "pool": dict(POOL, **(pool or {})), "states": states, "runner": runner, "job": 5}

# W1 独审 P1-1 复现件：模型 rc9、runner rc9、.out 带正常结束文字 → 不能成功
man, out_dir = make_man("rc9")
task_rc["rc"] = 9
polls(poll_row({"m1": done_state("m1", 9)}, runner="9 2026-11-17T08:00:06Z", pool={"failed": 1}))
assert b.follow(man, out_dir, 60) == 1 and man["ok"] is False
assert man["results"]["m1"]["ok"] is False and man["results"]["m1"]["rc"] == 9 and man["results"]["m1"]["check"]["ok"] is True
# 模型 rc0 但原任务 rc≠0（池报失败）→ 批次不成功；共用输入 SHA 变了 → 不成功
for name, rc, pool in (("runner3", 3, {}), ("sharedbad", 0, {"shared_verified": False})):
    man, out_dir = make_man(name)
    task_rc["rc"] = rc
    polls(poll_row({"m1": done_state("m1", 0)}, pool=pool))
    assert b.follow(man, out_dir, 60) == 1 and man["ok"] is False and man["results"]["m1"]["ok"] is True, name
# 正对照：全部 rc0、池收尾、共用输入未变 → 成功；先看到 RUNNING 再 DONE，身份一致
man, out_dir = make_man("good")
task_rc["rc"] = 0
polls(poll_row({"m1": dict(done_state("m1", None), state="RUNNING")}, runner=None, pool={"finished": False}),
      poll_row({"m1": done_state("m1", 0)}))
assert b.follow(man, out_dir, 60) == 0 and man["ok"] is True and man["timing"]["kernel_interval_peak"] == 1
assert man["results"]["m1"]["collect"]["status"] == "COMPLETE" and man["results"]["m1"]["kernel_wall_s"] == 5.0
assert man["results"]["m1"]["observed_wall_s"] == 5.1 and man["task"]["final"]["rc"] == 0
# 身份变了（RUNNING 时 pid 10，DONE 时 pid 11）→ 不成功
man, out_dir = make_man("ident")
polls(poll_row({"m1": dict(done_state("m1", None), state="RUNNING")}, runner=None, pool={"finished": False}),
      poll_row({"m1": done_state("m1", 0, pid=11)}))
assert b.follow(man, out_dir, 60) == 1 and man["results"]["m1"]["identity_ok"] is False
# 池退出时模型没有终态（池异常）：不取回、不补跑
man, out_dir = make_man("noterminal")
n_pulls = len(pulls)
polls(poll_row({"m1": dict(done_state("m1", None), state="RUNNING")}, runner="1 x", pool={"finished": False}))
task_rc["rc"] = 1
assert b.follow(man, out_dir, 60) == 1 and man["results"]["m1"]["state"] == "RUNNING" and len(pulls) == n_pulls
task_rc["rc"] = 0
# 池没起模型（REFUSED / NOT_ADMITTED / START_FAILED）：不取回，记失败
man, out_dir = make_man("refused")
polls(poll_row({"m1": {"owner": OWNER, "key": "m1", "state": "REFUSED", "reason": "PROCESSORS 9 > cpu budget 4"}}, pool={"failed": 1}))
assert b.follow(man, out_dir, 60) == 1 and man["results"]["m1"]["state"] == "REFUSED" and len(pulls) == n_pulls

# W1 独审 P1-2 复现件：pull 返回 124 → 记 UNKNOWN，不发删除、不重取；再 --resume 也不再取
man, out_dir = make_man("pull124")
pull_mode["rc"] = 124
n_pulls = len(pulls)
polls(poll_row({"m1": done_state("m1", 0)}))
assert b.follow(man, out_dir, 60) == 7 and remote_calls == []
assert man["results"]["m1"]["collect"]["status"] == "UNKNOWN" and man["results"]["m1"]["collect"]["claim"]["transfer_id"] == "9" * 32
del man["results"]["m1"]["ok"]  # 好比上次在判定前中断
polls(poll_row({"m1": done_state("m1", 0)}))
assert b.follow(man, out_dir, 60) == 7 and len(pulls) == n_pulls + 1 and remote_calls == []
# 取回途中断开（只留下 STARTED）→ 下次记 UNKNOWN，不重取
man, out_dir = make_man("pullstarted")
man["results"]["m1"] = {"collect": {"status": "STARTED", "local": str(out_dir / "m1")}}
polls(poll_row({"m1": done_state("m1", 0)}))
assert b.follow(man, out_dir, 60) == 7 and man["results"]["m1"]["collect"]["status"] == "UNKNOWN" and len(pulls) == n_pulls + 1
pull_mode["rc"] = 0

# poll_once：状态里的 owner 不是本批次的 → 致命，follow 什么都不取
b.poll_once = REAL_POLL
b.run_ps = lambda *a, **k: (0, "XDEV_JSON " + json.dumps({"ok": True, "now": 1, "runner": None, "pool": None,
                            "states": [json.dumps({"owner": "f" * 32, "key": "m1", "state": "DONE", "rc": 0})]}) + "\n", "", 9)
man, out_dir = make_man("ownerbad")
r = b.poll_once(man)
assert r["ok"] is False and r["fatal"] is True and "STATE_OWNER_MISMATCH" in r["reason"]
b.pull_dir = fake_pull
n_pulls = len(pulls)
assert b.follow(man, out_dir, 60) == 7 and len(pulls) == n_pulls
b.run_ps = lambda *a, **k: (0, "XDEV_JSON " + json.dumps({"ok": True, "now": 1, "runner": None, "pool": json.dumps(POOL),
                            "states": [json.dumps(done_state("m1", 0)), '{"owner": "' + OWNER + '", "key": "m1", "sta']}) + "\n", "", 9)
r = b.poll_once(man)
assert r["ok"] and r["states"]["m1"]["rc"] == 0 and r["pool"]["finished"] is True  # 半截文件下一轮再读
b.run_ps = lambda *a, **k: (0, 'XDEV_JSON {"ok": false, "reason": "TASK_BINDING_MISMATCH"}\n', "", 9)
assert b.poll_once(man)["fatal"] is True
script_seen = []
b.run_ps = lambda script, **k: script_seen.append(script) or (124, "", "", 9)
assert b.poll_once(man)["ok"] is False and b.poll_once(man).get("fatal") is False
assert ("$meta.owner -cne '" + OWNER + "'") in script_seen[0] and "rc.txt" in script_seen[0]
assert script_seen[0].index("rc.txt") < script_seen[0].index("xdev_state.json")  # 先读 runner 回执，后读模型状态

# run()：整批交给 W1 task_submit 一次（全部 .inp + 共用数据、池正文、批次名）；结果未知不重提
submits = []
xdev_tasks.task_submit = lambda inputs, command, name: submits.append((inputs, command, name)) or {
    "ok": False, "name": name, "stage": "push", "state": "UNKNOWN", "rc": 124, "reason": "NO_CONFIRMED_RECEIPT"}
inps = [str(tmp / "a" / "m1.inp"), str(tmp / "a" / "m2.inp")]
out_dir = tmp / "run1"
assert b.run(inps, [str(tmp / "a" / "d.dat")], str(out_dir), 60) == 7 and len(submits) == 1
inputs, command, name = submits[0]
assert inputs == [str((tmp / "a" / "m1.inp").resolve()), str((tmp / "a" / "m2.inp").resolve()), str((tmp / "a" / "d.dat").resolve())]
assert command.startswith("# XDEV_MPLUS_POOL") and name.startswith("mb")
saved = json.loads((out_dir / b.MANIFEST).read_text(encoding="utf-8"))
assert saved["task"]["state"] == "UNKNOWN" and saved["task"]["name"] == name and saved["capacity_scope"] == "BATCH_ONLY"
assert b.run(inps, [str(tmp / "a" / "d.dat")], str(out_dir), 60) == 3 and len(submits) == 1  # 同一 OUT 不再提交
assert b.run(inps, [], str(tmp / "run2"), 60, jobs=0) == 2 and len(submits) == 1             # 参数不合格：本地拒，不碰远端
assert b.run(inps, [], str(tmp / "run2"), -1) == 2 and len(submits) == 1
# resume：没有本机 journal → 拒；原次 UNKNOWN → 不重放；原预约未启动（RESERVED）→ 只调一次 W1 task_resume
assert b.resume(str(out_dir), 60) == 7
b._journal_owner = lambda n: OWNER
resumes = []
xdev_tasks.task_resume = lambda n: resumes.append(n) or {"ok": True, "state": "STARTING", "stage": "start", "rc": 0}
xdev_tasks.task_status = lambda n, nbytes=4000, wait_seconds=45: {"ok": False, "state": "UNKNOWN", "rc": 7, "reason": "ORIGINAL_QUEUE_ID_UNAVAILABLE"}
assert b.resume(str(out_dir), 60) == 7 and resumes == []
status_rows = iter([{"ok": True, "state": "RESERVED", "rc": 0, "upload_status": "PENDING"},
                    {"ok": True, "state": "DONE", "rc": 0, "binding_verified": True}])
xdev_tasks.task_status = lambda n, nbytes=4000, wait_seconds=45: next(status_rows)
polls(poll_row({k: done_state(k, 0) for k in ("m1", "m2")}, pool={"done": 2}))
assert b.resume(str(out_dir), 60) == 0 and resumes == [name] and len(submits) == 1
final = json.loads((out_dir / b.MANIFEST).read_text(encoding="utf-8"))
assert final["ok"] is True and sorted(final["results"]) == ["m1", "m2"] and final["timing"]["kernel_interval_peak"] == 2
# 旧版（v1）清单：那时批次不是 W1 任务，只保留原记录
(tmp / "v1").mkdir()
(tmp / "v1" / b.MANIFEST).write_text(json.dumps({"schema": "xdev-mplus-batch-v1"}), encoding="utf-8")
assert b.resume(str(tmp / "v1"), 60) == 3
print("XDEV_MPLUS_BATCH_TEST_PASS")
