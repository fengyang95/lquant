"""CliAgentService：把「起一个无头 CLI 子进程、把它的输出流映射成事件」抽成公共骨架。

claude_code 与 codex 两个 provider 的差异**全部收在四个钩子里**：

- ``label`` / ``stderr_tag``：日志与报错里的 provider 名
- ``_build_cmd(content, cli_sid)``：命令行形状
- ``_parse_line(raw)``：一行输出 → 归一化事件（无状态）
- ``_make_line_parser()``：本轮使用的行解析器；跨行有状态的 provider
  （claude 的流式去重）覆写它，默认即 ``_parse_line`` 的薄封装

其余（会话落库、会话单飞、超时、取消、子进程回收、失败收尾）只有**一份**实现。
这类「同一套语义两个出口」的分叉是本仓的高频缺陷来源（A2A 的错误码在
executor 与 rpc 各写一遍、`/ask` 与 A2A 的裁剪口径各写一遍，都栽过），
所以第二个 provider 落地时不再复制第二份。

归一化事件契约（两个 parser 都必须产出这套键）::

    {"kind": str, "text": str, "name": str, "args": dict,
     "summary": str, "session_id": str, "tool_use_id": str, "data": dict}

``kind`` 语义：

===============  ==========================================================
``delta``        正文增量：落库 + 外发（唯一会改变回答正文的事件）
``thinking``     推理过程（只外发，供过程数据透传）
``system``       白名单化的运行时信息（只外发）
``tool_call``    工具调用：名 + 入参
``tool_result``  工具结果：全文 + 截断摘要
``session``      记录 CLI 侧会话 id（codex 的 thread_id、claude 的 session_id）
``done``         正常收尾
``error``        **终态失败**（claude 的 error 结果行、codex 的 turn.failed）
===============  ==========================================================

⚠️ 别把「非致命告警」映射成 ``error``：codex 会把「模型元数据缺失」这类
提示也发成 item 级 error，而那一轮仍会正常 completed —— 用 ``error`` 会让
每一次调用都被判死。非致命信息走 ``system``（带 ``data.level``）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from lquant.agent.errors import AgentError
from lquant.agent.schemas import AgentEvent, Message, Session
from lquant.agent.service import AgentService, Emit, _warn_autonomous_once
from lquant.agent.sessions import SessionStore
from lquant.agent.spawn import SpawnedChild, spawn_child
from lquant.agent.workspace import ensure_workspace

_LOG = logging.getLogger(__name__)


def _name_set(value: object) -> set[str] | None:
    """会话配置里的能力名单 → 启用集。

    ``None`` = 不裁剪（全开）；列表/元组/集合 = 按名单裁剪（空集合 = 一个都不启用）。
    形态不合法时按**不裁剪**处理：宁可多给能力，也不要因为一条脏数据让 agent
    变成什么都不会的裸模型（能力缺失是静默的，用户只会看到「它怎么不会用工具」）。
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple, set, frozenset)):
        return {str(x) for x in value}
    return None


def _global_config() -> dict:
    """运行时全局 agent 配置（延迟导入：cli_agent 被 service 延迟加载）。"""
    from lquant.agent.runtime import effective_agent_config  # noqa: PLC0415

    return effective_agent_config()


class CliAgentService(AgentService):
    """无头 CLI provider 的公共基类；子类只需实现命令构造与输出解析。

    每次 ``send_message`` 起一个 CLI 子进程，cwd 为工作区；续聊依赖 CLI 自身
    的会话续接能力（claude 的 ``--resume`` / codex 的 ``exec resume``），
    会话 id 落库在 ``ask_sessions.cli_session_id``。
    """

    #: 写进日志与报错文本的 provider 名（子类同时把它当 provider 用）
    label = "cli"
    #: stderr 日志前缀
    stderr_tag = "cli"
    #: provider 名 —— 会话级路由与实例缓存按它认人（子类与 label 同值）
    provider = "cli"

    def __init__(self, store: SessionStore) -> None:
        super().__init__(store)
        #: 会话 → 子进程（单槽：同会话同时只允许一个，见 _claim）
        self._procs: dict[str, SpawnedChild] = {}
        #: 工作区**父目录**：每个会话的工作区是 ``<base>/<sid>``（见 _workspace_for）。
        #: 注意这里存的是父目录而不是某个具体工作区 —— 同一个 service 实例会
        #: 并发服务多个会话，具体工作区必须按轮传入，不能是实例状态。
        self._workspace_base: Path = Path()
        self._root: Path = Path()
        # 运行参数一律**在每次运行时解析**，不在构造时快照：service 实例是按
        # (store, provider) 缓存的单例，快照下来「前端改完超时/权限」就永远不生效。
        # None = 没显式指定 → 走运行时全局配置（可在「问 AI」页改）。
        self._timeout_override: float | None = None
        self._skip_permissions_override: bool | None = None
        self._partial_messages_override: bool | None = None

    def _init_runtime(self, *, workspace_dir: str, root: Path,
                      timeout_seconds: float | None = None,
                      skip_permissions: bool | None = None,
                      partial_messages: bool | None = None) -> None:
        """公共运行时初始化（子类解析完 provider 专属配置后调用）。

        三个 ``*_override`` 都为 ``None`` 时表示「跟随运行时配置」，这是生产路径；
        测试与 A2A 执行器会显式传值，那种情况以显式值为准。
        """
        self._root = Path(root)
        self._workspace_base = self._root / workspace_dir
        self._timeout_override = None if timeout_seconds is None else float(timeout_seconds)
        self._skip_permissions_override = skip_permissions
        self._partial_messages_override = partial_messages

    # ---- 运行参数解析（会话 cfg > 构造参数 > 运行时全局配置）----------------

    def _timeout_for(self, cfg: dict) -> float:
        if cfg.get("timeout_seconds") is not None:
            return float(cfg["timeout_seconds"])
        if self._timeout_override is not None:
            return self._timeout_override
        return float(_global_config()["timeout_seconds"])

    def _resolve_skip_permissions(self, cfg: dict | None = None) -> bool:
        """会话级 ``skip_permissions`` > 构造时显式值 > 运行时全局配置。

        会话级能覆盖是「这一轮敢不敢放手」这个诉求的直接体现：一个只问答的
        会话不该继承全局打开的 ``--dangerously-skip-permissions``。
        ``cfg`` 里的 ``None`` 是「跟随默认档」，**不等于 False** —— 用
        ``is not None`` 判断，别写成 ``if cfg.get(...)``。
        """
        cfg = cfg or {}
        if cfg.get("skip_permissions") is not None:
            return bool(cfg["skip_permissions"])
        if self._skip_permissions_override is not None:
            return self._skip_permissions_override
        return bool(_global_config()["skip_permissions"])

    def _resolve_partial_messages(self, cfg: dict | None = None) -> bool:
        cfg = cfg or {}
        if cfg.get("partial_messages") is not None:
            return bool(cfg["partial_messages"])
        if self._partial_messages_override is not None:
            return self._partial_messages_override
        return bool(_global_config()["partial_messages"])

    def _workspace_for(self, sid: str, cfg: dict) -> Path:
        """按会话**锁定的**能力配置准备并返回该会话的工作区。

        每轮都重新生成（幂等）：只重写 CLAUDE.md/AGENTS.md 与 ``.claude/``，
        所以改了 skill 文件下一轮就生效；agent 在工作区里写下的产物不受影响。

        ``cfg`` 里的 ``skills`` / ``mcp_tools``：``None`` = 不裁剪（全开），
        列表（含空列表）= 按名单裁剪。
        """
        return ensure_workspace(
            str(self._workspace_base / sid), self._root,
            enabled_skills=_name_set(cfg.get("skills")),
            enabled_tools=_name_set(cfg.get("mcp_tools")))

    # ---- 子类钩子 ---------------------------------------------------------

    def _build_cmd(self, content: str, cli_sid: str | None, workspace: Path,
                   cfg: dict | None = None) -> list[str]:
        """构造本次调用的命令行。

        ``workspace`` 是本次运行的会话工作区；``cfg`` 是会话级配置 —— 权限 /
        token 级流式这类**按会话可覆盖**的开关必须从它解析（子类调
        ``self._resolve_skip_permissions(cfg)``）。早期版本没有这个参数，两个
        子类只能读全局值，于是会话级的权限旋钮写了也没人看。
        """
        raise NotImplementedError

    def _parse_line(self, raw: bytes) -> list[dict]:
        """CLI 的一行输出 → 归一化事件列表。"""
        raise NotImplementedError

    def _make_line_parser(self) -> Callable[[bytes], list[dict]]:
        """构造**本轮 ``_consume`` 专用**的行解析器（无状态默认实现）。

        默认逐行转发给 ``_parse_line``。跨行有状态的 provider 覆写它 ——
        claude 的 token 增量去重需要跨行记住 ``message.id``（见 claude_json
        的 ``StreamParser``）。``_consume`` 每轮只调用**一次**，把有状态解析器
        的作用域天然限制在「一轮输出」里；不要实现成每行新建一个实例，
        那等于退回无状态。
        """
        return lambda raw: self._parse_line(raw)

    # ---- 会话 -------------------------------------------------------------

    async def create_session(self, context: dict | None,
                             agent_config: dict | None = None, *,
                             title: str | None = None) -> Session:
        return await self.store.create(context, agent_config, title=title)

    async def list_sessions(self) -> list[Session]:
        return await self.store.list()

    async def delete_session(self, sid: str) -> None:
        await self.cancel(sid)
        await self.store.delete(sid)

    async def get_messages(self, sid: str) -> list[Message]:
        return await self.store.messages(sid)

    # ---- 主链路 -----------------------------------------------------------

    async def send_message(self, sid: str, content: str, on_event: Emit,
                           user_msg: Message | None = None) -> Message:
        if not content.strip():
            raise AgentError("消息不能为空", status_code=400)
        ses = await self.store.get(sid)
        if ses is None:
            raise AgentError("会话不存在", status_code=404)
        # API 路径已同步落库并传入，避免二次写入（也避免轮询等待）
        user_msg = user_msg or await self.persist_user_message(sid, content)
        self._claim(sid)  # 同会话单飞 + 全局并发上限：超限 → 429，不许互相踩运行时引用
        try:
            await self._run(sid, content, on_event)
        except asyncio.CancelledError:
            await self._mark_interrupted(sid)
            await on_event(AgentEvent(type="error", message="已中断"))
            raise
        except Exception as e:  # noqa: BLE001
            _LOG.exception("%s agent 失败", self.label)
            if not getattr(e, "recorded", False):
                # _fail_with 已把错误写入增量消息；此处兜底其他异常
                await self.store.add_message(sid, "assistant", f"出错了：{e}")
            await on_event(AgentEvent(type="error", message=str(e)))
        finally:
            self._release(sid)
        return user_msg

    async def cancel(self, sid: str) -> None:
        t = self._tasks.pop(sid, None)
        if t is not None and not t.done():
            t.cancel()
        proc = self._procs.pop(sid, None)
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()

    async def _run(self, sid: str, content: str, on_event: Emit) -> None:
        cfg = await self.store.get_agent_config(sid)
        # 权限是**每次运行**解析的（会话级 > 构造参数 > 全局，前端都能改），
        # 所以「全自主」告警也在这里判：只判一次全局值会漏掉会话级打开的情形。
        _warn_autonomous_once(self.provider, self._resolve_skip_permissions(cfg))
        workspace = self._workspace_for(sid, cfg)
        cli_sid = await self.store.get_cli_session_id(sid)
        content = await self._with_briefing(sid, content, cli_sid)
        cmd = self._build_cmd(content, cli_sid, workspace, cfg)
        proc: SpawnedChild | None = None
        try:
            proc = spawn_child(cmd, cwd=str(workspace))
            await proc.attach()
        except OSError as e:
            # 子进程可能已经起来了（posix_spawn 成功、attach 才失败）：
            # 不回收就会留下没人管的 CLI 进程（全自主权限，还会继续跑）
            if proc is not None:
                await self._reap(proc)
            raise AgentError(f"无法启动 {self.label} CLI：{e}") from e
        self._procs[sid] = proc
        # 运行中列表要能看到 pid 与工作区：用户判断「卡住了要不要杀」时，
        # 能拿 pid 去 ps 一眼比只看到「已跑 300 秒」有用得多。
        self.set_run_info(sid, pid=proc.pid, workspace=str(workspace))
        await self._consume(sid, proc, on_event, cfg=cfg)

    async def _with_briefing(self, sid: str, content: str, cli_sid: str | None) -> str:
        """首次调用时把「另开会话」带来的上下文简报拼在提问前面。

        来历：换 provider 另开会话（``POST /ask/sessions/{sid}/fork``）时，新会话
        那边没有任何 CLI 侧历史（claude 的 session_id / codex 的 thread_id 是
        **另一个 CLI 的**，续接过去就是串台）。所以老会话的对话被渲染成一段
        简报落在 ``session.context.briefing`` 里，在这里注入。

        为什么注入点放在这里而不是建会话时当成一条 user 消息写进去：那样这条
        简报会以「用户说过的话」出现在消息列表里，改也改不掉、删也删不干净；
        而且用户看到的正文会变成一大段转述。放在命令行构造这一层，简报只影响
        送给 CLI 的那一份 prompt，界面上的对话仍然是干净的。

        只在 ``cli_sid`` 还是空的时候注入（= 本会话还没跟 CLI 对上话）：第二
        轮起 CLI 自己带着上下文，再注入一次就是同一段历史重复两遍。
        """
        if cli_sid:
            return content
        ses = await self.store.get(sid)
        ctx = ses.context if ses is not None else None
        if not isinstance(ctx, dict):
            return content
        briefing = str(ctx.get("briefing") or "").strip()
        if not briefing:
            return content
        return (f"{briefing}\n\n"
                "（以上是此前另一个后端会话的对话记录，供你接续上下文；"
                "请直接回答最后的问题。）\n\n"
                f"---\n\n{content}")

    @staticmethod
    async def _reap(proc: SpawnedChild) -> None:
        """terminate + waitpid 回收（半启动的子进程必须收干净）。"""
        await proc.reap()

    async def _consume(self, sid: str, proc: SpawnedChild, on_event: Emit,
                       cfg: dict | None = None) -> None:
        cfg = cfg or {}
        timeout = self._timeout_for(cfg)  # 会话级 > 构造参数 > 运行时全局
        stderr_lines: list[str] = []

        async def pump_stderr() -> None:
            async for raw in proc.stderr:
                line = raw.decode(errors="replace").rstrip()
                stderr_lines.append(line)
                _LOG.warning("[%s stderr] %s", self.stderr_tag, line)

        stderr_task = asyncio.create_task(pump_stderr())
        # 先登记 id 再落库：取消可能正好落在下面的 await 上，那时拿不到返回值，
        # 但补文案必须有目标（见 _mark_interrupted）。
        mid = uuid.uuid4().hex
        self._ans_id[sid] = mid
        ans_msg = await self.store.add_message(sid, "assistant", "", mid=mid)
        started = time.monotonic()
        saw_done = False
        error_text = ""  # kind=error 时记录，收尾跳过二次 fail
        # tool_use_id → 工具名：有些 CLI 的 result 块自身不带名字，靠它回填
        # （过程数据帧要能标出结果属于哪个工具）
        tool_names: dict[str, str] = {}
        # 每轮只构造一次：有状态解析器（claude 的流式去重）靠它跨行记住状态
        parse = self._make_line_parser()
        try:
            while True:
                if time.monotonic() - started > timeout:
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
                for ev in parse(raw):
                    kind = ev["kind"]
                    if kind == "delta":
                        await self.store.append_assistant_delta(
                            sid, ans_msg.id, ev["text"])
                        await on_event(AgentEvent(
                            type="assistant_delta", text=ev["text"],
                            message_id=ans_msg.id))
                    elif kind == "thinking":
                        await on_event(AgentEvent(
                            type="thinking", text=ev["text"]))
                    elif kind == "system":
                        await on_event(AgentEvent(
                            type="system", data=ev["data"]))
                    elif kind == "tool_call":
                        if ev.get("tool_use_id"):
                            tool_names[ev["tool_use_id"]] = ev["name"]
                        await on_event(AgentEvent(
                            type="tool_call", name=ev["name"], args=ev["args"]))
                    elif kind == "tool_result":
                        await on_event(AgentEvent(
                            type="tool_result",
                            name=ev.get("name") or tool_names.pop(
                                ev.get("tool_use_id", ""), ""),
                            text=ev["text"], summary=ev["summary"]))
                    elif kind == "session":
                        # CLI 侧会话 id 可能先于正文到达（codex 的 thread.started）
                        if ev.get("session_id"):
                            await self.store.set_cli_session_id(
                                sid, ev["session_id"])
                    elif kind == "done":
                        saw_done = True
                        if ev.get("session_id"):
                            await self.store.set_cli_session_id(
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
            # 取消已请求但**还没投递**时，投递点会落在 finally 里的第一个 await
            # 上（进程回收），回收与留痕都会被半途打断。先吃掉这个请求让收尾
            # 跑完，结束后再原样抛出 —— 取消语义不变，少一次「进程没回收干净」。
            task = asyncio.current_task()
            cancelling = task is not None and task.cancelling() > 0
            if cancelling:
                with contextlib.suppress(Exception):  # noqa: BLE001
                    task.uncancel()
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(stderr_task, timeout=2.0)
            # 只回收自己的引用：取消路径可能已被 cancel() 提前 pop，
            # 那种情况下不能再动（否则会误伤后续接手的那个 run）
            if self._procs.get(sid) is proc:
                self._procs.pop(sid, None)
            if cancelling:
                raise asyncio.CancelledError()
        # 跑出明确结论（正常收尾或 CLI 自己报了错）→ 取消标记不再适用。
        # 注意这行必须在 finally **之后**：取消落在回收阶段时上面已经 raise，
        # 不会走到这里，标记才能补上。
        self._ans_id.pop(sid, None)
        if not saw_done and not error_text:
            tail = "\n".join(stderr_lines[-5:])[-400:]
            await self._fail_with(sid, ans_msg.id,
                                  f"{self.label} CLI 未正常收尾；stderr 梗概：{tail}")

    async def _fail_with(self, sid: str, mid: str, text: str) -> None:
        """失败收尾：错误文本复用增量 assistant 消息，不另落一条。"""
        await self.store.append_assistant_delta(sid, mid, text)
        err = AgentError(text)
        err.recorded = True  # send_message 据此跳过重复落库
        raise err
