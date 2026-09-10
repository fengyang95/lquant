"""报告增强：NW t 值卡片、分组 IC / 换手率 / 成本敏感性节 + 向后兼容。"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from lquant.factors.evaluate.report import factor_report
from lquant.factors.evaluate.returns import forward_return

# 18 股 × 3 行业（每组 6 只 ≥ min_obs=5），组内每日样本足够算 IC
_SPEC = [  # (symbol, 每日漂移, amount, 行业)
    ("A1", 0.05, 1e7, "银行"), ("A2", 0.04, 2e7, "银行"),
    ("A3", 0.03, 3e7, "银行"), ("A4", 0.02, 4e7, "银行"),
    ("A5", 0.018, 4.5e7, "银行"), ("A6", 0.016, 5e7, "银行"),
    ("B1", 0.02, 5e8, "医药"), ("B2", 0.015, 6e8, "医药"),
    ("B3", 0.01, 7e8, "医药"), ("B4", 0.005, 8e8, "医药"),
    ("B5", 0.004, 8.5e8, "医药"), ("B6", 0.003, 9e8, "医药"),
    ("C1", 0.004, 9e8, "钢铁"), ("C2", 0.003, 1e9, "钢铁"),
    ("C3", 0.002, 1.1e9, "钢铁"), ("C4", 0.001, 1.2e9, "钢铁"),
    ("C5", 0.0008, 1.3e9, "钢铁"), ("C6", 0.0005, 1.4e9, "钢铁"),
]


def _df(n: int = 30) -> pl.DataFrame:
    d0 = date(2024, 1, 2)
    rows = []
    for s, drift, amt, ind in _SPEC:
        for i in range(n):
            rows.append({
                "symbol": s,
                "trade_date": d0 + timedelta(days=i),
                "close": 10.0 * (1.0 + drift) ** i,
                "amount": amt,
                "industry_sw1": ind,
            })
    df = pl.DataFrame(rows)
    df = df.with_columns(
        (pl.col("close") / pl.col("close").shift(3).over("symbol") - 1).alias("mom"))
    return forward_return(df, periods=[1])


def test_report_contains_new_sections():
    html = factor_report(_df(), "mom", "fwd_ret_1", group_col="industry_sw1",
                         bps_list=[0.0, 15.0, 30.0])
    assert "NW" in html
    assert "分组 IC" in html
    assert "换手率" in html
    assert "成本敏感性" in html
    assert "<table>" in html
    assert "viable" in html


def test_report_rolling_section():
    """滚动窗口章节：默认窗口 60 会因样本不足而显示空态。"""
    html = factor_report(_df(), "mom", "fwd_ret_1")
    assert "滚动窗口" in html
    assert "数据不足：滚动 RankIC" in html


def test_report_rolling_section_with_data():
    """样本足够时滚动章节有 SVG 折线。"""
    html = factor_report(_df(n=90), "mom", "fwd_ret_1")
    assert "滚动窗口" in html
    assert "<svg" in html.split("滚动窗口")[1]  # 滚动章节内有图
    assert "数据不足" not in html.split("滚动窗口")[1]


def test_report_backward_compatible():
    html = factor_report(_df(), "mom", "fwd_ret_1")
    assert "<html" in html
    assert "分组 IC" not in html      # group_col 缺省不出现该节
    assert "成本敏感性" not in html    # bps_list 缺省不出现该节
    assert "换手率" in html


def test_report_cum_ic_chart_has_data():
    """回归：ic_summary 不返回 series 曾导致累计 IC 图静默空白。"""
    html = factor_report(_df(), "mom", "fwd_ret_1")
    assert "累计 IC" in html
    assert "数据不足：累计 IC" not in html


def test_report_group_col_missing_column():
    """group_col 指定但列不存在 → 静默跳过该节，不抛错。"""
    html = factor_report(_df(), "mom", "fwd_ret_1", group_col="not_a_col")
    assert "分组 IC" not in html
    assert "<html" in html
