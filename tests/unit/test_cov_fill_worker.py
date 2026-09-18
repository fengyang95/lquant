"""批次一覆盖补充：monitor/worker.py spawn/supervisor/错误提取分支。"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from lquant.monitor import worker as wk


def _mk_job(status="finished"):
    job = MagicMock()
    job.get_status.return_value = status
    job.id = "j9"
    job.enqueued_at = 100.0
    return job


# ---------- _latest_error / _to_ts ----------


def test_latest_error_none_result():
    job = MagicMock()
    job.latest_result.return_value = None
    assert wk._latest_error(job) is None


def test_latest_error_failure_truncates():
    res = MagicMock()
    res.type.name = "Failure"
    res.return_value = "x" * 300
    job = MagicMock()
    job.latest_result.return_value = res
    msg = wk._latest_error(job)
    assert msg is not None and len(msg) == 200 and msg.startswith("x")


def test_latest_error_non_failure_and_exception():
    res = MagicMock()
    res.type.name = "SuccessfulJob"
    job = MagicMock()
    job.latest_result.return_value = res
    assert wk._latest_error(job) is None
    job2 = MagicMock()
    job2.latest_result.side_effect = RuntimeError("boom")
    assert wk._latest_error(job2) is None


def test_to_ts_variants():
    assert wk._to_ts(None) is None
    dt = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert wk._to_ts(dt) == dt.timestamp()
    assert wk._to_ts("3.5") == 3.5  # 无 timestamp 属性 → float() 兜底


# ---------- current_job_fn / stop_supervisor / _redis_ok ----------


def test_current_job_fn_exception_returns_none():
    with patch.object(wk, "Worker") as fake:
        fake.all.side_effect = RuntimeError("no redis")
        assert wk.current_job_fn() is None


def test_stop_supervisor_sets_event():
    wk._supervisor_stop.clear()
    wk.stop_supervisor()
    assert wk._supervisor_stop.is_set()
    wk._supervisor_stop.clear()


def test_spawn_worker_redis_down():
    with patch.object(wk, "_redis_ok", return_value=False):
        with pytest.raises(SystemExit) as ei:
            wk.spawn_worker("w", ["q"])
        assert ei.value.code == 1


def test_spawn_worker_runs_and_stops_sampler():
    started, stopped = [], []

    class FakeW:
        def __init__(self, queues):
            self.queues = queues

        def run(self):
            started.append(True)

    from lquant.monitor import proc_sampler

    with patch.object(wk, "_redis_ok", return_value=True), \
         patch.object(wk, "MonitoringWorker", FakeW), \
         patch.object(proc_sampler, "start_sampler",
                      lambda name, current_job_fn=None: started.append(name)), \
         patch.object(proc_sampler, "stop_sampler",
                      lambda: stopped.append(True)):
        wk.spawn_worker("general-0", wk.GENERAL_QUEUES)
        assert started == ["general-0", True]
    assert stopped == [True]


def test_run_supervisor_redis_down():
    with patch.object(wk, "_redis_ok", return_value=False):
        with pytest.raises(SystemExit) as ei:
            wk.run_supervisor(1, 1)
        assert ei.value.code == 1


class _FakeProc:
    def __init__(self, name, alive=True):
        self.name = name
        self.pid = 999999999  # 不存在的 pid；os.kill 已被替换
        self.exitcode = 0
        self._alive = alive

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        pass

    def terminate(self):
        self._alive = False


def test_run_supervisor_respawn_and_shutdown():
    """watchdog respawn + SIGTERM 优雅停机（不真拉子进程）。"""
    wk._supervisor_stop.clear()
    spawned = []
    handlers = {}

    def fake_spawn(name, queues):
        spawned.append(name)
        # 第二轮 general-0 已死 → 触发 respawn 分支
        return _FakeProc(name, alive=len(spawned) == 1)

    def fake_sleep(_):
        wk._supervisor_stop.set()

    def fake_signal(sig, fn):
        handlers[sig] = fn

    killed = []
    real_kill = os.kill

    def fake_kill(pid, sig):
        killed.append((pid, sig))

    with patch.object(wk, "_redis_ok", return_value=True), \
         patch.object(wk, "_spawn_process", fake_spawn), \
         patch.object(wk.signal, "signal", fake_signal), \
         patch.object(time, "sleep", fake_sleep), \
         patch.object(os, "kill", fake_kill):
        wk.run_supervisor(1, 1)
        assert spawned == ["general-0", "backtest-0", "backtest-0"]
        import signal as signal_mod

        with pytest.raises(SystemExit) as ei:
            handler = handlers[signal_mod.SIGTERM]
            handler(signal_mod.SIGTERM, None)
        assert ei.value.code == 0
        assert len(killed) == 1  # 仅存活的 general-0 被发 SIGINT，已死的直接 join
    wk._supervisor_stop.clear()


def test_run_supervisor_shutdown_kills_alive_children():
    """停机时存活子进程先 SIGINT 再 join/terminate。"""
    wk._supervisor_stop.clear()
    handlers = {}

    def fake_spawn(name, queues):
        return _FakeProc(name, alive=True)

    def fake_signal(sig, fn):
        handlers[sig] = fn

    killed = []

    with patch.object(wk, "_redis_ok", return_value=True), \
         patch.object(wk, "_spawn_process", fake_spawn), \
         patch.object(wk.signal, "signal", fake_signal), \
         patch.object(time, "sleep", lambda _: wk._supervisor_stop.set()), \
         patch.object(os, "kill", lambda pid, sig: killed.append(sig)):
        wk.run_supervisor(2, 0)
        import signal as signal_mod

        with pytest.raises(SystemExit):
            handler = handlers[signal_mod.SIGINT]
            handler(signal_mod.SIGINT, None)
        assert killed == [signal_mod.SIGINT, signal_mod.SIGINT]


def test_redis_ok_delegates_to_server_jobs():
    import lquant.server.jobs as jobs_mod

    with patch.object(jobs_mod, "_redis_available", return_value=True):
        assert wk._redis_ok() is True
    with patch.object(jobs_mod, "_redis_available", return_value=False):
        assert wk._redis_ok() is False


def test_spawn_process_starts_real_subprocess():
    """_spawn_process 真拉 spawn 子进程：Redis 缺失时子进程 exit 1。"""
    p = wk._spawn_process("covtest-worker", wk.GENERAL_QUEUES)
    assert p.name == "covtest-worker"
    p.join(timeout=20)
    assert p.exitcode == 1  # 子进程 Redis 不可用 → SystemExit(1)


def test_perform_job_passes_through_ok():
    evs = []

    with patch.object(wk, "emit_task_event", lambda **kw: evs.append(kw)), \
         patch.object(wk.Worker, "__init__", lambda self, *a, **k: None), \
         patch.object(wk.Worker, "perform_job", return_value=True), \
         patch.object(wk, "_latest_error", return_value=None):
        w = wk.MonitoringWorker(["q"])
        job = _mk_job("finished")
        queue = MagicMock()
        queue.name = "q"
        assert w.perform_job(job, queue) is True
    assert evs[-1]["message"] is None
