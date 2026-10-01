"""A2A 端点：``GET /.well-known/agent-card.json`` + ``POST /a2a``。

**不吃 /api 前缀**：Agent Card 按 RFC 8615 挂在规范位，A2A 客户端直接来取。
``main.py`` 那圈 ``prefix="/api"`` 的循环不能套这个 router —— 这是最容易接错的一处，
所以本模块单独 include（见 ``server/main.py``）。

安全：``LQ_A2A_TOKEN`` 未设置时按本机单人使用，启动会打 warning；
设置后 ``POST /a2a`` 必须带 ``Authorization: Bearer <token>``。
注意 A2A 端点等于「可执行本机代码」的入口：CLI 执行体（``agent.provider``，
claude_code / codex 都会带全自主旗标），细则见 docs/SECURITY.md。
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from lquant.agent.a2a import rpc
from lquant.agent.a2a.card import (
    PROTOCOL_VERSION,
    build_agent_card,
    default_description,
    load_skills,
)
from lquant.agent.a2a.executor import get_a2a_executor
from lquant.agent.schemas import AgentEvent
from lquant.core.config import api_base_url, get_settings
from lquant.server.api.ask import get_event_bus

_LOG = logging.getLogger(__name__)

router = APIRouter(tags=["a2a"])

_JSON_HEADERS = {"A2A-Version": PROTOCOL_VERSION}
_SSE_HEADERS = {
    "A2A-Version": PROTOCOL_VERSION,
    "Cache-Control": "no-store",
    # 让 nginx 之类的反代不要缓冲，否则 SSE 会被攒成一坨
    "X-Accel-Buffering": "no",
}


def a2a_token() -> str:
    return os.getenv("LQ_A2A_TOKEN", "").strip()


def skills_dir() -> Path:
    return get_settings().config_dir / "skills"


_warned_unauthenticated = False


def warn_if_unauthenticated() -> None:
    """启动告警：A2A 端点等于「可执行本机代码」的入口，无 token 时要说清楚。"""
    global _warned_unauthenticated
    if _warned_unauthenticated or a2a_token():
        return
    _warned_unauthenticated = True
    _LOG.warning(
        "A2A 端点已开放且未配置 LQ_A2A_TOKEN：能访问 %s/a2a 的调用方，"
        "就能通过内置执行体在本机执行代码（provider=%s）。"
        "共享网络/生产环境请设置 LQ_A2A_TOKEN 并保持只绑回环（LQ_API_HOST 默认 127.0.0.1）。",
        api_base_url(), get_settings().agent.provider)


async def _publish(sid: str, event: AgentEvent) -> None:
    """旁路到 WS 事件总线：外部 Agent 提问时，/ask 页面也能实时看到。"""
    await get_event_bus().publish(sid, event)


@router.get("/.well-known/agent-card.json")
async def agent_card() -> JSONResponse:
    """Agent Card（技能列表由 config/skills/*/SKILL.md 自动派生）。"""
    card = build_agent_card(
        base_url=api_base_url(),
        skills=load_skills(skills_dir()),
        description=default_description(get_settings().agent.provider),
        with_bearer_auth=bool(a2a_token()),
    )
    return JSONResponse(
        card.to_a2a(),
        headers={**_JSON_HEADERS, "Cache-Control": "public, max-age=60"},
    )


@router.post("/a2a", response_model=None)
async def a2a_jsonrpc(request: Request) -> JSONResponse | StreamingResponse:
    token = a2a_token()
    if token and request.headers.get("authorization", "") != f"Bearer {token}":
        return JSONResponse(
            {"error": "unauthorized", "detail": "缺少或错误的 Bearer token"},
            status_code=401, headers={"WWW-Authenticate": "Bearer", **_JSON_HEADERS})

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - 非法 JSON 按 -32700 语义回，而不是 500
        return JSONResponse(rpc.parse_error_body(), headers=_JSON_HEADERS)

    executor = await get_a2a_executor(_publish)
    outcome = await rpc.dispatch(payload, executor)
    if outcome.stream is None:
        return JSONResponse(outcome.body, headers=_JSON_HEADERS)
    return StreamingResponse(_sse(outcome.stream), media_type="text/event-stream",
                             headers=_SSE_HEADERS)


async def _sse(frames: AsyncIterator[dict]) -> AsyncIterator[bytes]:
    async for frame in frames:
        payload = json.dumps(frame, ensure_ascii=False, default=str)
        yield f"data: {payload}\n\n".encode()
