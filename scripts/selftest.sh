#!/bin/sh
# xdev 自测（v2）：只用合成文件，任务名带时间戳；会在 Windows 的 %LOCALAPPDATA%\xdev\inbox 留两个小文件
HERE=$(cd "$(dirname "$0")" && pwd); X="$HERE/xdev"
T=$(date +%H%M%S); W=${TMPDIR:-/tmp}/xdev-selftest-$T; mkdir -p "$W"; cd "$W" || exit 1
pass=0; fail=0
ok() { if [ "$1" = "$2" ]; then pass=$((pass+1)); echo "PASS $3"; else fail=$((fail+1)); echo "FAIL $3 (got $1 want $2)"; fi; }
python3 -B -c "import ast,sys; [ast.parse(open(f,encoding='utf-8').read(), f) for f in sys.argv[1:]]" "$HERE"/*.py; ok $? 0 "syntax"
$X health >/dev/null 2>&1; ok $? 0 "health"
head -c 300000 /dev/urandom > "rt 中文.bin"; s1=$(shasum -a 256 "rt 中文.bin" | cut -c1-64)
$X push "rt 中文.bin" "%LOCALAPPDATA%\\xdev\\inbox\\rt-$T 中文.bin" >/dev/null 2>&1; ok $? 0 "push"
$X pull "%LOCALAPPDATA%\\xdev\\inbox\\rt-$T 中文.bin" "back.bin" >/dev/null 2>&1; ok $? 0 "pull"
ok "$(shasum -a 256 back.bin | cut -c1-64)" "$s1" "roundtrip sha256"
$X detach "rg-$T" -c '"root=" + $XRoot; "expand_matches=" + ((Expand "%LOCALAPPDATA%") -eq $env:LOCALAPPDATA); exit 7' >/dev/null 2>&1; ok $? 0 "detach"
$X wait "rg-$T" --max 120 > wait.out 2>&1; ok $? 7 "wait rc passthrough"
grep -q "XDEV_STATE rg-$T DONE rc=7" wait.out; ok $? 0 "wait state DONE (not LOST)"
$X tail "rg-$T" 2>&1 | grep -q "expand_matches=True"; ok $? 0 "detach Expand defined"
$X detach "rg-$T" -c 'exit 0' >/dev/null 2>&1; ok $? 3 "duplicate name refused"
$X pull '%LOCALAPPDATA%\xdev\inbox\no-such-file.bin' nf.bin >/dev/null 2>&1; ok $? 3 "pull missing -> 3"
python3 -B "$HERE/test_advertise.py" >/dev/null 2>&1; ok $? 0 "advertise-host selftest"
python3 -B "$HERE/test_result_read.py" "$HERE/xdev_core.py" result-reader.json >/dev/null 2>&1; ok $? 0 "partial and Unicode result reader"
echo "REGRESS pass=$pass fail=$fail dir=$W"
[ "$fail" -eq 0 ]
