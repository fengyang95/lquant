"""Alpha360 原生重实现（Phase 3.5）—— 对照 qlib ``Alpha360DL`` 源码。

最关键的断言：

1. **恰好 360 个特征**，命名与顺序与 qlib 的生成循环一致
   （字段序 CLOSE/OPEN/HIGH/LOW/VWAP/VOLUME，滞后 59→0）；
2. **VOLUME 族用 volume 归一，不是 close** —— 这是最容易写错的一处；
3. **``CLOSE0 ≡ 1.0``** 哨兵（``$close/$close``）；
4. **无未来函数**：改未来价格不得改变过去特征；
5. 手算对拍若干特征（含边界 lag=59 与 lag=0）。
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from lquant.factors.alpha360 import (
    FEATURE_COUNT,
    FIELDS,
    LAGS,
    compute,
    compute_all,
    feature_names,
    has_factor,
    list_builtin,
    resolve_name,
)


def _panel(n_days: int = 70, n_sym: int = 3, seed: int = 0) -> pl.DataFrame:
    """确定性面板：价格与成交量都按可复算的公式生成（不用随机数）。"""
    rows = []
    for j in range(n_sym):
        px = 10.0 + j
        for i in range(n_days):
            d = date(2026, 1, 5) + timedelta(days=i)
            pre = px
            px = round(pre * (1 + 0.01 * np.sin(i / 3.0 + j)), 4)
            vol = 1_000_000.0 + i * 1000.0 + j * 100.0
            rows.append({"trade_date": d, "symbol": f"S{j}", "open": pre,
                         "high": max(pre, px) * 1.001, "low": min(pre, px) * 0.999,
                         "close": px, "pre_close": pre, "volume": vol,
                         "amount": vol * px})
    return pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))


# ----------------------------------------------------------- 目录与命名

def test_feature_count_is_exactly_360():
    assert FEATURE_COUNT == 360
    assert len(feature_names()) == 360
    assert len(set(feature_names())) == 360          # 无重名


def test_feature_order_matches_qlib_generation_loop():
    """字段序固定，每个字段内部滞后 59→0（与 qlib 的双层循环一致）。"""
    names = feature_names()
    assert names[0] == "CLOSE59" and names[59] == "CLOSE0"
    assert names[60] == "OPEN59" and names[119] == "OPEN0"
    assert names[120] == "HIGH59"
    assert names[180] == "LOW59"
    assert names[240] == "VWAP59"
    assert names[300] == "VOLUME59" and names[359] == "VOLUME0"
    assert [f for f in FIELDS] == ["CLOSE", "OPEN", "HIGH", "LOW", "VWAP", "VOLUME"]


def test_list_builtin_shape_matches_alpha158():
    """与 ``qlib_alpha.list_builtin`` 同结构（name/family/window/formula）。"""
    items = list_builtin()
    assert len(items) == 360
    for it in items[:3] + items[-3:]:
        assert set(it) == {"name", "family", "window", "formula"}
    assert items[0]["name"] == "CLOSE59" and items[0]["window"] == 59
    assert items[-1]["name"] == "VOLUME0" and items[-1]["window"] == 0
    assert "close" in items[0]["formula"]
    assert "1e-12" in items[-1]["formula"]        # volume 族的分母写法


def test_resolve_name_and_has_factor():
    assert resolve_name("CLOSE5") == ("CLOSE", 5)
    assert resolve_name("volume0") == ("VOLUME", 0)
    assert resolve_name("VWAP59") == ("VWAP", 59)
    for bad in ("CLOSE60", "CLOSE", "NOPE1", "CLOSEX"):
        with pytest.raises(KeyError):
            resolve_name(bad)
        assert has_factor(bad) is False
    assert has_factor("CLOSE0") is True


def test_compute_all_returns_360_columns():
    df = _panel()
    out = compute_all(df)
    assert all(n in out.columns for n in feature_names())
    # _prep 会额外派生 _vwap（VWAP 族的来源列），所以是 +361
    assert out.width == df.width + 361
    assert "_vwap" in out.columns


def test_compute_all_subset_families_and_lags():
    df = _panel()
    out = compute_all(df, families={"close"}, lags=(0, 1, 5))
    for n in ("CLOSE0", "CLOSE1", "CLOSE5"):
        assert n in out.columns
    assert "OPEN0" not in out.columns
    with pytest.raises(ValueError, match="滞后超出"):
        compute_all(df, lags=(60,))


# ----------------------------------------------------------- 公式口径

def test_close0_is_exactly_one():
    """``CLOSE0 = $close/$close`` 恒等于 1（哨兵）。"""
    out = compute_all(_panel(), families={"close"}, lags=(0,))
    assert np.allclose(out["CLOSE0"].to_numpy(), 1.0, atol=1e-12)


def test_volume_family_normalized_by_volume_not_close():
    """VOLUME 族的分母是**当日 volume**，不是 close —— 写错会让量纲彻底错掉。"""
    df = _panel()
    out = compute_all(df, families={"volume"}, lags=(0, 1))
    d = out.sort(["symbol", "trade_date"])
    vol = d["volume"].to_numpy()
    expect0 = vol / (vol + 1e-12)
    assert np.allclose(d["VOLUME0"].to_numpy(), expect0, atol=1e-12)
    # VOLUME1 = 前一日 volume / 当日 volume
    for _, g in d.group_by("symbol", maintain_order=True):
        v = g["volume"].to_numpy()
        v1 = g["VOLUME1"].to_numpy()
        assert np.allclose(v1[1:], v[:-1] / (v[1:] + 1e-12), atol=1e-12)
        assert np.isnan(v1[0])          # 首日无前值


def test_price_family_hand_checked():
    """价格族：``FIELD{i} = Ref($field, i) / $close``，手算对拍。"""
    df = _panel(n_days=5, n_sym=1)
    out = compute_all(df, families={"close", "open"}, lags=(0, 1, 4))
    g = out.sort("trade_date")
    close = g["close"].to_numpy()
    open_ = g["open"].to_numpy()
    assert np.allclose(g["CLOSE0"].to_numpy(), 1.0, atol=1e-12)
    assert np.allclose(g["CLOSE1"].to_numpy()[1:], close[:-1] / close[1:], atol=1e-12)
    assert np.allclose(g["CLOSE4"].to_numpy()[4:], close[:-4] / close[4:], atol=1e-12)
    assert np.allclose(g["OPEN0"].to_numpy(), open_ / close, atol=1e-12)


def test_vwap_derived_from_amount_over_volume():
    df = _panel(n_days=3, n_sym=1)
    out = compute_all(df, families={"vwap"}, lags=(0,))
    g = out.sort("trade_date")
    expect = (g["amount"] / g["volume"]) / g["close"]
    assert np.allclose(g["VWAP0"].to_numpy(), expect.to_numpy(), atol=1e-12)


def test_vwap_null_when_suspended():
    """停牌（volume=0）时 vwap 为 null → VWAP 族也为 null，不产生 inf。"""
    df = _panel(n_days=3, n_sym=1).with_columns(
        pl.when(pl.col("trade_date") == pl.col("trade_date").max())
        .then(pl.lit(0.0)).otherwise(pl.col("volume")).alias("volume"))
    out = compute_all(df, families={"vwap"}, lags=(0,))
    g = out.sort("trade_date")
    assert g["VWAP0"].null_count() == 1
    assert np.isfinite(g["VWAP0"].drop_nulls().to_numpy()).all()


def test_warmup_rows_are_null():
    """前 59 天没有 lag=59 的值 → null（不是 0，也不是压缩序列）。"""
    out = compute_all(_panel(n_days=70, n_sym=1), families={"close"}, lags=(59,))
    g = out.sort("trade_date")
    assert g["CLOSE59"][:59].null_count() == 59
    assert g["CLOSE59"][59:].null_count() == 0


def test_compute_single_returns_factor_column():
    df = _panel(n_days=5, n_sym=1)
    out = compute(df, "CLOSE2")
    assert "_factor" in out.columns
    g = out.sort("trade_date")
    close = g["close"].to_numpy()
    assert np.allclose(g["_factor"].to_numpy()[2:], close[:-2] / close[2:], atol=1e-12)


def test_missing_columns_raise():
    df = _panel().drop("amount")
    with pytest.raises(KeyError, match="缺少列"):
        compute_all(df)


# ----------------------------------------------------------- 无未来函数

def test_future_prices_do_not_change_past_features():
    """改未来价格不得改变过去特征（只用 shift(i>=0) 与当日值）。"""
    df = _panel(n_days=70, n_sym=2)
    out = compute_all(df, families={"close"}, lags=(0, 1, 10))
    days = sorted(df["trade_date"].unique().to_list())
    cut = days[40]
    mutated = df.with_columns(
        pl.when(pl.col("trade_date") > cut).then(pl.col("close") * 10.0)
        .otherwise(pl.col("close")).alias("close"))
    out2 = compute_all(mutated, families={"close"}, lags=(0, 1, 10))

    a = out.filter(pl.col("trade_date") <= cut).sort(["symbol", "trade_date"])
    b = out2.filter(pl.col("trade_date") <= cut).sort(["symbol", "trade_date"])
    for c in ("CLOSE0", "CLOSE1", "CLOSE10"):
        # equal_nan=True：预热期为 null→NaN，NaN != NaN 会让 allclose 假失败
        assert np.allclose(a[c].to_numpy(), b[c].to_numpy(), atol=1e-15,
                           equal_nan=True), c


def test_alpha360_and_alpha158_share_api_shape():
    """两个因子集的清单结构一致（同一流程可切换）。"""
    from lquant.factors.qlib_alpha import list_builtin as a158

    k360 = set(list_builtin()[0])
    k158 = set(a158()[0])
    assert k360 == k158


def test_lags_and_fields_constants_consistent():
    assert LAGS == 60 and len(FIELDS) == 6
    assert len(FIELDS) * LAGS == FEATURE_COUNT
