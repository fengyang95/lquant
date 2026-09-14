"""JQRunner 沙箱内 get_fundamentals 的绑定桥。

复用 research/dialect 的 resolve()，按当日交易日绑定 jq_shim 上下文，
保证 financial_pit 的 pub_date <= 当日 PIT 过滤不被绕过。
"""
from __future__ import annotations

from typing import Any

_STATE: dict[str, Any] = {"day": None, "universe": []}


def set_day(day, universe: list[str]) -> None:
    _STATE["day"] = day
    _STATE["universe"] = list(universe)


def make_get_fundamentals():
    from lquant.research.dialect import jq_shim

    def get_fundamentals(query, date=None):
        day = date if date is not None else _STATE["day"]
        if day is None:
            raise RuntimeError("get_fundamentals: 沙箱未绑定当前交易日")
        jq_shim.bind(jq_shim.JQContext(engine=None, trade_date=day,
                                       universe=list(_STATE["universe"])))
        return jq_shim.get_fundamentals(query, date=day)

    return get_fundamentals
