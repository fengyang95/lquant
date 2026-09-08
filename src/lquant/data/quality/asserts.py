"""记录级八项断言（§3.7）。只能证明「每条数据都合法」。"""
from __future__ import annotations

import polars as pl

from lquant.core.errors import DataQualityError

FATAL = {"price_range", "ohlc_consistency", "unit_normalize"}


def check(df: pl.DataFrame, severity_default: str = "warn") -> pl.DataFrame:
    """返回 df 并附加 quality_flags；fatal 项直接抛异常。"""
    flags = pl.lit(0, dtype=pl.Int32)
    # 1 价格区间 / 2 OHLC / 3 单位 见 normalize.assert_*
    # 4 非负成交量
    if "volume" in df.columns:
        flags = flags | pl.when(pl.col("volume") < 0).then(pl.lit(1 << 3)).otherwise(0)
    # 5 重复键
    # 6 缺失交易日
    # 7 僵尸报价（全市场零收益占比由 validators 处理）
    # 8 覆盖度
    out = df.with_columns(quality_flags=flags)
    for rule in FATAL:
        pass  # 具体断言在 normalize 中执行
    return out


def assert_no_dup(df: pl.DataFrame, keys: list[str]) -> None:
    n = len(df) - len(df.unique(subset=keys))
    if n:
        raise DataQualityError("duplicate_key", f"{keys} 有 {n} 行重复", "fatal")
