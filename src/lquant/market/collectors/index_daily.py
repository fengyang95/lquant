"""指数日线（close 时点）。

主要用途：回测基准（替代「全市场等权」这种无指数时的近似）、
大盘看板的指数对比。BaoStock 对指数代码同样走 query_history_k_data_plus，
免费且带 pre_close —— 不用东财反推。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import polars as pl

from lquant.core.types import now_cn

__all__ = ["INDEX_POOL", "fetch_index_daily"]

# 常看指数池：symbol -> 名称（补齐覆盖从这里扩）
INDEX_POOL: dict[str, str] = {
    "000001.SH": "上证指数",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
}

_SCHEMA = {
    "trade_date": pl.Date, "symbol": pl.Utf8, "name": pl.Utf8,
    "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
    "pre_close": pl.Float64, "volume": pl.Float64, "amount": pl.Float64,
    "collected_at": pl.Datetime("us"),
}


def _empty() -> pl.DataFrame:
    return pl.DataFrame(schema=_SCHEMA)


def fetch_index_daily(trade_date=None, start: str | None = None,
                      end: str | None = None, *, demo: bool = False) -> pl.DataFrame:
    """拉指数日线。缺省窗口 = 近 30 个自然日（调度器每日增量足够）。"""
    from datetime import timedelta

    end_d = date.fromisoformat(end) if end else (datetime.now().date())
    start_d = date.fromisoformat(start) if start else end_d - timedelta(days=30)

    if demo:
        return _demo(start_d, end_d)

    try:
        from lquant.data.providers import get_provider

        provider = get_provider()
        target = provider.providers[0] if hasattr(provider, "providers") else provider
        df = target.daily_bars(list(INDEX_POOL), start_d, end_d)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 指数日线拉取失败（可 demo 模式验证链路）: {e}")
        return _empty()

    if not len(df):
        return _empty()
    # 指数名映射 + 清掉 sec_type 等个股专用列，对齐 index_daily 表结构
    keep = df.select(
        "trade_date", "symbol", "open", "high", "low", "close", "pre_close",
        "volume", "amount",
    ).with_columns(
        pl.col("symbol").replace_strict(INDEX_POOL, default=None).alias("name"),
        collected_at=pl.lit(now_cn(), dtype=pl.Datetime("us")),
    )
    return keep.select(list(_SCHEMA)).sort(["symbol", "trade_date"])


def _demo(start_d: date, end_d: date) -> pl.DataFrame:
    import random

    rows = []
    for sym, name in INDEX_POOL.items():
        random.seed(hash(f"{sym}{start_d}") % (2**31))
        close = {"000001.SH": 3200.0, "000300.SH": 3900.0, "000905.SH": 5200.0,
                 "000852.SH": 5800.0, "399001.SZ": 10500.0, "399006.SZ": 2100.0}[sym]
        d = start_d
        while d <= end_d:
            if d.weekday() < 5:
                pre = close
                close = round(max(close * (1 + random.uniform(-0.02, 0.02)), 100.0), 2)
                rows.append({
                    "trade_date": d, "symbol": sym, "name": name,
                    "open": round(pre * (1 + random.uniform(-0.008, 0.008)), 2),
                    "high": max(pre, close) * 1.004, "low": min(pre, close) * 0.996,
                    "close": close, "pre_close": pre,
                    "volume": random.uniform(1e8, 3e9), "amount": random.uniform(1e10, 8e11),
                    "collected_at": now_cn(),
                })
            d += timedelta(days=1)
    return pl.DataFrame(rows, schema=_SCHEMA)
