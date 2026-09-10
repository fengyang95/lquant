"""因子评估链路 golden 测试：固定小数据集 → 固定期望值。

固定输入（5 只股票 × 10 天）下的 forward_return / ic_summary / add_quantile
期望值已写死（golden 固化于 2026-09-09）。之后任何 op/评估行为变化都会导致
这里 FAIL —— 防止“重构悄悄改语义”。

数据形状（人工可推导）：
- A 等比 +10%/天（mom 恒为 1.1^3-1=0.331，fwd_ret_1 恒 0.1，两者都最大）
- B 平盘（mom=0，fwd=0）
- C 等比 -10%/天（mom=-0.271，fwd=-0.1，两者都最小）
- D 锯齿 9↔11（mom/fwd 逐日 ±0.222/±0.182 交替）
- E 缓涨（mom≈0.029，fwd≈0.0095）
"""
from __future__ import annotations

import polars as pl
import pytest

from lquant.factors.evaluate.ic import ic_summary
from lquant.factors.evaluate.quantile import add_quantile
from lquant.factors.evaluate.returns import forward_return


def _build_df() -> pl.DataFrame:
    """手工可验证的确定性数据：5 只股票 × 10 天。

    mom 列 = close/close.shift(3).over("symbol") - 1，在测试输入构造时现算
    （这是测试输入构造，不是被测代码），保证与 close 逐行对齐。
    """
    prices = {
        "A": [10.0 * 1.1 ** i for i in range(10)],
        "B": [10.0] * 10,
        "C": [10.0 * 0.9 ** i for i in range(10)],
        "D": [10.0 + (1.0 if i % 2 else -1.0) for i in range(10)],
        "E": [10.0 + 0.1 * i for i in range(10)],
    }
    df = pl.DataFrame([
        {"symbol": s, "trade_date": d, "close": prices[s][d - 1]}
        for d in range(1, 11) for s in ["A", "B", "C", "D", "E"]
    ])
    return df.with_columns(
        (pl.col("close") / pl.col("close").shift(3).over("symbol") - 1).alias("mom")
    )


DF = _build_df()


def test_golden_fwd_ret_1_pointwise():
    """fwd_ret_1 逐点值（A/B/C 三只）+ 尾行 null。"""
    out = forward_return(DF, periods=[1])
    golden = {
        "A": [0.1] * 9 + [None],
        "B": [0.0] * 9 + [None],
        "C": [-0.1] * 9 + [None],
    }
    for sym, expected in golden.items():
        got = out.filter(pl.col("symbol") == sym).sort("trade_date")["fwd_ret_1"].to_list()
        assert len(got) == 10
        for g, e in zip(got[:-1], expected[:-1], strict=False):
            assert g == pytest.approx(e, rel=1e-9)
        assert got[-1] is None  # 尾行无未来 → null
    # 方向性：D 锯齿 ±0.222/±0.182 交替，E 缓涨 ≈ 0.0095
    d = out.filter(pl.col("symbol") == "D").sort("trade_date")["fwd_ret_1"].to_list()
    assert d[0] == pytest.approx(11.0 / 9.0 - 1, rel=1e-9)
    assert d[1] == pytest.approx(9.0 / 11.0 - 1, rel=1e-9)
    e = out.filter(pl.col("symbol") == "E").sort("trade_date")["fwd_ret_1"].to_list()
    assert e[0] == pytest.approx(10.1 / 10.0 - 1, rel=1e-9)


def test_golden_ic_summary():
    """IC 汇总：均值/正比例/天数写死；rank_ic 恒为 0.4（截面序恒定）。"""
    out = forward_return(DF, periods=[1])
    s = ic_summary(out, factor="mom", ret_col="fwd_ret_1")
    ic, rank = s["ic"], s["rank_ic"]
    assert ic["mean"] == pytest.approx(0.26170636342268244, rel=1e-9)
    assert rank["mean"] == pytest.approx(0.39999999999999997, rel=1e-9)
    assert ic["positive_rate"] == pytest.approx(1.0, rel=1e-9)
    assert rank["positive_rate"] == pytest.approx(1.0, rel=1e-9)
    # 日期 1-3 mom 为 null，日期 10 fwd_ret 为 null → 只剩 4..9 共 6 天
    assert ic["n_days"] == 6
    assert rank["n_days"] == 6


def test_golden_add_quantile():
    """每日 q 分布：mom 非空的 7 天，每天 5 只按 ordinal rank 映射到 {2,4,6,8,10}。"""
    out = forward_return(DF, periods=[1])
    q = add_quantile(out, factor="mom", n_groups=10)
    # 日期 1-3 mom 全 null → q 为 null；4-10 每天 5 个有效值
    valid = q.filter(pl.col("q").is_not_null())
    assert valid["trade_date"].n_unique() == 7
    counts = valid.group_by("q").len().sort("q")
    assert counts["q"].to_list() == [2, 4, 6, 8, 10]
    assert counts["len"].to_list() == [7, 7, 7, 7, 7]
    # 排序方向：每日 mom 最大的 A 拿最高组 10，最小的 C 拿最低组 2
    d10 = q.filter(pl.col("trade_date") == 10).sort("mom", descending=True)
    assert d10["q"].to_list() == [10, 8, 6, 4, 2]   # A > D > E > B > C
    assert d10["symbol"].to_list() == ["A", "D", "E", "B", "C"]
