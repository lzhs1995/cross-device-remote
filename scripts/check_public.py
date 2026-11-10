#!/usr/bin/env python3
"""Scan publishable files and optionally Git history for private machine material."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent.parent
SKIP = {'.git', '__pycache__', '.venv', '.pytest_cache'}
TEXT = {'.py', '.ps1', '.vbs', '.md', '.json', '.sh', '.yml', '.yaml', '.svg', '.inp', '.dat', '.r', '.txt'}
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(n) for n in
                         ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
TAILSCALE_NETWORK = ipaddress.ip_network('100.64.0.0/10')
IP_LITERAL = re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,3})?(?![A-Za-z0-9_.])')
EMAIL = re.compile(r"(?<![\w.+-])[\w.!#$%&'*+/=?^_`{|}~-]+@"
                   r'(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+'
                   r'[A-Za-z]{2,63}(?![\w.-])')


def private_patterns(terms):
    """具體字串不當成正規式；ASCII 詞界避免短使用者名稱命中較長公開名稱。"""
    patterns = []
    for term in terms:
        left = r'(?<![A-Za-z0-9_])' if re.match(r'[A-Za-z0-9_]', term[0]) else ''
        right = r'(?![A-Za-z0-9_])' if re.match(r'[A-Za-z0-9_]', term[-1]) else ''
        patterns.append(re.compile(left + re.escape(term) + right, re.IGNORECASE))
    return patterns


def load_private_denylist(path=None, required=False, *, environ=None, home=None):
    """只讀外部清單；錯誤訊息不回顯私有路徑、字串或 JSON 內容。"""
    env = os.environ if environ is None else environ
    chosen = path or env.get('XDEV_PRIVATE_DENYLIST')
    explicit = bool(chosen)
    source = Path(chosen).expanduser() if chosen else (
        (Path.home() if home is None else Path(home)) /
        '.config/cross-device-remote/public-denylist.json')
    if not source.exists():
        if explicit or required:
            raise ValueError('private denylist unavailable')
        return []
    try:
        # 發布清單應很小；拒絕誤把資料檔或大型設定載入記憶體。
        if source.stat().st_size > 128 * 1024:
            raise ValueError
        value = json.loads(source.read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError, ValueError):
        raise ValueError('private denylist unreadable or invalid') from None
    terms = value.get('terms') if isinstance(value, dict) else value
    if not isinstance(terms, list) or any(not isinstance(t, str) or not t.strip() for t in terms):
        raise ValueError('private denylist must contain nonempty strings')
    if required and not terms:
        raise ValueError('private denylist empty')
    return private_patterns(list(dict.fromkeys(terms)))


def network_description(literal, network):
    """只豁免明寫且無 host bits 的網段；/32 仍是具體主機。"""
    if '/' not in literal:
        return False
    try:
        described = ipaddress.ip_network(literal, strict=True)
        return described.prefixlen < 32 and described.subnet_of(network)
    except ValueError:
        return False


def public_example_email(address):
    domain = address.rsplit('@', 1)[1].lower()
    return (domain in {'users.noreply.github.com', 'noreply.github.com'} or
            re.fullmatch(r'(?:[a-z0-9-]+\.)*example\.[a-z0-9-]+', domain) is not None)


def problems(name, raw, denylist=()):
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
        'provider token': r'\bsk-[A-Za-z0-9_-]{16,}\b',
        'Slack token': r'\bxox[bp]-[A-Za-z0-9_-]{12,}\b',
        'AWS access key': r'\bAKIA[0-9A-Z]{16}\b',
        'Tailscale token': r'\btskey-(?:auth|api)-[A-Za-z0-9_-]{16,}\b',
        'MAC address': r'(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])'
                       r'|(?<![0-9A-Fa-f-])(?:[0-9A-Fa-f]{2}-){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f-])'
                       r'|(?<![0-9A-Fa-f.])(?:[0-9A-Fa-f]{4}\.){2}[0-9A-Fa-f]{4}(?![0-9A-Fa-f.])',
        'personal research path': r'Documents/c[n]m|PP_LGCM_Remote_' + r'Continuation|tailf[0-9a-f]+',
    }
    # 本檔的正規式本身不是機器資料。
    for kind, pattern in rules.items():
        if re.search(pattern, s):
            reasons.append(kind)
    for match in IP_LITERAL.finditer(s):
        literal = match.group(0)
        value = literal.partition('/')[0]
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        # 保留舊掃描器對裸 Tailscale 網段基址的歷史豁免。
        if (address in TAILSCALE_NETWORK and literal != str(TAILSCALE_NETWORK.network_address)
                and not network_description(literal, TAILSCALE_NETWORK)):
            reasons.append('individual Tailscale address')
        if any(address in net and not network_description(literal, net) for net in PRIVATE_NETWORKS):
            reasons.append('private RFC1918 address')
    if any(not public_example_email(m.group(0)) for m in EMAIL.finditer(s)):
        reasons.append('personal email address')
    for match in re.finditer(r'(?<![A-Za-z0-9_])(?:[0-9a-f]{40}|[0-9a-f]{32})(?![A-Za-z0-9_])', s, re.I):
        context = s[max(0, match.start() - 160):match.end() + 160]
        if re.search(r'\bgist(?:[_-]?(?:id|url))?\b', context, re.I):
            reasons.append('private gist identifier')
            break
    # 白名單只影響通用規則；明確的私有清單仍可拒絕範例郵箱、公開格式字串等。
    if any(pattern.search(s) or pattern.search(name) for pattern in denylist):
        reasons.append('private denylist match')
    if Path(name).name in {'secret', 'agent2.json', 'config.json'} or '.bak' in name or name.endswith('.pyc'):
        reasons.append('private state or generated backup')
    return sorted(set(reasons))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', action='store_true', help='scan all reachable Git blobs too')
    parser.add_argument('--denylist', type=Path,
                        help='private JSON string array or {"terms": [...]}; overrides XDEV_PRIVATE_DENYLIST')
    parser.add_argument('--require-private-denylist', action='store_true',
                        help='require a nonempty private denylist for local publication; CI may omit it')
    args = parser.parse_args(argv)
    try:
        denylist = load_private_denylist(args.denylist, args.require_private_denylist)
    except ValueError as exc:
        print('XDEV_PUBLIC_SCAN_ERROR ' + str(exc))
        return 2
    errors, count = [], 0
    for p in sorted(ROOT.rglob('*')):
        if not p.is_file() or any(part in SKIP for part in p.relative_to(ROOT).parts):
            continue
        name = p.relative_to(ROOT).as_posix()
        count += 1
        for reason in problems(name, p.read_bytes(), denylist):
            # 檔名本身也可能含私有字串，不在公開掃描日誌回顯路徑或命中內容。
            errors.append(f'worktree: {reason}')
    if args.history:
        output = subprocess.check_output(['git', 'rev-list', '--objects', '--all'], cwd=ROOT, text=True)
        for row in output.splitlines():
            oid, _, name = row.partition(' ')
            kind = subprocess.check_output(['git', 'cat-file', '-t', oid], cwd=ROOT, text=True).strip()
            if kind != 'blob':
                continue
            count += 1
            for reason in problems(name, subprocess.check_output(['git', 'cat-file', 'blob', oid], cwd=ROOT), denylist):
                errors.append(f'history: {reason}')
    for error in errors:
        print(error)
    print(f'XDEV_PUBLIC_SCAN files_and_blobs={count} findings={len(errors)}')
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
