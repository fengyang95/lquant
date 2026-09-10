"""start/stop_monitor 生命周期：enabled 开关 + 线程启停。"""
from __future__ import annotations

from unittest.mock import patch

import lquant.monitor as mon


def test_start_disabled(monkeypatch):
    with patch.object(mon.flusher, "start_flusher") as sf, \
         patch.object(mon.proc_sampler, "start_sampler") as ss:
        monkeypatch.setenv("LQ_MONITOR_ENABLED", "0")
        from lquant.core.config import get_settings

        get_settings.cache_clear()
        mon.start_monitor()
        mon.stop_monitor()
        sf.assert_not_called()
        ss.assert_not_called()
        get_settings.cache_clear()


def test_start_enabled_starts_threads(monkeypatch):
    monkeypatch.setenv("LQ_MONITOR_ENABLED", "1")
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    with patch.object(mon.flusher, "start_flusher", return_value=None) as sf, \
         patch.object(mon.proc_sampler, "start_sampler", return_value=None) as ss, \
         patch.object(mon.flusher, "stop_flusher") as sfn, \
         patch.object(mon.proc_sampler, "stop_sampler") as ssn:
        mon.start_monitor()
        sf.assert_called_once()
        ss.assert_called_once()
        mon.stop_monitor()
        sfn.assert_called_once()
        ssn.assert_called_once()
    get_settings.cache_clear()


def test_app_wraps_middleware_and_lifecycle_hooks():
    """create_app 返回 MonitorMiddleware 包裹的 ASGI 栈；startup/shutdown 钩子注册。"""
    import os

    os.environ.setdefault("LQ_SYNC_WORKER", "0")
    from fastapi.testclient import TestClient

    from lquant.monitor.api_mw import MonitorMiddleware
    from lquant.server.main import create_app

    app = create_app()
    assert isinstance(app, MonitorMiddleware)
    base = getattr(app, "app", app)  # MonitorMiddleware 包裹时取底层 FastAPI
    paths = set(base.openapi()["paths"])
    assert "/api/health" in paths  # 既有路由不被中间件包裹破坏
    with TestClient(app) as client:  # __enter__/__exit__ 触发 startup/shutdown
        r = client.get("/api/health")
        assert r.status_code == 200
