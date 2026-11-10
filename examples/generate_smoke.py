#!/usr/bin/env python3
"""Reproduce the bundled synthetic Mplus input; uses no external data."""
from pathlib import Path
import random


def content():
    rng = random.Random(417)
    rows = []
    for _ in range(300):
        x1, x2, error = (rng.gauss(0, 1) for _ in range(3))
        y = 1.5 + 0.8 * x1 - 0.3 * x2 + error
        rows.append(f'{y:.6f} {x1:.6f} {x2:.6f}\n')
    return ''.join(rows)


if __name__ == '__main__':
    path = Path(__file__).resolve().with_name('smoke_mplus.dat')
    path.write_text(content(), encoding='ascii')
    print('Synthetic data: 300 rows, y x1 x2, seed 417')
