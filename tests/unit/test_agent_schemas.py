"""agent/schemas 单元测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lquant.agent.schemas import AgentEvent, Message, Session


def test_session_defaults():
    s = Session(id="s1")
    assert s.title == "新会话"
    assert s.context == {}
    assert s.created_at == ""


def test_session_full():
    s = Session(id="s2", title="t", context={"k": [1, 2]}, created_at="2026-01-01")
    assert s.context == {"k": [1, 2]}
    dict(s)  # 模型可序列化路径
    s.model_dump()


def test_message_role_validation():
    m = Message(id="m1", session_id="s1", role="user", content="hi")
    assert m.role == "user"
    assert m.tool_calls == []
    with pytest.raises(ValidationError):
        Message(id="m2", session_id="s1", role="robot", content="x")


def test_agent_event_defaults():
    e = AgentEvent(type="assistant_delta")
    assert e.text == ""
    assert e.name == ""
    assert e.args == {}
    assert e.summary == ""
    assert e.message_id == ""
    assert e.message == ""


def test_agent_event_full():
    e = AgentEvent(
        type="tool_call",
        name="query",
        args={"a": 1},
        summary="s",
        message_id="m",
        message="hello",
        text="t",
    )
    assert e.args == {"a": 1}
    e.model_dump()
