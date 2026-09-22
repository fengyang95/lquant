import polars as pl
import pytest

from lquant.core.errors import DataQualityError
from lquant.data.normalize import assert_ohlc, assert_plausible_prices, normalize_symbols, to_yuan


def test_symbol_normalize():
    df = pl.DataFrame({"symbol": ["sh.600000", "600519", "000001.SZ"]})
    out = normalize_symbols(df)
    assert out["symbol"].to_list() == ["600000.SH", "600519.SH", "000001.SZ"]


def test_unit_convert():
    df = pl.DataFrame({"amount": [1.0]})
    assert to_yuan(df, "amount", "万元")["amount"][0] == 10000.0


def test_price_range_fatal():
    df = pl.DataFrame({"close": [100000.0]})
    with pytest.raises(DataQualityError):
        assert_plausible_prices(df)


def test_ohlc_check():
    df = pl.DataFrame({"open": [10.0], "high": [9.0], "low": [11.0], "close": [10.0]})
    with pytest.raises(DataQualityError):
        assert_ohlc(df)


def test_price_range_suspension_exempt():
    """停牌行（is_suspended=True）价格 0/null 豁免：baostock 停牌行 OHLC 全 0。"""
    df = pl.DataFrame({
        "open": [10.0, 0.0, 0.0],
        "high": [10.5, 0.0, 0.0],
        "low": [9.5, 0.0, 0.0],
        "close": [10.2, 0.0, 0.0],
        "is_suspended": [False, True, True],
    })
    assert_plausible_prices(df)  # 不抛


def test_price_range_null_still_fatal_when_not_suspended():
    df = pl.DataFrame({
        "close": [10.0, None],
        "is_suspended": [False, False],
    })
    with pytest.raises(DataQualityError):
        assert_plausible_prices(df)


def test_ohlc_suspension_exempt():
    df = pl.DataFrame({
        "open": [10.0, 0.0],
        "high": [10.5, 0.0],
        "low": [9.5, 0.0],
        "close": [10.2, 0.0],
        "is_suspended": [False, True],
    })
    assert_ohlc(df)  # 不抛
