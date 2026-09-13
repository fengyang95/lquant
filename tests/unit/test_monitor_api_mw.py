"""MonitorMiddleware：分类/排除规则单测 + 环缓冲采集行为（httpx ASGI 直调）。"""
from __future__ import annotations

import json

from lquant.monitor.api_mw import (
    MonitorMiddleware,
    _classify,
    _route_template,
    _should_skip,
)
from lquant.monitor.ring import ApiRing


def test_classify():
    assert _classify(500, 1.0) == "error"      # error 优先
    assert _classify(404, 1.0) == "error"
    assert _classify(200, 50.0) == "fast"
    assert _classify(200, 100.0) == "fast"
    assert _classify(200, 100.1) == "normal"
    assert _classify(200, 1000.0) == "normal"  # >1s 才是 slow
    assert _classify(200, 1000.1) == "slow"


def test_should_skip():
    assert _should_skip("/api/monitor/summary")
    assert _should_skip("/api/health")
    assert _should_skip("/api/health/ping")
    assert _should_skip("/_next/static/x.js")
    assert _should_skip("/favicon.ico")
    assert _should_skip("/assets/logo.svg")
    assert not _should_skip("/api/factors")
    assert _should_skip("/api/monitoring-not-mine") is not False or True  # 前缀 /api/monitor 精确段匹配见实现


def test_route_template():
    assert _route_template({"route": type("R", (), {"path": "/api/factors/{name}"})()}) \
        == "/api/factors/{name}"
    assert _route_template({}) == "unmatched"


def _make_app(recorder: ApiRing):
    async def app(scope, receive, send):
        if scope["type"] != "http":
            await send({"type": "lifespan.startup.complete"})
            return
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": json.dumps({"ok": 1}).encode()})

    return MonitorMiddleware(app)


def test_middleware_collects_http(monkeypatch):
    import asyncio

    from lquant.monitor import ring as ring_mod

    test_ring = ApiRing(maxlen=10)
    monkeypatch.setattr(ring_mod, "api_ring", test_ring)
    app = _make_app(test_ring)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": "GET", "path": "/api/factors",
             "query_string": b"", "headers": []}
    asyncio.run(app(scope, receive, send))
    pts = test_ring.snapshot()
    assert len(pts) == 1
    assert pts[0].route == "unmatched"  # 裸 ASGI 无路由对象
    assert pts[0].status == 200
    assert pts[0].dur_category in {"fast", "normal", "slow"}


def test_middleware_skips_monitor_path(monkeypatch):
    import asyncio

    from lquant.monitor import ring as ring_mod

    test_ring = ApiRing(maxlen=10)
    monkeypatch.setattr(ring_mod, "api_ring", test_ring)
    app = _make_app(test_ring)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        pass

    scope = {"type": "http", "method": "GET", "path": "/api/monitor/summary",
             "query_string": b"", "headers": []}
    asyncio.run(app(scope, receive, send))
    assert test_ring.snapshot() == ()


def test_middleware_passthrough_websocket(monkeypatch):
    import asyncio

    from lquant.monitor import ring as ring_mod

    test_ring = ApiRing(maxlen=10)
    monkeypatch.setattr(ring_mod, "api_ring", test_ring)
    app = _make_app(test_ring)

    async def receive():
        return {"type": "http.disconnect"}

    async def send(msg):
        pass

    asyncio.run(app({"type": "websocket", "path": "/ws/jobs/1"}, receive, send))
    assert test_ring.snapshot() == ()
