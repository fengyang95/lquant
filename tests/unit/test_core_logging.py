"""core.logging 单测：run_scope 绑定/恢复、stdlib 拦截、幂等、文件 sink。

loguru 是全局单例，用自建 sink list 收集断言；setup_logging 统一传 tmp_path
的 log_dir，避免测试往仓库 logs/ 写文件（enqueue 线程异步写，记得 complete）。
"""

from __future__ import annotations

import contextlib
import logging
import re

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


def test_intercept_handler_reports_real_caller(sink_records):
    """stdlib 记录必须指向真实业务调用点，而不是 logging 内部帧。

    回归：旧实现把 depth 写死成 2，且循环首轮条件即为假（currentframe()
    返回的就是 emit 自己的帧）—— depth 恒为 2，恰好落在
    `logging.callHandlers` 上。于是所有经 stdlib 转发的日志（A2A 告警、
    agent 任务、uvicorn 等）在监控页运行日志里都显示成
    `logging:callHandlers:1762`，真实来源全丢。
    """
    logging.getLogger("my.stdlib.caller").warning("caller-marker")
    rec = next(m.record for m in sink_records if "caller-marker" in str(m))
    assert rec["name"] == __name__
    assert rec["function"] == "test_intercept_handler_reports_real_caller"
    assert rec["line"] > 0


def test_no_percent_style_args_on_loguru():
    """静态守卫：loguru 只认 str.format 的 {} 占位，%-style 会静默丢参数。

    回归：`daily.py::backfill_pool` 用 `logger.warning("缺口 %d 天…", n)`
    调 loguru，格式化参数被整体丢弃，监控页运行日志里只剩
    「日级完整性缺口 %d 天（应写 %d 只）：%s」—— 数值一个都没有，
    缺口天数/应有标的数/明细全不可见。
    """
    import ast
    import pathlib

    src_root = pathlib.Path(__file__).resolve().parents[2] / "src"
    pct = re.compile(r"%[-#0 +]?\d*(?:\.\d+)?[sdrfgeExXo]|%\(\w+\)")
    offenders: list[str] = []
    for p in sorted(src_root.rglob("*.py")):
        text = p.read_text(encoding="utf-8")
        if "loguru" not in text and "get_logger(" not in text:
            continue
        tree = ast.parse(text)
        loguru_names, stdlib_names = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "loguru":
                loguru_names.update(a.asname or a.name for a in node.names)
            elif isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                callee = getattr(node.value, "func", None)
                dotted = _dotted(callee) if isinstance(node.value, ast.Call) else ""
                if dotted == "get_logger":
                    loguru_names.update(targets)
                elif dotted == "logging.getLogger":
                    stdlib_names.update(targets)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            if not isinstance(owner, ast.Name) or owner.id not in loguru_names - stdlib_names:
                continue
            fmt = node.args[0] if node.args else None
            if not isinstance(fmt, ast.Constant) or not isinstance(fmt.value, str):
                continue
            if len(node.args) > 1 and pct.search(fmt.value):
                offenders.append(f"{p.relative_to(src_root)}:{node.lineno}")

    assert not offenders, (
        "loguru 不认 %-style 占位符，参数会被静默丢弃；请改用 {} 或 f-string：\n  "
        + "\n  ".join(offenders))


def _dotted(node) -> str:
    """把 `a.b.c`（或裸名字 `a`）还原成点号字符串；认不出返回空串。"""
    import ast

    if isinstance(node, ast.Name):
        return node.id
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return ""
    return ".".join([cur.id, *reversed(parts)])


def test_get_logger_returns_callable():
    log = get_logger("probe")
    assert callable(log.info)


def test_run_id_flows_into_extra(sink_records):
    with run_scope("run42"):
        get_logger("probe").info("run-id-marker")
    extras = [m.record["extra"]["run_id"] for m in sink_records
              if "run-id-marker" in str(m)]
    assert extras and all(e == "run42" for e in extras)
