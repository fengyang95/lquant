"""DataProvider 协议。

每个具体 Provider 实现 6 个核心方法 + 声明 capability。
切换一个源的工作量 = 写一个 adapter + 改 yaml 优先级 + 双源对拍。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

import polars as pl

from lquant.data.capability import Capability


class DataProvider(ABC):
    name: str = "base"
    # 子类声明支持的能力；路由层据此选源
    capability: frozenset[Capability] = frozenset()

    def has(self, cap: Capability | str) -> bool:
        c = Capability.parse(cap) if isinstance(cap, str) else cap
        return c in self.capability

    def require(self, cap: Capability | str) -> None:
        """不支持就显式报错，绝不静默返回空。"""
        from lquant.core.errors import CapabilityMissing

        c = Capability.parse(cap) if isinstance(cap, str) else cap
        if not self.has(c):
            raise CapabilityMissing(self.name, c.value)

    # ---- 6 个核心方法 ----
    @abstractmethod
    def daily_bars(self, symbols: list[str], start: date, end: date) -> pl.DataFrame:
        """日线，schema 见 data.schema.DAILY_BAR。"""

    @abstractmethod
    def minute_bars(self, symbols: list[str], start: date, end: date, freq: str) -> pl.DataFrame: ...

    @abstractmethod
    def adj_factors(self, symbols: list[str], start: date, end: date) -> pl.DataFrame: ...

    @abstractmethod
    def financial_pit(self, symbols: list[str], start: date, end: date) -> pl.DataFrame:
        """必须返回 stat_date 与 pub_date 双日期。"""

    @abstractmethod
    def securities(self) -> pl.DataFrame: ...

    @abstractmethod
    def trade_calendar(self, start: date, end: date) -> pl.DataFrame: ...

    # ---- 可选（默认抛 CapabilityMissing）----
    def etf_meta(self) -> pl.DataFrame:
        self.require(Capability.ETF_SPOT)
        raise NotImplementedError

    def realtime(self, symbols: list[str]) -> pl.DataFrame:
        self.require(Capability.REALTIME)
        raise NotImplementedError

    def health(self) -> bool:
        return True
