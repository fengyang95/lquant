"""问 AI 数据模型。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Session(BaseModel):
    id: str
    title: str = "新会话"
    context: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""


class Message(BaseModel):
    id: str
    session_id: str
    role: Literal["user", "assistant", "system"]
    content: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = ""


class AgentEvent(BaseModel):
    """流式事件；type ∈ assistant_delta|tool_call|tool_result|done|error。"""
    type: str
    text: str = ""
    name: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    message_id: str = ""
    message: str = ""
