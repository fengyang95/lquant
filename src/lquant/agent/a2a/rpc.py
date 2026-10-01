"""JSON-RPC 2.0 分发：方法名映射 + 参数校验 + 错误码。

方法名两套都收（见 ``METHOD_ALIASES``）：规范 §3.5.1 要求
``{category}/{action}``（``message/send``），但多个官方 SDK 实际发的是
proto 派生的 PascalCase 名（``SendMessage``）。漏一边就接不上，两边都收成本几乎为零。

流式响应的**每一帧**都是完整的 JSON-RPC 响应信封
（``{"jsonrpc":"2.0","id":<请求id>,"result":<StreamResponse>}``），
包装在这里做，传输层只管把 dict 写成 SSE ``data:`` 行。
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from lquant.agent.a2a.errors import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    A2AError,
)
from lquant.agent.a2a.executor import A2AExecutor
from lquant.agent.a2a.types import Message as A2AMessage

_LOG = logging.getLogger(__name__)

JSONRPC_VERSION = "2.0"

#: 方法别名 → 规范内名
METHOD_ALIASES: dict[str, str] = {
    "message/send": "message/send",
    "SendMessage": "message/send",
    "message/stream": "message/stream",
    "SendStreamingMessage": "message/stream",
    "tasks/get": "tasks/get",
    "GetTask": "tasks/get",
    "tasks/cancel": "tasks/cancel",
    "CancelTask": "tasks/cancel",
}


@dataclass
class RpcOutcome:
    """一次分发的产物：普通 JSON 响应 **或** 一串待投递的 JSON-RPC 帧。"""

    body: dict | None = None
    stream: AsyncIterator[dict] | None = None


def error_body(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": JSONRPC_VERSION, "id": rid,
            "error": A2AError(code, message).to_error()}


def result_body(rid: Any, result: Any) -> dict:
    return {"jsonrpc": JSONRPC_VERSION, "id": rid, "result": result}


def parse_error_body() -> dict:
    return error_body(None, PARSE_ERROR, "请求体不是合法 JSON")


def _require_str(params: dict, key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise A2AError(INVALID_PARAMS, f"缺少必填参数 {key}")
    return value.strip()


def _message_param(params: dict) -> A2AMessage:
    raw = params.get("message")
    if not isinstance(raw, dict):
        raise A2AError(INVALID_PARAMS, "缺少必填参数 message")
    try:
        return A2AMessage.model_validate(raw)
    except ValidationError as e:
        raise A2AError(INVALID_PARAMS, f"message 非法: {e.error_count()} 处校验错误") from e


def _context_param(params: dict) -> str | None:
    value = params.get("contextId")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _task_id_param(params: dict) -> str:
    for key in ("id", "taskId"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise A2AError(INVALID_PARAMS, "缺少必填参数 id")


async def _wrap_stream(rid: Any, frames: AsyncIterator[dict]) -> AsyncIterator[dict]:
    async for frame in frames:
        yield result_body(rid, frame)


async def dispatch(payload: Any, executor: A2AExecutor) -> RpcOutcome:
    """分发单个 JSON-RPC 请求。**任何**异常都收敛成 RpcOutcome，不往上抛。"""
    if isinstance(payload, list):
        # 规范允许批量请求，但流式方法不能进批量；统一拒绝以保证行为可预期
        return RpcOutcome(body=error_body(None, INVALID_REQUEST, "不支持 JSON-RPC 批量请求"))
    if not isinstance(payload, dict):
        return RpcOutcome(body=error_body(None, INVALID_REQUEST, "请求体必须是 JSON 对象"))

    rid = payload.get("id")
    if payload.get("jsonrpc") != JSONRPC_VERSION:
        return RpcOutcome(body=error_body(rid, INVALID_REQUEST, 'jsonrpc 必须是 "2.0"'))
    method = payload.get("method")
    if not isinstance(method, str):
        return RpcOutcome(body=error_body(rid, INVALID_REQUEST, "缺少 method"))
    canonical = METHOD_ALIASES.get(method)
    if canonical is None:
        return RpcOutcome(body=error_body(rid, METHOD_NOT_FOUND, f"未实现的方法: {method}"))
    params = payload.get("params")
    params = {} if params is None else params
    if not isinstance(params, dict):
        return RpcOutcome(body=error_body(rid, INVALID_PARAMS, "params 必须是对象"))

    try:
        if canonical == "message/send":
            task = await executor.send(_message_param(params), _context_param(params))
            return RpcOutcome(body=result_body(rid, task.to_a2a()))
        if canonical == "message/stream":
            frames = await executor.open_stream(_message_param(params), _context_param(params))
            return RpcOutcome(stream=_wrap_stream(rid, frames))
        if canonical == "tasks/get":
            raw_hist = params.get("historyLength")
            task = await executor.get(
                _task_id_param(params),
                history_length=raw_hist if isinstance(raw_hist, int) else None)
            return RpcOutcome(body=result_body(rid, task.to_a2a()))
        task = await executor.cancel(_task_id_param(params))
        return RpcOutcome(body=result_body(rid, task.to_a2a()))
    except A2AError as e:
        return RpcOutcome(body=error_body(rid, e.code, e.message))
    except Exception as e:  # noqa: BLE001 - 兜底：任何内部异常都回 JSON-RPC 而不是 500
        _LOG.exception("A2A JSON-RPC 处理失败 method=%s", method)
        return RpcOutcome(body=error_body(rid, INTERNAL_ERROR, f"内部错误: {e}"))
