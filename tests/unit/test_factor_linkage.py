"""因子联动：compute_factor_columns + JQRunner.get_factor_values。"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from lquant.backtest.jqapi import JQRunner
from lquant.factors.panel import compute_factor_columns

BUILTIN = "ROC5"  # 真实内置动量族因子：Ref($close,5)/$close
DSL_EXPR = "Ts_Mean($close,5)/$close"


def make_df(days: int = 40) -> pl.DataFrame:
    """40 日 × 2 标的 OHLCV；600000.SH 每日 +2%，000001.SZ 每日 -1%。"""
    rows = []
    px = {"600000.SH": 100.0, "000001.SZ": 50.0}
    for i in range(days):
        d = date.fromordinal(date(2026, 1, 5).toordinal() + i)
        for s, base in px.items():
            p = base * (1.02**i) if s.startswith("6") else base * (0.99**i)
            pre = (
                p
                if i == 0
                else (base * (1.02 ** (i - 1)) if s.startswith("6") else base * (0.99 ** (i - 1)))
            )
            rows.append(
                dict(
                    trade_date=d,
                    symbol=s,
                    open=p,
                    high=p * 1.005,
                    low=p * 0.995,
                    close=p,
                    pre_close=pre,
                    volume=1e8,
                    amount=p * 1e8,
                )
            )
    return pl.DataFrame(rows)


# ---------------------------------------------------------------- panel.py


def test_panel_builtin_and_dsl_mapping():
    df, colmap = compute_factor_columns(make_df(), [BUILTIN, DSL_EXPR])
    assert colmap == {BUILTIN: f"_f_{BUILTIN}", DSL_EXPR: f"_f_{DSL_EXPR}"}
    for col in colmap.values():
        assert col in df.columns
        s = df[col]
        assert s.null_count() < len(df)  # 无全空列
    # 手算对照：600000.SH 第 10 日 ROC5 = close[i-5]/close[i] = 1.02**-5
    row = df.filter(
        (pl.col("symbol") == "600000.SH")
        & (pl.col("trade_date") == date(2026, 1, 5).fromordinal(date(2026, 1, 5).toordinal() + 10))
    )
    assert row[f"_f_{BUILTIN}"][0] == pytest.approx(1.02**-5)


def test_panel_invalid_formula_raises_valueerror():
    with pytest.raises(ValueError, match="not_exist_99"):
        compute_factor_columns(make_df(), ["not_exist_99"])


# ---------------------------------------------------------------- JQ API


def test_get_factor_values_in_runner():
    code = f"""
def initialize(context):
    set_benchmark('000300.SH')

def handle_data(context):
    v = get_factor_values('{BUILTIN}', ['600000.SH'], count=3)
    if v['600000.SH'] and v['600000.SH'][-1] < 1.0:
        order('600000.SH', 100)
"""
    res = JQRunner(code, factor_formulas=[BUILTIN]).run(make_df())
    assert res.error is None
    assert res.metrics["n_trades"] >= 1


def test_get_factor_values_no_lookahead_and_truncation():
    """count=3 最多 3 条且严格截至当日；末日值 = ROC5 = 1.02**-5（无未来）。"""
    code = f"""
def handle_data(context):
    v = get_factor_values('{BUILTIN}', ['600000.SH'], count=3)
    ns_ = len(v['600000.SH'])
    record(n_vals=ns_)
    if ns_:
        record(last=v['600000.SH'][-1])
"""
    res = JQRunner(code, factor_formulas=[BUILTIN]).run(make_df())
    assert res.error is None
    ns = res.records["n_vals"]
    assert max(n for _, n in ns) <= 3
    assert ns[-1][1] == 3  # 窗口填满后每日 3 条
    last = res.records["last"][-1][1]
    assert last == pytest.approx(1.02**-5)


def test_get_factor_values_unregistered_raises():
    code = """
def handle_data(context):
    get_factor_values('ROC5', ['600000.SH'], count=1)
"""
    res = JQRunner(code).run(make_df())
    assert res.error is not None
    assert "factor_formulas" in res.error


def test_factor_formulas_with_dict_data_raises_early():
    r = JQRunner("def handle_data(c, d):\n    pass\n", factor_formulas=[BUILTIN])
    with pytest.raises(ValueError, match="factor_formulas"):
        r.run({date(2026, 1, 5): {}})
