"""统一日志：loguru 单轨 + stdlib 拦截 + 文件轮转 + run_id/trace_id 贯穿。

用法：
- 进程入口（server、sync 调度、脚本）调用一次 ``setup_logging()``。
- 业务代码 ``from lquant.core.logging import get_logger; log = get_logger(__name__)``。
- 一次作业/请求用 ``run_scope("sync", sync_id=...)`` 包住，期间所有日志
  自动带上 run_id（contextvars 实现，线程/async 安全）。
- stdlib ``logging.getLogger(...)`` 的输出被 InterceptHandler 转发到
  loguru，三轨不再并行。
"""
from __future__ import annotations

import contextvars
import logging
import os
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

__all__ = ["setup_logging", "get_logger", "run_scope", "current_run_id",
           "new_run_id", "InterceptHandler", "log_file_path"]

# 每次作业/请求的贯穿标识；loguru patcher 注入 extra，格式里统一输出
_run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "lquant_run_id", default=None)

_LOG_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<7} | "
    "{extra[run_id]} | {name}:{function}:{line} - {message}"
)


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def current_run_id() -> str | None:
    return _run_id_var.get()


@contextmanager
def run_scope(run_id: str | None = None):
    """在一次作业/请求范围内绑定 run_id，期间所有日志自动携带。"""
    rid = run_id or new_run_id()
    token = _run_id_var.set(rid)
    try:
        yield rid
    finally:
        _run_id_var.reset(token)


def log_file_path(log_dir: str | None = None) -> Path:
    d = Path(log_dir or os.environ.get("LQ_LOG_DIR", "logs"))
    return d / "lquant.log"


class InterceptHandler(logging.Handler):
    """把 stdlib logging 记录转发给 loguru，统一输出与文件落盘。"""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        from loguru import logger

        try:
            level: str | int = record.levelname
        except ValueError:
            level = record.levelno
        # 透传调用点：跳过 stdlib 内部帧，让 name/line 指向真实业务代码
        frame, depth = logging.currentframe(), 2
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage())


def setup_logging(level: str | None = None, sink: str | None = None,
                  *, log_dir: str | None = None) -> None:
    """初始化全局日志。幂等（重复调用先清空 handlers）。

    - stderr 彩色控制台输出（诊断友好）
    - 默认文件 sink：LQ_LOG_DIR/logs/lquant.log，轮转 50MB / 保留 14 天，
      保证出问题时「一定有迹可循」——不再依赖 shell 层 stdout 重定向
    - 拦截 stdlib logging（uvicorn/apscheduler 等三方库日志并入统一格式）
    """
    from loguru import logger

    lvl = (level or os.environ.get("LQ_LOG_LEVEL", "INFO")).upper()
    logger.remove()
    logger.configure(extra={"run_id": "-"}, patcher=_run_id_patch)
    logger.add(
        sys.stderr, level=lvl,
        format="<green>{time:HH:mm:ss.SSS}</green> | <level>{level:<7}</level> | "
               "{extra[run_id]} | <cyan>{name}:{function}:{line}</cyan> - {message}")
    path = Path(sink) if sink else log_file_path(log_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 运行日志保留 14 天：排查跨天问题时仍能回溯；enqueue 多进程安全
    logger.add(str(path), level=lvl, format=_LOG_FORMAT,
               rotation="50 MB", retention="14 days",
               enqueue=True, backtrace=True, diagnose=False)
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    # 三方库普遍噪声偏高：root 放行，已知名单压到 WARNING
    for noisy in ("urllib3", "httpx", "apscheduler", "tzlocal"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _run_id_patch(record: dict) -> None:  # pragma: no cover - 极小纯函数
    record["extra"]["run_id"] = current_run_id() or "-"


def get_logger(name: str):
    """业务代码统一入口（loguru logger），沿用全局配置与 run_id 绑定。"""
    from loguru import logger

    return logger.bind(name_hint=name)
