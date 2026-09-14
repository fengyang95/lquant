"""分钟线入库（默认 60min，中证 800 池 + 断点续传）。

为什么默认 60min 而不是 1min：
- 1min 全市场 10 年 ≈ 数 TB，本地盘扛不住；BaoStock 也无 1min
- 60min 足够支撑日内择时研究和 T+0 ETF 策略
- 需要 1min 时换 mootdx（通达信 TCP，不封 IP）增量补
"""
from __future__ import annotations

from datetime import date

import polars as pl

from lquant.core.types import today_cn
from lquant.data.ingest.checkpoint import Checkpoint
from lquant.data.store.parquet import write_minute


def backfill_minute(
    symbols: list[str],
    start: date | str = "2020-01-01",
    end: date | str | None = None,
    freq: str = "60min",
    batch: int = 10,
) -> int:
    from loguru import logger

    from lquant.data.providers import get_provider

    if freq not in ("5min", "15min", "30min", "60min"):
        raise ValueError(f"BaoStock 无 {freq}，1min 请用 mootdx")

    start_d = start if isinstance(start, date) else date.fromisoformat(start)
    end_d = end if isinstance(end, date) else (date.fromisoformat(end) if end else today_cn())

    provider = get_provider()
    target = provider.providers[0] if hasattr(provider, "providers") else provider

    cp = Checkpoint(f"minute_{freq}")
    todo = cp.remaining(list(symbols))
    logger.info(f"分钟线回填 {freq} {len(todo)} 只（已完成 {len(cp)}）")

    done = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i : i + batch]
        try:
            df: pl.DataFrame = target.minute_bars(chunk, start_d, end_d, freq)
        except Exception as e:  # noqa: BLE001
            # 失败批不标记完成：否则 transient 网络错误会把这批标的永久
            # 记为 done，重跑全部跳过 —— 分钟线静默缺失（同 financial.py）。
            logger.warning(f"批次 {i} 失败，未标记（重跑将重试）: {e}")
            continue
        if len(df):
            write_minute(df)
        cp.mark(chunk)
        done += len(chunk)
        if done % 50 == 0:
            logger.info(f"  分钟线进度 {done}/{len(todo)}")
    return done


__all__ = ["backfill_minute"]
