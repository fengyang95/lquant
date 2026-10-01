"""parse_stream_line 的单元测试。"""

import json

from lquant.agent.claude_json import parse_stream_line


def _ev(**kw) -> dict:
    """事件 dict 的完整默认形态（键集变动只需改这一处）。"""
    base = {"kind": "", "text": "", "name": "", "args": {}, "summary": "",
            "session_id": "", "tool_use_id": "", "data": {}}
    base.update(kw)
    return base


class TestBadLines:
    def test_non_json_returns_empty(self):
        assert parse_stream_line("not json") == []

    def test_blank_line_returns_empty(self):
        assert parse_stream_line("") == []
        assert parse_stream_line("   ") == []

    def test_non_dict_json_returns_empty(self):
        assert parse_stream_line("[1, 2, 3]") == []
        assert parse_stream_line('"hello"') == []

    def test_unknown_type_returns_empty(self):
        assert parse_stream_line(json.dumps({"type": "stream_event"})) == []


class TestAssistant:
    def test_text_block_becomes_delta(self):
        line = json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "你好"}]},
            }
        )
        assert parse_stream_line(line) == [_ev(kind="delta", text="你好")]

    def test_tool_use_block_becomes_tool_call(self):
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "查一下"},
                        {"type": "tool_use", "id": "t1", "name": "bash", "input": {"cmd": "ls"}},
                    ]
                },
            }
        )
        events = parse_stream_line(line)
        assert len(events) == 2
        assert events[0]["kind"] == "delta"
        assert events[1] == _ev(kind="tool_call", name="bash", args={"cmd": "ls"},
                                tool_use_id="t1")

    def test_thinking_block_becomes_thinking(self):
        line = json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "thinking", "thinking": "先查数据"}]},
            }
        )
        assert parse_stream_line(line) == [_ev(kind="thinking", text="先查数据")]

    def test_thinking_without_text_still_emitted(self):
        line = json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "thinking"}]}}
        )
        assert parse_stream_line(line) == [_ev(kind="thinking")]

    def test_unknown_block_type_ignored(self):
        line = json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "redacted_thinking"}]}}
        )
        assert parse_stream_line(line) == []

    def test_missing_message_returns_empty(self):
        assert parse_stream_line(json.dumps({"type": "assistant"})) == []


class TestUserToolResult:
    def test_string_content(self):
        line = json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "content": "ok"}],
                },
            }
        )
        assert parse_stream_line(line) == [_ev(kind="tool_result", text="ok", summary="ok")]

    def test_tool_use_id_carried_for_name_backfill(self):
        line = json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
                    ],
                },
            }
        )
        assert parse_stream_line(line)[0]["tool_use_id"] == "t1"

    def test_non_string_content_serialized(self):
        line = json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "content": {"a": 1}}],
                },
            }
        )
        events = parse_stream_line(line)
        assert events[0]["kind"] == "tool_result"
        assert json.loads(events[0]["text"]) == {"a": 1}

    def test_summary_truncated_to_200(self):
        long_text = "x" * 500
        line = json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "content": long_text}],
                },
            }
        )
        events = parse_stream_line(line)
        assert events[0]["text"] == long_text
        assert events[0]["summary"] == "x" * 200


class TestSystem:
    def test_init_keeps_whitelisted_fields_only(self):
        line = json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "cwd": "/Users/lyp/code/lquant",          # 保留：由出站层脱敏成 <path>
                "apiKeySource": "user",                   # 凭据来源：不回
                "session_id": "cli-s-1",                  # 内部会话：不回
                "uuid": "u-1",
                "model": "claude-sonnet-4-5",
                "permissionMode": "bypassPermissions",
                "tools": ["Bash", "Read"],
                "mcp_servers": [{"name": "lquant"}],
            }
        )
        assert parse_stream_line(line) == [
            _ev(kind="system", data={
                "subtype": "init",
                "model": "claude-sonnet-4-5",
                "permissionMode": "bypassPermissions",
                "cwd": "/Users/lyp/code/lquant",
                "tools": ["Bash", "Read"],
                "mcp_servers": [{"name": "lquant"}],
            })
        ]

    def test_system_without_whitelisted_field_is_dropped(self):
        assert parse_stream_line(json.dumps({"type": "system", "uuid": "u-1"})) == []

    def test_apikey_source_never_parsed(self):
        line = json.dumps({"type": "system", "subtype": "init",
                           "apiKeySource": "ANTHROPIC_API_KEY"})
        assert parse_stream_line(line) == [_ev(kind="system", data={"subtype": "init"})]


class TestResult:
    def test_success_becomes_done_with_session_id(self):
        line = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": "完成",
                "session_id": "s-123",
            }
        )
        assert parse_stream_line(line) == [
            _ev(kind="done", text="完成", session_id="s-123")
        ]

    def test_error_subtype_becomes_error(self):
        line = json.dumps(
            {
                "type": "result",
                "subtype": "error_max_turns",
                "result": "超限",
            }
        )
        assert parse_stream_line(line)[0]["kind"] == "error"
        assert parse_stream_line(line)[0]["text"] == "超限"

    def test_error_subtype_falls_back_to_error_field(self):
        line = json.dumps(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "error": "boom",
            }
        )
        events = parse_stream_line(line)
        assert events[0]["kind"] == "error"
        assert events[0]["text"] == "boom"

    def test_error_subtype_no_info_uses_default(self):
        line = json.dumps({"type": "result", "subtype": "error_during_execution"})
        events = parse_stream_line(line)
        assert events[0]["kind"] == "error"
        assert events[0]["text"] == "执行失败"


class TestMalformedBlocks:
    """形状不对的块要跳过而不是抛异常 —— 否则一行脏数据就能打断整条流。"""

    def test_non_dict_block_in_assistant_is_skipped(self):
        line = json.dumps({
            "type": "assistant",
            "message": {"content": ["裸字符串", {"type": "text", "text": "ok"}]},
        })
        events = parse_stream_line(line)
        assert [e["kind"] for e in events] == ["delta"]
        assert events[0]["text"] == "ok"

    def test_user_without_message_dict_returns_empty(self):
        assert parse_stream_line(json.dumps({"type": "user"})) == []
        assert parse_stream_line(json.dumps({"type": "user", "message": "x"})) == []

    def test_user_blocks_other_than_tool_result_are_skipped(self):
        line = json.dumps({
            "type": "user",
            "message": {"content": [
                {"type": "text", "text": "不该外发"},
                {"type": "tool_result", "tool_use_id": "t1", "content": "done"},
            ]},
        })
        events = parse_stream_line(line)
        assert len(events) == 1
        assert events[0]["kind"] == "tool_result"
        assert events[0]["text"] == "done"
