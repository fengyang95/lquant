"""server/jobs.py 分支覆盖补齐：flusher 异常、探测缓存、取消、RQ 分支、注册表淘汰。"""
from __future__ import annotations

import threading
import time
import types

import pytest


@pytest.fixture
def local_env(tmp_path, monkeypatch):
    """隔离 LQ_ROOT + 强制本地降级模式（无 Redis）。"""
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("lquant.server.jobs._redis_available", lambda ttl=0: False)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_jobs_state():
    """用例间清理进程内注册表，避免相互污染。"""
    from lquant.server import jobs

    jobs._LOCAL_JOBS.clear()
    jobs._CANCELABLE.clear()
    yield
    jobs._LOCAL_JOBS.clear()
    jobs._CANCELABLE.clear()


def test_flusher_exception_is_logged(local_env, monkeypatch) -> None:
    """落库失败：flusher 捕获并 logger.exception，队列照样 task_done 不卡 join。"""
    from loguru import logger

    from lquant.server import jobs

    jobs._ensure_record_thread()
    records: list = []
    hid = logger.add(records.append, level="DEBUG")

    class _Boom:
        def __enter__(self):
            raise RuntimeError("db lock")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(jobs, "writer", lambda: _Boom())
    try:
        jobs._record_update("probe-flusher", "finished")
        deadline = time.monotonic() + 3
        while not any("批量落库失败" in str(m) for m in records):
            if time.monotonic() > deadline:
                pytest.fail("flusher 异常未被日志留痕")
            time.sleep(0.05)
    finally:
        logger.remove(hid)


def test_flusher_batch_task_done(local_env) -> None:
    """批量 flush：多个条目一次落库后逐个 task_done，queue.join() 可返回。"""
    from lquant.server import jobs
    from lquant.server.jobs import _RECORD_QUEUE, get_job_record

    jobs._ensure_record_thread()
    for i in range(3):
        jobs._record_insert(f"batch-{i}", None, "lquant-default")
    _RECORD_QUEUE.join()
    assert get_job_record("batch-0") is not None
    assert get_job_record("batch-2") is not None


def test_get_job_record_row_null_created_at(local_env) -> None:
    """created_at 为 NULL 的历史行 → created_at 0.0。"""
    from lquant.core.db import writer
    from lquant.server.jobs import get_job_record

    with writer() as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS job_record ("
            "job_id VARCHAR PRIMARY KEY, name VARCHAR, queue VARCHAR, "
            "status VARCHAR, error VARCHAR, created_at TIMESTAMP, updated_at TIMESTAMP)")
        con.execute(
            "INSERT INTO job_record VALUES ('null-ts', 'n', 'q', 'started', NULL, NULL, NULL)")
    rec = get_job_record("null-ts")
    assert rec is not None and rec["created_at"] == 0.0


def test_get_job_record_read_error_returns_none(local_env, monkeypatch) -> None:
    from lquant.server import jobs

    class _BoomCon:
        def __enter__(self):
            raise RuntimeError("reader down")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(jobs, "reader", _BoomCon)
    assert jobs.get_job_record("x") is None  # 连接失败与查询失败统一按缺失处理


def test_get_job_record_query_error_returns_none(local_env, monkeypatch) -> None:
    """查询失败（如表不存在）按缺失处理返回 None。"""
    from lquant.server import jobs

    class _BoomCon:
        def __enter__(self):
            return types.SimpleNamespace(
                execute=lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no table")))

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(jobs, "reader", lambda: _BoomCon())
    assert jobs.get_job_record("x") is None


def test_mark_interrupted_failure_returns_zero(local_env, monkeypatch) -> None:
    from lquant.server import jobs

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(jobs, "_ensure_job_record_once", _boom)
    assert jobs.mark_interrupted_jobs() == 0


def test_probe_redis_success_and_failure(monkeypatch, local_env) -> None:
    import redis

    from lquant.server import jobs

    class FakeClient:
        def __init__(self, ok):
            self._ok = ok

        def ping(self):
            if not self._ok:
                raise ConnectionError("down")
            return True

    monkeypatch.setattr(redis, "from_url",
                        lambda url, socket_connect_timeout=1: FakeClient(True))
    assert jobs._probe_redis() is True

    monkeypatch.setattr(redis, "from_url",
                        lambda url, socket_connect_timeout=1: FakeClient(False))
    assert jobs._probe_redis() is False

    def _boom(*a, **kw):
        raise RuntimeError("no redis pkg path")

    monkeypatch.setattr(redis, "from_url", _boom)
    assert jobs._probe_redis() is False


def test_get_redis_builds_client(monkeypatch, local_env) -> None:
    import redis

    from lquant.server import jobs

    sentinel = object()
    monkeypatch.setattr(redis, "from_url", lambda url: sentinel)
    jobs.get_redis.cache_clear()
    try:
        assert jobs.get_redis() is sentinel
    finally:
        jobs.get_redis.cache_clear()


def test_local_job_status_branches() -> None:
    from lquant.server.jobs import LocalJob

    j = LocalJob()
    j._thread = None
    j._error = None
    assert j.get_status() == "finished"
    assert j.result is None
    j._result = {"ok": 1}
    assert j.result == {"ok": 1}
    j._error = "boom"
    assert j.get_status() == "failed"
    assert j.error == "boom"
    j2 = LocalJob()
    j2._canceled = True
    assert j2.get_status() == "canceled"


def test_register_cancelable_eviction(local_env) -> None:
    """登记表容量兜底：优先淘汰已取消条目；无可淘汰时删最旧。"""
    from lquant.server import jobs

    jobs._CANCELABLE.clear()
    canceled = jobs._CancelTarget(queue="q", local_job=None)
    canceled.canceled = True
    jobs._register_cancelable("old-canceled", canceled)
    for i in range(jobs._CANCELABLE_MAX - 1):
        jobs._register_cancelable(f"j{i}", jobs._CancelTarget(queue="q", local_job=None))
    assert "old-canceled" in jobs._CANCELABLE
    jobs._register_cancelable("new", jobs._CancelTarget(queue="q", local_job=None))
    assert "old-canceled" not in jobs._CANCELABLE  # 已取消的最先被淘汰
    assert "new" in jobs._CANCELABLE

    # 无已取消条目 → 删最旧
    jobs._CANCELABLE.clear()
    for i in range(jobs._CANCELABLE_MAX):
        jobs._register_cancelable(f"k{i}", jobs._CancelTarget(queue="q", local_job=None))
    jobs._register_cancelable("kn", jobs._CancelTarget(queue="q", local_job=None))
    assert "k0" not in jobs._CANCELABLE and "kn" in jobs._CANCELABLE


def test_ensure_record_thread_double_check_inner(local_env) -> None:
    """内层双检：持锁瞬间另一线程已起 flusher → 直接返回（用假锁模拟）。"""
    from lquant.server import jobs

    jobs._ensure_record_thread()
    original = jobs._RECORD_THREAD

    class SneakyLock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    try:
        jobs._RECORD_THREAD = None  # 让外层快路径放行
        real_lock = threading.Lock()

        class RaceLock:
            def __enter__(self):
                jobs._RECORD_THREAD = original  # 进锁瞬间另一线程已完成启动
                return self

            def __exit__(self, *a):
                return False

        jobs._record_thread_lock = RaceLock()
        jobs._ensure_record_thread()
        assert jobs._RECORD_THREAD is original
    finally:
        jobs._record_thread_lock = real_lock
        jobs._RECORD_THREAD = original


class _AliveThread:
    def is_alive(self):
        return True


def test_register_job_evicts_oldest_finished(local_env) -> None:
    from lquant.server import jobs

    jobs._LOCAL_JOBS.clear()
    for _ in range(jobs._LOCAL_JOBS_MAX):
        probe = jobs.LocalJob()
        probe._thread = None  # 已结束 → finished，可被淘汰
        jobs._LOCAL_JOBS[probe.id] = probe
    new = jobs.LocalJob()
    jobs._register_job(new)
    assert new.id in jobs._LOCAL_JOBS
    assert len(jobs._LOCAL_JOBS) <= jobs._LOCAL_JOBS_MAX

    # 全是运行中任务时无可淘汰：容量兜底放行（超出 max）
    jobs._LOCAL_JOBS.clear()
    for _ in range(jobs._LOCAL_JOBS_MAX):
        r = jobs.LocalJob()
        r._thread = _AliveThread()
        jobs._register_job(r)
    jobs._register_job(jobs.LocalJob())
    assert len(jobs._LOCAL_JOBS) == jobs._LOCAL_JOBS_MAX + 1


def test_get_job_local_hit_and_miss(local_env) -> None:
    from lquant.server import jobs

    j = jobs.LocalJob()
    jobs._LOCAL_JOBS[j.id] = j
    assert jobs.get_job(j.id) is j
    jobs._LOCAL_JOBS.clear()
    assert jobs.get_job("ghost") is None  # redis 不可用分支


def test_get_job_redis_fetch(local_env, monkeypatch) -> None:
    import rq.job

    from lquant.server import jobs

    jobs._LOCAL_JOBS.clear()
    monkeypatch.setattr(jobs, "_redis_available", lambda ttl=0: True)

    class FakeJob:
        @staticmethod
        def fetch(job_id, connection):
            if job_id == "boom":
                raise RuntimeError("gone")
            return "rq-job"

    monkeypatch.setattr(rq.job, "Job", FakeJob)
    assert jobs.get_job("rid") == "rq-job"
    assert jobs.get_job("boom") is None


def test_request_cancel_paths(local_env, monkeypatch) -> None:
    import rq.job

    from lquant.server import jobs

    assert jobs.request_cancel("ghost") is False  # 未登记

    lj = jobs.LocalJob()
    jobs._register_cancelable(lj.id, jobs._CancelTarget(queue="q", local_job=lj))
    assert jobs.request_cancel(lj.id) is True
    assert lj._canceled is True

    # RQ 分支：queued → cancel() 受理
    monkeypatch.setattr(jobs, "_redis_available", lambda ttl=0: True)
    fake_rq_job = types.SimpleNamespace(get_status=lambda: "queued", cancel=lambda: None)

    class FakeJob:
        @staticmethod
        def fetch(job_id, connection):
            if job_id == "boom":
                raise RuntimeError("gone")
            return fake_rq_job

    monkeypatch.setattr(rq.job, "Job", FakeJob)
    jobs._register_cancelable("rq1", jobs._CancelTarget(queue="q", local_job=None))
    assert jobs.request_cancel("rq1") is True
    # 运行中（非 queued）→ 不受理
    fake_rq_job.get_status = lambda: "started"
    assert jobs.request_cancel("rq1") is False
    # fetch 抛异常 → 不受理
    assert jobs.request_cancel("boom") is False


def test_enqueue_local_with_progress_and_cancel(local_env) -> None:
    """任务体带 cancel_check/progress 形参 → 自动注入回调；取消标记可被任务体轮询。"""
    import threading

    from lquant.server import jobs

    seen: dict = {}
    started = threading.Event()
    release = threading.Event()

    def task(cancel_check=None, progress=None):
        seen["has_progress"] = progress is not None
        progress(done=1, total=2, phase="p1")
        started.set()
        release.wait(2)
        seen["canceled"] = cancel_check()

    job = jobs.enqueue("lquant-default", task, job_id="probe-cb")
    assert started.wait(2), "任务线程未启动"
    assert jobs.request_cancel(job.id) is True
    release.set()
    job._thread.join(5)

    assert seen["has_progress"] is True
    assert seen["canceled"] is True
    from lquant.server.progress import get_progress

    assert get_progress(job.id) is not None


def test_enqueue_local_failure_records_error(local_env) -> None:
    from lquant.server import jobs
    from lquant.server.jobs import _RECORD_QUEUE, get_job_record

    def boom():
        raise ValueError("炸")

    job = jobs.enqueue("lquant-default", boom)
    job._thread.join(5)
    assert job.error and "ValueError" in job.error
    assert job.get_status() == "failed"
    _RECORD_QUEUE.join()
    rec = get_job_record(job.id)
    assert rec is not None and rec["status"] == "failed"


def test_enqueue_rq_branch(local_env, monkeypatch) -> None:
    """Redis 可用 → 走 RQ；回调注入与 InvalidJobOperation 覆盖重入队。"""
    import rq
    import rq.exceptions
    import rq.job

    from lquant.server import jobs

    monkeypatch.setattr(jobs, "_redis_available", lambda ttl=0: True)

    captured: dict = {}
    _InvalidJobOperation = type("InvalidJobOperation", (Exception,), {})

    class FakeQueue:
        def __init__(self, name, connection):
            captured["queue"] = name

        def enqueue(self, fn, *args, job_id=None, **kw):
            captured["job_id"] = job_id
            captured["kw"] = kw
            if not captured.get("retried"):
                captured["retried"] = True
                raise _InvalidJobOperation()
            return types.SimpleNamespace(id=job_id)

    monkeypatch.setattr(rq, "Queue", FakeQueue)
    monkeypatch.setattr(rq.exceptions, "InvalidJobOperation", _InvalidJobOperation)
    # 覆盖重入队的前提是残留 job 已是终态：get_status 返回 finished
    # （修复 43：queued/started/deferred/scheduled 会被拒绝而非覆盖）
    monkeypatch.setattr(rq.job, "Job", type("Job", (object,), {
        "fetch": staticmethod(
            lambda jid, connection: types.SimpleNamespace(
                delete=lambda: None,
                get_status=lambda refresh=False: "finished"))}))

    def task(x, cancel_check=None, progress=None):
        return x

    job = jobs.enqueue("lquant-default", task, 42, job_id="rq-probe", name="RQ 任务")
    assert job.id == "rq-probe"
    assert captured["queue"] == "lquant-default"
    assert captured["kw"]["cancel_check"]() is False
    assert captured["kw"]["progress"] is not None
    from lquant.server.progress import get_job_name

    assert get_job_name("rq-probe") == "RQ 任务"


def test_enqueue_signature_probe_rejects_uninspectable(local_env) -> None:
    """fn 不可内省（builtin 无签名）→ 不注入回调，照常执行。"""
    from lquant.server import jobs

    job = jobs.enqueue("lquant-default", print)
    job._thread.join(5)
    assert job.get_status() == "finished"


def test_list_recent_redis_branch(local_env, monkeypatch) -> None:
    """Redis 模式下列表来自 RQ 各注册表；抖动时返回 []。"""
    import rq
    import rq.job
    import rq.registry

    from lquant.server import jobs

    monkeypatch.setattr(jobs, "_redis_available", lambda ttl=0: True)

    fake_job = types.SimpleNamespace(
        get_id=lambda: "rj1", get_status=lambda: "finished", enqueued_at=None)

    class FakeQueue:
        def __init__(self, name, connection):
            pass

        def get_jobs(self):
            return [fake_job]

    class FakeReg:
        def __init__(self, name, connection):
            pass

        def get_jobs(self):
            return []

    class FakeFin:
        def __init__(self, name, connection):
            pass

        def get_job_ids(self):
            return ["gone"]

    monkeypatch.setattr(rq, "Queue", FakeQueue)
    monkeypatch.setattr(rq.registry, "StartedJobRegistry", FakeReg)
    monkeypatch.setattr(rq.registry, "FailedJobRegistry", FakeReg)
    monkeypatch.setattr(rq.registry, "FinishedJobRegistry", FakeFin)
    monkeypatch.setattr(rq.job, "Job", type("Job", (object,), {
        "fetch": staticmethod(
            lambda jid, connection: (_ for _ in ()).throw(RuntimeError))}))

    out = jobs.list_recent_jobs(limit=10)
    assert out and out[0]["id"] == "rj1"
    assert out[0]["created_at"] == 0.0

    # Redis 抖动 → 空列表
    def _boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr(jobs, "get_redis", _boom)
    assert jobs.list_recent_jobs(limit=10) == []


def test_recent_job_records_read_error(local_env, monkeypatch) -> None:
    from lquant.server import jobs

    def _boom():
        raise RuntimeError("down")

    monkeypatch.setattr(jobs, "reader", _boom)
    assert jobs._recent_job_records(5) == []


def test_ensure_job_record_once_idempotent(local_env) -> None:
    """同路径第二次调用走快路径返回（幂等）。"""
    from lquant.server import jobs

    jobs._ENSURED_PATH = None
    jobs._ensure_job_record_once()
    first = jobs._ENSURED_PATH
    assert first
    jobs._ensure_job_record_once()
    assert first == jobs._ENSURED_PATH


def test_ensure_record_thread_fast_path(local_env) -> None:
    """flusher 存活时直接返回，不重复起线程。"""
    from lquant.server import jobs

    jobs._ensure_record_thread()
    t = jobs._RECORD_THREAD
    assert t is not None
    jobs._ensure_record_thread()
    assert jobs._RECORD_THREAD is t
