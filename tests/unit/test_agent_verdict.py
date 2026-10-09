"""结构化结论契约：数字必须带 source/as_of，弃权必须说明理由。"""
from __future__ import annotations

import json

import pytest

from lquant.agent.sessions import SessionStore, verdict_db_path, write_verdict_sync
from lquant.agent.verdict import (
    AgentVerdict,
    VerdictRejected,
    parse_verdict,
    verdict_payload,
)


def _ok(**kw) -> dict:
    base = {
        "ticker": "600519.SH", "as_of": "2026-10-08", "direction": "看多",
        "confidence": 0.6, "summary": "北向成交额创近期新高",
        "claims": [{"metric": "北向成交额", "value": 2771.55, "unit": "亿元",
                    "source": "northbound_flow", "as_of": "2026-10-08"}],
        "evidence": [{"source": "get_market_overview", "as_of": "2026-10-08",
                      "quote": "总成交额 2771.55 亿元"}],
    }
    base.update(kw)
    return base


def test_accepts_complete_verdict():
    v = parse_verdict(_ok())
    assert isinstance(v, AgentVerdict) and v.direction == "看多"
    payload = verdict_payload(v)
    assert payload["ticker"] == "600519.SH" and payload["abstain"] == 0
    assert json.loads(payload["payload_json"])["claims"][0]["source"] == "northbound_flow"


def test_parse_verdict_passthrough_for_model_instance():
    """已经是 AgentVerdict 就原样返回（调用方不必先 dump 再 parse）。"""
    v = AgentVerdict(ticker="600519.SH", as_of="2026-10-08", direction="中性",
                     confidence=0.5,
                     evidence=[{"source": "s", "as_of": "2026-10-08"}])
    assert parse_verdict(v) is v


def test_accepts_json_string_and_wrapper_key():
    assert parse_verdict(json.dumps(_ok())).ticker == "600519.SH"
    assert parse_verdict({"verdict": _ok()}).ticker == "600519.SH"
    assert parse_verdict({"input": _ok()}).ticker == "600519.SH"


def test_claim_without_source_or_as_of_is_rejected():
    """契约的核心约束：数字字段没有 source/as_of 直接拒收。"""
    for drop in ("source", "as_of"):
        claim = {"metric": "净利润增速", "value": 25.0}
        claim.update({k: v for k, v in
                      _ok()["claims"][0].items() if k not in (drop,)})
        with pytest.raises(VerdictRejected) as e:
            parse_verdict(_ok(claims=[claim]))
        assert drop in str(e.value)


def test_verdict_without_evidence_is_rejected():
    with pytest.raises(VerdictRejected, match="evidence"):
        parse_verdict(_ok(evidence=[]))


def test_verdict_without_ticker_or_as_of_is_rejected():
    with pytest.raises(VerdictRejected, match="ticker"):
        parse_verdict(_ok(ticker=""))
    with pytest.raises(VerdictRejected, match="as_of"):
        parse_verdict(_ok(as_of=""))


def test_confidence_must_be_positive_when_not_abstaining():
    with pytest.raises(VerdictRejected, match="confidence"):
        parse_verdict(_ok(confidence=0))


def test_abstain_requires_direction_and_reason():
    good = parse_verdict({"abstain": True, "direction": "abstain",
                          "withheld": ["2025 日线缺失，无法回测"]})
    assert good.abstain and good.withheld
    with pytest.raises(VerdictRejected, match="direction"):
        parse_verdict({"abstain": True, "direction": "中性", "withheld": ["x"]})
    with pytest.raises(VerdictRejected, match="withheld"):
        parse_verdict({"abstain": True, "direction": "abstain"})
    with pytest.raises(VerdictRejected, match="abstain=true"):
        parse_verdict({"direction": "abstain", "ticker": "600519.SH",
                       "as_of": "2026-10-08", "confidence": 0.5,
                       "evidence": [{"source": "s", "as_of": "a"}]})


def test_bad_shapes_are_rejected_with_readable_message():
    with pytest.raises(VerdictRejected, match="JSON"):
        parse_verdict("不是 json")
    with pytest.raises(VerdictRejected, match="JSON 对象"):
        parse_verdict([1, 2, 3])
    with pytest.raises(VerdictRejected, match="confidence"):
        parse_verdict(_ok(confidence=2))


# ---- 落库：MCP 侧同步写、主服务侧异步读，同一个文件 ----

def test_sync_write_then_async_read(tmp_path, monkeypatch):
    db = tmp_path / "ask.db"
    monkeypatch.setenv("LQ_ASK_DB", str(db))
    assert verdict_db_path() == str(db)
    vid = write_verdict_sync(str(db), "sid-1", verdict_payload(parse_verdict(_ok())))
    assert vid

    import asyncio

    async def read():
        store = SessionStore(str(db))
        try:
            return await store.list_verdicts("sid-1")
        finally:
            await store.close()

    rows = asyncio.run(read())
    assert len(rows) == 1
    assert rows[0]["ticker"] == "600519.SH" and rows[0]["direction"] == "看多"
    assert json.loads(rows[0]["payload_json"])["as_of"] == "2026-10-08"


def test_verdict_db_path_none_when_unbound(monkeypatch):
    monkeypatch.delenv("LQ_ASK_DB", raising=False)
    assert verdict_db_path() is None
    monkeypatch.setenv("LQ_ASK_DB", "   ")
    assert verdict_db_path() is None
