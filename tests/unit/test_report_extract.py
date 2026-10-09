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


# ---------- 脏 JSON / 结构不可用条目：显式可见，不静默 ----------


def test_parse_llm_json_strips_code_fence():
    """LLM 常把 JSON 包在 ```json 围栏里：这是可解析的，不该报「不是合法 JSON」。"""
    from lquant.research.report_extract import _parse_llm_json

    assert _parse_llm_json('```json\n{"proposals": [{"expr": "x"}]}\n```') == [{"expr": "x"}]


def test_parse_llm_json_extracts_balanced_object_from_prose():
    """围栏外带前后话术：退化到第一个括号配平的 {...} 片段。"""
    from lquant.research.report_extract import _parse_llm_json

    raw = '好的，结果如下：{"proposals": [{"expr": "y"}]} 以上。'
    assert _parse_llm_json(raw) == [{"expr": "y"}]


@pytest.mark.parametrize("raw", [None, 123, ["a"], {"proposals": []}])
def test_parse_llm_json_non_string_raises_value_error(raw):
    """content 为 None/数字/数组时不能再裸抛 TypeError（逃出 CLI 只剩不可读信息）。"""
    from lquant.research.report_extract import _parse_llm_json

    with pytest.raises(ValueError):
        _parse_llm_json(raw)


def test_malformed_entry_lands_in_rejected_with_reason():
    """LLM 用 expression 等键名：条目不再消失，带结构原因进 rejected。"""
    res = extract_proposals(
        "研报",
        llm_fn=fake_llm(
            [{"expr": GOOD, "note": "ok"}, {"expression": "Rank($close)", "note": "键名不符"}]
        ),
    )
    assert [a["expr"] for a in res["accepted"]] == [GOOD]
    assert len(res["rejected"]) == 1
    assert "缺少可用的 expr" in res["rejected"][0]["reason"]
    assert "expression" in res["rejected"][0]["reason"]  # 原键名/值可见，便于排障


def test_non_dict_entry_lands_in_rejected():
    """非对象条目（LLM 返回字符串数组）也不能被列表推导静默吃掉。"""
    res = extract_proposals("研报", llm_fn=fake_llm([{"expr": GOOD}, "Rank($close)"]))
    assert [a["expr"] for a in res["accepted"]] == [GOOD]
    assert len(res["rejected"]) == 1
    assert "不是 JSON 对象" in res["rejected"][0]["reason"]


def test_all_malformed_raises_real_reason_not_g0():
    """全部结构不可用：说清是 LLM 输出坏了，绝不冒充「G0 淘汰」。"""
    with pytest.raises(ValueError, match="全部无法解析"):
        extract_proposals("研报", llm_fn=fake_llm([{"expression": GOOD}]))


def test_empty_proposals_array_raises_distinct_reason():
    """proposals 合法但为空：也不能让 CLI 报成「G0 淘汰了 0 条」。"""
    with pytest.raises(ValueError, match="为空数组"):
        extract_proposals("研报", llm_fn=fake_llm([]))


def test_cli_propose_reports_malformed_loudly(monkeypatch):
    """CLI 出口：结构全坏时报「提取失败: ...全部无法解析」，不是「没有通过 G0」。"""
    import lquant.research.report_extract as m

    monkeypatch.setattr(m, "_env_llm", lambda: fake_llm([{"expression": GOOD}]))
    r = CliRunner().invoke(propose, [], input="研报文本：动量效应")
    assert r.exit_code != 0
    assert "全部无法解析" in r.output
    assert "没有通过 G0" not in r.output


def test_empty_report_raises():
    with pytest.raises(ValueError, match="研报文本为空"):
        extract_proposals("   ", llm_fn=fake_llm([]))


# ---------- 输入长度门禁：不静默截断，也不等到 LLM 侧 HTTP 400 ----------


def test_oversized_report_rejected_before_llm_call():
    """超长研报显式报错，且拦在 LLM 调用之前（不会先烧一次请求）。"""
    called: list[str] = []

    def spy_llm(system: str, user: str) -> str:
        called.append(user)
        return json.dumps({"proposals": [{"expr": GOOD}]})

    with pytest.raises(ValueError, match="超长"):
        extract_proposals("研" * 40, llm_fn=spy_llm, max_input_chars=10)
    assert called == []  # 长度门禁先于任何网络调用


def test_input_limit_env_override(monkeypatch):
    """LQ_LLM_MAX_INPUT_CHARS 覆盖默认上限：调小则拒绝，调大后同一文本通过。"""
    monkeypatch.setenv("LQ_LLM_MAX_INPUT_CHARS", "20")
    with pytest.raises(ValueError, match="超长"):
        extract_proposals("研" * 50, llm_fn=fake_llm([{"expr": GOOD}]))
    res = extract_proposals("研" * 5, llm_fn=fake_llm([{"expr": GOOD}]))
    assert res["accepted"][0]["expr"] == GOOD


def test_input_limit_default_is_positive():
    from lquant.research.report_extract import DEFAULT_MAX_INPUT_CHARS, _max_input_chars

    assert DEFAULT_MAX_INPUT_CHARS > 0
    assert _max_input_chars() == DEFAULT_MAX_INPUT_CHARS


def test_invalid_input_limit_env_raises_loudly(monkeypatch):
    """配置写错（非整数）不能静默退回默认值，否则门禁形同虚设。"""
    monkeypatch.setenv("LQ_LLM_MAX_INPUT_CHARS", "not-a-number")
    with pytest.raises(ValueError, match="LQ_LLM_MAX_INPUT_CHARS"):
        extract_proposals("研报", llm_fn=fake_llm([{"expr": GOOD}]))


def test_nonpositive_input_limit_raises_loudly(monkeypatch):
    """上限 ≤ 0 同样是配置错误：env 与调用参数两条入口都必须显式报错。

    静默当成「无上限」会让长度门禁形同虚设（0 字上限下任何研报都超长，
    静默放行则相反），所以两条路径都不允许悄悄吞掉。
    """
    from lquant.research.report_extract import _max_input_chars

    called: list[str] = []

    def spy_llm(system: str, user: str) -> str:
        called.append(user)
        return json.dumps({"proposals": [{"expr": GOOD}]})

    monkeypatch.setenv("LQ_LLM_MAX_INPUT_CHARS", "0")
    with pytest.raises(ValueError, match="LQ_LLM_MAX_INPUT_CHARS 须为正整数"):
        _max_input_chars()
    with pytest.raises(ValueError, match="max_input_chars 须为正整数"):
        extract_proposals("研报", llm_fn=spy_llm, max_input_chars=0)
    assert called == []  # 参数校验先于任何网络调用


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
