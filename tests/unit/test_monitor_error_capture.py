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
    """500 响应已发出后同请求再抛异常：仍只记一条，但必须带上异常详情。

    Starlette 的真实顺序就是「先发 500 → 再抛出」；若在 response.start
    处就地记一笔无详情的错误，未捕获异常的 error_type/message/traceback
    会全部丢失（监控页错误日志只剩一行状态码）。
    """
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
    assert errs[0].status == 500
    assert errs[0].error_type == "RuntimeError"
    assert errs[0].message == "after-500"
    assert errs[0].traceback_tail and "after-500" in errs[0].traceback_tail
    # 同一请求只采一次耗时样本（响应起点已记过，异常路径不得再补一笔）
    assert len([p for p in ring_mod.api_ring.snapshot() if p.status == 500]) == 1


def test_bare_exception_without_response_still_records(monkeypatch):
    """没有 response.start 的裸 ASGI 异常：错误与耗时都要补记。"""
    ring_mod = _fresh_rings(monkeypatch)

    async def app(scope, receive, send):
        raise RuntimeError("bare-boom")

    import contextlib

    with contextlib.suppress(RuntimeError):
        _run(MonitorMiddleware(app), _scope("GET", "/api/x"))
    errs = [p for p in ring_mod.error_ring.snapshot()]
    assert len(errs) == 1 and errs[0].message == "bare-boom"
    assert len([p for p in ring_mod.api_ring.snapshot() if p.status == 500]) == 1


def test_midstream_failure_after_200_records_500(monkeypatch):
    """已开流（200）后中途抛异常：错误日志必须记 500，不能把 2xx 写进错误表。"""
    ring_mod = _fresh_rings(monkeypatch)

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": []})
        raise RuntimeError("mid-stream")

    import contextlib

    with contextlib.suppress(RuntimeError):
        _run(MonitorMiddleware(app), _scope())
    errs = [p for p in ring_mod.error_ring.snapshot()]
    assert len(errs) == 1
    assert errs[0].status == 500
    assert errs[0].message == "mid-stream"


def test_error_ring_is_independent_from_api_ring(monkeypatch):
    ring_mod = _fresh_rings(monkeypatch)
    ring_mod.error_ring.append(_err_pt())
    assert ring_mod.api_ring.snapshot() == ()


def _err_pt():
    from lquant.monitor.types import ApiErrorPoint

    return ApiErrorPoint(ts=1.0, route="r", method="GET", status=500,
                         error_type="T", message="m", traceback_tail=None)
