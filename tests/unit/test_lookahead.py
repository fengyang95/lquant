"""前视偏差防线：TS 算子因果性 + forward_return 前瞻语义。

TS 算子必须“只看过去”：截断到任意 T 日重算，T 日之前的值必须逐位一致。
forward_return 必须把未来 h 期收益对齐到当前行（shift(-h)）——
这两条不变量是 IC 不虚高的根基。
"""
from __future__ import annotations

import polars as pl
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from lquant.factors.engine import FactorEngine
from lquant.factors.evaluate.returns import forward_return

SYMBOLS = ["A", "B", "C", "D", "E"]
DATES = [d for d in range(1, 61)]          # 60 个交易日
TS_EXPRS = [
    "Ts_Mean($close,5)", "Ts_Std($close,10)", "Ts_Return($close,1)",
    "Ts_Delay($close,3)", "Ts_Sum($close,7)",
]


def _make_df(price_lists: list[list[float]], symbols: list[str], n_dates: int) -> pl.DataFrame:
    rows = [{"symbol": s, "trade_date": d, "close": price_lists[i][d - 1]}
            for i, s in enumerate(symbols) for d in range(1, n_dates + 1)]
    return pl.DataFrame(rows)


# 每只标的一条 60 日价格序列（st.dictionaries 生成 300 键太慢，会触发 too_slow）
_price_lists = st.lists(
    st.floats(1.0, 100.0, allow_nan=False, allow_infinity=False),
    min_size=60, max_size=60,
)
prices = st.tuples(*[_price_lists] * len(SYMBOLS))


@settings(max_examples=15, deadline=None)
@given(price_lists=prices, cut=st.integers(25, 55))
def test_ts_ops_causal_truncation_invariance(price_lists, cut):
    """截断到 cut 日重算，cut 日之前的值必须完全一致（含 NaN 位置）。"""
    full = _make_df(list(price_lists), SYMBOLS, 60)
    cut_df = full.filter(pl.col("trade_date") <= cut)
    for expr in TS_EXPRS:
        a = FactorEngine(full.lazy()).compute(expr, name="f").sort(["symbol", "trade_date"])
        b = FactorEngine(cut_df.lazy()).compute(expr, name="f").sort(["symbol", "trade_date"])
        head_a = a.filter(pl.col("trade_date") <= cut)["f"]
        head_b = b["f"]
        assert head_a.null_count() == head_b.null_count(), expr
        va, vb = head_a.drop_nulls(), head_b.drop_nulls()
        assert len(va) == len(vb)
        if len(va):
            assert (va - vb).abs().max() < 1e-12, expr


@settings(max_examples=15, deadline=None)
@given(price_lists=prices, h=st.integers(1, 5), cut=st.integers(30, 55))
def test_forward_return_lookahead_alignment(price_lists, h, cut):
    """fwd_ret_h(T) == close(T+h)/close(T) - 1，且截断后 T-1 期结果不变。"""
    full = _make_df(list(price_lists), SYMBOLS, 60)
    out = forward_return(full, periods=[h])
    col = f"fwd_ret_{h}"
    # 手工逐点核对
    for s in ["A", "B"]:
        sub = out.filter(pl.col("symbol") == s).sort("trade_date")
        closes = sub["close"].to_list()
        got = sub[col].to_list()
        for i in range(len(closes) - h):
            if closes[i + h] is not None:
                assert got[i] == pytest.approx(closes[i + h] / closes[i] - 1, rel=1e-12)
        assert got[-h:] == [None] * h           # 尾部 h 期必须是 null（无未来）
    # 截断不变：截断会让尾部 h 行失去未来，所以只断言 trade_date <= cut-h 的行一致
    cut_df = full.filter(pl.col("trade_date") <= cut)
    out_cut = forward_return(cut_df, periods=[h])
    a = out.filter(pl.col("trade_date") <= cut - h).sort(["symbol", "trade_date"])[col]
    b = out_cut.filter(pl.col("trade_date") <= cut - h).sort(["symbol", "trade_date"])[col]
    assert a.null_count() == b.null_count()
    assert (a.drop_nulls() - b.drop_nulls()).abs().max() < 1e-12
