"""因子分析能力审计修复的回归测试。

对应 `docs/因子分析能力完备性审计.md`：

- A1 报告「归因分解」不再静默回退到 symbol（无分类维度时整节不出现）
- A2 ``synthesize(ic_weighted)`` 用日均 RankIC，不是全样本池化相关
- B1 ``evaluate()`` 转发 ``group_col`` / ``bps_list``（分组 IC / 成本敏感性）
- B2 ``evaluate()`` 带评级；``with_robustness=True`` 时带 L3 稳健性
- B3 ``neutral_views`` 交付行业内分组的**真实结果**（不是 True 标记）
- D3 ``ic_decay_table`` / ``cost_adjusted_nav`` / ``np_cumprod`` 已移除
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from lquant.factors.analysis import _mean_rank_ic, synthesize
from lquant.factors.evaluate import evaluate
from lquant.factors.evaluate.neutral_views import (
    industry_group_quantile_summary,
    neutral_views,
)
from lquant.factors.evaluate.report import factor_report
from lquant.factors.evaluate.returns import forward_return

_INDUSTRIES = ["银行", "医药", "钢铁"]


def _panel(n_days: int = 40, n_sym: int = 18, *, industry: bool = True,
           seed: int = 3) -> pl.DataFrame:
    """合成面板：因子与次日收益弱相关，可选带 industry_sw1 列。"""
    rng = np.random.default_rng(seed)
    rows = []
    for s in range(n_sym):
        px = 10.0 + s
        for i in range(n_days):
            f = rng.normal()
            px *= 1.0 + 0.02 * f + rng.normal(0, 0.005)
            rows.append({
                "symbol": f"S{s:02d}",
                "trade_date": dt.date(2024, 1, 1) + dt.timedelta(days=i),
                "close": px,
                "amount": 1e7 * (s + 1),
                "f": f,
            })
    df = pl.DataFrame(rows)
    df = forward_return(df, "close", periods=[1, 5])
    if industry:
        meta = pl.DataFrame({
            "symbol": [f"S{s:02d}" for s in range(n_sym)],
            "industry_sw1": [_INDUSTRIES[s % len(_INDUSTRIES)] for s in range(n_sym)],
        })
        df = df.join(meta, on="symbol", how="left")
    return df


def _panel_with_cov_industry(n_days: int = 40, n_sym: int = 18) -> pl.DataFrame:
    """带 ``cov_industry_sw1`` 的面板 —— API 路径下行业的实际列名。"""
    df = _panel(n_days, n_sym, industry=False)
    meta = pl.DataFrame({
        "symbol": [f"S{s:02d}" for s in range(n_sym)],
        "cov_industry_sw1": [_INDUSTRIES[s % len(_INDUSTRIES)] for s in range(n_sym)],
    })
    return df.join(meta, on="symbol", how="left")


# ───────────────────────────── A1 报告归因 ─────────────────────────────

def test_report_attribution_absent_when_no_category_dimension() -> None:
    """没有任何分类维度时，「归因分解」整节不出现 —— 绝不拿 symbol 顶替。"""
    html = factor_report(_panel(industry=False), "f", "fwd_ret_1")
    assert "<html" in html
    assert "归因分解" not in html
    assert "行业暴露" not in html


def test_report_attribution_never_uses_symbol_fallback() -> None:
    """回归：曾因 `cat_col or ... else "symbol"` 让 242/242 份报告按个股归因。"""
    df = _panel(industry=False)          # 有 symbol 但无行业列
    assert "symbol" in df.columns
    html = factor_report(df, "f", "fwd_ret_1")
    assert "归因分解 · symbol" not in html
    assert "多空行业暴露总和" not in html


def test_report_attribution_prefers_cov_industry_column() -> None:
    """API 路径：行业以 cov_industry_sw1 出现时应当被用上。"""
    html = factor_report(_panel_with_cov_industry(), "f", "fwd_ret_1")
    assert "归因分解 · cov_industry_sw1" in html


# ───────────────────────── A2 synthesize 口径 ─────────────────────────

def _uneven_panel() -> pl.DataFrame:
    """逐日截面规模不等 —— 池化相关与日均相关必然分叉。"""
    rng = np.random.default_rng(0)
    rows = []
    for d in range(6):
        n = 20 if d % 2 == 0 else 60
        for i in range(n):
            f = rng.normal()
            rows.append({"trade_date": f"2024-01-{d + 1:02d}", "symbol": f"s{i:03d}",
                         "close": 10.0, "f": f,
                         "fwd_ret_1": 0.3 * f + rng.normal()})
    return pl.DataFrame(rows)


def test_mean_rank_ic_is_daily_mean_not_pooled() -> None:
    """日均 RankIC 必须等于「逐日算完再平均」，而不是全样本池化。"""
    df = _uneven_panel()
    got = _mean_rank_ic(df, "f", "fwd_ret_1")
    ranked = df.with_columns([pl.col("f").rank().over("trade_date"),
                              pl.col("fwd_ret_1").rank().over("trade_date")])
    daily = float(
        ranked.group_by("trade_date")
        .agg([pl.len().alias("n"), pl.corr("f", "fwd_ret_1").alias("ic")])
        .filter(pl.col("n") >= 5)["ic"].mean()
    )
    pooled = float(ranked.select(pl.corr("f", "fwd_ret_1"))[0, 0])
    assert got == pytest.approx(daily, rel=1e-9)
    assert abs(got - pooled) > 1e-6        # 两者确实不同，否则这条测试没意义


def test_synthesize_ic_weighted_runs_and_is_finite() -> None:
    """ic_weighted 合成结果必须是有限值（历史上权重可能算出 NaN）。"""
    df = _panel(industry=False)
    out = synthesize(df, ["f"], method="ic_weighted", ic_horizon=1)
    assert "_syn" in out.columns
    vals = out["_syn"].drop_nulls().to_numpy()
    assert len(vals) > 0
    assert np.isfinite(vals).all()


# ──────────────────── B1 evaluate() 参数转发 ────────────────────

def test_evaluate_forwards_group_col_and_bps_list() -> None:
    """`evaluate()` 曾静默丢弃 group_col / bps_list → 报告两节从未产出。"""
    res = evaluate(_panel(), "f", n_groups=6, with_report=True,
                   group_col="industry_sw1", bps_list=[0.0, 10.0])
    html = res["report"]
    assert "分组 IC" in html
    assert "成本敏感性" in html


def test_evaluate_report_has_no_symbol_attribution() -> None:
    """端到端：evaluate() 产出的报告不得出现按个股的「归因分解」。"""
    res = evaluate(_panel(industry=False), "f", n_groups=6, with_report=True)
    assert "归因分解" not in res["report"]


# ──────────────────── B2 评级 / 稳健性 ────────────────────

def test_evaluate_includes_rating() -> None:
    res = evaluate(_panel(), "f", with_report=False)
    assert res["rating"]["rating"] in ("weak", "moderate", "strong")
    assert "blockers" in res["rating"]


def test_evaluate_robustness_is_opt_in() -> None:
    """默认不跑稳健性（贵）；显式开启后才出现。"""
    plain = evaluate(_panel(), "f", with_report=False)
    assert "robustness" not in plain

    res = evaluate(_panel(), "f", with_report=False, with_robustness=True,
                   expr="Ts_Mean($close,5)", covs=[])
    rob = res["robustness"]
    assert rob["verdict"] in ("robust", "fragile", "unknown")
    names = {c["name"] for c in rob["checks"]}
    assert {"time_stability", "start_date_sensitivity", "best_month_removal"} <= names


# ──────────────────── B3 行业内分组 ────────────────────

def test_neutral_views_industry_group_quantile_returns_numbers() -> None:
    """第三种中性化视图必须给出数字，而不是一个 True 标记。"""
    df = _panel_with_cov_industry()
    v = neutral_views(df, "f", "fwd_ret_1",
                      covariates=None, group_col="cov_industry_sw1", n_groups=3)
    igq = v["industry_group_quantile"]
    assert isinstance(igq, dict)
    assert igq["insufficient"] is False
    assert igq["groups"], "行业内分组至少应产出一个组"
    assert igq["top_bottom_spread"] is not None
    assert igq["n_groups"] == 3


def test_industry_group_quantile_summary_missing_column_is_insufficient() -> None:
    out = industry_group_quantile_summary(_panel(industry=False), "f", "fwd_ret_1",
                                          "nope", 3)
    assert out["insufficient"] is True
    assert out["groups"] == []


# ──────────────────── D3 死代码已移除 ────────────────────

def test_dead_helpers_removed() -> None:
    from lquant.factors.evaluate import costs, excess, ic, quantile

    for name in ("ic_decay_table",):
        assert not hasattr(ic, name), f"{name} 应已删除（decay_profile 取代）"
    assert not hasattr(costs, "cost_adjusted_nav")
    assert not hasattr(quantile, "np_cumprod")
    assert not hasattr(excess, "np_cumprod")
