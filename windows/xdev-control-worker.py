#!/usr/bin/env python3
"""Explicit Windows control/1 launcher; no install, service, queue or cursor changes.

Run with the approved native Python 3.14 executable. Arguments are the worker
arguments of scripts/xdev_control.py (--url, --state, --secret-file, --channel-id,
and optionally --max-workers, --seconds, --poll). Keep this source tree intact.
"""
from pathlib import Path
import runpy
import sys


if __name__ == "__main__":
    sys.argv = [str(Path(__file__).resolve().parents[1] / "scripts" / "xdev_control.py"), "worker", *sys.argv[1:]]
    runpy.run_path(sys.argv[0], run_name="__main__")
