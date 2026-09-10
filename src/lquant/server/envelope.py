"""统一响应封套：{ code, data, message, trace_id }（设计文档 4.API 契约）。

选型取舍：
- 旧路由（market/data/factors/backtests…）早已返回裸 dict/list，且被大量测试断言。
  全量改造会牵连几十处断言，且无收益 —— 所以封套通过 `EnvRoute` **显式挂载**到新路由
  （etf / settings），新旧接口并存，前端用 `getData()` 对封套接口解包。
- EnvRoute 复写 get_route_handler：成功 return 收进信封；HTTPException 转成同构信封并
  **保留 status_code**（code=1）。前端按 status 分支，body 走信封，两侧契约一致。
- 已带 code 的 payload 不二次包，避免嵌套信封。

注意：封套只改 JSON 响应形状，不改 HTTP 语义 —— 404/422 仍返回对应状态码。
"""
from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.requests import Request
from starlette.responses import Response

CODE_OK = 0
CODE_ERR = 1

_LOG = logging.getLogger(__name__)


def _first_validation_error(e: RequestValidationError) -> str:
    """从 pydantic 校验错误里抽一条人话（不给客户端回完整异常对象）。"""
    errs = getattr(e, "errors", lambda: [])()
    if errs:
        loc = next((str(x) for x in errs[0].get("loc", ()) if x != "body"), None)
        msg = errs[0].get("msg") or "参数校验失败"
        return f"{loc}: {msg}" if loc else msg
    return "参数校验失败"


def new_trace_id() -> str:
    return uuid.uuid4().hex[:12]


def ok(data, message: str = "ok", *, code: int = CODE_OK, trace_id: str | None = None) -> dict:
    return {"code": code, "data": data, "message": message,
            "trace_id": trace_id or new_trace_id()}


def err(message: str, *, code: int = CODE_ERR, data=None, trace_id: str | None = None) -> dict:
    return {"code": code, "data": data, "message": message,
            "trace_id": trace_id or new_trace_id()}


def make_router(*, prefix: str = "", tags: list[str] | None = None) -> APIRouter:
    """建一个自带封套的 APIRouter（route_class=EnvRoute）。"""
    return APIRouter(prefix=prefix, tags=tags, route_class=EnvRoute)


class EnvRoute(APIRoute):
    """把 handler 的 JSON 响应包进 {code,data,message,trace_id}。

    只处理 dict/list 的成功返回与 HTTPException；StreamingResponse 等原样放行。
    """

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                resp = await original(request)
            except HTTPException as e:
                return _enveloped_error(e.status_code, e.detail)
            except RequestValidationError as e:
                # 422 保留状态码，body 走信封（不给客户端吐 pydantic 原始错误堆）
                msg = _first_validation_error(e)
                return _enveloped_error(422, msg)
            except Exception:  # noqa: BLE001 - 兜底成信封，避免前端拿到非 JSON 500
                _LOG.exception("enveloped route 未捕获异常 %s %s", request.method,
                               request.url.path)
                return _enveloped_error(500, "服务内部错误，请稍后重试")
            return _wrap(resp)

        return handler


def _wrap(resp: Response) -> Response:
    # 只改 JSON 响应；文件流/重定向/纯文本原样放行。
    # 注意不能只认 JSONResponse：FastAPI 对返回 dict 的路由给的是**基类 Response**，
    # 对返回 None 才是 JSONResponse —— 一律按 content-type 判断。
    if "application/json" not in (resp.headers.get("content-type") or "").lower():
        return resp
    try:
        body = json.loads(resp.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return resp
    if isinstance(body, dict) and "code" in body:
        return resp                      # 已封套，不二次包
    # 原响应带旧 Content-Length（body 变长后已失效）：丢它让 Starlette 重算，
    # 否则 uvicorn 下浏览器读到截断/错帧 body —— 封套后长度必须跟信封一致。
    headers = {k: v for k, v in resp.headers.items() if k.lower() != "content-length"}
    return JSONResponse(ok(body), status_code=resp.status_code, headers=headers)


def _enveloped_error(status_code: int, detail) -> Response:
    msg = detail if isinstance(detail, str) else str(detail)
    return JSONResponse(err(msg), status_code=status_code)