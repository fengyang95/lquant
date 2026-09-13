"""ApiRing / TaskEventQueue：线程安全、环绕淘汰、drain 原子性、不可变记录。"""
from __future__ import annotations

import threading

from lquant.monitor.ring import ApiRing, TaskEventQueue, api_ring, local_events
from lquant.monitor.types import ApiMetricPoint, TaskEvent


def _pt(i: int) -> ApiMetricPoint:
    return ApiMetricPoint(ts=float(i), route="/api/x", method="GET",
                          status=200, duration_ms=10.0, dur_category="fast")


def test_ring_wraparound():
    r = ApiRing(maxlen=3)
    for i in range(5):
        r.append(_pt(i))
    assert len(r.snapshot()) == 3
    assert r.snapshot()[-1].ts == 4.0
    assert r.snapshot()[0].ts == 2.0  # 最旧两个被淘汰


def test_ring_snapshot_is_immutable_copy():
    r = ApiRing(maxlen=3)
    r.append(_pt(1))
    snap = r.snapshot()
    r.append(_pt(2))
    assert len(snap) == 1  # snapshot 不随后续 append 变化
    assert isinstance(snap, tuple)


def test_ring_drain_atomic():
    r = ApiRing(maxlen=10)
    r.append(_pt(1))
    r.append(_pt(2))
    got = r.drain()
    assert len(got) == 2
    assert r.snapshot() == ()
    assert r.drain() == ()


def test_ring_concurrent_append():
    r = ApiRing(maxlen=1000)

    def push():
        for i in range(200):
            r.append(_pt(i))

    ts = [threading.Thread(target=push) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(r.snapshot()) == 800


def test_event_queue_drain_and_overflow():
    q = TaskEventQueue(maxlen=2)
    for i in range(4):
        q.put(TaskEvent(event_ts=float(i), job_id=f"j{i}", job_name="n",
                        event="finished", queue=None, enqueued_at=None,
                        started_at=None, finished_at=None, elapsed_ms=None,
                        queue_delay_ms=None, message=None))
    got = q.drain()
    assert [e.job_id for e in got] == ["j2", "j3"]  # 满则丢最旧
    assert q.drain() == ()


def test_frozen_types():
    import dataclasses

    import pytest

    p = _pt(1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.status = 500  # type: ignore[misc]


def test_package_singletons():
    assert isinstance(api_ring, ApiRing)
    assert isinstance(local_events, TaskEventQueue)
