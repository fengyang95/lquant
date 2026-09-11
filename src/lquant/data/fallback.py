"""Fallback 链 + 健康度。

原则：一个源为主，比对只用于标记与降级，绝不用于取值（多源拼收益率会跳变）。
仲裁至少需要 3 个可用源 —— 2 源时只知道有分歧，无法判断谁错。
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import polars as pl

from lquant.core.errors import SourceUnavailable
from lquant.data.base import DataProvider
from lquant.data.capability import Capability


class HealthTracker:
    """连续失败自动降级，避免每次请求都先撞死源。"""

    def __init__(self, cooldown_sec: int = 300) -> None:
        self.cooldown = cooldown_sec
        self._fails: dict[str, int] = {}
        self._down_until: dict[str, float] = {}

    def ok(self, provider: str) -> None:
        self._fails.pop(provider, None)
        self._down_until.pop(provider, None)

    def fail(self, provider: str) -> None:
        n = self._fails.get(provider, 0) + 1
        self._fails[provider] = n
        self._down_until[provider] = time.monotonic() + min(self.cooldown * n, 3600)

    def available(self, provider: str) -> bool:
        t = self._down_until.get(provider)
        return t is None or time.monotonic() >= t


class FallbackProvider(DataProvider):
    def __init__(self, providers: list[DataProvider], health: HealthTracker | None = None) -> None:
        if not providers:
            raise ValueError("至少需要 1 个 provider")
        self.providers = providers
        self.health = health or HealthTracker()
        self.name = "fallback"
        # 实际服务了最近一次调用的源名（血缘用：fallback 切源后 source 不能标错）
        self.last_source: str | None = None
        self.capability = frozenset().union(*(p.capability for p in providers))

    def _route(self, cap: Capability) -> list[DataProvider]:
        cands = [p for p in self.providers if p.has(cap) and self.health.available(p.name)]
        if not cands:
            all_have = [p.name for p in self.providers if p.has(cap)]
            raise SourceUnavailable(
                "fallback",
                f"无可用源支持 {cap.value}（具备该能力但已降级: {all_have or '无'}）",
            )
        return cands

    def _call(self, cap: Capability, method: str, **kw):
        from loguru import logger

        last: Exception | None = None
        for p in self._route(cap):
            try:
                out = getattr(p, method)(**kw)
                self.health.ok(p.name)
                self.last_source = p.name
                return out
            except Exception as e:  # noqa: BLE001
                last = e
                self.health.fail(p.name)
                # 逐源记录：只留最后一个错误会让多源链的失败原因不可诊断
                logger.warning(
                    f"provider {p.name} 调用 {method} 失败，切换下一源: "
                    f"{type(e).__name__}: {e}"
                )
                continue
        raise SourceUnavailable("fallback", f"所有源均失败: {last}")

    # ---- 代理 6 个核心方法 ----
    def daily_bars(self, symbols, start, end):
        return self._call(Capability.DAILY, "daily_bars", symbols=symbols, start=start, end=end)

    _MINUTE_CAP = {
        "1min": Capability.MINUTE_1, "5min": Capability.MINUTE_5,
        "15min": Capability.MINUTE_15, "30min": Capability.MINUTE_30,
        "60min": Capability.MINUTE_60,
    }

    def minute_bars(self, symbols, start, end, freq):
        # 按频度选能力，不能一律按 minute_5 路由 —— 有的源只有 60 分钟
        cap = self._MINUTE_CAP.get(freq)
        if cap is None:
            raise ValueError(f"未知分钟频度: {freq}")
        return self._call(
            cap, "minute_bars", symbols=symbols, start=start, end=end, freq=freq,
        )

    def adj_factors(self, symbols, start, end):
        return self._call(Capability.ADJ_FACTOR, "adj_factors", symbols=symbols, start=start, end=end)

    def financial_pit(self, symbols, start, end):
        return self._call(Capability.FINANCIAL_PIT, "financial_pit", symbols=symbols, start=start, end=end)

    def securities(self, day: date | None = None):
        return self._call(Capability.REFERENCE, "securities", day=day)

    def trade_calendar(self, start, end):
        return self._call(Capability.CALENDAR, "trade_calendar", start=start, end=end)

    def etf_meta(self) -> pl.DataFrame:
        return self._call(Capability.ETF_META, "etf_meta")

    def realtime(self, symbols: list[str]) -> pl.DataFrame:
        return self._call(Capability.REALTIME, "realtime", symbols=symbols)

    def recent_daily_probe(self) -> pl.DataFrame:
        """健康检查用：拉最近一天少量数据。"""
        end = date.today()
        return self._call(
            Capability.DAILY, "daily_bars",
            symbols=["000001.SH"], start=end - timedelta(days=10), end=end,
        )
