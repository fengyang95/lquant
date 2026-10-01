"""AgentService 抽象与工厂。claude code 接入方实现同一接口。"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from lquant.agent.errors import AgentError
from lquant.agent.schemas import AgentEvent, Message, Session
from lquant.agent.sessions import SessionStore

Emit = Callable[[AgentEvent], Awaitable[None]]


class AgentService(ABC):
    def __init__(self, store: SessionStore) -> None:
        self.store = store
        #: 会话 → 正在跑的回答任务（单槽：同一会话同时只允许一个，见 _claim）
        self._tasks: dict[str, asyncio.Task | None] = {}

    @abstractmethod
    async def create_session(self, context: dict | None) -> Session: ...

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


_cache: dict[str, AgentService] = {}

_WARNED_PROVIDERS: set[str] = set()


async def get_agent_service() -> AgentService:
    """按 settings.agent.provider 选实现；单例缓存。"""
    if not _cache:
        from lquant.core.config import get_settings  # noqa: PLC0415

        s = get_settings()
        store = SessionStore(str(s.root / "data" / "ask.db"))
        provider = s.agent.provider
        if provider == "mock":
            from lquant.agent.mock import MockAgentService  # noqa: PLC0415

            _cache["service"] = MockAgentService(store)
        elif provider == "claude_code":
            from lquant.agent.claude_code import (  # noqa: PLC0415
                ClaudeCodeAgentService,
            )

            if provider not in _WARNED_PROVIDERS:
                if s.agent.skip_permissions:
                    logging.getLogger(__name__).warning(
                        "claude_code provider 以 --dangerously-skip-permissions 全自主运行："
                        "该权限边界仅限本地单人环境，勿将服务暴露到非本机地址"
                        "（API 绑定由 LQ_API_HOST 控制，默认 127.0.0.1）。"
                        "不需要 CLI 落盘/执行命令时，设 agent.skip_permissions=false")
                _WARNED_PROVIDERS.add(provider)
            _cache["service"] = ClaudeCodeAgentService(store)
        else:
            raise AgentError(f"未知 agent provider: {provider}")
    return _cache["service"]


async def shutdown_agent_service() -> None:
    """关闭单例 agent service 的底层存储（aiosqlite 连接）。

    为什么必须显式关：aiosqlite 每条连接的 worker 线程是 **non-daemon**，且只有
    `await db.close()` 才会把它停掉。不关就会让解释器退出时卡在
    `threading._shutdown` —— 表现为 uvicorn 优雅停机不退出、测试跑完不返回。
    未创建过 service 时是空操作。
    """
    svc = _cache.pop("service", None)
    if svc is None:
        return
    try:
        await svc.store.close()
    except Exception:  # noqa: BLE001 - 收尾失败只记日志，不阻断停机
        logging.getLogger(__name__).warning("关闭 agent 会话存储失败", exc_info=True)
    finally:
        # A2A 执行器单例持有同一个 store，必须一起放掉
        from lquant.agent.a2a.executor import reset_a2a_executor  # noqa: PLC0415

        reset_a2a_executor()
