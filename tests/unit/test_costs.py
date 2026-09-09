"""换手率与成本敏感性：高 IC 低净收益的照妖镜。"""
from __future__ import annotations

import polars as pl

from lquant.factors.evaluate.costs import cost_matrix, factor_turnover


def _frozen_df() -> pl.DataFrame:
    """5 股 × 10 天，mom 列恒定（symbol 序数 × 0.1）→ 分组不变 → 换手 0。"""
    rows = [{"symbol": s, "trade_date": d, "mom": i * 0.1}
            for d in range(1, 11) for i, s in enumerate("abcde")]
    return pl.DataFrame(rows)


def _flip_df() -> pl.DataFrame:
    """mom 每日反序：昨日最大的今天最小 → 多空两端每日完全换血。"""
    rows = []
    for d in range(1, 11):
        for i, s in enumerate("abcde"):
            mom = i * 0.1 if d % 2 else (4 - i) * 0.1
            rows.append({"symbol": s, "trade_date": d, "mom": mom})
    return pl.DataFrame(rows)


def test_zero_turnover_when_factor_frozen():
    t = factor_turnover(_frozen_df(), "mom", n_groups=5)
    assert len(t) == 9  # 首日无可比持仓，不计入
    assert (t["turnover_avg"] == 0).all()
    assert (t["turnover_long"] == 0).all()
    assert (t["turnover_short"] == 0).all()


def test_full_turnover_when_factor_shuffled_daily():
    t = factor_turnover(_flip_df(), "mom", n_groups=5)
    assert (t["turnover_long"] == 1).all()
    assert (t["turnover_short"] == 1).all()
    assert (t["turnover_avg"] == 1).all()


def test_turnover_top_bottom_explicit():
    t = factor_turnover(_frozen_df(), "mom", n_groups=5, top=5, bottom=1)
    assert (t["turnover_avg"] == 0).all()


def test_cost_matrix_monotone():
    # 满换手因子：每日净收益 = 毛收益 - turnover_avg×(bps/1e4)×2（常数）→ bps 越高净年化越低。
    # fwd_ret_1 取明日 mom 的相反数，让多空毛收益为正（反转排序下直接取 shift(-1) 会做反方向）
    df = _flip_df().sort(["symbol", "trade_date"]).with_columns(
        (-pl.col("mom").shift(-1).over("symbol")).alias("fwd_ret_1")
    )
    m = cost_matrix(df, "mom", "fwd_ret_1", bps_list=[0.0, 5.0, 15.0, 30.0],
                    n_groups=5)
    nets = m["net_annual"].to_list()
    assert all(nets[i] >= nets[i + 1] - 1e-9 for i in range(len(nets) - 1))
    # 满换手 + 30bps 双边 → 成本实打实侵蚀，net(0) > net(30)
    assert m[0, "net_annual"] > m[-1, "net_annual"]
    assert m["viable"].dtype == pl.Boolean
