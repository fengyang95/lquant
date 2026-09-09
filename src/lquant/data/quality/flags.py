"""quality_flags 位掩码（细化方案 §3.8.5）：标记而非删除。

数据问题往往事后才看清，删掉就找不回来了 —— 坏数据打标落库，
下游因子计算按需过滤：

    df.filter(pl.col("quality_flags") == 0)              # 严格模式
    df.filter((pl.col("quality_flags") & 0b10011) == 0)  # 容忍停牌填充
"""
from __future__ import annotations

import polars as pl

PRICE_OUT_OF_RANGE = 1 << 0   # 价格越界（0.1–10000 元外）
ADJ_ANOMALY = 1 << 1          # 复权异常（收益率越涨跌停 / 因子跳变）
ZOMBIE = 1 << 2               # 疑似僵尸（零成交零收益、源站未更新）
CROSS_SOURCE_DIFF = 1 << 3    # 跨源不一致（Phase 2 跨源印证打标）
SUSPENSION_FILL = 1 << 4      # 停牌填充（零成交但价格在动）
OHLC_CONFLICT = 1 << 5        # high/low 与 open/close 矛盾
NEG_QTY = 1 << 6              # 负成交量 / 负成交额
UNIT_MISMATCH = 1 << 7        # 单位错（amount 与 close*volume 差数量级）
PRE_CLOSE_MISSING = 1 << 8    # 昨收缺失（上市首日合法，warn 不阻断）
NEW_LISTING = 1 << 9          # 新股（上市未满 min_listed_days，波动/涨跌停口径特殊）
SUSPENDED = 1 << 10           # 停牌（当日零成交）
ST_RISK = 1 << 11             # ST 风险警示（特殊涨跌幅限制，需单独处理）

__all__ = ["PRICE_OUT_OF_RANGE", "ADJ_ANOMALY", "ZOMBIE", "CROSS_SOURCE_DIFF",
           "SUSPENSION_FILL", "OHLC_CONFLICT", "NEG_QTY", "UNIT_MISMATCH",
           "PRE_CLOSE_MISSING", "NEW_LISTING", "SUSPENDED", "ST_RISK",
           "or_flags", "hit"]


def hit(mask: int, condition: pl.Expr) -> pl.Expr:
    """condition 为真时返回位掩码，否则 0 —— 供 or_flags 合并。"""
    return pl.when(condition).then(pl.lit(mask, dtype=pl.Int32)).otherwise(0)


def or_flags(df: pl.DataFrame, *masks: pl.Expr) -> pl.DataFrame:
    """把若干条件表达式（命中 → 位掩码 int，未命中 → 0）合并进 quality_flags。"""
    flags = (pl.col("quality_flags") if "quality_flags" in df.columns
             else pl.lit(0, dtype=pl.Int32))
    for m in masks:
        flags = flags | m
    return df.with_columns(quality_flags=flags.cast(pl.Int32))
