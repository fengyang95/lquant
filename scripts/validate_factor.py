#!/usr/bin/env python3
"""因子结果正确性验证入口（docs/FACTOR_VALIDATION.md F1-F6 + N1-N6）。

用法: uv run python scripts/validate_factor.py
逐层跑 pytest 子集，打印 PASS/FAIL，任一失败退出 1。

N 层用**显式 node id** 而不是 `-k` 子串匹配：改名即报错，不会静默跑空。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_FILE = "tests/unit/test_factor_accuracy.py"
GOLDEN = "tests/unit/test_factor_golden.py"
COV_FILE = "tests/unit/test_covariates.py"

LAYERS = [
    ("F1", [GOLDEN]),
    ("F2", [TEST_FILE, "-k", "f2"]),
    ("F3", [TEST_FILE, "-k", "f3"]),
    ("F4", [TEST_FILE, "-k", "f4"]),
    ("F5", [TEST_FILE, "-k", "f5"]),
    ("F6", [TEST_FILE, "-k", "f6"]),
    # N1-N6 中性化专项（M2.5）：每一项一条断言，见 docs/FACTOR_VALIDATION.md
    ("N1", [f"{COV_FILE}::test_n1_winsorize_mad_handcalc"]),
    ("N2", [f"{COV_FILE}::test_n2_self_neutralize_zero"]),
    ("N3", [f"{COV_FILE}::test_n3_industry_mean_equals_within_group_demean",
            f"{COV_FILE}::test_industry_pit_asof"]),
    ("N4", [f"{COV_FILE}::test_n4_residual_orthogonal"]),
    ("N5", [f"{COV_FILE}::test_market_cap_is_log"]),
    ("N6", [f"{COV_FILE}::test_n6_all_covariates_missing_raises"]),
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
