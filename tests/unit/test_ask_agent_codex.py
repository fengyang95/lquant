"""CodexAgentService：fake codex 脚本驱动的事件流/续聊/单飞/取消/超时。

fake 脚本的 JSONL 输出**照实机格式写**（codex-cli 0.159.3），
包括每次必现的「模型元数据缺失」非致命 item error。
"""
from __future__ import annotations

import asyncio
import contextlib
import textwrap
from pathlib import Path

import pytest

from lquant.agent.codex import CodexAgentService
from lquant.agent.errors import AgentError
from lquant.agent.sessions import SessionStore

_FAKE_CODEX = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, sys, time

    argv = sys.argv[1:]
    if "--mode" in argv and "hang" in argv:
        time.sleep(60)

    is_resume = "resume" in argv
    text = "第2轮续聊" if is_resume else "第1轮分析"
    tid = "fake-thread-1"

    def emit(obj):
        print(json.dumps(obj, ensure_ascii=False), flush=True)

    emit({"type": "thread.started", "thread_id": tid})
    emit({"type": "item.completed", "item": {"id": "item_0", "type": "error",
          "message": "Model metadata for `x` not found."}})
    emit({"type": "turn.started"})
    emit({"type": "item.completed", "item": {"id": "item_1", "type": "reasoning",
          "text": "先查行情再下结论"}})
    emit({"type": "item.started", "item": {"id": "item_2", "type": "mcp_tool_call",
          "server": "lquant", "tool": "get_daily",
          "arguments": {"symbol": "600519", "days": 3},
          "result": None, "error": None, "status": "in_progress"}})
    emit({"type": "item.completed", "item": {"id": "item_2", "type": "mcp_tool_call",
          "server": "lquant", "tool": "get_daily",
          "arguments": {"symbol": "600519", "days": 3},
          "result": {"content": [{"type": "text", "text": "收盘价 1700.0"}],
                     "structured_content": None},
          "error": None, "status": "completed"}})
    emit({"type": "item.started", "item": {"id": "item_3",
          "type": "command_execution",
          "command": "/bin/zsh -lc 'cat /Users/lyp/secret.txt'",
          "aggregated_output": "", "exit_code": None, "status": "in_progress"}})
    emit({"type": "item.completed", "item": {"id": "item_3",
          "type": "command_execution",
          "command": "/bin/zsh -lc 'cat /Users/lyp/secret.txt'",
          "aggregated_output": "hello", "exit_code": 0, "status": "completed"}})
    emit({"type": "item.completed", "item": {"id": "item_4",
          "type": "agent_message", "text": text}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}})
    """
)


@pytest.fixture()
def fake_script(tmp_path: Path) -> str:
    p = tmp_path / "fake_codex.py"
    p.write_text(_FAKE_CODEX, encoding="utf-8")
    p.chmod(0o755)
    return str(p)


def _svc(tmp_path, fake_script, **kw) -> CodexAgentService:
    base = dict(codex_path=fake_script, codex_args=[],
                workspace_dir=str(tmp_path / "ws"), root=tmp_path,
                timeout_seconds=30)
    base.update(kw)
    return CodexAgentService(SessionStore(str(tmp_path / "ask.db")), **base)


async def _run(svc, sid, content):
    events: list = []

    async def on_event(e):
        events.append(e)

    await svc.send_message(sid, content, on_event)
    return events


# ---- 命令行形状 ----------------------------------------------------------

def test_build_cmd_shape_without_resume(tmp_path, fake_script):
    svc = _svc(tmp_path, fake_script)
    cmd = svc._build_cmd("查茅台", None)
    assert cmd[1:3] == ["exec", "--json"]
    assert "--skip-git-repo-check" in cmd
    assert cmd[cmd.index("-C") + 1] == str(svc._workspace)
    # MCP 通过 -c 注入，且**不写**用户的 ~/.codex/config.toml
    joined = " ".join(cmd)
    assert "mcp_servers.lquant.command=" in joined
    assert "mcp_servers.lquant.args=" in joined
    assert "mcp_servers.lquant.env=" in joined
    # 无会话 id 时不应出现 resume 子命令，prompt 收尾
    assert "resume" not in cmd
    assert cmd[-1] == "查茅台"


def test_build_cmd_resume_places_session_id_before_prompt(tmp_path, fake_script):
    svc = _svc(tmp_path, fake_script)
    cmd = svc._build_cmd("第二问", "01a0f6fb-69dd-7753-b7b3-7f874a9370cf")
    i = cmd.index("resume")
    assert cmd[i + 1] == "01a0f6fb-69dd-7753-b7b3-7f874a9370cf"
    assert cmd[i + 2] == "第二问"


def test_build_cmd_skip_permissions_toggle(tmp_path, fake_script):
    on = _svc(tmp_path, fake_script, skip_permissions=True)._build_cmd("x", None)
    off = _svc(tmp_path, fake_script, skip_permissions=False)._build_cmd("x", None)
    # 审批不 bypass 时 MCP 工具会被直接拒（实测），故默认必须带上 bypass 旗标
    assert "--dangerously-bypass-approvals-and-sandbox" in on
    assert "--dangerously-bypass-approvals-and-sandbox" not in off
    assert off[off.index("-s") + 1] == "workspace-write"


# ---- 主链路 -------------------------------------------------------------

async def test_event_sequence_and_thread_id_persisted(tmp_path, fake_script):
    svc = _svc(tmp_path, fake_script)
    ses = await svc.create_session(None)
    events = await _run(svc, ses.id, "茅台怎么样")
    types = [e.type for e in events]

    # 推理过程透传
    assert any(e.type == "thinking" and "先查行情" in e.text for e in events)
    # MCP 与 shell 两类工具都进流
    calls = [e for e in events if e.type == "tool_call"]
    assert {c.name for c in calls} == {"lquant/get_daily", "shell"}
    assert any(c.args.get("symbol") == "600519" for c in calls)
    results = [e for e in events if e.type == "tool_result"]
    assert any(r.name == "lquant/get_daily" and "1700.0" in r.text for r in results)
    # 非致命 item error 不得把这一轮判死
    assert types[-1] == "done"
    deltas = "".join(e.text for e in events if e.type == "assistant_delta")
    assert "第1轮分析" in deltas
    # thread_id 落库（中性访问器）
    assert await svc.store.get_cli_session_id(ses.id) == "fake-thread-1"
    # 旧名仍可用（同一列）
    assert await svc.store.get_claude_session_id(ses.id) == "fake-thread-1"


async def test_non_fatal_item_error_does_not_fail_turn(tmp_path, fake_script):
    """每次调用都会发「模型元数据缺失」的 item error —— 必须降级为 system。"""
    svc = _svc(tmp_path, fake_script)
    ses = await svc.create_session(None)
    events = await _run(svc, ses.id, "hi")
    assert not any(e.type == "error" for e in events)
    notices = [e for e in events if e.type == "system"]
    assert notices and notices[0].data["level"] == "error"


async def test_second_round_resumes_session(tmp_path, fake_script):
    svc = _svc(tmp_path, fake_script)
    ses = await svc.create_session(None)
    await _run(svc, ses.id, "第一问")
    events = await _run(svc, ses.id, "第二问")
    deltas = "".join(e.text for e in events if e.type == "assistant_delta")
    assert "第2轮续聊" in deltas


async def test_workspace_has_agents_md_for_codex(tmp_path, fake_script):
    """codex 读 AGENTS.md（不是 CLAUDE.md），两个都写。"""
    svc = _svc(tmp_path, fake_script)
    assert (svc._workspace / "AGENTS.md").is_file()
    assert (svc._workspace / "CLAUDE.md").is_file()
    text = (svc._workspace / "AGENTS.md").read_text(encoding="utf-8")
    assert "数据访问优先级" in text


async def test_workspace_mcp_spec_matches_json_and_cli(tmp_path, fake_script):
    """claude 的 mcp.json 与 codex 的 -c 必须是同一份规格（防两套配置分叉）。"""
    import json

    svc = _svc(tmp_path, fake_script)
    spec = json.loads((svc._workspace / ".claude" / "mcp.json").read_text(
        encoding="utf-8"))["mcpServers"]["lquant"]
    joined = " ".join(svc._build_cmd("x", None))
    assert spec["command"] in joined
    assert spec["args"] == ["-m", "lquant.agent.mcp_server"]
    assert 'mcp_servers.lquant.args=["-m","lquant.agent.mcp_server"]' in joined


# ---- 取消 / 超时 / 异常 --------------------------------------------------

async def test_cancel_interrupts(tmp_path, fake_script):
    svc = _svc(tmp_path, fake_script, codex_args=["--mode", "hang"])
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
    msgs = await svc.get_messages(ses.id)
    assert all("已中断" in m.content for m in msgs if m.role == "assistant")


async def test_timeout_terminates(tmp_path, fake_script):
    svc = _svc(tmp_path, fake_script, timeout_seconds=1,
               codex_args=["--mode", "hang"])
    ses = await svc.create_session(None)
    events = await _run(svc, ses.id, "慢慢想")
    assert any(e.type == "error" and "执行超时" in e.message for e in events)
    msgs = await svc.get_messages(ses.id)
    assert all(m.content for m in msgs if m.role == "assistant")


async def test_missing_cli_error(tmp_path):
    svc = _svc(tmp_path, "/nonexistent", codex_path="/nonexistent/codex")
    ses = await svc.create_session(None)
    events = await _run(svc, ses.id, "hi")
    errs = [e for e in events if e.type == "error"]
    # 报错要能一眼看出是哪个 provider 的 CLI 起不来
    assert errs and "codex" in errs[0].message


async def test_empty_message_rejected(tmp_path, fake_script):
    svc = _svc(tmp_path, fake_script)
    ses = await svc.create_session(None)
    with pytest.raises(AgentError) as exc:
        await svc.send_message(ses.id, "  ", lambda e: None)
    assert exc.value.status_code == 400


async def test_second_concurrent_message_same_session_rejected(tmp_path, fake_script):
    """同会话单飞（与 claude provider 同一份实现）。"""
    svc = _svc(tmp_path, fake_script, codex_args=["--mode", "hang"])
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

    await svc.cancel(ses.id)
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(first, timeout=10)
    assert svc._tasks == {} and svc._procs == {}


async def test_attach_failure_reaps_spawned_child(tmp_path, fake_script, monkeypatch):
    """半启动的子进程必须回收（两个 provider 共用 spawn 模块，各自都要验）。"""
    from lquant.agent import spawn as spawn_mod

    svc = _svc(tmp_path, fake_script, codex_args=["--mode", "hang"])
    ses = await svc.create_session(None)

    spawned: list = []
    real_init = spawn_mod.SpawnedChild.__init__

    def spy_init(self, argv, cwd):  # noqa: ANN001
        real_init(self, argv, cwd)
        spawned.append(self)

    async def boom(_self):
        raise OSError("pipe 断了")

    monkeypatch.setattr(spawn_mod.SpawnedChild, "__init__", spy_init)
    monkeypatch.setattr(spawn_mod.SpawnedChild, "attach", boom)
    # 前提校验：patch 必须真的落在 spawn_child 会实例化的那个类上，否则本用例是空转
    assert spawn_mod.SpawnedChild.__init__ is spy_init

    events = await _run(svc, ses.id, "hi")
    assert any(e.type == "error" for e in events)
    assert len(spawned) == 1
    assert spawned[0].returncode is not None  # 已 waitpid 回收 ≠ 孤儿
    assert svc._procs == {}


# ---- 会话管理 / 终态失败 --------------------------------------------------

async def test_delete_session_cancels_and_removes(tmp_path, fake_script):
    svc = _svc(tmp_path, fake_script)
    ses = await svc.create_session(None)
    await svc.delete_session(ses.id)
    assert await svc.store.get(ses.id) is None


async def test_unknown_session_is_404(tmp_path, fake_script):
    svc = _svc(tmp_path, fake_script)
    with pytest.raises(AgentError) as exc:
        await svc.send_message("no-such-sid", "hi", lambda e: None)
    assert exc.value.status_code == 404


_FAILING_CODEX = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json

    def emit(obj):
        print(json.dumps(obj, ensure_ascii=False), flush=True)

    emit({"type": "thread.started", "thread_id": "fake-thread-fail"})
    emit({"type": "item.completed", "item": {"id": "item_1", "type": "reasoning",
          "text": "先看看行情"}})
    emit({"type": "turn.failed", "message": "模型调用被拒绝"})
    """
)


@pytest.fixture()
def failing_script(tmp_path: Path) -> str:
    p = tmp_path / "fake_codex_fail.py"
    p.write_text(_FAILING_CODEX, encoding="utf-8")
    p.chmod(0o755)
    return str(p)


async def test_turn_failed_emits_one_error_and_reuses_assistant_message(
        tmp_path, failing_script):
    """``turn.failed`` 是**真终态**（区别于非致命的 item error）：只发一次 error，
    错误文本复用那条增量 assistant 消息，不另起一条，也不再补一条「未正常收尾」。"""
    svc = _svc(tmp_path, failing_script)
    ses = await svc.create_session(None)
    events = await _run(svc, ses.id, "有问题吗")

    errs = [e for e in events if e.type == "error"]
    assert len(errs) == 1
    assert "模型调用被拒绝" in errs[0].message
    assert not any(e.type == "done" for e in events)

    assistant = [m for m in await svc.get_messages(ses.id)
                 if m.role == "assistant"]
    assert len(assistant) == 1
    assert "模型调用被拒绝" in assistant[0].content
