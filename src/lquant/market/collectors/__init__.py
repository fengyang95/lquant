"""采集器注册表。

注册表驱动的意义：新增一个采集器，调度器、API、CLI、看板前端
全都自动感知，不用改任何一处调用代码。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date

import polars as pl

from lquant.core.registry import Registry

COLLECTORS: Registry = Registry("collectors")


def collector(name: str, label: str = "", table: str = "", schedule: str = "close",
              critical: bool = False, **meta) -> Callable:
    """注册采集器。

    schedule : 采集时点 —— close(15:05) / evening(18:00) / preopen(09:00) / intraday
    critical : 失败是否必须告警。涨停池为 True（数据不可回溯）。
    """
    return COLLECTORS.register(name, {"name": name, "label": label or name,
                                      "table": table, "schedule": schedule,
                                      "critical": critical, **meta})


def run(name: str, trade_date: date | str | None = None, **kw) -> pl.DataFrame:
    return COLLECTORS.get(name)(trade_date=trade_date, **kw)


def run_all(schedule: str | None = None, trade_date: date | str | None = None,
            **kw) -> dict[str, pl.DataFrame]:
    """按调度时点批量执行。单个采集器失败不影响其他。"""
    out: dict[str, pl.DataFrame] = {}
    for k in COLLECTORS:
        meta = COLLECTORS.meta(k)
        if schedule and meta.get("schedule") != schedule:
            continue
        try:
            out[k] = COLLECTORS.get(k)(trade_date=trade_date, **kw)
        except Exception as e:  # noqa: BLE001
            out[k] = pl.DataFrame()
            if meta.get("critical"):
                raise
            print(f"[warn] 采集器 {k} 失败: {e}")
    return out


def list_collectors() -> list[dict]:
    return COLLECTORS.describe()


from lquant.market.collectors import limit_up, money_flow, sector, sentiment  # noqa: E402,F401
from lquant.market.collectors.dragon_tiger import fetch_dragon_tiger  # noqa: E402
from lquant.market.collectors.index_daily import INDEX_POOL, fetch_index_daily  # noqa: E402,F401
from lquant.market.collectors.limit_up import (  # noqa: E402
    fetch_broken_pool,
    fetch_limit_down_pool,
    fetch_limit_up_pool,
)
from lquant.market.collectors.money_flow import fetch_money_flow, fetch_northbound  # noqa: E402
from lquant.market.collectors.sector import fetch_concepts, fetch_sectors  # noqa: E402
from lquant.market.collectors.sentiment import compute_sentiment, sentiment_score  # noqa: E402

collector("limit_up_pool", label="涨停池", table="limit_up_pool",
          schedule="close", critical=True)(fetch_limit_up_pool)
collector("limit_down_pool", label="跌停池", table="limit_down_pool",
          schedule="close")(fetch_limit_down_pool)
collector("broken_pool", label="炸板池", table="limit_up_pool",
          schedule="close", critical=True)(fetch_broken_pool)
collector("money_flow", label="个股资金流", table="money_flow",
          schedule="close")(fetch_money_flow)
collector("sector", label="板块行情", table="sector_daily",
          schedule="close")(fetch_sectors)
collector("northbound", label="北向资金", table="northbound_flow",
          schedule="evening")(fetch_northbound)
collector("sentiment", label="市场情绪", table="sentiment_daily",
          schedule="close")(compute_sentiment)
collector("dragon_tiger", label="龙虎榜", table="dragon_tiger",
          schedule="evening")(fetch_dragon_tiger)
collector("index_daily", label="指数日线", table="index_daily",
          schedule="close")(fetch_index_daily)

__all__ = ["COLLECTORS", "collector", "run", "run_all", "list_collectors",
           "fetch_limit_up_pool", "fetch_limit_down_pool", "fetch_broken_pool",
           "fetch_money_flow", "fetch_northbound", "fetch_sectors", "fetch_concepts",
           "compute_sentiment", "sentiment_score", "fetch_dragon_tiger",
           "fetch_index_daily", "INDEX_POOL"]
