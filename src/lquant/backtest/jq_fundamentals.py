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
        day = _STATE["day"]
        if day is None:
            raise RuntimeError("get_fundamentals: 沙箱未绑定当前交易日")
        if date is not None:
            # G6:显式 date 钳制到当前交易日,策略传未来日期也绝不能看到未来披露
            day = min(date, day)
        jq_shim.bind(jq_shim.JQContext(engine=None, trade_date=day,
                                       universe=list(_STATE["universe"])))
        return jq_shim.get_fundamentals(query, date=day)

    return get_fundamentals
