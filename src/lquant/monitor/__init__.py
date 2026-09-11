"""lquant 监控子系统：采集（中间件/sampler/worker 事件）→ flusher 落盘 → 查询。"""
from __future__ import annotations

import logging

_LOG = logging.getLogger(__name__)

from lquant.monitor import flusher, proc_sampler  # noqa: E402


def start_monitor() -> None:
    """http 进程启动钩子：flusher 线程 + 自身采样。失败只 log。"""
    try:
        from lquant.core.config import get_settings

        if not get_settings().monitor_enabled:
            return
        flusher.start_flusher()
        proc_sampler.start_sampler("http")
    except Exception:  # noqa: BLE001
        _LOG.exception("start_monitor 失败")


def stop_monitor() -> None:
    try:
        proc_sampler.stop_sampler()
        flusher.stop_flusher()  # final flush
    except Exception:  # noqa: BLE001
        _LOG.exception("stop_monitor 失败")
