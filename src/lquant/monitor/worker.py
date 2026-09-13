"""MonitoringWorker（任务事件发射）+ current_job_fn（worker registry 匹配）。"""
from __future__ import annotations

import logging
import os
import signal
import threading
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


GENERAL_QUEUES = ["lquant-default", "lquant-ingest"]
BACKTEST_QUEUE = "lquant-backtest"

# supervisor 优雅停机标志（_shutdown / stop_supervisor 置位）
_supervisor_stop = threading.Event()


def stop_supervisor() -> None:
    """请求 supervisor watchdog 退出（优雅停机入口）。"""
    _supervisor_stop.set()


def _redis_ok() -> bool:
    from lquant.server.jobs import _redis_available

    return _redis_available()


def spawn_worker(name: str, queues: list[str]) -> None:
    """子进程入口（top-level，spawn 可 pickle）。Redis 失败 exit 1。"""
    if not _redis_ok():
        print("错误: Redis 不可用 —— 先启动 Redis（docker compose up -d redis）",
              file=__import__("sys").stderr)
        raise SystemExit(1)
    from lquant.monitor import proc_sampler

    w = MonitoringWorker(queues)
    proc_sampler.start_sampler(name, current_job_fn=current_job_fn)
    try:
        w.run()
    finally:
        proc_sampler.stop_sampler()


def _spawn_process(name: str, queues: list[str]):
    """独立函数便于测试替身。"""
    import multiprocessing

    ctx = multiprocessing.get_context("spawn")
    p = ctx.Process(target=spawn_worker, args=(name, queues), name=name,
                    daemon=True)
    p.start()
    return p


def run_supervisor(general: int, backtest: int) -> None:
    """拉起 worker 组 + watchdog respawn；Ctrl-C → 子进程 SIGINT → 5s grace → terminate。"""
    if not _redis_ok():
        print("错误: Redis 不可用 —— 先启动 Redis（docker compose up -d redis）",
              file=__import__("sys").stderr)
        raise SystemExit(1)
    _supervisor_stop.clear()
    children: list = []
    for i in range(general):
        children.append(_spawn_process(f"general-{i}", GENERAL_QUEUES))
    for i in range(backtest):
        children.append(_spawn_process(f"backtest-{i}", [BACKTEST_QUEUE]))
    print(f"[worker] 已拉起 {len(children)} 个 worker 进程")

    def _shutdown(signum, frame):
        _supervisor_stop.set()
        for p in children:
            if p.is_alive():
                import os as _os

                _os.kill(p.pid, signal.SIGINT)  # RQ friendly shutdown
        deadline = time.time() + 5
        for p in children:
            p.join(timeout=max(0.1, deadline - time.time()))
        for p in children:
            if p.is_alive():
                p.terminate()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    while not _supervisor_stop.is_set():
        time.sleep(5)
        for idx, p in enumerate(children):
            if not p.is_alive():
                _LOG.warning("worker %s 退出(code=%s)，respawn", p.name,
                             p.exitcode)
                name, queues = p.name, (
                    GENERAL_QUEUES if p.name.startswith("general-")
                    else [BACKTEST_QUEUE])
                children[idx] = _spawn_process(name, queues)
