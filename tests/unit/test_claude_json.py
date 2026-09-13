"""parse_stream_line 的单元测试。"""

import json

from lquant.agent.claude_json import parse_stream_line


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
        assert parse_stream_line(json.dumps({"type": "system", "subtype": "init"})) == []


class TestAssistant:
    def test_text_block_becomes_delta(self):
        line = json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "你好"}]},
            }
        )
        assert parse_stream_line(line) == [
            {"kind": "delta", "text": "你好", "name": "", "args": {}, "summary": "", "session_id": ""}
        ]

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
        assert events[1] == {
            "kind": "tool_call",
            "text": "",
            "name": "bash",
            "args": {"cmd": "ls"},
            "summary": "",
            "session_id": "",
        }

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
        assert parse_stream_line(line) == [
            {"kind": "tool_result", "text": "ok", "name": "", "args": {}, "summary": "ok", "session_id": ""}
        ]

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
            {"kind": "done", "text": "完成", "name": "", "args": {}, "summary": "", "session_id": "s-123"}
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
