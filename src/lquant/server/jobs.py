"""任务队列：RQ（Redis 可用）或本地降级执行（Redis 不可用）。

单机开发环境经常没有 Redis —— 直接抛错会把整个 API 打瘫。
降级策略：无 Redis 时任务在后台线程同步执行，返回一个 duck-typed job
（有 id / get_status() / result），调用方代码不用改。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

QUEUES = ("lquant-default", "lquant-ingest", "lquant-backtest", "lquant-mining")


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
    created_at: float = field(default_factory=time.time)
    queue: str | None = None
    _canceled: bool = False
    _result: Any = None
    _error: str | None = None
    _thread: threading.Thread | None = None

    def get_status(self) -> str:
        if self._canceled:
            return "canceled"
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
_LOCAL_JOBS_MAX = 200  # 降级模式的兜底容量：超过就淘汰已结束的最旧任务
_JOBS_LOCK = threading.Lock()


def _register_job(job: LocalJob) -> None:
    """注册本地任务并淘汰最旧的已结束任务（防降级模式内存泄漏）。"""
    with _JOBS_LOCK:
        if len(_LOCAL_JOBS) >= _LOCAL_JOBS_MAX:
            for jid, j in list(_LOCAL_JOBS.items()):
                if j.get_status() != "started":
                    del _LOCAL_JOBS[jid]
                    if len(_LOCAL_JOBS) < _LOCAL_JOBS_MAX:
                        break
        _LOCAL_JOBS[job.id] = job


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


@dataclass
class _CancelTarget:
    """可取消目标的登记：本地任务用 LocalJob 引用，RQ 任务记队列名。"""

    queue: str
    local_job: LocalJob | None


# job_id → 取消目标。入队时登记，request_cancel / list_recent 消费。
_CANCELABLE: dict[str, _CancelTarget] = {}
_CANCELABLE_MAX = 500  # 登记表容量兜底，超出淘汰最旧条目


def _register_cancelable(job_id: str, target: _CancelTarget) -> None:
    with _JOBS_LOCK:
        if len(_CANCELABLE) >= _CANCELABLE_MAX:
            _CANCELABLE.pop(next(iter(_CANCELABLE)), None)
        _CANCELABLE[job_id] = target


def request_cancel(job_id: str) -> bool:
    """请求取消任务。本地任务：置 canceled 标记（线程自然结束后结果被丢弃）；
    RQ 任务：queued 状态可真取消，运行中的返回 False。返回是否已受理。"""
    with _JOBS_LOCK:
        target = _CANCELABLE.get(job_id)
    if target is None:
        return False
    if target.local_job is not None:
        lj = target.local_job
        lj._canceled = True
        return True
    if _redis_available():
        try:
            from rq.job import Job

            job = Job.fetch(job_id, connection=get_redis())
            if job.get_status() == "queued":
                job.cancel()
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


def enqueue(queue: str, fn, *args, job_id: str | None = None, **kwargs):
    """入队。fn 接受 cancel_check 形参时自动注入协作式取消探针。

    协作式取消：长任务在批次间轮询 cancel_check()，返回 True 就提前收尾。
    本地降级线程无法强杀 —— 任务体不配合时，cancel 只能保证状态标记与
    结果丢弃，线程跑到自然结束。
    """
    if _redis_available():
        from rq import Queue

        q = Queue(queue, connection=get_redis())
        job = q.enqueue(fn, *args, job_id=job_id, **kwargs) if job_id \
            else q.enqueue(fn, *args, **kwargs)
        _register_cancelable(job.id, _CancelTarget(queue=queue, local_job=None))
        return job

    # 本地降级：后台线程执行，异常留在 job 上而不是炸请求方
    job = LocalJob(id=job_id) if job_id else LocalJob()
    job.queue = queue
    _register_job(job)
    _register_cancelable(job.id, _CancelTarget(queue=queue, local_job=job))

    # fn 签名里有 cancel_check 才注入（RQ 模式同样透传）
    import inspect

    try:
        sig = inspect.signature(fn)
        if "cancel_check" in sig.parameters:
            kwargs["cancel_check"] = lambda jid=job.id: _is_canceled(jid)  # noqa: E731
    except (TypeError, ValueError):
        pass

    def _run():
        started = time.time()
        from lquant.monitor.emit import emit_task_event

        emit_task_event(event="started", job_id=job.id,
                        job_name=getattr(fn, "__name__", str(fn)), queue=queue,
                        enqueued_at=None, started_at=started, finished_at=None)
        try:
            job._result = fn(*args, **kwargs)
            emit_task_event(event="finished", job_id=job.id,
                            job_name=getattr(fn, "__name__", str(fn)),
                            queue=queue, enqueued_at=None, started_at=started,
                            finished_at=time.time())
        except Exception as e:  # noqa: BLE001
            job._error = f"{type(e).__name__}: {e}"
            emit_task_event(event="failed", job_id=job.id,
                            job_name=getattr(fn, "__name__", str(fn)),
                            queue=queue, enqueued_at=None, started_at=started,
                            finished_at=time.time(),
                            message=f"{type(e).__name__}: {e}")

    job._thread = threading.Thread(target=_run, name=f"localjob-{job.id}", daemon=True)
    job._thread.start()
    return job


def _is_canceled(job_id: str) -> bool:
    with _JOBS_LOCK:
        t = _CANCELABLE.get(job_id)
        return bool(t and t.local_job is not None and t.local_job._canceled)


def list_recent_jobs(limit: int = 50) -> list[dict]:
    """最近入队的队列任务（本地降级注册表 + Redis 模式下的 RQ 各注册表）。

    返回归一结构 {id, status, created_at, error}，按创建时间倒序。
    created_at 是本地标记；RQ 侧取 job.enqueued_at。
    """
    out: list[dict] = []
    if _redis_available():
        try:
            from contextlib import suppress

            from rq import Queue
            from rq.job import Job
            from rq.registry import FailedJobRegistry, FinishedJobRegistry, StartedJobRegistry

            con = get_redis()
            for qname in QUEUES:
                q = Queue(qname, connection=con)
                jobs: list = list(q.get_jobs())
                for reg in (StartedJobRegistry(qname, connection=con),
                            FailedJobRegistry(qname, connection=con)):
                    jobs += reg.get_jobs()
                fin = FinishedJobRegistry(qname, connection=con)
                for jid in fin.get_job_ids()[-limit:]:
                    with suppress(Exception):
                        jobs.append(Job.fetch(jid, connection=con))  # noqa: BLE001 - 过期被清，跳过
                for j in jobs:
                    out.append({"id": j.get_id(), "status": j.get_status() or "queued",
                                "created_at": j.enqueued_at.timestamp()
                                if j.enqueued_at else 0.0,
                                "error": None, "queue": qname})
        except Exception:  # noqa: BLE001 - Redis 抖动不炸列表
            pass
        return out
    with _JOBS_LOCK:
        jobs = sorted(_LOCAL_JOBS.values(), key=lambda j: j.created_at,
                      reverse=True)[:limit]
    return [{"id": j.id, "status": j.get_status(), "queue": j.queue,
             "created_at": j.created_at, "error": j.error} for j in jobs]
