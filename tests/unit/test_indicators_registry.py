"""指标注册表、新增指标数值正确性、以及兼容转发层。"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from lquant.indicators import (
    CATEGORIES,
    INDICATORS,
    PANES,
    add_bbi,
    add_kdj,
    add_tiandao,
    add_turnover_ma,
    add_volume_ratio,
    add_volume_surge,
    compute,
    compute_many,
    min_window,
    outputs,
    register_indicator,
    required_history,
    xma_half_window,
    xma_truncated,
)


def make_ohlcv(n: int = 120, seed: int = 5) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    return pl.DataFrame({
        "trade_date": [date(2026, 1, 1) + timedelta(days=i) for i in range(n)],
        "open": close,
        "high": close + rng.uniform(0, 1, n),
        "low": close - rng.uniform(0, 1, n),
        "close": close,
        "volume": rng.uniform(1e6, 5e6, n),
        "turnover_rate": rng.uniform(0.5, 5.0, n),
    })


# ---------- 注册表 ----------

def test_builtin_indicators_registered():
    keys = INDICATORS.keys()
    for k in ("ma", "ema", "macd", "boll", "rsi", "kdj", "bbi",
              "volume_ratio", "volume_surge", "turnover_ma", "tiandao"):
        assert k in keys, k


def test_describe_is_frontend_ready():
    desc = INDICATORS.describe()
    assert desc and all({"name", "label", "category", "pane", "min_window", "outputs"}
                        <= set(d) for d in desc)
    assert all(d["category"] in CATEGORIES for d in desc)
    assert all(d["pane"] in PANES for d in desc)


def test_price_pane_is_explicit_not_inferred_from_category():
    """回归：MACD 属 trend，但量纲与价格差两个数量级，绝不能叠到价格轴。

    所以「画在哪」由注册表显式声明，而不是前端按 category 猜。
    """
    by_name = {d["name"]: d for d in INDICATORS.describe()}
    price = {n for n, d in by_name.items() if d["pane"] == "price"}
    assert price == {"ma", "ema", "boll", "bbi", "tiandao"}
    assert by_name["macd"]["pane"] == "sub"      # 同属 trend，但不进价格轴
    assert by_name["kdj"]["pane"] == "sub"
    assert by_name["volume_ratio"]["pane"] == "volume"


def test_compute_returns_original_columns_and_order():
    df = make_ohlcv()
    out = compute("ma", df, periods=(5,))
    assert out.columns[: len(df.columns)] == df.columns
    assert out.height == df.height


def test_compute_many_chains_with_params():
    out = compute_many(make_ohlcv(), ["ma", "rsi"], params={"ma": {"periods": (3,)},
                                                            "rsi": {"n": 6}})
    assert "ma3" in out.columns and "rsi6" in out.columns
    assert "ma5" not in out.columns


def test_min_window_and_required_history():
    assert min_window("boll") == 40
    assert required_history("boll") == 290
    assert outputs("macd") == ["ema12", "ema26", "macd_dif", "macd_dea", "macd_hist"]


def test_unknown_name_raises_with_choices():
    with pytest.raises(KeyError, match="未注册"):
        compute("does_not_exist", make_ohlcv(10))


def test_register_rejects_bad_category_pane_and_negative_window():
    with pytest.raises(ValueError, match="未知指标类别"):
        register_indicator("x1", category="nope")
    with pytest.raises(ValueError, match="未知挂载面板"):
        register_indicator("x2", pane="nope")
    with pytest.raises(ValueError, match="min_window"):
        register_indicator("x3", min_window=-1)


def test_pane_defaults_to_sub_so_it_never_lands_on_price_axis():
    @register_indicator("pane_default_demo")
    def _f(df: pl.DataFrame) -> pl.DataFrame:      # pragma: no cover
        return df

    assert INDICATORS.meta("pane_default_demo")["pane"] == "sub"


def test_duplicate_registration_rejected():
    @register_indicator("dup_demo", outputs=("dup_demo",))
    def _f(df: pl.DataFrame) -> pl.DataFrame:      # pragma: no cover - 不应被执行
        return df

    with pytest.raises(ValueError, match="重复注册"):
        @register_indicator("dup_demo")
        def _g(df: pl.DataFrame) -> pl.DataFrame:  # pragma: no cover - 不应被执行
            return df


def test_outputs_defaults_to_name():
    @register_indicator("solo_demo")
    def _f(df: pl.DataFrame) -> pl.DataFrame:      # pragma: no cover
        return df

    assert outputs("solo_demo") == ["solo_demo"]


# ---------- 数值正确性 ----------

def test_xma_truncated_equals_trailing_mean():
    df = make_ohlcv(40)
    got = df.select(xma_truncated("close", 25).alias("v"))["v"].to_numpy()
    c = df["close"].to_numpy()
    h = xma_half_window(25)                       # 12
    for i in (0, 5, 12, 20, 39):
        left = max(0, i - h)
        assert got[i] == pytest.approx(c[left:i + 1].mean(), abs=1e-9)


def test_xma_half_window_validation():
    assert xma_half_window(25) == 12
    assert xma_half_window(24) == 12
    assert xma_half_window(1) == 0
    with pytest.raises(ValueError, match="正整数"):
        xma_half_window(0)


def test_tiandao_channel_ordering_and_signals():
    df = add_tiandao(make_ohlcv())
    tail = df.drop_nulls("td_jinniu")
    assert tail.height > 0
    assert (tail["td_jinniu"] >= tail["td_jinzuan"]).all()    # 上轨 ≥ 下轨
    # 信号定义：跌破金钻 → 买；升破金牛 → 卖
    row = tail.row(0, named=True)
    assert row["td_gold_buy"] == (row["close"] < row["td_jinzuan"])
    assert row["td_gold_sell"] == (row["close"] > row["td_jinniu"])


def test_volume_ratio_excludes_current_bar():
    """分母必须不含当日：放量当天不能被自己把分母抬高。"""
    vol = [10.0] * 20 + [100.0]
    df = pl.DataFrame({"volume": vol})
    out = add_volume_ratio(df, n=20)
    assert out["volume_ratio"][-1] == pytest.approx(100.0 / 10.0, abs=1e-9)
    assert out["volume_ratio"][0] is None


def test_volume_surge_flag():
    df = pl.DataFrame({"volume": [10.0] * 6 + [100.0]})
    out = add_volume_surge(df, n=5, ratio=1.5)
    assert out["volume_surge"].to_list()[-1] is True
    assert out["volume_surge"].to_list()[5] is False


def test_turnover_ma_missing_column_is_noop():
    df = pl.DataFrame({"close": [1.0, 2.0]})
    assert add_turnover_ma(df).columns == ["close"]


def test_kdj_bounds_and_zero_range_guard():
    df = make_ohlcv()
    out = add_kdj(df)
    k = out["kdj_k"].drop_nulls()
    assert (k >= 0).all() and (k <= 100).all()
    flat = pl.DataFrame({"high": [5.0] * 20, "low": [5.0] * 20, "close": [5.0] * 20})
    got = add_kdj(flat, n=9)
    assert got["kdj_k"][-1] == pytest.approx(50.0, abs=1e-6)  # 无波动 → RSV=50


def test_bbi_is_mean_of_four_mas():
    df = make_ohlcv(60)
    out = add_bbi(df)
    c = df["close"].to_numpy()
    expect = np.mean([c[-3:].mean(), c[-6:].mean(), c[-12:].mean(), c[-24:].mean()])
    assert out["bbi"][-1] == pytest.approx(expect, abs=1e-9)


# ---------- 兼容转发层 ----------

def test_legacy_module_forwards_to_new_implementation():
    """factors.indicators 只是转发层：函数对象必须与新实现同一。"""
    from lquant.factors.indicators import WARMUP, add_all, add_ma
    from lquant.indicators.trend import add_ma as new_add_ma

    out = add_all(make_ohlcv())
    for col in ("ma5", "ma20", "macd_hist", "rsi14", "boll_upper"):
        assert col in out.columns
    assert WARMUP["rsi"] == 40
    assert add_ma is new_add_ma
