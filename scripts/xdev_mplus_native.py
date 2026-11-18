"""xdev mplus a.inp b.inp ... --native BINDING --deadline S：走 W1 GeneralBatchAdmission 的主机级准入路线。

整批仍是一个 W1 具名任务（task_submit 冻结意图、逐文件上传留回执、只启动一次）。和 BATCH_ONLY 池不同，
任务正文不自己起 Mplus：先核工作目录、依赖（driver/adapter/backend）和上传的 plan/config 的精确 SHA，
再用绑定里批准的 Windows Python 同步调用 W1 的 native_batch_driver 一次，并把它的退出码原样交给 runner。
同库预约、挂起创建、Job、原句柄终态核验与释放都在这个长寿命 Python 进程里；PowerShell 不碰预约。

Mac 端等原任务终态后先把整个 driver journal 取回一次并逐项核验（PLAN 等于冻结 plan、事件序列完整、
每个成员的 SPAWN/IDENTITY 事件与完整 terminal、result 同属本批原 attempt、RESULT 与轮询原文一致、成员集
不多不少不重复）；核过了才按 plan 的 working_directory 逐个 pull_dir 取回，核 .out 的 SHA 与 driver 记录相同，
再做 .out 文本判定。journal 没取回或核不过，整批按证据未知（7）处理，不重取、不重调。
driver 的 processes_ok 只说明进程和文件检查通过，不是科学验收；不读也不伪造旧池的 xdev_state/xdev_pool。

attempt_id 带批次名：adapter 以 (controller, attempt_id) 为永久主键，同名模型在下一批不能再用同一个 id。

绑定文件私有（Windows 路径、预算、协调窗口、Python/psutil 身份），不进仓库。现有接口限制：成员 deadline 必须
0 < S <= 180 秒；没有优先级/EcoQoS 选项；只支持 DIRECT_MPLUS、同批一个引擎。
"""
import hashlib
import json
import math
import re
import sys
import time
from pathlib import Path, PureWindowsPath

import xdev_mplus_batch as batch
import xdev_tasks
from xdev_core import emitted, fail, ps_quote, run_ps

SCHEMA = "xdev-mplus-native-v1"
BINDING_SCHEMA = "xdev-mplus-native-binding-v1"
PLAN_SCHEMA = "native-general-batch-plan-v1"
CONFIG_SCHEMA = "general-batch-admission-v1"
RESULT_SCHEMA = "native-general-batch-result-v1"
# W1 独审接受的配对（successor03）；绑定里写的 SHA 必须与此相同
PINS = {"driver": "7412f8de15ef899aa2dce5277cabb308e377b15939478c8caa95f0f03309cb85",
        "adapter": "278866890dea196ed60ebd2acd32ff274eaebb1ad8b82d9e0156b1dec8e2e09b",
        "backend": "aed6e2410d52f3a89c924b56b3dfe4d9b607960a61eef973dfbf45546aa8f5d4"}
PLAN_FILE, CONFIG_FILE, CONTROL_DIR = "_xdev_native_plan.json", "_xdev_native_admission.json", "_xdev_native"
JOURNAL = "driver-evidence"
MAX_DEADLINE_S = 180
SAFE_INP = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\.inp\Z")  # 与 driver 的 input_name 规则相同
SAFE_DATA = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,120}\Z")
SAFE_ATTEMPT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\Z")  # 与 driver 的 attempt_id 规则相同（最长 101 字符）
SHA = re.compile(r"[0-9a-f]{64}\Z")
BINDING_KEYS = {"schema", "work_root", "python", "driver", "adapter", "backend", "engine", "admission", "model_budget"}
PYTHON_KEYS = {"path", "sha256", "version", "architecture", "psutil"}
# 在 Windows 上用绑定的 Python 跑一次，报解释器与 psutil 身份。-c 时 sys.path[0] 是 ''（当前目录），换成 driver 所在目录，
# 与 driver 以脚本启动时相同；开了 safe-path 时两种启动都不加这一项，就不动。
# 代码里不能有双引号（PowerShell 5.1 给原生程序传参会吃掉）；driver 路径作为 argv[1] 传入，不拼进代码。
PYTHON_PROBE = ("import sys,os;d=os.path.dirname(os.path.abspath(sys.argv[1]));"
                "sys.path[:1]=[d] if sys.path[:1]==[''] else sys.path[:1];import json,platform,psutil;"
                "print(json.dumps({'version':platform.python_version(),'architecture':platform.machine(),"
                "'psutil_version':psutil.__version__,'psutil_path':os.path.dirname(os.path.abspath(psutil.__file__))}))")
# driver 事件名（successor03 原文）；其他名字出现即视为不是这一版 driver 写的 journal
EVENTS = {"ORIGINAL_BATCH_INTENT", "MEASURED_BATCH_RESERVED", "ORIGINAL_FILES_STAGED", "SPAWN_INTENT",
          "IDENTITY_CAPTURED_SUSPENDED", "IDENTITY_BOUND_BEFORE_RESUME", "SPAWN_RETURNED", "SPAWN_UNKNOWN",
          "NATIVE_TERMINAL", "ADMISSION_TERMINAL_ACCEPTED", "ORIGINAL_HANDLES_CLOSED", "DRIVER_RESULT_PERSISTED"}
# 原进程身份：与 backend verify_terminal 用原句柄核的字段相同，另加引擎 SHA
IDENTITY_KEYS = ("pid", "birth_filetime", "birth_filetime_100ns", "birth_utc", "exe_path", "exe_sha256", "request_id", "job_id",
                 "attempt_id", "model_id", "route", "input_sha256", "host_id", "clock_domain")
# 成员结算后每个 attempt 恰好一条、且按此先后出现的事件（NATIVE_TERMINAL 可因安全结算重试出现多条，另核）
MEMBER_SEQUENCE = ("ORIGINAL_FILES_STAGED", "SPAWN_INTENT", "IDENTITY_CAPTURED_SUSPENDED", "IDENTITY_BOUND_BEFORE_RESUME",
                   "SPAWN_RETURNED")
MEMBER_AFTER = ("ADMISSION_TERMINAL_ACCEPTED", "ORIGINAL_HANDLES_CLOSED")
ADMISSION_KEYS = {"helper_module", "provider_directory", "registry", "original_controller", "policy_id", "policy",
                  "external_evidence", "overhead"}
BUDGET_KEYS = {"physical_bytes", "commit_bytes", "output_budget_bytes", "temp_budget_bytes"}
POOL_ONLY = {"cpu_budget": "--cpu-budget", "mem_reserve_gib": "--mem-reserve-gib", "commit_reserve_gib": "--commit-reserve-gib",
             "model_mem_gib": "--model-mem-gib", "disk_reserve_gib": "--disk-reserve-gib", "model_timeout_s": "--model-timeout",
             "admission_wait_s": "--admission-wait", "priority": "--priority"}
SCOPE = {"capacity_scope": "GENERAL_BATCH_ADMISSION", "admission_owner": "W1 native_batch_driver (long-lived Windows Python)",
         "native_acceptance": "not performed by xdev; processes_ok is not scientific validation"}


def _sha_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _winabs(value, label: str) -> str:
    """绑定里的 Windows 路径：本地绝对路径，不含 %VAR% 占位、..、UNC 或控制字符。"""
    if not isinstance(value, str) or not value or "%" in value or any(ord(c) < 32 for c in value):
        raise ValueError(f"binding {label} must be an absolute local Windows path without %VAR% placeholders")
    p = PureWindowsPath(value)
    if (not p.is_absolute() or p.anchor.startswith("\\\\") or ".." in p.parts or '"' in value
            or any(":" in x for x in p.parts[1:])):  # 与 adapter 的 winpath 相同：盘符之后不许再有冒号（流名）
        raise ValueError(f"binding {label} must be an absolute local Windows path, got {value!r}")
    return str(p)


def _positive(value, label: str) -> int:
    if type(value) is not int or not 1 <= value <= 2 ** 53:
        raise ValueError(f"binding {label} must be a positive whole number of bytes, got {value!r}")
    return value


def load_binding(path: str) -> dict:
    """读私有绑定并核结构；driver/adapter/backend 的 SHA 必须是 W1 审过的配对，C: 保留量不低于 20e9 B。
    其余策略、窗口和 provider 由 Windows 上的 adapter 原样核验，这里不放宽也不补值。"""
    p = Path(path).expanduser()
    if p.is_symlink() or not p.is_file():
        raise ValueError(f"--native binding is not a regular file: {path}")
    raw = p.read_bytes()
    try:
        b = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"--native binding is not JSON: {exc}") from None
    if not isinstance(b, dict) or set(b) != BINDING_KEYS or b.get("schema") != BINDING_SCHEMA:
        raise ValueError(f"--native binding needs exactly {sorted(BINDING_KEYS)} with schema {BINDING_SCHEMA}")
    out = {"work_root": _winabs(b["work_root"], "work_root").rstrip("\\")}
    py = b["python"]
    if not isinstance(py, dict) or set(py) != PYTHON_KEYS or not SHA.fullmatch(str(py.get("sha256"))):
        raise ValueError(f"binding python needs exactly {sorted(PYTHON_KEYS)} with a lowercase sha256")
    if (not isinstance(py["version"], str) or not re.fullmatch(r"\d+\.\d+\.\d+", py["version"])
            or not isinstance(py["architecture"], str) or not re.fullmatch(r"[A-Za-z0-9_]{1,32}", py["architecture"])):
        raise ValueError("binding python.version must look like 3.12.7 and python.architecture like AMD64")
    ps = py["psutil"]
    if (not isinstance(ps, dict) or set(ps) != {"version", "path"} or not isinstance(ps["version"], str)
            or not re.fullmatch(r"\d+(\.\d+){1,3}", ps["version"])):
        raise ValueError("binding python.psutil needs exactly {version, path}")
    # 只核 python.exe 存在不够：Windows 正文先核它的 SHA，再用它报的版本/架构与 psutil 版本/目录逐项比对
    out["python"] = _winabs(py["path"], "python.path")
    out["python_pin"] = {"sha256": py["sha256"], "version": py["version"], "architecture": py["architecture"],
                         "psutil_version": ps["version"], "psutil_path": _winabs(ps["path"], "python.psutil.path")}
    for role in PINS:
        d = b[role]
        if not isinstance(d, dict) or set(d) != {"path", "sha256"}:
            raise ValueError(f"binding {role} needs exactly {{path, sha256}}")
        if d["sha256"] != PINS[role]:
            raise ValueError(f"binding {role} sha256 {str(d['sha256'])[:12]} is not the W1-reviewed pair {PINS[role][:12]}")
        out[role] = {"path": _winabs(d["path"], role + ".path"), "sha256": d["sha256"]}
    e = b["engine"]
    if not isinstance(e, dict) or set(e) != {"exe_path", "exe_sha256"} or not SHA.fullmatch(str(e.get("exe_sha256"))):
        raise ValueError("binding engine needs exactly {exe_path, exe_sha256}")
    if PureWindowsPath(_winabs(e["exe_path"], "engine.exe_path")).name.lower() != "mplus.exe":
        raise ValueError("binding engine.exe_path must name Mplus.exe")
    out["engine"] = dict(e)
    a = b["admission"]
    if not isinstance(a, dict) or set(a) != ADMISSION_KEYS:
        raise ValueError(f"binding admission needs exactly {sorted(ADMISSION_KEYS)}")
    for k in ("helper_module", "provider_directory", "registry"):
        _winabs(a[k], "admission." + k)
    if not isinstance(a["original_controller"], str) or not a["original_controller"]:
        raise ValueError("binding admission.original_controller is required")
    floor = ((a.get("policy") or {}).get("volume_reserve_bytes") or {}).get("C:\\")
    if type(floor) is not int or floor < 20_000_000_000:
        raise ValueError("binding admission.policy.volume_reserve_bytes['C:\\\\'] must be >= 20,000,000,000 B")
    ev = a["external_evidence"]
    if not isinstance(ev, dict) or set(ev) != {"path", "sha256"} or not SHA.fullmatch(str(ev.get("sha256"))):
        raise ValueError("binding admission.external_evidence needs exactly {path, sha256}")
    _winabs(ev["path"], "admission.external_evidence.path")
    out["admission"] = json.loads(canonical(a))
    mb = b["model_budget"]
    if not isinstance(mb, dict) or set(mb) != BUDGET_KEYS:
        raise ValueError(f"binding model_budget needs exactly {sorted(BUDGET_KEYS)}")
    out["model_budget"] = {k: _positive(mb[k], "model_budget." + k) for k in sorted(BUDGET_KEYS)}
    return {"binding": out, "sha256": _sha_bytes(raw), "path": str(p.resolve())}


def deadline(value) -> float:
    if value is None:
        raise ValueError(f"--native needs --deadline S with 0 < S <= {MAX_DEADLINE_S}; no default and no silent cap")
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= MAX_DEADLINE_S:
        raise ValueError(f"--deadline must be a number with 0 < S <= {MAX_DEADLINE_S} seconds (driver limit), got {value!r}")
    return value


def attempt_id(batch_name: str, key: str) -> str:
    """原次 attempt_id = 批次名.模型名。adapter 的 general_batch_attempts 以 (controller, attempt_id) 为永久主键，
    释放后也不删，所以同一个模型名在下一批必须换 id。driver 限 101 字符；放不下就截短模型名并接它 SHA256 的前 8 位。"""
    a = f"{batch_name}.{key}"
    if len(a) > 101:
        a = f"{batch_name}.{key[:91 - len(batch_name)]}-{_sha_bytes(key.encode())[:8]}"
    if len(batch_name) > 80 or not SAFE_ATTEMPT.fullmatch(a):
        raise ValueError(f"cannot form a safe attempt id for model {key!r} in batch {batch_name}")
    return a


def native_plan(inps: list, inputs: list, bound: dict, deadline_s, jobs=None):
    """复用批次的本地检查（.inp、重名、写共用文件），再生成 driver plan 与 admission config。
    任一模型或文件名不符合 driver 规则就整批拒绝，不改科学输入里的文件引用。"""
    b = bound["binding"]
    seconds = deadline(deadline_s)
    lim = batch.limits(jobs=jobs)
    pl = batch.plan(inps, inputs, lim)
    models, shared = pl["models"], pl["shared"]
    if len(models) + len(shared) + 2 > 128:  # 两个控制文件也算 W1 task 输入
        raise ValueError(f"{len(models)} models + {len(shared)} shared inputs + 2 control files > 128 files per task")
    taken = {PLAN_FILE.casefold(), CONFIG_FILE.casefold()}
    for m in models:
        if not SAFE_INP.fullmatch(m["inp"]) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", m["key"]):
            raise ValueError(f"{m['inp']}: the native driver needs an ASCII name [A-Za-z0-9][A-Za-z0-9_.-]*.inp; rename the file")
        if m["inp"].casefold() in taken:
            raise ValueError(f"{m['inp']} collides with a control file name")
    for s in shared:
        clash = s["name"].casefold() in taken or any(s["name"].casefold() in (m["inp"].casefold(), m["out"].casefold()) for m in models)
        if not SAFE_DATA.fullmatch(s["name"]) or clash:
            raise ValueError(f"shared input {s['name']}: the native driver needs an ASCII name distinct from every .inp/.out and control file")
    jobs_k = lim["jobs"] or len(models)
    if jobs_k > len(models):
        raise ValueError(f"--jobs {jobs_k} > {len(models)} models (GeneralBatchAdmission needs K <= batch size)")
    mb, w = b["model_budget"], b["work_root"] + "\\" + pl["batch"]
    staged = sum(s["bytes"] for s in shared)
    rows = []
    for m in models:
        size, inp_sha = xdev_tasks._input_digest(Path(m["local_inp"]))
        if size + staged > mb["output_budget_bytes"]:
            raise ValueError(f"{m['inp']}: staged inputs {size + staged} B exceed model_budget.output_budget_bytes")
        k, a = m["key"], attempt_id(pl["batch"], m["key"])
        # 目录仍按模型名放在本批工作目录下（路径短，Mplus 不超 MAX_PATH）；取回只按 plan 里这一个 working_directory
        request = {"original_controller": b["admission"]["original_controller"], "attempt_id": a,
                   "request_id": f"{pl['batch']}.{k}", "job_id": f"{pl['batch']}/{k}", "model_id": k, "route": "DIRECT_MPLUS",
                   "working_directory": f"{w}\\models\\{k}\\cwd", "temp_directory": f"{w}\\models\\{k}\\tmp",
                   "input_sha256": inp_sha, "data_sha256": {s["name"]: s["sha256"] for s in shared},
                   "exe_path": b["engine"]["exe_path"], "exe_sha256": b["engine"]["exe_sha256"],
                   "physical_bytes": mb["physical_bytes"], "commit_bytes": mb["commit_bytes"], "processors": m["processors"],
                   "output_budget_bytes": mb["output_budget_bytes"], "temp_budget_bytes": mb["temp_budget_bytes"]}
        rows.append({"request": request, "input_source": f"{w}\\{m['inp']}", "input_name": m["inp"],
                     "data_sources": {s["name"]: f"{w}\\{s['name']}" for s in shared}, "deadline_seconds": seconds})
        m.update(attempt_id=a, input_sha256=inp_sha, working_directory=request["working_directory"])
    if len({m["attempt_id"].casefold() for m in models}) != len(models):  # journal 文件名按 attempt，Windows 不分大小写
        raise ValueError("two models map to the same attempt id; rename one")
    nplan = {"schema": PLAN_SCHEMA, "batch_id": pl["batch"], "journal_directory": f"{w}\\{JOURNAL}", "models": rows}
    adm = b["admission"]
    nconfig = {"schema": CONFIG_SCHEMA, **{k: adm[k] for k in sorted(ADMISSION_KEYS)},
               "batch_id": pl["batch"], "max_parallel": jobs_k}
    for k in ("external_host_admission_enforced_by_xdev", "host_reservations"):  # 批内池的声明不适用于本路线
        pl.pop(k, None)
    pl.update(schema=SCHEMA, work_dir=w, max_parallel=jobs_k, deadline_seconds=seconds, **SCOPE)
    return pl, nplan, nconfig

# W1 任务正文：cwd 是任务目录 work\<name>，上传的输入已由 W1 输入核验按 SHA 核过。
# 只同步调用 driver 一次；这里不起 Mplus、不读写预约。桥接回执 xdev_native_wrapper.json 只记身份关系与 driver 退出码。
NATIVE_PS = r"""# XDEV_MPLUS_NATIVE_BODY
$cfg = (__CFG__ | ConvertFrom-Json)
$Root = (Get-Location).ProviderPath
$meta = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'task.json') -Raw | ConvertFrom-Json
function Utc { [DateTime]::UtcNow.ToString('o') }
function Sha([string]$p) { (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLowerInvariant() }
function WriteJson([string]$path, $obj) {
  $tmp = $path + '.part'; $text = $obj | ConvertTo-Json -Compress -Depth 6
  for ($n = 1; $n -le 25; $n++) {
    try {
      [IO.File]::WriteAllText($tmp, $text, (New-Object Text.UTF8Encoding($false)))
      if (Test-Path -LiteralPath $path) { [IO.File]::Replace($tmp, $path, [NullString]::Value) } else { [IO.File]::Move($tmp, $path) }
      return
    } catch { Start-Sleep -Milliseconds 200 }
  }
  [Console]::Error.WriteLine('WRITE_FAILED ' + $path)
}
$Note = Join-Path $Root 'xdev_native_wrapper.json'
$w = [ordered]@{ schema = 'xdev-mplus-native-wrapper-v1'; owner = [string]$meta.owner; name = [string]$meta.name
  remote_dir = [string]$meta.remote_dir; batch_id = $cfg.batch_id; plan_sha256 = $cfg.plan_sha256
  config_sha256 = $cfg.config_sha256; journal = $cfg.journal; state = 'CHECKING'; at = (Utc) }
function Refuse([string]$why) { $w.state = 'REFUSED'; $w.reason = $why; $w.at = (Utc); WriteJson $Note $w; exit 2 }
if (-not [string]::Equals($Root, [string]$cfg.work_dir, [StringComparison]::OrdinalIgnoreCase)) { Refuse ('WORK_DIR_MISMATCH ' + $Root) }
if ([string]$meta.name -cne [string]$cfg.batch_id) { Refuse 'TASK_NAME_IS_NOT_BATCH_ID' }
foreach ($d in $cfg.pins) {
  if (-not (Test-Path -LiteralPath $d.path -PathType Leaf)) { Refuse ('MISSING ' + $d.role) }
  if ((Sha $d.path) -ne $d.sha256) { Refuse ('SHA_MISMATCH ' + $d.role) }
}
if (-not (Test-Path -LiteralPath $cfg.python -PathType Leaf)) { Refuse 'PYTHON_MISSING' }
if ((Sha $cfg.python) -ne $cfg.python_pin.sha256) { Refuse 'PYTHON_SHA_MISMATCH' }
# 同一解释器、同样 -B、sys.path[0] 同 driver 目录：报版本/架构与实际导入的 psutil；与绑定逐项不符就不启动 driver
$probe = @(& $cfg.python -B -c $cfg.probe $cfg.driver_path)
if ($LASTEXITCODE -ne 0) { Refuse ('PYTHON_PROBE_FAILED rc=' + $LASTEXITCODE) }
try { $pv = $probe[-1] | ConvertFrom-Json } catch { Refuse 'PYTHON_PROBE_UNREADABLE' }
$w.python_verified = [ordered]@{ version = [string]$pv.version; architecture = [string]$pv.architecture
  psutil_version = [string]$pv.psutil_version; psutil_path = [string]$pv.psutil_path }
if ($pv.version -cne $cfg.python_pin.version -or $pv.architecture -cne $cfg.python_pin.architecture -or
    $pv.psutil_version -cne $cfg.python_pin.psutil_version -or -not [string]::Equals([IO.Path]::GetFullPath([string]$pv.psutil_path),
      [IO.Path]::GetFullPath([string]$cfg.python_pin.psutil_path), [StringComparison]::OrdinalIgnoreCase)) {
  Refuse 'PYTHON_OR_PSUTIL_MISMATCH'
}
if (Test-Path -LiteralPath $cfg.journal) { Refuse 'JOURNAL_EXISTS_NO_REPLAY' }
$Models = Join-Path $Root 'models'
if (Test-Path -LiteralPath $Models) { Refuse 'MODELS_DIR_EXISTS_NO_REPLAY' }
$null = New-Item -ItemType Directory -Path $Models
$w.state = 'DRIVER_STARTING'; $w.at = (Utc); WriteJson $Note $w
$p = Start-Process -FilePath $cfg.python -ArgumentList $cfg.argline -WorkingDirectory $Root -NoNewWindow -PassThru `
  -RedirectStandardOutput (Join-Path $Root 'driver.stdout') -RedirectStandardError (Join-Path $Root 'driver.stderr')
$null = $p.Handle
$w.state = 'DRIVER_RUNNING'; $w.driver_pid = $p.Id; $w.driver_start_utc = $p.StartTime.ToUniversalTime().ToString('o'); $w.at = (Utc)
WriteJson $Note $w
$p.WaitForExit()
$code = $p.ExitCode
if ($null -eq $code) { $w.state = 'DRIVER_EXIT_UNKNOWN'; $w.at = (Utc); WriteJson $Note $w; exit 7 }
$w.state = 'DRIVER_EXITED'; $w.driver_exit_code = [int]$code; $w.at = (Utc); WriteJson $Note $w
exit ([int]$code)
"""


def native_command(pl: dict, bound: dict, plan_sha: str, config_sha: str) -> str:
    b, w = bound["binding"], pl["work_dir"]
    plan_path, config_path = f"{w}\\{PLAN_FILE}", f"{w}\\{CONFIG_FILE}"
    cfg = {"batch_id": pl["batch"], "work_dir": w, "python": b["python"], "python_pin": b["python_pin"],
           "probe": PYTHON_PROBE, "driver_path": b["driver"]["path"], "journal": f"{w}\\{JOURNAL}",
           "plan_sha256": plan_sha, "config_sha256": config_sha,
           "pins": [{"role": r, "path": b[r]["path"], "sha256": b[r]["sha256"]} for r in PINS]
                   + [{"role": "plan", "path": plan_path, "sha256": plan_sha},
                      {"role": "config", "path": config_path, "sha256": config_sha}],
           # driver 的四个参数，仅此一次调用
           "argline": (f'-B "{b["driver"]["path"]}" --plan "{plan_path}" --admission-config "{config_path}"'
                       f' --adapter-module "{b["adapter"]["path"]}" --backend-module "{b["backend"]["path"]}"')}
    return NATIVE_PS.replace("__CFG__", ps_quote(canonical(cfg)))

def _parse(text):
    """None = 文件不存在；'PARTIAL' = 有内容但不是完整 JSON（正在写或已截断），按未知处理。"""
    if text is None:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return "PARTIAL"


def poll_once(man: dict, full: bool = False) -> dict:
    """一个只读状态作业：先核 task.json 的 owner/名字/目录，再先读 runner 回执，后读桥接回执与 driver journal。
    full=True 时读出 RESULT 与各 attempt 的 terminal/result 全文（收尾用）；否则只看是否已出现。"""
    t = man["task"]
    attempts = [m["attempt_id"] for m in man["models"]]
    script = f"""# XDEV_MPLUS_NATIVE_POLL
$J = Join-Path $XRoot {ps_quote('jobs' + chr(92) + t['name'])}
function ReadShared([string]$f) {{
  if (-not (Test-Path -LiteralPath $f)) {{ return $null }}
  $fs = [IO.File]::Open($f, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
  try {{ (New-Object IO.StreamReader($fs, [Text.Encoding]::UTF8)).ReadToEnd() }} finally {{ $fs.Dispose() }}
}}
function Tail([string]$f) {{ $x = ReadShared $f; if ($null -ne $x -and $x.Length -gt 3000) {{ $x.Substring($x.Length - 3000) }} else {{ $x }} }}
$mt = ReadShared (Join-Path $J 'task.json')
if (-not $mt) {{ Emit @{{ ok = $false; reason = 'NO_TASK_META' }}; exit 3 }}
$meta = $mt | ConvertFrom-Json; $W = Expand {ps_quote(man['remote_dir'])}
if ($meta.owner -cne {ps_quote(t['owner'])} -or $meta.name -cne {ps_quote(t['name'])} -or $meta.kind -cne 'task' -or
    [IO.Path]::GetFullPath([string]$meta.remote_dir) -ine [IO.Path]::GetFullPath($W)) {{
  Emit @{{ ok = $false; reason = 'TASK_BINDING_MISMATCH' }}; exit 7
}}
$runner = ReadShared (Join-Path $J 'rc.txt')
$jd = Join-Path $W {ps_quote(JOURNAL)}
$full = {'$true' if full else '$false'}
$ev = ReadShared (Join-Path $jd 'events.jsonl')
$lines = if ($ev) {{ @($ev -split "`n" | Where-Object {{ $_.Trim() }}) }} else {{ @() }}
$members = @(foreach ($a in @({ps_quote(json.dumps(attempts))} | ConvertFrom-Json)) {{
  $tf = Join-Path $jd ($a + '.terminal.json'); $rf = Join-Path $jd ($a + '.result.json')
  if ($full) {{ [ordered]@{{ attempt_id = $a; terminal = (ReadShared $tf); result = (ReadShared $rf) }} }}
  else {{ [ordered]@{{ attempt_id = $a; settled = (Test-Path -LiteralPath $rf) }} }}
}})
Emit @{{ ok = $true; now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); runner = $runner
  wrapper = (ReadShared (Join-Path $W 'xdev_native_wrapper.json')); journal_exists = (Test-Path -LiteralPath $jd)
  result = $(if ($full) {{ ReadShared (Join-Path $jd 'RESULT.json') }} else {{ $null }})
  result_exists = (Test-Path -LiteralPath (Join-Path $jd 'RESULT.json')); events = $lines.Count
  last_event = $(if ($lines.Count) {{ $lines[-1] }} else {{ $null }}); members = $members
  driver_stderr = $(if ($full) {{ Tail (Join-Path $W 'driver.stderr') }} else {{ $null }}) }}
"""
    rc, out, err, i = run_ps(script, timeout=60, wait=240, quiet=True)
    rows = emitted(out)
    if rc != 0 or len(rows) != 1 or not rows[0].get("ok"):
        why = rows[0].get("reason") if len(rows) == 1 else None
        return {"ok": False, "fatal": why == "TASK_BINDING_MISMATCH", "reason": f"rc={rc} job={i} {why or (out + err)[-400:]}"}
    r = rows[0]
    wrapper = _parse(r.get("wrapper"))
    if isinstance(wrapper, dict):
        want = {"owner": t["owner"], "name": t["name"], "batch_id": man["batch"],
                "plan_sha256": man["plan_sha256"], "config_sha256": man["config_sha256"]}
        bad = {k: wrapper.get(k) for k, v in want.items() if wrapper.get(k) != v}
        if bad:
            return {"ok": False, "fatal": True, "reason": f"WRAPPER_BINDING_MISMATCH job={i} {bad}"}
    members = {x.get("attempt_id"): x for x in (r.get("members") or []) if isinstance(x, dict)}
    return {"ok": True, "job": i, "now": r.get("now"), "runner": r.get("runner"), "wrapper": wrapper,
            "journal_exists": r.get("journal_exists") is True, "result_exists": r.get("result_exists") is True,
            "result": _parse(r.get("result")) if full else None, "events": r.get("events"),
            "last_event": _parse(r.get("last_event")), "members": members, "driver_stderr": r.get("driver_stderr")}

def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _judge(man: dict, m: dict, member: dict, attempt_result, out_dir: Path) -> dict:
    """成员成功 = driver 记 EXITED、exit_code 0、无校验错误、processes_ok；attempt 结果文件与 RESULT 一致；
    取回完整；本机算出的 .out SHA 等于 driver 记录；.out 文本判定通过。任何一项缺都不成功。"""
    rec = man["results"].setdefault(m["key"], {})
    col = rec.get("collect") or {}
    out_file = out_dir / m["key"] / m["out"]
    got = _file_sha(out_file) if col.get("status") == "COMPLETE" and out_file.is_file() else None
    sha_ok = got is not None and got == member.get("output_sha256")
    consistent = attempt_result == member
    chk = (batch.check_out(out_file.read_text(encoding="utf-8", errors="replace")) if got
           else {"ok": False, "reason": "no .out came back" if col.get("status") == "COMPLETE"
                 else f"not collected ({col.get('status') or 'never'})"})
    ok = (member.get("processes_ok") is True and member.get("exit_code") == 0 and member.get("terminal") == "EXITED"
          and not member.get("validation_errors") and consistent and col.get("status") == "COMPLETE" and sha_ok and chk["ok"])
    rec.update(ok=ok, attempt_id=m["attempt_id"], pid=member.get("pid"), exit_code=member.get("exit_code"),
               terminal=member.get("terminal"), validation_errors=member.get("validation_errors"),
               processes_ok=member.get("processes_ok"), driver_output_sha256=member.get("output_sha256"),
               collected_output_sha256=got, output_sha_ok=sha_ok, attempt_result_consistent=consistent,
               scientific_validation=member.get("scientific_validation"), check=chk,
               out=str(out_file) if out_file.is_file() else None)
    # 证据不一致（SHA 或 attempt 文件与 RESULT 不符）不能当普通失败，也不重取：留给原次核实
    rec["evidence_mismatch"] = bool(col.get("status") == "COMPLETE" and (not sha_ok or not consistent))
    return rec


def timing(terminals: dict) -> dict:
    """进程区间取 driver terminal 里的内核出生/退出 FILETIME（100 ns）；首尾相接不算重叠。"""
    spans = [(t["birth_filetime_100ns"], t["native_exit_filetime_100ns"]) for t in terminals.values()
             if isinstance(t, dict) and type(t.get("birth_filetime_100ns")) is int and type(t.get("native_exit_filetime_100ns")) is int]
    if not spans:
        return {}
    events = sorted([(a, 1) for a, _ in spans] + [(b, -1) for _, b in spans])
    cur = peak = 0
    for _, d in events:
        cur += d
        peak = max(peak, cur)
    return {"kernel_span_s": round((max(b for _, b in spans) - min(a for a, _ in spans)) / 1e7, 3),
            "kernel_sum_s": round(sum(b - a for a, b in spans) / 1e7, 3), "kernel_interval_peak": peak}


def _collect_journal(man: dict, out_dir: Path) -> dict:
    """driver journal 按 plan 的 journal_directory 用原 pull_dir 协议取回一次；结果未知不重取。"""
    rec = man.setdefault("journal_collect", {})
    if rec.get("status") == "STARTED":
        rec.update(status="UNKNOWN", error="interrupted during the original pull_dir; not repeated")
    if rec:
        return rec
    local = out_dir / "_native_journal"
    rec.update(status="STARTED", local=str(local))
    batch._save(man, out_dir)
    why = None
    try:
        rc = batch.pull_dir(man["work_dir"] + "\\" + JOURNAL, str(local))
    except (OSError, ValueError, RuntimeError) as exc:
        rc, why = 7, str(exc)
    claim = local.with_name("." + local.name + ".xdev-claim.json")
    rec.update(status="UNKNOWN" if rc == 124 or why or claim.exists() else ("COMPLETE" if rc == 0 else "FAILED"), rc=rc, error=why)
    batch._save(man, out_dir)
    return rec


def _record(path: Path, errors: list, label: str):
    """driver 用 write_new 写的记录：规范 JSON 加一个换行。读不出或字节不是这个样子都记错误，返回 None。"""
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
        if raw == (canonical(value) + "\n").encode():
            return value
        errors.append(f"{label}: bytes are not the driver's canonical record")
    except (OSError, ValueError) as exc:
        errors.append(f"{label}: unreadable ({type(exc).__name__})")
    return None


def _events(path: Path, errors: list) -> list:
    """events.jsonl：每行一个规范 JSON 对象，sequence 从 1 连续递增，事件名属于 successor03 driver。"""
    try:
        lines = path.read_bytes().split(b"\n")
    except OSError:
        errors.append("events.jsonl: missing")
        return []
    if lines[-1] != b"":
        errors.append("events.jsonl: last line not terminated")
    out = []
    for i, raw in enumerate(lines[:-1], 1):
        try:
            e = json.loads(raw)
            if not isinstance(e, dict) or raw != canonical(e).encode():
                raise ValueError("not a canonical object")
        except ValueError:
            errors.append(f"events.jsonl line {i}: not a driver record")
            return []
        if e.get("sequence") != i or not isinstance(e.get("event"), str) or e["event"] not in EVENTS:
            errors.append(f"events.jsonl line {i}: sequence {e.get('sequence')!r} event {e.get('event')!r}")
            return []
        out.append(e)
    return out


def _event_attempt(e: dict):
    """事件属于哪个 attempt：顶层与 request/identity/terminal 里的 attempt_id 必须一致；不一致返回一个不属于任何 plan 的标记。"""
    vals = [x.get("attempt_id") for x in (e.get(k) for k in ("request", "identity", "terminal")) if isinstance(x, dict)]
    ids = {a if isinstance(a, str) else repr(a) for a in vals + [e.get("attempt_id")] if a is not None}
    return ids.pop() if len(ids) == 1 else ("MIXED:" + ",".join(sorted(ids)) if ids else None)


def verify_journal(man: dict, out_dir: Path, r: dict) -> dict:
    """取回的 driver journal 是否就是本批这一次原 run：冻结 plan/config 未变、PLAN.json 等于冻结 plan、
    RESULT 与轮询原文相同且成员集与 plan 完全对上、每个成员的 STAGED/SPAWN/IDENTITY/TERMINAL 事件与完整
    terminal、result 文件同属 plan 里的原 attempt、桥接回执与原任务 rc 与 RESULT 相符。只读本机文件。
    返回 ok、errors 与核过的 members/terminals；任何一项不符都不算成功，由收尾按证据未知处理。"""
    errors = []
    ctl, jd = out_dir / CONTROL_DIR, out_dir / "_native_journal"
    frozen = {}
    for label, name, want in (("plan", PLAN_FILE, man["plan_sha256"]), ("config", CONFIG_FILE, man["config_sha256"])):
        try:
            raw = (ctl / name).read_bytes()
        except OSError:
            raw = b""
        if _sha_bytes(raw) != want:
            return {"ok": False, "errors": [f"local frozen {label} file missing or changed since submit"]}
        frozen[label] = json.loads(raw)
    plan, config = frozen["plan"], frozen["config"]
    requests = {row["request"]["attempt_id"]: row["request"] for row in plan["models"]}
    attempts = [m["attempt_id"] for m in man["models"]]
    if list(requests) != attempts or plan.get("batch_id") != man["batch"]:
        return {"ok": False, "errors": ["manifest models differ from the frozen plan"]}
    allowed = {"PLAN.json", "events.jsonl", "RESULT.json"} | {f"{a}.{x}.json" for a in attempts for x in ("terminal", "result")}
    present = {p.name: p for p in jd.iterdir()} if jd.is_dir() else {}
    odd = sorted(k for k, p in present.items() if k not in allowed or p.is_symlink() or not p.is_file())
    if odd:
        errors.append(f"journal has files this batch's driver does not write: {odd[:5]}")
    pj = _record(jd / "PLAN.json", errors, "PLAN.json")
    if pj is not None and pj != {"plan": plan, "evidence_class": "NATIVE_COLLECTION"}:
        errors.append("PLAN.json is not this batch's frozen plan")
    res = _record(jd / "RESULT.json", errors, "RESULT.json")
    if res is not None and not isinstance(res, dict):
        errors.append("RESULT.json is not an object")
        res = None
    members = {}
    if res is not None:
        if res != r.get("result"):
            errors.append("collected RESULT.json differs from the polled original")
        ms, un = res.get("members"), res.get("unresolved_attempt_ids")
        if (res.get("schema") != RESULT_SCHEMA or res.get("batch_id") != man["batch"] or res.get("evidence_class") != "NATIVE_COLLECTION"
                or not isinstance(ms, list) or not all(isinstance(x, dict) for x in ms) or not isinstance(un, list)):
            errors.append("RESULT.json is not a native result of this batch")
        else:
            ids = [x.get("attempt_id") for x in ms]
            if (not all(isinstance(a, str) for a in ids + un) or len(set(ids)) != len(ids) or len(set(un)) != len(un)
                    or set(ids) & set(un) or set(ids) | set(un) != set(attempts)):
                errors.append(f"RESULT members {ids} + unresolved {un} are not exactly the plan's attempts once each")
            else:
                members = dict(zip(ids, ms))
    events = _events(jd / "events.jsonl", errors)
    if not events:
        errors.append("events.jsonl: no driver events")
    else:
        if len(events) != r.get("events"):
            errors.append(f"collected {len(events)} events, polled original had {r.get('events')}")
        first, last = events[0], events[-1]
        if (first["event"] != "ORIGINAL_BATCH_INTENT"
                or first.get("configuration_sha256") != _sha_bytes(canonical(config).encode())):
            errors.append("first event is not this batch's ORIGINAL_BATCH_INTENT")
        if (last["event"] != "DRIVER_RESULT_PERSISTED" or last.get("summary") != res
                or sum(e["event"] == "DRIVER_RESULT_PERSISTED" for e in events) != 1):
            errors.append("events do not end with exactly one DRIVER_RESULT_PERSISTED equal to RESULT")
        foreign = sorted({str(a) for a in map(_event_attempt, events) if a is not None and a not in requests})
        if foreign:
            errors.append(f"events name attempts outside this batch: {foreign[:5]}")
    by = {a: [e for e in events if _event_attempt(e) == a] for a in attempts}
    snap = r.get("members") or {}
    terminals, results = {}, {}
    for a in attempts:  # 轮询原文与取回文件逐件相同（没有的两边都没有）
        for x in ("terminal", "result"):
            f = jd / f"{a}.{x}.json"
            local = _record(f, errors, f.name) if f.name in present else None
            text = (snap.get(a) or {}).get(x)
            try:
                polled = None if text is None else json.loads(text)
            except (TypeError, ValueError):
                polled = "PARTIAL"
            if local != polled:
                errors.append(f"{f.name}: collected file differs from the polled original")
            (terminals if x == "terminal" else results)[a] = local
    for a, mem in members.items():
        req, ev, term = requests[a], by[a], terminals.get(a)
        named = {k: [e for e in ev if e["event"] == k] for k in EVENTS}
        once = {k: named[k][0] for k in MEMBER_SEQUENCE + MEMBER_AFTER if len(named[k]) == 1}
        missing = [k for k in MEMBER_SEQUENCE + MEMBER_AFTER if k not in once]
        if missing or named["SPAWN_UNKNOWN"] or not named["NATIVE_TERMINAL"]:
            errors.append(f"{a}: events not exactly once each {missing} / SPAWN_UNKNOWN {len(named['SPAWN_UNKNOWN'])}"
                          f" / NATIVE_TERMINAL {len(named['NATIVE_TERMINAL'])}")
            continue
        seq = [once[k]["sequence"] for k in MEMBER_SEQUENCE] + [min(e["sequence"] for e in named["NATIVE_TERMINAL"])] + \
              [once[k]["sequence"] for k in MEMBER_AFTER]
        if seq != sorted(seq) or len(set(seq)) != len(seq):
            errors.append(f"{a}: settlement events out of order")
        staged = once["ORIGINAL_FILES_STAGED"]
        if staged.get("input_sha256") != req["input_sha256"] or staged.get("data_sha256") != req["data_sha256"]:
            errors.append(f"{a}: staged input/data SHA differ from the plan")
        if once["SPAWN_INTENT"].get("request") != req:
            errors.append(f"{a}: SPAWN_INTENT request is not the plan's request")
        if not isinstance(term, dict) or any(e.get("terminal") != term for e in named["NATIVE_TERMINAL"]):
            errors.append(f"{a}: terminal file missing or differs from its NATIVE_TERMINAL events")
            continue
        ids = [once["IDENTITY_CAPTURED_SUSPENDED"].get("identity"), once["SPAWN_RETURNED"].get("identity")]
        if not all(isinstance(i, dict) and all(k in i and i[k] == term.get(k) for k in IDENTITY_KEYS) for i in ids):
            errors.append(f"{a}: IDENTITY_CAPTURED/SPAWN_RETURNED identity differs from the terminal record")
        if (any(term.get(k) != req[k] for k in ("attempt_id", "request_id", "job_id", "model_id", "route", "input_sha256", "exe_sha256"))
                or PureWindowsPath(str(term.get("exe_path"))) != PureWindowsPath(req["exe_path"])):
            errors.append(f"{a}: terminal identity is not the plan's request/engine")
        pid, birth, end = term.get("pid"), term.get("birth_filetime_100ns"), term.get("native_exit_filetime_100ns")
        if (type(pid) is not int or pid <= 0 or type(birth) is not int or birth <= 0 or term.get("birth_filetime") != birth
                or type(end) is not int or end < birth or term.get("terminal") not in ("EXITED", "TERMINATED")
                or type(term.get("exit_code")) is not int or term.get("descendants_complete") is not True):
            errors.append(f"{a}: terminal record incomplete (pid/birth/exit/descendants)")
        if (results.get(a) != mem or mem.get("pid") != pid or mem.get("exit_code") != term.get("exit_code")
                or mem.get("terminal") != term.get("terminal") or mem.get("descendants_complete") is not True):
            errors.append(f"{a}: member result does not match its terminal/result files")
    want_rc = None
    if isinstance(res, dict):
        want_rc = 0 if res.get("processes_ok") is True else (7 if res.get("unresolved_attempt_ids") else 1)
    w, fin = r.get("wrapper"), man["task"].get("final") or {}
    if not isinstance(w, dict) or w.get("state") != "DRIVER_EXITED" or w.get("driver_exit_code") != want_rc or fin.get("rc") != want_rc:
        errors.append(f"bridge/task exit code does not match RESULT (wrapper {w.get('driver_exit_code') if isinstance(w, dict) else None},"
                      f" task {fin.get('rc')}, expected {want_rc})")
    pin, pv = man.get("python_pin") or {}, (w or {}).get("python_verified") if isinstance(w, dict) else None
    if (not isinstance(pv, dict) or any(pv.get(k) != pin.get(k) for k in ("version", "architecture", "psutil_version"))
            or PureWindowsPath(str(pv.get("psutil_path"))) != PureWindowsPath(str(pin.get("psutil_path")))):
        errors.append("bridge receipt lacks the bound Python/psutil identity")
    return {"ok": not errors, "errors": errors, "members": members,
            "terminals": {a: t for a, t in terminals.items() if a in members}}


def conclude(man: dict, out_dir: Path, r: dict, t0: float) -> int:
    """原任务已核终态后的唯一收尾：先分清 driver 有没有开过 journal；有 RESULT 就先把整个 journal 取回一次并核验，
    核过了才按 plan 的 working_directory 逐成员取回 .out 与判定。"""
    keys = [m["key"] for m in man["models"]]
    fin = man["task"].get("final") or {}
    wrapper, result = r.get("wrapper"), r.get("result")
    man.update(wrapper=wrapper, driver_stderr_tail=r.get("driver_stderr"), events=r.get("events"))
    unknown, stage = [], None
    if not r.get("journal_exists"):
        # driver 在建 journal 前就停了（正文拒绝、依赖或 plan 不符）：没有预约、没有起进程
        stage = "NOT_STARTED"
        why = (wrapper or {}).get("reason") if isinstance(wrapper, dict) else None
        for k in keys:
            man["results"].setdefault(k, {}).update(ok=False, state="NOT_STARTED", reason=why or "driver journal never created")
    elif not isinstance(result, dict) or result.get("schema") != RESULT_SCHEMA or result.get("batch_id") != man["batch"]:
        # journal 在但 RESULT 缺失、半截或不属于本批：原次未知，不取回、不重调 driver
        stage = "RESULT_UNKNOWN"
        unknown = keys
        for k in keys:
            man["results"].setdefault(k, {}).update(ok=False, state="UNKNOWN", reason=f"RESULT {'missing' if result is None else 'unreadable or foreign'}")
    else:
        stage = "RESULT"
        man["native_result"] = {k: result.get(k) for k in ("admission_state", "processes_ok", "scientific_validation",
                                                           "errors", "unresolved_attempt_ids", "evidence_class")}
        # journal 取回一次：只有 COMPLETE 才核；FAILED/UNKNOWN/中断都不成功，也不重取
        jc = _collect_journal(man, out_dir)
        v = verify_journal(man, out_dir, r) if jc.get("status") == "COMPLETE" else {
            "ok": False, "errors": [f"driver journal collection {jc.get('status')} (rc={jc.get('rc')}); not repeated"]}
        man["journal_verification"] = {"ok": v["ok"], "errors": v["errors"][:50]}
        if not v["ok"]:
            # 证据链没闭合：不取模型目录、不判成功；原 RESULT/轮询原文与认领原样留在清单里
            unknown = list(keys)
            for k in keys:
                man["results"].setdefault(k, {}).update(ok=False, state="EVIDENCE_UNVERIFIED", reason=v["errors"][0])
            print("XDEV_MPLUS_EVIDENCE_UNVERIFIED " + "; ".join(v["errors"][:5]), file=sys.stderr)
        else:
            unresolved = set(result.get("unresolved_attempt_ids") or [])
            for m in man["models"]:
                a, rec = m["attempt_id"], man["results"].setdefault(m["key"], {})
                member = v["members"].get(a)
                if member is None:
                    rec.update(ok=False, state="UNRESOLVED" if a in unresolved else "NOT_IN_RESULT")
                    unknown.append(m["key"])
                    continue
                batch._collect_one(man, m["key"], out_dir, remote=m["working_directory"])
                rec = _judge(man, m, member, member, out_dir)
                rec["state"] = "SETTLED"
                if (rec.get("collect") or {}).get("status") == "UNKNOWN" or rec["evidence_mismatch"]:
                    unknown.append(m["key"])
                batch._save(man, out_dir)
                print(f"XDEV_MPLUS_DONE {m['key']} ok={rec['ok']} exit_code={rec['exit_code']} terminal={rec['terminal']}"
                      f" sha_ok={rec['output_sha_ok']} collect={(rec.get('collect') or {}).get('status')} out={rec['out']}")
            man["timing"] = timing(v["terminals"])
    man["stage"] = stage
    man["mac_follow_s"] = round(time.monotonic() - t0, 1)
    nr = man.get("native_result") or {}
    man["ok"] = (stage == "RESULT" and fin.get("state") == "DONE" and fin.get("rc") == 0 and nr.get("processes_ok") is True
                 and nr.get("admission_state") == "RELEASED" and not nr.get("unresolved_attempt_ids") and not unknown
                 and (man.get("journal_collect") or {}).get("status") == "COMPLETE"
                 and (man.get("journal_verification") or {}).get("ok") is True
                 and all(man["results"].get(k, {}).get("ok") is True for k in keys))
    batch._save(man, out_dir)
    summary = {"ok": man["ok"], "batch": man["batch"], "stage": stage, "task_rc": fin.get("rc"), "models": len(keys),
               "failed": [k for k in keys if man["results"].get(k, {}).get("ok") is not True], "unknown": sorted(set(unknown)),
               "admission_state": nr.get("admission_state"), "processes_ok": nr.get("processes_ok"),
               "capacity_scope": SCOPE["capacity_scope"], "scientific_validation": "NOT_PERFORMED_BY_XDEV",
               **(man.get("timing") or {}), "manifest": str(out_dir / batch.MANIFEST)}
    man["exit_code"] = 0 if man["ok"] else (7 if unknown or stage == "RESULT_UNKNOWN" else 1)
    batch._save(man, out_dir)
    print("XDEV_MPLUS_NATIVE " + json.dumps(summary, ensure_ascii=False))
    return man["exit_code"]

def follow(man: dict, out_dir: Path, max_seconds: int) -> int:
    """轮询到 runner 回执出现，并经 W1 task_status 核到原任务 DONE；然后读 driver 证据统一收取（简单首版不早取）。"""
    t0, gap, last = time.monotonic(), 3, None
    end = t0 + max_seconds
    name = man["task"]["name"]
    if not man["task"].get("owner"):
        return fail(f"XDEV_MPLUS_BINDING_FAILED {name}: no verified owner in the local task journal; nothing polled", 7)
    while "final" not in man["task"]:
        r = poll_once(man)
        if not r["ok"]:
            if r.get("fatal"):
                man["task"]["poll_error"] = r["reason"]
                batch._save(man, out_dir)
                return fail(f"XDEV_MPLUS_BINDING_FAILED {r['reason']}; nothing collected or rerun", 7)
            print(f"XDEV_MPLUS_POLL_RETRY {r['reason']}", file=sys.stderr)
        else:
            w = r["wrapper"] if isinstance(r["wrapper"], dict) else {}
            settled = sum(1 for x in r["members"].values() if x.get("settled"))
            ev = r["last_event"].get("event") if isinstance(r["last_event"], dict) else None
            note = (w.get("state"), r["events"], settled, ev)
            if note != last:
                print(f"  {time.strftime('%H:%M:%S')} {man['batch']} wrapper={note[0]} events={note[1]}"
                      f" settled={settled}/{len(man['models'])} last={ev}", file=sys.stderr)
                last = note
            if r["runner"] is not None:
                ts = xdev_tasks.task_status(name, nbytes=2000)
                man["task"]["final"] = {k: ts.get(k) for k in ("ok", "state", "rc", "finished", "job_id", "binding_verified",
                                                               "reason", "err", "runner_error")}
                batch._save(man, out_dir)
                if not ts.get("ok") or ts.get("state") != "DONE":
                    return fail(f"XDEV_MPLUS_TASK_UNCONFIRMED {name} state={ts.get('state')} {ts.get('reason')}; nothing rerun", 7)
                break
        if time.monotonic() + gap > end:
            batch._save(man, out_dir)
            print(f"XDEV_MPLUS_STILL_RUNNING {man['batch']} (later: xdev mplus --resume {out_dir})")
            return 124
        time.sleep(gap)
        gap = min(gap * 2, 30)
    if man["task"]["final"].get("state") != "DONE":
        return fail(f"XDEV_MPLUS_TASK_UNCONFIRMED {name} state={man['task']['final'].get('state')}; nothing rerun", 7)
    for _ in range(3):
        r = poll_once(man, full=True)
        if r["ok"] or r.get("fatal"):
            break
        time.sleep(5)
    if not r["ok"]:
        batch._save(man, out_dir)
        return fail(f"XDEV_MPLUS_EVIDENCE_UNREAD {r['reason']}; nothing collected or rerun (retry with --resume {out_dir})", 7)
    return conclude(man, out_dir, r, t0)


def run(inps: list, inputs: list, out: str, max_seconds: int, binding: str, deadline_s=None, jobs=None, **pool_options) -> int:
    """pool_options 是 BATCH_ONLY 池的参数；本路线没有对应语义，显式给了就整批拒绝而不是静默忽略。"""
    out_dir = Path(out).expanduser()
    try:
        used = sorted(POOL_ONLY[k] for k, v in pool_options.items() if v is not None)
        if used:
            raise ValueError(f"{', '.join(used)} not supported on the --native route (no silent mapping)")
        if type(max_seconds) is not int or max_seconds < 0:
            raise ValueError(f"--max must be a whole number of seconds >= 0, got {max_seconds!r}")
        bound = load_binding(binding)
        pl, nplan, nconfig = native_plan(inps, inputs, bound, deadline_s, jobs)
    except ValueError as exc:
        return fail(f"XDEV_MPLUS_REFUSED {exc}", 2)
    if (out_dir / batch.MANIFEST).exists():
        return fail(f"XDEV_MPLUS_REFUSED {out_dir / batch.MANIFEST} exists; continue it with --resume {out_dir} or pick a new --out", 3)
    busy = [m["key"] for m in pl["models"]
            if (out_dir / m["key"]).exists() or (out_dir / f".{m['key']}.xdev-claim.json").exists()]
    if busy or (out_dir / CONTROL_DIR).exists() or (out_dir / "_native_journal").exists():
        return fail(f"XDEV_MPLUS_REFUSED local model/control dirs or claims already exist under {out_dir}: {busy}", 3)
    ctl = out_dir / CONTROL_DIR
    ctl.mkdir(parents=True)
    raw_plan, raw_config = (canonical(nplan) + "\n").encode(), (canonical(nconfig) + "\n").encode()
    (ctl / PLAN_FILE).write_bytes(raw_plan)
    (ctl / CONFIG_FILE).write_bytes(raw_config)
    plan_sha, config_sha = _sha_bytes(raw_plan), _sha_bytes(raw_config)
    command = native_command(pl, bound, plan_sha, config_sha)
    if len(command.encode()) > 60 * 1024:
        return fail(f"XDEV_MPLUS_REFUSED native body is {len(command.encode())} bytes; split the batch", 2)
    name = pl["batch"]
    man = {**pl, "binding": {"path": bound["path"], "sha256": bound["sha256"]}, "plan_sha256": plan_sha,
           "config_sha256": config_sha, "pins": dict(PINS), "python_pin": bound["binding"]["python_pin"],
           "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "task": {"name": name, "remote_dir": pl["remote_dir"], "state": "SUBMITTING"}, "results": {}}
    batch._save(man, out_dir)
    files = [m["local_inp"] for m in pl["models"]] + [s["local"] for s in pl["shared"]] + [str(ctl / PLAN_FILE), str(ctl / CONFIG_FILE)]
    sub = xdev_tasks.task_submit(files, command, name)
    batch._record_task(man, sub)
    batch._save(man, out_dir)
    if not sub.get("ok"):
        if sub.get("state") == "REFUSED":
            return fail(f"XDEV_MPLUS_REFUSED {json.dumps(sub, ensure_ascii=False)}", sub.get("rc") or 2)
        return fail(f"XDEV_MPLUS_START_UNKNOWN {name} stage={sub.get('stage')} state={sub.get('state')} {sub.get('reason')};"
                    f" observe with: xdev mplus --resume {out_dir} (never resubmits)", 7)
    print(f"XDEV_MPLUS_NATIVE_SUBMITTED {name} models={len(pl['models'])} K={pl['max_parallel']}"
          f" deadline_s={pl['deadline_seconds']} out={out_dir}", file=sys.stderr)
    return follow(man, out_dir, max_seconds)


def resume(man: dict, out_dir: Path, max_seconds: int) -> int:
    """接回原 native 批次：只沿原 W1 任务观察；RESERVED 才按 task_resume 续传未尝试的输入并启动一次。
    driver 只在原任务正文里跑过一次，这里从不重新调用，也不把旧进程句柄交给新 owner。"""
    name = man["task"]["name"]
    owner = batch._journal_owner(name)
    if owner is None or man["task"].get("owner") not in (None, owner):
        return fail(f"XDEV_MPLUS_BINDING_FAILED {name}: local task journal missing or changed; nothing resumed", 7)
    man["task"]["owner"] = owner
    if "final" not in man["task"]:
        ts = xdev_tasks.task_status(name)
        man["task"]["resume_status"] = {k: ts.get(k) for k in ("ok", "state", "rc", "reason", "upload_status", "job_id")}
        if ts.get("ok") and ts.get("state") == "RESERVED":
            batch._record_task(man, xdev_tasks.task_resume(name))
            batch._save(man, out_dir)
            if not man["task"].get("ok"):
                return fail(f"XDEV_MPLUS_START_UNKNOWN {name} {man['task'].get('state')} {man['task'].get('reason')}; nothing replayed", 7)
        elif not ts.get("ok"):
            batch._save(man, out_dir)
            return fail(f"XDEV_MPLUS_TASK_UNKNOWN {name} {ts.get('state')} {ts.get('reason')}; original requests kept, nothing replayed",
                        ts.get("rc") or 7)
    batch._save(man, out_dir)
    if man.get("stage"):  # 已经收尾过：只重报原结论，不再取回
        print("XDEV_MPLUS_NATIVE " + json.dumps({"ok": man.get("ok"), "batch": man["batch"], "stage": man["stage"],
                                                 "manifest": str(out_dir / batch.MANIFEST)}, ensure_ascii=False))
        return man.get("exit_code", 7)
    return follow(man, out_dir, max_seconds)
