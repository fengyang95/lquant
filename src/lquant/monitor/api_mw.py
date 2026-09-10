"""纯 ASGI 计时中间件：请求开始 → http.response.start（TTFB 口径）。

不走 BaseHTTPMiddleware（流式响应/后台任务语义问题）。采集失败绝不影响请求。
"""
from __future__ import annotations

import contextlib
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lquant.monitor.types import ApiMetricPoint

_STATIC_SUFFIXES = (".js", ".css", ".svg", ".png", ".ico", ".map")


def _classify(status: int, duration_ms: float) -> str:
    if status >= 400:
        return "error"  # error 优先于耗时分类
    if duration_ms <= 100.0:
        return "fast"
    if duration_ms > 1000.0:
        return "slow"
    return "normal"


def _should_skip(path: str) -> bool:
    if path.startswith("/api/monitor"):  # 自监控端点不采集（自反馈）
        return True
    if path == "/api/health" or path.startswith("/api/health/"):
        return True
    if path.startswith("/_next"):
        return True
    return path.endswith(_STATIC_SUFFIXES)


def _route_template(scope: Scope) -> str:
    route = scope.get("route")  # send 时路由已完成（scope 为同一 dict 引用）
    return getattr(route, "path", None) or "unmatched"


class MonitorMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        from lquant.monitor import ring as ring_mod  # 动态取单例，测试可替换

        if scope["type"] != "http" or _should_skip(scope.get("path", "")):
            await self.app(scope, receive, send)
        else:
            start = time.perf_counter()

            async def send_wrapper(message: Message) -> None:
                if message["type"] == "http.response.start":
                    try:
                        elapsed = (time.perf_counter() - start) * 1000.0
                        status = int(message.get("status", 0))
                        ring_mod.api_ring.append(ApiMetricPoint(
                            ts=time.time(), route=_route_template(scope),
                            method=str(scope.get("method", "")), status=status,
                            duration_ms=elapsed, dur_category=_classify(status, elapsed)))
                    except Exception:  # noqa: BLE001 - 采集失败不影响响应
                        pass
                await send(message)

            try:
                await self.app(scope, receive, send_wrapper)
            except Exception:
                # 异常路径也记一笔（Starlette 会在内层转 500；此处兜住裸 ASGI 直调）
                elapsed = (time.perf_counter() - start) * 1000.0
                with contextlib.suppress(Exception):
                    ring_mod.api_ring.append(ApiMetricPoint(
                        ts=time.time(), route=_route_template(scope),
                        method=str(scope.get("method", "")), status=500,
                        duration_ms=elapsed, dur_category="error"))
                raise
