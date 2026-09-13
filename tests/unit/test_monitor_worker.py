"""MonitoringWorker 事件发射 + current_job_fn registry 匹配（mock RQ）。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from lquant.monitor import emit as emit_mod  # noqa: F401
from lquant.monitor import worker as wk


def _mk_job(status="finished"):
    job = MagicMock()
    job.get_status.return_value = status
    job.id = "j9"
    job.enqueued_at = 100.0
    return job


def _run_perform(status, result_of_latest=None):
    """直接调 MonitoringWorker.perform_job 的包装逻辑（不触发真实 RQ）。"""
    evs = []

    def fake_emit(**kw):
        evs.append(kw)

    job = _mk_job(status)
    latest = MagicMock()
    if result_of_latest is not None:
        latest.result.return_value = result_of_latest  # Result.Failure 返回值是 exc 字符串

    with patch.object(wk, "emit_task_event", fake_emit), \
         patch.object(wk.Worker, "__init__", lambda self, *a, **k: None), \
         patch.object(wk.Worker, "perform_job", return_value=status == "finished"), \
         patch.object(wk, "_latest_error", return_value=result_of_latest):
        w = wk.MonitoringWorker(["lquant-default"])
        queue = MagicMock()
        queue.name = "lquant-default"
        w.perform_job(job, queue)
    return evs, job


def test_perform_started_and_finished():
    evs, _ = _run_perform("finished")
    assert evs[0]["event"] == "started"
    assert evs[-1]["event"] == "finished"
    assert evs[-1]["job_id"] == "j9"


def test_perform_failed_with_message():
    evs, _ = _run_perform("failed", result_of_latest="ValueError: bad")
    assert evs[-1]["event"] == "failed"
    assert evs[-1]["message"] == "ValueError: bad"


def test_started_event_has_no_finished():
    evs, _ = _run_perform("finished")
    assert evs[0]["finished_at"] is None
    assert evs[0]["queue"] == "lquant-default"


def test_current_job_fn_matches_pid():
    w = MagicMock()
    w.pid = 4242
    w.get_current_job_id.return_value = "job-zz"
    with patch.object(wk, "Worker") as fake_w:
        fake_w.all.return_value = [w]
        with patch.object(wk.os, "getpid", return_value=4242):
            assert wk.current_job_fn() == "job-zz"


def test_current_job_fn_no_match():
    with patch.object(wk, "Worker") as fake_w:
        fake_w.all.return_value = []
        assert wk.current_job_fn() is None
