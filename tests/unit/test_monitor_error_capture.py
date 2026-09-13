"""MonitorMiddleware 错误采集：5xx 响应与裸异常路径记入 error_ring。"""
from __future__ import annotations

import asyncio

from lquant.monitor.api_mw import MonitorMiddleware
from lquant.monitor.ring import ApiRing


def _run(app, scope):
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        pass

    asyncio.run(app(scope, receive, send))


def _scope(method="GET", path="/api/factors"):
    return {"type": "http", "method": method, "path": path,
            "query_string": b"", "headers": []}


def _fresh_rings(monkeypatch):
    """隔离模块级单例：api/error 环缓冲都换成测试实例。"""
    from lquant.monitor import ring as ring_mod

    monkeypatch.setattr(ring_mod, "api_ring", ApiRing(maxlen=10))
    monkeypatch.setattr(ring_mod, "error_ring", ApiRing(maxlen=10))
    return ring_mod


def test_middleware_records_error_on_5xx(monkeypatch):
    ring_mod = _fresh_rings(monkeypatch)

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 500,
                    "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    _run(MonitorMiddleware(app), _scope())
    pts = [p for p in ring_mod.error_ring.snapshot() if p.status == 500]
    assert pts and pts[0].route == "unmatched"


def test_middleware_records_error_on_exception(monkeypatch):
    ring_mod = _fresh_rings(monkeypatch)

    async def app(scope, receive, send):
        raise RuntimeError("boom-evaluate")

    import contextlib

    with contextlib.suppress(RuntimeError):
        _run(MonitorMiddleware(app), _scope("POST", "/api/factors/evaluate"))
    errs = [p for p in ring_mod.error_ring.snapshot() if p.message == "boom-evaluate"]
    assert errs and errs[0].error_type == "RuntimeError"
    assert errs[0].status == 500
    assert errs[0].traceback_tail and "RuntimeError" in errs[0].traceback_tail


def test_error_recorded_once_per_request(monkeypatch):
    """500 响应已记一笔后，同请求再抛异常不得双记。"""
    ring_mod = _fresh_rings(monkeypatch)

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 500,
                    "headers": []})
        raise RuntimeError("after-500")

    import contextlib

    with contextlib.suppress(RuntimeError):
        _run(MonitorMiddleware(app), _scope())
    errs = [p for p in ring_mod.error_ring.snapshot()]
    assert len(errs) == 1


def test_error_ring_is_independent_from_api_ring(monkeypatch):
    ring_mod = _fresh_rings(monkeypatch)
    ring_mod.error_ring.append(_err_pt())
    assert ring_mod.api_ring.snapshot() == ()


def _err_pt():
    from lquant.monitor.types import ApiErrorPoint

    return ApiErrorPoint(ts=1.0, route="r", method="GET", status=500,
                         error_type="T", message="m", traceback_tail=None)
