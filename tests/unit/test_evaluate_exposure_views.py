"""纯暴露 vs 组合暴露双视角归因（Phase 3.4）。

核心价值：**两者背离时，「可交易的组合」与「信号」不是一回事**。
典型场景：信号只轻微偏向小市值，但取 Top 10% 后组合变成极端小市值 ——
只看组合暴露完全看不到这一点。

断言要点：

1. 纯暴露用 ``w_i = f_i / Σ|f_i|``（负值因子不能把权重打爆）；
2. 组合暴露反映实际持仓（分箱 + 等权 + 多空）；
3. 构造「信号轻微偏向 + 极端分箱放大」的数据，``exposure_views`` 必须
   报出背离；
4. 纯因子与市值正交时不得误报背离。
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from lquant.factors.evaluate.attribution import (
    exposure_views,
    portfolio_exposure,
    pure_exposure,
)

D0 = date(2026, 1, 5)


def _panel(n_days: int = 30, n_sym: int = 60, *, coupling: float = 0.0,
           amplify: float = 1.0, seed: int = 0) -> pl.DataFrame:
    """构造面板。

    ``coupling``：因子与 log 市值的相关性（0 = 正交，>0 = 因子偏大市值）；
    ``amplify``：非线性放大（>1 拉开极端值，<1 压缩极端值）。

    注意 ``amplify != 1`` 会**削弱**双视角背离：放大极端值后，
    ``|f|`` 加权的纯暴露也被极端值主导，于是它和分箱组合暴露一起变极端，
    两者反而更接近。线性耦合（``amplify=1``）下的背离有解析预期：
    组合的多空暴露 ≈ ``2·1.755·ρ``（个截面标准差），纯暴露 ≈ ``1.253·ρ``，
    差值 ≈ ``2.26·ρ`` —— 实测 gap_z 随 ρ 线性增长（0.3→0.65，1.0→1.31）。
    """
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n_days):
        d = D0 + timedelta(days=i)
        logcap = rng.normal(23.0, 1.0, n_sym)
        noise = rng.normal(0, 1, n_sym)
        f = coupling * (logcap - logcap.mean()) + noise
        if amplify != 1.0:
            f = np.sign(f) * np.abs(f) ** amplify
        for j in range(n_sym):
            rows.append({"trade_date": d, "symbol": f"S{j:03d}",
                         "factor": float(f[j]),
                         "market_cap": float(np.exp(logcap[j])),
                         "turnover_rate": float(abs(noise[j]) + 0.5),
                         "fwd_ret_1": float(0.01 * f[j])})
    return pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))


# ----------------------------------------------------------- 纯暴露

def test_pure_exposure_weights_by_absolute_sum():
    """``w_i = f_i / Σ|f_i|``：手工核验单期结果。"""
    df = pl.DataFrame({
        "trade_date": [D0, D0, D0],
        "symbol": ["a", "b", "c"],
        "factor": [1.0, -2.0, 3.0],          # Σ|f| = 6
        "style": [10.0, 20.0, 30.0],
    }).with_columns(pl.col("trade_date").cast(pl.Date))
    out = pure_exposure(df, "factor", ["style"])
    # (1*10 + -2*20 + 3*30) / 6 = (10 - 40 + 90)/6 = 10
    assert out["style"][0] == pytest.approx(10.0)


def test_pure_exposure_handles_all_negative_factor():
    """全负因子：Σ|f| 归一保证权重仍是有限值（用 Σf 会符号翻转）。"""
    df = pl.DataFrame({
        "trade_date": [D0, D0],
        "symbol": ["a", "b"],
        "factor": [-1.0, -3.0],
        "style": [1.0, 2.0],
    }).with_columns(pl.col("trade_date").cast(pl.Date))
    out = pure_exposure(df, "factor", ["style"])
    # (-1*1 + -3*2)/4 = -7/4
    assert out["style"][0] == pytest.approx(-1.75)


def test_pure_exposure_is_per_day():
    """逐期独立归一：跨日不混（否则一天的极端值会污染另一天）。"""
    rows = []
    for i, f in enumerate(([1.0, 1.0], [100.0, 100.0])):
        d = D0 + timedelta(days=i)
        for j, v in enumerate(f):
            rows.append({"trade_date": d, "symbol": f"S{j}", "factor": v,
                         "style": 1.0 + j})
    df = pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))
    out = pure_exposure(df, "factor", ["style"])
    assert len(out) == 2
    assert out["style"].to_list() == pytest.approx([1.5, 1.5])   # 两天各自等权


def test_pure_exposure_empty_when_no_style_columns():
    df = _panel(n_days=3, n_sym=10)
    assert len(pure_exposure(df, "factor", ["nope"])) == 0


def test_pure_exposure_skips_zero_factor_day():
    """某日因子全 0 → 分母为 0，该日应被跳过而不是产生 inf/nan。"""
    df = pl.DataFrame({
        "trade_date": [D0, D0, D0 + timedelta(days=1), D0 + timedelta(days=1)],
        "symbol": ["a", "b", "a", "b"],
        "factor": [0.0, 0.0, 1.0, -1.0],
        "style": [1.0, 2.0, 1.0, 3.0],
    }).with_columns(pl.col("trade_date").cast(pl.Date))
    out = pure_exposure(df, "factor", ["style"])
    assert len(out) == 1
    assert np.isfinite(out["style"].to_numpy()).all()


# ----------------------------------------------------------- 组合暴露

def test_portfolio_exposure_long_short_is_top_minus_bottom():
    df = pl.DataFrame({
        "trade_date": [D0] * 4,
        "symbol": ["a", "b", "c", "d"],
        "factor": [1.0, 2.0, 3.0, 4.0],
        "style": [10.0, 20.0, 30.0, 40.0],
    }).with_columns(pl.col("trade_date").cast(pl.Date))
    out = portfolio_exposure(df, "factor", ["style"], n_groups=2)
    # 第 2 组 = {c,d} 均值 35；第 1 组 = {a,b} 均值 15 → 20
    assert out["style"][0] == pytest.approx(20.0)


def test_portfolio_exposure_long_side_only():
    df = pl.DataFrame({
        "trade_date": [D0] * 4,
        "symbol": ["a", "b", "c", "d"],
        "factor": [1.0, 2.0, 3.0, 4.0],
        "style": [10.0, 20.0, 30.0, 40.0],
    }).with_columns(pl.col("trade_date").cast(pl.Date))
    out = portfolio_exposure(df, "factor", ["style"], n_groups=2, side="long")
    assert out["style"][0] == pytest.approx(35.0)


# ----------------------------------------------------------- 双视角对照

def test_divergence_detected_when_binning_amplifies_exposure():
    """因子线性偏向大市值 → 分箱后的组合暴露明显强于纯暴露，必须报背离。

    机制：纯暴露是 ``|f|`` 加权的全截面平均，组合暴露是极端十分位之差；
    对线性耦合，后者约为前者的 2.8 倍（见 ``_panel`` 的解析推导）。
    """
    df = _panel(coupling=1.0, amplify=1.0, seed=1)
    v = exposure_views(df, "factor", ["market_cap"], n_groups=10)
    assert v["compare"], "对照表不能为空"
    row = v["compare"][0]
    assert row["cov"] == "market_cap"
    assert row["portfolio_mean"] > row["pure_mean"]      # 组合暴露更强
    assert abs(row["gap_z"]) >= 1.0, row
    assert v["divergences"], v["compare"]


def test_divergence_scales_with_coupling():
    """背离幅度随耦合强度单调增长（口径自洽性检查）。"""
    zs = []
    for rho in (0.3, 0.6, 1.0, 1.5):
        v = exposure_views(_panel(coupling=rho, amplify=1.0, seed=1),
                           "factor", ["market_cap"], n_groups=10)
        zs.append(v["compare"][0]["gap_z"])
    assert zs == sorted(zs) and zs[0] < zs[-1]


def test_no_divergence_reported_when_orthogonal():
    """因子与风格正交时不得误报背离（否则这个信号会天天报警，失去意义）。"""
    for seed in (2, 3, 4):
        v = exposure_views(_panel(coupling=0.0, amplify=1.0, seed=seed),
                           "factor", ["market_cap"], n_groups=10)
        z = v["compare"][0]["gap_z"]
        assert z is None or abs(z) < 0.3, (seed, z)
        assert v["divergences"] == []


def test_exposure_views_reports_both_views_and_metadata():
    df = _panel(coupling=0.3, amplify=1.5, seed=3)
    v = exposure_views(df, "factor", ["market_cap", "turnover_rate"], n_groups=5)
    assert set(v) == {"pure", "portfolio", "compare", "divergences",
                      "side", "n_groups"}
    assert v["side"] == "long_short" and v["n_groups"] == 5
    assert len(v["pure"]) == v["portfolio"].height == 30
    assert {r["cov"] for r in v["compare"]} == {"market_cap", "turnover_rate"}
    for r in v["compare"]:
        assert r["n_days"] == 30
        assert r["gap"] == pytest.approx(r["portfolio_mean"] - r["pure_mean"])
        assert r["style_scale"] > 0
        if r["gap_z"] is not None:
            assert r["gap_z"] == pytest.approx(r["gap"] / r["style_scale"])


def test_exposure_views_gap_z_none_when_pure_has_no_variance():
    """纯暴露在时间上无波动（常数）→ gap_z 记 None，而不是除零成 inf。"""
    rows = []
    for i in range(5):
        d = D0 + timedelta(days=i)
        for j in range(10):
            rows.append({"trade_date": d, "symbol": f"S{j}", "factor": 1.0,
                         "style": 7.0})
    df = pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))
    v = exposure_views(df, "factor", ["style"], n_groups=5)
    assert v["compare"][0]["gap_z"] is None


def test_exposure_views_side_passthrough():
    df = _panel(coupling=0.4, seed=4)
    a = exposure_views(df, "factor", ["market_cap"], side="long")
    b = exposure_views(df, "factor", ["market_cap"], side="long_short")
    assert a["side"] == "long" and b["side"] == "long_short"
    # 只做多与多空的暴露不同（前者含市场 beta）
    assert a["compare"][0]["portfolio_mean"] != pytest.approx(
        b["compare"][0]["portfolio_mean"])
