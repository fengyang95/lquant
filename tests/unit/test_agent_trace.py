"""工具调用留痕：as_of/degraded 提取 + FIFO 配对 + 留痕失败不影响对话。"""
from __future__ import annotations

from lquant.agent.schemas import AgentEvent
from lquant.agent.trace import MAX_DETAIL, TracedEmitter, extract_meta, truncate


class _Sink:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def __call__(self, row: dict) -> None:
        self.rows.append(row)


class _BoomSink:
    async def __call__(self, row: dict) -> None:
        raise RuntimeError("db down")


def _emitter(sink, emitted=None, msg_id="m1"):
    async def emit(ev):
        if emitted is not None:
            emitted.append(ev)

    return TracedEmitter("sid-1", "run-1", emit, sink, msg_id=lambda: msg_id)


# ---- extract_meta ----

def test_extract_meta_as_of_from_explicit_and_trade_date():
    assert extract_meta('{"as_of": "2026-10-08"}') == ("2026-10-08", None)
    assert extract_meta('{"trade_date": "2026-10-06"}') == ("2026-10-06", None)
    # data 一层也要看
    assert extract_meta('{"data": {"trade_date": "2026-10-06"}}')[0] == "2026-10-06"
    # 纯日期串
    assert extract_meta("2026-10-08") == ("2026-10-08", None)
    assert extract_meta("2026-10-08 09:30:00") == ("", None)


def test_extract_meta_degraded_only_when_explicit():
    """未知 ≠ 没降级：读不到就别填 False。"""
    assert extract_meta('{"degraded": true}') == ("", True)
    assert extract_meta('{"unavailable": true}') == ("", True)
    assert extract_meta('{"data": {}, "unavailable": false}') == ("", False)
    assert extract_meta('{"rows": []}') == ("", None)
    assert extract_meta('{"unavailable": "yes"}') == ("", None)   # 非布尔 → 未知
    assert extract_meta("") == ("", None)
    assert extract_meta("{bad json") == ("", None)


def test_truncate_marks_original_length():
    assert truncate("abc", 10) == "abc"
    out = truncate("x" * (MAX_DETAIL + 5))
    assert out.endswith(f"…（已截断，原长 {MAX_DETAIL + 5} 字）")
    assert truncate(None) == ""


# ---- TracedEmitter ----

async def test_pair_call_and_result():
    sink = _Sink()
    em = _emitter(sink)
    await em(AgentEvent(type="assistant_delta", text="先算算"))
    await em(AgentEvent(type="tool_call", name="get_daily", args={"symbol": "600519"}))
    await em(AgentEvent(type="tool_result", name="get_daily", text='{"as_of": "2026-10-08"}',
                        summary="60 根日线"))
    assert len(sink.rows) == 2
    first, second = sink.rows
    assert first["status"] == "running" and first["seq"] == 1
    assert '"symbol"' in first["args_json"] and first["message_id"] == "m1"
    assert second["status"] == "ok" and second["seq"] == 1
    assert second["summary"] == "60 根日线"
    assert second["as_of"] == "2026-10-08"
    assert second["duration_ms"] is not None
    assert second["finished_at"] and second["started_at"]


async def test_result_without_call_is_recorded():
    """只有结果没有调用（CLI 输出被截断）也不丢过程。"""
    sink = _Sink()
    await _emitter(sink)(AgentEvent(type="tool_result", name="shell", text="ls"))
    assert len(sink.rows) == 1
    row = sink.rows[0]
    assert row["name"] == "shell" and row["status"] == "ok" and row["args_json"] == "{}"


async def test_pairs_by_name_then_fifo():
    """名字对得上优先配对；名字都空时按 FIFO。"""
    sink = _Sink()
    em = _emitter(sink)
    await em(AgentEvent(type="tool_call", name="a"))
    await em(AgentEvent(type="tool_call", name="b"))
    await em(AgentEvent(type="tool_result", name="b", text="1"))
    rows = [r for r in sink.rows if r["status"] == "ok"]
    assert rows and rows[0]["name"] == "b"
    # 剩下的 a 还没闭合；再来一个无名结果按 FIFO 落在 a 上
    await em(AgentEvent(type="tool_result", name="", text="2"))
    done = [r for r in sink.rows if r["status"] == "ok"]
    assert done[-1]["name"] == "a"


async def test_error_result_sets_status_and_error():
    sink = _Sink()
    em = _emitter(sink)
    await em(AgentEvent(type="tool_call", name="get_quotes", args={}))
    await em(AgentEvent(type="tool_result", name="get_quotes", text="boom",
                        data={"is_error": True}))
    row = sink.rows[-1]
    assert row["status"] == "error" and row["error"] == "boom"


async def test_sink_failure_does_not_break_conversation():
    """留痕写库失败必须被吞掉，事件照常下发。"""
    emitted: list = []
    em = _emitter(_BoomSink(), emitted=emitted)
    await em(AgentEvent(type="tool_call", name="x"))
    await em(AgentEvent(type="tool_result", name="x", text="y"))
    assert [e.type for e in emitted] == ["tool_call", "tool_result"]


async def test_non_tool_events_pass_through_untouched():
    sink = _Sink()
    emitted: list = []
    em = _emitter(sink, emitted=emitted)
    await em(AgentEvent(type="thinking", text="嗯"))
    assert sink.rows == [] and len(emitted) == 1


# ---- 端到端：mock provider 跑一轮，留痕真的落库 ----

async def test_mock_run_persists_trace_rows(tmp_path, monkeypatch):
    from lquant.agent import mock as mock_mod
    from lquant.agent.mock import MockAgentService
    from lquant.agent.sessions import SessionStore

    monkeypatch.setattr(mock_mod, "fetch_quotes", lambda symbols, **kw: [
        {"symbol": s, "name": "测试股", "price": 11.0, "change_pct": 0.0,
         "ts": "2026-10-08 15:00:00"} for s in symbols])
    svc = MockAgentService(SessionStore(str(tmp_path / "ask.db")))
    ses = await svc.create_session({"symbol": "600519"})

    async def on_event(_e):
        return None

    await svc.send_message(ses.id, "贵州茅台", on_event)
    rows = await svc.store.list_tool_trace(ses.id)
    assert [r["status"] for r in rows] == ["running", "ok"]
    assert rows[0]["seq"] == rows[1]["seq"] == 1
    assert rows[1]["name"] == "get_quote"
    assert rows[1]["summary"]                      # 结果摘要非空
    assert rows[1]["duration_ms"] is not None
    # 注意：mock provider 是先发工具事件、后建 assistant 占位消息，所以这两行
    # 的 message_id 为空；cli provider（claude/codex）先建占位消息，那里会带上。
