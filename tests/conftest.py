"""测试夹具。"""
from __future__ import annotations

import contextlib

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


@pytest.fixture(autouse=True, scope="session")
def _isolate_app_logs(tmp_path_factory):
    """整个测试会话把应用日志重定向到临时目录。

    app 启动（server.main 的 lifespan）与 factors.ops.rust_bridge 都会调用
    setup_logging()，默认文件 sink 是 logs/lquant.log —— 正是监控页「运行日志」
    面板的数据源。不隔离的话，跑一次测试就把用例**刻意构造**的 ERROR
    （如 test_selfcheck 里 monkeypatch 成必炸的 Engine）灌进生产日志，监控页
    上表现为一排红色 ERROR，真假故障混在一起。

    重定向后测试日志落在 tmp，logs/lquant.log 只反映真实运行。
    需要指定日志目录的用例（如 test_monitor_logs）自行 monkeypatch LQ_LOG_DIR 覆盖。
    """
    import os

    d = tmp_path_factory.mktemp("lq-logs")
    prev = os.environ.get("LQ_LOG_DIR")
    os.environ["LQ_LOG_DIR"] = str(d)
    yield d
    if prev is None:
        os.environ.pop("LQ_LOG_DIR", None)
    else:
        os.environ["LQ_LOG_DIR"] = prev


def pytest_configure(config):
    """注册收尾插件（见 :class:`_ShutdownGuard`）。"""
    config.pluginmanager.register(_ShutdownGuard(), "lq-shutdown-guard")


class _ShutdownGuard:
    """收尾：尽力关掉 aiosqlite；然后硬退，避免卡在解释器退出。

    ``pytest_sessionfinish`` —— 关掉 agent service 单例持有的 aiosqlite 连接。
    为什么必须显式关：aiosqlite 每条连接的 worker 线程是 non-daemon，只有
    ``await close()``（在事件循环还活着时）才会把它停掉。靠 GC 的 ``__del__``
    不行 —— 那时循环已关闭，worker 里 ``future.get_loop().call_soon_threadsafe``
    会抛异常，恰好卡在 ``break`` 之前，线程永远退不出去。连接不关，解释器就会停在
    ``threading._shutdown`` —— 表现为 pytest 打印完结果却不返回。
    线上由 FastAPI 的 shutdown 钩子收尾，但 ASGITransport 不跑 lifespan。

    ``pytest_unconfigure`` —— 兜底硬退。上面那步只是**尽力而为**：只要有一条连接
    漏网（或 loguru ``enqueue=True`` 的 ``_queued_writer`` 卡在队列上），进程就会
    停在 ``threading._shutdown``。实测 CI：3969 passed / 7 skipped / 360s
    打完结果，然后**干等 34 分钟**被 ``timeout -s ABRT 2400`` 打死（exit 124），
    job 记成 failure —— 每条用例其实都是绿的。这不是「测试慢」，是「进程退不出」。

    放在 ``pytest_unconfigure`` 是刻意的：它晚于所有 ``pytest_sessionfinish``
    （pytest-cov 正是在那里 finish() 并写 coverage.xml），所以覆盖率报告、终端
    摘要、退出码都已定稿，这里再 ``os._exit`` 不会丢东西。解释器级的 atexit /
    ``Py_Finalize`` 会被跳过，这正是目的。
    """

    _exitstatus = 0

    def pytest_sessionfinish(self, session, exitstatus):  # noqa: ARG002
        self._exitstatus = int(exitstatus or 0)
        import asyncio
        import contextlib

        from lquant.agent import service as agent_service

        with contextlib.suppress(Exception):  # 收尾失败不改变测试结论
            asyncio.run(agent_service.shutdown_agent_service())

    def pytest_unconfigure(self, config):
        self._finalize_coverage(config)
        import os
        import sys

        with contextlib.suppress(Exception):  # 冲刷失败也必须退出
            sys.stdout.flush()
            sys.stderr.flush()
        os._exit(self._exitstatus)

    @staticmethod
    def _finalize_coverage(config) -> None:
        """硬退之前显式落盘 coverage，别让报告凭空消失。

        coverage 默认靠 ``atexit`` 写 ``.coverage`` 与报告，而 ``os._exit`` 跳过
        atexit —— 不补这一步，``--cov-report=xml:coverage.xml`` 就不再产出文件，
        pre-push 里读它的 diff-cover 门禁会直接判失败（「文件不存在」）。
        本函数放在 ``pytest_unconfigure``：此刻 pytest-cov 已在
        ``pytest_sessionfinish`` 里 finish() 过，数据是完整的，补写报告是纯收益。
        """
        with contextlib.suppress(Exception):
            import coverage

            cov = coverage.Coverage.current()
            if cov is None:
                return
            cov.stop()
            cov.save()  # 数据文件（diff-cover 也吃这个）
            specs = _cov_report_specs(config)
            from coverage.misc import CoverageException

            for spec in specs:
                kind, _, dest = spec.partition(":") if ":" in spec else (spec, "", None)
                kind = kind.strip()
                try:
                    if kind == "xml":
                        cov.xml_report(outfile=dest or "coverage.xml", ignore_errors=True)
                    elif kind == "json":
                        cov.json_report(outfile=dest or "coverage.json", ignore_errors=True)
                    elif kind == "html":
                        cov.html_report(directory=dest or "htmlcov", ignore_errors=True)
                    elif kind in ("term", "term-missing"):
                        cov.report(show_missing=(kind == "term-missing"), ignore_errors=True)
                except CoverageException:
                    pass  # 单个报告失败不该阻断退出


def _cov_report_specs(config) -> list[str]:
    """从 pytest-cov 的 controller 取 ``--cov-report=`` 规格；没装/没开则空。"""
    with contextlib.suppress(Exception):
        plugin = config.pluginmanager.getplugin("_cov")
        controller = getattr(plugin, "cov_controller", None)
        if controller is not None:
            return [str(s) for s in getattr(controller, "report_specs", ())]
    return []


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
