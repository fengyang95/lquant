"""纯 ASGI 计时中间件：请求开始 → http.response.start（TTFB 口径）。

不走 BaseHTTPMiddleware（流式响应/后台任务语义问题）。采集失败绝不影响请求。
"""
from __future__ import annotations

import contextlib
import time
import traceback

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lquant.monitor.types import ApiErrorPoint, ApiMetricPoint

_STATIC_SUFFIXES = (".js", ".css", ".svg", ".png", ".ico", ".map")

_TRACEBACK_TAIL_CHARS = 2000  # 堆栈尾段截断长度，控内存/表体积


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


def _monitor_enabled() -> bool:
    """读缓存 settings 的开关（get_settings 有 lru_cache，仅首次解析 yaml/env）。"""
    try:
        from lquant.core.config import get_settings  # dynamic import avoids cycle

        return get_settings().monitor_enabled
    except Exception:  # noqa: BLE001 - 配置读取失败不阻断请求
        return True


def _record_error(scope: Scope, status: int, exc: BaseException | None) -> None:
    """把 5xx 响应 / 裸异常记入错误环缓冲；采集失败绝不影响请求。"""
    try:
        from lquant.monitor import ring as ring_mod

        err_type = type(exc).__name__ if exc is not None else None
        msg = str(exc)[:500] if exc is not None else None
        tb = "".join(traceback.format_exception(exc)[-3:])[-_TRACEBACK_TAIL_CHARS:] \
            if exc is not None else None
        ring_mod.error_ring.append(ApiErrorPoint(
            ts=time.time(), route=_route_template(scope),
            method=str(scope.get("method", "")), status=status,
            error_type=err_type, message=msg, traceback_tail=tb))
    except Exception:  # noqa: BLE001 - 采集失败不影响响应
        pass


class MonitorMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        from lquant.monitor import ring as ring_mod  # 动态取单例，测试可替换

        if scope["type"] != "http" or _should_skip(scope.get("path", "")) \
                or not _monitor_enabled():
            await self.app(scope, receive, send)
        else:
            start = time.perf_counter()
            # 已发出的响应状态（response.start 只来一次）。响应起点拿不到异常，
            # 而 Starlette 处理未捕获异常的顺序是「先发 500 → 再抛出」——
            # 若在 response.start 处就地记账，错误日志只会留下一行状态码，
            # error_type/message/traceback 全空（监控页上显示为「— / —」）。
            # 因此 5xx 的记账延后到 app 返回/抛出之后再做一次。
            started: dict[str, int] = {}

            async def send_wrapper(message: Message) -> None:
                if message["type"] == "http.response.start":
                    try:
                        elapsed = (time.perf_counter() - start) * 1000.0
                        status = int(message.get("status", 0))
                        started["status"] = status
                        ring_mod.api_ring.append(ApiMetricPoint(
                            ts=time.time(), route=_route_template(scope),
                            method=str(scope.get("method", "")), status=status,
                            duration_ms=elapsed, dur_category=_classify(status, elapsed)))
                    except Exception:  # noqa: BLE001 - 采集失败不影响响应
                        pass
                await send(message)

            try:
                await self.app(scope, receive, send_wrapper)
            except Exception as exc:
                # 异常路径：详情只有这里拿得到（含内层已发的 500）。
                # 响应起点已记过采样就不重复补 —— 否则同一请求在耗时曲线上被记两次。
                if "status" not in started:
                    elapsed = (time.perf_counter() - start) * 1000.0
                    with contextlib.suppress(Exception):
                        ring_mod.api_ring.append(ApiMetricPoint(
                            ts=time.time(), route=_route_template(scope),
                            method=str(scope.get("method", "")), status=500,
                            duration_ms=elapsed, dur_category="error"))
                # 已开流的响应可能先报 200 再中途炸：对错误日志而言仍是失败请求，
                # 不能把 2xx/3xx 记进错误表。
                status = max(int(started.get("status") or 500), 500)
                _record_error(scope, status, exc)
                raise
            else:
                # 正常返回：只有「处理器直接返回 5xx」才需要记一笔（无异常详情）。
                status = int(started.get("status") or 0)
                if status >= 500:
                    _record_error(scope, status, None)
