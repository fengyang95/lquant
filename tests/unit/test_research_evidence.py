"""C2 证据约束栈：压缩包 / 引用校验 / 阶段检查点 / 失败路径纪律。

覆盖 FEATURE-IDEAS.md:115 的四条硬要求：
  - 压缩双上限 + 编号稳定 + 计数正确 + 确定性（含输入乱序）；
  - 引用 id 必须存在、引文必须命中（空格/全半角差异可容忍），失败给原因；
  - 检查点四元组任一变化拒绝复用；
  - 未通过引用校验的中间态在类型层面不能变成最终报告；
  - 连接错误/取消不触发 JSON 修复（打桩断言）。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import urllib.error
from unittest.mock import Mock

import pytest

from lquant.research.evidence import (
    FinalReport,
    LLMFormatError,
    UnvalidatedIntermediateError,
    compress_evidence,
    evidence_digest,
    guarded_repair,
    normalize_for_match,
    open_checkpoint,
    sanitize_failure_text,
    validate_citations,
)

GOOD = "Rank(Ts_Mean($close, 5) / $close - 1)"


def src(sid: str, content: str, *, kind: str = "news", title: str = "", date: str = "") -> dict:
    return {
        "id": sid,
        "title": title or f"来源-{sid}",
        "content": content,
        "published_at": date,
        "kind": kind,
    }


# ---------------------------------------------------------------------------
# 1) 确定性证据压缩包
# ---------------------------------------------------------------------------


def test_card_limit_trims_by_priority_and_keeps_ids_stable():
    evidence = [
        src("n1", "一般新闻正文" * 3, kind="news", date="2024-01-01"),
        src("d1", "经营情况：产能与订单" * 3, kind="disclosure", title="投资者关系活动记录", date="2024-03-01"),
        src("n2", "另一条新闻" * 3, kind="news", date="2024-02-01"),
        src("m1", "方法论说明" * 3, kind="methodology"),
    ]
    pack = compress_evidence(evidence, max_cards=2, max_bytes=100000)

    # 编号只做保留/丢弃，绝不重编号：卡片 id 必须逐字来自输入
    assert set(pack.ids) <= {e["id"] for e in evidence}
    assert "d1" in pack.ids  # 业务披露优先
    assert pack.stats.original_source_count == 4
    assert pack.stats.selected_source_count == 2
    assert len(pack.dropped) == 2
    assert all("卡片数上限" in d.reason for d in pack.dropped)
    assert pack.stats.original_content_bytes > pack.stats.selected_content_bytes


def test_byte_limit_drops_lower_priority_and_reports_truncation():
    big = "营业收入同比增长，订单饱满。" * 40  # 约 560*3 字节
    evidence = [
        src("n1", big, kind="news", date="2024-05-01"),
        src("d1", big, kind="disclosure", title="经营情况公告", date="2024-04-01"),
    ]
    pack = compress_evidence(evidence, max_cards=10, max_bytes=450, per_source_bytes=400)

    assert pack.ids == ("d1",)  # 高优先级先吃预算
    assert pack.stats.selected_content_bytes <= 600
    assert [t.id for t in pack.truncated] == ["d1"]
    assert pack.truncated[0].kept_bytes < pack.truncated[0].original_bytes
    assert [d.id for d in pack.dropped] == ["n1"]
    assert "预算耗尽" in pack.dropped[0].reason
    assert any("截断" in note for note in pack.limitations)


def test_compression_is_deterministic_under_repeat_and_shuffle():
    # 两条同分同日期：并列时必须靠 id 兜底，否则乱序会改变结果
    evidence = [
        src("b", "同样长度的正文内容甲乙丙丁", kind="news", date="2024-01-01"),
        src("a", "同样长度的正文内容甲乙丙丁", kind="news", date="2024-01-01"),
        src("c", "第三条正文内容" * 2, kind="news", date="2024-02-01"),
    ]
    first = compress_evidence(evidence, max_cards=2, max_bytes=100000).to_dict()
    second = compress_evidence(evidence, max_cards=2, max_bytes=100000).to_dict()
    shuffled = compress_evidence(list(reversed(evidence)), max_cards=2, max_bytes=100000).to_dict()

    assert first == second
    assert first == shuffled
    assert [c["id"] for c in first["cards"]] == ["c", "a"]


def test_compression_duplicate_id_fails_loudly():
    with pytest.raises(ValueError, match="id 重复"):
        compress_evidence([src("x", "a"), src("x", "b")])


def test_compression_empty_evidence_fails_loudly():
    with pytest.raises(ValueError, match="证据列表为空"):
        compress_evidence([])


def test_evidence_digest_is_order_independent():
    evidence = [src("a", "甲"), src("b", "乙")]
    assert evidence_digest(evidence) == evidence_digest(list(reversed(evidence)))
    assert evidence_digest(evidence) != evidence_digest([src("a", "甲改"), src("b", "乙")])


# ---------------------------------------------------------------------------
# 2) 引用校验
# ---------------------------------------------------------------------------


def _cards():
    return compress_evidence(
        [
            src("s1", "公司2023年营业收入为 100 亿元。同比下滑3%。", kind="disclosure", title="年报"),
            src("s2", "行业景气度回升，需求端订单增加。", kind="news"),
        ],
        max_bytes=100000,
    ).cards


def test_quote_hits_after_whitespace_and_fullwidth_normalization():
    report = {
        "claims": [
            {
                "note": "营收百亿",
                "source_ids": ["s1"],
                # 全角数字 + 多余空格：规范化后应命中
                "quote": "公司2023年营业收入为１００ 亿元。",
            }
        ]
    }
    verdict = validate_citations(report, _cards())
    assert verdict.ok
    assert verdict.verified_claims == 1
    assert verdict.failures == ()
    assert verdict.fallback is None


def test_unknown_source_id_fails_with_reason_and_fallback():
    report = {"claims": [{"note": "杜撰来源", "source_ids": ["s999"]}]}
    verdict = validate_citations(report, _cards())

    assert not verdict.ok
    assert verdict.fallback  # 失败必带降级口径，不静默通过
    assert [f.code for f in verdict.failures] == ["UNKNOWN_SOURCE"]
    assert "s999" in verdict.failures[0].detail
    assert "s1" in verdict.failures[0].detail  # 明细里给出可用编号


def test_missing_source_ids_fails():
    verdict = validate_citations({"claims": [{"note": "没有引用"}]}, _cards())
    assert not verdict.ok
    assert verdict.failures[0].code == "MISSING_SOURCE"


def test_quote_not_found_reports_quote_and_sources():
    report = {"claims": [{"note": "编造引文", "source_ids": ["s1"], "quote": "公司营收翻倍"}]}
    verdict = validate_citations(report, _cards())

    assert not verdict.ok
    failure = verdict.failures[0]
    assert failure.code == "QUOTE_NOT_FOUND"
    assert "s1" in failure.detail
    assert "公司营收翻倍" in failure.detail


def test_proposals_list_is_accepted_as_report():
    """接线处传的就是 accepted 提案列表，校验器要能直接吃。"""
    proposals = [
        {"expr": GOOD, "note": "订单驱动", "source_ids": ["s2"], "quote": "需求端订单增加"},
    ]
    verdict = validate_citations(proposals, _cards())
    assert verdict.ok
    assert verdict.checked_claims == 1


def test_empty_report_is_rejected_not_silently_passed():
    with pytest.raises(ValueError, match="没有任何论点"):
        validate_citations({"claims": []}, _cards())


def test_duplicate_card_ids_fail_loudly():
    cards = [c.to_dict() for c in _cards()]
    with pytest.raises(ValueError, match="id 重复"):
        validate_citations({"claims": [{"note": "x", "source_ids": ["s1"]}]}, cards + cards[:1])


def test_normalize_for_match_folds_width_and_space():
    assert normalize_for_match("Ａ Ｂ　１") == "ab1"
    assert normalize_for_match(" 营 收 ") == normalize_for_match("营收")


# ---------------------------------------------------------------------------
# 3) 阶段检查点
# ---------------------------------------------------------------------------

IDENTITY = {
    "model_identity": "deepseek-v3",
    "prompt_version": "report-extract-v1",
    "request": {"report": "研报A", "max_proposals": 10},
    "evidence": [src("s1", "正文")],
}


def _checkpoint():
    return open_checkpoint(**IDENTITY)


@pytest.mark.parametrize(
    "field",
    ["model_identity", "prompt_version", "request", "evidence"],
)
def test_checkpoint_refuses_reuse_when_any_of_four_changes(field):
    cp = _checkpoint()
    changed = dict(IDENTITY)
    if field == "model_identity":
        changed[field] = "gpt-4o"
    elif field == "prompt_version":
        changed[field] = "report-extract-v2"
    elif field == "request":
        changed[field] = {"report": "研报B", "max_proposals": 10}
    else:
        changed[field] = [src("s1", "正文改")]

    with pytest.raises(Exception, match="重新分析"):
        open_checkpoint(previous=cp, **changed)
    # 同一实例恢复也走同一条拒绝路径
    with pytest.raises(Exception, match="不能继续旧研究"):
        cp.resume(open_checkpoint(**changed).identity)


def test_checkpoint_allows_reuse_when_identity_unchanged():
    cp = _checkpoint()
    cp.record("outline", {"questions": ["q1"]})
    resumed = open_checkpoint(previous=cp, **IDENTITY)

    assert resumed is cp
    assert resumed.stage_names == ("outline",)
    assert resumed.intermediate("outline").value == {"questions": ["q1"]}
    assert resumed.resume(cp.identity) is cp


def test_checkpoint_serialization_has_no_final_report():
    cp = _checkpoint()
    cp.record("outline", {"questions": []})
    payload = cp.to_dict()

    assert set(payload) == {"version", "identity", "stages", "failures"}
    assert "final" not in json.dumps(payload)
    restored = type(cp).from_dict(json.loads(json.dumps(payload)))
    assert restored.stage_names == ("outline",)


def test_unvalidated_intermediate_cannot_become_final_report():
    cp = _checkpoint()
    cards = _cards()
    bad_report = {"claims": [{"note": "引用不存在来源", "source_ids": ["nope"]}]}
    cp.record("draft", bad_report)

    # 结构保证 1：检查点里的中间态只能以 IntermediateStage 取出
    intermediate = cp.intermediate("draft")
    assert not isinstance(intermediate.value, FinalReport)

    # 结构保证 2：finalize 在引用校验失败时抛错，绝不返回最终报告
    with pytest.raises(UnvalidatedIntermediateError, match="引用校验失败"):
        cp.finalize(bad_report, cards)

    # 结构保证 3：连直接构造 FinalReport 都过不去
    from lquant.research.evidence import CitationVerdict

    failing = validate_citations(bad_report, cards)
    with pytest.raises(UnvalidatedIntermediateError):
        FinalReport(identity=cp.identity, claims=(), validation=failing)
    assert isinstance(failing, CitationVerdict)

    # 校验通过后才拿得到最终报告
    good_report = {"claims": [{"note": "有引用", "source_ids": ["s1"]}]}
    final = cp.finalize(good_report, cards)
    assert isinstance(final, FinalReport)
    assert final.validation.ok


def test_final_report_cannot_be_written_back_as_intermediate():
    cp = _checkpoint()
    final = cp.finalize({"claims": [{"note": "x", "source_ids": ["s1"]}]}, _cards())
    with pytest.raises(TypeError, match="不能回写"):
        cp.record("final", final)


def test_stage_failure_is_bounded_and_redacted_and_not_publicly_exposed():
    cp = _checkpoint()
    long_content = "prompt 回显 " * 800 + " api_key=sk-supersecretvalue"
    failure = cp.record_failure("synthesis", "连接失败", long_content)

    assert len(failure.error) <= 2000 + 40
    public = cp.public_failures()[0]
    assert set(public) == {"stage", "error", "error_chars"}
    assert "content" not in public
    assert "prompt 回显" not in json.dumps(cp.to_dict(), ensure_ascii=False)


# ---------------------------------------------------------------------------
# 4) 失败路径纪律
# ---------------------------------------------------------------------------


def test_connection_error_does_not_trigger_repair():
    repair = Mock(return_value="{}")
    with pytest.raises(urllib.error.URLError):
        guarded_repair(repair, urllib.error.URLError("connection refused"))
    repair.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [asyncio.CancelledError(), concurrent.futures.CancelledError(), KeyboardInterrupt()],
)
def test_cancellation_does_not_trigger_repair(error):
    repair = Mock(return_value="{}")
    with pytest.raises(type(error)):
        guarded_repair(repair, error)
    repair.assert_not_called()


def test_format_error_does_trigger_repair():
    repair = Mock(return_value="{'ok': 1}")
    assert guarded_repair(repair, LLMFormatError("bad json")) == "{'ok': 1}"
    repair.assert_called_once()


def test_sanitize_failure_text_limits_and_redacts():
    text = "api_key=sk-abcdefghijklmnop " + "x" * 3000 + " Bearer abcdefghijklmnop"
    cleaned = sanitize_failure_text(text, max_chars=200)

    assert cleaned.startswith("api_key=[REDACTED]")
    assert len(cleaned) <= 200 + 40
    assert "sk-abcdefghijklmnop" not in cleaned
    assert "Bearer abcdefghijklmnop" not in cleaned
    assert "[REDACTED]" in cleaned

    extra = sanitize_failure_text("key=mysecretvalue here", extra_secrets=["mysecretvalue"])
    assert "mysecretvalue" not in extra


# ---------------------------------------------------------------------------
# 5) 接线：report_extract 的引用约束与降级路径
# ---------------------------------------------------------------------------


def _fake_llm(payload: list[dict] | None = None, raw: str | None = None):
    def fn(system: str, user: str) -> str:
        return raw if raw is not None else json.dumps({"proposals": payload}, ensure_ascii=False)

    return fn


EVIDENCE = [
    src("s1", "公司2023年营业收入为 100 亿元。", kind="disclosure", title="年报", date="2024-04-01"),
    src("s2", "行业需求端订单增加。", kind="news", date="2024-03-01"),
]


def test_wiring_passes_when_citations_verified():
    from lquant.research.report_extract import extract_proposals

    res = extract_proposals(
        "研报正文",
        llm_fn=_fake_llm(
            [{"expr": GOOD, "note": "营收百亿", "source_ids": ["s1"], "quote": "公司2023年营业收入为１００ 亿元。"}]
        ),
        evidence=EVIDENCE,
    )
    assert res["status"] == "ok"
    assert res["citation_validation"]["ok"] is True
    assert res["evidence_pack"]["stats"]["selected_source_count"] == 2
    assert "validation_failed" not in res["accepted"][0]


def test_wiring_degrades_and_marks_when_citation_unknown():
    from lquant.research.report_extract import extract_proposals

    res = extract_proposals(
        "研报正文",
        llm_fn=_fake_llm([{"expr": GOOD, "note": "杜撰", "source_ids": ["s999"]}]),
        evidence=EVIDENCE,
    )
    # 不静默、不冒充成功：状态降级 + 逐条原因
    assert res["status"] == "degraded"
    assert res["fallback"]
    assert res["citation_validation"]["ok"] is False
    assert res["accepted"][0]["validation_failed"] is True
    assert any("s999" in r for r in res["accepted"][0]["validation_reasons"])
    # 原始提案仍保留，便于人审
    assert res["accepted"][0]["expr"] == GOOD


def test_wiring_without_evidence_keeps_legacy_shape():
    from lquant.research.report_extract import extract_proposals

    res = extract_proposals("研报正文", llm_fn=_fake_llm([{"expr": GOOD, "note": "x"}]))
    assert res["status"] == "ok"
    assert "evidence_pack" not in res
    assert "citation_validation" not in res


def test_wiring_transport_error_never_calls_repair():
    from lquant.research.report_extract import extract_proposals

    def boom(system: str, user: str) -> str:
        raise urllib.error.URLError("connection refused")

    repair = Mock(return_value="{}")
    with pytest.raises(urllib.error.URLError):
        extract_proposals("研报正文", llm_fn=boom, repair_fn=repair, evidence=EVIDENCE)
    repair.assert_not_called()


def test_wiring_format_error_triggers_repair_with_fixed_json():
    from lquant.research.report_extract import extract_proposals

    repair = Mock(return_value=json.dumps({"proposals": [{"expr": GOOD, "note": "修好了"}]}))
    res = extract_proposals("研报正文", llm_fn=_fake_llm(raw="这不是JSON"), repair_fn=repair)

    repair.assert_called_once()
    assert res["accepted"][0]["expr"] == GOOD


# ── 归一化/压缩/引用/检查点的退化分支（覆盖率补齐） ─────────────────────


def test_evidence_coercion_error_branches():
    from lquant.research import evidence as ev

    with pytest.raises(TypeError, match="必须是 mapping"):
        ev._coerce_source(42)
    with pytest.raises(ValueError, match="缺少来源 id"):
        ev._coerce_source({"title": "x"})
    assert ev._first_value({}, ("a",), "d") == "d"
    assert ev._first_value({"a": 5}, ("a",)) == "5"

    with pytest.raises(TypeError, match="必须是 str"):
        ev.normalize_for_match(42)
    with pytest.raises(TypeError, match="必须是 mapping"):
        ev._coerce_card(42)
    with pytest.raises(TypeError, match="不是 mapping"):
        ev.coerce_cards({"id": "a"})
    with pytest.raises(ValueError, match="id 重复"):
        ev.coerce_cards([src("a", "x"), src("a", "y")])


def test_evidence_id_and_quote_coercion():
    from lquant.research import evidence as ev

    assert ev._as_id_list(None) == ()
    assert ev._as_id_list("  ") == ()
    assert ev._as_id_list("a") == ("a",)
    assert ev._as_id_list({"a": 1, "b": 2}) == ("a", "b")
    assert ev._as_id_list(["a", " ", {"source_id": "b"}, 42]) == ("a", "b")
    with pytest.raises(TypeError, match="source_ids 不支持"):
        ev._as_id_list(42)

    assert ev._as_quotes(None) == ()
    assert ev._as_quotes("q")[0].quote == "q"
    assert ev._as_quotes({"source_id": "a", "quote": "q"})[0].source_id == "a"
    assert len(ev._as_quotes(["q1", "q2"])) == 2
    with pytest.raises(TypeError, match="quotes 不支持"):
        ev._as_quotes(42)
    with pytest.raises(TypeError, match="单条引文"):
        ev._coerce_quote(42)


def test_extract_claims_and_quote_hits_guards():
    from lquant.research import evidence as ev

    with pytest.raises(TypeError, match="不是 mapping"):
        ev._coerce_claim("x", 0)
    with pytest.raises(ValueError, match="缺少 claims"):
        ev._extract_claims({})
    with pytest.raises(TypeError, match="必须是带 claims"):
        ev._extract_claims(42)
    with pytest.raises(ValueError, match="必须是数组"):
        ev._extract_claims({"claims": "not-list"})
    assert ev._quote_hits("abc", "   ") is False


def test_validate_citations_failure_branches():
    from lquant.research import evidence as ev

    cards = [src("a", "公司发布公告，营业总收入同比增长 20%")]
    codes = lambda v: {f.code for f in v.failures}  # noqa: E731

    v = ev.validate_citations({"claims": [{"text": "x", "source_ids": ["ghost"]}]}, cards)
    assert not v.ok and "UNKNOWN_SOURCE" in codes(v)
    assert not ev.validate_citations({"claims": [{"text": "x"}]}, cards).ok
    v = ev.validate_citations(
        {"claims": [{"text": "x", "source_ids": ["a"],
                     "quotes": [{"source_id": "a", "quote": "   "}]}]}, cards)
    assert "EMPTY_QUOTE" in codes(v)
    v = ev.validate_citations(
        {"claims": [{"text": "x", "quotes": [{"quote": "营业总收入"}]}]}, cards)
    assert "MISSING_SOURCE" in codes(v)
    v = ev.validate_citations(
        {"claims": [{"text": "x", "source_ids": ["a"],
                     "quotes": [{"source_id": "a", "quote": "不存在的句子"}]}]}, cards)
    assert "QUOTE_NOT_FOUND" in codes(v)

    ok = ev.validate_citations(
        {"claims": [{"text": "x", "source_ids": ["a"],
                     "quotes": [{"quote": "营业总收入"}]}]}, cards)
    assert ok.ok and ok.verified_claims == 1 and ok.fallback is None


def test_compress_evidence_guards_and_title_only():
    from lquant.research import evidence as ev

    for kwargs in ({"max_cards": 0}, {"max_bytes": 0}, {"per_source_bytes": 0}):
        with pytest.raises(ValueError, match="必须为正整数"):
            ev.compress_evidence([src("a", "x")], **kwargs)

    titled = ev.compress_evidence([src("a", "", title="只有标题")])
    assert titled.cards and titled.cards[0].compression.startswith("仅标题")
    assert any("仅有标题" in n for n in titled.limitations)

    empty = ev.compress_evidence([{"id": "a", "title": "", "content": ""}])
    assert not empty.cards and empty.dropped  # 无正文且无标题 → 丢弃并可见


def test_truncate_score_and_compress_body_helpers():
    from lquant.research import evidence as ev

    assert ev._truncate_bytes("abc", 0) == ""
    assert ev._truncate_bytes("abc", 2) == "ab"
    assert ev._score_sentence("营业总收入同比增长 5%", 0) > ev._score_sentence("普通句子", 5)
    assert ev._compress_body("   ", 10) == ("", "empty")
    assert ev._compress_body("短句。", 100)[1] == "no_compression"
    long = "第一句关于风险与下滑。第二句是营业总收入同比增长。第三句无关内容在这里。"
    assert ev._compress_body(long, 20)[1] in ("sentence_selection", "head_truncate")
    assert "…" in ev._join_sentences([(0, "A"), (2, "C")])


def test_checkpoint_identity_and_fingerprints():
    from lquant.research import evidence as ev

    ident = ev.CheckpointIdentity("m", "p", "r", "e")
    assert ev.CheckpointIdentity.from_dict(ident.to_dict()) == ident
    with pytest.raises(ValueError, match="缺少字段"):
        ev.CheckpointIdentity.from_dict({"model_identity": "m"})
    assert ident.digest and ident.changed_fields(ev.CheckpointIdentity("m2", "p", "r", "e")) == (
        "model_identity",)

    assert ev.compute_request_fingerprint("x") == ev.content_hash({"request": "x"})
    assert ev.compute_request_fingerprint({"b": 1, "a": 2}) == ev.compute_request_fingerprint(
        {"a": 2, "b": 1})
    assert isinstance(ev.compute_request_fingerprint(42), str)

    pack = ev.compress_evidence([src("a", "x")])
    assert ev.evidence_digest(pack) == ev.content_hash(pack.to_dict())
    assert ev.evidence_digest([src("a", "x")])


def test_stage_failure_and_final_report_guards():
    from lquant.research import evidence as ev

    f = ev.StageFailure.build("s", RuntimeError("boom"), "y" * 999)
    assert "boom" in f.error and repr(f).startswith("StageFailure")
    assert "diagnostic" not in f.to_dict()  # 对外序列化不带失败原文
    with pytest.raises(ValueError, match="max_chars"):
        ev.StageFailure.build("s", "x", max_chars=0)

    ident = ev.CheckpointIdentity("m", "p", "r", "e")
    good = ev.CitationVerdict(True, 1, 1, ())
    fr = ev.FinalReport(ident, (), good)
    with pytest.raises(TypeError, match="不能回写成中间态"):
        ev.IntermediateStage("s", fr)
    with pytest.raises(ev.UnvalidatedIntermediateError, match="必须携带"):
        ev.FinalReport(ident, (), "not-a-verdict")  # type: ignore[arg-type]
    with pytest.raises(ev.UnvalidatedIntermediateError, match="引用校验未通过"):
        ev.FinalReport(ident, (), ev.CitationVerdict(False, 1, 0, (), fallback="x"))


def test_research_checkpoint_guards_and_roundtrip():
    from lquant.research import evidence as ev

    ident = ev.CheckpointIdentity("m", "p", "r", "e")
    cp = ev.ResearchCheckpoint(ident)
    with pytest.raises(ValueError, match="stage 不能为空"):
        cp.record("   ", "v")
    with pytest.raises(KeyError, match="没有阶段"):
        cp.intermediate("ghost")
    with pytest.raises(ValueError, match="版本不兼容"):
        ev.ResearchCheckpoint.from_dict({"version": 999})

    verdict = ev.validate_citations({"claims": [{"text": "x"}]}, [src("a", "x")])
    with pytest.raises(ev.UnvalidatedIntermediateError, match="引用校验失败"):
        cp.finalize({"claims": [{"text": "x"}]}, [src("a", "x")])
    assert cp.public_failures()  # 失败已记账

    cp2 = ev.ResearchCheckpoint(ident, failures={"s": ev.StageFailure.build("s", "e")})
    restored = ev.ResearchCheckpoint.from_dict(cp2.to_dict())
    assert restored.public_failures()[0]["stage"] == "s"

    fr = ev.FinalReport(ident, (), verdict if verdict.ok else ev.CitationVerdict(True, 0, 0, ()))
    bad = {**cp.to_dict(), "stages": {"final": fr}}
    with pytest.raises(ValueError, match="不允许出现最终报告"):
        ev.ResearchCheckpoint.from_dict(bad)
