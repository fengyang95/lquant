"""core.logging 单测：run_scope 绑定/恢复、stdlib 拦截、幂等、文件 sink。

loguru 是全局单例，用自建 sink list 收集断言；setup_logging 统一传 tmp_path
的 log_dir，避免测试往仓库 logs/ 写文件（enqueue 线程异步写，记得 complete）。
"""

from __future__ import annotations

import contextlib
import logging

import pytest
from loguru import logger

from lquant.core.logging import (
    current_run_id,
    get_logger,
    run_scope,
    setup_logging,
)


@pytest.fixture()
def sink_records(tmp_path):
    """setup_logging 走 tmp 文件 sink，再挂自建 list sink 收集断言。"""
    records: list = []
    setup_logging(level="INFO", log_dir=str(tmp_path))
    handler_id = logger.add(records.append, level="DEBUG")
    yield records
    # setup_logging 幂等测试会在中途 logger.remove() 重置全部 sink，
    # 这里的 handler_id 可能已失效 —— 容忍后兜底全清。
    with contextlib.suppress(ValueError):
        logger.remove(handler_id)
    logger.remove()  # 收掉文件/控制台 sink，不污染后续测试


def test_run_scope_bind_and_restore():
    assert current_run_id() is None
    with run_scope("abc123") as rid:
        assert rid == "abc123"
        assert current_run_id() == "abc123"
    assert current_run_id() is None


def test_run_scope_generates_id():
    with run_scope() as rid:
        assert rid and len(rid) == 12


def test_run_scope_nested_and_restored():
    with run_scope("outer"):
        with run_scope("inner") as inner:
            assert inner == "inner"
            assert current_run_id() == "inner"
        assert current_run_id() == "outer"


def test_intercept_handler_forwards_stdlib(sink_records):
    logging.getLogger("my.stdlib.probe").warning("hello %s", "stdlib")
    assert any("hello stdlib" in str(m) for m in sink_records)


def test_setup_logging_idempotent(tmp_path, sink_records):
    sink_records.clear()
    get_logger("probe").info("idem-marker-1")
    setup_logging(level="INFO", log_dir=str(tmp_path))
    # setup_logging 内部 logger.remove() 会收掉测试 sink，重挂后继续收集
    logger.add(sink_records.append, level="DEBUG")
    get_logger("probe").info("idem-marker-2")
    logger.complete()
    # 两次调用各只出现一次 —— 说明没有叠加重复 sink
    assert sum("idem-marker-1" in str(m) for m in sink_records) == 1
    assert sum("idem-marker-2" in str(m) for m in sink_records) == 1


def test_file_sink_creates_dir_and_writes(tmp_path):
    setup_logging(log_dir=str(tmp_path))
    get_logger("probe").info("file-sink-marker")
    logger.complete()  # enqueue=True 异步写，flush 后再断言
    f = tmp_path / "lquant.log"
    assert f.exists()
    assert "file-sink-marker" in f.read_text()
    logger.remove()


def test_get_logger_returns_callable():
    log = get_logger("probe")
    assert callable(log.info)


def test_run_id_flows_into_extra(sink_records):
    with run_scope("run42"):
        get_logger("probe").info("run-id-marker")
    extras = [m.record["extra"]["run_id"] for m in sink_records
              if "run-id-marker" in str(m)]
    assert extras and all(e == "run42" for e in extras)
