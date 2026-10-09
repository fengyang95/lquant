"""关键价位指标集（``lquant.indicators.levels``）。

覆盖四件事：
1. 结构完整性 —— 各类价位都产出、字段齐全、类型/方向/强度是闭集；
2. 按现价上下分组正确 —— 支撑都在现价下、压力都在现价上、组内按距离排序；
3. **前缀不变性** —— 逐行版本 ``add_levels`` 不得引用未来数据；
4. 退化输入 —— 行数不足 / 全 null 价不崩，缺核心列必须抛错。
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from lquant.indicators import (
    INDICATORS,
    LEVEL_TYPES,
    assert_no_lookahead,
    compute,
    compute_levels,
    min_window,
    required_history,
)
from lquant.indicators.levels import (
    SIDES,
    STRENGTHS,
    LevelSet,
    PriceLevel,
    add_levels,
    atr,
)

ADD_LEVEL_OUTPUTS = (
    "lvl_pivot", "lvl_pivot_r1", "lvl_pivot_s1",
    "lvl_support_1", "lvl_support_2", "lvl_resistance_1", "lvl_resistance_2",
)


def make_ohlcv(n: int = 320, seed: int = 3, *, gap_at: int | None = 250,
               gap_pct: float = 0.06) -> pl.DataFrame:
    """带一个**未回补**向上缺口的随机日线。

    缺口之后再叠一段单边上行，保证后续 K 线不会回到缺口真空带里 ——
    否则随机游走随时可能把它回补掉，测试会变成 flaky。
    """
    rng = np.random.default_rng(seed)
    close = 30 + np.cumsum(rng.normal(0, 0.25, n))
    if gap_at is not None and 0 < gap_at < n:
        jump = gap_pct * close[gap_at - 1]
        close[gap_at:] += jump + np.linspace(0.0, 3.0, n - gap_at)
    close = np.maximum(close, 1.0)
    high = close + rng.uniform(0.02, 0.2, n)
    low = close - rng.uniform(0.02, 0.2, n)
    return pl.DataFrame({
        "trade_date": [date(2025, 1, 1) + timedelta(days=i) for i in range(n)],
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": rng.uniform(1e6, 5e6, n),
    })


# ---------- 1. 结构完整性 ----------


def test_every_level_type_is_produced():
    ls = compute_levels(make_ohlcv())
    produced = {lv.type for lv in ls.levels}
    assert set(LEVEL_TYPES) <= produced, f"缺少价位类型: {set(LEVEL_TYPES) - produced}"
    assert len(ls) == len(ls.levels) > 0
    assert ls.close is not None and ls.as_of == date(2025, 1, 1) + timedelta(days=319)


def test_level_fields_are_complete_and_closed_sets():
    ls = compute_levels(make_ohlcv())
    for lv in ls.levels:
        assert isinstance(lv, PriceLevel)
        assert lv.type in LEVEL_TYPES
        assert lv.side in SIDES
        assert lv.strength in STRENGTHS
        assert lv.rank >= 1
        assert lv.label
        assert math.isfinite(lv.value) and lv.value > 0
        assert lv.detail
        d = lv.to_dict()
        assert d["type_label"] == LEVEL_TYPES[lv.type]
        assert d["value"] == lv.value and d["side"] == lv.side


def test_pivot_tiers_cover_all_seven_points():
    ls = compute_levels(make_ohlcv())
    assert sorted(lv.tier for lv in ls.by_type("pivot")) == [0, 1, 1, 2, 2, 3, 3]


def test_invalid_construction_is_rejected():
    with pytest.raises(ValueError, match="未知价位分组"):
        PriceLevel(value=1.0, label="x", type="nope", side="support")
    with pytest.raises(ValueError, match="未知价位方向"):
        PriceLevel(value=1.0, label="x", type="pivot", side="nope")
    with pytest.raises(ValueError, match="未知强度档位"):
        PriceLevel(value=1.0, label="x", type="pivot", side="support", strength="nope")
    with pytest.raises(ValueError, match="有限数"):
        PriceLevel(value=float("nan"), label="x", type="pivot", side="support")


# ---------- 2. 按现价分组 ----------


def test_grouping_by_current_price_is_correct():
    ls = compute_levels(make_ohlcv())
    close = ls.close
    assert close is not None
    assert all(lv.value < close for lv in ls.supports)
    assert all(lv.value > close for lv in ls.resistances)
    assert all(lv.value == close for lv in ls.neutral)
    # 组内按距现价由近到远
    for pool in (ls.supports, ls.resistances):
        dists = [abs(lv.value - close) for lv in pool]
        assert dists == sorted(dists)
    # 全局也按距离升序
    assert [abs(lv.value - close) for lv in ls.levels] == sorted(
        abs(lv.value - close) for lv in ls.levels)
    # nearest 就是组内第一个
    assert ls.nearest("support", 1) == ls.supports[:1]
    assert ls.nearest("resistance", 2) == ls.resistances[:2]
    with pytest.raises(ValueError, match="support/resistance"):
        ls.nearest("neutral")


def test_side_agrees_with_stored_value_for_every_level():
    """回归：side 必须由**取整后**的 value 与 close 决定。

    曾经 builders 用未取整的中间量判方向，导致 value 恰好等于 close 的价位被标成
    resistance —— 分组与数值自相矛盾。
    """
    ls = compute_levels(make_ohlcv())
    assert ls.close is not None
    for lv in ls.levels:
        expected = ("resistance" if lv.value > ls.close
                    else "support" if lv.value < ls.close else "neutral")
        assert lv.side == expected, f"{lv.label} {lv.value} vs close {ls.close}"


def test_rank_is_within_type_and_contiguous():
    ls = compute_levels(make_ohlcv())
    for level_type in LEVEL_TYPES:
        ranks = [lv.rank for lv in ls.by_type(level_type)]
        assert ranks == list(range(1, len(ranks) + 1)), level_type


def test_to_dict_exposes_grouped_views():
    payload = compute_levels(make_ohlcv()).to_dict()
    assert set(payload) >= {"close", "as_of", "levels", "supports", "resistances",
                            "neutral", "by_type", "notes", "params"}
    assert len(payload["levels"]) == len(payload["supports"]) + \
        len(payload["resistances"]) + len(payload["neutral"])
    assert payload["as_of"] == "2025-11-16"
    assert set(payload["by_type"]) == set(LEVEL_TYPES)


def test_unfilled_gap_is_kept_and_filled_gap_is_dropped():
    """缺口口径：**与缺口真空带有重叠**即算回补（参考实现代码语义）。"""
    df = pl.DataFrame({
        "trade_date": [date(2026, 1, i) for i in range(1, 6)],
        "high": [10.0, 10.6, 10.7, 9.4, 9.3],
        "low": [9.8, 10.5, 10.2, 8.9, 8.8],
        "close": [9.9, 10.5, 10.6, 9.2, 9.0],
        "volume": [1e6] * 5,
    })
    # 第 2 根：low 10.5 > 前 high 10.0 → 向上缺口 (10.0, 10.5)
    # 第 3 根区间 [10.2, 10.7] 与缺口重叠 → 已回补，必须被过滤
    # 第 4 根：high 9.4 < 前 low 10.2 → 向下缺口 (9.4, 10.2)
    # 第 5 根区间 [8.8, 9.3] 完全在缺口下方 → 未回补，保留
    gaps = compute_levels(df).by_type("gap")
    assert [lv.label for lv in gaps] == ["向下缺口"]
    assert gaps[0].value == pytest.approx(9.8)
    assert gaps[0].side == "resistance"      # 缺口在现价 9.0 上方 → 压力


# ---------- 3. 前缀不变性（未来函数门禁） ----------


def test_add_levels_passes_prefix_invariance():
    df = make_ohlcv(n=180, gap_at=120)
    assert_no_lookahead(add_levels, df, out_cols=ADD_LEVEL_OUTPUTS)


def test_add_levels_brackets_close_and_orders_two_tiers():
    df = make_ohlcv(n=200)
    out = add_levels(df).drop_nulls(["lvl_support_1", "lvl_support_2",
                                     "lvl_resistance_1", "lvl_resistance_2"])
    assert out.height > 0
    assert (out["lvl_support_1"] < out["close"]).all()
    assert (out["lvl_support_2"] <= out["lvl_support_1"]).all()
    assert (out["lvl_resistance_1"] > out["close"]).all()
    assert (out["lvl_resistance_2"] >= out["lvl_resistance_1"]).all()


def test_atr_is_wilder_smoothed_and_prefix_safe():
    df = make_ohlcv(n=80)
    out = atr(df, 14)
    assert "atr14" in out.columns
    assert out["atr14"][:13].is_null().all()          # 预热期
    assert (out["atr14"].drop_nulls() > 0).all()


# ---------- 4. 退化输入 ----------


def test_short_frame_yields_pivot_without_crashing():
    ls = compute_levels(make_ohlcv(n=3))
    assert ls.close is not None
    assert ls.by_type("pivot")                        # 枢轴只需 1 根
    assert ls.notes                                   # 其余类型的跳过原因必须显式
    assert any("成交密集区跳过" in n for n in ls.notes)
    assert add_levels(make_ohlcv(n=3)).height == 3    # 逐行版同样不崩


def test_all_null_prices_returns_empty_level_set():
    nulls = pl.DataFrame({
        "trade_date": [date(2026, 1, 1), date(2026, 1, 2)],
        "high": [None, None], "low": [None, None],
        "close": [None, None], "volume": [None, None],
    })
    ls = compute_levels(nulls)
    assert isinstance(ls, LevelSet)
    assert ls.close is None and len(ls) == 0
    assert any("无效价格" in n for n in ls.notes)
    assert add_levels(nulls).height == 2


def test_empty_frame_returns_empty_level_set():
    empty = pl.DataFrame(schema={"trade_date": pl.Date, "high": pl.Float64,
                                 "low": pl.Float64, "close": pl.Float64,
                                 "volume": pl.Float64})
    ls = compute_levels(empty)
    assert ls.close is None and len(ls) == 0
    assert any("有效价格行为 0" in n for n in ls.notes)


def test_missing_core_column_raises_loudly():
    with pytest.raises(ValueError, match="缺少必需列"):
        compute_levels(pl.DataFrame({"close": [1.0, 2.0]}))
    with pytest.raises(ValueError, match="缺少必需列"):
        add_levels(pl.DataFrame({"close": [1.0, 2.0]}))


def test_missing_volume_only_skips_volume_profile():
    ls = compute_levels(make_ohlcv().drop("volume"))
    assert not ls.by_type("sr")
    assert any("缺少 volume 列" in n for n in ls.notes)
    assert ls.by_type("pivot")                        # 其他类型不受影响


# ---------- 5. 注册表接线 ----------


def test_levels_is_registered_for_frontend_and_api():
    assert "levels" in INDICATORS
    meta = INDICATORS.meta("levels")
    assert meta["pane"] == "price"                    # 价位是价格量纲，必须叠价格轴
    assert meta["category"] == "channel"
    assert tuple(meta["outputs"]) == ADD_LEVEL_OUTPUTS
    assert min_window("levels") == 60
    assert required_history("levels") == 310
    assert "levels" in {d["name"] for d in INDICATORS.describe()}


def test_compute_levels_via_registry_preserves_shape():
    df = make_ohlcv(n=120, gap_at=None)
    out = compute("levels", df)
    assert out.columns[: len(df.columns)] == df.columns
    assert out.height == df.height
    assert all(c in out.columns for c in ADD_LEVEL_OUTPUTS)
