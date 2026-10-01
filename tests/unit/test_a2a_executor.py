"""A2A 执行体：全链路（fake claude 脚本）、共用会话事实源、取消、busy 守卫。"""
from __future__ import annotations

import asyncio
import contextlib
import textwrap
from pathlib import Path

import pytest

from lquant.agent.a2a.errors import (
    INVALID_PARAMS,
    TASK_NOT_CANCELABLE,
    A2AError,
    SessionBusyError,
)
from lquant.agent.a2a.executor import A2AExecutor
from lquant.agent.a2a.types import Message as A2AMessage
from lquant.agent.a2a.types import Part, Role, TaskState
from lquant.agent.claude_code import ClaudeCodeAgentService
from lquant.agent.sessions import SessionStore

_FAKE_CLAUDE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, sys, time

    argv = sys.argv[1:]
    if "hang" in argv:
        time.sleep(60)
    if "fail" in argv:
        sys.exit(3)
    prompt = argv[argv.index("-p") + 1] if "-p" in argv else ""
    prefix = "续聊：" if "--resume" in argv else ""
    print(json.dumps({"type": "system", "subtype": "init", "model": "fake-model",
                      "cwd": "/Users/lyp/work", "session_id": "fake-sid"},
                     ensure_ascii=False), flush=True)
    for idx, piece in enumerate(("第一段 ", "第二段")):
        blocks = [{"type": "text", "text": piece},
                  {"type": "tool_use", "name": "get_quotes",
                   "input": {"symbols": ["600519"]}}]
        if idx == 0:
            blocks.insert(0, {"type": "thinking", "thinking": "先查行情再下结论"})
        print(json.dumps({"type": "assistant", "message": {"content": blocks}},
                         ensure_ascii=False), flush=True)
        print(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "收盘价 1700.0"}]}},
            ensure_ascii=False), flush=True)
    print(json.dumps({"type": "result", "subtype": "success",
                      "session_id": "fake-sid", "result": prefix + prompt},
                     ensure_ascii=False), flush=True)
    """
)

_ANSWER = "第一段 第二段"


def _msg(text: str = "茅台怎么样", mid: str = "m1") -> A2AMessage:
    return A2AMessage(messageId=mid, role=Role.USER, parts=[Part(text=text)])


def _mode(ex: A2AExecutor, mode: str) -> None:
    """给 fake claude 脚本追加一个模式开关（hang / fail）。"""
    ex.service._claude_args = [mode]


@pytest.fixture()
def fake_script(tmp_path: Path) -> str:
    p = tmp_path / "fake_claude.py"
    p.write_text(_FAKE_CLAUDE, encoding="utf-8")
    p.chmod(0o755)
    return str(p)


@pytest.fixture()
async def env(tmp_path, fake_script):
    store = SessionStore(str(tmp_path / "ask.db"))
    svc = ClaudeCodeAgentService(
        store, claude_path=fake_script, claude_args=[],
        workspace_dir=str(tmp_path / "ws"), root=tmp_path, timeout_seconds=30)
    yield svc, A2AExecutor(svc), store
    await store.close()


# ---- 正常链路 ------------------------------------------------------------

async def test_drives_session_by_its_locked_provider(env):
    """A2A 驱动 /ask 建的会话时，必须按它**锁定的** provider 路由。

    回归：执行器固定用注入的全局默认实例，于是 claude 会去答一个锁定到 codex
    的会话，并把 claude 的 session_id 写进 ``ask_sessions.claude_session_id``
    —— 下一轮 /ask 路由回 codex 时就会拿这个 id 去 resume 别人的线程；
    单飞也会被劈成两半（槽位按 service 实例存，两个实例各看各的）。
    """
    svc, ex, store = env
    assert svc.provider == "claude_code"
    ses = await store.create({"source": "ask"}, {"provider": "mock"})

    task = await ex.send(_msg("今天大盘怎么样"), ses.id)

    assert task.status.state == TaskState.COMPLETED
    answer = task.artifacts[0].parts[0].text
    assert "provider=mock" in answer          # 由 mock 作答，不是 claude
    assert await store.get_cli_session_id(ses.id) is None  # claude 没写串会话 id


async def test_unlocked_session_uses_injected_service(env):
    """未锁定 provider 的会话（A2A 自建 / 老会话）仍走注入的那个实例。"""
    svc, ex, store = env
    task = await ex.send(_msg("茅台怎么样"))
    assert task.status.state == TaskState.COMPLETED
    assert await store.get_cli_session_id(task.context_id) == "fake-sid"


async def test_send_returns_completed_task(env):
    svc, ex, store = env
    task = await ex.send(_msg())

    assert task.status.state == TaskState.COMPLETED
    assert task.artifacts and task.artifacts[0].parts[0].text == _ANSWER
    assert task.artifacts[0].artifact_id == f"{task.id}-answer"
    # history 只回文本 part，且 user/agent 各一条
    roles = [m.role for m in task.history]
    assert roles == [Role.USER, Role.AGENT]
    assert task.history[0].parts[0].text == "茅台怎么样"
    # runner 与运行时引用都收敛干净
    assert ex._runs == {} and svc._tasks == {}


async def test_send_shares_session_fact_source(env):
    """A2A 提问要能在 /ask 侧看到（同一个 ask.db、同一条 ask_sessions 行）。"""
    svc, ex, store = env
    task = await ex.send(_msg())

    ses = await store.get(task.context_id)
    assert ses is not None
    assert ses.context.get("source") == "a2a"
    assert [m.role for m in await store.messages(task.context_id)] == ["user", "assistant"]
    assert [s.id for s in await svc.list_sessions()] == [task.context_id]


async def test_reusing_context_id_keeps_same_session(env):
    svc, ex, store = env
    first = await ex.send(_msg(mid="m1"))
    second = await ex.send(_msg("再问一次", mid="m2"), first.context_id)

    assert second.context_id == first.context_id
    assert len(await svc.list_sessions()) == 1
    assert len(await store.messages(first.context_id)) == 4


async def test_open_stream_frame_sequence(env):
    _svc, ex, _store = env
    frames = [f async for f in await ex.open_stream(_msg())]

    # 规范：流必须以 Task 对象开头
    assert "task" in frames[0]
    assert frames[0]["task"]["status"]["state"] == TaskState.SUBMITTED
    assert frames[1]["statusUpdate"]["status"]["state"] == TaskState.WORKING

    kinds = [next(iter(f)) for f in frames]
    assert kinds[0] == "task" and kinds[-1] == "statusUpdate"
    assert "artifactUpdate" in kinds

    # 回答正文 = `-answer` artifact 的增量按序拼接；末帧为终态且 final=True
    def _chunks(suffix: str) -> list[str]:
        return [f["artifactUpdate"]["artifact"]["parts"][0]["text"]
                for f in frames if "artifactUpdate" in f
                and f["artifactUpdate"]["artifact"]["artifactId"].endswith(suffix)]

    assert "".join(_chunks("-answer")) == _ANSWER
    assert frames[-1]["statusUpdate"]["status"]["state"] == TaskState.COMPLETED
    assert frames[-1]["statusUpdate"]["final"] is True

    # 过程数据挂在独立的 -trace artifact 上：thinking 全文 + system 白名单字段
    trace = "".join(_chunks("-trace"))
    assert "先查行情再下结论" in trace
    assert "fake-model" in trace
    assert "/Users/lyp/work" not in trace and "<path>" in trace   # 本机路径已脱敏

    # statusUpdate 的进度帧仍只带工具名（入参在 trace 帧里，不在这条通道）
    tool_frames = [f for f in frames
                   if f.get("statusUpdate", {}).get("metadata", {}).get("tool")]
    assert tool_frames and all(
        "600519" not in str(f["statusUpdate"]) for f in tool_frames)


async def test_sink_receives_events(env):
    _svc, ex, _store = env
    seen: list[tuple[str, str]] = []

    async def sink(sid: str, event) -> None:
        seen.append((sid, event.type))

    ex.set_sink(sink)
    task = await ex.send(_msg())
    assert {t for _sid, t in seen} >= {"assistant_delta", "tool_call", "done"}
    assert all(sid == task.context_id for sid, _t in seen)


async def test_history_length_controls_echo(env):
    _svc, ex, _store = env
    task = await ex.send(_msg())
    bare = await ex.get(task.id, history_length=0)
    assert bare.history is None
    assert bare.artifacts  # 产物不受 historyLength 影响
    assert bare.status.state == TaskState.COMPLETED


# ---- 失败与取消 ----------------------------------------------------------

async def test_failure_maps_to_failed_state(env):
    _svc, ex, _store = env
    _mode(ex, "fail")
    task = await ex.send(_msg())

    assert task.status.state == TaskState.FAILED
    assert task.artifacts  # 错误文本复用 assistant 消息，作为产物回给调用方


async def test_cancel_maps_to_canceled(env):
    _svc, ex, _store = env
    _mode(ex, "hang")
    run = await ex.start(_msg())
    task = await ex.cancel(run.task_id)

    assert task.status.state == TaskState.CANCELED
    assert ex._runs == {}
    # 已中断也要留痕：不留一条空 assistant 消息
    msgs = await ex.service.store.messages(run.context_id)
    assert all(m.content for m in msgs if m.role == "assistant")


async def test_cancel_terminal_task_rejected(env):
    _svc, ex, _store = env
    task = await ex.send(_msg())
    with pytest.raises(A2AError) as exc:
        await ex.cancel(task.id)
    assert exc.value.code == TASK_NOT_CANCELABLE


async def test_cancel_unknown_task_rejected(env):
    _svc, ex, _store = env
    with pytest.raises(A2AError):
        await ex.cancel("nope")


async def test_cancel_without_live_run_still_converges(env):
    """进程重启后内存里没有 run：只把落库状态收敛到 CANCELED（不报错）。"""
    _svc, ex, store = env
    _mode(ex, "hang")
    run = await ex.start(_msg())
    # 模拟「run 已不在内存」（换掉 executor 的注册表，但 store 里的行还在）
    ex._runs.clear()
    task = await ex.cancel(run.task_id)
    assert task.status.state == TaskState.CANCELED
    # 收尾仍在跑的那个进程与 runner，别留孤儿
    with contextlib.suppress(Exception):
        await ex.service.cancel(run.context_id)
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await asyncio.wait_for(run.task, 10)
    assert await store.get_a2a_task(run.task_id) is not None


# ---- 守卫 ----------------------------------------------------------------

async def test_same_context_concurrent_message_rejected(env):
    """同一 contextId 同时只允许一个回答在跑（单飞）。

    ``start()`` 里的 is_busy 是**快路径**，权威守卫在 AgentService 的 claim
    （槽位由真正干活的 task 占）。所以要先等 pump 拿到槽位，断言才是确定的；
    真出现竞态时的兜底是「第二个任务以 FAILED 收场」，不会两条并行。
    """
    svc, ex, _store = env
    _mode(ex, "hang")
    run = await ex.start(_msg(mid="m1"))
    for _ in range(100):
        if svc.is_busy(run.context_id):
            break
        await asyncio.sleep(0.02)
    assert svc.is_busy(run.context_id), "pump 应在启动后很快占住执行槽位"

    with pytest.raises(SessionBusyError):
        await ex.start(_msg(mid="m2"), run.context_id)
    await ex.cancel(run.task_id)


async def test_unknown_context_id_rejected(env):
    """contextId 指向不存在的会话 → -32602（参数值非法，不是请求体非法）。"""
    _svc, ex, store = env
    with pytest.raises(A2AError) as exc:
        await ex.start(_msg(), "no-such-context")
    assert exc.value.code == INVALID_PARAMS
    assert await store.list_a2a_tasks("no-such-context") == []


async def test_empty_text_message_rejected_before_any_side_effect(env):
    _svc, ex, store = env
    blank = A2AMessage(messageId="m", role=Role.USER, parts=[Part(text="   ")])
    with pytest.raises(A2AError) as exc:
        await ex.start(blank)
    assert exc.value.code == -32602
    assert await store.list_a2a_tasks("") == []
    assert await ex.service.list_sessions() == []
