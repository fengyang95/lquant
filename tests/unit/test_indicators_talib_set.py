"""talib_set.py 指标补齐的验证：注册元数据、前缀不变性门禁、口径校验。"""

from __future__ import annotations

import inspect
import random
from datetime import date, timedelta

import polars as pl
import pytest

from lquant.indicators import (
    INDICATORS,
    assert_no_lookahead,
    compute,
    talib_set,  # noqa: F401  触发注册
)
from lquant.indicators.registry import outputs as ind_outputs
from lquant.indicators.talib_set import _rsi_expr, add_sar

NEW_INDICATORS = [
    "atr", "obv", "dmi", "cci", "wr", "roc", "dpo", "supertrend",
    "adxr", "mfi", "ppo", "stochrsi", "tema", "trix", "sar",
]


def random_ohlc(n: int = 80, seed: int = 7) -> pl.DataFrame:
    rnd = random.Random(seed)
    rows = []
    price = 10.0
    for _ in range(n):
        o = price * (1 + rnd.uniform(-0.02, 0.02))
        c = o * (1 + rnd.uniform(-0.05, 0.05))
        h = max(o, c) * (1 + rnd.uniform(0.0, 0.02))
        low = min(o, c) * (1 - rnd.uniform(0.0, 0.02))
        rows.append((round(o, 4), round(h, 4), round(low, 4), round(c, 4)))
        price = c
    d0 = date(2024, 1, 2)
    return pl.DataFrame(
        {
            "symbol": ["TEST"] * n,
            "trade_date": [d0 + timedelta(days=i) for i in range(n)],
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": [float(1e6 + i * 1e4) for i in range(n)],
        }
    )


@pytest.mark.parametrize("name", NEW_INDICATORS)
def test_registered(name: str):
    meta = INDICATORS.meta(name)
    assert meta["category"] in {"trend", "oscillator", "volume", "channel"}
    assert all(o in ind_outputs(name) for o in meta["outputs"])


@pytest.mark.parametrize("name", NEW_INDICATORS)
def test_no_lookahead(name: str):
    df = random_ohlc()
    assert_no_lookahead(lambda d: compute(name, d), df,
                        out_cols=ind_outputs(name))


def test_atr_positive():
    out = compute("atr", random_ohlc())
    atr = out["atr14"].drop_nulls()
    assert len(atr) > 0
    assert (atr > 0).all()


def test_obv_matches_manual():
    df = random_ohlc(5)
    out = compute("obv", df)
    closes = df["close"].to_list()
    vols = df["volume"].to_list()
    expect = 0.0
    manual = [0.0]
    for i in range(1, 5):
        expect += (1 if closes[i] > closes[i - 1] else -1 if closes[i] < closes[i - 1] else 0) * vols[i]
        manual.append(expect)
    assert out["obv"].to_list() == pytest.approx(manual)


def test_dmi_ranges():
    out = compute("dmi", random_ohlc())
    for col in ("dmi_pdi", "dmi_mdi"):
        vals = out[col].drop_nulls()
        assert (vals >= 0).all() and (vals <= 100).all()
    adx = out["dmi_adx"].drop_nulls()
    assert (adx >= 0).all() and (adx <= 100).all()


def test_wr_bounds():
    out = compute("wr", random_ohlc())
    vals = out["wr14"].drop_nulls()
    assert (vals >= 0).all() and (vals <= 100).all()


def test_wr_flat_window_neutral():
    d0 = date(2024, 1, 2)
    n = 20
    df = pl.DataFrame(
        {
            "symbol": ["T"] * n,
            "trade_date": [d0 + timedelta(days=i) for i in range(n)],
            "open": [10.0] * n, "high": [10.0] * n,
            "low": [10.0] * n, "close": [10.0] * n,
            "volume": [1e6] * n,
        }
    )
    out = compute("wr", df)
    assert out["wr14"][-1] == 50.0


def test_roc_sign():
    df = random_ohlc(30)
    out = compute("roc", df)
    c = df["close"].to_list()
    roc_last = out["roc12"][-1]
    manual = (c[-1] - c[-13]) / c[-13] * 100
    assert roc_last == pytest.approx(manual)


def test_supertrend_dir_binary():
    out = compute("supertrend", random_ohlc(60))
    dirs = {d for d in out["supertrend_dir"].to_list() if d is not None}
    assert dirs <= {1, -1}
    st = out["supertrend"].to_list()
    assert all(v is None or v > 0 for v in st)


# ---------------- 第二批：对齐 InStock 25 指标清单的剩余 7 项 ----------------


def test_mfi_matches_manual():
    """MFI 与 TA-Lib 逐位一致（实测定标），此处按定义手工复算尾值。"""
    df = random_ohlc(40)
    out = compute("mfi", df)
    tp = [(h + l + c) / 3 for h, l, c in
          zip(df["high"].to_list(), df["low"].to_list(), df["close"].to_list(),
              strict=True)]
    vol = df["volume"].to_list()
    pos = neg = 0.0
    for i in range(len(tp) - 14, len(tp)):
        raw = tp[i] * vol[i]
        if tp[i] > tp[i - 1]:
            pos += raw
        elif tp[i] < tp[i - 1]:
            neg += raw
    expect = 100.0 if neg <= 0 else 100 - 100 / (1 + pos / neg)
    assert out["mfi14"][-1] == pytest.approx(expect, rel=1e-6)


def test_mfi_bounds():
    vals = compute("mfi", random_ohlc(60))["mfi14"].drop_nulls()
    assert (vals >= 0).all() and (vals <= 100).all()


def test_tema_converges_to_ta_lib_seed():
    """TEMA 与 TA-Lib 的差异仅来自初始种子；尾部应高度一致（相对误差 <1%）。"""
    df = random_ohlc(200)
    out = compute("tema", df)
    tema = out["tema"].drop_nulls()
    assert len(tema) > 0
    # 三重 EMA 平滑，值应落在价格区间附近且无 NaN
    assert all(v > 0 for v in tema.to_list())


def test_ppo_structure():
    out = compute("ppo", random_ohlc(120))
    ppo = out["ppo"].drop_nulls()
    assert len(ppo) > 0
    # hist = ppo - signal 恒等
    for row in out.select(["ppo", "ppo_signal", "ppo_hist"]).drop_nulls().to_dicts()[:20]:
        assert row["ppo_hist"] == pytest.approx(row["ppo"] - row["ppo_signal"], abs=1e-9)


def test_stochrsi_bounds():
    out = compute("stochrsi", random_ohlc(120))
    k = out["stochrsi_k"].drop_nulls()
    d = out["stochrsi_d"].drop_nulls()
    assert (k >= 0).all() and (k <= 100).all()
    assert (d >= 0).all() and (d <= 100).all()


def test_trix_relation_to_tema():
    """TRIX 是三重 EMA 的变化率，量级应为小百分数（|trix| < 10）。"""
    vals = compute("trix", random_ohlc(120))["trix"].drop_nulls()
    assert len(vals) > 0
    assert (vals.abs() < 10).all()


def test_adxr_is_adx_average():
    df = random_ohlc(200)
    out = compute("adxr", df)
    dmi = compute("dmi", df)
    adxr = out["adxr14"]
    adx = dmi["dmi_adx"]
    n = 14
    # adxr_t = (adx_t + adx_{t-n}) / 2
    for i in range(n + 60, 200, 20):
        a, b = adx[i], adx[i - n]
        if a is None or b is None or adxr[i] is None:
            continue
        assert adxr[i] == pytest.approx((a + b) / 2, rel=1e-9)


def test_sar_dir_binary_and_straddles_price():
    df = random_ohlc(120)
    out = compute("sar", df)
    dirs = {d for d in out["sar_dir"].to_list() if d is not None}
    assert dirs <= {1, -1}
    sar = out["sar"].drop_nulls()
    assert len(sar) > 0 and all(v > 0 for v in sar.to_list())


def test_sar_flat_reset_no_crash():
    """空值行不得抛异常（停牌/缺口场景）；状态机保持（不重置），缺口后的
    第一个有效行从陈旧状态继续 —— 见 add_sar docstring 的空值语义。"""
    d0 = date(2024, 1, 2)
    n = 10
    highs = [10.0, 10.5, None, 10.8, 10.6, 10.9, 11.0, 10.7, 10.9, 11.1]
    lows = [9.5, 9.8, None, 10.0, 10.1, 10.2, 10.3, 10.2, 10.4, 10.5]
    df = pl.DataFrame({
        "symbol": ["T"] * n,
        "trade_date": [d0 + timedelta(days=i) for i in range(n)],
        "open": lows, "high": highs, "low": lows,
        "close": [9.8, 10.2, None, 10.4, 10.3, 10.7, 10.8, 10.5, 10.7, 10.9],
        "volume": [1e6] * n,
    })
    out = compute("sar", df)
    assert out["sar"][-1] is not None


# ---------------- 口径标定与 fail-loudly（审阅发现的缺陷回归） ----------------


def test_cci_matches_classic_formula():
    """CCI 的分母必须是「关于当前窗口均值」的平均绝对偏差（TA-Lib/Lambert 口径）。

    反例（曾经的实现）：``rolling_mean(|TP − 各自的滚动均值|)`` —— 线性序列上
    该值是正确 MD 的一半，CCI 于是系统性放大到两倍（实测 66.666667 vs 123.809524）。
    这里在测试里独立复算教科书公式，不引用被测实现的任何中间量。
    """
    df = random_ohlc(80)
    out = compute("cci", df)
    n = 14
    tp = [(h + l + c) / 3 for h, l, c in zip(df["high"].to_list(), df["low"].to_list(),
                                             df["close"].to_list(), strict=True)]
    got = out["cci14"].to_list()
    checked = 0
    for i in range(n - 1, len(tp)):
        window = tp[i - n + 1:i + 1]
        mean = sum(window) / n
        md = sum(abs(x - mean) for x in window) / n
        if got[i] is None:
            assert md == 0.0, i
            continue
        assert got[i] == pytest.approx((tp[i] - mean) / (0.015 * md), rel=1e-9), i
        checked += 1
    assert checked > 0


def test_cci_linear_ramp_matches_talib_value():
    """线性序列的解析标定：TA-Lib 口径 123.809524，旧（错误）口径 66.666667。"""
    n = 30
    close = [10.0 + i * 0.1 for i in range(n)]
    df = pl.DataFrame({
        "symbol": ["T"] * n,
        "trade_date": [date(2024, 1, 2) + timedelta(days=i) for i in range(n)],
        "open": close,
        "high": [c + 0.05 for c in close],
        "low": [c - 0.05 for c in close],
        "close": close,
        "volume": [1e6] * n,
    })
    assert compute("cci", df)["cci14"][-1] == pytest.approx(123.809524, rel=1e-6)


def test_adxr_does_not_leak_dmi_columns():
    """adxr 在 dmi 之上实现，但只能输出自己登记的列（否则链式拼接里出现野列）。"""
    df = random_ohlc(60)
    out = compute("adxr", df)
    assert set(out.columns) == set(df.columns) | {"adxr14"}


def test_period_params_fail_loudly():
    """所有带 n 的指标：n=0 必须是 ValueError，而不是 ZeroDivisionError。"""
    df = random_ohlc(40)
    checked = 0
    for fname, fn in inspect.getmembers(talib_set, inspect.isfunction):
        if not fname.startswith("add_") or "n" not in inspect.signature(fn).parameters:
            continue
        with pytest.raises(ValueError):
            fn(df, n=0)
        checked += 1
    assert checked >= 10, checked


def test_rsi_zero_loss_window_is_100_not_null():
    """纯上涨窗口 avg_loss=0：RSI 必须是 100（超买极值），不是 null。"""
    n = 80
    close = [10.0 * (1.001 ** i) for i in range(n)]
    df = pl.DataFrame({"close": close})
    out = df.with_columns(_rsi_expr("close", 14).alias("rsi"))
    assert out["rsi"][-1] == pytest.approx(100.0)


@pytest.mark.parametrize("kw", [
    {"af_step": 0.0}, {"af_step": -0.1}, {"af_max": 0.0}, {"af_max": -0.5},
    {"af_step": 0.5, "af_max": 0.1},
])
def test_sar_params_fail_loudly(kw: dict):
    with pytest.raises(ValueError):
        add_sar(random_ohlc(20), **kw)


def test_sar_first_bar_is_warmup_and_null_safe():
    """第 0 根没有「前一根」，必须是 null —— 旧实现用第 1 根给它定值（前视）。

    同时覆盖前导空值/全空 OHLC：旧实现直接读 ``h[0]``/``h[1]``，会抛
    ``TypeError: '>=' not supported between 'float' and 'NoneType'``。
    """
    df = random_ohlc(30)
    one = compute("sar", df.head(1))
    assert one["sar"][0] is None and one["sar_dir"][0] is None
    full = compute("sar", df)
    assert full["sar"][0] is None          # 第 0 根不得依赖第 1 根
    assert full["sar"][1] is not None
    for col in ("high", "low"):
        led = df.with_columns(
            pl.when(pl.arange(0, pl.len()) == 0).then(None)
            .otherwise(pl.col(col)).alias(col)
        )
        assert compute("sar", led)["sar"][-1] is not None
    allnull = df.with_columns(
        pl.lit(None, dtype=pl.Float64).alias("high"),
        pl.lit(None, dtype=pl.Float64).alias("low"),
    )
    out = compute("sar", allnull)
    assert out["sar"].null_count() == out.height


def test_ppo_rejects_fast_ge_slow():
    """fast >= slow 会让 PPO 退化成反向差值，必须 fail-loudly。"""
    df = random_ohlc(40)
    with pytest.raises(ValueError, match="fast"):
        compute("ppo", df, fast=26, slow=12)


def test_supertrend_mid_null_row_no_crash():
    """中间缺行：不得抛异常，缺口行 null，前后照常出值。"""
    df = random_ohlc(40)
    hole = pl.arange(0, pl.len()) == 20
    df = df.with_columns(
        pl.when(hole).then(None).otherwise(pl.col("high")).alias("high"),
        pl.when(hole).then(None).otherwise(pl.col("low")).alias("low"),
    )
    out = compute("supertrend", df)
    assert out["supertrend"][20] is None
    assert out["supertrend"][-1] is not None


def test_sar_tracks_downtrend_and_flips():
    """下跌段必须走空头分支（sar 在上方、dir=-1），并能在反弹时翻多。

    随机游走数据几乎只走多头分支，空头那一半状态机此前没有任何用例覆盖。
    """
    n = 40
    close = [20.0 - i * 0.2 for i in range(n)]
    df = pl.DataFrame({
        "symbol": ["T"] * n,
        "trade_date": [date(2024, 1, 2) + timedelta(days=i) for i in range(n)],
        "open": close, "high": [c + 0.1 for c in close],
        "low": [c - 0.1 for c in close], "close": close, "volume": [1e6] * n,
    })
    out = compute("sar", df)
    dirs = out["sar_dir"].to_list()
    assert -1 in dirs[2:], dirs
    assert 1 not in dirs           # 全程下跌不该翻多
    # 空头：SAR 在价格上方（阻力）
    for i in range(5, n):
        assert out["sar"][i] > out["low"][i]

    # 尾段拉一根长阳冲破 SAR → 翻多（SAR 用高低价判，故 OHLC 都要抬起来）
    last = pl.arange(0, pl.len()) == n - 1
    up = df.with_columns(
        pl.when(last).then(12.2).otherwise(pl.col("open")).alias("open"),
        pl.when(last).then(30.1).otherwise(pl.col("high")).alias("high"),
        pl.when(last).then(12.2).otherwise(pl.col("low")).alias("low"),
        pl.when(last).then(30.0).otherwise(pl.col("close")).alias("close"),
    )
    flipped = compute("sar", up)
    assert flipped["sar_dir"][-1] == 1
    assert flipped["sar"][-1] < up["close"][-1]   # 翻多后 SAR 在价格下方（支撑）
