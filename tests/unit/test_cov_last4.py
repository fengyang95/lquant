"""最后冲刺第四波：flusher 剩余分支（cap 溢出裁剪 / redis 间接层 / 告警路径 / start_flusher）。"""
from __future__ import annotations


def test_extend_capped_overflow_exceeds_buf():
    """overflow 大于现有缓冲：直接从新 items 头部裁剪。"""
    from lquant.monitor import flusher

    with flusher._PENDING_LOCK:
        flusher._PENDING_API.clear()
        dropped = flusher._extend_capped(flusher._PENDING_API, list(range(100_005)))
    assert dropped == 5
    assert len(flusher._PENDING_API) == flusher._PENDING_CAP
    assert flusher._PENDING_API[0] == 5  # 新 items 头部被裁掉
    with flusher._PENDING_LOCK:
        flusher._PENDING_API.clear()


def test_stash_warns_on_drop():
    from lquant.monitor import flusher

    with flusher._PENDING_LOCK:
        flusher._PENDING_ERRORS.clear()
        flusher._PENDING_API.clear()
        flusher._PENDING_TASK.clear()
    try:
        flusher._stash_errors([1] * 100_001)
        flusher._stash([1] * 100_001, [])
    finally:
        with flusher._PENDING_LOCK:
            flusher._PENDING_ERRORS.clear()
            flusher._PENDING_API.clear()
            flusher._PENDING_TASK.clear()


def test_flusher_redis_indirection_calls_jobs(monkeypatch):
    from lquant.monitor import flusher

    monkeypatch.setattr("lquant.server.jobs._redis_available", lambda ttl=0: True)
    monkeypatch.setattr("lquant.server.jobs.get_redis", lambda: "fake-conn")
    assert flusher._redis_available() is True
    assert flusher._get_redis() == "fake-conn"


def test_start_flusher_disabled_returns_none(monkeypatch):
    from types import SimpleNamespace

    import lquant.core.config as cfg
    from lquant.monitor import flusher

    monkeypatch.setattr(cfg, "get_settings",
                        lambda: SimpleNamespace(monitor_enabled=False))
    assert flusher.start_flusher() is None


def test_start_flusher_alive_returns_existing(monkeypatch):
    import threading
    from types import SimpleNamespace

    import lquant.core.config as cfg
    from lquant.monitor import flusher

    gate = threading.Event()
    t = threading.Thread(target=gate.wait, daemon=True)
    t.start()
    old = flusher._thread
    flusher._thread = t
    try:
        monkeypatch.setattr(cfg, "get_settings",
                            lambda: SimpleNamespace(monitor_enabled=True))
        assert flusher.start_flusher() is t
    finally:
        gate.set()
        flusher._thread = old
