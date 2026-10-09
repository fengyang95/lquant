"""MockAgentService 事件序列与异常路径。"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from lquant.agent.mock import MockAgentService
from lquant.agent.sessions import SessionStore


def _fake_quotes(symbols: list[str], backend=None, *, timeout: float = 5.0) -> list[dict]:
    """离线行情桩：不触网，返回确定性行。"""
    return [{"symbol": s, "name": "测试股", "price": 100.0 + len(s),
             "change_pct": 1.5, "ts": "2026-09-10 10:00:00"} for s in symbols]


@pytest.fixture()
async def svc(tmp_path, monkeypatch):
    monkeypatch.setattr("lquant.agent.mock.fetch_quotes", _fake_quotes)
    return MockAgentService(SessionStore(str(tmp_path / "ask.db")))


async def _run(svc, content, context=None):
    """创建会话、发送消息、收集事件。"""
    events: list = []

    async def on_event(e):
        events.append(e)

    ses = await svc.create_session(context)
    await svc.send_message(ses.id, content, on_event)
    return ses, events


async def test_event_sequence(svc):
    ses, events = await _run(svc, "贵州茅台怎么样")
    types = [e.type for e in events]
    assert types[0] == "tool_call"
    assert types[1] == "tool_result"
    assert types[-1] == "done"
    assert "assistant_delta" in types
    # done 的事件带 message_id，且该消息落库
    mid = events[-1].message_id
    msgs = await svc.get_messages(ses.id)
    assert any(m.id == mid and m.role == "assistant" and m.content for m in msgs)
    # tool_call 记录进消息
    assert any(m.tool_calls for m in msgs if m.role == "assistant")


async def test_context_symbol_in_answer(svc):
    _, events = await _run(svc, "这只股票怎么样", context={"symbol": "600519"})
    deltas = "".join(e.text for e in events if e.type == "assistant_delta")
    assert "600519" in deltas


async def test_error_event_on_failure(svc):
    events: list = []
    ses = await svc.create_session(None)

    async def on_event(e):
        events.append(e)

    with patch("lquant.agent.mock.fetch_quotes", side_effect=RuntimeError("boom")):
        await svc.send_message(ses.id, "600519 行情", on_event)
    assert events[-1].type == "error"
    # 错误也要落库为 assistant 消息，便于历史回放
    msgs = await svc.get_messages(ses.id)
    assert msgs[-1].role == "assistant"


async def test_cancel_leaves_marker_in_assistant_message(svc):
    """mock provider 取消也要留痕（与 claude_code 同口径）。"""
    import asyncio

    ses = await svc.create_session({"symbol": "600519"})

    async def on_event(_e):
        return None

    task = asyncio.ensure_future(svc.send_message(ses.id, "贵州茅台", on_event))
    # 等占位消息落库再取消：测的是语义而不是「谁先跑」
    for _ in range(200):
        if any(m.role == "assistant" for m in await svc.get_messages(ses.id)):
            break
        await asyncio.sleep(0.01)
    await svc.cancel(ses.id)
    with pytest.raises(asyncio.CancelledError):
        await task
    msgs = await svc.get_messages(ses.id)
    assistant = [m for m in msgs if m.role == "assistant"]
    assert assistant and "已中断" in assistant[-1].content
