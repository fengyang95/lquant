"""AgentService 抽象与工厂。claude code 接入方实现同一接口。"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
import weakref
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from pathlib import Path

from lquant.agent.concurrency import (
    register_run,
    release_run,
    running_count,
)
from lquant.agent.errors import AgentError
from lquant.agent.schemas import AgentEvent, Message, Session
from lquant.agent.sessions import SessionStore

Emit = Callable[[AgentEvent], Awaitable[None]]


class AgentService(ABC):
    #: provider 名（claude_code / codex / mock）。会话级路由与实例缓存都按它认人 ——
    #: 不能靠类名或 label 反推：label 是给日志看的，mock 又没有 label。
    provider: str = ""

    def __init__(self, store: SessionStore) -> None:
        self.store = store
        #: 会话 → 正在跑的回答任务（单槽：同一会话同时只允许一个，见 _claim）
        self._tasks: dict[str, asyncio.Task | None] = {}
        #: 会话 → 本轮运行的可见信息（开始时间 / pid …）。
        #: 与 ``_tasks`` 同生命周期：``_claim`` 建、``_release`` 清。
        self._run_meta: dict[str, dict] = {}
        #: 会话 → 本轮 assistant 占位消息 id。**先登记再落库**：取消可能正好落在
        #: ``add_message`` 的 await 上，那时拿不到返回值，只能靠预先登记的 id
        #: 找到目标行补文案（见 ``_mark_interrupted``）。
        self._ans_id: dict[str, str] = {}

    def trace_emitter(self, sid: str, on_event: Emit) -> Emit:
        """把 on_event 包成「带留痕」的版本（见 agent/trace.py）。

        放在基类、由 provider 在跑之前包一次：留痕是**过程数据的一次落库**，
        不该由调用方（HTTP / A2A / 测试）各自记得去挂 —— 那样迟早有一条路径漏掉。
        """
        from lquant.agent.trace import TracedEmitter  # noqa: PLC0415

        return TracedEmitter(sid, run_id=uuid.uuid4().hex, emit=on_event,
                             sink=self.store.add_tool_trace,
                             msg_id=lambda: self._ans_id.get(sid, ""))

    async def _mark_interrupted(self, sid: str) -> None:
        """取消留痕：给本轮的 assistant 消息补一句「（已中断）」。

        为什么放在最外层（``send_message`` 的取消分支）而不是各 provider 的
        循环里：取消可能落在「占位消息落库」这个 await 上，那时 provider 内部
        的 try/finally 还没进去；只有「先登记 id + 最外层兜底」才能覆盖全部
        取消时点。

        幂等靠 ``_ans_id`` 的 pop：本轮跑出**明确结论**（done / error）时
        provider 会先把它清掉，于是这里天然不再补 —— 所以这个方法可以
        「只要被调用就补」，不必去猜消息内容完不完整（截断到一半的回答同样
        需要这个标记，只看「内容非空」会把它当成完整回答）。
        """
        mid = self._ans_id.pop(sid, None)
        if not mid:
            return
        try:
            if await self.store.get_message(sid, mid) is not None:
                await self.store.append_assistant_delta(sid, mid, "（已中断）")
        except Exception:  # noqa: BLE001 - 留痕失败不该改写取消语义
            logging.getLogger("lquant.agent").exception(
                "取消留痕失败 sid=%s mid=%s", sid, mid)

    @abstractmethod
    async def create_session(self, context: dict | None,
                             agent_config: dict | None = None, *,
                             title: str | None = None) -> Session: ...

    @abstractmethod
    async def list_sessions(self) -> list[Session]: ...

    @abstractmethod
    async def delete_session(self, sid: str) -> None: ...

    @abstractmethod
    async def get_messages(self, sid: str) -> list[Message]: ...

    @abstractmethod
    async def send_message(self, sid: str, content: str, on_event: Emit,
                           user_msg: Message | None = None) -> Message: ...

    @abstractmethod
    async def cancel(self, sid: str) -> None: ...

    def is_busy(self, sid: str) -> bool:
        """该会话是否有未结束的回答。"""
        t = self._tasks.get(sid)
        return t is not None and not t.done()

    def _claim(self, sid: str) -> None:
        """占住会话的执行槽位；已被占用 → 409，全局并发超限 → 429。

        为什么必须挡同会话并发：运行时引用（任务 / 子进程）是按会话**单槽**存的，
        同会话两条并发消息会互相覆盖引用 —— `/cancel` 打到错的那个进程、
        先结束的那个把另一个的引用 pop 掉变成孤儿，前端还会看到两条回答的事件交错。
        单飞（one in-flight per session）是唯一说得清的语义。

        为什么要挡全局并发：每个回答都是一个全自主权限的 CLI 子进程，没有上限时
        多开会话齐发就能把本机打死。上限判定放在这里（而不是 API 层）是刻意的：
        A2A 出站路径不经过 ``server.api.ask``，只拦 HTTP 那个口等于给 A2A 留了
        一条绕过上限的路。**不排队**：直接拒绝，让用户看到明确的「超限」而不是
        一个永远不动的转圈。
        """
        if self.is_busy(sid):
            raise AgentError("该会话已有正在执行的回答，请等待完成或先取消",
                             status_code=409)
        cap = max_concurrent_runs()
        if running_count() >= cap:
            raise AgentError(
                f"同时在跑的 agent 已达上限 {cap} 个，请等待其中一些结束或先终止",
                status_code=429)
        self._tasks[sid] = asyncio.current_task()
        self._run_meta[sid] = {
            #: 归属校验用（见 _release 的第 3 种情形）；**不进 API 响应**
            "task": self._tasks[sid],
            "provider": self.provider,
            "started_at": time.time(),
            "started_monotonic": time.monotonic(),
            "pid": None,
            "workspace": "",
        }
        register_run(self, sid)

    def _release(self, sid: str) -> None:
        """只释放**自己的**槽位：别人接手后不能被我们 pop 掉。

        三种情形都要对：

        1. 正常结束 —— ``_tasks[sid]`` 还是自己，连账本一起放掉；
        2. ``cancel()`` 先把 ``_tasks`` 里的任务 pop 掉再 cancel（所以身份判断
           恒为假），这一轮确实结束了，槽位空着就必须放账本与运行信息 ——
           不放的话账本会留下永久记录，并发上限被慢慢吃光，而现象只是
           「用着用着就开始 429」；
        3. ``cancel()`` 之后**新的一轮已经接手**了同一个 sid（pop 掉的瞬间
           ``is_busy`` 就是假，理论上接得上）。这时候绝对不能放账本：那等于
           把别人的额度扣掉，上限形同虚设。判据是槽位现在有没有人。
        """
        me = asyncio.current_task()
        if self._tasks.get(sid) is me:
            self._tasks.pop(sid, None)
            self._run_meta.pop(sid, None)
            release_run(self, sid)
            return
        if self._tasks.get(sid) is None and self._run_meta.get(sid, {}).get("task") is me:
            self._run_meta.pop(sid, None)
            release_run(self, sid)

    def set_run_info(self, sid: str, **fields: object) -> None:
        """补充本轮运行的可见信息（当前只有 CLI provider 会写 pid / workspace）。

        容忍会话已经不在跑（取消路径可能先把槽位释放掉）：这种情况下丢掉这条
        补充信息正是想要的 —— 往一个已经不存在的槽位里写字，等于给进程列表
        留一条永远不消失的幽灵记录。
        """
        meta = self._run_meta.get(sid)
        if meta is not None:
            meta.update(fields)

    def running_runs(self) -> list[dict]:
        """本实例上正在跑的回答（供 ``GET /ask/runs`` 汇总）。

        ``elapsed`` 由 ``started_monotonic`` 现算而不是落库：它只是给人看的
        「跑了多久」，不需要跨进程一致，也就不值得为它多存一列。
        """
        now = time.monotonic()
        out: list[dict] = []
        for sid, task in list(self._tasks.items()):
            if task is None or task.done():
                continue
            meta = dict(self._run_meta.get(sid, {}))
            started = meta.pop("started_monotonic", None)
            meta.pop("task", None)  # asyncio.Task 不可序列化，绝不能进 API 响应
            out.append({
                "session_id": sid,
                "provider": meta.pop("provider", self.provider),
                "elapsed_seconds": round(now - started, 1) if started else 0.0,
                **meta,
            })
        return out

    async def persist_user_message(self, sid: str, content: str) -> Message:
        """同步落库 user 消息并返回。

        从 send_message 里拆出来，是为了让 API 能在**起后台任务之前**就拿到
        user 消息：否则只能轮询等它落库，既有并发取错消息的竞态，还得设一个
        凭空的超时上限。`send_message(user_msg=...)` 据此跳过二次落库。
        """
        return await self.store.add_message(sid, "user", content)


#: (库路径, provider) → 该 provider 的 service 单例。多个 provider 可以同时存在
#: （会话级选择），同一个库上它们**共用同一个 SessionStore**（见 _get_store）。
_cache: dict[tuple[str, str], AgentService] = {}

#: 全局唯一的会话存储。会话事实源只有一个：换 provider 只换「谁来答」，
#: 会话/消息/A2A 任务记录必须还在同一张表里（A2A 的 contextId 就是 ask_sessions.id）。
#: **按 root 键控**：root 变了（测试里换 LQ_ROOT）必须换库，否则会写到上一个
#: root 的 ask.db 去 —— 集成测试就是靠「清缓存 + 换 LQ_ROOT」做隔离的。
_store: SessionStore | None = None
_store_root: Path | None = None

_WARNED_PROVIDERS: set[str] = set()

#: 创建过的所有 service 实例（含 A2A 执行器自带 store 的那份）。
#: 弱集合：只用来枚举「谁可能正在跑」，不参与缓存，不延长生命周期。
_ALL_SERVICES: weakref.WeakSet[AgentService] = weakref.WeakSet()


def max_concurrent_runs() -> int:
    """全局并发上限（运行时配置，前端可在 AI 设置面板改，改完立即生效）。"""
    from lquant.agent.runtime import effective_agent_config  # noqa: PLC0415

    return int(effective_agent_config()["max_concurrent_runs"])


def running_runs_all() -> list[dict]:
    """**所有** provider 实例上正在跑的回答。

    为什么要跨实例汇总：``_cache`` 只装走全局 store 的那些实例，A2A 执行器
    自带的实例不在里面；只看缓存会漏掉「A2A 那边正在跑」的那部分，而
    「运行中」列表漏一条比多一条更糟（用户会照着这个列表判断能不能关服务）。
    """
    rows: list[dict] = []
    for svc in list(_ALL_SERVICES):
        try:
            rows.extend(svc.running_runs())
        except Exception:  # noqa: BLE001 - 枚举不该因为某个实例异常而整体失败
            logging.getLogger(__name__).warning("枚举运行中回答失败", exc_info=True)
    rows.sort(key=lambda r: r.get("elapsed_seconds", 0.0), reverse=True)
    return rows


def _get_store() -> SessionStore:
    """惰性建 SessionStore；root 变化时换库，并把绑在旧库上的 service 一起作废。

    作废 service 缓存是必须的：它们构造时就抓了旧 store，不清掉就会出现
    「按新库读会话、往旧库写消息」的分裂 —— 表现为新建的会话查不到、
    或两个库各有一半消息。旧 store 的关闭交给 :func:`shutdown_agent_service`
    （aiosqlite 的连接只能 await 关，这里是同步上下文；生产环境 root 不变，
    只有测试会走到这条重建路径）。
    """
    global _store, _store_root  # noqa: PLW0603 - 单例，与 _cache 同一条纪律
    from lquant.core.config import get_settings  # noqa: PLC0415

    root = get_settings().root
    if _store is None or _store_root != root:
        _store = SessionStore(str(root / "data" / "ask.db"))
        _store_root = root
        _cache.clear()
    return _store


def default_agent_config() -> dict:
    """新建会话未指定时的能力配置：全局 provider + 全局默认 skill / MCP 工具。

    ``skills`` / ``mcp_tools`` 为 ``None`` 表示**不裁剪**（全开），空列表表示
    一个都不启用 —— 两者语义不同，前端据此决定预填成什么样。

    取值走 :func:`lquant.agent.runtime.effective_agent_config`：这三项都能在
    「问 AI」页的 AI 设置面板里改，改完**下一次新建会话即生效**。
    """
    from lquant.agent.runtime import effective_agent_config  # noqa: PLC0415

    a = effective_agent_config()
    return {"provider": a["provider"], "skills": a["default_skills"],
            "mcp_tools": a["default_mcp_tools"]}


def _warn_autonomous_once(name: str, skip_permissions: bool) -> None:
    if name in _WARNED_PROVIDERS or not skip_permissions:
        return
    logging.getLogger(__name__).warning(
        "%s provider 以「全自主」权限运行"
        "（claude_code: --dangerously-skip-permissions / "
        "codex: --dangerously-bypass-approvals-and-sandbox）："
        "该权限边界仅限本地单人环境，勿将服务暴露到非本机地址"
        "（API 绑定由 LQ_API_HOST 控制，默认 127.0.0.1）。"
        "不需要 CLI 落盘/执行命令时，设 agent.skip_permissions=false", name)
    _WARNED_PROVIDERS.add(name)


async def get_agent_service(provider: str | None = None, *,
                            store: SessionStore | None = None) -> AgentService:
    """按 provider 取实现；**每个 (store, provider) 一个单例**。

    ``provider=None`` → 用全局默认（运行时配置的 ``agent.provider``，可在
    「问 AI」页改，改完下一条消息就走新后端）。
    ``store=None`` → 全局 store。缓存按 store 区分是必须的：A2A 执行器拿的是
    自己注入的 store，若按 provider 名缓存，它会拿到绑在全局库上的实例，
    往不存在的会话里写消息（FOREIGN KEY 直接炸）。
    """
    from lquant.agent.runtime import effective_agent_config  # noqa: PLC0415

    cfg = effective_agent_config()
    name = provider or cfg["provider"]
    st = store or _get_store()
    key = (st.path, name)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    if name == "mock":
        from lquant.agent.mock import MockAgentService  # noqa: PLC0415

        svc: AgentService = MockAgentService(st)
    elif name in ("claude_code", "codex"):
        _warn_autonomous_once(name, bool(cfg["skip_permissions"]))
        if name == "claude_code":
            from lquant.agent.claude_code import (  # noqa: PLC0415
                ClaudeCodeAgentService,
            )

            svc = ClaudeCodeAgentService(st)
        else:
            from lquant.agent.codex import CodexAgentService  # noqa: PLC0415

            svc = CodexAgentService(st)
    else:
        raise AgentError(f"未知 agent provider: {name}")
    _cache[key] = svc
    _ALL_SERVICES.add(svc)
    return svc


async def get_service_for_session(sid: str, *,
                                  store: SessionStore | None = None) -> AgentService:
    """按会话**锁定的** provider 取实现；会话未指定（老会话 / A2A 建的）→ 全局默认。

    会话级操作（发消息 / 取消 / 读消息 / 删除）都要走这里，不能直接用
    ``get_agent_service()``：那会把「codex 会话」的取消打到 claude 的进程表上，
    而运行时引用（任务 / 子进程）是**按 service 实例**存的。

    ``store`` 传了就在那个库上解析（A2A 执行器用自己注入的那个）。
    """
    st = store or _get_store()
    cfg = await st.get_agent_config(sid)
    return await get_agent_service(cfg.get("provider"), store=st)


async def shutdown_agent_service() -> None:
    """关闭 agent service 的底层存储（aiosqlite 连接）并清空 provider 缓存。

    为什么必须显式关：aiosqlite 每条连接的 worker 线程是 **non-daemon**，且只有
    `await db.close()` 才会把它停掉。不关就会让解释器退出时卡在
    `threading._shutdown` —— 表现为 uvicorn 优雅停机不退出、测试跑完不返回。
    未创建过 service 时是空操作。
    """
    global _store, _store_root  # noqa: PLW0603 - 与 _get_store 同一处单例
    _cache.clear()
    store, _store, _store_root = _store, None, None
    if store is None:
        return
    try:
        await store.close()
    except Exception:  # noqa: BLE001 - 收尾失败只记日志，不阻断停机
        logging.getLogger(__name__).warning("关闭 agent 会话存储失败", exc_info=True)
    finally:
        # A2A 执行器单例持有同一个 store，必须一起放掉
        from lquant.agent.a2a.executor import reset_a2a_executor  # noqa: PLC0415

        reset_a2a_executor()
