"""测试夹具。"""
from __future__ import annotations

import polars as pl
import pytest


@pytest.fixture(autouse=True)
def _skip_env_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """全局禁用 .env 加载：单测绝不读开发者本地 src/lquant/.env（token）。

    需要 .env 行为的测试自行 monkeypatch.delenv("LQ_ENV_SKIP") 或直接
    构造临时文件 + 显式路径调用。
    """
    monkeypatch.setenv("LQ_ENV_SKIP", "1")


@pytest.fixture(autouse=True, scope="session")
def _force_mock_agent_provider():
    """整个测试会话固定 provider=mock。

    生产默认已是 claude_code（「问 AI」= 问 Claude Code），但单测**不该**依赖
    本机装没装 claude CLI，更不该真的拉起带 --dangerously-skip-permissions 的
    子进程。需要真实 claude 语义的用例自己注入 fake 脚本
    （见 tests/unit/test_ask_agent_claude.py）。

    必须在任何 get_settings() 之前生效：app.yaml 的 `${LQ_AGENT_PROVIDER:...}`
    是**加载时**插值的，缓存一旦建立就固化，所以这里同时清一次 lru_cache。
    """
    import os

    from lquant.core.config import get_settings

    prev = os.environ.get("LQ_AGENT_PROVIDER")
    os.environ["LQ_AGENT_PROVIDER"] = "mock"
    get_settings.cache_clear()
    yield
    if prev is None:
        os.environ.pop("LQ_AGENT_PROVIDER", None)
    else:
        os.environ["LQ_AGENT_PROVIDER"] = prev
    get_settings.cache_clear()


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
