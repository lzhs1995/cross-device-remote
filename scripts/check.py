#!/usr/bin/env python3
"""Bounded offline checks; no real profile, queue, Windows host or Mplus."""
import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent


def main():
    for path in ROOT.rglob('*.py'):
        if '.git' not in path.parts:
            ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    with tempfile.TemporaryDirectory(prefix='xdev-check-') as tmp:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        commands = [[sys.executable, '-B', str(ROOT / 'scripts' / name)] for name in
                    ['test_advertise.py', 'test_clauder.py', 'test_health.py', 'test_init.py', 'test_install.py']]
        commands.append([sys.executable, '-B', str(ROOT / 'scripts/test_result_read.py'),
                         str(ROOT / 'scripts/xdev_core.py'), str(Path(tmp) / 'receipt.json')])
        commands.append([sys.executable, '-B', str(ROOT / 'scripts/check_public.py')])
        if os.name == 'nt':
            commands.append([sys.executable, '-B', str(ROOT / 'scripts/test_windows_worker.py')])
        for command in commands:
            print('CHECK', Path(command[2]).name, flush=True)
            subprocess.run(command, cwd=ROOT, env=env, check=True, timeout=240)
    print('XDEV_OFFLINE_CHECK_PASS')


if __name__ == '__main__':
    main()
