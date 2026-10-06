"""tests/unit 共享夹具：缓存隔离等跨文件测试卫生。"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_fundamentals_cache():
    """G13 缓存是进程级 dict，键不含数据源身份；每个用例前清空，
    防止前一个用例（可能用不同 tmp 库/种子）的结果跨用例污染。"""
    from lquant.research.dialect.fundamentals import clear_fundamentals_cache

    clear_fundamentals_cache()
    yield


@pytest.fixture(autouse=True)
def _isolate_report_dir(tmp_path, monkeypatch):
    """把报告目录指到 tmp —— 用例不许往真实的 ``data/reports`` 写文件。

    之前 ``/factors/evaluate``、``/factors/synthesize`` 的用例会真的在
    ``data/reports`` 里留下 ``covrate.html`` / ``covfail.html`` / ``ws*.html``
    这类产物：既污染报告中心（读者会看到一堆测试垃圾），也让「陈旧报告」
    的统计失真。需要真实目录的用例自行覆盖本夹具。
    """
    from lquant.server.api import factors as api

    monkeypatch.setattr(api, "REPORT_DIR", tmp_path / "reports")
    yield
