"""AgentService 抽象与工厂。claude code 接入方实现同一接口。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from lquant.agent.errors import AgentError
from lquant.agent.schemas import AgentEvent, Message, Session
from lquant.agent.sessions import SessionStore

Emit = Callable[[AgentEvent], Awaitable[None]]


class AgentService(ABC):
    def __init__(self, store: SessionStore) -> None:
        self.store = store

    @abstractmethod
    async def create_session(self, context: dict | None) -> Session: ...

    @abstractmethod
    async def list_sessions(self) -> list[Session]: ...

    @abstractmethod
    async def delete_session(self, sid: str) -> None: ...

    @abstractmethod
    async def get_messages(self, sid: str) -> list[Message]: ...

    @abstractmethod
    async def send_message(self, sid: str, content: str, on_event: Emit) -> Message: ...

    @abstractmethod
    async def cancel(self, sid: str) -> None: ...


_cache: dict[str, AgentService] = {}


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
        else:
            raise AgentError(f"未知 agent provider: {provider}")
    return _cache["service"]
