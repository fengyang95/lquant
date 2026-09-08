"""JoinQuant API 兼容层。

方言只翻译 API 调用，撮合/费率/T+N 全由内核加 RuleSet 决定 ——
所以 JQ 策略跑出来的成本与原生策略完全一致。
"""
from __future__ import annotations

from datetime import date

import polars as pl


class JQContext:
    """运行时上下文，由方言适配器注入。"""

    def __init__(self, engine, trade_date: date, universe: list[str]) -> None:
        self.engine = engine
        self.current_dt = trade_date
        self.universe = universe
        self.portfolio = None


def attribute_history(security, count=1, unit="1d", fields=("open", "close"),
                      df=True, skip_paused=False, fq="pre"):
    """⚠️ 返回的是【上一单位时间】为止的数据，不含当前 bar。"""
    from lquant.data.store.parquet import read_daily

    lf = read_daily([security])
    out = (lf.filter(pl.col("trade_date") <= _ctx().current_dt)
             .sort("trade_date").tail(count).collect())
    return out.to_pandas() if df else out


def history(count=1, unit="1d", field="avg", security_list=None, df=True,
            skip_paused=False, fq="pre"):
    raise NotImplementedError("M6b")


def get_fundamentals(query, date=None):
    """必须走 PIT：pub_date <= 当前日。"""
    raise NotImplementedError("M6b：先实现 financial_pit 的 pub_date 过滤")


def order_target_value(security, value):
    _ctx().engine.order_target_value(security, value)


def order_target_percent(security, percent):
    _ctx().engine.order_target_percent(security, percent)


def order(security, amount):
    _ctx().engine.order(security, amount)


def get_all_securities(types=None, date=None):
    from lquant.data.store.catalog import SecurityRepo

    return pl.DataFrame({"symbol": SecurityRepo().active_symbols()}).to_pandas()


def get_index_stocks(index_symbol, date=None):
    raise NotImplementedError("需要指数成分表")


def get_trade_days(start_date=None, end_date=None, count=None):
    from lquant.core.calendar import trade_days

    return trade_days(start_date, end_date)


def log(*args):
    from loguru import logger

    logger.info(" ".join(map(str, args)))


_CURRENT: JQContext | None = None


def _ctx() -> JQContext:
    if _CURRENT is None:
        raise RuntimeError("JQ 上下文未初始化")
    return _CURRENT


def bind(ctx: JQContext) -> None:
    global _CURRENT
    _CURRENT = ctx
