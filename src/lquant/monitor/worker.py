"""MonitoringWorker（任务事件发射）+ current_job_fn（worker registry 匹配）。"""
from __future__ import annotations

import logging
import os
import time

from rq import Worker

from lquant.monitor.emit import emit_task_event

_LOG = logging.getLogger(__name__)


def _latest_error(job) -> str | None:
    """从 RQ Result 取失败消息（截断 200）；无则 None。"""
    try:
        res = job.latest_result()
        if res is not None and getattr(res, "type", None) is not None \
                and "failure" in str(getattr(res.type, "name", "")).lower():
            return str(res.return_value)[:200]
    except Exception:  # noqa: BLE001
        pass
    return None


def _to_ts(dt) -> float | None:
    if dt is None:
        return None
    return dt.timestamp() if hasattr(dt, "timestamp") else float(dt)


class MonitoringWorker(Worker):
    def perform_job(self, job, queue):
        job_name = getattr(job, "func_name", None) or job.id
        started = time.time()
        emit_task_event(event="started", job_id=job.id, job_name=job_name,
                        queue=queue.name, enqueued_at=_to_ts(job.enqueued_at),
                        started_at=started, finished_at=None)
        ok = super().perform_job(job, queue)
        finished = time.time()
        status = job.get_status()
        emit_task_event(
            event="finished" if status == "finished" else "failed",
            job_id=job.id, job_name=job_name, queue=queue.name,
            enqueued_at=_to_ts(job.enqueued_at), started_at=started,
            finished_at=finished, message=None if ok else _latest_error(job))
        return ok


def current_job_fn():
    """RQ Worker registry 按 pid 找自身 → 当前 job id；找不到 None。"""
    try:
        me = os.getpid()
        for w in Worker.all():
            if getattr(w, "pid", None) == me:
                return w.get_current_job_id()  # RQ 版本 API 以 uv.lock 为准
    except Exception:  # noqa: BLE001
        return None
    return None
