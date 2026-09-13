"""ClaudeCodeAgentService：fake claude 脚本驱动的事件流/续聊/取消/超时。"""
from __future__ import annotations

import asyncio
import contextlib
import textwrap
from pathlib import Path

import pytest

from lquant.agent.claude_code import ClaudeCodeAgentService
from lquant.agent.errors import AgentError
from lquant.agent.sessions import SessionStore

_FAKE_CLAUDE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, sys, time

    argv = sys.argv[1:]
    if "--mode" in argv and "hang" in argv:
        time.sleep(60)

    is_resume = "--resume" in argv
    text = "第2轮续聊" if is_resume else "第1轮分析"
    sid = "fake-sid-1"
    assistant = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": text},
        {"type": "tool_use", "name": "get_quote", "input": {"symbols": ["600519"]}},
    ]}}
    user = {"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "收盘价 1700.0"}
    ]}}
    result = {"type": "result", "subtype": "success", "session_id": sid,
              "result": text}
    for line in (assistant, user, result):
        print(json.dumps(line, ensure_ascii=False), flush=True)
    """
)


@pytest.fixture()
def fake_script(tmp_path: Path) -> str:
    p = tmp_path / "fake_claude.py"
    p.write_text(_FAKE_CLAUDE, encoding="utf-8")
    p.chmod(0o755)
    return str(p)


def _svc(tmp_path, fake_script, **kw) -> ClaudeCodeAgentService:
    # fake 脚本带 shebang 直接作为可执行入口；claude_args 追加在命令末尾
    base = dict(claude_path=fake_script, claude_args=[],
                workspace_dir=str(tmp_path / "ws"), root=tmp_path,
                timeout_seconds=30)
    base.update(kw)
    return ClaudeCodeAgentService(SessionStore(str(tmp_path / "ask.db")), **base)


async def _run(svc, sid, content):
    events: list = []

    async def on_event(e):
        events.append(e)

    await svc.send_message(sid, content, on_event)
    return events


async def test_event_sequence_and_sid_persisted(tmp_path, fake_script):
    svc = _svc(tmp_path, fake_script)
    ses = await svc.create_session(None)
    events = await _run(svc, ses.id, "茅台怎么样")
    types = [e.type for e in events]
    assert "tool_call" in types and "tool_result" in types
    assert types[-1] == "done"
    deltas = "".join(e.text for e in events if e.type == "assistant_delta")
    assert "第1轮分析" in deltas
    # done 事件带 message_id，且该 assistant 消息内容落库
    mid = events[-1].message_id
    msgs = await svc.get_messages(ses.id)
    m = next(m for m in msgs if m.id == mid)
    assert m.role == "assistant" and "第1轮分析" in m.content
    # tool_result 事件带 summary
    tr = next(e for e in events if e.type == "tool_result")
    assert tr.summary
    # claude session id 落库，第二轮复用
    assert await svc.store.get_claude_session_id(ses.id) == "fake-sid-1"


async def test_second_round_resumes_session(tmp_path, fake_script):
    svc = _svc(tmp_path, fake_script)
    ses = await svc.create_session(None)
    await _run(svc, ses.id, "第一问")
    assert await svc.store.get_claude_session_id(ses.id) == "fake-sid-1"
    events = await _run(svc, ses.id, "第二问")
    deltas = "".join(e.text for e in events if e.type == "assistant_delta")
    assert "第2轮续聊" in deltas


async def test_cancel_interrupts(tmp_path, fake_script):
    svc = _svc(tmp_path, fake_script, claude_args=["--mode", "hang"])
    ses = await svc.create_session(None)
    events: list = []

    async def on_event(e):
        events.append(e)

    task = asyncio.ensure_future(svc.send_message(ses.id, "慢慢想", on_event))
    await asyncio.sleep(0.5)
    await svc.cancel(ses.id)
    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(task), timeout=10)
    assert any(e.type == "error" and "已中断" in e.message for e in events)


async def test_timeout_terminates(tmp_path, fake_script):
    svc = _svc(tmp_path, fake_script, timeout_seconds=1,
               claude_args=["--mode", "hang"])
    ses = await svc.create_session(None)
    events = await _run(svc, ses.id, "慢慢想")
    assert any(e.type == "error" and "执行超时" in e.message for e in events)
    # 不留空 assistant 消息：错误文本复用增量消息
    msgs = await svc.get_messages(ses.id)
    assert all(m.content for m in msgs if m.role == "assistant")


async def test_missing_cli_error(tmp_path):
    svc = _svc(tmp_path, "/nonexistent", claude_path="/nonexistent/claude")
    ses = await svc.create_session(None)
    events = await _run(svc, ses.id, "hi")
    assert any(e.type == "error" for e in events)
    msgs = await svc.get_messages(ses.id)
    assert msgs[-1].role == "assistant"


async def test_empty_message_rejected(tmp_path, fake_script):
    svc = _svc(tmp_path, fake_script)
    ses = await svc.create_session(None)
    with pytest.raises(AgentError) as exc:
        await svc.send_message(ses.id, "  ", lambda e: None)
    assert exc.value.status_code == 400

