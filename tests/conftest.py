"""测试夹具。"""
from __future__ import annotations

import polars as pl
import pytest


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """收尾：关掉 agent service 单例持有的 aiosqlite 连接。

    为什么必须显式关：aiosqlite 每条连接的 worker 线程是 non-daemon，只有
    `await close()`（在事件循环还活着时）才会把它停掉。靠 GC 的 `__del__` 不行——
    那时循环已关闭，worker 里 `future.get_loop().call_soon_threadsafe(...)` 会抛
    异常，恰好卡在 `break` 之前，线程永远退不出去。连接不关，解释器就会停在
    `threading._shutdown` —— 表现为 pytest 打印完结果却不返回。
    线上由 FastAPI 的 shutdown 钩子收尾，但 ASGITransport 不跑 lifespan。
    """
    import asyncio
    import contextlib

    from lquant.agent import service as agent_service

    with contextlib.suppress(Exception):  # 收尾失败不改变测试结论
        asyncio.run(agent_service.shutdown_agent_service())


@pytest.fixture
def daily_bars() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["000001.SZ"] * 5 + ["600000.SH"] * 5,
            "trade_date": [__import__("datetime").date(2026, 1, d) for d in range(5, 10)] * 2,
            "open": [10.0, 10.1, 10.2, 10.3, 10.4] * 2,
            "high": [10.5] * 10,
            "low": [9.5] * 10,
            "close": [10.0, 10.1, 10.2, 10.3, 10.4] * 2,
            "pre_close": [10.0, 10.0, 10.1, 10.2, 10.3] * 2,
            "volume": [1e6] * 10,
            "amount": [1e7] * 10,
        }
    )
