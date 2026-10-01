"""AgentEvent ⇄ A2A 帧映射：逐一断言每种事件的产物与顺序。"""
from __future__ import annotations

import json

import pytest

from lquant.agent.a2a.errors import INVALID_PARAMS, A2AError
from lquant.agent.a2a.mapper import (
    FrameBuilder,
    ask_message_to_a2a,
    prompt_from_message,
)
from lquant.agent.a2a.types import Message, Part, Role, TaskState
from lquant.agent.schemas import AgentEvent
from lquant.agent.schemas import Message as AskMessage


def _builder() -> FrameBuilder:
    return FrameBuilder("t1", "c1")


# ---- 入站 ----------------------------------------------------------------

def test_prompt_joins_and_strips_text_parts():
    msg = Message(messageId="m", role=Role.USER,
                  parts=[Part(text="  第一行  "), Part(text="第二行")])
    assert prompt_from_message(msg) == "第一行\n第二行"


@pytest.mark.parametrize("parts", [
    [Part(data={"k": 1})],
    [Part(text="   ")],
    [Part(text=""), Part(text="\n")],
])
def test_prompt_without_usable_text_is_invalid_params(parts):
    msg = Message(messageId="m", role=Role.USER, parts=parts)
    with pytest.raises(A2AError) as exc:
        prompt_from_message(msg)
    assert exc.value.code == INVALID_PARAMS


# ---- 出站：状态与正文 ------------------------------------------------------

def test_working_frame():
    frame = _builder().working()
    ev = frame["statusUpdate"]
    assert ev["status"]["state"] == TaskState.WORKING
    assert ev["final"] is False
    assert ev["taskId"] == "t1" and ev["contextId"] == "c1"


def test_delta_produces_append_artifact_chunk():
    b = _builder()
    frames = b.on_event(AgentEvent(type="assistant_delta", text="你好"))
    assert len(frames) == 1
    up = frames[0]["artifactUpdate"]
    assert up["artifact"]["parts"] == [{"text": "你好"}]
    assert up["append"] is True and up["lastChunk"] is False
    assert up["artifact"]["artifactId"] == "t1-answer"
    assert b.text == "你好"
    assert not b.terminal


def test_empty_delta_is_dropped_and_not_accumulated():
    b = _builder()
    assert b.on_event(AgentEvent(type="assistant_delta", text="")) == []
    assert b.text == ""


# ---- 出站：过程数据 --------------------------------------------------------

def test_thinking_goes_to_trace_artifact():
    frames = _builder().on_event(AgentEvent(type="thinking", text="先查数据"))
    assert len(frames) == 1
    up = frames[0]["artifactUpdate"]
    assert up["artifact"]["artifactId"] == "t1-trace"
    assert up["artifact"]["name"] == "trace"
    assert up["artifact"]["parts"] == [{"text": "先查数据"}]
    assert up["metadata"] == {"kind": "thinking"}
    assert up["append"] is True and up["lastChunk"] is False


def test_system_event_serialized_and_redacted():
    frames = _builder().on_event(AgentEvent(type="system", data={
        "subtype": "init", "model": "claude-sonnet-4-5", "cwd": "/Users/lyp/x"}))
    up = frames[0]["artifactUpdate"]
    assert up["metadata"] == {"kind": "system"}
    assert json.loads(up["artifact"]["parts"][0]["text"]) == {
        "subtype": "init", "model": "claude-sonnet-4-5", "cwd": "<path>"}


def test_tool_call_keeps_name_only_in_status_but_full_args_in_trace():
    frames = _builder().on_event(AgentEvent(
        type="tool_call", name="get_quotes", args={"symbols": ["600519"]}))
    assert [list(f) for f in frames] == [["statusUpdate"], ["artifactUpdate"]]

    status = frames[0]["statusUpdate"]
    assert status["status"]["state"] == TaskState.WORKING
    assert status["status"]["message"]["role"] == Role.AGENT
    assert "get_quotes" in status["status"]["message"]["parts"][0]["text"]
    assert status["metadata"] == {"tool": "get_quotes"}

    trace = frames[1]["artifactUpdate"]
    assert trace["metadata"] == {"kind": "tool_call", "name": "get_quotes"}
    assert json.loads(trace["artifact"]["parts"][0]["text"]) == {"symbols": ["600519"]}


def test_tool_call_args_redacted_in_trace():
    frames = _builder().on_event(AgentEvent(
        type="tool_call", name="bash",
        args={"script": "/Users/lyp/run.py", "auth_token": "sk-abcdefghijkl"}))
    payload = json.loads(frames[1]["artifactUpdate"]["artifact"]["parts"][0]["text"])
    assert payload == {"script": "<path>", "auth_token": "***"}
    assert "sk-abcdefghijkl" not in str(frames)


def test_tool_result_status_summary_truncated_and_full_text_in_trace():
    full = "完整工具输出" * 100
    frames = _builder().on_event(AgentEvent(
        type="tool_result", name="get_daily", text=full, summary="摘要"))

    status = frames[0]["statusUpdate"]
    assert status["metadata"] == {"toolResult": "摘要"}
    assert "message" not in status["status"]

    trace = frames[1]["artifactUpdate"]
    assert trace["metadata"] == {"kind": "tool_result", "name": "get_daily"}
    assert trace["artifact"]["parts"][0]["text"] == full


def test_tool_result_without_text_falls_back_to_summary():
    frames = _builder().on_event(AgentEvent(
        type="tool_result", name="t", text="", summary="摘要"))
    assert frames[1]["artifactUpdate"]["artifact"]["parts"][0]["text"] == "摘要"


def test_trace_text_truncated_over_limit():
    frames = _builder().on_event(AgentEvent(
        type="tool_result", name="big", text="a" * 70000))
    up = frames[1]["artifactUpdate"]
    assert len(up["artifact"]["parts"][0]["text"]) == 65536
    assert up["metadata"]["truncated"] is True


def test_trace_emitted_flag_tracks_process_frames():
    b = _builder()
    assert b.trace_emitted is False
    b.on_event(AgentEvent(type="thinking", text="想"))
    assert b.trace_emitted is True
    b.on_event(AgentEvent(type="done"))
    assert b.trace_emitted is True


# ---- 出站：收尾 -----------------------------------------------------------

def test_done_emits_last_chunk_then_completed():
    b = _builder()
    b.on_event(AgentEvent(type="assistant_delta", text="答案"))
    frames = b.on_event(AgentEvent(type="done", message_id="m2"))
    assert len(frames) == 2
    assert frames[0]["artifactUpdate"]["lastChunk"] is True
    assert frames[0]["artifactUpdate"]["artifact"]["parts"] == [{"text": ""}]
    assert frames[1]["statusUpdate"]["status"]["state"] == TaskState.COMPLETED
    assert frames[1]["statusUpdate"]["final"] is True
    assert b.state == TaskState.COMPLETED and b.terminal


def test_done_without_any_output_skips_empty_artifact():
    frames = _builder().on_event(AgentEvent(type="done"))
    assert len(frames) == 1
    assert frames[0]["statusUpdate"]["status"]["state"] == TaskState.COMPLETED


def test_done_closes_trace_then_answer_then_status():
    b = _builder()
    b.on_event(AgentEvent(type="thinking", text="想"))
    b.on_event(AgentEvent(type="assistant_delta", text="答案"))
    frames = b.on_event(AgentEvent(type="done"))
    assert [list(f) for f in frames] == [
        ["artifactUpdate"], ["artifactUpdate"], ["statusUpdate"]]
    assert frames[0]["artifactUpdate"]["metadata"] == {"kind": "trace_end"}
    assert frames[0]["artifactUpdate"]["lastChunk"] is True
    assert frames[1]["artifactUpdate"]["artifact"]["artifactId"] == "t1-answer"
    assert frames[1]["artifactUpdate"]["lastChunk"] is True
    assert frames[2]["statusUpdate"]["status"]["state"] == TaskState.COMPLETED


def test_done_with_trace_but_no_answer_text():
    b = _builder()
    b.on_event(AgentEvent(type="thinking", text="想"))
    frames = b.on_event(AgentEvent(type="done"))
    assert [list(f) for f in frames] == [["artifactUpdate"], ["statusUpdate"]]


def test_error_maps_to_failed_with_message():
    b = _builder()
    b.on_event(AgentEvent(type="assistant_delta", text="半截"))
    frames = b.on_event(AgentEvent(type="error", message="执行超时"))
    assert [list(f) for f in frames] == [["artifactUpdate"], ["statusUpdate"]]
    assert frames[-1]["statusUpdate"]["status"]["state"] == TaskState.FAILED
    assert frames[-1]["statusUpdate"]["status"]["message"]["parts"] == [{"text": "执行超时"}]
    assert frames[-1]["statusUpdate"]["final"] is True


def test_error_closes_open_trace():
    b = _builder()
    b.on_event(AgentEvent(type="thinking", text="想"))
    frames = b.on_event(AgentEvent(type="error", message="炸了"))
    assert [list(f) for f in frames] == [["artifactUpdate"], ["statusUpdate"]]
    assert frames[0]["artifactUpdate"]["metadata"] == {"kind": "trace_end"}


def test_error_after_mark_canceled_maps_to_canceled():
    b = _builder()
    b.mark_canceled()
    frames = b.on_event(AgentEvent(type="error", message="已中断"))
    assert frames[-1]["statusUpdate"]["status"]["state"] == TaskState.CANCELED


def test_unknown_event_type_produces_nothing():
    assert _builder().on_event(AgentEvent(type="whatever")) == []


# ---- 落库消息 → A2A Message（非流式回放）----------------------------------

def test_ask_message_to_a2a_only_text_parts():
    m = AskMessage(id="mid", session_id="s1", role="assistant", content="结果",
                   tool_calls=[{"name": "get_quotes", "args": {"symbols": ["600519"]}}])
    out = ask_message_to_a2a(m, context_id="c1", task_id="t1").to_a2a()
    assert out["messageId"] == "mid"
    assert out["role"] == Role.AGENT
    assert out["parts"] == [{"text": "结果"}]
    assert out["contextId"] == "c1" and out["taskId"] == "t1"
    # history 仍只回文本（过程数据只走流式 trace artifact）
    assert "tool_calls" not in str(out)
    assert "600519" not in str(out)


def test_ask_message_user_role():
    m = AskMessage(id="u", session_id="s1", role="user", content="问题")
    assert ask_message_to_a2a(m, context_id="c1").to_a2a()["role"] == Role.USER
