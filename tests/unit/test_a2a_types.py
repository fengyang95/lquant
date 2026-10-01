"""A2A 数据模型：camelCase 线格式、Part 四选一、必需字段、终态集合。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from lquant.agent.a2a.types import (
    TERMINAL_STATES,
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Artifact,
    Message,
    Part,
    Role,
    Task,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)


def test_part_requires_exactly_one_payload():
    with pytest.raises(ValidationError):
        Part()
    with pytest.raises(ValidationError):
        Part(text="a", url="http://x")
    with pytest.raises(ValidationError):
        Part(text="a", data={"k": 1})


def test_empty_text_part_is_valid_and_serializes():
    # lastChunk 收尾帧用的就是空文本 part，必须合法且真的出现在 JSON 里
    assert Part(text="").to_a2a() == {"text": ""}


def test_to_a2a_uses_camel_case_and_drops_none():
    m = Message(messageId="m1", role=Role.USER, parts=[Part(text="hi")], contextId="c1")
    assert m.to_a2a() == {
        "messageId": "m1", "role": "ROLE_USER",
        "parts": [{"text": "hi"}], "contextId": "c1",
    }
    assert "taskId" not in m.to_a2a()


def test_message_required_fields():
    with pytest.raises(ValidationError):
        Message(role=Role.USER, parts=[])  # 缺 messageId
    with pytest.raises(ValidationError):
        Message(messageId="m", parts=[])   # 缺 role


def test_inbound_message_tolerates_extra_fields_and_snake_case():
    m = Message.model_validate({
        "messageId": "m", "role": Role.USER,
        "parts": [{"text": "x", "mediaType": "text/plain"}],
        "extensions": ["urn:x"],
    })
    assert m.parts[0].text == "x"
    assert m.model_extra["extensions"] == ["urn:x"]
    # populate_by_name：内部构造可用 snake_case
    assert Message(message_id="m", role=Role.USER, parts=[Part(text="x")]).message_id == "m"


def test_task_roundtrip_and_required_status():
    t = Task(id="t1", status=TaskStatus(state=TaskState.WORKING), contextId="c1")
    dumped = t.to_a2a()
    assert dumped == {"id": "t1", "status": {"state": "TASK_STATE_WORKING"},
                      "contextId": "c1"}
    assert Task.model_validate(dumped).id == "t1"
    with pytest.raises(ValidationError):
        Task(id="t1")


def test_artifact_and_status_update_aliases():
    a = Artifact(artifactId="a1", parts=[Part(text="x")], name="answer")
    assert a.to_a2a() == {"artifactId": "a1", "parts": [{"text": "x"}], "name": "answer"}
    ev = TaskStatusUpdateEvent(taskId="t1", contextId="c1",
                               status=TaskStatus(state=TaskState.COMPLETED), final=True)
    assert ev.to_a2a()["final"] is True
    assert ev.to_a2a()["taskId"] == "t1"


def test_terminal_states_set():
    assert TaskState.COMPLETED in TERMINAL_STATES
    assert TaskState.CANCELED in TERMINAL_STATES
    assert TaskState.FAILED in TERMINAL_STATES
    assert TaskState.WORKING not in TERMINAL_STATES
    assert TaskState.SUBMITTED not in TERMINAL_STATES


def test_agent_card_requires_core_fields():
    with pytest.raises(ValidationError):
        AgentCard(name="x")
    card = AgentCard(
        name="n", description="d", version="1",
        supportedInterfaces=[AgentInterface(url="http://h/a2a", protocolBinding="JSONRPC",
                                            protocolVersion="1.0")],
        capabilities=AgentCapabilities(streaming=True),
        defaultInputModes=["text/plain"], defaultOutputModes=["text/plain"],
        skills=[AgentSkill(id="s", name="s", description="d", tags=["t"])],
    )
    dumped = card.to_a2a()
    assert dumped["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"
    assert dumped["defaultInputModes"] == ["text/plain"]
    assert dumped["skills"][0]["tags"] == ["t"]
    # 未声明的可选字段不出现（默认 input/output modes 是必填，不做兜底）
    assert "securitySchemes" not in dumped
