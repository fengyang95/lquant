"""筹码分布 CYQ：注册枚举、模型语义、口径标注、前缀不变性。"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from lquant.indicators import (
    CYQ_PROXY_NOTE,
    INDICATORS,
    assert_no_lookahead,
    compute,
    cost_distribution,
    profit_ratio,
)


def make_df(closes: list[float], volumes: list[float] | None = None) -> pl.DataFrame:
    """合成 OHLCV：H/L 取 close ±0.1 → 成交代表价 (H+L+2C)/4 恰等于 close，便于解析验证。"""
    n = len(closes)
    return pl.DataFrame(
        {
            "trade_date": [date(2026, 1, 1) + timedelta(days=i) for i in range(n)],
            "open": closes,
            "high": [c + 0.1 for c in closes],
            "low": [c - 0.1 for c in closes],
            "close": closes,
            "volume": volumes or [1e6] * n,
        }
    )


# ---------- 注册表 ----------


def test_cyq_registered_with_valid_meta():
    meta = INDICATORS.meta("cyq_profit_ratio")  # 未注册即 KeyError，兼作存在性断言
    assert meta["category"] == "oscillator"  # 0~1 比率 → 摆动类，画子图
    assert meta["pane"] == "sub"
    assert set(meta["inputs"]) >= {"high", "low", "close", "volume"}
    assert meta["outputs"] == ["cyq_profit_ratio"]
    assert meta["min_window"] == 5


def test_compute_keeps_row_count_and_bounds():
    rng = np.random.default_rng(7)
    closes = list(100 + np.cumsum(rng.normal(0, 1, 80)))
    df = make_df([float(c) for c in closes])
    out = compute("cyq_profit_ratio", df)
    assert out.height == df.height
    v = out["cyq_profit_ratio"]
    assert (v.drop_nulls() >= 0).all() and (v.drop_nulls() <= 1).all()


def test_missing_columns_yield_null_column_not_crash():
    """缺输入列：补全 null 占位（outputs 声明了列），保持行数不崩批量链路。"""
    df = pl.DataFrame({"close": [1.0, 2.0, 3.0]})
    out = compute("cyq_profit_ratio", df)
    assert out.height == 3
    assert out["cyq_profit_ratio"].null_count() == 3


# ---------- 模型语义（H=L=C±0.1 → 代表价恰为 close，可解析验证） ----------


def test_grinding_up_yields_high_profit_ratio():
    """缓涨 + 低换手：早期低价筹码留存 → 现价上方筹码占比接近 1。"""
    closes = [10 + 2 * i / 19 for i in range(20)]  # 10 → 12 匀速缓涨
    df = make_df(closes)
    got = profit_ratio(df, float_shares=1e8)  # 日换手 1e6/1e8 = 1%
    # 末日新筹码权重 1%/(Σ0.99^k) ≈ 5.4%，其余全部获利
    assert got["profit_ratio"] == pytest.approx(
        1 - 0.01 / (0.01 * sum(0.99**k for k in range(20))), abs=0.01
    )
    assert got["proxy"] is False
    assert got["note"] == "float_shares 显式给定"


def test_full_turnover_clears_history():
    """单日全换手（t=1）：历史筹码当日清零，只剩当日筹码 → 获利盘为 0（代表价=close）。"""
    closes = [10.0] * 5 + [12.0] * 5 + [9.0]  # 前面涨跌再怎么走都清零
    df = make_df(closes, volumes=[1e6] * 11)
    got = profit_ratio(df, float_shares=1e6)  # 日量/流通 = 100%
    assert got["profit_ratio"] == 0.0


def test_deep_decline_leaves_little_profit():
    closes = [12 - 2 * i / 19 for i in range(20)] + [10.05]  # 12 跌到 10，末日小反弹
    df = make_df(closes)
    got = profit_ratio(df, float_shares=1e8)
    assert got["profit_ratio"] < 0.1


def test_proxy_turnover_keeps_relative_structure():
    """代理口径：放量日换手大、缩量日小 —— 结构性断言而非精确数值。"""
    closes = [10.0] * 10 + [12.0]  # 恒量铺底 + 末日放量
    vols = [1e6] * 10 + [5e6]
    df = make_df(closes, volumes=vols)
    got = profit_ratio(df)
    assert got["proxy"] is True
    assert got["note"] == CYQ_PROXY_NOTE
    # 末日放量 → 代理换手 clip 到 1 → 历史清零 → 获利盘 0（代表价=close=12 不小于自身）
    assert got["profit_ratio"] == 0.0


def test_zero_volume_degrades_to_none():
    """全零成交量：无筹码信息 → 显式 None，不伪造 0 或 1。"""
    df = make_df([10.0] * 6, volumes=[0.0] * 6)
    got = profit_ratio(df)
    assert got["profit_ratio"] is None


def test_empty_df_degrades():
    assert cost_distribution(pl.DataFrame()) == []
    assert profit_ratio(pl.DataFrame())["profit_ratio"] is None
    assert profit_ratio(pl.DataFrame())["note"] == "无数据"


def test_float_shares_column_is_picked_up():
    """注册指标链路：df 自带 float_shares 列时用显式口径（与调用参数等价）。"""
    closes = [10 + 2 * i / 19 for i in range(20)]
    df = make_df(closes).with_columns(pl.lit(1e8).alias("float_shares"))
    out = compute("cyq_profit_ratio", df)
    via_column = out["cyq_profit_ratio"][-1]
    via_arg = profit_ratio(df, float_shares=1e8)["profit_ratio"]
    assert via_column == pytest.approx(via_arg)


# ---------- cost_distribution（直方图，仅末端快照） ----------


def test_cost_distribution_sums_to_one():
    rng = np.random.default_rng(3)
    closes = list(100 + np.cumsum(rng.normal(0, 1, 60)))
    dist = cost_distribution(make_df([float(c) for c in closes]), bins=30)
    assert dist
    assert sum(p for _, p in dist) == pytest.approx(1.0, abs=1e-9)
    centers = [c for c, _ in dist]
    assert centers == sorted(centers)  # bin 中心有序
    assert all(99.0 <= c <= 102.0 for c in centers)  # 落在价格区间内


# ---------- 前缀不变性（无未来函数门禁） ----------


def test_no_lookahead_proxy_turnover():
    """代理口径（expanding 均量）与解析式滚动都必须严格前缀不变。"""
    rng = np.random.default_rng(11)
    closes = list(100 + np.cumsum(rng.normal(0, 1, 40)))
    df = make_df([float(c) for c in closes])
    assert_no_lookahead(lambda d: compute("cyq_profit_ratio", d), df, out_cols=["cyq_profit_ratio"])


def test_no_lookahead_explicit_float_shares():
    rng = np.random.default_rng(12)
    closes = list(100 + np.cumsum(rng.normal(0, 1, 40)))
    df = make_df([float(c) for c in closes]).with_columns(pl.lit(1e8).alias("float_shares"))
    assert_no_lookahead(lambda d: compute("cyq_profit_ratio", d), df, out_cols=["cyq_profit_ratio"])


def test_no_lookahead_when_float_shares_changes_midway():
    """股本列**中途变化**（送转/增发）时仍必须前缀不变。

    修复前把整列折成「最后一个非空值」贯穿全历史：早期行的换手率用到了未来
    才知道的股本 → 前缀重算结果分叉。常数股本列测不出这个 bug，所以这条用
    「变化」的股本列守门。
    """
    closes = [10.0, 10.2, 10.4, 10.6, 10.8, 11.0, 11.2, 11.4, 11.6, 11.8, 12.0, 12.2]
    fs = [1e8] * 10 + [5e8] * 2  # 第 11 天（index 10）股本翻 5 倍
    df = make_df(closes).with_columns(pl.Series("float_shares", fs, dtype=pl.Float64))

    assert_no_lookahead(lambda d: compute("cyq_profit_ratio", d), df, out_cols=["cyq_profit_ratio"])

    full = compute("cyq_profit_ratio", df)["cyq_profit_ratio"].to_list()
    prefix = compute("cyq_profit_ratio", df.head(8))["cyq_profit_ratio"].to_list()
    assert full[:8] == prefix, "股本列变化后早期行结果被改写 → 未来函数"


def test_float_shares_row_null_falls_back_to_proxy_not_zero():
    """股本列个别日 null → 该行退回代理口径（不是静默换手 0 / 整段塌掉）。"""
    closes = [10.0, 10.2, 10.4, 10.6, 10.8, 11.0, 11.2]
    fs = [1e8, 1e8, None, 1e8, 1e8, 1e8, 1e8]
    df = make_df(closes).with_columns(pl.Series("float_shares", fs, dtype=pl.Float64))
    out = compute("cyq_profit_ratio", df)["cyq_profit_ratio"].drop_nulls()
    assert len(out) == len(closes)  # 没有任何一行因 null 股本而丢失
    assert (out >= 0).all() and (out <= 1).all()
