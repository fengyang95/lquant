"""AgentService 抽象与工厂。claude code 接入方实现同一接口。"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from pathlib import Path

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

    @abstractmethod
    async def create_session(self, context: dict | None,
                             agent_config: dict | None = None) -> Session: ...

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
        """占住会话的执行槽位；已被占用 → 409。

        为什么必须挡：运行时引用（任务 / 子进程）是按会话**单槽**存的，
        同会话两条并发消息会互相覆盖引用 —— `/cancel` 打到错的那个进程、
        先结束的那个把另一个的引用 pop 掉变成孤儿，前端还会看到两条回答的事件交错。
        单飞（one in-flight per session）是唯一说得清的语义。
        """
        if self.is_busy(sid):
            raise AgentError("该会话已有正在执行的回答，请等待完成或先取消",
                             status_code=409)
        self._tasks[sid] = asyncio.current_task()

    def _release(self, sid: str) -> None:
        """只释放**自己的**槽位：别人接手后不能被我们 pop 掉。"""
        if self._tasks.get(sid) is asyncio.current_task():
            self._tasks.pop(sid, None)

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
    """
    from lquant.core.config import get_settings  # noqa: PLC0415

    a = get_settings().agent
    return {"provider": a.provider, "skills": a.default_skills,
            "mcp_tools": a.default_mcp_tools}


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

    ``provider=None`` → 用全局默认（``settings.agent.provider``）。
    ``store=None`` → 全局 store。缓存按 store 区分是必须的：A2A 执行器拿的是
    自己注入的 store，若按 provider 名缓存，它会拿到绑在全局库上的实例，
    往不存在的会话里写消息（FOREIGN KEY 直接炸）。
    """
    from lquant.core.config import get_settings  # noqa: PLC0415

    s = get_settings()
    name = provider or s.agent.provider
    st = store or _get_store()
    key = (st.path, name)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    if name == "mock":
        from lquant.agent.mock import MockAgentService  # noqa: PLC0415

        svc: AgentService = MockAgentService(st)
    elif name in ("claude_code", "codex"):
        _warn_autonomous_once(name, s.agent.skip_permissions)
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
