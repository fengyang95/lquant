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



def test_nested_cs_ts_materialization():
    """Rank(Ts_Mean(...)) —— CS 外层内嵌 TS 必须分步物化且产出因子列。

    回归：原 plan() 只处理 TS(CS) 方向，CS(TS) 返回空步骤列表，
    engine 静默返回没有因子列的 df（错不报错）。
    """
    import datetime as dt

    import numpy as np
    import polars as pl

    from lquant.factors.engine import FactorEngine

    rng = np.random.default_rng(3)
    rows = []
    for s in range(4):
        px = 10.0 + s
        for i in range(30):
            px *= 1 + rng.normal(0, 0.01)
            rows.append({"symbol": f"S{s}", "trade_date": dt.date(2025, 1, 1) + dt.timedelta(days=i),
                         "close": px})
    df = pl.DataFrame(rows)
    out = FactorEngine(df.lazy()).compute("Rank(Ts_Mean($close, 5))", "f")
    assert "f" in out.columns
    # 手算对照：S0 第 5 天的截面秩
    s0 = out.filter(pl.col("symbol") == "S0").sort("trade_date")
    expect = None
    vals = {}
    for sym in [f"S{i}" for i in range(4)]:
        sub = out.filter(pl.col("symbol") == sym).sort("trade_date")
        vals[sym] = sub["close"].rolling_mean(5).to_list()[4]
    r = vals["S0"]
    rank = 1 + sum(1 for v in vals.values() if v < r)
    assert s0["f"][4] == pytest.approx(float(rank))


def test_nested_ts_cs_materialization():
    """Ts_Mean(Rank($close), 5) —— TS 外层内嵌 CS 同样正确。"""
    import datetime as dt

    import polars as pl

    from lquant.factors.engine import FactorEngine

    rows = []
    for s in range(4):
        for i in range(30):
            rows.append({"symbol": f"S{s}", "trade_date": dt.date(2025, 1, 1) + dt.timedelta(days=i),
                         "close": 10.0 + s * 0.1 - 0.05 * i})
    df = pl.DataFrame(rows)
    out = FactorEngine(df.lazy()).compute("Ts_Mean(Rank($close), 5)", "f")
    assert "f" in out.columns
    assert out["f"].is_not_null().sum() > 0
