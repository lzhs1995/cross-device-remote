"""xdev push / pull / pull-dir：Mac 与 Windows 之间搬文件，两端都按 sha256 核。

push：文件放进 <state_dir>/files/，Windows 经 /file/<名字> 下载、核哈希，成功后删暂存件。
pull：Windows 端按块（3 MB）读出、base64 放进回执，Mac 端拼回、核整文件 sha256。服务端协议不变。
"""
import base64
import hashlib
import re
import shutil
import time
import zipfile
from pathlib import Path

from xdev_core import STATE, emitted, fail, ps_quote, run_ps

CHUNK = 3 * 1024 * 1024  # 回执上限 8 MB 文本；3 MB 原始数据 base64 后约 4 MB


def push(local: str, remote: str | None = None, overwrite: bool = False) -> int:
    src = Path(local).expanduser()
    data = src.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", src.name)[:80] or "file"
    staged = f"xdev-{sha[:12]}-{safe}"
    (STATE / "files" / staged).write_bytes(data)
    dest = remote or rf"%LOCALAPPDATA%\xdev\inbox\{src.name}"
    script = f"""$Dest = Expand {ps_quote(dest)}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Dest) | Out-Null
if ((Test-Path -LiteralPath $Dest) -and -not {'$true' if overwrite else '$false'}) {{ Emit @{{ ok = $false; reason = 'DEST_EXISTS'; dest = $Dest }}; exit 3 }}
$Tmp = $Dest + '.xdev-part'
Invoke-WebRequest -UseBasicParsing -Uri ($MacServer + '/file/{staged}') -OutFile $Tmp -TimeoutSec 600
$h = (Get-FileHash -LiteralPath $Tmp -Algorithm SHA256).Hash.ToLower()
if ($h -ne '{sha}') {{ Remove-Item -LiteralPath $Tmp -Force; Emit @{{ ok = $false; reason = 'HASH_MISMATCH'; got = $h }}; exit 4 }}
if (Test-Path -LiteralPath $Dest) {{ Move-Item -LiteralPath $Dest -Destination ($Dest + '.xdev-old-' + (Get-Date -Format 'yyyyMMddHHmmss')) }}
Move-Item -LiteralPath $Tmp -Destination $Dest
Emit @{{ ok = $true; dest = $Dest; sha256 = $h; bytes = (Get-Item -LiteralPath $Dest).Length }}
"""
    rc, out, err, i = run_ps(script, timeout=max(120, len(data) // 200_000))
    rows = emitted(out)
    if rc == 0 and rows and rows[-1].get("ok"):
        (STATE / "files" / staged).unlink()  # 暂存件是本工具建的副本，核过就删
        print(f"XDEV_PUSHED {rows[-1]['dest']} sha256={sha} bytes={len(data)} job={i}")
        return 0
    return fail(f"XDEV_PUSH_FAILED rc={rc} job={i} {rows[-1] if rows else (out + err)[-800:]}", rc or 1)


def _chunk_script(remote: str, offset: int, with_stat: bool) -> str:
    stat = "$h = (Get-FileHash -LiteralPath $P -Algorithm SHA256).Hash.ToLower()\n" if with_stat else "$h = ''\n"
    return f"""$P = Expand {ps_quote(remote)}
if (-not (Test-Path -LiteralPath $P -PathType Leaf)) {{ Emit @{{ ok = $false; reason = 'NOT_FOUND'; path = $P }}; exit 3 }}
{stat}$fs = New-Object IO.FileStream($P, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
try {{
  $size = $fs.Length; [void]$fs.Seek({offset}, [IO.SeekOrigin]::Begin)
  $buf = New-Object byte[] {CHUNK}; $n = $fs.Read($buf, 0, {CHUNK})
}} finally {{ $fs.Dispose() }}
Emit @{{ ok = $true; size = $size; sha256 = $h; offset = {offset}; n = $n; b64 = [Convert]::ToBase64String($buf, 0, $n) }}
"""


def pull(remote: str, local: str, overwrite: bool = False, quiet: bool = False) -> int:
    dst = Path(local).expanduser()
    if dst.exists() and not overwrite:
        return fail(f"XDEV_PULL_REFUSED local exists: {dst}", 3)
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".xdev-part")
    offset, size, want = 0, None, None
    with open(part, "wb") as f:
        while size is None or offset < size:
            rc, out, err, i = run_ps(_chunk_script(remote, offset, size is None), timeout=300)
            rows = emitted(out)
            if rc != 0 or not rows or not rows[-1].get("ok"):
                f.close(); part.unlink()
                return fail(f"XDEV_PULL_FAILED rc={rc} job={i} {rows[-1] if rows else (out + err)[-800:]}", rc or 1)
            r = rows[-1]
            if size is None:
                size, want = int(r["size"]), r["sha256"]
            if int(r["n"]) == 0 and offset < size:
                f.close(); part.unlink()
                return fail(f"XDEV_PULL_SHORT_READ offset={offset} size={size}", 5)
            f.write(base64.b64decode(r["b64"]))
            offset += int(r["n"])
    got = hashlib.sha256(part.read_bytes()).hexdigest()
    if got != want:
        part.unlink()
        return fail(f"XDEV_PULL_CHANGED remote file changed during pull (want {want}, got {got}); pull again when it is final", 5)
    if dst.exists():
        dst.rename(dst.with_name(dst.name + ".xdev-old-" + time.strftime("%Y%m%d%H%M%S")))
    part.rename(dst)
    if not quiet:
        print(f"XDEV_PULLED {dst} sha256={got} bytes={size}")
    return 0


def pull_dir(remote_dir: str, local_dir: str) -> int:
    dst = Path(local_dir).expanduser()
    if dst.exists() and any(dst.iterdir()):
        return fail(f"XDEV_PULL_REFUSED local dir not empty: {dst}", 3)
    tag = time.strftime("%Y%m%d%H%M%S")
    zscript = f"""Add-Type -AssemblyName System.IO.Compression.FileSystem
$D = Expand {ps_quote(remote_dir)}
if (-not (Test-Path -LiteralPath $D -PathType Container)) {{ Emit @{{ ok = $false; reason = 'NOT_FOUND'; path = $D }}; exit 3 }}
$Z = Join-Path $XRoot 'outbox\\pull-{tag}.zip'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Z) | Out-Null
[IO.Compression.ZipFile]::CreateFromDirectory($D, $Z)
Emit @{{ ok = $true; zip = $Z; files = @(Get-ChildItem -LiteralPath $D -Recurse -File).Count }}
"""
    rc, out, err, i = run_ps(zscript, timeout=600)
    rows = emitted(out)
    if rc != 0 or not rows or not rows[-1].get("ok"):
        return fail(f"XDEV_ZIP_FAILED rc={rc} job={i} {rows[-1] if rows else (out + err)[-800:]}", rc or 1)
    rzip, nfiles = rows[-1]["zip"], rows[-1]["files"]
    lzip = dst.parent / f".xdev-pull-{tag}.zip"
    rc = pull(rzip, str(lzip), quiet=True)
    run_ps(f"Remove-Item -LiteralPath {ps_quote(rzip)} -Force", timeout=60, quiet=True)  # 远端临时 zip 是本工具建的
    if rc:
        return rc
    dst.mkdir(parents=True, exist_ok=True)
    root = dst.resolve()
    with zipfile.ZipFile(lzip) as z:
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            if name.endswith("/"):
                continue
            target = (root / name).resolve()
            if root not in target.parents:  # 防 zip 路径穿越
                return fail(f"XDEV_UNSAFE_ZIP_ENTRY {info.filename}", 5)
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as s, open(target, "wb") as t:
                shutil.copyfileobj(s, t)
    lzip.unlink()
    got = sorted(p for p in root.rglob("*") if p.is_file())
    for p in got:
        print(f"  {hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(root)}")
    print(f"XDEV_PULLED_DIR {root} files={len(got)} remote_files={nfiles}")
    return 0 if len(got) == nfiles else 5
