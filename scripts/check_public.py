#!/usr/bin/env python3
"""Scan publishable files and optionally Git history for private machine material."""
import argparse
import ipaddress
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent.parent
SKIP = {'.git', '__pycache__', '.venv', '.pytest_cache'}
TEXT = {'.py', '.ps1', '.vbs', '.md', '.json', '.sh', '.yml', '.yaml', '.svg', '.inp', '.dat', '.r', '.txt'}


def problems(name, raw):
    reasons = []
    if len(raw) > 2_000_000:
        reasons.append('unexpected large file')
    try:
        s = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        return reasons + ['unreviewed binary content']
    rules = {
        'private tailnet hostname': r'\b[\w.-]+\.ts\.net\b',
        'personal macOS home': r'/Users/(?!<|example\b)[A-Za-z0-9_.-]+/',
        'personal Windows home': r'(?i)[A-Z]:[/\\]Users[/\\](?!<|Public\b|example\b)[A-Za-z0-9_.-]+[/\\]',
        'personal Linux home': r'/home/(?!<|example\b)[A-Za-z0-9_.-]+/',
        'private key': r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
        'GitHub token': r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b',
        'provider token': r'\bsk-[A-Za-z0-9_-]{30,}\b',
        'personal research path': r'Documents/c[n]m|PP_LGCM_Remote_' + r'Continuation|tailf[0-9a-f]+',
    }
    # 本檔的正規式本身不是機器資料。
    for kind, pattern in rules.items():
        if re.search(pattern, s):
            reasons.append(kind)
    for value in re.findall(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])', s):
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        if address in ipaddress.ip_network('100.64.0.0/10') and value != '100.64.0.0':
            reasons.append('individual Tailscale address')
    if Path(name).name in {'secret', 'agent2.json', 'config.json'} or '.bak' in name or name.endswith('.pyc'):
        reasons.append('private state or generated backup')
    return sorted(set(reasons))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', action='store_true', help='scan all reachable Git blobs too')
    args = parser.parse_args()
    errors, count = [], 0
    for p in sorted(ROOT.rglob('*')):
        if not p.is_file() or any(part in SKIP for part in p.relative_to(ROOT).parts):
            continue
        name = p.relative_to(ROOT).as_posix()
        count += 1
        for reason in problems(name, p.read_bytes()):
            errors.append(f'{name}: {reason}')
    if args.history:
        output = subprocess.check_output(['git', 'rev-list', '--objects', '--all'], cwd=ROOT, text=True)
        for row in output.splitlines():
            oid, _, name = row.partition(' ')
            kind = subprocess.check_output(['git', 'cat-file', '-t', oid], cwd=ROOT, text=True).strip()
            if kind != 'blob':
                continue
            count += 1
            for reason in problems(name, subprocess.check_output(['git', 'cat-file', 'blob', oid], cwd=ROOT)):
                errors.append(f'git:{oid[:12]}:{name}: {reason}')
    for error in errors:
        print(error)
    print(f'XDEV_PUBLIC_SCAN files_and_blobs={count} findings={len(errors)}')
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
