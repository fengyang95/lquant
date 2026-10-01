"""portfolio/screener 单元测试（纯内存 polars，不联网）。"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from lquant.portfolio.screener import (
    FilterConfig,
    apply_filters,
    filter_report,
    score,
    screen,
)


def _df(**extra) -> pl.DataFrame:
    """2 天 × 4 标的基础行情，默认全部通过过滤器。"""
    base = {
        "trade_date": [date(2026, 1, 6), date(2026, 1, 6),
                       date(2026, 1, 6), date(2026, 1, 6)],
        "symbol": ["A", "B", "C", "D"],
        "close": [10.0, 20.0, 30.0, 40.0],
        "amount": [1e7, 1e7, 1e7, 1e7],
        "volume": [1000, 1000, 1000, 1000],
        "pre_close": [9.0, 20.0, 30.0, 40.0],
        "high": [10.5, 21.0, 31.0, 41.0],
        "low": [9.5, 19.0, 29.0, 39.0],
        "mom20": [0.10, 0.20, -0.05, 0.30],
    }
    base.update(extra)
    return pl.DataFrame(base)


# ---------- FilterConfig ----------


def test_filter_config_to_dict():
    cfg = FilterConfig(max_count=5)
    d = cfg.to_dict()
    assert d["max_count"] == 5 and d["exclude_st"] is True
    # 不改原对象
    assert cfg.max_count == 5


# ---------- apply_filters ----------


def test_apply_filters_st_by_flag_and_name():
    df = _df(is_st=[True, False, False, False])
    assert apply_filters(df)["symbol"].to_list() == ["B", "C", "D"]
    df2 = _df(name=["ST 甲", "B", "C 退", "D"])
    assert apply_filters(df2)["symbol"].to_list() == ["B", "D"]
    # exclude_st=False 保留
    assert len(apply_filters(df, FilterConfig(exclude_st=False))) == 4


def test_apply_filters_missing_columns_skipped():
    """缺列条件自动跳过，不报错。"""
    df = pl.DataFrame({"symbol": ["A", "B"], "close": [10.0, 20.0]})
    out = apply_filters(df)
    assert out["symbol"].to_list() == ["A", "B"]


def test_apply_filters_min_list_days():
    d = date(2026, 1, 6)
    df = _df(list_date=[d - timedelta(days=10), d - timedelta(days=100),
                        None, d - timedelta(days=61)])
    # 源码行为：list_date 为 null 的行条件为 null，被 filter 一并剔除
    assert apply_filters(df)["symbol"].to_list() == ["B", "D"]


def test_apply_filters_liquidity_low_price_suspended():
    df = _df(amount=[1e6, 1e7, 1e7, 1e7], close=[0.5, 0.5, 30.0, 40.0],
             volume=[1000, 1000, 0, 1000])
    assert apply_filters(df)["symbol"].to_list() == ["D"]


def test_apply_filters_limit_up_and_down():
    # A 一字涨停：high==low==close，close >= pre_close*1.1
    df = _df(high=[9.9, 21.0, 31.0, 41.0], low=[9.9, 19.0, 29.0, 39.0],
             close=[9.9, 20.0, 30.0, 40.0], pre_close=[9.0, 20.0, 30.0, 40.0])
    assert apply_filters(df)["symbol"].to_list() == ["B", "C", "D"]
    # 跌停默认不排除，开启后 A 被剔除
    cfg = FilterConfig(exclude_limit_down=True)
    assert apply_filters(df, cfg)["symbol"].to_list() == ["B", "C", "D"]
    assert len(apply_filters(df, FilterConfig(exclude_limit_up=False))) == 4


def test_apply_filters_verbose_prints(capsys):
    df = _df(is_st=[True, False, False, False])
    apply_filters(df, verbose=True)
    assert "ST" in capsys.readouterr().out


# ---------- score ----------


def test_score_zscore_weighted():
    df = score(_df(), {"mom20": 1.0})
    s = df["score"].to_list()
    # 标准化后均值为 0，极差 > 0
    assert abs(sum(s)) < 1e-9
    assert max(s) > 0 > min(s)


def test_score_list_equal_weights_and_rank_method():
    df = score(_df(), ["mom20"], method="rank")
    assert "score" in df.columns
    # rank 归一到 (0,1]
    assert df["score"].max() <= 1.0 + 1e-9 and df["score"].min() > 0


def test_score_multi_factor_weighted_sum():
    """两个因子加权合成（覆盖 parts 累加分支）。"""
    df = score(_df(), {"mom20": 0.6, "close": 0.4})
    s = df["score"].to_list()
    single_mom = score(_df(), {"mom20": 1.0})["score"].to_list()
    single_close = score(_df(), {"close": 1.0})["score"].to_list()
    for a, b, c in zip(s, single_mom, single_close, strict=True):
        assert a == pytest.approx(0.6 * b + 0.4 * c)


def test_score_missing_factor_raises():
    with pytest.raises(KeyError, match="因子列不存在"):
        score(_df(), ["not_a_factor"])


def test_score_constant_factor_std_fallback():
    """std≈0 的常数因子不会炸（fallback 1.0），截面内全为 0。"""
    df = _df().with_columns(pl.lit(5.0).alias("const"))
    out = score(df, {"const": 1.0})
    assert out["score"].abs().sum() < 1e-9


# ---------- screen ----------


def test_screen_top_n_and_ascending():
    df = _df()
    out = screen(df, ["mom20"], top_n=2)
    assert out["symbol"].to_list() == ["D", "B"]
    out2 = screen(df, ["mom20"], top_n=2, ascending=True)
    assert out2["symbol"].to_list() == ["C", "A"]


def test_screen_empty_after_filter_returns_empty():
    df = _df(volume=[0, 0, 0, 0])
    out = screen(df, ["mom20"])
    assert out.height == 0


def test_screen_top_n_zero_keeps_all():
    out = screen(_df(), ["mom20"], top_n=0)
    assert out.height == 4


# ---------- filter_report ----------


def test_filter_report_counts():
    df = _df(is_st=[True, False, False, False])
    rep = filter_report(df)
    assert rep["total"] == 4 and rep["kept"] == 3 and rep["dropped"] == 1
    assert abs(rep["kept_ratio"] - 0.75) < 1e-9
    assert rep["config"]["exclude_st"] is True


def test_filter_report_empty_df():
    rep = filter_report(_df().head(0))
    assert rep == {**rep, "kept_ratio": 0.0, "kept": 0, "total": 0, "dropped": 0}
