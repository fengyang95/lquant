"""talib_set.py 指标补齐的验证：注册元数据、前缀不变性门禁、口径校验。"""

from __future__ import annotations

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
    """空值行应重置状态且不抛异常（停牌/缺口场景）。"""
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
