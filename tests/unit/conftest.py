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
