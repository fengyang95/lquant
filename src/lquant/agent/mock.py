"""MockAgentService：脚本化回复，tool_call 真调行情接口，不依赖 LLM。"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid

from lquant.agent.errors import AgentError
from lquant.agent.schemas import AgentEvent, Message, Session
from lquant.agent.service import AgentService
from lquant.market.ticks import fetch_quotes

_LOG = logging.getLogger(__name__)


def _extract_symbol(text: str, context: dict) -> str | None:
    """从提问或会话上下文里抠 6 位代码。"""
    if sym := context.get("symbol"):
        return str(sym)
    m = re.search(r"\b([03568]\d{5})\b", text)
    return m.group(1) if m else None


class MockAgentService(AgentService):
    provider = "mock"

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

    async def send_message(self, sid, content, on_event, user_msg=None) -> Message:
        if not content.strip():
            raise AgentError("消息不能为空", status_code=400)
        ses = await self.store.get(sid)
        if ses is None:
            raise AgentError("会话不存在", status_code=404)
        # API 路径已同步落库并传入，避免二次写入（也避免轮询等待）
        user_msg = user_msg or await self.persist_user_message(sid, content)
        self._claim(sid)  # 同会话单飞（与 claude_code provider 同一条纪律）
        try:
            await self._run(sid, content, ses.context, on_event)
        except asyncio.CancelledError:
            await self._mark_interrupted(sid)
            await on_event(AgentEvent(type="error", message="已中断"))
            raise
        except Exception as e:  # noqa: BLE001
            _LOG.exception("mock agent 失败")
            err = await self.store.add_message(sid, "assistant", f"出错了：{e}")
            await on_event(AgentEvent(type="error", message=str(e)))
            return err
        finally:
            self._release(sid)
        return user_msg

    async def _run(self, sid, content, context, on_event) -> None:
        on_event = self.trace_emitter(sid, on_event)   # 与 cli provider 同口径
        rows = []
        symbol = _extract_symbol(content, context)
        tc = {"name": "market_overview", "args": {}}
        if symbol:
            tc = {"name": "get_quote", "args": {"symbols": [symbol]}}
            await on_event(AgentEvent(type="tool_call", name="get_quote",
                                      args={"symbols": [symbol]}))
            await asyncio.sleep(0.2)
            rows = await asyncio.wait_for(
                asyncio.to_thread(fetch_quotes, [symbol]), timeout=8.0)
            await on_event(AgentEvent(type="tool_result", name="get_quote",
                                      summary=f"查询到 {len(rows)} 条实时行情"))
        else:
            await on_event(AgentEvent(type="tool_call", name="market_overview", args={}))
            await on_event(AgentEvent(type="tool_result", name="market_overview",
                                      summary="大盘概览已获取"))

        mid = uuid.uuid4().hex
        self._ans_id[sid] = mid            # 先登记再落库，取消才有目标可写
        ans_msg = await self.store.add_message(sid, "assistant", "", tool_calls=[tc],
                                               mid=mid)
        parts = ["（Mock 回答 | provider=mock，非 LLM 生成）"]
        if symbol:
            q = rows[0] if rows else None
            price = q.get("price") if q else None
            if price is not None:
                parts.append(f"{symbol} 当前价 {price}。")
            else:
                parts.append(f"{symbol} 暂无实时行情。")
        else:
            parts.append("今天大盘整体平稳。")
        parts.append("数据时点：实时快照（Mock 演示，正式实现由 claude code 生成分析）。")
        try:
            for chunk in "".join(parts):
                await self.store.append_assistant_delta(sid, ans_msg.id, chunk)
                await on_event(AgentEvent(type="assistant_delta", text=chunk,
                                          message_id=ans_msg.id))
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            # 与 claude_code provider 同口径：取消也要留下明确标记
            await self._mark_interrupted(sid)
            raise
        await self.store.finish_assistant(sid, ans_msg.id)
        self._ans_id.pop(sid, None)        # 已跑完，取消标记不再适用
        await on_event(AgentEvent(type="done", message_id=ans_msg.id))

    async def cancel(self, sid: str) -> None:
        t = self._tasks.pop(sid, None)
        if t is not None and not t.done():
            t.cancel()
