"""ClaudeCodeAgentService：以子进程驱动无头 claude CLI，stream-json 映射为事件流。"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path

from lquant.agent.claude_json import parse_stream_line
from lquant.agent.errors import AgentError
from lquant.agent.schemas import AgentEvent, Message, Session
from lquant.agent.service import AgentService
from lquant.agent.sessions import SessionStore
from lquant.agent.workspace import ensure_workspace

_LOG = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是 lquant 量化研究平台的 AI 助手。回答 A 股相关问题时："
    "优先使用 lquant MCP 工具查询数据，先查数、结论先行，用中文回答。"
)


class ClaudeCodeAgentService(AgentService):
    """每次 send_message 起一个 ``claude -p`` 子进程，cwd 为工作区。

    续聊依赖 claude CLI 的 ``--resume``：首轮 result 事件的 session_id 落库，
    后续轮次带上继续同一 CLI 会话。
    """

    def __init__(
        self,
        store: SessionStore,
        claude_path: str | None = None,
        claude_args: list[str] | None = None,
        workspace_dir: str | None = None,
        root: Path | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        from lquant.core.config import get_settings  # noqa: PLC0415

        s = get_settings()
        self._claude_path = claude_path or s.agent.claude_path
        self._claude_args = claude_args or []
        self._workspace_dir = workspace_dir or s.agent.workspace_dir
        self._root = Path(root) if root else s.root
        self._timeout = float(timeout_seconds if timeout_seconds is not None
                              else s.agent.timeout_seconds)
        super().__init__(store)
        self._workspace = ensure_workspace(self._workspace_dir, self._root)
        self._tasks: dict[str, asyncio.Task] = {}
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    async def create_session(self, context: dict | None) -> Session:
        return await self.store.create(context)

    async def list_sessions(self) -> list[Session]:
        return await self.store.list()

    async def delete_session(self, sid: str) -> None:
        await self.cancel(sid)
        await self.store.delete(sid)

    async def get_messages(self, sid: str) -> list[Message]:
        return await self.store.messages(sid)

    async def send_message(self, sid: str, content: str, on_event) -> Message:
        if not content.strip():
            raise AgentError("消息不能为空", status_code=400)
        ses = await self.store.get(sid)
        if ses is None:
            raise AgentError("会话不存在", status_code=404)
        user_msg = await self.store.add_message(sid, "user", content)
        self._tasks[sid] = asyncio.current_task()
        try:
            await self._run(sid, content, on_event)
        except asyncio.CancelledError:
            await on_event(AgentEvent(type="error", message="已中断"))
            raise
        except Exception as e:  # noqa: BLE001
            _LOG.exception("claude_code agent 失败")
            err = await self.store.add_message(sid, "assistant", f"出错了：{e}")
            await on_event(AgentEvent(type="error", message=str(e)))
            return err
        return user_msg

    async def cancel(self, sid: str) -> None:
        t = self._tasks.pop(sid, None)
        if t and not t.done():
            t.cancel()
        proc = self._procs.pop(sid, None)
        if proc and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()

    async def _run(self, sid: str, content: str, on_event) -> None:
        claude_sid = await self.store.get_claude_session_id(sid)
        cmd = [
            self._claude_path,
            "-p", content,
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--append-system-prompt", _SYSTEM_PROMPT,
            "--mcp-config", str(self._workspace / ".claude" / "mcp.json"),
        ]
        if claude_sid:
            cmd += ["--resume", claude_sid]
        cmd += self._claude_args
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self._workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, NotADirectoryError, PermissionError) as e:
            raise AgentError(f"无法启动 claude CLI：{e}") from e
        self._procs[sid] = proc
        await self._consume(sid, proc, on_event)

    async def _consume(self, sid: str, proc, on_event) -> None:
        stderr_lines: list[str] = []

        async def pump_stderr() -> None:
            async for raw in proc.stderr:
                line = raw.decode(errors="replace").rstrip()
                stderr_lines.append(line)
                _LOG.warning("[claude stderr] %s", line)

        stderr_task = asyncio.create_task(pump_stderr())
        ans_msg = await self.store.add_message(sid, "assistant", "")
        started = time.monotonic()
        saw_done = False
        try:
            while True:
                if time.monotonic() - started > self._timeout:
                    proc.terminate()
                    tail = "\n".join(stderr_lines[-5:])[-400:]
                    raise AgentError(f"执行超时；stderr 梗概：{tail}")
                try:
                    raw = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=1.0)
                except TimeoutError:
                    continue
                if not raw:
                    break
                for ev in parse_stream_line(raw.decode(errors="replace")):
                    kind = ev["kind"]
                    if kind == "delta":
                        await self.store.append_assistant_delta(
                            sid, ans_msg.id, ev["text"])
                        await on_event(AgentEvent(
                            type="assistant_delta", text=ev["text"],
                            message_id=ans_msg.id))
                    elif kind == "tool_call":
                        await on_event(AgentEvent(
                            type="tool_call", name=ev["name"], args=ev["args"]))
                    elif kind == "tool_result":
                        await on_event(AgentEvent(
                            type="tool_result", name=ev.get("name", ""),
                            summary=ev["summary"]))
                    elif kind == "done":
                        saw_done = True
                        if ev.get("session_id"):
                            await self.store.set_claude_session_id(
                                sid, ev["session_id"])
                        await self.store.finish_assistant(sid, ans_msg.id)
                        await on_event(AgentEvent(
                            type="done", message_id=ans_msg.id))
                    elif kind == "error":
                        await self.store.add_message(sid, "assistant", ev["text"])
                        await on_event(AgentEvent(type="error", message=ev["text"]))
        finally:
            if proc.returncode is None:
                proc.terminate()
            stderr_task.cancel()
            self._procs.pop(sid, None)
        if not saw_done:
            tail = "\n".join(stderr_lines[-5:])[-400:]
            raise AgentError(f"claude CLI 未正常收尾；stderr 梗概：{tail}")
