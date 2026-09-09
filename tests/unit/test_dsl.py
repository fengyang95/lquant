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


def test_compile_window_arg_is_python_int():
    """数字实参（窗口）必须传 Python int，不能是 pl.lit —— 否则实算是崩的。"""
    import datetime as dt

    import polars as pl

    from lquant.factors.dsl.compiler import compile_expr

    df = pl.DataFrame({
        "trade_date": [dt.date(2026, 3, 2), dt.date(2026, 3, 3)],
        "symbol": ["S000", "S000"], "close": [10.0, 10.5],
    })
    out = df.with_columns(compile_expr(parse("Ts_Mean($close, 2)", "f").root).alias("m"))
    assert "m" in out.columns
    # 窗口 2 在 2 行上第 2 行即可算出；若窗口实参是 Expr，这里会抛 TypeError 而非产出
    assert out["m"].fill_null(0.0).max() > 0
