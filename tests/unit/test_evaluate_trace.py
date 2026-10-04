"""截面快照 trace（Phase 3.3）：某期某箱的成分与收益明细。

最关键的两条断言：

1. **与 ``quantile.py`` 同口径**：trace 选出的成分必须与 ``add_quantile``
   的分箱结果逐 symbol 一致。两套分箱会造出「曲线上第 10 组赚钱、快照里
   第 10 组却是另一批票」—— 比没有 trace 更危险。
2. **无未来函数**：成分筛选只能用目标日的因子值。改未来价格不得改变当日
   成分（前瞻收益本身是未来信息，那是 label 的正常用法，不在此列）。
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from lquant.core.errors import FactorError
from lquant.factors.evaluate import add_quantile, trace_periods, trace_snapshot

D0 = date(2026, 1, 5)


def _panel(n_days: int = 40, n_sym: int = 20, seed: int = 4) -> pl.DataFrame:
    """确定性面板：因子与未来收益正相关（便于断言方向）。"""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n_days):
        d = D0 + timedelta(days=i)
        for j in range(n_sym):
            f = float(rng.normal(0, 1))
            rows.append({"trade_date": d, "symbol": f"S{j:02d}", "factor": f,
                         "close": 10.0 + j})
    df = pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))
    # 前瞻收益 = 因子 * 0.01 + 噪声（确定性）
    rng2 = np.random.default_rng(seed + 1)
    return df.with_columns(
        pl.Series("fwd_ret_1", 0.01 * df["factor"].to_numpy()
                  + rng2.normal(0, 0.001, len(df)))).with_columns(
        pl.col("trade_date").cast(pl.Date))


# ----------------------------------------------------------- 与 quantile 同口径

def test_members_match_add_quantile_exactly():
    """成分必须与 quantile.add_quantile 的分箱逐 symbol 一致（同口径硬要求）。"""
    df = _panel()
    d = sorted(df["trade_date"].unique().to_list())[10]
    for side, group in (("long", 10), ("short", 1)):
        out = trace_snapshot(df, "factor", date=d, bins=10, side=side)
        tagged = add_quantile(df.filter(pl.col("trade_date") == d), "factor", 10)
        expect = set(tagged.filter(pl.col("q") == group)["symbol"].to_list())
        got = {m["symbol"] for m in out["members"]}
        assert got == expect
        assert out["group"] == group


def test_group_numbering_matches_quantile_convention():
    """1 = 因子最低、bins = 最高（与 quantile.py 的约定一致）。"""
    df = _panel()
    d = sorted(df["trade_date"].unique().to_list())[5]
    low = trace_snapshot(df, "factor", date=d, side="short")
    high = trace_snapshot(df, "factor", date=d, side="long")
    assert low["group"] == 1 and high["group"] == 10
    assert low["mean_factor"] < high["mean_factor"]


def test_top_and_long_are_synonyms():
    df = _panel()
    d = sorted(df["trade_date"].unique().to_list())[3]
    a = trace_snapshot(df, "factor", date=d, side="long")
    b = trace_snapshot(df, "factor", date=d, side="top")
    assert {m["symbol"] for m in a["members"]} == {m["symbol"] for m in b["members"]}
    c = trace_snapshot(df, "factor", date=d, side="bottom")
    assert c["group"] == 1


def test_bins_count_affects_grouping():
    """bins 变了，同一侧取到的成分也应随之变化（分箱数真的生效）。"""
    df = _panel()
    d = sorted(df["trade_date"].unique().to_list())[7]
    m10 = {m["symbol"] for m in trace_snapshot(df, "factor", date=d, bins=10)["members"]}
    m4 = {m["symbol"] for m in trace_snapshot(df, "factor", date=d, bins=4)["members"]}
    assert m4 != m10 and len(m4) > len(m10)


# ----------------------------------------------------------- 手算对拍

def test_members_and_contribution_hand_checked():
    """小型确定性面板，手算分箱与贡献。"""
    df = pl.DataFrame({
        "trade_date": [D0] * 5,
        "symbol": ["a", "b", "c", "d", "e"],
        "factor": [1.0, 2.0, 3.0, 4.0, 5.0],
        "fwd_ret_1": [0.01, 0.02, 0.03, 0.04, 0.05],
    }).with_columns(pl.col("trade_date").cast(pl.Date))

    out = trace_snapshot(df, "factor", date=D0, bins=5, side="long")
    # 第 5 箱只有 e
    assert out["group"] == 5
    assert out["n_members"] == 1 and out["n_symbols"] == 5
    assert out["members"][0]["symbol"] == "e"
    assert out["members"][0]["factor"] == pytest.approx(5.0)
    assert out["members"][0]["return"] == pytest.approx(0.05)
    assert out["members"][0]["contribution"] == pytest.approx(0.05)
    assert out["mean_return"] == pytest.approx(0.05)
    assert out["total_contribution"] == pytest.approx(0.05)
    assert out["weight_scheme"] == "equal_within_group"


def test_short_side_hand_checked_with_equal_weights():
    df = pl.DataFrame({
        "trade_date": [D0] * 4,
        "symbol": ["a", "b", "c", "d"],
        "factor": [1.0, 2.0, 3.0, 4.0],
        "fwd_ret_1": [0.02, 0.04, 0.06, 0.08],
    }).with_columns(pl.col("trade_date").cast(pl.Date))
    out = trace_snapshot(df, "factor", date=D0, bins=2, side="short")
    assert out["group"] == 1
    assert {m["symbol"] for m in out["members"]} == {"a", "b"}
    assert out["mean_return"] == pytest.approx(0.03)          # (0.02+0.04)/2
    for m in out["members"]:
        assert m["contribution"] == pytest.approx(m["return"] * 0.5)


def test_members_sorted_by_factor_within_group():
    df = _panel()
    d = sorted(df["trade_date"].unique().to_list())[12]
    long_m = trace_snapshot(df, "factor", date=d, side="long")["members"]
    assert [m["factor"] for m in long_m] == sorted(
        [m["factor"] for m in long_m], reverse=True)
    short_m = trace_snapshot(df, "factor", date=d, side="short")["members"]
    assert [m["factor"] for m in short_m] == sorted([m["factor"] for m in short_m])


def test_top_truncates_members_but_not_summary():
    df = _panel(n_sym=40)
    d = sorted(df["trade_date"].unique().to_list())[4]
    full = trace_snapshot(df, "factor", date=d, bins=4, side="long")
    cut = trace_snapshot(df, "factor", date=d, bins=4, side="long", top=3)
    assert len(cut["members"]) == 3 and cut["returned"] == 3
    assert cut["n_members"] == full["n_members"]         # 汇总仍反映全箱
    assert cut["mean_return"] == pytest.approx(full["mean_return"])
    assert cut["members"][0] == full["members"][0]       # 仍是因子最高那几只


# ----------------------------------------------------------- 无未来函数

def test_future_prices_do_not_change_membership():
    """改未来价格不得改变当日成分（成分只能用当日因子值）。"""
    df = _panel()
    days = sorted(df["trade_date"].unique().to_list())
    d = days[10]
    base = trace_snapshot(df, "factor", date=d, side="long")
    base_syms = {m["symbol"] for m in base["members"]}

    future = [x for x in days if x > d]
    mutated = df.with_columns(
        pl.when(pl.col("trade_date").is_in(future))
        .then(pl.col("close") * 100.0)
        .otherwise(pl.col("close")).alias("close"),
        pl.when(pl.col("trade_date").is_in(future))
        .then(pl.lit(9.99)).otherwise(pl.col("fwd_ret_1")).alias("fwd_ret_1"),
    )
    after = trace_snapshot(mutated, "factor", date=d, side="long")
    assert {m["symbol"] for m in after["members"]} == base_syms
    assert after["mean_factor"] == pytest.approx(base["mean_factor"])


def test_forward_return_is_derived_when_missing():
    """没有 fwd_ret_N 时按 close 现算（label 来自未来是评价的正常用法）。"""
    df = _panel().drop("fwd_ret_1")
    d = sorted(df["trade_date"].unique().to_list())[10]
    out = trace_snapshot(df, "factor", date=d, side="long", horizon=1)
    assert out["n_members"] > 0
    assert out["mean_return"] is not None


# ----------------------------------------------------------- 边界与错误

def test_unknown_date_lists_nearby_dates():
    df = _panel()
    with pytest.raises(FactorError, match="不在数据里"):
        trace_snapshot(df, "factor", date=date(2030, 1, 1))


def test_bad_params_raise():
    df = _panel()
    d = sorted(df["trade_date"].unique().to_list())[0]
    with pytest.raises(FactorError, match="bins 必须"):
        trace_snapshot(df, "factor", date=d, bins=1)
    with pytest.raises(FactorError, match="未知方向"):
        trace_snapshot(df, "factor", date=d, side="middle")
    with pytest.raises(FactorError, match="horizon 必须"):
        trace_snapshot(df, "factor", date=d, horizon=0)
    with pytest.raises(FactorError, match="因子列不存在"):
        trace_snapshot(df, "nope", date=d)
    with pytest.raises(FactorError, match="日期列不存在"):
        trace_snapshot(df, "factor", date=d, by="nope")
    with pytest.raises(FactorError, match="标的列不存在"):
        trace_snapshot(df, "factor", date=d, symbol_col="nope")
    with pytest.raises(FactorError, match="日期格式非法"):
        trace_snapshot(df, "factor", date="not-a-date")


def test_empty_group_returns_empty_members_but_full_summary():
    """因子几乎全相同（rank 分箱下末箱可能为空）时不能 KeyError。"""
    df = pl.DataFrame({
        "trade_date": [D0] * 4,
        "symbol": ["a", "b", "c", "d"],
        "factor": [1.0, 1.0, 1.0, 1.0],
        "fwd_ret_1": [0.01, 0.02, 0.03, 0.04],
    }).with_columns(pl.col("trade_date").cast(pl.Date))
    out = trace_snapshot(df, "factor", date=D0, bins=10, side="long")
    assert out["n_symbols"] == 4
    assert isinstance(out["members"], list)
    assert out["mean_return"] is None or isinstance(out["mean_return"], float)
    assert len(out["groups"]) >= 1


def test_single_symbol_cross_section():
    df = pl.DataFrame({
        "trade_date": [D0],
        "symbol": ["only"],
        "factor": [0.5],
        "fwd_ret_1": [0.03],
    }).with_columns(pl.col("trade_date").cast(pl.Date))
    out = trace_snapshot(df, "factor", date=D0, bins=5, side="long")
    assert out["n_symbols"] == 1 and out["n_members"] == 1
    assert out["members"][0]["symbol"] == "only"


def test_result_is_json_serializable():
    df = _panel()
    d = sorted(df["trade_date"].unique().to_list())[9]
    out = trace_snapshot(df, "factor", date=d, side="long", top=5)
    text = json.dumps(out)                      # 不炸即通过（NaN 会让 json 产出非法字面量）
    assert "NaN" not in text and "Infinity" not in text


# ----------------------------------------------------------- trace_periods

def test_trace_periods_ranks_by_absolute_return():
    df = _panel(n_days=20)
    rows = trace_periods(df, "factor", side="long", bins=5, limit=5)
    assert len(rows) == 5
    assert all(r["group"] == 5 for r in rows)
    abs_rets = [abs(r["return"]) for r in rows]
    assert abs_rets == sorted(abs_rets, reverse=True)


def test_trace_periods_short_side_uses_lowest_group():
    df = _panel(n_days=20)
    rows = trace_periods(df, "factor", side="short", bins=5, limit=3)
    assert all(r["group"] == 1 for r in rows)


def test_trace_periods_requires_factor_and_return():
    df = _panel()
    with pytest.raises(FactorError, match="因子列不存在"):
        trace_periods(df, "nope")
    with pytest.raises(FactorError, match="缺少收益列"):
        trace_periods(df.drop("fwd_ret_1").drop("close"), "factor")
