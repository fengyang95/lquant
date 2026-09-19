"""因子评价链路缺陷修复回归测试（2026-09-18）。

覆盖：_prep 跨股票泄漏、add_quantile/top_n NaN 防护、ic_series is_finite、
lasso 中性化中心化、group_ic 零方差 IR。
"""
from __future__ import annotations

import math

import polars as pl
import pytest

from lquant.factors.evaluate.group_ic import ic_by_group
from lquant.factors.evaluate.ic import ic_series
from lquant.factors.evaluate.quantile import add_quantile, group_returns
from lquant.factors.evaluate.top_n import top_n_returns
from lquant.factors.preprocess.neutralize import lasso
from lquant.factors.qlib_alpha import _prep


def _panel() -> pl.DataFrame:
    """两股票小面板：B 的首日数据完整（泄漏时 B1 会吃到 A3 的 close/volume）。"""
    return pl.DataFrame({
        "symbol": ["A", "A", "A", "B", "B", "B"],
        "trade_date": [1, 2, 3, 1, 2, 3],
        "open": [100.0, 110.0, 121.0, 10.0, 11.0, 12.0],
        "high": [101.0, 111.0, 122.0, 10.5, 11.5, 12.5],
        "low": [99.0, 109.0, 120.0, 9.5, 10.5, 11.5],
        "close": [100.0, 110.0, 121.0, 10.3, 11.0, 12.0],
        "volume": [1e6, 1.1e6, 1.2e6, 5e5, 5.5e5, 6e5],
        "amount": [1e8, 1.1e8, 1.2e8, 5e7, 5.5e7, 6e7],
    })


def _nan_panel() -> pl.DataFrame:
    """单截面 + 一个 NaN 因子值。"""
    return pl.DataFrame({
        "trade_date": [1, 1, 1, 1, 1, 1],
        "symbol": ["a", "b", "c", "d", "e", "f"],
        "f": [1.0, float("nan"), 2.0, 3.0, 4.0, 5.0],
        "fwd_ret_1": [0.01, 0.02, 0.03, 0.1, 0.2, 0.3],
    })


# ---------------------------------------------------------------- _prep 泄漏


def test_prep_no_cross_symbol_leakage():
    out = _prep(_panel())
    b1 = out.filter((pl.col("symbol") == "B") & (pl.col("trade_date") == 1))
    assert b1["_ret"][0] is None, "B 首日 _ret 不得吃 A 末日 close"
    assert b1["_pc"][0] is None
    assert b1["_pv"][0] is None
    assert b1["_vchg"][0] is None


def test_prep_first_row_of_each_symbol_is_null():
    out = _prep(_panel())
    first = out.filter(pl.col("trade_date") == 1)
    assert first["_ret"].null_count() == 2


# ---------------------------------------------------------------- NaN 防护


def test_add_quantile_nan_row_gets_null_group():
    q = add_quantile(_nan_panel(), "f", 2)
    nan_row = q.filter(pl.col("f").is_nan())
    assert nan_row["q"][0] is None, "NaN 行不得进最高分位组"


def test_add_quantile_nan_not_in_denominator():
    # 5 个有效值分 2 组：NaN 计入分母时分组整体挤偏（q1 会少一只）
    g = group_returns(_nan_panel(), "f", "fwd_ret_1", 2)
    n1 = g.filter(pl.col("q") == 1)["n"][0]
    assert n1 == 2, "NaN 不计入分母，q1 应有 2 只"


def test_top_n_nan_not_picked():
    t = top_n_returns(_nan_panel(), "f", "fwd_ret_1", 2)
    assert set(t["members"][0]) == {"e", "f"}, "NaN 不得优先进 Top-N"


def test_ic_series_nan_row_dropped_not_counted():
    s = ic_series(_nan_panel(), "f", "fwd_ret_1", min_obs=5)
    # NaN 被剔除后截面剩 5 个有效样本，min_obs=5 仍应通过且 IC 非 NaN
    assert len(s) == 1
    assert s["n"][0] == 5
    assert math.isfinite(s["ic"][0]), "单 NaN 不得毒化整日 IC"


def test_ic_series_all_nan_day_dropped():
    d = _nan_panel().with_columns(pl.lit(float("nan")).alias("f2"))
    s = ic_series(d, "f2", "fwd_ret_1", min_obs=5)
    assert len(s) == 0, "全 NaN 截面应整天剔除"


# ---------------------------------------------------------------- lasso


def test_lasso_residual_zero_when_y_linear_in_x():
    """y 完全由 x 线性决定时，中性化残差应≈0（中心化口径错的实现做不到）。"""
    pytest.importorskip("sklearn")
    n = 12
    x = [float(i) for i in range(n)]
    df = pl.DataFrame({
        "trade_date": [1] * n,
        "f": [3.0 * xi + 7.0 for xi in x],
        "market_cap": x,
        "industry_sw1": ["t1"] * n,
    })
    out = lasso(df, "f", by="trade_date",
                factors=["market_cap"], market_cap_source="market_cap")
    resid = out["f"].to_list()
    # lasso(alpha=1e-4) 有正则松弛，线性关系应被剔除到 ~1e-5 量级
    assert max(abs(v) for v in resid) < 1e-3, f"线性关系应被完全剔除: {resid}"


def test_lasso_residual_zero_mean():
    """正确中心化下残差均值为 0；旧实现偏移 mean(y) - y[0]。"""
    pytest.importorskip("sklearn")
    n = 12
    x = [float(i) for i in range(n)]
    y = [3.0 * xi + 7.0 + (0.1 if i % 2 else -0.1) for i, xi in enumerate(x)]
    df = pl.DataFrame({
        "trade_date": [1] * n,
        "f": y,
        "market_cap": x,
        "industry_sw1": ["t1"] * n,
    })
    out = lasso(df, "f", by="trade_date",
                factors=["market_cap"], market_cap_source="market_cap")
    assert abs(sum(out["f"].to_list())) < 1e-6, "残差均值应为 0"


# ---------------------------------------------------------------- group_ic


def test_group_ic_ir_nan_on_constant_ic():
    """组内 IC 恒定（std≈0）时 IR 应为 NaN 而不是 ±1e16。"""
    n = 10
    df = pl.DataFrame({
        "trade_date": [i // 5 for i in range(n)],
        "symbol": [f"s{i % 5}" for i in range(n)],
        "f": [1.0, 2.0, 3.0, 4.0, 5.0] * 2,
        "fwd_ret_1": [0.01, 0.02, 0.03, 0.04, 0.05] * 2,
        "g": ["g1"] * n,
    })
    out = ic_by_group(df, "f", "fwd_ret_1", "g", min_obs=5)
    assert len(out) == 1
    assert math.isnan(out["ir"][0]), "常数 IC 的 IR 必须是 NaN"


def test_size_group_nan_excluded():
    """amount 为 NaN 的行不得进最高市值组，也不挤偏其余分组。"""
    from lquant.factors.evaluate.group_ic import size_group

    df = pl.DataFrame({
        "trade_date": [1] * 6,
        "amount": [1.0, 2.0, float("nan"), 3.0, 4.0, 5.0],
    })
    out = size_group(df, n_groups=2)
    nan_q = out.filter(pl.col("amount").is_nan())["size_q"][0]
    assert nan_q is None, "NaN 市值行不得有分组"
    q1_n = out.filter(pl.col("size_q") == 1).height
    # 5 个有效值分 2 组：rank 1-2 → q1（2 只），rank 3-5 → q2
    assert q1_n == 2, f"分组应只按 5 个有效值计（q1={q1_n}）"
