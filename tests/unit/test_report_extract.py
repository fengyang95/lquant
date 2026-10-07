"""研报 → 因子提案：FakeLLM 注入、G0 双层防御、消费端契约闭环。"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from lquant.cli.commands.factor import propose
from lquant.factors.mining.llm import load_proposals
from lquant.research.report_extract import (
    capability_prompt,
    extract_proposals,
    write_proposals,
)

GOOD = "Rank(Ts_Mean($close, 5) / $close - 1)"


def fake_llm(payload: list[dict], raw: str | None = None):
    """返回固定 LLM 行为的注入桩：payload 合法时产 JSON，raw 优先（测坏格式）。"""

    def fn(system: str, user: str) -> str:
        return raw if raw is not None else json.dumps({"proposals": payload}, ensure_ascii=False)

    return fn


# ---------- 双层防御：G0 淘汰可见 ----------


def test_valid_expr_passes_and_invalid_lands_in_rejected():
    res = extract_proposals(
        "研报说动量效应显著",
        llm_fn=fake_llm(
            [
                {"expr": GOOD, "note": "短期反转后动量"},
                {"expr": "Foo($close, 5)", "note": "幻觉算子"},
                {"expr": "Rank($revenue)", "note": "幻觉字段"},
            ]
        ),
    )
    assert [a["expr"] for a in res["accepted"]] == [GOOD]
    assert len(res["rejected"]) == 2
    assert all(r["reason"] == "STATIC_FAIL" for r in res["rejected"])
    # 不静默：被拒的连同原文与原因都在返回里
    assert res["rejected"][0]["expr"] == "Foo($close, 5)"


def test_max_proposals_truncates_with_explicit_reason():
    res = extract_proposals(
        "text", llm_fn=fake_llm([{"expr": GOOD, "note": "x"}] * 3), max_proposals=2
    )
    assert len(res["accepted"]) == 2
    assert len(res["rejected"]) == 1
    assert res["rejected"][0]["reason"] == "超过 max_proposals 上限"


def test_bad_json_raises_with_snippet():
    with pytest.raises(ValueError, match="不是合法 JSON"):
        extract_proposals("text", llm_fn=fake_llm(None, raw="这不是JSON"))


def test_missing_proposals_array_raises():
    raw = json.dumps({"factors": []})
    with pytest.raises(ValueError, match="proposals"):
        extract_proposals("text", llm_fn=fake_llm(None, raw=raw))


def test_empty_report_raises():
    with pytest.raises(ValueError, match="研报文本为空"):
        extract_proposals("   ", llm_fn=fake_llm([]))


# ---------- env 与密钥卫生 ----------


def test_env_llm_requires_key_loudly(monkeypatch):
    import lquant.research.report_extract as m

    monkeypatch.delenv("LQ_LLM_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="LQ_LLM_API_KEY"):
        m._env_llm()


def test_capability_prompt_is_registry_driven():
    """prompt 里的算子/字段来自真实注册表 —— 新注册算子自动进 LLM 视野。"""
    p = capability_prompt()
    assert "$close" in p and "Ts_Mean" in p
    assert "Ts_Mean($close, 20)" in p or "示例" in p or "Rank(" in p


# ---------- 消费端契约闭环 ----------


def test_write_proposals_roundtrip_with_load_proposals(tmp_path):
    """write_proposals 的 JSONL 必须能被 mine --generator proposals 的
    load_proposals 直接读回 —— 生产端与消费端同一契约。"""
    out = str(tmp_path / "props.jsonl")
    items = [{"expr": GOOD, "note": "动量"}, {"expr": "Rank($turnover_rate)", "note": "换手"}]
    n = write_proposals(items, out)
    assert n == 2
    loaded = load_proposals(out)
    assert [p["expr"] for p in loaded] == [GOOD, "Rank($turnover_rate)"]


# ---------- CLI ----------


def test_cli_propose_writes_out_and_reports_rejected(tmp_path, monkeypatch):
    import lquant.research.report_extract as m

    monkeypatch.setattr(
        m,
        "_env_llm",
        lambda: fake_llm(
            [
                {"expr": GOOD, "note": "ok"},
                {"expr": "Foo($close)", "note": "bad"},
            ]
        ),
    )
    out = str(tmp_path / "props.jsonl")
    report = tmp_path / "report.txt"
    report.write_text("某券商研报：低波动异象", encoding="utf-8")
    r = CliRunner().invoke(propose, [str(report), "--out", out])
    assert r.exit_code == 0, r.output
    assert "rejected" in r.output  # stderr 明细随混合输出可见
    assert [p["expr"] for p in load_proposals(out)] == [GOOD]
    assert "generator proposals" in r.output  # 指路下一步


def test_cli_propose_fails_loudly_when_all_rejected(monkeypatch):
    import lquant.research.report_extract as m

    monkeypatch.setattr(m, "_env_llm", lambda: fake_llm([{"expr": "Foo($close)", "note": "bad"}]))
    r = CliRunner().invoke(propose, [], input="研报文本：动量效应")
    assert r.exit_code != 0
    assert "没有通过 G0" in r.output
