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


def _clear_ring() -> None:
    from lquant.monitor.ring import api_ring

    api_ring.drain()


def test_disabled_no_ring_capture(monkeypatch, tmp_path):
    """enabled=False：请求不进环缓冲，stop_flusher 不做 final flush。"""
    import asyncio

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LQ_MONITOR_ENABLED", "0")
    monkeypatch.setenv("LQ_MONITOR_DB", str(tmp_path / "off.duckdb"))
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    try:
        _clear_ring()
        from lquant.monitor.api_mw import MonitorMiddleware

        async def app(scope, receive, send):  # noqa: ANN001
            await send({"type": "http.response.start", "status": 200,
                        "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def receive():  # noqa: ANN001
            return {"type": "http.request", "body": b"", "more_body": False}

        mw = MonitorMiddleware(app)
        scope = {"type": "http", "path": "/api/factors", "method": "GET"}
        asyncio.run(mw(scope, receive, lambda m: asyncio.sleep(0)))

        from lquant.monitor.ring import api_ring

        assert len(api_ring.drain()) == 0  # 未采集

        # stop_flusher 未启动过：不做 final flush，不创建 duckdb 文件
        from lquant.monitor import flusher

        flusher.stop_flusher()
        assert not (tmp_path / "off.duckdb").exists()
    finally:
        get_settings.cache_clear()


def test_disabled_flush_once_no_db(monkeypatch, tmp_path):
    """enabled=False 时 flush_once 直接落盘也可能建库——验证开关下不写库。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LQ_MONITOR_ENABLED", "0")
    monkeypatch.setenv("LQ_MONITOR_DB", str(tmp_path / "off2.duckdb"))
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    try:
        _clear_ring()
        from lquant.monitor import flusher

        flusher.flush_once()
        assert not (tmp_path / "off2.duckdb").exists()
    finally:
        get_settings.cache_clear()
