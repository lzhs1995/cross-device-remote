#!/usr/bin/env python3
"""Install the shared skill without changing private config or running services."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

SOURCE = Path(__file__).resolve().parent.parent
AGENTS = {
    'codex': '.codex/skills',
    'claude': '.claude/skills',
    'gemini': '.gemini/skills',
    'antigravity': '.gemini/antigravity/skills',
}


def install(home, agents, replace=False, check=False):
    home = home.expanduser().resolve()
    shared = home / '.agents/skills/cross-device-remote'
    links = [(shared, SOURCE)] + [(home / AGENTS[a] / 'cross-device-remote', shared) for a in agents]
    # 先檢查全部目的地，避免裝到一半才發現衝突。
    conflicts = [str(p) for p, target in links
                 if os.path.lexists(p) and p.resolve() != target.resolve()]
    if check:
        rows = [{'path': str(p), 'matches': p.is_dir() and p.resolve() == target.resolve()} for p, target in links]
        return {'ok': all(r['matches'] for r in rows), 'links': rows}
    if conflicts and not replace:
        raise ValueError('Existing installations preserved. Use --replace for dated backups: ' + ', '.join(conflicts))
    backups = []
    for p, target in links:
        if os.path.lexists(p) and p.resolve() == target.resolve():
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(p):
            backup = p.with_name(p.name + '.backup-' + time.strftime('%Y%m%dT%H%M%S') + '-' + str(time.time_ns()))
            p.rename(backup)
            backups.append(str(backup))
        p.symlink_to(target, target_is_directory=True)
    config = home / '.config/cross-device-remote/config.json'
    created = False
    if not os.path.lexists(config):
        config.parent.mkdir(parents=True, exist_ok=True)
        # 獨占建立，保留任何既有的私有機器設定。
        with config.open('x', encoding='utf-8') as f:
            cfg = json.loads((SOURCE / 'config.example.json').read_text())
            cfg['state_dir'] = str(home / '.local/state/cross-device-remote')
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write('\n')
        config.chmod(0o600)
        created = True
    return {'ok': True, 'source': str(SOURCE), 'shared_skill': str(shared),
            'agents': agents, 'backups': backups, 'config_created': created,
            'services_started': False, 'note': 'Configure and pair Windows before executing remote commands.'}


def main():
    # Windows 管道默认编码可能不是 UTF-8；JSON 回执仍可可靠表达中文路径。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--agents', nargs='+', choices=AGENTS, default=list(AGENTS))
    p.add_argument('--prefix', type=Path, default=Path.home(), help='user home; custom path for isolated installations')
    p.add_argument('--replace', action='store_true', help='back up conflicting entries; never delete them')
    p.add_argument('--check', action='store_true', help='read-only link check')
    a = p.parse_args()
    try:
        result = install(a.prefix, list(dict.fromkeys(a.agents)), a.replace, a.check)
    except (ValueError, OSError) as e:
        p.exit(2, 'XDEV_INSTALL_REFUSED ' + str(e) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
