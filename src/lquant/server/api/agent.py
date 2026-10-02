"""Agent 能力配置：可用能力清单 + skill 文件读写。

「问 AI」页据此渲染新建会话弹层（provider / skill / MCP 工具）与设置页的
skill 编辑器。校验口径与落盘位置全部收在 :mod:`lquant.agent.capabilities`
（与 A2A Agent Card 同源），这里只做 HTTP 语义映射。
"""
from __future__ import annotations

from fastapi import HTTPException

from lquant.agent import capabilities
from lquant.agent.service import default_agent_config
from lquant.core.config import get_settings
from lquant.server.envelope import make_router

router = make_router(prefix="/agent", tags=["agent"])


def _root():
    return get_settings().root


@router.get("/capabilities")
async def get_capabilities():
    """可用能力清单 + 全局默认（新建会话弹层据此预填）。"""
    return {
        "providers": capabilities.available_providers(),
        "skills": capabilities.list_skills(_root()),
        "mcp_tools": capabilities.list_mcp_tools(),
        "defaults": default_agent_config(),
    }


@router.get("/skills/{name}")
async def read_skill(name: str):
    try:
        content = capabilities.read_skill(_root(), name)
    except capabilities.CapabilityError as e:
        raise HTTPException(400, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    return {"name": name, "content": content}


@router.put("/skills/{name}")
async def write_skill(name: str, body: dict):
    """新建或覆盖一个 skill（整份 SKILL.md 原文）。"""
    content = str((body or {}).get("content") or "")
    try:
        capabilities.write_skill(_root(), name, content)
    except capabilities.CapabilityError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}


@router.delete("/skills/{name}")
async def delete_skill(name: str):
    try:
        capabilities.delete_skill(_root(), name)
    except capabilities.CapabilityError as e:
        raise HTTPException(400, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    return {"ok": True}
