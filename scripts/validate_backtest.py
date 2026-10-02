#!/usr/bin/env python
"""回测准确性一键体检（docs/BACKTEST_VALIDATION.md）。

用法:
    .venv/bin/python scripts/validate_backtest.py

逐项执行 L1~L6 的自动化验证测试并输出清单报告；任一项失败则退出码非 0，
可直接用作 CI/发布前的门禁。

L5/L6 的落地位置见 docs/BACKTEST_VALIDATION.md：
- L5 用内置的零依赖对照引擎 `frictionless_buy_hold`（adapter 协议）做无摩擦一致性；
- L6 用「回测 vs 模拟盘」同一笔成交的对账。
第三方引擎（vectorbt/RQAlpha）与「回测 vs 实盘」仍属后续项，见文件末尾注记。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNIT = Path("tests/unit")
ACC = f"{UNIT}/test_backtest_accuracy.py"
FIX = f"{UNIT}/test_backtest_defect_fixes.py"

# (层, 说明, 测试文件, 用例名)
CHECKS = [
    ("L1", "金标准手算 · 净值/成交/持仓逐项对照", ACC, "test_golden_buy_hold_hand_computed"),
    ("L1", "金标准手算 · 指标全链路", ACC, "test_golden_metrics_hand_formula"),
    ("L2", "会计恒等式 · 零费率资金守恒", ACC, "test_cash_conservation_zero_fee"),
    ("L2", "会计恒等式 · 真实费率资金守恒", ACC, "test_cash_conservation_with_fees"),
    ("L3", "性质测试 · 无未来函数（截断不变性）", ACC, "test_no_lookahead_truncation_invariance"),
    ("L3", "性质测试 · T+N 可卖约束", ACC, "test_t_plus_n_sell_constraint"),
    ("L3", "性质测试 · 涨跌停拒单", ACC, "test_limit_up_buy_rejected"),
    ("L3", "性质测试 · 滑点单调性", ACC, "test_slippage_monotonicity"),
    ("L3", "性质测试 · 成交时点（next_open/close）", ACC, "test_next_open_fill_price_and_delay"),
    ("L4", "指标交叉核对 · 年化/回撤/夏普独立重算", ACC, "test_metrics_cross_check_on_random_run"),
    # 2026-10-02 复审新增：静默算错类不变量（缺陷回归锁）
    ("L2", "涨跌停价按 tick 取整（3.63→3.99 拒单）", FIX,
     "test_limit_up_price_is_tick_rounded_and_rejects_fill"),
    ("L2", "印花税历史区间 + 买卖方向（2008-09-19 起单边）", FIX,
     "test_stamp_duty_history_covers_both_sides_and_2023_cut"),
    ("L3", "除权日新建仓不欠配（挂单同比例调整）", FIX,
     "test_ex_dividend_entry_not_undersized"),
    ("L3", "除权日净值连续（JQ 路径与 Engine 一致）", FIX,
     "test_jq_path_engine_path_agree_on_split_nav"),
    ("L3", "逐日 ST 涨跌幅（按当日戴帽状态，非全期恒定）", FIX,
     "test_is_st_flip_mid_backtest_changes_limit"),
    ("L3", "退市核销（不按最后收盘价永久冻结）", FIX,
     "test_delisted_position_is_written_off_not_frozen"),
    ("L3", "零股清仓可全卖（A 股零股须一次性卖出）", FIX,
     "test_full_liquidation_sells_odd_lot"),
    ("L3", "T+N 按交易日（周五买入 T+2 周一不可卖）", FIX,
     "test_t_plus_n_uses_trading_days_not_calendar_days"),
    ("L3", "限价单语义（限价未触及不成交 / 成交价不劣于限价）", FIX,
     "test_limit_order_fills_at_limit_or_better"),
    # L5 交叉引擎对照
    ("L5", "交叉引擎 · 无摩擦 NAV 与独立实现逐日一致", FIX,
     "test_l5_frictionless_nav_matches_independent_reference"),
    ("L5", "交叉引擎 · 摩擦成本为正且随费率单调", FIX,
     "test_l5_friction_cost_is_positive_and_monotonic_in_rate"),
    ("L5", "交叉引擎 · AdapterOutput 与 Engine.run 同形", FIX,
     "test_l5_adapter_output_matches_engine_contract"),
    # L6 模拟盘对账
    ("L6", "模拟盘对账 · 同笔成交的持仓/费用/净值序列一致", FIX,
     "test_paper_vs_backtest_reconciliation"),
    ("L6", "模拟盘对账 · T+N 交易日 + 整手口径对齐", FIX,
     "test_paper_t_plus_n_by_trading_days_and_lot_size"),
    ("L6", "模拟盘对账 · 公司行为份额调整", FIX,
     "test_paper_applies_corporate_action_share_adjustment"),
]

# 仍属后续项（明确标注，绝不混进「通过」里）
PENDING = [
    ("L5", "接入第三方引擎（vectorbt/RQAlpha）做独立对照",
     "需放开 pip 依赖；协议与内置对照引擎已就绪"),
    ("L6", "回测 vs **实盘**对账（成交明细方向/数量一致性）",
     "需真实委托流水"),
    ("—", "backtrader 对账重跑（baseline/momentum_rotation/grid_trading）",
     "510300.SH/159915.SZ 缺 2024 起日线，见 docs/BACKTEST_VALIDATION.md"),
]


def main() -> int:
    print("=" * 68)
    print("回测准确性体检  docs/BACKTEST_VALIDATION.md")
    print("=" * 68)

    failed: list[str] = []
    for layer, desc, rel, node in CHECKS:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", f"{rel}::{node}", "-q", "--no-header"],
            capture_output=True, text=True, cwd=ROOT)
        ok = r.returncode == 0
        mark = "\033[32m✔\033[0m" if ok else "\033[31m✘\033[0m"
        print(f"{mark} [{layer}] {desc}")
        if not ok:
            failed.append(f"{layer} {desc}")
            tail = (r.stdout or r.stderr).strip().splitlines()[-8:]
            print("\n".join("      " + line for line in tail))

    print("-" * 68)
    for layer, desc, note in PENDING:
        print(f"\033[33m◌\033[0m [{layer}] {desc}  ({note})")

    print("-" * 68)
    n_ok = len(CHECKS) - len(failed)
    if failed:
        print(f"结论: {n_ok}/{len(CHECKS)} 通过；✘ 项必须先修复再信任任何回测数字：")
        for f in failed:
            print(f"  ✘ {f}")
        return 1
    print(f"结论: {n_ok}/{len(CHECKS)} 全部通过（✔ 为已验证事实；◌ 为明确的后续项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
