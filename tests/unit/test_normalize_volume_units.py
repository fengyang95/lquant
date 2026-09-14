"""日线 volume/amount 单位归一纯函数测试（入湖统一口径：volume=股、amount=元）。

各 provider 源单位（考证见各 provider 模块级常量注释）：
- baostock: volume=股、amount=元（系数 1.0，显式声明）
- akshare:  成交量=手（×100）、成交额=元（×1）
- tushare:  vol=手（×100）、amount=千元（×1000）
"""
from __future__ import annotations

import polars as pl
import pytest

from lquant.core.errors import DataQualityError
from lquant.data.normalize import UNIT_TO_YUAN, VOLUME_TO_SHARES, scale_unit


def _ratio(df: pl.DataFrame) -> float:
    """amount / (close × volume) —— UNIT_MISMATCH 断言的口径，正确实现应恒为 1。"""
    close = df["close"][0]
    volume = df["volume"][0]
    amount = df["amount"][0]
    assert volume and close and amount
    return amount / (close * volume)


# ---------------------------------------------------------------- 系数表
def test_volume_to_shares_table() -> None:
    assert VOLUME_TO_SHARES == {"股": 1.0, "手": 100.0, "万手": 1e6}


def test_amount_table_has_qianyuan() -> None:
    # tushare amount 是千元 —— UNIT_TO_YUAN 必须覆盖
    assert UNIT_TO_YUAN["千元"] == 1e3
    assert UNIT_TO_YUAN["元"] == 1.0


# ------------------------------------------------- provider 源单位 → 湖内口径
def test_baostock_volume_shares_amount_yuan_passthrough() -> None:
    """baostock：volume 已是股、amount 已是元 —— 系数 1.0，值不变。"""
    df = pl.DataFrame({
        "close": [10.0],
        "volume": [10_000.0],   # 股
        "amount": [100_000.0],  # 元
    })
    out = scale_unit(scale_unit(df, col="volume", unit="股"), col="amount", unit="元")
    assert out["volume"].to_list() == [10_000.0]
    assert out["amount"].to_list() == [100_000.0]
    assert _ratio(out) == pytest.approx(1.0)


def test_akshare_volume_hand_to_shares() -> None:
    """akshare 东财日线：成交量=手（×100），成交额已是元（×1）。"""
    df = pl.DataFrame({
        "close": [10.0],
        "成交量": [1_000.0],   # 手
        "成交额": [1_000_000.0],  # 元（= close × 1000手×100 = 10 × 100,000 股）
    })
    out = scale_unit(df, col="成交量", unit="手")
    assert out["成交量"].to_list() == [100_000.0]  # → 股
    out = scale_unit(out.rename({"成交量": "volume"}), col="成交额", unit="元")
    assert out["成交额"].to_list() == [1_000_000.0]
    assert _ratio(out.rename({"成交额": "amount"})) == pytest.approx(1.0)


def test_tushare_vol_hand_amount_qianyuan() -> None:
    """tushare pro：vol=手（×100）、amount=千元（×1000）。"""
    df = pl.DataFrame({
        "close": [10.0],
        "vol": [100.0],       # 手
        "amount": [100.0],    # 千元（= 10 元 × 10,000 股 = 100,000 元）
    })
    out = scale_unit(df, col="vol", unit="手")
    out = scale_unit(out, col="amount", unit="千元")
    out = out.rename({"vol": "volume"})
    assert out["volume"].to_list() == [10_000.0]    # 100 手 → 10000 股
    assert out["amount"].to_list() == [100_000.0]   # 100 千元 → 1e5 元
    assert _ratio(out) == pytest.approx(1.0)


# ---------------------------------------------------------------- 边界
def test_scale_unit_null_zero_negative_passthrough() -> None:
    df = pl.DataFrame({"volume": [None, 0.0, -50.0, 100.0]})
    out = scale_unit(df, col="volume", unit="手")
    assert out["volume"].to_list() == [None, 0.0, -5_000.0, 10_000.0]


def test_scale_unit_empty_df() -> None:
    df = pl.DataFrame({"volume": []}, schema={"volume": pl.Float64})
    assert scale_unit(df, col="volume", unit="手")["volume"].len() == 0


def test_scale_unit_missing_column_passthrough() -> None:
    df = pl.DataFrame({"close": [10.0]})
    assert scale_unit(df, col="volume", unit="手").equals(df)


def test_scale_unit_unknown_unit_raises() -> None:
    df = pl.DataFrame({"volume": [1.0]})
    with pytest.raises(DataQualityError):
        scale_unit(df, col="volume", unit="桶")


def test_scale_unit_does_not_mutate_input() -> None:
    df = pl.DataFrame({"volume": [100.0]})
    out = scale_unit(df, col="volume", unit="手")
    assert df["volume"].to_list() == [100.0]
    assert out is not df
