"""A2A 1.0 数据模型（够用子集）。

线格式严格对齐规范：字段名 camelCase，枚举值用 proto 派生的大写形式
（``TASK_STATE_*`` / ``ROLE_*``）。

序列化统一走 :meth:`A2ABase.to_a2a` —— ``by_alias`` + ``exclude_none``。
``exclude_none`` 不是洁癖：``Part`` 的规范约束是「text/raw/url/data 四选一」，
未设置的字段必须**不出现在 JSON 里**，否则消费方无法判断用哪个分支。

校验只在**入站**（外部给的 Message）严格；出站对象由本仓构造，
``state`` 之类保持 ``str`` 而不做 Literal 校验 —— 未来规范加状态值时不会炸。
"""
from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaskState:
    """A2A TaskState 枚举值。"""

    UNSPECIFIED = "TASK_STATE_UNSPECIFIED"
    SUBMITTED = "TASK_STATE_SUBMITTED"
    WORKING = "TASK_STATE_WORKING"
    COMPLETED = "TASK_STATE_COMPLETED"
    FAILED = "TASK_STATE_FAILED"
    CANCELED = "TASK_STATE_CANCELED"
    INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
    REJECTED = "TASK_STATE_REJECTED"
    AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"


#: 终态：进入后不可再流转（tasks/cancel 据此回 TaskNotCancelableError）
TERMINAL_STATES = frozenset({
    TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED, TaskState.REJECTED,
})


class Role:
    """A2A Role 枚举值。"""

    USER = "ROLE_USER"
    AGENT = "ROLE_AGENT"


class A2ABase(BaseModel):
    # populate_by_name：允许用 snake_case 关键字构造；extra=allow：容忍规范新增字段
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    def to_a2a(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)


class Part(A2ABase):
    """消息/产物的一段内容。规范要求 text / raw / url / data 四选一。"""

    text: str | None = None
    data: dict[str, Any] | None = None
    url: str | None = None
    raw: str | None = None
    media_type: str | None = Field(default=None, alias="mediaType")
    metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Part:
        present = [k for k in ("text", "data", "url", "raw") if getattr(self, k) is not None]
        if len(present) != 1:
            raise ValueError(f"Part 必须且只能设置 text/raw/url/data 之一，当前={present}")
        return self


class Message(A2ABase):
    message_id: str = Field(alias="messageId")
    role: str
    parts: list[Part]
    context_id: str | None = Field(default=None, alias="contextId")
    task_id: str | None = Field(default=None, alias="taskId")
    metadata: dict[str, Any] | None = None


class Artifact(A2ABase):
    artifact_id: str = Field(alias="artifactId")
    parts: list[Part]
    name: str | None = None
    description: str | None = None
    metadata: dict[str, Any] | None = None


class TaskStatus(A2ABase):
    state: str
    message: Message | None = None
    timestamp: str | None = None


class Task(A2ABase):
    id: str
    status: TaskStatus
    context_id: str | None = Field(default=None, alias="contextId")
    artifacts: list[Artifact] | None = None
    history: list[Message] | None = None
    metadata: dict[str, Any] | None = None


class TaskStatusUpdateEvent(A2ABase):
    task_id: str = Field(alias="taskId")
    context_id: str = Field(alias="contextId")
    status: TaskStatus
    final: bool = False
    metadata: dict[str, Any] | None = None


class TaskArtifactUpdateEvent(A2ABase):
    task_id: str = Field(alias="taskId")
    context_id: str = Field(alias="contextId")
    artifact: Artifact
    append: bool = False
    last_chunk: bool = Field(default=False, alias="lastChunk")
    metadata: dict[str, Any] | None = None


class StreamResponse(A2ABase):
    """流式响应的一帧：``task`` / ``message`` / ``statusUpdate`` / ``artifactUpdate`` 四选一。

    SSE 里投递的是**这个包装对象**，不是裸的 statusUpdate/artifactUpdate
    （规范：``StreamResponse`` MUST contain exactly one of ...）。
    """

    task: Task | None = None
    message: Message | None = None
    status_update: TaskStatusUpdateEvent | None = Field(default=None, alias="statusUpdate")
    artifact_update: TaskArtifactUpdateEvent | None = Field(
        default=None, alias="artifactUpdate")

    @model_validator(mode="after")
    def _exactly_one(self) -> StreamResponse:
        present = [k for k in ("task", "message", "status_update", "artifact_update")
                   if getattr(self, k) is not None]
        if len(present) != 1:
            raise ValueError(
                f"StreamResponse 必须且只能设置 task/message/statusUpdate/artifactUpdate 之一，"
                f"当前={present}")
        return self


def stream_task(task: Task) -> dict[str, Any]:
    return StreamResponse(task=task).to_a2a()


def stream_status(ev: TaskStatusUpdateEvent) -> dict[str, Any]:
    return StreamResponse(status_update=ev).to_a2a()


def stream_artifact(ev: TaskArtifactUpdateEvent) -> dict[str, Any]:
    return StreamResponse(artifact_update=ev).to_a2a()


class AgentProvider(A2ABase):
    organization: str
    url: str | None = None


class AgentCapabilities(A2ABase):
    streaming: bool = False
    push_notifications: bool = Field(default=False, alias="pushNotifications")
    extended_agent_card: bool = Field(default=False, alias="extendedAgentCard")
    extensions: list[str] | None = None


class AgentSkill(A2ABase):
    id: str
    name: str
    description: str
    tags: list[str]


class AgentInterface(A2ABase):
    url: str
    protocol_binding: str = Field(alias="protocolBinding")
    protocol_version: str = Field(alias="protocolVersion")
    tenant: str | None = None


class AgentCard(A2ABase):
    name: str
    description: str
    version: str
    supported_interfaces: list[AgentInterface] = Field(alias="supportedInterfaces")
    capabilities: AgentCapabilities
    default_input_modes: list[str] = Field(alias="defaultInputModes")
    default_output_modes: list[str] = Field(alias="defaultOutputModes")
    skills: list[AgentSkill]
    provider: AgentProvider | None = None
    documentation_url: str | None = Field(default=None, alias="documentationUrl")
    security_schemes: dict[str, Any] | None = Field(default=None, alias="securitySchemes")
    security_requirements: list[dict[str, Any]] | None = Field(
        default=None, alias="securityRequirements")
    icon_url: str | None = Field(default=None, alias="iconUrl")


def text_part(text: str) -> Part:
    return Part(text=text)


def agent_message(text: str, *, context_id: str = "", task_id: str = "",
                  message_id: str = "") -> Message:
    """构造一条 agent 侧 Message（状态说明用）。"""
    return Message(
        messageId=message_id or uuid.uuid4().hex,
        role=Role.AGENT,
        parts=[text_part(text)],
        contextId=context_id or None,
        taskId=task_id or None,
    )
