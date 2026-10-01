"""AgentEvent ⇄ A2A 对象映射（纯逻辑，无 IO）。

出站：``AgentEvent`` → ``StreamResponse`` 帧（``{"task"|"statusUpdate"|"artifactUpdate"}``）。
入站：A2A ``Message`` → 一段 prompt 文本。

两条产物通道（都走 ``artifactUpdate`` + ``append``）：

- ``{task_id}-answer``：回答正文（由 ``assistant_delta`` 累积）。
- ``{task_id}-trace``：**过程数据** —— ``thinking`` / ``tool_call``（含入参）/
  ``tool_result``（全文）/ ``system``；每帧 ``metadata.kind`` 标明类型，
  ``metadata.name`` 标出所属工具。只想要答案的消费方认 ``-answer`` 即可。

设计取舍：

- **增量正文走 artifactUpdate(append=True)**，而不是把每次 delta 塞进 status.message ——
  A2A 客户端普遍按 ``append`` 累积产物；status 只承载「状态 + 进度说明」。
- **过程数据一律先过脱敏**（``agent/redact.py``）：工具入参可能含本机路径/令牌，
  而 A2A 调用方是外部进程。脱敏放在**出站**做，内部事件保持保真 ——
  ``/ask`` 页面等本机消费方不受影响。
- ``statusUpdate`` 仍只带工具名与 200 字结果摘要，属兼容层：不需要过程数据的
  消费方无需改动；要全量的走 ``-trace``。
- ``done``/``error`` 先给开过的产物补 ``lastChunk`` 再发终态，让累积方能显式收尾；
  一个字都没输出过（也没过程数据）时不补空帧。
"""
from __future__ import annotations

import json

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
from lquant.agent.redact import redact, redact_text
from lquant.agent.schemas import AgentEvent

#: statusUpdate 里工具结果摘要的上限（兼容层用；全文走 trace artifact）
_TOOL_SUMMARY_LEN = 200

#: trace 单帧文本上限：工具结果可能是 MB 级，超限截断并在 metadata 标 truncated
_MAX_TRACE_TEXT = 65536

#: trace 收尾帧的 kind
_TRACE_END = "trace_end"


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
        self.trace_id = f"{task_id}-trace"
        self.text = ""
        self.state = TaskState.SUBMITTED
        self.canceled = False
        #: 是否已发过过程数据帧（决定收尾要不要补 trace 的 lastChunk）
        self.trace_emitted = False

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

    def _trace(self, text: str, *, kind: str, name: str = "",
               last_chunk: bool = False) -> dict:
        """过程数据帧：挂独立 artifact，别和 answer 混在一起。"""
        meta: dict = {"kind": kind}
        if name:
            meta["name"] = name
        if len(text) > _MAX_TRACE_TEXT:
            text = text[:_MAX_TRACE_TEXT]
            meta["truncated"] = True
        if kind != _TRACE_END:
            self.trace_emitted = True
        return stream_artifact(TaskArtifactUpdateEvent(
            taskId=self.task_id, contextId=self.context_id,
            artifact=Artifact(artifactId=self.trace_id, name="trace",
                              parts=[text_part(text)]),
            append=True, lastChunk=last_chunk, metadata=meta))

    def _tail_frames(self) -> list[dict]:
        """收尾补的 ``lastChunk`` 空帧：answer 与 trace 各自收口。"""
        frames: list[dict] = []
        if self.trace_emitted:
            frames.append(self._trace("", kind=_TRACE_END, last_chunk=True))
        if self.text:
            frames.append(self._artifact("", last_chunk=True))
        return frames

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

        if ev.type == "thinking":
            return [self._trace(redact_text(ev.text), kind="thinking")]

        if ev.type == "system":
            return [self._trace(json.dumps(redact(ev.data), ensure_ascii=False),
                                kind="system")]

        if ev.type == "tool_call":
            # status 帧只带工具名（兼容层）；入参走 trace 且已脱敏
            return [
                self._status(TaskState.WORKING, message=f"正在调用 {ev.name}",
                             metadata={"tool": ev.name}),
                self._trace(json.dumps(redact(ev.args), ensure_ascii=False),
                            kind="tool_call", name=ev.name),
            ]

        if ev.type == "tool_result":
            return [
                self._status(TaskState.WORKING,
                             metadata={"toolResult": ev.summary[:_TOOL_SUMMARY_LEN]}),
                self._trace(redact_text(ev.text or ev.summary),
                            kind="tool_result", name=ev.name),
            ]

        if ev.type == "done":
            frames = self._tail_frames()
            frames.append(self._status(TaskState.COMPLETED, final=True))
            return frames

        if ev.type == "error":
            frames = self._tail_frames()
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
