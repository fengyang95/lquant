"""emit_task_event：Redis 可用走 list、不可用落内存队列、失败只 log。"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from lquant.monitor import emit as emit_mod
from lquant.monitor.ring import local_events
from lquant.monitor.types import TaskEvent


def _emit(**kw):
    kw.setdefault("event", "finished")
    kw.setdefault("job_id", "j1")
    kw.setdefault("job_name", "demo_fn")
    kw.setdefault("queue", "lquant-default")
    kw.setdefault("enqueued_at", 100.0)
    kw.setdefault("started_at", 110.0)
    kw.setdefault("finished_at", 120.5)
    emit_mod.emit_task_event(**kw)


def test_fields_derived():
    ev = emit_mod._build_event(
        event="finished", job_id="j1", job_name="n", queue="q",
        enqueued_at=100.0, started_at=110.0, finished_at=120.5, message=None)
    assert isinstance(ev, TaskEvent)
    assert ev.elapsed_ms == 10500.0
    assert ev.queue_delay_ms == 10000.0


def test_delay_none_when_missing():
    ev = emit_mod._build_event(event="started", job_id="j1", job_name="n",
                               queue="q", enqueued_at=None, started_at=5.0,
                               finished_at=None, message=None)
    assert ev.elapsed_ms is None
    assert ev.queue_delay_ms is None


def test_redis_unavailable_goes_memory():
    local_events.drain()
    with patch.object(emit_mod, "_redis_available", return_value=False):
        _emit()
    got = local_events.drain()
    assert len(got) == 1
    assert got[0].job_id == "j1"
    assert got[0].elapsed_ms == 10500.0


def test_redis_pushes_two_lists():
    local_events.drain()
    fake = MagicMock()
    with patch.object(emit_mod, "_redis_available", return_value=True), \
         patch.object(emit_mod, "_get_redis", return_value=fake):
        _emit()
    assert fake.lpush.call_count == 2
    keys = [c.args[0] for c in fake.lpush.call_args_list]
    assert keys == ["lquant:monitor:events", "lquant:monitor:recent"]
    payload = json.loads(fake.lpush.call_args_list[0].args[1])
    assert payload["queue_delay_ms"] == 10000.0
    fake.ltrim.assert_called_once_with("lquant:monitor:recent", 0, 49)
    assert local_events.drain() == ()


def test_redis_error_falls_back_to_memory():
    local_events.drain()
    fake = MagicMock()
    fake.lpush.side_effect = RuntimeError("boom")
    with patch.object(emit_mod, "_redis_available", return_value=True), \
         patch.object(emit_mod, "_get_redis", return_value=fake):
        _emit()
    assert len(local_events.drain()) == 1  # Redis 失败兜底进内存队列
