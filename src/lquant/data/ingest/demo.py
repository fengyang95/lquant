"""演示数据生成器：无网络环境下的开箱即用。

为什么需要它：
- BaoStock 走原始 TCP（常被公司网络/代理 TUN 阻断），东财 HTTP 也可能被拦。
- 平台的价值链路（看板/因子/回测）不该被数据源卡死。
- 合成数据格式与真实数据完全一致（同一 schema、同一质量断言），
  换真数据时零改动 —— `lq data sync` 直接覆盖。

方法：几何布朗运动 + 跳空，代码全部用真实存在的格式（600xxx.SH / 000xxx.SZ /
510xxx.SH ETF），但数据是合成的。**绝不用于真实回测结论。**
"""
from __future__ import annotations

import numpy as np
import polars as pl

from lquant.core.types import now_cn
from lquant.data.store.catalog import EtfMetaRepo, SecurityRepo, TradeCalendarRepo
from lquant.data.store.parquet import write_daily

STOCK_CODES = [
    ("600000.SH", "浦发银行"), ("600036.SH", "招商银行"), ("600519.SH", "贵州茅台"),
    ("600887.SH", "伊利股份"), ("601318.SH", "中国平安"), ("601899.SH", "紫金矿业"),
    ("000001.SZ", "平安银行"), ("000002.SZ", "万科A"), ("000333.SZ", "美的集团"),
    ("000651.SZ", "格力电器"), ("002594.SZ", "比亚迪"), ("300750.SZ", "宁德时代"),
    ("688111.SH", "金山办公"), ("603259.SH", "药明康德"), ("600276.SH", "恒瑞医药"),
    ("000858.SZ", "五粮液"), ("002415.SZ", "海康威视"), ("600900.SH", "长江电力"),
    ("601088.SH", "中国神华"), ("600030.SH", "中信证券"),
]

# (代码, 名称, sellable_after_days)：T+0/T+1 用真实规则标注
ETF_CODES = [
    ("510300.SH", "沪深300ETF", 1), ("510050.SH", "上证50ETF", 1),
    ("588000.SH", "科创50ETF", 1), ("159915.SZ", "创业板ETF", 1),
    ("513100.SH", "纳指ETF", 0), ("513050.SH", "中概互联ETF", 0),
    ("512880.SH", "证券ETF", 1), ("512690.SH", "酒ETF", 1),
    ("511260.SH", "十年国债ETF", 0), ("518880.SH", "黄金ETF", 0),
]


def _calendar(start: str, end: str) -> pl.DataFrame:
    import pandas as pd

    bdays = pd.bdate_range(start, end)   # 工作日近似交易日（节假日不影响链路验证）
    return pl.DataFrame({
        # pandas DatetimeIndex.date 是 object 数组，必须显式 cast 成 Date
        "trade_date": pl.Series(list(bdays.date), dtype=pl.Date),
        "is_open": True,
        "exchange": "SSE",
    })


def _synth_bars(symbol: str, dates: list, seed: int, base_price: float,
                sec_type: str) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    n = len(dates)
    ret = rng.normal(0.0003, 0.25 / np.sqrt(244), n)   # 年化波动 25%
    close = base_price * np.exp(np.cumsum(ret))
    open_ = close * (1 + rng.normal(0, 0.004, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.008, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.008, n)))
    pre_close = np.concatenate([[base_price], close[:-1]])
    volume = rng.lognormal(15, 0.6, n)
    return pl.DataFrame({
        "trade_date": dates,
        "symbol": [symbol] * n,
        "open": open_.round(2), "high": high.round(2),
        "low": low.round(2), "close": close.round(2),
        "pre_close": pre_close.round(2),
        "volume": volume.round(0),
        "amount": (volume * close).round(0),
        "turnover_rate": rng.uniform(0.2, 5, n).round(2),
        "adj_factor": np.ones(n),
        "sec_type": [sec_type] * n,
    })


def generate_demo(start: str = "2024-01-01", end: str | None = None) -> dict:
    """生成合成地基 + 日线。幂等：同 key 直接覆盖。"""
    from datetime import date

    from loguru import logger

    end = end or date.today().isoformat()
    dates = _calendar(start, end)["trade_date"].to_list()
    logger.info(f"生成演示数据 {len(dates)} 个交易日 {start}~{end}")

    n_cal = TradeCalendarRepo().upsert(_calendar(start, end))

    secs = pl.DataFrame({
        "symbol": [c for c, _ in STOCK_CODES] + [c for c, _, _ in ETF_CODES],
        "name": [n for _, n in STOCK_CODES] + [n for _, n, _ in ETF_CODES],
        "sec_type": ["stock"] * len(STOCK_CODES) + ["etf"] * len(ETF_CODES),
        "board": ["main"] * len(STOCK_CODES) + ["unknown"] * len(ETF_CODES),
        "list_date": date(2010, 1, 1),
        "delist_date": None,
        "is_st": False,
        "source": "demo",
        "updated_at": now_cn().replace(tzinfo=None),
    })
    n_sec = SecurityRepo().upsert(secs)

    etfs = pl.DataFrame({
        "symbol": [c for c, _, _ in ETF_CODES],
        "name": [n for _, n, _ in ETF_CODES],
        "track_index": [n for _, n, _ in ETF_CODES],
        "sellable_after_days": [t for _, _, t in ETF_CODES],
        "as_of": date.today(),
        "source": "demo",
    })
    EtfMetaRepo().upsert(etfs)

    frames = []
    for i, (sym, _) in enumerate(STOCK_CODES):
        frames.append(_synth_bars(sym, dates, seed=100 + i,
                                  base_price=5.0 + (i * 13.7) % 250, sec_type="stock"))
    for i, (sym, _, _) in enumerate(ETF_CODES):
        frames.append(_synth_bars(sym, dates, seed=500 + i,
                                  base_price=1.0 + (i * 0.83) % 6, sec_type="etf"))
    daily = pl.concat(frames).with_columns(
        source=pl.lit("demo"),
        quality_flags=pl.lit(0, dtype=pl.Int32),
        ingested_at=pl.lit(now_cn().replace(tzinfo=None), dtype=pl.Datetime),
        data_version=pl.lit("demo"),
    )
    write_daily(daily)

    logger.info(f"演示数据就绪: 日历 {n_cal} 天 / 标的 {n_sec} 只 / 日线 {len(daily)} 行")
    return {"calendar": n_cal, "securities": n_sec, "daily_rows": len(daily)}
