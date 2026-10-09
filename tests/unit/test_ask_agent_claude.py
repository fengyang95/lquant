"""ClaudeCodeAgentService：fake claude 脚本驱动的事件流/续聊/取消/超时。"""
from __future__ import annotations

import asyncio
import contextlib
import json
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


async def test_cancel_leaves_marker_in_assistant_message(tmp_path, fake_script):
    """取消也要留痕（与超时路径同口径）：不留一条「有头无尾」的空 assistant 消息。"""
    svc = _svc(tmp_path, fake_script, claude_args=["--mode", "hang"])
    ses = await svc.create_session(None)

    async def on_event(_e):
        return None

    task = asyncio.ensure_future(svc.send_message(ses.id, "慢慢想", on_event))
    # 等到 assistant 占位消息落库再取消：占位消息在子进程起来后的 _consume 里建，
    # 固定 sleep(0.5) 在 xdist 并行、CPU 争抢时会跑输 —— 取消早于占位消息创建，
    # 取消路径就没地方写「已中断」，断言随机变红。
    for _ in range(200):
        if any(m.role == "assistant" for m in await svc.get_messages(ses.id)):
            break
        await asyncio.sleep(0.05)
    # 占位消息可见 ≠ 服务端协程已从 add_message 返回（aiosqlite 在 worker 线程
    # 里提交，提交可见先于原协程被唤醒）。再让出一次事件循环，等 _consume 进入
    # 带 CancelledError 处理的 try —— 否则取消落在 await 里，标记无处可写。
    await asyncio.sleep(0.1)
    await svc.cancel(ses.id)
    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(task), timeout=10)

    msgs = await svc.get_messages(ses.id)
    assistant = [m for m in msgs if m.role == "assistant"]
    assert assistant and all("已中断" in m.content for m in assistant)


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


async def test_second_concurrent_message_same_session_rejected(tmp_path, fake_script):
    """同会话单飞：运行时引用（task/子进程）按会话单槽存，放两条并发会互相踩。

    回归 D1：旧实现第二条直接覆盖 _tasks[sid]，/cancel 会打到错的那个任务，
    先结束的那条还会把 _procs[sid] 提前 pop 掉，留下孤儿进程。
    """
    svc = _svc(tmp_path, fake_script, claude_args=["--mode", "hang"])
    ses = await svc.create_session(None)

    async def on_event(_e):
        return None

    first = asyncio.ensure_future(svc.send_message(ses.id, "第一问", on_event))
    for _ in range(100):
        if svc.is_busy(ses.id):
            break
        await asyncio.sleep(0.02)
    assert svc.is_busy(ses.id)

    with pytest.raises(AgentError) as exc:
        await svc.send_message(ses.id, "第二问", on_event)
    assert exc.value.status_code == 409

    # 槽位归第一个任务所有，取消要打到它（不是被覆盖后的引用）
    owned = svc._tasks[ses.id]
    await svc.cancel(ses.id)
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(first, timeout=10)
    assert owned.done()
    assert svc._tasks == {} and svc._procs == {}


async def test_slot_released_after_completion(tmp_path, fake_script):
    """跑完要释放槽位，否则同一会话再也发不出第二条消息。"""
    svc = _svc(tmp_path, fake_script)
    ses = await svc.create_session(None)
    await _run(svc, ses.id, "第一问")
    assert not svc.is_busy(ses.id)
    events = await _run(svc, ses.id, "第二问")
    assert events[-1].type == "done"


async def test_attach_failure_reaps_spawned_child(tmp_path, fake_script, monkeypatch):
    """回归 D2：attach() 失败时子进程已经起来了，必须 terminate + 回收。

    旧实现只 raise，那个 claude 进程（全自主权限）会带着 stdout 管道一直跑下去。
    """
    from lquant.agent import claude_code as cc

    svc = _svc(tmp_path, fake_script, claude_args=["--mode", "hang"])
    ses = await svc.create_session(None)

    spawned: list = []
    real_init = cc._Child.__init__

    def spy_init(self, argv, cwd):  # noqa: ANN001
        real_init(self, argv, cwd)
        spawned.append(self)

    async def boom(_self):
        raise OSError("pipe 断了")

    monkeypatch.setattr(cc._Child, "__init__", spy_init)
    monkeypatch.setattr(cc._Child, "attach", boom)

    events = await _run(svc, ses.id, "hi")
    assert any(e.type == "error" for e in events)
    assert len(spawned) == 1
    # 已被 waitpid 回收（returncode 有值 ≠ None）→ 不是孤儿
    assert spawned[0].returncode is not None
    assert svc._procs == {}


# ---- 命令行形状：token 级流式开关 -------------------------------------------


def test_build_cmd_includes_partial_messages_flag(tmp_path, fake_script):
    svc = _svc(tmp_path, fake_script, partial_messages=True)
    cmd = svc._build_cmd("hi", None, tmp_path / "ws")
    assert "--include-partial-messages" in cmd
    # 与 stream-json 配套，别丢了输出格式
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"


def test_build_cmd_omits_partial_messages_flag_when_disabled(tmp_path, fake_script):
    """老版本 claude CLI 不认这个旗标，开关关掉时命令里不能出现。"""
    svc = _svc(tmp_path, fake_script, partial_messages=False)
    cmd = svc._build_cmd("hi", None, tmp_path / "ws")
    assert "--include-partial-messages" not in cmd


def test_line_parser_reuses_stream_state(tmp_path, fake_script):
    """回归：本轮解析器必须跨行复用，否则增量与 assistant 整块一起落库 → 正文翻倍。"""
    svc = _svc(tmp_path, fake_script)
    parse = svc._make_line_parser()
    start = json.dumps({"type": "stream_event", "event": {
        "type": "message_start", "message": {"id": "m1", "content": []}}})
    delta = json.dumps({"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "text_delta", "text": "你好"}}})
    block = json.dumps({"type": "assistant", "message": {
        "id": "m1", "content": [{"type": "text", "text": "你好"}]}})
    assert parse(start.encode()) == []
    assert [e["kind"] for e in parse(delta.encode())] == ["delta"]
    assert parse(block.encode()) == []  # 同 id 的整块被去重


_PARTIAL_CLAUDE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json

    # 实机取证（claude 2.1.263，--include-partial-messages）的形状：
    # message_start → content_block_delta ×N → assistant 整块 → result
    sid = "partial-sid"
    mid = "msg-partial-1"
    chunks = ["贵州", "茅台", "上涨"]
    print(json.dumps({"type": "system", "subtype": "init", "model": "m1",
                      "tools": ["Bash"], "session_id": sid}), flush=True)
    print(json.dumps({"type": "stream_event", "session_id": sid, "event": {
        "type": "message_start", "message": {"id": mid, "role": "assistant",
                                             "content": []}}}), flush=True)
    for c in chunks:
        print(json.dumps({"type": "stream_event", "session_id": sid, "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": c}}}), flush=True)
    # 整块会把同一段正文再给一遍：解析层必须按 message id 去重
    print(json.dumps({"type": "assistant", "message": {
        "id": mid, "content": [{"type": "text", "text": "".join(chunks)}]}}), flush=True)
    print(json.dumps({"type": "result", "subtype": "success", "session_id": sid,
                      "result": "".join(chunks)}), flush=True)
    """
)


@pytest.fixture()
def partial_script(tmp_path: Path) -> str:
    p = tmp_path / "fake_claude_partial.py"
    p.write_text(_PARTIAL_CLAUDE, encoding="utf-8")
    p.chmod(0o755)
    return str(p)


async def test_partial_messages_stream_once_end_to_end(tmp_path, partial_script):
    """端到端：token 级增量逐条外发，且**不**被 assistant 整块翻倍。

    这是「打字机效果」的验收点：既要有 N 条 assistant_delta（而不是 1 条整块），
    拼起来的正文又必须与整块逐字一致。
    """
    svc = _svc(tmp_path, partial_script, partial_messages=True)
    ses = await svc.create_session(None)
    events = await _run(svc, ses.id, "茅台怎么样")

    deltas = [e.text for e in events if e.type == "assistant_delta"]
    assert deltas == ["贵州", "茅台", "上涨"]  # 逐片到达，不是一整块
    assert "".join(deltas) == "贵州茅台上涨"  # 也没有翻倍
    # 落库内容 = 增量的拼接（事实源与流式一致）
    msgs = await svc.get_messages(ses.id)
    assert [m.content for m in msgs if m.role == "assistant"] == ["贵州茅台上涨"]
    assert events[-1].type == "done"


async def test_legacy_cli_without_stream_events_falls_back_to_whole_block(
        tmp_path, fake_script):
    """老 CLI（没有 stream_event）：正文仍以 assistant 整块下发一次，不丢也不翻倍。"""
    svc = _svc(tmp_path, fake_script, partial_messages=False)
    ses = await svc.create_session(None)
    events = await _run(svc, ses.id, "茅台怎么样")
    deltas = [e.text for e in events if e.type == "assistant_delta"]
    assert deltas == ["第1轮分析"]

