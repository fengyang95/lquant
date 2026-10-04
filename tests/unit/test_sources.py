"""M2 来源接入：qlib 翻译 round-trip 全覆盖 + canonical 去重。"""
from __future__ import annotations

import pytest

from lquant.factors.dsl.analyzer import check
from lquant.factors.dsl.parser import parse
from lquant.factors.sources.qlib_source import factor_id, translate

FIELDS = {"trade_date", "symbol", "open", "high", "low", "close", "volume",
          "amount", "pre_close", "turnover_rate", "adj_factor"}


def test_translate_all_alpha158():
    """Alpha158 全部 158 条公式翻译后必须能 parse+check（单一执行语义硬规则）。"""
    from lquant.factors.qlib_alpha import list_builtin

    bad = []
    for item in list_builtin():
        f = item["formula"]
        try:
            out = translate(f)
            check(parse(out, "t"), allowed_fields=FIELDS)
        except Exception as e:  # noqa: BLE001
            bad.append((item["name"], f, str(e)))
    assert not bad, f"{len(bad)} 条翻译失败: {bad[:5]}"


def test_factor_id_case_insensitive_dedup():
    a = factor_id("Mean($close,20)/$close")
    b = factor_id("mean($CLOSE,20)/$close")
    assert a == b


def test_canonical_commutative_dedup():
    from lquant.factors.dsl.parser import parse
    from lquant.factors.dsl.printer import canonical

    a = canonical(parse("Ts_Mean($close,5) * 2", "a").root)
    b = canonical(parse("2 * Ts_Mean($close,5)", "b").root)
    assert a == b


def test_translate_missing_op_raises():
    with pytest.raises(ValueError):
        translate("ZzzzUnknown($close, 5)")


# ---------------- 语义正确性（原先只测了"能 parse"，漏掉了"意思对不对"）----------------

def _call_names(expr: str) -> list[str]:
    from lquant.factors.dsl.ast_nodes import BinaryOp, Call, UnaryOp

    def walk(node):
        yield node
        if isinstance(node, UnaryOp):
            yield from walk(node.arg)
        elif isinstance(node, BinaryOp):
            yield from walk(node.left)
            yield from walk(node.right)
        elif isinstance(node, Call):
            for arg in node.args:
                yield from walk(arg)

    return [n.name for n in walk(parse(expr, "scan").root) if isinstance(n, Call)]


def test_rolling_max_min_are_not_rewritten_to_elementwise():
    """Qlib 的 Max/Min 是**滚动**算子，必须译成 Ts_Max/Ts_Min。

    历史 bug：按「参数个数 == 2」把它们改写成 Greater/Less（逐元素取大/小），
    于是 `Max($high,20)` 变成 `max(最高价, 常数20)` —— 即最高价本身，
    MAX/MIN/RSV 整族因子静默退化成错误值。
    """
    assert translate("Max($high,20)") == "Ts_Max($high,20)"
    assert translate("Min($low,10)") == "Ts_Min($low,10)"
    assert translate("Max($close,5)/Min($close,5)") == "Ts_Max($close,5)/Ts_Min($close,5)"


def test_elementwise_greater_less_pass_through():
    """逐元素取大/小本来就该原样保留（Qlib 与 lquant 同名同义）。"""
    assert translate("Greater($open,$close)") == "Greater($open,$close)"
    assert translate("Less($open,$close)") == "Less($open,$close)"


def test_rsv_keeps_rolling_window():
    """RSV 的分母必须是 N 日高低区间，不是当日振幅。"""
    out = translate("($close-Min($low,10))/(Max($high,10)-Min($low,10)+1e-12)")
    assert out == "($close-Ts_Min($low,10))/(Ts_Max($high,10)-Ts_Min($low,10)+1e-12)"


def test_every_source_rolling_max_min_survives_translation():
    """不变量：源公式里每个滚动 Max/Min 都必须落成 Ts_Max/Ts_Min。

    按「源算子 → 目标算子」计数守恒来判，而不是看输出里有没有
    `Greater(x, 常数)` —— 后者是**合法**写法（SUMP/SUM 族用 `Greater(x, 0)`
    做 ReLU 截断），拿它当误译特征会误伤。这个不变量只钉一件事：
    滚动算子不许在翻译中变成逐元素算子。
    """
    from lquant.factors.qlib_alpha import list_builtin

    bad = []
    for item in list_builtin():
        src_names = _call_names(item["formula"])
        out_names = _call_names(translate(item["formula"]))
        want = sum(1 for n in src_names if n.lower() in ("max", "min"))
        got = sum(1 for n in out_names if n in ("Ts_Max", "Ts_Min"))
        if want != got:
            bad.append((item["name"], want, got))
    assert not bad, f"滚动 Max/Min 数量在翻译中丢失: {bad[:5]}"


def test_normalize_prefers_qlib_translation_for_every_alpha158_formula():
    """归一化不变量：每条 Alpha158 源公式都必须译成 `translate` 的结果。

    钉住同名歧义：lquant 也有 `Rank`（截面秩，1 参），若不校验元数，
    `Rank($close,10)` 会被当成截面秩直接放行，RANK 整族因子静默算错。
    """
    from lquant.factors.dsl.normalize import normalize
    from lquant.factors.qlib_alpha import list_builtin

    bad = []
    for item in list_builtin():
        want = translate(item["formula"])
        got = normalize(item["formula"])
        if got != want:
            bad.append((item["name"], item["formula"], want, got))
    assert not bad, f"归一化未走 qlib 翻译: {bad[:5]}"
