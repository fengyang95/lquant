#!/usr/bin/env python3
"""因子结果正确性验证入口（docs/FACTOR_VALIDATION.md F1-F4）。

用法: uv run python scripts/validate_factor.py
逐层跑 pytest 子集，打印 PASS/FAIL，任一失败退出 1。
"""
from __future__ import annotations

from pathlib import Path

import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TEST_FILE = "tests/unit/test_factor_accuracy.py"
GOLDEN = "tests/unit/test_factor_golden.py"

LAYERS = [
    ("F1", [GOLDEN]),
    ("F2", [TEST_FILE, "-k", "f2"]),
    ("F3", [TEST_FILE, "-k", "f3"]),
    ("F4", [TEST_FILE, "-k", "f4"]),
]

if __name__ == "__main__":
    failed = []
    for layer, args in LAYERS:
        cmd = [sys.executable, "-m", "pytest", *args, "-q", "--no-header"]
        r = subprocess.run(cmd, check=False, cwd=ROOT)
        status = "PASS" if r.returncode == 0 else "FAIL"
        print(f"\n== {layer} {status} ==")
        if r.returncode != 0:
            failed.append(layer)
    print("=" * 40)
    print("ALL PASS" if not failed else f"FAILED: {failed}")
    sys.exit(1 if failed else 0)
