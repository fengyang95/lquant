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
