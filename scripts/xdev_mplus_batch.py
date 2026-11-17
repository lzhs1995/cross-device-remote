"""xdev mplus a.inp b.inp ...：多个 Mplus 模型在 Windows 上同时跑。

一个批次就是一个 W1 具名任务（xdev_tasks.task_submit）：本机先冻结意图（每个 .inp 与共用数据的 SHA、
池正文 BODY.ps1、原样 RUNNER.ps1），再原子预约名字、逐个上传（各有 transfer 回执）、只启动一次。
池正文在 Windows 任务目录里起本地进程池；之后每轮一个只读状态作业看全部模型，
跑完一个就用 pull_dir 取回一个（远端 zip 只在本机全部核完后才删；未知结果不删、不重取）。

准入只管本批次（capacity_scope = BATCH_ONLY）：同时运行数 ≤ --jobs；PROCESSORS 之和 ≤ --cpu-budget，
首个模型也不例外，单个模型超预算就不起；Windows 可用物理内存和可用提交量都减去在跑模型尚未用到的预约额，
仍要不低于保留量加新模型预约额；输出卷剩余不低于 --disk-reserve-gib。
它不读写共享 HostReservations，看不见别的队列和 R/Stata 任务的预约。主机级严格准入是 W1 的
GeneralBatchAdmission（长寿命 Python 原 owner 持有进程与 Job 句柄），尚未接到本池。接线要把池的起进程核心
（BatchAdmit 判定、Start-Process、终态）换成该 owner 内的 driver；只把 BatchAdmit 换成一次短命 acquire 不够。
静态的 BATCH_ONLY 声明不等于严格准入已完成。超时只结束池自己起的直接子进程（没有 Job 对象）。

Mplus 子进程的优先级（--priority，默认 Normal）和关闭 EcoQoS 只作用于本批次池起的 Mplus，冻结在 BODY.ps1 里；
RUNNER 和其他 Stata/R/detach 任务不变。
"""
import fnmatch
import json
import math
import re
import sys
import time
import uuid
from pathlib import Path, PureWindowsPath

import xdev_tasks
from xdev_core import emitted, fail, ps_quote, run_ps
from xdev_files import pull_dir

MANIFEST = "_xdev_mplus_batch.json"
SCHEMA = "xdev-mplus-batch-v2"
PRIORITIES = ("Normal", "AboveNormal", "BelowNormal", "inherit")
MAX_MODELS = 100
SCOPE = {"capacity_scope": "BATCH_ONLY", "external_host_admission_enforced_by_xdev": False,
         "host_reservations": "not used; strict host admission (W1 GeneralBatchAdmission, a long-lived owner of the "
                              "child/Job handles) is not connected here; wiring it moves the pool's launch core (BatchAdmit, "
                              "Start-Process, terminal state) into that owner, not a short-lived acquire"}
RAN = ("DONE", "TIMEOUT")                             # 起过进程，有模型目录可取回
NOT_RUN = ("REFUSED", "NOT_ADMITTED", "START_FAILED")  # 没起进程
SUMMARY_ERROR = re.compile(r"\b[1-9]\d*\s+ERROR\(S\)\s+FOUND\s+IN\s+THE\s+INPUT\s+INSTRUCTIONS\b", re.I)


def check_out(text: str) -> dict:
    """判断一个 .out 是否正常结束。Monte Carlo 输出没有 TERMINATED NORMALLY，改看完成的重复次数。
    Mplus 9 有时只打印汇总行「1 ERROR(S) FOUND IN THE INPUT INSTRUCTIONS」，也算失败。"""
    errors = [ln.strip() for ln in text.splitlines() if "*** ERROR" in ln or "*** FATAL ERROR" in ln
              or SUMMARY_ERROR.search(ln) or "DID NOT TERMINATE NORMALLY" in ln]
    normal = "THE MODEL ESTIMATION TERMINATED NORMALLY" in text
    m = re.search(r"Number of replications\s+Requested\s+(\d+)\s+Completed\s+(\d+)", text)
    mc = {"requested": int(m.group(1)), "completed": int(m.group(2))} if m else None
    ok = not errors and (normal or bool(mc and mc["completed"] == mc["requested"]))
    return {"ok": ok, "terminated_normally": normal, "monte_carlo": mc, "mplus_errors": errors[:10]}


def _strip_comments(text: str) -> str:
    # Mplus 注释以 ! 开头到行尾
    return "\n".join(ln.split("!", 1)[0] for ln in text.splitlines())


def inp_processors(text: str) -> int:
    """PROCESSORS = 4; / PROCESSORS ARE 4 2; 取第一个数，缺省 1。"""
    m = re.search(r"\bPROC[A-Z]*\s*(?:=|\bARE\b|\bIS\b)\s*(\d+)", _strip_comments(text), flags=re.I)
    return max(1, int(m.group(1))) if m else 1


def _sections(text: str):
    """按行首的 Mplus 命令名（`SAVEDATA:`、`DATA IMPUTATION:` 这类）切段。"""
    text = _strip_comments(text)
    marks = list(re.finditer(r"^[ \t]*([A-Za-z]+(?:[ \t]+[A-Za-z]+)?)[ \t]*:", text, flags=re.M))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        yield re.sub(r"\s+", " ", m.group(1)).upper(), text[m.end():end]


def written_names(text: str) -> list:
    """.inp 会写出的文件名：SAVEDATA 段的全部取值，MONTECARLO / DATA IMPUTATION 段的 SAVE、RESULTS。"""
    names = []
    for head, body in _sections(text):
        for stmt in body.split(";"):
            m = re.match(r"\s*([A-Za-z0-9_]+)\s*(?:=|\bIS\b|\bARE\b)\s*(.*)", stmt, flags=re.I | re.S)
            if m and (head == "SAVEDATA" or (head in ("MONTECARLO", "DATA IMPUTATION")
                                             and m.group(1).upper() in ("SAVE", "RESULTS"))):
                names += [t.strip("\"'") for t in re.split(r"[\s,]+", m.group(2).strip()) if t.strip("\"'")]
    return names


def model_key(inp: Path) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", inp.stem)[:60] or "model"


def _whole(name, value, lo, hi):
    if value is None:
        return None
    if type(value) is not int or not lo <= value <= hi:
        raise ValueError(f"{name} must be a whole number {lo}..{hi} (or omitted), got {value!r}")
    return value


def _finite(name, value, lo, hi, *, above_lo=False):
    if type(value) not in (int, float) or not math.isfinite(value) or not lo <= value <= hi or (above_lo and value == lo):
        raise ValueError(f"{name} must be a finite number {'>' if above_lo else '>='} {lo} and <= {hi}, got {value!r}")
    return float(value)


def limits(jobs=None, cpu_budget=None, mem_reserve_gib=1.5, model_mem_gib=0.5, commit_reserve_gib=2.0,
           disk_reserve_gib=2.0, model_timeout_s=None, admission_wait_s=1800, priority="Normal") -> dict:
    """批内准入参数：全部是有限数字，越界整批拒绝。jobs/cpu_budget 省略时由 Windows 端按模型数/逻辑 CPU−2 定。"""
    if priority not in PRIORITIES:
        raise ValueError(f"priority must be one of {PRIORITIES}, got {priority!r}")
    return {"jobs": _whole("--jobs", jobs, 1, MAX_MODELS),
            "cpu_budget": _whole("--cpu-budget", cpu_budget, 1, 4096),
            "mem_reserve_gib": _finite("--mem-reserve-gib", mem_reserve_gib, 0, 4096),
            "model_mem_gib": _finite("--model-mem-gib", model_mem_gib, 0, 4096, above_lo=True),
            "commit_reserve_gib": _finite("--commit-reserve-gib", commit_reserve_gib, 0, 4096),
            "disk_reserve_gib": _finite("--disk-reserve-gib", disk_reserve_gib, 0, 65536),
            "model_timeout_s": _whole("--model-timeout", model_timeout_s, 1, 30 * 86400),
            "admission_wait_s": _whole("--admission-wait", admission_wait_s, 1, 86400) or 1800,
            "priority": priority}


def plan(inps: list, inputs: list, lim: dict) -> dict:
    """本地检查并生成批次计划；任何一个输入不合格就整批拒绝，不碰远端。"""
    if not inps or len(inps) > MAX_MODELS:
        raise ValueError(f"give 1..{MAX_MODELS} .inp files, got {len(inps)}")
    if len(inps) + len(inputs) > 128:  # W1 task_submit 每个任务最多 128 个输入
        raise ValueError(f"{len(inps)} models + {len(inputs)} shared inputs > 128 files per task")
    models, keys, names = [], {}, {}
    for raw in inps:
        p = Path(raw).expanduser()
        if p.is_symlink() or not p.is_file() or p.suffix.lower() != ".inp":
            raise ValueError(f"not a regular .inp file: {raw}")
        key = model_key(p)
        if key.casefold() in keys:
            raise ValueError(f"two models map to the same directory {key}: {keys[key.casefold()]} and {raw}; rename one")
        if p.name.casefold() in names:  # 输入都平放在任务目录里
            raise ValueError(f"duplicate input file name: {p.name}")
        keys[key.casefold()] = names[p.name.casefold()] = raw
        text = p.read_text(encoding="utf-8", errors="replace")
        procs = inp_processors(text)
        if lim["cpu_budget"] is not None and procs > lim["cpu_budget"]:
            raise ValueError(f"{p.name} asks PROCESSORS={procs} > --cpu-budget {lim['cpu_budget']}; it could never start")
        models.append({"key": key, "local_inp": str(p.resolve()), "inp": p.name, "out": p.stem + ".out",
                       "processors": procs, "writes": written_names(text)})
    shared = []
    for raw in inputs:
        p = Path(raw).expanduser()
        if p.is_symlink() or not p.is_file():
            raise ValueError(f"input is not a regular file: {raw}")
        if p.name.casefold() in names:
            raise ValueError(f"duplicate input file name: {p.name}")
        names[p.name.casefold()] = raw
        size, sha = xdev_tasks._input_digest(p)  # 池收尾按这个 SHA 核共用输入没被改
        shared.append({"name": p.name, "local": str(p.resolve()), "bytes": size, "sha256": sha})
    for m in models:
        writes = m.pop("writes")
        hits = sorted({s["name"] for s in shared for w in writes
                       if fnmatch.fnmatch(s["name"].casefold(), PureWindowsPath(w).name.casefold())})
        if hits:
            raise ValueError(f"{m['inp']} writes over shared input {hits} (SAVEDATA / SAVE / RESULTS); give the output another name")
    batch = "mb" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex
    if not xdev_tasks._valid_name(batch):
        raise ValueError(f"bad batch name {batch}")
    return {"schema": SCHEMA, "batch": batch, "remote_dir": xdev_tasks._work_dir(batch),
            "models": models, "shared": shared, **SCOPE}


# Windows 端进程池，作为 W1 任务正文运行：cwd 是任务目录 work\<name>，输入已由 W1 的输入核验按 SHA 核过。
# PowerShell 变量名不分大小写：根目录叫 $Root，别和循环变量 $r 重名。
# 状态都先写 .part 再替换：models\<key>\xdev_state.json（每个模型）、xdev_pool.json（池心跳）。
POOL_PS = r"""# XDEV_MPLUS_POOL
$Root = (Get-Location).ProviderPath
$cfg = __CFG__ | ConvertFrom-Json
$Meta = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'task.json') -Raw | ConvertFrom-Json
$Owner = [string]$Meta.owner
if ([IO.Path]::GetFullPath([string]$Meta.remote_dir) -ine [IO.Path]::GetFullPath($Root)) { throw 'XDEV_POOL_ROOT_MISMATCH' }
if (-not ('XdevMplusPool' -as [type])) {
  Add-Type @"
using System; using System.IO; using System.ComponentModel; using System.Runtime.InteropServices; using Microsoft.Win32.SafeHandles;
public static class XdevMplusPool {
  [StructLayout(LayoutKind.Sequential)] public struct PPTS { public uint Version; public uint ControlMask; public uint StateMask; }
  [DllImport("kernel32.dll", SetLastError=true)] public static extern bool SetProcessInformation(IntPtr h, int cls, ref PPTS info, uint size);
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] public static extern bool CreateHardLink(string link, string existing, IntPtr sa);
  [StructLayout(LayoutKind.Sequential)] public class MEM { public uint Length = 64; public uint Load; public ulong TotalPhys; public ulong AvailPhys;
    public ulong TotalPageFile; public ulong AvailPageFile; public ulong TotalVirtual; public ulong AvailVirtual; public ulong AvailExtendedVirtual; }
  [DllImport("kernel32.dll", SetLastError=true)] public static extern bool GlobalMemoryStatusEx([In, Out] MEM m);
  // { 可用物理内存, 可用提交量（commit limit - committed） }，字节
  public static double[] Memory() { MEM m = new MEM(); if (!GlobalMemoryStatusEx(m)) throw new Win32Exception(Marshal.GetLastWin32Error()); return new double[] { m.AvailPhys, m.AvailPageFile }; }
  [StructLayout(LayoutKind.Sequential)] public struct BHFI { public uint Attr, C1, C2, A1, A2, W1, W2, Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow; }
  [DllImport("kernel32.dll", SetLastError=true)] public static extern bool GetFileInformationByHandle(SafeFileHandle h, out BHFI info);
  // 卷序列号 + 文件号：硬链接与源相同，复制件不同
  public static string FileId(string path) {
    using (FileStream fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete)) {
      BHFI i; if (!GetFileInformationByHandle(fs.SafeFileHandle, out i)) throw new Win32Exception(Marshal.GetLastWin32Error());
      return i.Volume.ToString("x8") + ":" + i.IndexHigh.ToString("x8") + i.IndexLow.ToString("x8");
    }
  }
}
"@
}
function Utc($t) { ([datetime]$t).ToUniversalTime().ToString('o') }
function Sha([string]$f) { (Get-FileHash -LiteralPath $f -Algorithm SHA256).Hash.ToLower() }
function WriteJson([string]$path, $obj) {
  # 状态作业可能正在读旧文件：替换失败就稍后重试，不让池因一次共享冲突退出
  $tmp = $path + '.part'; $text = $obj | ConvertTo-Json -Compress -Depth 6
  for ($n = 1; $n -le 25; $n++) {
    try {
      [IO.File]::WriteAllText($tmp, $text, (New-Object Text.UTF8Encoding($false)))
      # 备份名必须是真 null：PowerShell 把 $null 传成空串，Replace 会抛异常
      if (Test-Path -LiteralPath $path) { [IO.File]::Replace($tmp, $path, [NullString]::Value) } else { [IO.File]::Move($tmp, $path) }
      return
    } catch { Start-Sleep -Milliseconds 200 }
  }
  [Console]::Error.WriteLine('WRITE_FAILED ' + $path)
}
function State([string]$key) { Join-Path $Root ('models\' + $key + '\xdev_state.json') }
$Clock = [Diagnostics.Stopwatch]::StartNew()
function Ms { [long]$Clock.ElapsedMilliseconds }
$GiB = [double]1073741824
$Drive = New-Object IO.DriveInfo([IO.Path]::GetPathRoot($Root))
$models = @($cfg.models); $shared = @($cfg.shared)
$jobs = if ($null -ne $cfg.jobs) { [int]$cfg.jobs } else { $models.Count }
$budget = if ($null -ne $cfg.cpu_budget) { [int]$cfg.cpu_budget } else { [Math]::Max(1, [Environment]::ProcessorCount - 2) }
$need = [double]$cfg.model_mem_gib * $GiB
$memReserve = [double]$cfg.mem_reserve_gib * $GiB; $commitReserve = [double]$cfg.commit_reserve_gib * $GiB
$diskReserve = [double]$cfg.disk_reserve_gib * $GiB
$timeoutMs = if ($null -ne $cfg.model_timeout_s) { [long]$cfg.model_timeout_s * 1000 } else { [long]0 }
$Exe = Expand ([string]$cfg.mplus)
$Retired = Join-Path $Root '.xdev-retired'
# 每个模型：自己的目录（.inp 复制一份）、自己的 TEMP；共用数据用硬链接，不在 C: 上复制 N 份，不支持时退回复制
$pending = New-Object System.Collections.ArrayList
foreach ($m in $models) {
  $d = Join-Path $Root ('models\' + $m.key)
  [IO.Directory]::CreateDirectory($d) | Out-Null
  [IO.Directory]::CreateDirectory((Join-Path $Root ('tmp\' + $m.key))) | Out-Null
  [IO.File]::Copy((Join-Path $Root $m.inp), (Join-Path $d $m.inp), $false)
  foreach ($s in $shared) {
    $t = Join-Path $d $s.name
    if (-not [XdevMplusPool]::CreateHardLink($t, (Join-Path $Root $s.name), [IntPtr]::Zero)) { [IO.File]::Copy((Join-Path $Root $s.name), $t, $false) }
  }
  [void]$pending.Add($m)
}
# 链接全建好再设只读：只读是文件属性，硬链接共用，模型写同名文件会被拒，改不到共用源
foreach ($m in $models) { foreach ($s in $shared) { (Get-Item -LiteralPath (Join-Path $Root ('models\' + $m.key + '\' + $s.name))).IsReadOnly = $true } }
foreach ($s in $shared) { (Get-Item -LiteralPath (Join-Path $Root $s.name)).IsReadOnly = $true }
function Final($m, [string]$st, [string]$why) {
  WriteJson (State $m.key) ([ordered]@{ owner = $Owner; key = $m.key; state = $st; reason = $why; at_ms = (Ms) })
  $script:bad++; $script:done++
}
function Retire($r) {
  # 模型退出后把本池建的共用链接/副本移出模型目录，取回时不再带回大数据；按文件号或 SHA 认，不按长度/时间
  $out = @()
  foreach ($s in $shared) {
    $t = Join-Path $r.dir $s.name; $src = Join-Path $Root $s.name
    if (-not (Test-Path -LiteralPath $t)) { $out += ,([ordered]@{ name = $s.name; result = 'MISSING' }); continue }
    $same = $false
    try { $same = ([XdevMplusPool]::FileId($t) -ceq [XdevMplusPool]::FileId($src)) } catch { }
    if (-not $same -and (Sha $t) -cne $s.sha256) { $out += ,([ordered]@{ name = $s.name; result = 'CHANGED_KEPT' }); continue }
    $dst = Join-Path $Retired ($r.m.key + '\' + $s.name); $how = 'COPY_RETIRED'
    if ($same) { $how = 'LINK_RETIRED' }
    try { [IO.Directory]::CreateDirectory((Split-Path -Parent $dst)) | Out-Null; [IO.File]::Move($t, $dst) } catch { $how = 'RETIRE_FAILED_KEPT' }
    $out += ,([ordered]@{ name = $s.name; result = $how })
  }
  ,$out
}
function Outstanding {
  # 在跑模型尚未用到的预约额（预约额减当前用量，不为负）：物理内存按工作集，提交量按私有字节
  $phys = [double]0; $commit = [double]0
  foreach ($x in $running.Values) {
    $ws = [double]0; $pb = [double]0
    try { $x.p.Refresh(); $ws = [double]$x.p.WorkingSet64; $pb = [double]$x.p.PrivateMemorySize64 } catch { }
    $phys += [Math]::Max([double]0, $need - $ws); $commit += [Math]::Max([double]0, $need - $pb)
  }
  @($phys, $commit)
}
function BatchAdmit([int]$want) {
  # 批内准入（BATCH_ONLY）：返回挡住的原因，空串 = 可以起。只看本批次自己的进程，不是主机级原子预约。
  # 主机严格准入（W1 GeneralBatchAdmission）要由长寿命执行器持有子进程/Job 句柄，不能在这里短命 acquire 后再由本池起进程。
  if ($running.Count -ge $jobs) { return "jobs $($running.Count)/$jobs" }
  $used = 0; foreach ($x in $running.Values) { $used += [int]$x.m.processors }
  if (($used + $want) -gt $budget) { return "threads $used+$want>$budget" }
  $mem = [XdevMplusPool]::Memory(); $o = Outstanding
  if (($mem[0] - $o[0] - $need) -lt $memReserve) {
    return ('physical available {0:N2} - outstanding {1:N2} - model {2:N2} < reserve {3:N2} GiB' -f ($mem[0] / $GiB), ($o[0] / $GiB), ($need / $GiB), ($memReserve / $GiB))
  }
  if (($mem[1] - $o[1] - $need) -lt $commitReserve) {
    return ('commit available {0:N2} - outstanding {1:N2} - model {2:N2} < reserve {3:N2} GiB' -f ($mem[1] / $GiB), ($o[1] / $GiB), ($need / $GiB), ($commitReserve / $GiB))
  }
  if ([double]$Drive.AvailableFreeSpace -lt $diskReserve) { return ('disk free {0:N2} < reserve {1:N2} GiB' -f ($Drive.AvailableFreeSpace / $GiB), ($diskReserve / $GiB)) }
  ''
}
$running = @{}; $done = 0; $bad = 0; $hold = ''; $holdSince = $null; $maxAlive = 0
while ($pending.Count -gt 0 -or $running.Count -gt 0) {
  # 1. 收尾已退出的模型；超时的只杀本池自己起的那个进程（凭原句柄）
  $alive = 0
  foreach ($k in @($running.Keys)) {
    $r = $running[$k]; $p = $r.p
    if (-not $p.HasExited) {
      if ($timeoutMs -gt 0 -and -not $r.killed -and ((Ms) - $r.start_ms) -ge $timeoutMs) { try { $p.Kill() } catch { }; $r.killed = $true }
      $alive++; continue
    }
    $p.WaitForExit()
    $endMs = Ms; $rc = $p.ExitCode
    $exitUtc = $null; try { $exitUtc = Utc $p.ExitTime } catch { }
    $cpu = $null; try { $cpu = [math]::Round($p.TotalProcessorTime.TotalSeconds, 1) } catch { }
    $kept = Retire $r
    $st = 'DONE'; if ($r.killed) { $st = 'TIMEOUT' }
    WriteJson (State $k) ([ordered]@{ owner = $Owner; key = $k; state = $st; rc = $rc; pid = $p.Id; start_utc = $r.born; exit_utc = $exitUtc;
      start_ms = $r.start_ms; observed_exit_ms = $endMs; cpu_s = $cpu; processors = [int]$r.m.processors; temp_dir = $r.temp;
      priority = $r.prio; throttle_off = $r.thr; shared = $kept })
    $running.Remove($k); $done++
    if ($st -ne 'DONE' -or $rc -ne 0) { $bad++ }
  }
  if ($alive -gt $maxAlive) { $maxAlive = $alive }
  # 2. 按本批次的数字起新模型；任何模型（包括第一个）都要过线程预算
  $hold = ''
  while ($pending.Count -gt 0) {
    $m = $pending[0]; $want = [int]$m.processors
    if ($want -gt $budget) { $pending.RemoveAt(0); Final $m 'REFUSED' "PROCESSORS $want > cpu budget $budget"; continue }
    $hold = BatchAdmit $want
    if ($hold) { break }
    $pending.RemoveAt(0)
    $d = Join-Path $Root ('models\' + $m.key); $tdir = Join-Path $Root ('tmp\' + $m.key)
    # 子进程创建时继承本进程环境：先换成本模型的 TEMP/TMP，起完马上换回
    $oldTemp = $env:TEMP; $oldTmp = $env:TMP; $env:TEMP = $tdir; $env:TMP = $tdir
    $p = $null; $why = ''
    try {
      $p = Start-Process -FilePath $Exe -ArgumentList ('"' + $m.inp + '" "' + $m.out + '"') -WorkingDirectory $d -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $d 'xdev_console.log') -RedirectStandardError (Join-Path $d 'xdev_console.err')
    } catch { $why = [string]$_ } finally { $env:TEMP = $oldTemp; $env:TMP = $oldTmp }
    if ($null -eq $p) { Final $m 'START_FAILED' $why; continue }
    $startMs = Ms; $null = $p.Handle
    $prio = 'inherit'; $thr = $false
    if ($cfg.priority -ne 'inherit') {
      try { $p.PriorityClass = [string]$cfg.priority; $prio = [string]$cfg.priority } catch { $prio = 'set_failed' }
      try {
        $pt = New-Object XdevMplusPool+PPTS; $pt.Version = 1; $pt.ControlMask = 1; $pt.StateMask = 0
        $thr = [XdevMplusPool]::SetProcessInformation($p.Handle, 4, [ref]$pt, [uint32][Runtime.InteropServices.Marshal]::SizeOf($pt))
      } catch { $thr = $false }
    }
    $born = Utc $p.StartTime
    $running[$m.key] = @{ m = $m; p = $p; dir = $d; temp = $tdir; born = $born; start_ms = $startMs; prio = $prio; thr = $thr; killed = $false }
    WriteJson (State $m.key) ([ordered]@{ owner = $Owner; key = $m.key; state = 'RUNNING'; pid = $p.Id; start_utc = $born; start_ms = $startMs;
      processors = [int]$m.processors; temp_dir = $tdir; priority = $prio; throttle_off = $thr })
  }
  # 3. 什么都没在跑还被内存/提交量/磁盘挡住：等满 admission_wait_s 就把剩下的记 NOT_ADMITTED，池不无限挂着
  if ($hold -and $running.Count -eq 0) {
    if ($null -eq $holdSince) { $holdSince = Ms }
    elseif (((Ms) - $holdSince) -ge ([long]$cfg.admission_wait_s * 1000)) {
      foreach ($m in @($pending)) { Final $m 'NOT_ADMITTED' ('nothing running and still held after ' + $cfg.admission_wait_s + ' s: ' + $hold) }
      $pending.Clear()
    }
  } else { $holdSince = $null }
  $mem = [XdevMplusPool]::Memory()
  WriteJson (Join-Path $Root 'xdev_pool.json') ([ordered]@{ owner = $Owner; at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); elapsed_ms = (Ms); pid = $PID;
    running = @($running.Keys); pending = $pending.Count; done = $done; failed = $bad; hold = $hold; max_alive_observed = $maxAlive;
    available_phys_gib = [math]::Round($mem[0] / $GiB, 2); available_commit_gib = [math]::Round($mem[1] / $GiB, 2);
    free_disk_gib = [math]::Round($Drive.AvailableFreeSpace / $GiB, 2); jobs = $jobs; cpu_budget = $budget; finished = $false })
  if ($pending.Count -gt 0 -or $running.Count -gt 0) { Start-Sleep -Milliseconds 1000 }
}
# 4. 收尾：核共用源 SHA；移出的链接确认仍是同一文件（副本则 SHA 未变）才删；最后恢复源文件可写
$sharedOk = $true; $report = @()
foreach ($s in $shared) {
  $src = Join-Path $Root $s.name
  if ((Sha $src) -cne $s.sha256) { $sharedOk = $false; $report += ,([ordered]@{ name = $s.name; sha_ok = $false }); continue }
  $srcId = [XdevMplusPool]::FileId($src); $removed = 0; $kept = 0
  foreach ($m in $models) {
    $l = Join-Path $Retired ($m.key + '\' + $s.name)
    if (-not (Test-Path -LiteralPath $l)) { continue }
    $isLink = $false; try { $isLink = ([XdevMplusPool]::FileId($l) -ceq $srcId) } catch { }
    if ($isLink -or (Sha $l) -ceq $s.sha256) { Remove-Item -LiteralPath $l -Force; $removed++ } else { $kept++ }
  }
  (Get-Item -LiteralPath $src).IsReadOnly = $false
  $report += ,([ordered]@{ name = $s.name; sha_ok = $true; retired_removed = $removed; retired_kept = $kept })
}
$code = 0; if ($bad -gt 0) { $code = 3 }; if (-not $sharedOk) { $code = 4 }
$mem = [XdevMplusPool]::Memory()
WriteJson (Join-Path $Root 'xdev_pool.json') ([ordered]@{ owner = $Owner; at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); elapsed_ms = (Ms); pid = $PID;
  running = @(); pending = 0; done = $done; failed = $bad; hold = ''; max_alive_observed = $maxAlive;
  available_phys_gib = [math]::Round($mem[0] / $GiB, 2); available_commit_gib = [math]::Round($mem[1] / $GiB, 2);
  free_disk_gib = [math]::Round($Drive.AvailableFreeSpace / $GiB, 2); jobs = $jobs; cpu_budget = $budget; finished = $true;
  shared_verified = $sharedOk; shared = $report; exit_code = $code })
exit $code
"""


def pool_command(pl: dict, lim: dict, mplus: str) -> str:
    cfg = {"mplus": mplus, **{k: lim[k] for k in ("jobs", "cpu_budget", "mem_reserve_gib", "model_mem_gib",
                                                   "commit_reserve_gib", "disk_reserve_gib", "model_timeout_s",
                                                   "admission_wait_s", "priority")},
           "models": [{k: m[k] for k in ("key", "inp", "out", "processors")} for m in pl["models"]],
           "shared": [{k: s[k] for k in ("name", "sha256")} for s in pl["shared"]]}
    return POOL_PS.replace("__CFG__", ps_quote(json.dumps(cfg)))


def poll_once(man: dict) -> dict:
    """一个只读状态作业看全部模型：先核 task.json 的 owner/名字/目录是本批次的，再先读 runner 回执后读状态，
    这样回执已在时读到的模型状态都是终态。不枚举 Win32_Process（这台机器上一次要 20–30 s，会堵住 agent）。"""
    t = man["task"]
    keys = [m["key"] for m in man["models"]]
    script = f"""# XDEV_MPLUS_POLL
$J = Join-Path $XRoot {ps_quote('jobs' + chr(92) + t['name'])}
function ReadShared([string]$f) {{
  if (-not (Test-Path -LiteralPath $f)) {{ return $null }}
  $fs = [IO.File]::Open($f, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
  try {{ (New-Object IO.StreamReader($fs, [Text.Encoding]::UTF8)).ReadToEnd() }} finally {{ $fs.Dispose() }}
}}
$mt = ReadShared (Join-Path $J 'task.json')
if (-not $mt) {{ Emit @{{ ok = $false; reason = 'NO_TASK_META' }}; exit 3 }}
$meta = $mt | ConvertFrom-Json; $W = Expand {ps_quote(t['remote_dir'])}
if ($meta.owner -cne {ps_quote(t['owner'])} -or $meta.name -cne {ps_quote(t['name'])} -or $meta.kind -cne 'task' -or
    [IO.Path]::GetFullPath([string]$meta.remote_dir) -ine [IO.Path]::GetFullPath($W)) {{
  Emit @{{ ok = $false; reason = 'TASK_BINDING_MISMATCH' }}; exit 7
}}
$runner = ReadShared (Join-Path $J 'rc.txt')
$keys = ({ps_quote(json.dumps(keys))} | ConvertFrom-Json)
$states = @(foreach ($k in $keys) {{ $s = ReadShared (Join-Path $W ('models\\' + $k + '\\xdev_state.json')); if ($s) {{ $s }} }})
Emit @{{ ok = $true; now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); runner = $runner; pool = (ReadShared (Join-Path $W 'xdev_pool.json')); states = $states }}
"""
    rc, out, err, i = run_ps(script, timeout=60, wait=240, quiet=True)
    rows = emitted(out)
    if rc != 0 or len(rows) != 1 or not rows[0].get("ok"):
        why = rows[0].get("reason") if len(rows) == 1 else None
        return {"ok": False, "fatal": why == "TASK_BINDING_MISMATCH", "reason": f"rc={rc} job={i} {why or (out + err)[-400:]}"}
    r = rows[0]
    states, bad = {}, []
    for s in r.get("states") or []:
        try:
            d = json.loads(s)
        except (TypeError, ValueError):
            continue  # 正在替换的半截文件：下一轮再读
        if not isinstance(d, dict) or d.get("owner") != t["owner"] or d.get("key") not in keys:
            bad.append(str(s)[:200])
        else:
            states[d["key"]] = d
    if bad:
        return {"ok": False, "fatal": True, "reason": f"STATE_OWNER_MISMATCH job={i} {bad[:3]}"}
    try:
        pool = json.loads(r["pool"]) if r.get("pool") else None
    except ValueError:
        pool = None
    if pool is not None and pool.get("owner") != t["owner"]:
        return {"ok": False, "fatal": True, "reason": f"POOL_OWNER_MISMATCH job={i}"}
    return {"ok": True, "now": r.get("now"), "pool": pool, "states": states, "runner": r.get("runner"), "job": i}


def _save(man: dict, out_dir: Path) -> None:
    tmp = out_dir / (MANIFEST + ".part")
    tmp.write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out_dir / MANIFEST)


def _ts(iso: str) -> float:
    # .NET 'o' 格式有 7 位小数；Python 只认 6 位
    from datetime import datetime
    return datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", iso).replace("Z", "+00:00")).timestamp()


def _collect_one(man: dict, key: str, out_dir: Path) -> dict:
    """用 W1 pull_dir 取回一个模型目录。原次只试一次：结果未知（124、异常或认领文件还在）就照原样保留
    远端 zip 和本机认领，不删、不重取；以后 --resume 也不会再取这个模型。"""
    rec = man["results"].setdefault(key, {})
    if "collect" in rec:
        if rec["collect"]["status"] == "STARTED":  # 上次在取回途中断开：原次结果未知，不重取
            rec["collect"].update(status="UNKNOWN", error="interrupted during the original pull_dir; not repeated")
            _save(man, out_dir)
        return rec["collect"]
    local = out_dir / key
    rec["collect"] = {"status": "STARTED", "local": str(local), "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    _save(man, out_dir)
    remote = man["task"]["remote_dir"] + "\\models\\" + key
    why = None
    try:
        rc = pull_dir(remote, str(local))
    except (OSError, ValueError, RuntimeError) as exc:
        rc, why = 7, str(exc)
    claim = local.with_name("." + local.name + ".xdev-claim.json")
    status = "COMPLETE" if rc == 0 else "FAILED"
    if rc == 124 or why or claim.exists():
        status = "UNKNOWN"
    rec["collect"].update(status=status, rc=rc, remote=remote, error=why)
    if claim.exists():
        try:
            rec["collect"]["claim"] = json.loads(claim.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    _save(man, out_dir)
    return rec["collect"]


def _judge(man: dict, key: str, out_dir: Path) -> dict:
    """模型成功 = 原进程 DONE 且 rc == 0、同一身份、取回完整、.out 判定通过；文本通过不能盖过非零退出码。"""
    m = next(x for x in man["models"] if x["key"] == key)
    s = man["states"].get(key) or {}
    rec = man["results"].setdefault(key, {})
    col = rec.get("collect") or {}
    out_file = out_dir / key / m["out"]
    if col.get("status") == "COMPLETE" and out_file.is_file():
        chk = check_out(out_file.read_text(encoding="utf-8", errors="replace"))
    else:
        chk = {"ok": False, "reason": "no .out came back" if col.get("status") == "COMPLETE"
               else f"not collected ({col.get('status') or 'never ran'})"}
    same = man["seen"].get(key) in (None, {"pid": s.get("pid"), "start_utc": s.get("start_utc")})
    rc = s.get("rc")
    ok = (s.get("state") == "DONE" and type(rc) is int and rc == 0 and same
          and col.get("status") == "COMPLETE" and chk["ok"])
    rec.update(ok=ok, state=s.get("state") or "NO_FINAL_STATE", rc=rc, reason=s.get("reason"), check=chk,
               identity_ok=same, out=str(out_file) if out_file.is_file() else None,
               **{k: s.get(k) for k in ("pid", "start_utc", "exit_utc", "start_ms", "observed_exit_ms", "cpu_s",
                                        "processors", "priority", "throttle_off", "temp_dir", "shared")})
    if type(s.get("start_ms")) is int and type(s.get("observed_exit_ms")) is int:
        rec["observed_wall_s"] = round((s["observed_exit_ms"] - s["start_ms"]) / 1000, 3)
    if s.get("start_utc") and s.get("exit_utc"):
        rec["kernel_wall_s"] = round(_ts(s["exit_utc"]) - _ts(s["start_utc"]), 3)
    return rec


def timing(results: dict, pool: dict | None) -> dict:
    """进程区间取 Windows 内核记录的 StartTime/ExitTime；max_alive_observed 是池同一轮扫描里
    HasExited=false 的子进程个数，是同一时刻真实存活的直接观测。墙钟差用池的单调计时（Stopwatch）。"""
    pool = pool or {}
    out = {"max_alive_observed": pool.get("max_alive_observed"),
           "pool_elapsed_s": round(pool["elapsed_ms"] / 1000, 3) if type(pool.get("elapsed_ms")) is int else None}
    spans = [(_ts(x["start_utc"]), _ts(x["exit_utc"])) for x in results.values() if x.get("start_utc") and x.get("exit_utc")]
    if spans:
        # 同一时刻先算退出再算启动：首尾相接的两个进程不算重叠
        events = sorted([(a, 1) for a, _ in spans] + [(b, -1) for _, b in spans])
        cur = peak = 0
        for _, d in events:
            cur += d
            peak = max(peak, cur)
        out.update(kernel_span_s=round(max(b for _, b in spans) - min(a for a, _ in spans), 3),
                   kernel_sum_s=round(sum(b - a for a, b in spans), 3), kernel_interval_peak=peak)
    return out


def _journal_owner(name: str):
    """owner 以本机 W1 任务 journal 为准（同时核 BODY/RUNNER 的冻结 SHA）。"""
    try:
        return xdev_tasks._load_task(xdev_tasks._task_root(name), name)["owner"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _record_task(man: dict, res: dict) -> None:
    keep = ("ok", "stage", "state", "rc", "reason", "task_receipt", "reserve_job_id", "start_job_id",
            "runner_pid", "job_id", "upload_status", "pending_inputs", "hint")
    man["task"].update({k: res.get(k) for k in keep if k in res})
    owner = _journal_owner(man["task"]["name"])
    if owner:
        man["task"]["owner"] = owner


def _finish(man: dict, out_dir: Path, t0: float) -> int:
    keys = [m["key"] for m in man["models"]]
    pool, fin = man.get("pool") or {}, man["task"].get("final") or {}
    man["mac_follow_s"] = round(time.monotonic() - t0, 1)
    man["timing"] = timing(man["results"], pool)
    unknown = [k for k in keys if (man["results"].get(k, {}).get("collect") or {}).get("status") == "UNKNOWN"]
    # 批次成功 = W1 核到原任务 DONE rc 0、池正常收尾且共用输入 SHA 未变、每个模型都成功
    man["ok"] = (fin.get("state") == "DONE" and fin.get("rc") == 0 and pool.get("finished") is True
                 and pool.get("shared_verified") is True and all(man["results"].get(k, {}).get("ok") is True for k in keys))
    _save(man, out_dir)
    summary = {"ok": man["ok"], "batch": man["batch"], "task_rc": fin.get("rc"), "models": len(keys),
               "failed": [k for k in keys if man["results"].get(k, {}).get("ok") is not True], "collect_unknown": unknown,
               "shared_verified": pool.get("shared_verified"), "capacity_scope": SCOPE["capacity_scope"],
               **man["timing"], "manifest": str(out_dir / MANIFEST)}
    print("XDEV_MPLUS_BATCH " + json.dumps(summary, ensure_ascii=False))
    return 0 if man["ok"] else (7 if unknown else 1)


def follow(man: dict, out_dir: Path, max_seconds: int) -> int:
    """轮询到池退出、并经 W1 task_status 核到原任务 DONE 为止；中途跑完的模型马上取回、判定。"""
    t0, gap, last = time.monotonic(), 3, None
    end = t0 + max_seconds
    keys = [m["key"] for m in man["models"]]
    name = man["task"]["name"]
    if not man["task"].get("owner"):
        return fail(f"XDEV_MPLUS_BINDING_FAILED {name}: no verified owner in the local task journal; nothing polled", 7)
    while True:
        r = poll_once(man)
        if not r["ok"]:
            if r.get("fatal"):
                man["task"]["poll_error"] = r["reason"]
                _save(man, out_dir)
                return fail(f"XDEV_MPLUS_BINDING_FAILED {r['reason']}; nothing collected or rerun", 7)
            print(f"XDEV_MPLUS_POLL_RETRY {r['reason']}", file=sys.stderr)
        else:
            for k, s in r["states"].items():
                if s.get("state") in ("RUNNING",) + RAN:
                    man["seen"].setdefault(k, {"pid": s.get("pid"), "start_utc": s.get("start_utc")})
                man["states"][k] = s
            for k in keys:
                state = (man["states"].get(k) or {}).get("state")
                if "ok" in man["results"].get(k, {}) or state not in RAN + NOT_RUN:
                    continue
                if state in RAN:
                    _collect_one(man, k, out_dir)
                rec = _judge(man, k, out_dir)
                _save(man, out_dir)
                print(f"XDEV_MPLUS_DONE {k} ok={rec['ok']} state={rec['state']} rc={rec['rc']}"
                      f" kernel_wall_s={rec.get('kernel_wall_s')} collect={(rec.get('collect') or {}).get('status')} out={rec['out']}")
            pool = r["pool"] or {}
            if pool:
                man["pool"] = pool
            note = (len(pool.get("running") or []), pool.get("pending"), pool.get("done"), pool.get("hold"))
            if note != last:
                print(f"  {time.strftime('%H:%M:%S')} {man['batch']} running={note[0]} pending={note[1]} done={note[2]}"
                      f" alive_max={pool.get('max_alive_observed')} phys={pool.get('available_phys_gib')}GiB"
                      f" commit={pool.get('available_commit_gib')}GiB {('hold: ' + note[3]) if note[3] else ''}", file=sys.stderr)
                last = note
            if r["runner"] is not None:
                # runner 回执先于状态读取：此时没有终态的模型就是池异常退出留下的，不取回、不补跑
                ts = xdev_tasks.task_status(name, nbytes=2000)
                man["task"]["final"] = {k: ts.get(k) for k in ("ok", "state", "rc", "finished", "job_id", "binding_verified",
                                                               "reason", "err", "runner_error")}
                if not ts.get("ok") or ts.get("state") != "DONE":
                    _save(man, out_dir)
                    return fail(f"XDEV_MPLUS_TASK_UNCONFIRMED {name} state={ts.get('state')} {ts.get('reason')}; nothing rerun", 7)
                for k in keys:
                    if "ok" not in man["results"].get(k, {}):
                        _judge(man, k, out_dir)
                break
            age = (r["now"] or 0) - (pool.get("at") or r["now"] or 0)
            if pool and age > 300:
                _save(man, out_dir)
                return fail(f"XDEV_MPLUS_POOL_STALE heartbeat {age}s old; inspect with: xdev task-status {name}", 7)
        if time.monotonic() + gap > end:
            _save(man, out_dir)
            print(f"XDEV_MPLUS_STILL_RUNNING {man['batch']} (later: xdev mplus --resume {out_dir})")
            return 124
        time.sleep(gap)
        gap = min(gap * 2, 30)
    return _finish(man, out_dir, t0)


def run(inps: list, inputs: list, out: str, max_seconds: int, **options) -> int:
    """options 见 limits()。先在本机落清单，再交给 W1 task_submit；任何一步结果未知都只能 --resume 观察原次。"""
    from xdev_tools import _tool
    out_dir = Path(out).expanduser()
    try:
        if type(max_seconds) is not int or max_seconds < 0:
            raise ValueError(f"--max must be a whole number of seconds >= 0, got {max_seconds!r}")
        lim = limits(**options)
        pl = plan(inps, inputs, lim)
        mplus = _tool("mplus")
        command = pool_command(pl, lim, mplus)
        if len(command.encode()) > 60 * 1024:  # W1 task 正文上限 64 KiB
            raise ValueError(f"pool body is {len(command.encode())} bytes; split the batch")
    except ValueError as exc:
        return fail(f"XDEV_MPLUS_REFUSED {exc}", 2)
    if (out_dir / MANIFEST).exists():
        return fail(f"XDEV_MPLUS_REFUSED {out_dir / MANIFEST} exists; continue it with --resume {out_dir} or pick a new --out", 3)
    busy = [m["key"] for m in pl["models"]
            if (out_dir / m["key"]).exists() or (out_dir / f".{m['key']}.xdev-claim.json").exists()]
    if busy:
        return fail(f"XDEV_MPLUS_REFUSED local model dirs or claims already exist under {out_dir}: {busy}", 3)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = pl["batch"]
    man = {**pl, "limits": lim, "mplus": mplus, "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "task": {"name": name, "remote_dir": pl["remote_dir"], "state": "SUBMITTING"},
           "states": {}, "seen": {}, "results": {}}
    _save(man, out_dir)
    sub = xdev_tasks.task_submit([m["local_inp"] for m in pl["models"]] + [s["local"] for s in pl["shared"]], command, name)
    _record_task(man, sub)
    _save(man, out_dir)
    if not sub.get("ok"):
        if sub.get("state") == "REFUSED":
            return fail(f"XDEV_MPLUS_REFUSED {json.dumps(sub, ensure_ascii=False)}", sub.get("rc") or 2)
        return fail(f"XDEV_MPLUS_START_UNKNOWN {name} stage={sub.get('stage')} state={sub.get('state')} {sub.get('reason')};"
                    f" observe with: xdev mplus --resume {out_dir} (never resubmits)", 7)
    print(f"XDEV_MPLUS_SUBMITTED {name} models={len(pl['models'])} out={out_dir}", file=sys.stderr)
    return follow(man, out_dir, max_seconds)


def resume(out: str, max_seconds: int) -> int:
    """接回 OUT 里的原批次。原预约还没启动时，按 W1 task_resume 只上传未尝试的输入并启动一次；原次未知的不重放。"""
    out_dir = Path(out).expanduser()
    try:
        man = json.loads((out_dir / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return fail(f"XDEV_MPLUS_NO_MANIFEST {out_dir / MANIFEST}: {exc}", 3)
    if man.get("schema") != SCHEMA:
        return fail(f"XDEV_MPLUS_REFUSED {out_dir / MANIFEST} is {man.get('schema')}; batches before v2 were not W1 tasks"
                    " and stay as recorded", 3)
    name = man["task"]["name"]
    owner = _journal_owner(name)
    if owner is None or man["task"].get("owner") not in (None, owner):
        return fail(f"XDEV_MPLUS_BINDING_FAILED {name}: local task journal missing or changed; nothing resumed", 7)
    man["task"]["owner"] = owner
    if "final" not in man["task"]:
        ts = xdev_tasks.task_status(name)
        man["task"]["resume_status"] = {k: ts.get(k) for k in ("ok", "state", "rc", "reason", "upload_status", "job_id")}
        if ts.get("ok") and ts.get("state") == "RESERVED":
            _record_task(man, xdev_tasks.task_resume(name))
            _save(man, out_dir)
            if not man["task"].get("ok"):
                return fail(f"XDEV_MPLUS_START_UNKNOWN {name} {man['task'].get('state')} {man['task'].get('reason')}; nothing replayed", 7)
        elif not ts.get("ok"):
            _save(man, out_dir)
            return fail(f"XDEV_MPLUS_TASK_UNKNOWN {name} {ts.get('state')} {ts.get('reason')}; original requests kept, nothing replayed",
                        ts.get("rc") or 7)
    _save(man, out_dir)
    return follow(man, out_dir, max_seconds)
