"""能力声明。

Fallback 链解决不了「某源根本没有 1 分钟线」——
必须由 Provider 声明能力，路由层按需选源。
"""
from __future__ import annotations

from enum import StrEnum


class Capability(StrEnum):
    DAILY = "daily"
    MINUTE_1 = "minute_1"
    MINUTE_5 = "minute_5"
    MINUTE_15 = "minute_15"
    MINUTE_30 = "minute_30"
    MINUTE_60 = "minute_60"
    ADJ_FACTOR = "adj_factor"
    INDEX_DAILY = "index_daily"
    FINANCIAL_PIT = "financial_pit"       # 必须含 pub_date，否则不算 PIT
    CORPORATE_ACTION = "corporate_action"
    CALENDAR = "calendar"
    REFERENCE = "reference"
    INDUSTRY = "industry"
    ETF_DAILY = "etf_daily"
    ETF_MINUTE_60 = "etf_minute_60"
    ETF_SPOT = "etf_spot"
    ETF_META = "etf_meta"         # 静态元数据：跟踪指数 / 费率 / T+N
    ETF_IOPV = "etf_iopv"
    ETF_SHARE = "etf_share"
    REALTIME = "realtime"
    MONEY_FLOW = "money_flow"
    LIMIT_UP = "limit_up"
    DRAGON_TIGER = "dragon_tiger"
    SECTOR = "sector"

    @classmethod
    def parse(cls, s: str) -> Capability:
        try:
            return cls(s)
        except ValueError:
            raise ValueError(f"未知能力: {s}，可选: {[c.value for c in cls]}") from None
