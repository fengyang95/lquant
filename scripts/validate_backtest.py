#!/usr/bin/env python
"""回测准确性一键体检（docs/BACKTEST_VALIDATION.md）。

用法:
    .venv/bin/python scripts/validate_backtest.py

依次执行 L1~L4 全部自动化验证测试并输出清单报告。
L5（交叉引擎对照）随 adapter 接入第三方引擎后自动纳入；
L6（模拟盘对账）随 paper trading 实跑后自动纳入。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_FILE = ROOT / "tests" / "unit" / "test_backtest_accuracy.py"

CHECKS = [
    ("L1", "金标准手算 · 净值/成交/持仓逐项对照", "test_golden_buy_hold_hand_computed"),
    ("L1", "金标准手算 · 指标全链路", "test_golden_metrics_hand_formula"),
    ("L2", "会计恒等式 · 零费率资金守恒", "test_cash_conservation_zero_fee"),
    ("L2", "会计恒等式 · 真实费率资金守恒", "test_cash_conservation_with_fees"),
    ("L3", "性质测试 · 无未来函数（截断不变性）", "test_no_lookahead_truncation_invariance"),
    ("L3", "性质测试 · T+N 可卖约束", "test_t_plus_n_sell_constraint"),
    ("L3", "性质测试 · 涨跌停拒单", "test_limit_up_buy_rejected"),
    ("L3", "性质测试 · 滑点单调性", "test_slippage_monotonicity"),
    ("L3", "性质测试 · 成交时点（next_open/close）", "test_next_open_fill_price_and_delay"),
    ("L4", "指标交叉核对 · 年化/回撤/夏普独立重算", "test_metrics_cross_check_on_random_run"),
]

PENDING = [
    ("L5", "交叉引擎对照 · adapter 接第三方引擎（无摩擦 NAV 一致性）", "待接入 vectorbt 后启用"),
    ("L6", "模拟盘对账 · 回测 vs paper 日收益偏差", "待模拟盘实跑后启用"),
]


def main() -> int:
    print("=" * 64)
    print("回测准确性体检  docs/BACKTEST_VALIDATION.md")
    print("=" * 64)

    for node_id in [c[2] for c in CHECKS]:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", f"{TEST_FILE}::{node_id}", "-q", "--no-header"],
            capture_output=True, text=True, cwd=ROOT)
        ok = r.returncode == 0
        layer, desc, _ = next(c for c in CHECKS if c[2] == node_id)
        mark = "\033[32m✔\033[0m" if ok else "\033[31m✘\033[0m"
        print(f"{mark} [{layer}] {desc}")
        if not ok:
            tail = (r.stdout or r.stderr).strip().splitlines()[-8:]
            print("\n".join("      " + line for line in tail))

    print("-" * 64)
    for layer, desc, note in PENDING:
        print(f"\033[33m◌\033[0m [{layer}] {desc}  ({note})")

    print("-" * 64)
    print("结论: ✔ 通过项为已验证事实；✘ 项必须先修复再信任任何回测数字。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
