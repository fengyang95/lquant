"""L3 鲁棒性检验回归：窗口扰动 / 分段稳定 / 起点敏感 / 剔除最佳月份 / OOS 衰减。

测试思路：用「已知信号强度」的合成面板造出确定性结论 ——
稳定信号必须通过、分段反向必须被抓到、alpha 集中在单月必须被判为 fragile。
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
import polars as pl
import pytest

from lquant.factors.evaluate.robustness import (
    best_month_removal,
    oos_decay,
    param_sensitivity,
    perturbed_expressions,
    robustness_summary,
    start_date_sensitivity,
    time_stability,
)

DAY0 = dt.date(2025, 1, 1)


def _signal_panel(n_days: int = 60, n_sym: int = 10, *, sign_first: float = 1.0,
                  sign_second: float = 1.0, boost: float = 0.001, seed: int = 7,
                  signal_days: int | None = None) -> pl.DataFrame:
    """f 与 fwd_ret_1 截面同向的面板：IC 符号由 sign_* 决定。

    ``signal_days`` 给定后，只有前这么多天的收益非零（其余恒为 0）——
    用来构造「alpha 全集中在头几个月」的样本。
    """
    rng = np.random.default_rng(seed)
    half = n_days // 2
    rows = []
    for d in range(n_days):
        sign = sign_first if d < half else sign_second
        active = signal_days is None or d < signal_days
        for i in range(n_sym):
            f = float(i) - (n_sym - 1) / 2
            ret = (sign * f * boost + rng.normal(0, boost * 0.5)) if active else 0.0
            rows.append({"trade_date": DAY0 + dt.timedelta(days=d),
                         "symbol": f"S{i:02d}", "f": f, "fwd_ret_1": ret})
    return pl.DataFrame(rows)


def _price_panel(n_days: int = 80, n_sym: int = 12, *, seed: int = 3) -> pl.DataFrame:
    """带 close 的面板 —— 参数敏感性要重算表达式，必须有原始字段。"""
    rng = np.random.default_rng(seed)
    px = {f"S{i:02d}": 10.0 + i for i in range(n_sym)}
    rows = []
    for d in range(n_days):
        for i in range(n_sym):
            s = f"S{i:02d}"
            px[s] *= (1 + (i - (n_sym - 1) / 2) * 0.001 + rng.normal(0, 0.0005))
            rows.append({"trade_date": DAY0 + dt.timedelta(days=d), "symbol": s,
                         "close": px[s], "open": px[s], "high": px[s] * 1.005,
                         "low": px[s] * 0.995, "volume": 1e5, "amount": px[s] * 1e4})
    return pl.DataFrame(rows)


# ────────────────────────── 表达式扰动 ──────────────────────────

def test_perturbed_expressions_only_touches_windows():
    """只看窗口参数：`-1` 那种语义常量不能被当成可调参数。"""
    out = perturbed_expressions("Rank(Ts_Mean($close,5)/$close-1)")
    assert out, "应当至少产出一条扰动"
    assert all("$close,6)" in o["expr"] or "$close,4)" in o["expr"] for o in out)
    # 常量 1 不能被扰动成 0.7 / 1.3
    assert not any("/$close-1.3" in o["expr"] or "/$close-0.7" in o["expr"] for o in out)
    # 同一窗口上下两侧，且去重（5 的 ±10% 与 ±20% 都落在 4/6）
    assert {o["perturbed"] for o in out} == {4, 6}
    assert len(out) == 2


def test_perturbed_expressions_dedup_multi_window():
    out = perturbed_expressions("Ts_Corr(Ts_Return($close,20),Ts_Std($volume,10),20)")
    exprs = [o["expr"] for o in out]
    assert len(exprs) == len(set(exprs)), "扰动表达式不能重复"
    # 三个窗口各自独立扰动，每条只改一个数字
    assert any("$close,22)" in e for e in exprs)
    assert any("$volume,11)" in e for e in exprs)


def test_param_sensitivity_reports_structure():
    df = _price_panel()
    res = param_sensitivity(df, "Ts_Mean($close,5)/$close-1")
    assert res["baseline"]["n_days"] > 0
    assert res["perturbations"], "应有扰动记录"
    assert math.isfinite(res["max_rel_change"])
    assert res["max_rel_change"] >= 0
    for r in res["perturbations"]:
        assert "expr" in r and "delta" in r and "icir" in r


def test_param_sensitivity_skips_when_no_window():
    """无窗口的表达式归为 insufficient（跳过），不算通过也不算失败。"""
    df = _price_panel()
    res = param_sensitivity(df, "Rank(Log($close))")
    assert res["passed"] is False
    assert res["insufficient"] is True
    assert res["perturbations"] == []
    assert "跳过" in res["hint"]


# ────────────────────────── 分段稳定性 ──────────────────────────

def test_time_stability_passes_for_persistent_signal():
    res = time_stability(_signal_panel(), "f", "fwd_ret_1")
    assert res["passed"] is True
    assert res["sign_consistent"] is True
    assert res["icir_ratio"] > 0.5


def test_time_stability_catches_regime_flip():
    """前半段正 IC、后半段负 IC —— 平均值可能还行，但它就是靠不住。"""
    res = time_stability(_signal_panel(sign_first=1.0, sign_second=-1.0), "f", "fwd_ret_1")
    assert res["passed"] is False
    assert res["sign_consistent"] is False
    assert len(res["splits"]) == 2


def test_time_stability_requires_two_splits():
    with pytest.raises(ValueError, match="n_splits"):
        time_stability(_signal_panel(), "f", "fwd_ret_1", n_splits=1)


def test_time_stability_short_sample_is_not_a_pass():
    res = time_stability(_signal_panel(n_days=3), "f", "fwd_ret_1")
    assert res["passed"] is False
    assert "交易日不足" in res["hint"]


# ────────────────────────── 起点敏感性 ──────────────────────────

def test_start_date_sensitivity_stable_signal():
    res = start_date_sensitivity(_signal_panel(n_days=120), "f", "fwd_ret_1", n_starts=4)
    assert res["passed"] is True
    # 判定用变异系数而非绝对标准差：ICIR 量级大时绝对标准差会误杀
    assert res["icir_cv"] < res["threshold"]
    assert res["icir_std"] > res["threshold"]
    assert len(res["runs"]) == 4


def test_start_date_sensitivity_needs_days():
    res = start_date_sensitivity(_signal_panel(n_days=12), "f", "fwd_ret_1", n_starts=5)
    assert res["passed"] is False
    assert "交易日不足" in res["hint"]


# ────────────────────────── 剔除最佳月份 ──────────────────────────

def test_best_month_removal_flags_single_month_alpha():
    """信号只存在于第一个月 —— 剔掉最好的几个月后什么都不剩。"""
    df = _signal_panel(n_days=220, signal_days=31)
    res = best_month_removal(df, "f", "fwd_ret_1", top_n=3, n_groups=5)
    assert res["n_months"] == 8
    assert res["total_ret"] > 0
    assert res["passed"] is False
    assert res["remaining_ret"] == pytest.approx(0.0, abs=1e-9)


def test_best_month_removal_survives_spread_alpha():
    df = _signal_panel(n_days=220)
    res = best_month_removal(df, "f", "fwd_ret_1", top_n=3, n_groups=5)
    assert res["passed"] is True
    assert res["remaining_ret"] > 0
    assert len(res["dropped_months"]) == 3


def test_best_month_removal_insufficient_when_too_few_months():
    """4 个月却要剔掉 5 个 —— 剔完什么都不剩，这个结论没有信息量。"""
    res = best_month_removal(_signal_panel(n_days=120), "f", "fwd_ret_1",
                             top_n=5, n_groups=5)
    assert res["insufficient"] is True
    assert res["passed"] is False
    assert "月份数不足" in res["hint"]


def test_best_month_removal_rejects_non_date_column():
    df = _signal_panel(n_days=40).with_columns(pl.col("trade_date").cast(pl.Utf8))
    res = best_month_removal(df, "f", "fwd_ret_1", n_groups=5)
    assert res["passed"] is False
    assert "按月聚合" in res["hint"]


# ────────────────────────── OOS 衰减 ──────────────────────────

def test_oos_decay_thresholds():
    assert oos_decay(0.5, 0.5)["passed"] is True
    assert oos_decay(0.5, 0.5)["decay"] == pytest.approx(0.0)
    mid = oos_decay(0.5, 0.3)
    assert mid["decay"] == pytest.approx(0.4)
    assert mid["passed"] is True
    bad = oos_decay(0.5, 0.2)
    assert bad["decay"] == pytest.approx(0.6)
    assert bad["passed"] is False


def test_oos_decay_handles_negative_and_invalid():
    # 方向翻转也算衰减：|ICIR| 从 0.5 掉到 -0.2 → 衰减 140%
    assert oos_decay(0.5, -0.2)["passed"] is False
    assert not math.isfinite(oos_decay(0.0, 0.2)["decay"])
    assert oos_decay(float("nan"), 0.2)["passed"] is False


def _panel_with_expr_fields(n_days: int = 120) -> pl.DataFrame:
    """带 close 的因子面板 —— 参数敏感性要重算表达式，必须有原始字段。"""
    return _price_panel(n_days=n_days).with_columns(
        pl.col("close").rank().over("trade_date").alias("f"),
        (pl.col("close").shift(-1).over("symbol") / pl.col("close") - 1).alias("fwd_ret_1"),
    )


# ────────────────────────── 汇总 ──────────────────────────

def test_robustness_summary_counts_skipped_separately():
    """样本够长时四项都判；没给 expr/IS-OOS 的两项记为 skipped，分母不含它们。"""
    df = _signal_panel(n_days=220)
    res = robustness_summary(df, "f", "fwd_ret_1", top_n=3)
    names = {c["name"]: c["status"] for c in res["checks"]}
    assert names["param_sensitivity"] == "skipped"     # 没给 expr
    assert names["oos_decay"] == "skipped"             # 没给 IS/OOS ICIR
    assert names["best_month_removal"] == "passed"
    assert res["n_judged"] == 3
    assert res["verdict"] == "robust"


def test_robustness_summary_unknown_when_nothing_judgeable():
    """样本太短 → 一项都判不了，不能硬报 fragile。"""
    res = robustness_summary(_signal_panel(n_days=3), "f", "fwd_ret_1",
                             n_splits=2, n_starts=5, top_n=5, n_groups=5)
    assert res["n_judged"] == 0
    assert res["verdict"] == "unknown"


def test_robustness_summary_judges_param_sensitivity_with_expr():
    res = robustness_summary(_panel_with_expr_fields(n_days=220), "f", "fwd_ret_1",
                             expr="Ts_Mean($close,5)/$close-1", top_n=3,
                             icir_is=0.5, icir_oos=0.5)
    assert res["n_judged"] == 5
    assert {c["name"] for c in res["checks"]} == {
        "param_sensitivity", "time_stability", "start_date_sensitivity",
        "best_month_removal", "oos_decay"}


def test_robustness_summary_is_fragile_on_regime_flip():
    df = _signal_panel(n_days=220, sign_first=1.0, sign_second=-1.0)
    res = robustness_summary(df, "f", "fwd_ret_1", top_n=3, icir_is=0.6, icir_oos=0.5)
    assert res["n_judged"] == 4
    assert res["n_passed"] < 4
    assert res["verdict"] == "fragile"
    assert all("detail" in c for c in res["checks"] if c["status"] != "skipped")
