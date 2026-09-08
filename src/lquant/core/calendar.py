"""交易日历。

A 股调休时：周末上班不是交易日，工作日放假也不是。
必须用交易所官方日历，不能用「工作日」推。
"""
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

from lquant.data.store.catalog import TradeCalendarRepo


@lru_cache(maxsize=1)
def _repo() -> TradeCalendarRepo:
    return TradeCalendarRepo()


def is_trading_day(d: date) -> bool:
    return _repo().is_trading_day(d)


def trade_days(start: date, end: date) -> list[date]:
    return _repo().range(start, end)


def prev_trade_day(d: date) -> date | None:
    days = _repo().range(d - timedelta(days=30), d - timedelta(days=1))
    return days[-1] if days else None


def next_trade_day(d: date) -> date | None:
    days = _repo().range(d + timedelta(days=1), d + timedelta(days=30))
    return days[0] if days else None
