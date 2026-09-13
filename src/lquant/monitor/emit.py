"""任务生命周期事件统一出口。

Redis 可用 → LPUSH events（flusher drain）+ recent（展示 list，LTRIM 保留最新）；
不可用 → 内存队列（flusher 同样抽取）。任何失败只 log，兜底进内存队列。
"""
from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lquant.monitor.types import TaskEvent

_LOG = logging.getLogger(__name__)

EVENTS_KEY = "lquant:monitor:events"
RECENT_KEY = "lquant:monitor:recent"
RECENT_MAX = 50


def _redis_available() -> bool:
    from lquant.server.jobs import _redis_available as fn

    return fn()


def _get_redis():
    from lquant.server.jobs import get_redis

    return get_redis()


def _build_event(*, event: str, job_id: str, job_name: str, queue: str | None,
                 enqueued_at: float | None, started_at: float | None,
                 finished_at: float | None, message: str | None) -> TaskEvent:
    from lquant.monitor.types import TaskEvent

    elapsed = (finished_at - started_at) * 1000.0 \
        if finished_at is not None and started_at is not None else None
    delay = (started_at - enqueued_at) * 1000.0 \
        if started_at is not None and enqueued_at is not None else None
    return TaskEvent(
        event_ts=time.time(), job_id=job_id, job_name=job_name, event=event,
        queue=queue, enqueued_at=enqueued_at, started_at=started_at,
        finished_at=finished_at, elapsed_ms=elapsed, queue_delay_ms=delay,
        message=(message or "")[:200] or None)


def emit_task_event(*, event: str, job_id: str, job_name: str,
                    queue: str | None, enqueued_at: float | None,
                    started_at: float | None, finished_at: float | None,
                    message: str | None = None) -> None:
    from lquant.monitor.ring import local_events
    from lquant.monitor.types import TaskEvent  # noqa: F401 (类型标注引用)

    try:
        ev = _build_event(event=event, job_id=job_id, job_name=job_name,
                          queue=queue, enqueued_at=enqueued_at,
                          started_at=started_at, finished_at=finished_at,
                          message=message)
    except Exception:  # noqa: BLE001
        _LOG.exception("emit_task_event 构造失败")
        return
    try:
        if _redis_available():
            r = _get_redis()
            payload = json.dumps({f: getattr(ev, f) for f in
                                  ("event_ts", "job_id", "job_name", "event",
                                   "queue", "enqueued_at", "started_at",
                                   "finished_at", "elapsed_ms",
                                   "queue_delay_ms", "message")})
            r.lpush(EVENTS_KEY, payload)
            r.lpush(RECENT_KEY, payload)
            r.ltrim(RECENT_KEY, 0, RECENT_MAX - 1)
            return
    except Exception:  # noqa: BLE001 - Redis 失败兜底内存
        _LOG.warning("emit_task_event Redis 写入失败，转内存队列", exc_info=True)
    try:
        local_events.put(ev)
    except Exception:  # noqa: BLE001
        _LOG.exception("emit_task_event 内存队列写入失败")
