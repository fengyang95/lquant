"""``codex exec --json`` 解析层单测。

**所有 JSONL fixture 都是从实机抓的原始行**（codex-cli 0.159.3），
不是照文档造的 —— 字段名与嵌套形状必须与真实输出一致，
否则解析层会在真机上静默丢事件。
"""

from __future__ import annotations

import json

from lquant.agent.claude_json import parse_stream_line
from lquant.agent.codex_json import parse_codex_line


def _line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _kinds(events: list[dict]) -> list[str]:
    return [e["kind"] for e in events]


# ---- 线程 / 轮次 ---------------------------------------------------------

def test_thread_started_yields_session_event():
    """codex 在正文之前就给 thread_id（claude 是挂在 done 上）。"""
    raw = '{"type":"thread.started","thread_id":"01a0f6f5-7856-7cf3-aee4-7e86d7bc7d03"}'
    assert parse_codex_line(raw) == [{
        "kind": "session", "text": "", "name": "", "args": {}, "summary": "",
        "session_id": "01a0f6f5-7856-7cf3-aee4-7e86d7bc7d03",
        "tool_use_id": "", "data": {},
    }]


def test_turn_completed_is_done():
    raw = '{"type":"turn.completed","usage":{"input_tokens":8480,"output_tokens":16}}'
    assert _kinds(parse_codex_line(raw)) == ["done"]


def test_turn_failed_is_terminal_error_with_string_reason():
    raw = '{"type":"turn.failed","message":"stream disconnected"}'
    evs = parse_codex_line(raw)
    assert _kinds(evs) == ["error"]
    assert evs[0]["text"] == "stream disconnected"


def test_turn_failed_accepts_nested_error_object():
    raw = '{"type":"turn.failed","error":{"message":"rate limited"}}'
    assert parse_codex_line(raw)[0]["text"] == "rate limited"


def test_turn_failed_without_reason_has_fallback_text():
    """失败帧绝不能是空文本，否则落库会留一条「有头无尾」的记录。"""
    assert parse_codex_line('{"type":"turn.failed"}')[0]["text"]


def test_thread_failed_is_also_terminal_error():
    assert _kinds(parse_codex_line('{"type":"thread.failed","reason":"boom"}')) == ["error"]


# ---- item: 正文与推理 ----------------------------------------------------

def test_reasoning_becomes_thinking():
    raw = _line({"type": "item.completed",
                 "item": {"id": "item_1", "type": "reasoning",
                          "text": "The user wants the latest close for 600519."}})
    evs = parse_codex_line(raw)
    assert _kinds(evs) == ["thinking"]
    assert evs[0]["text"].startswith("The user wants")


def test_agent_message_becomes_delta():
    raw = _line({"type": "item.completed",
                 "item": {"id": "item_2", "type": "agent_message", "text": "收到"}})
    evs = parse_codex_line(raw)
    assert _kinds(evs) == ["delta"]
    assert evs[0]["text"] == "收到"


def test_multiple_agent_messages_append_in_order():
    """同一轮可以出现多个 agent_message item，按序追加才是正文。"""
    raws = [
        _line({"type": "item.completed",
               "item": {"id": "item_2", "type": "agent_message", "text": "我先列一下工具。"}}),
        _line({"type": "item.completed",
               "item": {"id": "item_6", "type": "agent_message", "text": "结果是 42。"}}),
    ]
    texts = [e["text"] for r in raws for e in parse_codex_line(r) if e["kind"] == "delta"]
    assert texts == ["我先列一下工具。", "结果是 42。"]


def test_empty_agent_message_emits_nothing():
    raw = _line({"type": "item.completed",
                 "item": {"id": "item_2", "type": "agent_message", "text": ""}})
    assert parse_codex_line(raw) == []


def test_item_started_for_agent_message_emits_nothing():
    raw = _line({"type": "item.started",
                 "item": {"id": "item_2", "type": "agent_message", "text": ""}})
    assert parse_codex_line(raw) == []


# ---- item: 终态失败 vs 非致命告警（关键区分）----------------------------

def test_item_error_is_non_fatal_system_not_error():
    """实测每次调用都会发「模型元数据缺失」的 item error，而该轮照常 completed。

    映射成契约里的 ``error`` 会把每一次调用都判死 —— 这是本模块最容易踩的坑。
    """
    raw = _line({"type": "item.completed", "item": {
        "id": "item_0", "type": "error",
        "message": "Model metadata for `deepseek-v4.1-flash` not found. "
                   "Defaulting to fallback metadata; this can degrade performance"}})
    evs = parse_codex_line(raw)
    assert _kinds(evs) == ["system"]
    assert evs[0]["data"]["level"] == "error"
    assert "Model metadata" in evs[0]["data"]["message"]


def test_item_error_then_turn_completed_still_reaches_done():
    """整条链路上「告警 + 正常收尾」必须能走通（回归护栏）。"""
    raws = [
        _line({"type": "item.completed", "item": {"id": "item_0", "type": "error",
                                                 "message": "warn"}}),
        _line({"type": "turn.completed", "usage": {}}),
    ]
    kinds = [e["kind"] for r in raws for e in parse_codex_line(r)]
    assert kinds == ["system", "done"]
    assert "error" not in kinds


# ---- item: shell 命令 ----------------------------------------------------

def test_command_execution_started_is_tool_call():
    raw = _line({"type": "item.started", "item": {
        "id": "item_2", "type": "command_execution",
        "command": "/bin/zsh -lc 'cat note.txt'",
        "aggregated_output": "", "exit_code": None, "status": "in_progress"}})
    evs = parse_codex_line(raw)
    assert _kinds(evs) == ["tool_call"]
    assert evs[0]["name"] == "shell"
    assert evs[0]["args"]["command"] == "/bin/zsh -lc 'cat note.txt'"


def test_command_execution_completed_is_tool_result():
    raw = _line({"type": "item.completed", "item": {
        "id": "item_2", "type": "command_execution",
        "command": "/bin/zsh -lc 'cat note.txt'",
        "aggregated_output": "hello lquant\n", "exit_code": 0, "status": "completed"}})
    evs = parse_codex_line(raw)
    assert _kinds(evs) == ["tool_result"]
    assert evs[0]["name"] == "shell"
    assert evs[0]["text"] == "hello lquant\n"
    assert evs[0]["summary"] == "hello lquant\n"


def test_command_execution_without_output_falls_back_to_exit_code():
    """无输出的命令不能产生空帧（前端/A2A 侧会看到一条没内容的工具结果）。"""
    raw = _line({"type": "item.completed", "item": {
        "id": "item_2", "type": "command_execution", "command": "true",
        "aggregated_output": "", "exit_code": 0, "status": "completed"}})
    evs = parse_codex_line(raw)
    assert evs[0]["text"]
    assert "exit_code=0" in evs[0]["text"]


def test_command_execution_summary_is_truncated():
    raw = _line({"type": "item.completed", "item": {
        "id": "item_2", "type": "command_execution", "command": "seq 1 500",
        "aggregated_output": "x" * 900, "exit_code": 0, "status": "completed"}})
    evs = parse_codex_line(raw)
    assert len(evs[0]["summary"]) == 200
    assert len(evs[0]["text"]) == 900  # 全文保留，只有 summary 截断


# ---- item: MCP 工具 -----------------------------------------------------

def test_mcp_tool_call_started_is_tool_call():
    raw = _line({"type": "item.started", "item": {
        "id": "item_3", "type": "mcp_tool_call", "server": "lquant",
        "tool": "get_daily", "arguments": {"symbol": "600519", "days": 5},
        "result": None, "error": None, "status": "in_progress"}})
    evs = parse_codex_line(raw)
    assert _kinds(evs) == ["tool_call"]
    assert evs[0]["name"] == "lquant/get_daily"
    assert evs[0]["args"] == {"symbol": "600519", "days": 5}


def test_mcp_tool_call_completed_joins_content_texts():
    raw = _line({"type": "item.completed", "item": {
        "id": "item_3", "type": "mcp_tool_call", "server": "lquant",
        "tool": "get_daily", "arguments": {"symbol": "600519"},
        "result": {"content": [{"type": "text", "text": "[]"}],
                   "structured_content": None},
        "error": None, "status": "completed"}})
    evs = parse_codex_line(raw)
    assert _kinds(evs) == ["tool_result"]
    assert evs[0]["name"] == "lquant/get_daily"
    assert evs[0]["text"] == "[]"


def test_mcp_tool_call_failure_surfaces_error_message():
    raw = _line({"type": "item.completed", "item": {
        "id": "item_3", "type": "mcp_tool_call", "server": "lquant",
        "tool": "get_quotes", "arguments": {"symbols": ["600519.SH"]},
        "result": None,
        "error": {"message": "MCP tool call requires approval, but approval policy is never"},
        "status": "failed"}})
    evs = parse_codex_line(raw)
    assert _kinds(evs) == ["tool_result"]
    assert "调用失败" in evs[0]["text"]
    assert "requires approval" in evs[0]["text"]


def test_mcp_tool_call_falls_back_to_json_when_no_text_parts():
    raw = _line({"type": "item.completed", "item": {
        "id": "item_3", "type": "mcp_tool_call", "server": "lquant",
        "tool": "get_daily", "arguments": {},
        "result": {"structured_content": {"rows": 3}}, "error": None,
        "status": "completed"}})
    assert "structured_content" in parse_codex_line(raw)[0]["text"]


# ---- 未做一阶映射的 item 与无关行 ----------------------------------------

def test_file_change_degrades_to_system_instead_of_being_dropped():
    raw = _line({"type": "item.completed", "item": {
        "id": "item_9", "type": "file_change",
        "changes": [{"path": "a.py", "kind": "update"}]}})
    evs = parse_codex_line(raw)
    assert _kinds(evs) == ["system"]
    assert evs[0]["data"]["item_type"] == "file_change"
    assert evs[0]["data"]["item"]["changes"][0]["path"] == "a.py"


def test_unknown_future_item_type_is_not_silently_dropped():
    raw = _line({"type": "item.completed",
                 "item": {"id": "item_9", "type": "quantum_thing", "text": "?"}})
    evs = parse_codex_line(raw)
    assert _kinds(evs) == ["system"]
    assert evs[0]["data"]["item_type"] == "quantum_thing"


def test_turn_started_and_item_updated_are_ignored():
    assert parse_codex_line('{"type":"turn.started"}') == []
    raw = _line({"type": "item.updated",
                 "item": {"id": "item_2", "type": "command_execution", "command": "x"}})
    assert parse_codex_line(raw) == []


def test_malformed_lines_are_ignored():
    for bad in ("", "   ", "not json", "[1,2,3]", '"a string"', "{}"):
        assert parse_codex_line(bad) == [], bad


def test_item_payload_that_is_not_a_dict_is_ignored():
    assert parse_codex_line(_line({"type": "item.completed", "item": None})) == []
    assert parse_codex_line('{"type":"item.completed"}') == []


def test_unmapped_item_type_only_reports_when_completed():
    """未做一阶映射的 item：started 不发帧，completed 降级成 system（不静默丢）。"""
    started = _line({"type": "item.started",
                     "item": {"id": "item_10", "type": "file_change",
                              "path": "a.py"}})
    completed = _line({"type": "item.completed",
                       "item": {"id": "item_10", "type": "file_change",
                                "path": "a.py"}})
    assert parse_codex_line(started) == []
    ev = parse_codex_line(completed)[0]
    assert ev["kind"] == "system"
    assert ev["data"]["item_type"] == "file_change"


def test_completed_mcp_call_without_result_or_message_is_empty_text():
    """error 里没有 message、result 又是 None → 结果文本空串（不能渲染出 "None"）。"""
    raw = _line({"type": "item.completed",
                 "item": {"id": "item_9", "type": "mcp_tool_call",
                          "server": "lquant", "tool": "get_daily",
                          "arguments": {}, "result": None,
                          "error": {"type": "timeout"}, "status": "failed"}})
    ev = parse_codex_line(raw)[0]
    assert ev["kind"] == "tool_result"
    assert ev["text"] == ""
    assert ev["summary"] == ""


# ---- 跨 provider 契约 ---------------------------------------------------

def test_event_shape_matches_claude_parser_contract():
    """两个 parser 必须产出**同一套键**，否则 _consume 会在某个 provider 上 KeyError。"""
    claude_raw = json.dumps({"type": "assistant", "message": {
        "content": [{"type": "text", "text": "hi"}]}})
    codex_raw = _line({"type": "item.completed",
                       "item": {"id": "item_1", "type": "agent_message", "text": "hi"}})
    claude_ev = parse_stream_line(claude_raw)[0]
    codex_ev = parse_codex_line(codex_raw)[0]
    assert set(claude_ev) == set(codex_ev)
    assert claude_ev["kind"] == codex_ev["kind"] == "delta"
