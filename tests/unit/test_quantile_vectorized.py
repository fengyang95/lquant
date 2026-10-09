"""向量化改造的等价性测试：新实现（polars pivot/join）必须与旧实现（python 逐日循环）逐值一致。

这些断言就是这次性能改造的"安全网"——重写热点时最容易出的错是口径悄悄变了
（少算一天、换手分母换了、空组处理不同），数值一致性测出来才敢合并。
"""
from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from lquant.core.errors import DataQualityError
from lquant.factors.evaluate.costs import factor_turnover
from lquant.factors.evaluate.excess import benchmark_series, quantile_excess_nav
from lquant.factors.evaluate.quantile import (
    account_samples,
    add_quantile,
    group_returns,
    long_short_nav,
    pivot_group_returns,
    quantile_nav,
    quantile_summary,
)


def _panel(n_days: int = 25, n_sym: int = 20, seed: int = 3) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n_sym):
        px = 10.0 + i
        for d in range(n_days):
            px *= (1 + rng.normal(0, 0.02))
            rows.append({"trade_date": d, "symbol": f"S{i:03d}", "close": px,
                         "factor": float(i) + rng.normal(0, 0.5)})
    df = pl.DataFrame(rows).sort(["symbol", "trade_date"])
    return df.with_columns(
        (pl.col("close").shift(-1).over("symbol") / pl.col("close") - 1).alias("fwd_ret_1")
    ).drop_nulls("fwd_ret_1")


# ---------- 旧实现（逐日 python 循环），仅用于对账 ----------

def _old_long_short_nav(df, factor, ret_col, n_groups, date_col="trade_date",
                        top=None, bottom=None):
    top = top or n_groups
    bottom = bottom or 1
    g = group_returns(df, factor, ret_col, n_groups, date_col=date_col)
    dates = sorted(g[date_col].unique().to_list())
    hi, lo = {}, {}
    for d in dates:
        sub = g.filter(pl.col(date_col) == d)
        h = sub.filter(pl.col("q") == top)
        l = sub.filter(pl.col("q") == bottom)
        hi[d] = float(h["ret"][0]) if len(h) else 0.0
        lo[d] = float(l["ret"][0]) if len(l) else 0.0
    ls = [hi[d] - lo[d] for d in dates]
    return pl.DataFrame({
        date_col: dates,
        "ret_long": [hi[d] for d in dates],
        "ret_short": [lo[d] for d in dates],
        "ret_long_short": ls,
        "nav_long_short": list(np.cumprod(1 + np.array(ls))),
    })


def _old_quantile_nav(df, factor, ret_col, n_groups, date_col="trade_date"):
    g = group_returns(df, factor, ret_col, n_groups, date_col=date_col)
    dates = g.filter(pl.col("q") == 1).sort(date_col)[date_col].to_list()
    out = pl.DataFrame({date_col: dates})
    for q in range(1, n_groups + 1):
        sub = g.filter(pl.col("q") == q).sort(date_col)
        nav = np.nan_to_num(np.cumprod(1 + sub["ret"].to_numpy()), nan=0.0)
        out = out.with_columns(pl.Series(f"q{q}", list(nav)))
    return out.with_columns((pl.col(f"q{n_groups}") - pl.col("q1")).alias("long_short"))


def _old_turnover(df, factor, n_groups, top=None, bottom=None):
    top = top or n_groups
    bottom = bottom or 1
    d = add_quantile(df, factor, n_groups).select(["trade_date", "q", "symbol"]).drop_nulls("q")
    prev_l = prev_s = None
    rows = []
    for dt_raw, day in d.group_by("trade_date", maintain_order=True):
        dt = dt_raw[0] if isinstance(dt_raw, (list, tuple)) else dt_raw
        cl = set(day.filter(pl.col("q") == top)["symbol"].to_list())
        cs = set(day.filter(pl.col("q") == bottom)["symbol"].to_list())
        if prev_l is None:
            prev_l, prev_s = cl, cs
            continue
        tl = 1 - len(cl & prev_l) / len(cl) if cl else None
        ts = 1 - len(cs & prev_s) / len(cs) if cs else None
        vals = [v for v in (tl, ts) if v is not None]
        rows.append({"date": dt, "turnover_long": tl, "turnover_short": ts,
                     "turnover_avg": sum(vals) / len(vals) if vals else None})
        prev_l, prev_s = cl, cs
    return pl.DataFrame(rows)


# ---------- 对账 ----------

def test_long_short_nav_matches_python_loop():
    df = _panel()
    for ng in (5, 10):
        new = long_short_nav(df, "factor", "fwd_ret_1", ng)
        old = _old_long_short_nav(df, "factor", "fwd_ret_1", ng)
        assert new["ret_long"].to_list() == pytest.approx(old["ret_long"].to_list())
        assert new["ret_short"].to_list() == pytest.approx(old["ret_short"].to_list())
        assert new["nav_long_short"].to_list() == pytest.approx(
            old["nav_long_short"].to_list())


def test_quantile_nav_matches_python_loop():
    df = _panel()
    new = quantile_nav(df, "factor", "fwd_ret_1", 10)
    old = _old_quantile_nav(df, "factor", "fwd_ret_1", 10)
    assert new.columns == old.columns
    for q in range(1, 11):
        assert new[f"q{q}"].to_list() == pytest.approx(old[f"q{q}"].to_list())
    assert new["long_short"].to_list() == pytest.approx(old["long_short"].to_list())


def test_pivot_group_returns_shape_and_nulls():
    df = _panel(n_days=5, n_sym=4)          # 4 只票分 10 组 → 大部分组为空
    g = group_returns(df, "factor", "fwd_ret_1", 10)
    piv = pivot_group_returns(g, 10)
    assert piv.columns == ["trade_date"] + [str(q) for q in range(1, 11)]
    assert piv.height == 4
    # 空组补 null 而不是 0 —— 两者在下游含义完全不同（无人 vs 零收益）
    assert piv["6"].null_count() == 4


def test_turnover_matches_python_loop():
    for seed in (3, 11):
        df = _panel(seed=seed)
        new = factor_turnover(df, "factor", 10)
        old = _old_turnover(df, "factor", 10)
        assert new["date"].to_list() == old["date"].to_list()
        for c in ("turnover_long", "turnover_short", "turnover_avg"):
            a, b = new[c].to_list(), old[c].to_list()
            for x, y in zip(a, b, strict=True):
                if x is None or y is None:
                    assert x is None and y is None
                else:
                    assert x == pytest.approx(y)


def test_turnover_explicit_top_bottom_and_zero_when_frozen():
    rows = [{"trade_date": d, "symbol": s, "mom": i * 0.1}
            for d in range(1, 8) for i, s in enumerate("abcde")]
    df = pl.DataFrame(rows)
    t = factor_turnover(df, "mom", 5)
    assert len(t) == 6                        # 首日不计入
    assert (t["turnover_avg"] == 0).all()     # 因子恒定 → 分组不变 → 零换手
    t2 = factor_turnover(df, "mom", 5, top=5, bottom=1)
    assert (t2["turnover_avg"] == 0).all()


def test_quantile_summary_period_counts_and_ls_consistency():
    df = _panel()
    qs = quantile_summary(df, "factor", "fwd_ret_1", 5)
    assert all(g["n_periods"] == df["trade_date"].n_unique() for g in qs["groups"])
    ls = long_short_nav(df, "factor", "fwd_ret_1", 5)
    assert qs["long_short"]["annual_return"] == pytest.approx(
        float(np.prod(1 + ls["ret_long_short"].to_numpy()) ** (252 / len(ls)) - 1), rel=1e-6)


def test_excess_nav_matches_python_loop():
    df = _panel()
    g = group_returns(df, "factor", "fwd_ret_1", 10).join(
        benchmark_series(df, "fwd_ret_1"), on="trade_date", how="inner").sort(["trade_date", "q"])
    dates = g.filter(pl.col("q") == 1).sort("trade_date")["trade_date"].to_list()
    bench_nav = np.cumprod(1 + g.filter(pl.col("q") == 1).sort("trade_date")["bench"].to_numpy())
    new = quantile_excess_nav(df, "factor", "fwd_ret_1", 10)
    assert new["trade_date"].to_list() == dates
    for q in range(1, 11):
        sub = g.filter(pl.col("q") == q).sort("trade_date")
        nav_g = np.cumprod(1 + sub["ret"].to_numpy())
        old = nav_g / bench_nav
        assert new[f"ex_q{q}"].to_list() == pytest.approx(list(old))
    assert new["ex_long_short"].to_list() == pytest.approx(
        (new["ex_q10"] / new["ex_q1"]).to_list())


def test_excess_nav_marks_incomplete_group_as_null():
    """结构性空分位组（该组从头到尾没有成员）→ 整列 null，不毒化曲线。

    5 只票分 10 组：分位落点是 2/4/6/8/10，奇数位组永远为空。
    """
    df = _panel(n_days=8, n_sym=5)
    new = quantile_excess_nav(df, "factor", "fwd_ret_1", 10)
    for q in (1, 3, 5, 7, 9):
        assert new[f"ex_q{q}"].null_count() == new.height, f"ex_q{q} 应为全 null"
    for q in (2, 4, 6, 8, 10):
        assert new[f"ex_q{q}"].null_count() == 0, f"ex_q{q} 不应有 null"


def test_quantile_summary_empty_panel_has_same_keys():
    """空数据路径必须返回**同样的键**（值全 None）。

    调用方（server/api/factors.py）无条件读 ``res["quantile"]["long_short"]``：
    早退分支少一个键，空面板就会把报表端点打成 500（KeyError）。「没有结论」
    和「没有这个字段」对调用方是两回事。
    """
    empty = pl.DataFrame({"trade_date": [], "symbol": [], "f": [], "fwd_ret_1": []},
                         schema_overrides={"trade_date": pl.Date, "f": pl.Float64,
                                           "fwd_ret_1": pl.Float64})
    out = quantile_summary(empty, "f", "fwd_ret_1", n_groups=5)
    full = quantile_summary(_panel(), "factor", "fwd_ret_1", n_groups=5)
    assert set(out) == set(full)
    assert set(out["long_short"]) == set(full["long_short"])
    assert all(v is None for v in out["long_short"].values())
    assert out["groups"] == []


# ---------- P1-4：零感知分箱 / 组内分箱 / 丢样三分账 ----------

def _sign_panel(neg: int = 8, pos: int = 2, n_days: int = 3) -> pl.DataFrame:
    """偏斜符号面板：负值多、正值少（普涨日的常态）。

    普通分箱会把「一堆小负数」按名次摊进所有组；zero_aware 要求负号侧只占
    下半区，最高组一定是**真正最正**的那只。
    """
    vals = [-9.0 + i for i in range(neg)] + [0.3 + i for i in range(pos)]
    rows = []
    for d in range(n_days):
        for i, v in enumerate(vals):
            rows.append({"trade_date": d, "symbol": f"S{i:03d}",
                         "factor": v, "fwd_ret_1": 0.01 * (i + 1)})
    return pl.DataFrame(rows)


def test_zero_aware_bins_each_side_of_zero_separately():
    df = _sign_panel(neg=8, pos=2)
    plain = add_quantile(df, "factor", 5)["q"].to_list()[:10]
    aware = add_quantile(df, "factor", 5, zero_aware=True)["q"].to_list()[:10]
    assert plain != aware
    # 负号侧只占 1..2（k_neg = 5//2），两个正值占最后两组
    assert set(aware[:8]) == {1, 2}
    assert aware[8] > 2 and aware[9] == 5
    # 方向：因子值最大的那只落在最高组，最小那只落在最低组
    assert aware[9] == 5 and aware[0] == 1


def test_zero_aware_rejects_factor_without_a_negative_side():
    allpos = _sign_panel().with_columns((pl.col("factor").abs() + 1).alias("factor"))
    with pytest.raises(ValueError, match="以零为中心"):
        add_quantile(allpos, "factor", 4, zero_aware=True)
    allzero = _sign_panel().with_columns(pl.lit(0.0).alias("factor"))
    with pytest.raises(ValueError, match="以零为中心"):
        add_quantile(allzero, "factor", 4, zero_aware=True)


def test_zero_aware_and_by_group_are_mutually_exclusive():
    df = _sign_panel().with_columns((pl.int_range(pl.len()) % 2).alias("grp"))
    with pytest.raises(ValueError, match="互斥"):
        add_quantile(df, "factor", 4, zero_aware=True, by_group="grp")


def test_by_group_bins_within_each_group():
    """组内分箱：每个 (日, 组) 内部各自摊满分位，组与组之间可比。

    取两组、因子均值差 100：不按组分箱时低组因子会整体落进低分位，
    按组分箱后两组的 q 分布应当一致。
    """
    rows = []
    for d in range(3):
        for g, base in (("A", 0.0), ("B", 100.0)):
            for i in range(10):
                rows.append({"trade_date": d, "symbol": f"{g}{i}", "grp": g,
                             "factor": base + i, "fwd_ret_1": 0.01})
    df = pl.DataFrame(rows)
    def _means(qcol: str) -> list[float]:
        return (df.with_columns(qcol).group_by("grp").agg(pl.col("q").mean())
                .sort("grp")["q"].to_list())

    plain = _means(add_quantile(df, "factor", 5)["q"])
    within = _means(add_quantile(df, "factor", 5, by_group="grp")["q"])
    # 不按组分箱：A 组因子整体偏低 → 几乎全落在低分位；B 组反之
    assert plain[0] < 2.5 and plain[1] > 3.5
    # 按组分箱：两组各自的 q 分布完全一致（均值都回到 3.0）
    assert within == pytest.approx([3.0, 3.0], abs=1e-9)


def test_by_group_missing_column_raises():
    with pytest.raises(KeyError, match="by_group"):
        add_quantile(_sign_panel(), "factor", 4, by_group="nope")
    with pytest.raises(KeyError, match="by_group"):
        account_samples(_sign_panel(), "factor", by_group="nope")


def test_account_samples_splits_every_drop_cause():
    """丢样三分账：每一类丢样各归各的，不能合成一个数字。"""
    df = pl.DataFrame({
        "trade_date": [0, 0, 0, 0, 0, 0],
        "symbol": ["a", "b", "c", "d", "e", "f"],
        "factor": [1.0, None, float("nan"), 2.0, 3.0, 4.0],
        "fwd_ret_1": [0.01, 0.01, 0.01, None, 0.01, 0.01],
        "grp": ["x", "x", "x", "x", None, "x"],
    })
    acc = account_samples(df, "factor", "fwd_ret_1", by_group="grp")
    # a/f 可用；b 因子 null、c 因子 NaN、d 收益缺失、e 分组键缺失 —— 各算各的
    assert (acc.total, acc.used) == (6, 2)
    assert acc.factor_null == 1
    assert acc.factor_nonfinite == 1
    assert acc.ret_missing == 1
    assert acc.group_missing == 1
    assert acc.dropped == 4
    assert acc.dropped == (acc.factor_null + acc.factor_nonfinite
                           + acc.ret_missing + acc.group_missing)
    assert acc.loss_ratio == pytest.approx(4 / 6)
    assert acc.to_dict()["used"] == 2


def test_quantile_summary_reports_and_enforces_max_loss():
    n = 40
    rows = []
    for d in range(20):
        for i in range(n):
            rows.append({"trade_date": d, "symbol": f"S{i:03d}",
                         "factor": float(i),
                         # 故意缺 1/5 的前瞻收益（模拟回测期末尾）
                         "fwd_ret_1": None if i % 5 == 0 else 0.001 * i})
    df = pl.DataFrame(rows)
    out = quantile_summary(df, "factor", "fwd_ret_1", 5)
    # 每天 8 只（i % 5 == 0）× 20 天 = 160 行缺前瞻收益
    assert out["dropped"]["ret_missing"] == 160
    assert out["dropped"]["used"] == 40 * 20 - 160
    assert out["dropped"]["loss_ratio"] == pytest.approx(0.2)
    # 阈值内不报错
    assert quantile_summary(df, "factor", "fwd_ret_1", 5, max_loss=0.25)["dropped"]["used"]
    # 超阈值 fatal，并把三分账写进消息里
    with pytest.raises(DataQualityError, match="SAMPLE_LOSS") as e:
        quantile_summary(df, "factor", "fwd_ret_1", 5, max_loss=0.05)
    assert "前瞻收益缺失 160" in str(e.value)


def test_quantile_summary_echoes_binning_choice():
    df = _sign_panel()
    out = quantile_summary(df, "factor", "fwd_ret_1", 5, zero_aware=True)
    assert out["zero_aware"] is True and out["by_group"] is None
    df2 = _sign_panel().with_columns((pl.int_range(pl.len()) % 2).alias("grp"))
    out2 = quantile_summary(df2, "factor", "fwd_ret_1", 5, by_group="grp")
    assert out2["by_group"] == "grp" and out2["zero_aware"] is False


def test_nav_helpers_accept_binning_kwargs():
    df = _sign_panel(n_days=6)
    nav = quantile_nav(df, "factor", "fwd_ret_1", 5, zero_aware=True)
    assert "long_short" in nav.columns and len(nav)
    ls = long_short_nav(df, "factor", "fwd_ret_1", 5, zero_aware=True)
    assert "ret_long_short" in ls.columns and len(ls)
