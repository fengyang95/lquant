"""AgentEvent ⇄ A2A 对象映射（纯逻辑，无 IO）。

出站：``AgentEvent`` → ``StreamResponse`` 帧（``{"task"|"statusUpdate"|"artifactUpdate"}``）。
入站：A2A ``Message`` → 一段 prompt 文本。

设计取舍：

- **增量正文走 artifactUpdate(append=True)**，而不是把每次 delta 塞进 status.message ——
  A2A 客户端普遍按 ``append`` 累积产物；status 只承载「状态 + 进度说明」。
- **不把 tool 的原始入参透给调用方**：工具参数可能含本机路径/命令（claude CLI 是
  全自主权限），外发只带工具名与结果摘要。这与 ``message/send`` 的 history
  只回文本 part 是同一条约束（见 docs/SECURITY.md）。
- ``done`` 先补一帧 ``lastChunk`` 再发终态，让累积方能显式收尾；
  一个字都没输出过时不补空产物帧。
"""
from __future__ import annotations

from lquant.agent.a2a.errors import INVALID_PARAMS, A2AError
from lquant.agent.a2a.types import (
    TERMINAL_STATES,
    Artifact,
    Message,
    Part,
    Role,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    agent_message,
    stream_artifact,
    stream_status,
    text_part,
)
from lquant.agent.schemas import AgentEvent

#: 工具结果摘要外发上限（截断，避免把大段内部数据回给调用方）
_TOOL_SUMMARY_LEN = 200


def prompt_from_message(msg: Message) -> str:
    """拼接入站 Message 的 text part。无可用文本 → -32602。"""
    texts = [p.text.strip() for p in msg.parts
             if isinstance(p.text, str) and p.text.strip()]
    if not texts:
        raise A2AError(INVALID_PARAMS, "message.parts 中没有可用的 text 内容")
    return "\n".join(texts)


class FrameBuilder:
    """把一串 ``AgentEvent`` 按顺序翻译成 A2A 帧。

    有状态但**不碰 IO**：累积正文、记住终态，供 executor 落库与断言。
    """

    def __init__(self, task_id: str, context_id: str) -> None:
        self.task_id = task_id
        self.context_id = context_id
        self.artifact_id = f"{task_id}-answer"
        self.text = ""
        self.state = TaskState.SUBMITTED
        self.canceled = False

    # ---- 内部构造器 -------------------------------------------------------
    def _status(self, state: str, *, message: str = "", final: bool = False,
                metadata: dict | None = None) -> dict:
        self.state = state
        status = TaskStatus(
            state=state,
            message=agent_message(message, context_id=self.context_id,
                                  task_id=self.task_id) if message else None,
        )
        return stream_status(TaskStatusUpdateEvent(
            taskId=self.task_id, contextId=self.context_id,
            status=status, final=final, metadata=metadata))

    def _artifact(self, text: str, *, last_chunk: bool) -> dict:
        return stream_artifact(TaskArtifactUpdateEvent(
            taskId=self.task_id, contextId=self.context_id,
            artifact=Artifact(artifactId=self.artifact_id, name="answer",
                              parts=[text_part(text)]),
            append=True, lastChunk=last_chunk))

    # ---- 出站 -------------------------------------------------------------
    def working(self) -> dict:
        """首帧开工状态（executor 在收到第一个事件前先发）。"""
        return self._status(TaskState.WORKING)

    def mark_canceled(self) -> None:
        """标记载荷：用户主动取消（service 会发 type=error 的「已中断」事件）。"""
        self.canceled = True

    def on_event(self, ev: AgentEvent) -> list[dict]:
        if ev.type == "assistant_delta":
            if not ev.text:
                return []
            self.text += ev.text
            return [self._artifact(ev.text, last_chunk=False)]

        if ev.type == "tool_call":
            # 只带工具名：入参可能含本机路径/命令，不外发
            return [self._status(TaskState.WORKING, message=f"正在调用 {ev.name}",
                                 metadata={"tool": ev.name})]

        if ev.type == "tool_result":
            return [self._status(TaskState.WORKING,
                                 metadata={"toolResult": ev.summary[:_TOOL_SUMMARY_LEN]})]

        if ev.type == "done":
            frames = [self._artifact("", last_chunk=True)] if self.text else []
            frames.append(self._status(TaskState.COMPLETED, final=True))
            return frames

        if ev.type == "error":
            frames = [self._artifact("", last_chunk=True)] if self.text else []
            state = TaskState.CANCELED if self.canceled else TaskState.FAILED
            frames.append(self._status(state, message=ev.message or "执行失败", final=True))
            return frames

        return []

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


def ask_message_to_a2a(m, *, context_id: str, task_id: str = "") -> Message:
    """落库的 ``ask_messages`` 行 → A2A Message（只回文本 part，不含 tool_calls）。"""
    role = Role.USER if m.role == "user" else Role.AGENT
    parts: list[Part] = [text_part(m.content)]
    return Message(messageId=m.id, role=role, parts=parts,
                   contextId=context_id, taskId=task_id or None)
