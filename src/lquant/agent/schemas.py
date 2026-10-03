"""问 AI 数据模型。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Session(BaseModel):
    id: str
    title: str = "新会话"
    context: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    #: 会话级能力配置 ``{provider, skills, mcp_tools}``。``provider`` 创建时锁定；
    #: ``skills`` / ``mcp_tools`` 建后仍可改（``SessionStore.set_agent_config``，
    #: 每轮重建工作区所以下一轮生效）。空 dict 表示「未指定」，由调用方回退到
    #: 全局默认（老会话 / A2A 建的会话）。
    agent_config: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    id: str
    session_id: str
    role: Literal["user", "assistant", "system"]
    content: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = ""


class AgentEvent(BaseModel):
    """流式事件。

    ``type`` ∈ ``assistant_delta|thinking|tool_call|tool_result|system|done|error``。

    ``thinking`` / ``tool_call`` / ``tool_result`` / ``system`` 为**过程数据**：
    本机消费方（``/ask`` 页面）拿到的是原样事件，跨进程的 A2A 出站会先过脱敏
    （``agent/redact.py``）。
    """

    type: str
    text: str = ""
    name: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    message_id: str = ""
    message: str = ""
    #: 结构化载荷（如 system 行的白名单字段）；``args`` 语义已被工具入参占用
    data: dict[str, Any] = Field(default_factory=dict)
