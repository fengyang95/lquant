"""JQRunner 沙箱内 get_fundamentals 的绑定桥。

复用 research/dialect 的 resolve()，按当日交易日绑定 jq_shim 上下文，
保证 financial_pit 的 pub_date <= 当日 PIT 过滤不被绕过。

G9:状态从模块级全局改为 per-runner 实例（JQFundamentalsState），
同进程多个 JQRunner 不再互相污染（前视跨 runner 泄漏）。
模块级 set_day/make_get_fundamentals 保留为兼容包装，委托专用全局实例。
"""
from __future__ import annotations

from typing import Any


class JQFundamentalsState:
    """per-runner 的 get_fundamentals 绑定状态（当前交易日 + 股票池）。"""

    def __init__(self) -> None:
        self.day: Any = None
        self.universe: list[str] = []

    def set_day(self, day, universe: list[str]) -> None:
        self.day = day
        self.universe = list(universe)

    def make_get_fundamentals(self):
        from datetime import date as _date

        from lquant.research.dialect import jq_shim

        def get_fundamentals(query, date=None):
            day = self.day
            if day is None:
                raise RuntimeError("get_fundamentals: 沙箱未绑定当前交易日")
            if date is not None:
                # G6:显式 date 钳制到当前交易日,策略传未来日期也绝不能看到未来披露。
                # G20a:必须先归一成 date —— 历史上直接 min(str, date) 会抛
                # TypeError: '<' not supported between 'datetime.date' and 'str'，
                # 而聚宽用户习惯传 '2025-08-01' 这种字符串。
                req = date if isinstance(date, _date) else _date.fromisoformat(str(date)[:10])
                day = min(req, day)
            jq_shim.bind(jq_shim.JQContext(engine=None, trade_date=day,
                                           universe=list(self.universe)))
            return jq_shim.get_fundamentals(query, date=day)

        return get_fundamentals


# 兼容包装：委托专用全局实例（JQRunner 均已改持 per-runner 实例，不再读这里）。
_GLOBAL_STATE = JQFundamentalsState()


def set_day(day, universe: list[str]) -> None:
    _GLOBAL_STATE.set_day(day, universe)


def make_get_fundamentals():
    return _GLOBAL_STATE.make_get_fundamentals()
