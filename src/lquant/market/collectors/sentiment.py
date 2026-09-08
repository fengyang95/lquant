"""市场情绪：涨停数 / 炸板率 / 最高连板 / 昨日涨停今日表现。

四个指标里**炸板率最有信息量**：
- 涨停数多 + 炸板率低 → 情绪健康，赚钱效应真实
- 涨停数多 + 炸板率高 → 资金在高位派发，次日大概率回落
- 昨涨停今日收益为负 → 打板策略全面亏损，短期应降低仓位

情绪分（0~100）是四者的加权合成，用于看板上的一个数字总览。
它不是因子，不能直接拿来选股 —— 它衡量的是"市场状态"，
正确用法是作为策略的仓位调节开关。
"""
from __future__ import annotations

from datetime import date

import polars as pl

from lquant.core.types import now_cn
from lquant.market.collectors.limit_up import (
    fetch_broken_pool,
    fetch_limit_down_pool,
    fetch_limit_up_pool,
)

__all__ = ["compute_sentiment", "sentiment_score", "YesterdayLimitUp"]


class YesterdayLimitUp:
    """昨日涨停股今日表现。

    这个指标需要跨两日数据，所以单独封装：
    用昨天的涨停池 + 今天的行情，算它们的今日平均收益。
    """

    def __init__(self, price_lookup) -> None:
        self.price_lookup = price_lookup      # (date) -> DataFrame[symbol, close]

    def compute(self, prev_day: date, today: date, prev_pool: pl.DataFrame) -> float | None:
        if prev_pool is None or not len(prev_pool):
            return None
        px = self.price_lookup(today)
        if px is None or not len(px):
            return None
        joined = prev_pool.select("symbol").join(px, on="symbol", how="inner")
        if not len(joined) or "change_pct" not in joined.columns:
            return None
        return float(joined["change_pct"].mean())


def compute_sentiment(trade_date: date | str | None = None, *, demo: bool = False,
                      yesterday_return: float | None = None,
                      up_count: int | None = None,
                      down_count: int | None = None,
                      total_amount: float | None = None) -> pl.DataFrame:
    """汇总当日情绪。涨停/跌停/炸板三个池各采一次。"""
    zt = fetch_limit_up_pool(trade_date, demo=demo)
    zb = fetch_broken_pool(trade_date, demo=demo)
    dt = fetch_limit_down_pool(trade_date, demo=demo)

    d = zt["trade_date"][0] if len(zt) else (
        zb["trade_date"][0] if len(zb) else
        (dt["trade_date"][0] if len(dt) else date.today()))

    n_zt, n_zb, n_dt = len(zt), len(zb), len(dt)
    broken_rate = n_zb / max(n_zt + n_zb, 1)

    max_consec = 0
    if len(zt) and "limit_up_type" in zt.columns:
        # 连板高度用行业内的最大连板近似：真实连板数需要历史池，这里取炸板次数+1 的下界
        max_consec = int(zt["open_count"].max() or 0) if "open_count" in zt.columns else 0

    amount = total_amount
    if amount is None and len(zt) and "amount" in zt.columns:
        amount = float(zt["amount"].sum())

    score = sentiment_score(n_zt, n_zb, n_dt, yesterday_return)

    return pl.DataFrame([{
        "trade_date": d,
        "limit_up_count": n_zt,
        "limit_down_count": n_dt,
        "broken_count": n_zb,
        "broken_rate": round(broken_rate, 4),
        "max_consecutive": max_consec,
        "yesterday_limit_up_return": yesterday_return,
        "up_count": up_count,
        "down_count": down_count,
        "total_amount": amount,
        "sentiment_score": score,
        "collected_at": now_cn(),
    }])


def sentiment_score(n_limit_up: int, n_broken: int, n_limit_down: int,
                    yesterday_return: float | None = None) -> float:
    """情绪分 0~100。

    权重是经验值，含义如下：
    - 涨停数：0~80 家映射到 0~50 分（超过 80 家已是极度亢奋，不再加分）
    - 炸板率：越高越扣分，最多扣 25 分
    - 跌停数：越多越扣分，最多扣 15 分
    - 昨涨停今日收益：正加分负减分，最多 ±10 分
    """
    base = min(n_limit_up / 80.0, 1.0) * 50.0

    broken_rate = n_broken / max(n_limit_up + n_broken, 1)
    penalty_broken = min(broken_rate, 1.0) * 25.0
    penalty_down = min(n_limit_down / 20.0, 1.0) * 15.0

    bonus = 0.0
    if yesterday_return is not None:
        # 昨涨停今日收益 ±3% 对应 ±10 分
        bonus = max(-10.0, min(10.0, yesterday_return / 0.03 * 10.0))

    return round(max(0.0, min(100.0, base - penalty_broken - penalty_down + bonus)), 2)
