"""分组 IC：识破“只是小市值暴露”的因子。"""
from __future__ import annotations

import random

import polars as pl
import pytest

from lquant.factors.evaluate.group_ic import ic_by_group, size_group


def _df() -> pl.DataFrame:
    """6 股 20 天：小市值组内 mom 与收益完全正相关，大市值组内无关。

    小市值组（amount=1e7）3 只：mom = 当日序数（微调避免并列），ret = mom。
    大市值组（amount=1e9）3 只：ret 固定 seed 噪声，与 mom 无关。
    """
    random.seed(11)
    rows = []
    for d in range(1, 21):
        for i in range(3):
            # 小市值组：mom 与 ret 完全正相关
            mom = float(d) + i * 0.001
            rows.append({"symbol": f"S{i}", "trade_date": d,
                         "amount": 1e7, "size_cls": "small",
                         "mom": mom, "ret": mom})
        for i in range(3):
            rows.append({"symbol": f"B{i}", "trade_date": d,
                         "amount": 1e9, "size_cls": "big",
                         "mom": float(i) + 0.1 * d,
                         "ret": random.gauss(0, 1)})
    return pl.DataFrame(rows)


def test_small_cap_group_high_ic_big_cap_noise():
    # 每组每天仅 3 只 → min_obs 降到 2，否则 ic_series 的逐日 min_obs=5 会清空全部日期
    res = ic_by_group(_df(), "mom", "ret", "size_cls", min_obs=2)
    small = res.filter(pl.col("group") == "small")["ic_mean"][0]
    big = res.filter(pl.col("group") == "big")["ic_mean"][0]
    assert small > 0.9
    assert abs(big) < 0.3
    # min_obs 过滤：给定极小阈值时组直接被丢弃
    few = _df().head(4).with_columns(pl.lit("only").alias("size_cls"))
    assert ic_by_group(few, "mom", "ret", "size_cls", min_obs=5).is_empty()


def test_size_group_partitions_daily():
    df = size_group(_df(), mcap_col="amount", n_groups=3)
    assert set(df["size_q"].unique().to_list()) <= {1, 2, 3}
    # ordinal rank 打散同 amount 并列 → 6 只/日分成 3 组，每组 2 只
    counts = df.group_by(["trade_date", "size_q"]).len()
    assert set(counts["len"].to_list()) == {2}


def test_ic_by_group_missing_col_raises():
    with pytest.raises(KeyError):
        ic_by_group(_df(), "mom", "ret", "no_such_col")


def _null_size_df() -> pl.DataFrame:
    """120 股 10 天，一半市值缺失（size_q 为 null）。

    有效 60 只分 3 组 → 每组 20 只/日，满足 ic_by_group 透传给 ic_series 的 min_obs=5。
    """
    random.seed(3)
    rows = []
    for d in range(1, 11):
        for i in range(120):
            rows.append({
                "symbol": f"S{i}", "trade_date": d,
                "mom": random.gauss(0, 1), "ret": random.gauss(0, 1),
                "cov_market_cap": None if i % 2 == 0 else 1e9 * (i + 1),
            })
    return pl.DataFrame(rows)


def test_ic_by_group_skips_null_group_key():
    """未分组行（市值缺失 → size_q 为 null）不是「组」，不得产出 group=None 的行。

    回归：生产 API 用 ``f"size_q{int(r['group'])}"`` 渲染市值组，
    一旦 ic_by_group 漏出 None 组，int(None) 抛 TypeError 会让整个分组 IC 面板报错。
    断死「恰好 3 个有效市值组」：既防漏出 None，也防一刀切把有效组砍掉。
    """
    ds = size_group(_null_size_df(), mcap_col="cov_market_cap", n_groups=3)
    out = ic_by_group(ds, "mom", "ret", "size_q", min_obs=5)
    assert set(out["group"].to_list()) == {1, 2, 3}


def _null_industry_df() -> pl.DataFrame:
    """10 天 × 4 组各 6 只：A/B/C 三个行业 + 一组行业缺失（null）。

    null 组每天 6 只，足够越过 min_obs=2 —— 只有新增的 null 跳过能把它滤掉。
    """
    random.seed(5)
    rows = []
    for d in range(1, 11):
        for ind in ("A", "B", "C", None):
            for k in range(6):
                rows.append({
                    "symbol": f"{ind}-{k}", "trade_date": d, "industry": ind,
                    "mom": random.gauss(0, 1), "ret": random.gauss(0, 1),
                })
    return pl.DataFrame(rows)


def test_ic_by_group_skips_null_string_group():
    """行业列为 null 的行同样不是分组。"""
    out = ic_by_group(_null_industry_df(), "mom", "ret", "industry", min_obs=2)
    assert set(out["group"].to_list()) == {"A", "B", "C"}


def test_ic_by_group_skips_nan_group_key():
    """float 分组列里的 NaN 也不是分组（polars 会把 NaN 当独立分组吐出）。"""
    random.seed(9)
    rows = []
    for d in range(1, 11):
        for g in (1.0, 2.0, float("nan")):
            for _ in range(6):
                rows.append({"trade_date": d, "g": g,
                             "mom": random.gauss(0, 1), "ret": random.gauss(0, 1)})
    out = ic_by_group(pl.DataFrame(rows), "mom", "ret", "g", min_obs=2)
    assert set(out["group"].to_list()) == {1.0, 2.0}
