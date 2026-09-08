import pytest

from lquant.core.errors import FactorError, LookaheadError
from lquant.factors.dsl.analyzer import check
from lquant.factors.dsl.compiler import has_nested_ts_cs
from lquant.factors.dsl.parser import parse
from lquant.factors.ops import cs_ops, ts_ops  # noqa: F401  注册算子


def test_parse_and_analyze():
    ast = parse("Rank(Ts_Mean($close, 5) / $close - 1)", "f1")
    check(ast)
    assert ast.min_window == 5
    assert "close" in ast.fields


def test_unknown_op():
    from lquant.factors.dsl.parser import parse
    from lquant.factors.dsl.analyzer import check

    with pytest.raises(FactorError):
        check(parse("NoSuchOp($close)", "bad"))


def test_ts_cs_nesting_detected():
    # TS 内嵌 CS：必须分步物化（Polars issue #25691）
    ast = parse("Ts_Mean(Rank($close), 5)", "f2")
    assert has_nested_ts_cs(ast.root)
