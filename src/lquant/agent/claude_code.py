"""ClaudeCodeAgentService：以子进程驱动无头 claude CLI，stream-json 映射为事件流。"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
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


class _Child:
    """posix_spawn 启动的 claude 子进程，适配成 asyncio 子进程鸭子接口。

    为什么不用 asyncio.create_subprocess_exec：它走 fork+exec。进程内
    polars/pandas 已把 OpenBLAS 线程池拉起后，fork 的 pthread_atfork
    prepare（blas_thread_shutdown_）会去 join 正在等活的 BLAS 线程 ——
    竞态死锁且持有 GIL，整个 uvicorn 事件循环冻结（线上堆栈实锤：
    fork → blas_thread_shutdown_ → _pthread_join，主线程 take_gil）。
    posix_spawn 不经过 fork/atfork，整类问题根除。

    实现要点：posix_spawn 不支持 chdir，用 /bin/sh -c 'cd && exec' 包一层，
    exec 后 shell 被 claude 替换（同 pid，terminate/wait 语义不变）。
    stdout/stderr 用 os.pipe + file_actions DUP2 接到父进程，
    再 connect_read_pipe 桥成 StreamReader 供异步逐行读。
    """

    def __init__(self, argv: list[str], cwd: str) -> None:
        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()
        try:
            self._pid = os.posix_spawn(
                "/bin/sh",
                ["/bin/sh", "-c", 'cd "$1" && shift && exec "$@"', "sh", cwd, *argv],
                dict(os.environ),
                file_actions=[
                    (os.POSIX_SPAWN_DUP2, out_w, 1),
                    (os.POSIX_SPAWN_DUP2, err_w, 2),
                ],
            )
        except BaseException:
            for fd in (out_r, out_w, err_r, err_w):
                with contextlib.suppress(OSError):
                    os.close(fd)
            raise
        # 子进程已拿到 dup2 副本，父进程侧写端立即关，否则 EOF 不来
        os.close(out_w)
        os.close(err_w)
        self._out_r, self._err_r = out_r, err_r
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self._waited: int | None = None  # waitpid 已回收后的退出码

    async def attach(self) -> None:
        """把两个读端桥进事件循环（必须在事件循环线程内调用）。"""
        loop = asyncio.get_running_loop()
        for reader, fd in ((self.stdout, self._out_r),
                           (self.stderr, self._err_r)):
            protocol = asyncio.StreamReaderProtocol(reader)
            await loop.connect_read_pipe(
                lambda protocol=protocol: protocol,
                os.fdopen(fd, "rb", buffering=0))

    @property
    def returncode(self) -> int | None:
        if self._waited is not None:
            return self._waited
        try:
            pid, status = os.waitpid(self._pid, os.WNOHANG)
        except ChildProcessError:  # 已被别处回收
            return self._waited
        if pid == self._pid:
            self._waited = os.waitstatus_to_exitcode(status)
        return self._waited

    def terminate(self) -> None:
        os.kill(self._pid, signal.SIGTERM)

    async def wait(self) -> int:
        try:
            code = await asyncio.to_thread(os.waitpid, self._pid, 0)
        except ChildProcessError:
            # returncode 属性的 WNOHANG 轮询可能已抢先回收
            return self._waited if self._waited is not None else 0
        self._waited = os.waitstatus_to_exitcode(code[1])
        return self._waited


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
        skip_permissions: bool | None = None,
    ) -> None:
        from lquant.core.config import get_settings  # noqa: PLC0415

        s = get_settings()
        self._claude_path = claude_path or s.agent.claude_path
        self._claude_args = claude_args or []
        self._workspace_dir = workspace_dir or s.agent.workspace_dir
        self._root = Path(root) if root else s.root
        self._timeout = float(timeout_seconds if timeout_seconds is not None
                              else s.agent.timeout_seconds)
        # 无头 claude 需要跳过交互式授权，否则会挂住；但它是「全自主」权限。
        # 做成开关（默认保持原行为），不要散在命令行里硬编码。
        self._skip_permissions = (
            s.agent.skip_permissions if skip_permissions is None else skip_permissions)
        super().__init__(store)
        self._workspace = ensure_workspace(self._workspace_dir, self._root)
        self._tasks: dict[str, asyncio.Task] = {}
        self._procs: dict[str, _Child] = {}

    async def create_session(self, context: dict | None) -> Session:
        return await self.store.create(context)

    async def list_sessions(self) -> list[Session]:
        return await self.store.list()

    async def delete_session(self, sid: str) -> None:
        await self.cancel(sid)
        await self.store.delete(sid)

    async def get_messages(self, sid: str) -> list[Message]:
        return await self.store.messages(sid)

    async def send_message(self, sid: str, content: str, on_event,
                           user_msg: Message | None = None) -> Message:
        if not content.strip():
            raise AgentError("消息不能为空", status_code=400)
        ses = await self.store.get(sid)
        if ses is None:
            raise AgentError("会话不存在", status_code=404)
        # API 路径已同步落库并传入，避免二次写入（也避免轮询等待）
        user_msg = user_msg or await self.persist_user_message(sid, content)
        self._tasks[sid] = asyncio.current_task()
        try:
            await self._run(sid, content, on_event)
        except asyncio.CancelledError:
            await on_event(AgentEvent(type="error", message="已中断"))
            raise
        except Exception as e:  # noqa: BLE001
            _LOG.exception("claude_code agent 失败")
            if not getattr(e, "recorded", False):
                # _fail_with 已把错误写入增量消息；此处兜底其他异常
                await self.store.add_message(sid, "assistant", f"出错了：{e}")
            await on_event(AgentEvent(type="error", message=str(e)))
            return user_msg
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
            "--append-system-prompt", _SYSTEM_PROMPT,
            "--mcp-config", str(self._workspace / ".claude" / "mcp.json"),
        ]
        if self._skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if claude_sid:
            cmd += ["--resume", claude_sid]
        cmd += self._claude_args
        try:
            proc = _Child(cmd, cwd=str(self._workspace))
            await proc.attach()
        except OSError as e:
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
        error_text = ""  # kind=error 时记录，收尾跳过二次 fail
        try:
            while True:
                if time.monotonic() - started > self._timeout:
                    with contextlib.suppress(ProcessLookupError):
                        proc.terminate()
                    tail = "\n".join(stderr_lines[-5:])[-400:]
                    await self._fail_with(sid, ans_msg.id,
                                          f"执行超时；stderr 梗概：{tail}")
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
                        error_text = ev["text"]
                        # 与 done 对齐：错误文本复用增量消息，只发一次 error 事件
                        await self.store.append_assistant_delta(
                            sid, ans_msg.id, ev["text"])
                        await self.store.finish_assistant(sid, ans_msg.id)
                        await on_event(AgentEvent(
                            type="error", message=ev["text"]))
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(stderr_task, timeout=2.0)
            self._procs.pop(sid, None)
        if not saw_done and not error_text:
            tail = "\n".join(stderr_lines[-5:])[-400:]
            await self._fail_with(sid, ans_msg.id,
                                  f"claude CLI 未正常收尾；stderr 梗概：{tail}")

    async def _fail_with(self, sid: str, mid: str, text: str) -> None:
        """失败收尾：错误文本复用增量 assistant 消息，不另落一条。"""
        await self.store.append_assistant_delta(sid, mid, text)
        err = AgentError(text)
        err.recorded = True  # send_message 据此跳过重复落库
        raise err
