"""ETF 测试共享夹具（**不是测试模块** —— 文件名不匹配 ``test_*.py`` 故不被收集）。

为什么不放在某个 ``test_etf_*.py`` 里互相 import：``tests/`` 下没有 ``__init__.py``，
``from tests.unit.test_etf_x import …`` 只在「cwd 恰好是仓库根」时才解析得到，
换个目录跑（或用绝对路径收集）就 `ModuleNotFoundError`。仓库其余测试也从无
跨测试模块 import 的惯例。放在同目录的普通模块里，pytest 的 prepend 导入模式
会把 ``tests/unit`` 加进 ``sys.path``，与 cwd 无关。
"""
from __future__ import annotations

import datetime as dt
import math

import polars as pl

ETF = ["510300.SH", "510050.SH", "588000.SH", "159915.SZ", "513100.SH",
       "513050.SH", "512880.SH", "512690.SH", "511260.SH", "518880.SH"]
TYPES = ["equity", "equity", "equity", "equity", "qdii",
         "qdii", "equity", "equity", "bond", "commodity"]

FUND_SIZE = [300.0, 200.0, 50.0, 80.0, 20.0, 30.0, 40.0, 25.0, 5.0, 10.0]
SHARES = [100.0, 90.0, 40.0, 60.0, 15.0, 20.0, 30.0, 18.0, 4.0, 8.0]
FEES = [0.5, 0.5, 0.5, 0.5, 0.6, 0.6, 0.5, 0.5, 0.15, 0.5]


def bars(n_days: int = 400, symbols: list[str] | None = None) -> pl.DataFrame:
    """自造 ETF 日线：确定性正弦走势，保证每个因子都出非 null 值。"""
    symbols = symbols or ETF
    days = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(n_days + 60)]
    days = [d for d in days if d.weekday() < 5][:n_days]
    rows = []
    for i, s in enumerate(symbols):
        base = 1.0 + i * 0.5
        for j, d in enumerate(days):
            px = base * (1 + 0.0005 * j) + 0.05 * math.sin(j / 7 + i)
            rows.append({
                "symbol": s, "trade_date": d, "open": px, "high": px * 1.01,
                "low": px * 0.99, "close": px, "pre_close": px * 0.999,
                "volume": 1e6 * (1 + 0.1 * math.sin(j / 5 + i)),
                "amount": px * 1e6, "turnover_rate": 0.5 + 0.1 * math.sin(j / 9 + i),
            })
    return pl.DataFrame(rows)


def etf_meta(symbols: list[str] | None = None,
             types: list[str] | None = None) -> pl.DataFrame:
    """自造 etf_meta：符号数决定切片长度，避免 DataFrame 高度不匹配。"""
    n = len(symbols or ETF)
    symbols = symbols or ETF
    return pl.DataFrame({
        "symbol": symbols,
        "fund_type": (types or TYPES)[:n],
        "fund_size": FUND_SIZE[:n],
        "share_outstanding": SHARES[:n],
        "management_fee": FEES[:n],
    })
