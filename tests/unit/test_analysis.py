"""因子分析（相关性/合成）与技术指标的单元测试。"""
import polars as pl
import pytest


def _demo_daily(n_days: int = 120, n_syms: int = 12) -> pl.DataFrame:
    """合成日线：两因子相关、一因子独立，便于断言。"""
    import numpy as np

    rng = np.random.default_rng(7)
    dates = pl.date_range(pl.date(2026, 1, 5), pl.date(2026, 1, 5) + pl.duration(days=n_days * 2),
                          "1d", eager=True)[:n_days]
    rows = []
    for si in range(n_syms):
        base = 10 + si
        a = rng.normal(0, 0.02, n_days).cumsum()
        for i, d in enumerate(dates):
            rows.append({
                "symbol": f"{600000 + si}.SH", "trade_date": d,
                "open": base + a[i], "high": base + a[i] + 0.1,
                "low": base + a[i] - 0.1, "close": base + a[i],
                "volume": float(rng.integers(1e5, 1e6)),
                "amount": float(rng.integers(1e8, 1e9)),
            })
    return pl.DataFrame(rows)


def test_corr_high_for_same_factor_shifted():
    """pct_change_5 与 pct_change_10 同源动量，相关性应显著高于与波动率的相关。"""
    from lquant.factors.analysis import correlation

    df = _demo_daily()
    r = correlation(df, ["pct_change_5", "pct_change_10", "rolling_std_20"])
    m = r["matrix"]
    mom = abs(m[0][1])
    cross = abs(m[0][2])
    assert mom > cross, f"动量内部相关({mom}) 应高于动量-波动({cross})"
    # 对角线为 1
    for i in range(3):
        assert abs(m[i][i] - 1.0) < 1e-6


def test_corr_too_few_factors_rejected():
    from lquant.core.errors import FactorError
    from lquant.factors.analysis import correlation

    with pytest.raises((ValueError, FactorError)):
        correlation(_demo_daily(), ["pct_change_5"])


def test_corr_insufficient_cross_section_rejected():
    """每日有效样本 < 5 的薄截面：不产出相关矩阵，直接报错（不能静默给噪声）。"""
    from lquant.factors.analysis import correlation

    with pytest.raises(ValueError, match="有效截面不足"):
        correlation(_demo_daily(n_syms=2), ["pct_change_5", "rolling_std_20"])


def test_compute_factor_col_overwrites_same_name():
    """同名二次计算 = 覆盖重算（与 DSL 路径语义一致，不因列已存在而抛 DuplicateError）。

    用 KBAR 族（KMID）：它的 compute 分支保留原 df 的列，所以第二次调用时
    输出帧里会同时有 `KMID` 与 `_factor`，必须先丢旧列再 rename。
    """
    from lquant.factors.analysis import compute_factor_col

    once = compute_factor_col(_demo_daily(), "KMID", "KMID")
    twice = compute_factor_col(once, "KMID", "KMID")
    assert twice["KMID"].equals(once["KMID"])


def test_synthesize_equal_and_ic_weighted():
    from lquant.factors.analysis import synthesize

    df = _demo_daily()
    for method in ("equal", "ic_weighted"):
        out = synthesize(df, ["pct_change_5", "rolling_std_20"], method=method)
        assert "_syn" in out.columns
        assert out["_syn"].null_count() == 0
        assert len(out) > 0


def test_compute_factor_col_supports_qlib_builtin_names():
    """Qlib Alpha158 内置名（MA20 / RSV10）必须能在相关性 / 合成链路上算出来。

    因子研究页的「相关性 · 合成」页签把这些名字做成可点标签；早先这里直接
    落到 DSL 分支，抛 `字段 'MA20' 不在数据列中`（FactorError，不是 ValueError）
    → /factors/analyze 与 /factors/synthesize 双双 500。
    """
    from lquant.factors.analysis import compute_factor_col, correlation

    df = _demo_daily()
    out = compute_factor_col(df, "MA20")
    assert "MA20" in out.columns
    assert out["MA20"].null_count() < len(out)      # warmup 之外应有值

    r = correlation(df, ["MA20", "RSV10"])
    assert r["factors"] == ["MA20", "RSV10"]
    assert r["n_dates"] > 0
    assert abs(r["matrix"][0][0] - 1.0) < 1e-6


def test_synthesize_accepts_builtin_names():
    from lquant.factors.analysis import synthesize

    out = synthesize(_demo_daily(), ["MA20", "pct_change_5"], method="ic_weighted")
    assert "_syn" in out.columns
    assert len(out) > 0


def test_indicators_values_sane():
    """MA 收敛于 close、RSI 在 0-100、BOLL 包络中轨。"""
    from lquant.factors.indicators import add_all

    df = _demo_daily(n_days=200, n_syms=1).drop("symbol")
    out = add_all(df)
    last = out.tail(1).row(0, named=True)
    assert abs(last["ma5"] - out["close"].tail(5).mean()) < 1e-6
    assert 0 <= last["rsi14"] <= 100
    assert last["boll_lower"] <= last["boll_mid"] <= last["boll_upper"]
    # warmup 前为 null
    assert out["ma60"].head(10).null_count() == 10


def test_macd_cn_hist_convention():
    """国内口径 HIST = 2*(DIF-DEA)。"""
    from lquant.factors.indicators import add_macd

    df = _demo_daily(n_days=100, n_syms=1)
    out = add_macd(df).drop_nulls(["macd_hist"])
    row = out.tail(1).row(0, named=True)
    assert abs(row["macd_hist"] - 2 * (row["macd_dif"] - row["macd_dea"])) < 1e-9
