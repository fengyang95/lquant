"""任务队列：RQ（Redis 可用）或本地降级执行（Redis 不可用）。

单机开发环境经常没有 Redis —— 直接抛错会把整个 API 打瘫。
降级策略：无 Redis 时任务在后台线程同步执行，返回一个 duck-typed job
（有 id / get_status() / result），调用方代码不用改。
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

QUEUES = ("lquant-default", "lquant-ingest", "lquant-backtest")


@lru_cache(maxsize=1)
def _redis_available() -> bool:
    try:
        import redis

        from lquant.core.config import get_settings

        r = redis.from_url(get_settings().redis_url, socket_connect_timeout=1)
        r.ping()
        return True
    except Exception:  # noqa: BLE001 - 没装 redis 包 / 连不上，都算不可用
        return False


@lru_cache(maxsize=1)
def get_redis():
    import redis

    from lquant.core.config import get_settings

    return redis.from_url(get_settings().redis_url)


@dataclass
class LocalJob:
    """Redis 不可用时的本地执行凭证。"""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    _result: Any = None
    _error: str | None = None
    _thread: threading.Thread | None = None

    def get_status(self) -> str:
        if self._thread is None:
            return "finished" if self._error is None else "failed"
        return "started" if self._thread.is_alive() else "finished"

    @property
    def result(self) -> Any:
        return self._result

    @property
    def error(self) -> str | None:
        return self._error


# 本地任务的进程内注册表：/ws/jobs/{id} 靠它查状态。
# Redis 模式下 RQ Job 自带状态查询，不需要这里。
_LOCAL_JOBS: dict[str, LocalJob] = {}


def get_job(job_id: str):
    """按 id 取任务凭证（本地注册表优先，Redis 模式返回 None 由调用方处理）。"""
    job = _LOCAL_JOBS.get(job_id)
    if job is not None:
        return job
    if _redis_available():
        try:
            from rq.job import Job

            return Job.fetch(job_id, connection=get_redis())
        except Exception:  # noqa: BLE001
            return None
    return None


def enqueue(queue: str, fn, *args, **kwargs):
    if _redis_available():
        from rq import Queue

        q = Queue(queue, connection=get_redis())
        return q.enqueue(fn, *args, **kwargs)

    # 本地降级：后台线程执行，异常留在 job 上而不是炸请求方
    job = LocalJob()
    _LOCAL_JOBS[job.id] = job

    def _run():
        try:
            job._result = fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            job._error = f"{type(e).__name__}: {e}"

    job._thread = threading.Thread(target=_run, name=f"localjob-{job.id}", daemon=True)
    job._thread.start()
    return job
