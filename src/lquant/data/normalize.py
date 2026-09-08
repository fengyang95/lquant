"""归一化：代码格式 / 单位 / 列名 / 断言。

切换数据源时，80% 的 bug 出在这里 —— 三种代码格式、元/万元/亿元混用。
"""
from __future__ import annotations

import polars as pl

from lquant.core.errors import DataQualityError
from lquant.core.types import parse_symbol

# 常见列名别名 → 内部列名
ALIASES = {
    "date": "trade_date", "trade_date": "trade_date", "datetime": "ts", "time": "ts",
    "股票代码": "symbol", "证券代码": "symbol", "code": "symbol", "symbol": "symbol",
    "开盘": "open", "open": "open", "最高": "high", "high": "high",
    "最低": "low", "low": "low", "收盘": "close", "close": "close",
    "成交量": "volume", "vol": "volume", "volume": "volume",
    "成交额": "amount", "amount": "amount", "turnover": "amount",
    "涨跌幅": "pct_chg", "pctChg": "pct_chg",
    "昨收": "pre_close", "preClose": "pre_close", "pre_close": "pre_close",
}

UNIT_TO_YUAN = {"元": 1.0, "万元": 1e4, "亿": 1e8, "亿元": 1e8, "百万元": 1e6}


def rename_columns(df: pl.DataFrame) -> pl.DataFrame:
    mapping = {c: ALIASES[c] for c in df.columns if c in ALIASES}
    return df.rename(mapping)


def normalize_symbols(df: pl.DataFrame, col: str = "symbol") -> pl.DataFrame:
    """把源站的 sh.600000 / 600000 / 600519.SH 统一成 600000.SH。"""
    return df.with_columns(
        pl.col(col).map_elements(lambda s: str(parse_symbol(str(s))), return_dtype=pl.Utf8)
    )


def to_yuan(df: pl.DataFrame, col: str, unit: str) -> pl.DataFrame:
    factor = UNIT_TO_YUAN.get(unit)
    if factor is None:
        raise DataQualityError("unit_normalize", f"未知金额单位: {unit}")
    return df.with_columns((pl.col(col) * factor).alias(col))


def assert_plausible_prices(df: pl.DataFrame, cols: tuple[str, ...] = ("open", "high", "low", "close")) -> None:
    """fatal 级断言：价格应在 0.1 – 10000 元。"""
    exprs = []
    for c in cols:
        if c in df.columns:
            bad = df.filter((pl.col(c) <= 0) | (pl.col(c) > 10000) | pl.col(c).is_null())
            if len(bad):
                exprs.append(f"{c}: {len(bad)} 行越界")
    if exprs:
        raise DataQualityError("price_range", "; ".join(exprs))


def assert_ohlc(df: pl.DataFrame) -> None:
    """fatal 级：high >= max(open, close)，low <= min(open, close)。"""
    bad = df.filter(
        (pl.col("high") < pl.max_horizontal("open", "close"))
        | (pl.col("low") > pl.min_horizontal("open", "close"))
        | (pl.col("high") < pl.col("low"))
    )
    if len(bad):
        raise DataQualityError("ohlc_consistency", f"{len(bad)} 行 OHLC 矛盾")
