"""Top-N 持仓收缩测试：头部集中度对收益/超额/换手的影响。"""
from __future__ import annotations

import polars as pl
import pytest

from lquant.factors.evaluate.top_n import top_n_returns, top_n_summary


def _df() -> pl.DataFrame:
    """3 天 × 4 只：因子值固定排序 A>B>C>D，收益 = 因子序号 × 1%。

    Top2 组合每日收益 = (4% + 3%) / 2 = 3.5%；截面等权基准 = 2.5%。
    """
    rows = []
    for d in range(1, 4):
        for i, s in enumerate("ABCD"):
            rows.append({"symbol": s, "trade_date": d, "f": 4.0 - i,
                         "fwd_ret_1": 0.01 * (4 - i)})
    return pl.DataFrame(rows)


def test_top_n_picks_highest_factor():
    tr = top_n_returns(_df(), "f", "fwd_ret_1", n=2)
    assert tr["ret"].to_list() == pytest.approx([0.035] * 3)
    assert tr["members"].to_list() == [["A", "B"]] * 3


def test_top_n_ascending_option():
    tr = top_n_returns(_df(), "f", "fwd_ret_1", n=1, descending=False)
    assert tr["members"].to_list() == [["D"]] * 3
    assert tr["ret"].to_list() == pytest.approx([0.01] * 3)


def test_top_n_summary_excess_and_turnover():
    out = top_n_summary(_df(), "f", "fwd_ret_1", n_list=[1, 2])
    by_n = {r["n"]: r for r in out.to_dicts()}
    assert by_n[1]["annual_return"] > by_n[2]["annual_return"] > 0
    # Top2(3.5%) 跑赢等权基准(2.5%) → 年化超额 > 0；Top1 更高
    assert by_n[2]["annual_excess"] > 0
    assert by_n[1]["annual_excess"] > by_n[2]["annual_excess"]
    assert by_n[2]["annual_turnover"] == pytest.approx(0.0)   # 成员不变 → 零换手
    assert by_n[2]["n_periods"] == 3


def test_top_n_turnover_counts_replacement():
    # 第 2 天起因子排序反转 → Top1 从 A 换成 D，日均换手 = 1/2 天 = 0.5
    df = _df()
    df = pl.concat([
        df.filter(pl.col("trade_date") == 1),
        df.filter(pl.col("trade_date") > 1).with_columns(
            pl.col("f").max().over("trade_date").sub(pl.col("f")).add(1).alias("f")),
    ])
    out = top_n_summary(df, "f", "fwd_ret_1", n_list=[1])
    assert out["annual_turnover"][0] == pytest.approx(0.5 * 252)
