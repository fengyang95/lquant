"""任务队列：RQ（Redis 可用）或本地降级执行（Redis 不可用）。

单机开发环境经常没有 Redis —— 直接抛错会把整个 API 打瘫。
降级策略：无 Redis 时任务在后台线程同步执行，返回一个 duck-typed job
（有 id / get_status() / result），调用方代码不用改。
"""
from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from lquant.core.db import reader, writer

QUEUES = ("lquant-default", "lquant-ingest", "lquant-backtest", "lquant-mining",
          "lquant-qlib")

# ---------------------------------------------------------------- job_record

# 进程内注册表重启即失忆 —— 因子评价/回测扫参这类任务重启后前端只能报
# 「连接中断」。job_record 表把入队/终态落库：重启后 WS 兜底链与任务
# 中心历史仍可见（遗留 started 由启动钩子标记 interrupted）。
_JOB_RECORD_DDL = """
CREATE TABLE IF NOT EXISTS job_record (
    job_id     VARCHAR PRIMARY KEY,
    name       VARCHAR,
    queue      VARCHAR,
    status     VARCHAR,
    error      VARCHAR,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
)
"""

_TERMINAL = ("finished", "failed", "canceled", "interrupted")

# 持久化走后台 flusher：enqueue/终态只入队，绝不阻塞任务线程。
# 直接同步写 duckdb 会撞跨进程文件锁（与运行中的 API/CLI 抢写），
# 把 worker 线程挂住几十秒 —— 这是本设计最初的坑。
_RECORD_QUEUE: queue.Queue[tuple] = queue.Queue()
_RECORD_THREAD: threading.Thread | None = None


def _ensure_job_record(con) -> None:
    con.execute(_JOB_RECORD_DDL)


# DDL 幂等但并发 CREATE 会撞 duckdb 乐观并发（Catalog write-write conflict），
# 且每次写都跑 DDL 浪费 —— 按路径记一次：同路径进程内只建一次表。
_ENSURE_LOCK = threading.Lock()
_ENSURED_PATH: str | None = None


def _ensure_job_record_once() -> None:
    global _ENSURED_PATH
    from pathlib import Path

    from lquant.core.config import get_settings

    # 相对路径字符串在不同 cwd 下指向不同文件 —— 必须用解析后的绝对路径比较
    path = str(Path(get_settings().duckdb_path).resolve())
    if path == _ENSURED_PATH:
        return
    with _ENSURE_LOCK:
        if path == _ENSURED_PATH:
            return
        with writer() as con:
            _ensure_job_record(con)
        _ENSURED_PATH = path


def _record_flusher() -> None:
    while True:
        item = _RECORD_QUEUE.get()
        batch = [item]
        while len(batch) < 100 and not _RECORD_QUEUE.empty():
            batch.append(_RECORD_QUEUE.get_nowait())
        try:
            _ensure_job_record_once()
            with writer() as con:
                for kind, *args in batch:
                    if kind == "insert":
                        job_id, name, q = args
                        con.execute(
                            "INSERT OR REPLACE INTO job_record "
                            "VALUES (?, ?, ?, 'started', NULL, ?, ?)",
                            [job_id, name, q, _now(), _now()])
                    else:
                        job_id, status, error = args
                        con.execute(
                            "UPDATE job_record SET status = ?, error = ?, "
                            "updated_at = ? WHERE job_id = ? AND status NOT IN "
                            "('finished', 'failed', 'canceled', 'interrupted')",
                            [status, error, _now(), job_id])
        except Exception:  # noqa: BLE001 - 持久化失败不影响任务本身，但必须可见
            from loguru import logger

            logger.exception("job_record 批量落库失败")
        finally:
            # task_done 在写完之后：queue.join() 才等价于「已落库」
            _RECORD_QUEUE.task_done()
            for _ in batch[1:]:
                _RECORD_QUEUE.task_done()


_record_thread_lock = threading.Lock()


def _ensure_record_thread() -> None:
    """双检加锁：并发 enqueue 时只允许起一个 flusher 线程，
    避免两连接并发写 monitor.duckdb 触发 write-write conflict 丢记录。"""
    global _RECORD_THREAD
    if _RECORD_THREAD is not None and _RECORD_THREAD.is_alive():
        return
    with _record_thread_lock:
        if _RECORD_THREAD is not None and _RECORD_THREAD.is_alive():
            return
        _RECORD_THREAD = threading.Thread(
            target=_record_flusher, name="job-record-flusher", daemon=True)
        _RECORD_THREAD.start()


def _record_insert(job_id: str, name: str | None, queue: str) -> None:
    _ensure_record_thread()
    _RECORD_QUEUE.put(("insert", job_id, name, queue))


def _record_update(job_id: str, status: str, error: str | None = None) -> None:
    _ensure_record_thread()
    _RECORD_QUEUE.put(("update", job_id, status, error))


def get_job_record(job_id: str) -> dict | None:
    try:
        with reader() as con:
            row = con.execute(
                "SELECT job_id, name, queue, status, error, created_at "
                "FROM job_record WHERE job_id = ?", [job_id]).fetchone()
    except Exception:  # noqa: BLE001 - 表不存在/连接失败（如 monitor 库被占用）按缺失处理
        return None
    if row is None:
        return None
    return {"id": row[0], "name": row[1], "queue": row[2], "status": row[3],
            "error": row[4],
            "created_at": row[5].timestamp() if row[5] is not None else 0.0}


def mark_interrupted_jobs() -> int:
    """启动钩子：遗留 started 记录 → interrupted（服务重启打断）。"""
    try:
        _ensure_job_record_once()
        with writer() as con:
            n = con.execute(
                "SELECT count(*) FROM job_record WHERE status = 'started'"
            ).fetchone()[0]
            con.execute(
                "UPDATE job_record SET status = 'interrupted', updated_at = ? "
                "WHERE status = 'started'", [_now()])
        return int(n)
    except Exception:  # noqa: BLE001 - 标记失败不挡启动
        return 0


def _now():
    from datetime import datetime

    return datetime.now()


class JobCanceled(RuntimeError):
    """协作式取消收尾信号：任务体在 cancel_check() 返回 True 时抛出。"""


# Redis 可用性探测结果的 TTL 缓存（秒）。
# 早期这里是 @lru_cache(maxsize=1)，即首次探测结果被进程永久记住。单机环境下
# 这是坑：docker-compose 里 API 常比 Redis 先起来，首次探测失败就永久降级为
# 本地线程，即使 Redis 随后就绪也不会启用队列，直到手动重启 API；反过来 Redis
# 中途挂掉也会一直走 RQ 分支白等超时。改成带 TTL 的探测缓存后能自愈。
_REDIS_PROBE_TTL = 30.0  # 秒
_redis_probe_ok: bool | None = None
_redis_probe_at: float = 0.0
_redis_probe_lock = threading.Lock()


def _probe_redis() -> bool:
    """真正 ping 一次 Redis。没装 redis 包 / 连不上，都算不可用。

    单独建一个带 1s 连接超时的客户端，避免探测本身在 Redis 不可达时长时间阻塞
    （`get_redis()` 返回的客户端不带超时，是给正常操作复用的）。
    """
    try:
        import redis

        from lquant.core.config import get_settings

        r = redis.from_url(get_settings().redis_url, socket_connect_timeout=1)
        return bool(r.ping())
    except Exception:  # noqa: BLE001 - 没装 redis 包 / 连不上，都算不可用
        return False


def _redis_available(ttl: float = _REDIS_PROBE_TTL) -> bool:
    """Redis 是否可用。结果按 ttl 秒缓存，避免热路径（每条任务事件）都去 ping。

    ttl<=0 表示无条件重新探测（测试或强制刷新用）。
    """
    global _redis_probe_ok, _redis_probe_at
    if ttl > 0 and _redis_probe_ok is not None \
            and (time.monotonic() - _redis_probe_at) < ttl:
        return _redis_probe_ok
    with _redis_probe_lock:
        if ttl > 0 and _redis_probe_ok is not None \
                and (time.monotonic() - _redis_probe_at) < ttl:
            return _redis_probe_ok
        _redis_probe_ok = _probe_redis()
        _redis_probe_at = time.monotonic()
        return _redis_probe_ok


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
        return "started" if self._thread.is_alive() \
            else ("finished" if self._error is None else "failed")

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
    """可取消目标的登记：本地任务用 LocalJob 引用，RQ 任务记队列名。

    canceled 是协作式取消标记：request_cancel 受理时置位，探针
    （enqueue 注入的 cancel_check）轮询它 —— 本地与 RQ 分支统一。
    """

    queue: str
    local_job: LocalJob | None
    canceled: bool = False


# job_id → 取消目标。入队时登记，request_cancel / list_recent 消费。
_CANCELABLE: dict[str, _CancelTarget] = {}
_CANCELABLE_MAX = 500  # 登记表容量兜底，超出淘汰最旧条目


def _register_cancelable(job_id: str, target: _CancelTarget) -> None:
    with _JOBS_LOCK:
        if len(_CANCELABLE) >= _CANCELABLE_MAX:
            # 淘汰最旧时跳过未取消的运行中任务，避免长任务取消目标被挤丢
            for old_id in list(_CANCELABLE):
                old = _CANCELABLE[old_id]
                if old.canceled or (
                        old.local_job is not None and old.local_job._canceled):
                    del _CANCELABLE[old_id]
                    break
            else:
                del _CANCELABLE[next(iter(_CANCELABLE))]
        _CANCELABLE[job_id] = target


def request_cancel(job_id: str) -> bool:
    """请求取消任务。本地任务：置 canceled 标记（探针收尾或结果被丢弃）；
    RQ 任务：queued 可真取消（cancel()），运行中标记后由探针协作收尾，
    都返回是否已受理。"""
    with _JOBS_LOCK:
        target = _CANCELABLE.get(job_id)
    if target is None:
        return False
    if target.local_job is not None:
        lj = target.local_job
        lj._canceled = True
        target.canceled = True
        return True
    if _redis_available():
        try:
            from rq.job import Job

            job = Job.fetch(job_id, connection=get_redis())
            if job.get_status() == "queued":
                job.cancel()
                target.canceled = True
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


def enqueue(queue: str, fn, *args, job_id: str | None = None,
            name: str | None = None, **kwargs):
    """入队。fn 接受 cancel_check / progress 形参时自动注入对应回调。

    协作式取消：长任务在批次间轮询 cancel_check()，返回 True 就提前收尾。
    本地降级线程无法强杀 —— 任务体不配合时，cancel 只能保证状态标记与
    结果丢弃，线程跑到自然结束。
    progress：进度回调 set_progress(job_id, ...) 的偏函数，任务体在阶段
    边界调用它 —— /ws/jobs/{id} 与任务中心列表据此流式渲染进度条。
    name：任务中心显示名（如「因子评价」），登记进进度注册表。
    """
    import inspect

    from lquant.server.progress import set_job_name

    jid = job_id or uuid.uuid4().hex[:12]

    if name:
        set_job_name(jid, name)

    # 探针注入统一在分支前：fn 签名有 cancel_check / progress 就注入（RQ / 本地一致）
    try:
        sig = inspect.signature(fn)
        has_cancel = "cancel_check" in sig.parameters
        has_progress = "progress" in sig.parameters
    except (TypeError, ValueError):
        has_cancel = has_progress = False

    if _redis_available():
        from rq import Queue

        if has_cancel:
            kwargs["cancel_check"] = lambda: _is_canceled(jid)  # noqa: E731
        if has_progress:
            kwargs["progress"] = _make_progress_cb(jid)  # noqa: E731
        q = Queue(queue, connection=get_redis())
        from rq.exceptions import InvalidJobOperation
        from rq.job import Job

        try:
            job = q.enqueue(fn, *args, job_id=jid, **kwargs)
        except InvalidJobOperation:
            # 同 job_id 旧 job 仍在（retry 重入队）：删旧再入，等价覆盖
            Job.fetch(jid, connection=get_redis()).delete()
            job = q.enqueue(fn, *args, job_id=jid, **kwargs)
        _register_cancelable(job.id, _CancelTarget(queue=queue, local_job=None))
        return job

    # 本地降级：后台线程执行，异常留在 job 上而不是炸请求方
    job = LocalJob(id=job_id) if job_id else LocalJob()
    job.queue = queue
    _register_job(job)
    _register_cancelable(job.id, _CancelTarget(queue=queue, local_job=job))
    _record_insert(job.id, name, queue)

    if has_cancel:
        kwargs["cancel_check"] = lambda jid=job.id: _is_canceled(jid)  # noqa: E731
    if has_progress:
        kwargs["progress"] = _make_progress_cb(job.id)

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
        finally:
            _record_update(
                job.id,
                "canceled" if job._canceled else ("failed" if job._error else "finished"),
                job._error)

    job._thread = threading.Thread(target=_run, name=f"localjob-{job.id}", daemon=True)
    job._thread.start()
    return job


def _recent_job_records(limit: int) -> list[dict]:
    """job_record 表按创建时间倒序的记录（与内存条目同构）。"""
    try:
        with reader() as con:
            rows = con.execute(
                "SELECT job_id, name, queue, status, error, created_at "
                "FROM job_record ORDER BY created_at DESC LIMIT ?",
                [int(limit)]).fetchall()
    except Exception:  # noqa: BLE001 - 表不存在按空处理
        return []
    return [{"id": r[0], "name": r[1], "queue": r[2], "status": r[3],
             "error": r[4],
             "created_at": r[5].timestamp() if r[5] is not None else 0.0}
            for r in rows]


def _make_progress_cb(job_id: str):
    """progress 回调工厂：签名 (done=None, total=None, phase="", message=None)。"""
    from lquant.server.progress import set_progress

    def _cb(**kw) -> None:  # noqa: ANN003
        set_progress(job_id, **kw)

    return _cb


def _is_canceled(job_id: str) -> bool:
    with _JOBS_LOCK:
        t = _CANCELABLE.get(job_id)
        return bool(t and t.canceled)


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
    from lquant.server.progress import get_job_name

    mem = [{"id": j.id, "status": j.get_status(), "queue": j.queue,
            "created_at": j.created_at, "error": j.error,
            "name": get_job_name(j.id)} for j in jobs]
    # 合并 DB 记录：重启后内存注册表空，历史靠 job_record 表续命。
    # 同 id 内存优先（状态更新鲜），name 以 DB 为准（内存侧不存名）。
    seen = {j["id"] for j in mem}
    db_rows = _recent_job_records(limit)
    names = {r["id"]: r.get("name") for r in db_rows}
    merged = [{**j, "name": names.get(j["id"]) or j.get("name")} for j in mem]
    for r in db_rows:
        if r["id"] not in seen:
            merged.append(r)
    merged.sort(key=lambda x: x.get("created_at", 0.0), reverse=True)
    return merged[:limit]
