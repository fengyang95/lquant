"""多 horizon 并行 IC（Phase 4.2）+ 隔夜/日内切分（Phase 4.3）。

两条核心断言：

1. **``ic_by_horizon`` 与逐个 horizon 调 ``ic_summary`` 数值一致** ——
   它只是把 N 次扫描合并成 1 次，**不能改变口径**；
2. **``(1+隔夜)(1+日内) = 1+总``** 逐点成立（两段独立计算，是真交叉验证）。
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from lquant.factors.evaluate import (
    ic_by_horizon,
    ic_summary,
    session_ic,
    session_ic_summary,
    session_returns,
)
from lquant.factors.evaluate.returns import forward_return


def _panel(n_days: int = 60, n_sym: int = 25, seed: int = 0,
           overnight_bias: float = 0.0) -> pl.DataFrame:
    """构造「因子能预测次日收益」的面板；``overnight_bias`` 控制收益落在哪一段。

    ``overnight_bias=1`` → 次日收益几乎全在隔夜跳空（方差更大）；
    ``overnight_bias=0`` → 几乎全在日内。

    **因子必须预测未来**：``factor[t]`` 取的是第 t+1 日的两段收益加噪。
    早期版本把因子设成「当日」两段收益之和 —— 那时因子与未来收益无关，
    IC 全是噪声，`dominant` 当然随机（实测确实翻车）。
    """
    rng = np.random.default_rng(seed)
    ovn_sd = 0.01 * (0.3 + overnight_bias)
    idy_sd = 0.01 * (1.3 - overnight_bias)
    rows = []
    for j in range(n_sym):
        ovn = rng.normal(0, ovn_sd, n_days)
        idy = rng.normal(0, idy_sd, n_days)
        # 因子 = 次日两段收益 + 噪声（末行没有次日，置 0）
        nxt = np.concatenate([ovn[1:] + idy[1:], [0.0]])
        f = nxt + rng.normal(0, 0.0005, n_days)
        px = 10.0 + j
        for i in range(n_days):
            d = date(2026, 1, 5) + timedelta(days=i)
            pre = px
            op = pre * (1 + ovn[i])
            px = op * (1 + idy[i])
            rows.append({"trade_date": d, "symbol": f"S{j:02d}", "open": op,
                         "high": max(op, px), "low": min(op, px), "close": px,
                         "pre_close": pre, "volume": 1e6, "amount": 1e6 * px,
                         "factor": float(f[i])})
    return pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))


# ----------------------------------------------------------- 4.2 多 horizon

def test_ic_by_horizon_matches_loop_over_ic_summary():
    """并行版必须与逐个 horizon 循环的结果**完全一致**（只是省扫描）。"""
    df = forward_return(_panel(), "close", periods=[1, 5, 10, 20])
    tbl = ic_by_horizon(df, "factor", [1, 5, 10, 20])
    assert tbl.height == 4
    for r in tbl.iter_rows(named=True):
        h = r["horizon"]
        ref = ic_summary(df, "factor", f"fwd_ret_{h}")
        # 用相对容差：两条路径的聚合顺序不同，末位有几个 ulp 的差异
        # （IR 量级可达 1e3，绝对 1e-12 会假失败）
        assert r["ic_mean"] == pytest.approx(ref["ic"]["mean"], rel=1e-9, abs=1e-12)
        assert r["ic_ir"] == pytest.approx(ref["ic"]["ir"], rel=1e-9, abs=1e-9)
        assert r["ic_t"] == pytest.approx(ref["ic"]["t_stat"], rel=1e-9, abs=1e-9)
        assert r["rank_ic_mean"] == pytest.approx(ref["rank_ic"]["mean"], rel=1e-9,
                                                  abs=1e-12)
        assert r["positive_rate"] == pytest.approx(ref["ic"]["positive_rate"], rel=1e-9)
        assert r["n_days"] == ref["ic"]["n_days"]


def test_ic_by_horizon_derives_returns_when_missing():
    """收益列不存在时给 price_col 就现算（与 forward_return 同口径）。"""
    df = _panel()
    a = ic_by_horizon(df, "factor", [1, 5], price_col="close")
    b = ic_by_horizon(forward_return(df, "close", periods=[1, 5]), "factor", [1, 5])
    assert a.equals(b)


def test_ic_by_horizon_requires_returns_or_price():
    with pytest.raises(KeyError, match="缺少收益列"):
        ic_by_horizon(_panel(), "factor", [1, 5])


def test_ic_by_horizon_validates_inputs():
    df = forward_return(_panel(), "close", periods=[1, 5])
    with pytest.raises(ValueError, match="不能为空"):
        ic_by_horizon(df, "factor", [])
    with pytest.raises(ValueError, match="有重复"):
        ic_by_horizon(df, "factor", [1, 1])
    with pytest.raises(ValueError, match="必须为正整数"):
        ic_by_horizon(df, "factor", [0])
    with pytest.raises(KeyError, match="缺少因子列"):
        ic_by_horizon(df, "nope", [1])


def test_ic_by_horizon_defaults_and_columns():
    df = forward_return(_panel(), "close", periods=[1, 5, 10, 20])
    tbl = ic_by_horizon(df, "factor")
    assert tbl["horizon"].to_list() == [1, 5, 10, 20]
    assert set(tbl.columns) == {
        "horizon", "ic_mean", "ic_std", "ic_ir", "ic_t", "ic_t_nw",
        "rank_ic_mean", "rank_ic_ir", "rank_ic_t", "positive_rate",
        "ic_autocorr", "n_days"}


def test_ic_by_horizon_min_obs_filter_matches_series():
    """min_obs 过滤后两个入口的「有效天数」必须一致。

    注意 ``ic_summary`` 在**完全没有有效日**时返回 ``{}``（而不是 n_days=0）——
    那是它的既有契约，这里按契约兼容，不强行统一。
    """
    df = forward_return(_panel(n_days=30, n_sym=8), "close", periods=[1])
    tbl = ic_by_horizon(df, "factor", [1], min_obs=20)
    ref = ic_summary(df, "factor", "fwd_ret_1", min_obs=20)
    assert tbl["n_days"][0] == 0 and ref["ic"] == {}      # 两边都认定「无有效日」

    # 放宽门槛后两边天数一致
    tbl2 = ic_by_horizon(df, "factor", [1], min_obs=5)
    ref2 = ic_summary(df, "factor", "fwd_ret_1", min_obs=5)
    assert tbl2["n_days"][0] == ref2["ic"]["n_days"] > 0


# ----------------------------------------------------------- 4.3 会话切分

def test_session_identity_holds_pointwise():
    """``(1+隔夜)(1+日内) = 1+总``，两段独立计算 → 真交叉验证。"""
    out = session_returns(_panel(), periods=[1, 5, 10])
    for h in (1, 5, 10):
        o = out[f"fwd_ret_overnight_{h}"].to_numpy()
        i = out[f"fwd_ret_intraday_{h}"].to_numpy()
        t = out[f"fwd_ret_{h}"].to_numpy()
        m = np.isfinite(o) & np.isfinite(i) & np.isfinite(t)
        assert m.sum() > 100
        assert np.allclose((1 + o[m]) * (1 + i[m]), 1 + t[m], atol=1e-12)


def test_session_returns_hand_checked_h1():
    """h=1 手算：隔夜 = open_t/close_{t-1}−1，日内 = close_t/open_t−1。"""
    df = pl.DataFrame({
        "trade_date": [date(2026, 1, 5), date(2026, 1, 6)],
        "symbol": ["S", "S"],
        "open": [10.0, 11.0],
        "close": [10.5, 11.55],
    }).with_columns(pl.col("trade_date").cast(pl.Date))
    out = session_returns(df, periods=[1]).sort("trade_date")
    # 第 1 行（2026-01-06）：隔夜 11/10.5−1，日内 11.55/11−1
    assert out["fwd_ret_overnight_1"][0] == pytest.approx(11.0 / 10.5 - 1, abs=1e-12)
    assert out["fwd_ret_intraday_1"][0] == pytest.approx(11.55 / 11.0 - 1, abs=1e-12)
    assert out["fwd_ret_1"][0] == pytest.approx(11.55 / 10.5 - 1, abs=1e-12)
    # 最后一行没有未来 → null
    assert out["fwd_ret_1"][1] is None


def test_session_returns_no_cross_symbol_leak():
    """分组必须按 symbol：一只票的隔夜不能用到另一只的收盘。

    两只票的次日隔夜涨幅刻意不同（10% vs 5%），否则「串了」也看不出来。
    """
    rows = []
    for s, base, up in (("A", 10.0, 1.10), ("B", 100.0, 1.05)):
        for i, px in enumerate((base, base * up)):
            rows.append({"trade_date": date(2026, 1, 5 + i), "symbol": s,
                         "open": px, "close": px})
    df = pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))
    out = session_returns(df, periods=[1]).sort(["symbol", "trade_date"])
    got = {g["symbol"][0]: g["fwd_ret_overnight_1"].to_list()
           for _, g in out.group_by("symbol", maintain_order=True)}
    # 行 0 拿到的是「自己」次日的隔夜收益；行 1 无未来 → null
    assert got["A"][0] == pytest.approx(0.10, abs=1e-12)
    assert got["B"][0] == pytest.approx(0.05, abs=1e-12)
    assert got["A"][1] is None and got["B"][1] is None


def test_session_returns_suspended_day_gives_null_not_inf():
    """停牌（open/close = 0）→ log 归 null，不产生 inf/nan 污染整段。"""
    df = pl.DataFrame({
        "trade_date": [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)],
        "symbol": ["S"] * 3,
        "open": [10.0, 0.0, 10.5],
        "close": [10.0, 0.0, 10.8],
    }).with_columns(pl.col("trade_date").cast(pl.Date))
    out = session_returns(df, periods=[1]).sort("trade_date")
    vals = out["fwd_ret_overnight_1"].to_numpy()
    assert not np.isinf(np.nan_to_num(vals, nan=0.0)).any()
    assert np.isnan(vals).any()


def test_session_returns_validates_inputs():
    df = _panel(n_days=10, n_sym=3)
    with pytest.raises(KeyError, match="缺少列"):
        session_returns(df.drop("open"))
    with pytest.raises(ValueError, match="必须为正整数"):
        session_returns(df, periods=[0])
    with pytest.raises(ValueError, match="不能为空"):
        session_returns(df, periods=[])


def test_session_ic_covers_all_three_sessions():
    tbl = session_ic(_panel(), "factor", periods=[1, 5])
    assert set(tbl["session"].to_list()) == {"total", "overnight", "intraday"}
    assert sorted(set(tbl["horizon"].to_list())) == [1, 5]
    assert tbl.height == 6
    assert set(tbl.columns) == {"horizon", "session", "ic_mean", "ic_ir", "ic_t",
                                "rank_ic_mean", "rank_ic_ir", "positive_rate", "n_days"}


def test_session_ic_uses_identical_samples_across_sessions():
    """三段必须用同一份样本（否则三段 IC 之差混入样本选择偏差）。"""
    tbl = session_ic(_panel(), "factor", periods=[1])
    assert len(set(tbl["n_days"].to_list())) == 1


def test_session_ic_summary_detects_overnight_driven_signal():
    """收益全在隔夜 → dominant 必须是 overnight，overnight_share 接近 1。"""
    df = _panel(overnight_bias=1.0, seed=3)
    s = session_ic_summary(df, "factor", horizon=1)
    assert s["dominant"] == "overnight"
    assert s["overnight_share"] > 0.6


def test_session_ic_summary_detects_intraday_driven_signal():
    """收益全在日内 → dominant 必须是 intraday。"""
    df = _panel(overnight_bias=0.0, seed=4)
    s = session_ic_summary(df, "factor", horizon=1)
    assert s["dominant"] == "intraday"
    assert s["overnight_share"] < 0.4


def test_session_ic_summary_empty_input():
    s = session_ic_summary(_panel(n_days=5, n_sym=2), "factor", horizon=1, min_obs=999)
    assert s["dominant"] is None and s["overnight_share"] is None


def test_session_ic_summary_missing_factor_column():
    with pytest.raises(KeyError, match="缺少因子列"):
        session_ic_summary(_panel(), "not_a_factor", horizon=1)

