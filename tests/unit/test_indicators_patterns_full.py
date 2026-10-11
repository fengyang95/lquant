"""K 线形态全集 patterns_full.py 的验证：

1. 注册元数据（类别/面板/输出列/min_window/inputs）；
2. 每个形态的**教科书正例**（确认日信号必须 =1 —— 宁漏勿滥的下界约束）；
3. 全部指标过 ``assert_no_lookahead`` 前缀不变性门禁（随机 OHLC）；
4. 一字板（高=低）全 0 不炸；OHLC 缺行出 null；
5. 阈值参数越界 fail-loudly。

正例 OHLC 均为手工构造并逐一核对过判据的序列，确认日 = 最后一根。
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import polars as pl
import pytest

from lquant.indicators import (
    INDICATORS,
    assert_no_lookahead,
    compute,
    patterns_full,  # noqa: F401  触发注册
)
from lquant.indicators.patterns_full import (
    add_dark_cloud_cover,
    add_gravestone_doji,
    add_takuri,
)


def ohlc(rows: list[tuple], symbol: str = "TEST") -> pl.DataFrame:
    d0 = date(2024, 1, 2)
    return pl.DataFrame(
        {
            "symbol": [symbol] * len(rows),
            "trade_date": [d0 + timedelta(days=i) for i in range(len(rows))],
            "open": [float(r[0]) for r in rows],
            "high": [float(r[1]) for r in rows],
            "low": [float(r[2]) for r in rows],
            "close": [float(r[3]) for r in rows],
        }
    )


def random_ohlc(n: int = 60, seed: int = 42) -> pl.DataFrame:
    rnd = random.Random(seed)
    rows = []
    price = 10.0
    for _ in range(n):
        o = price * (1 + rnd.uniform(-0.02, 0.02))
        c = o * (1 + rnd.uniform(-0.05, 0.05))
        h = max(o, c) * (1 + rnd.uniform(0.0, 0.02))
        low = min(o, c) * (1 - rnd.uniform(0.0, 0.02))
        rows.append((round(o, 4), round(h, 4), round(low, 4), round(c, 4)))
        price = c
    return ohlc(rows)


PATTERN_NAMES = sorted(n for n in INDICATORS if n.startswith("pattern_"))

# 教科书正例：确认日（最后一根）必须命中。
TEXTBOOK: dict[str, list[tuple]] = {
    "pattern_doji": [(10, 10.8, 9.2, 10.05)],
    "pattern_rickshaw_man": [(10.0, 10.6, 9.3, 10.0)],
    "pattern_two_crows": [(9.0, 11.0, 8.98, 10.8), (10.9, 11.0, 10.4, 10.5), (10.3, 10.5, 10.1, 10.2)],
    "pattern_doji_star": [(10.8, 10.9, 9.8, 10.0), (9.7, 9.8, 9.6, 9.7)],
    "pattern_hammer": [(11, 11.3, 10.9, 11.2), (10.8, 11.1, 10.7, 11.0), (10.5, 10.9, 10.4, 10.8),
                       (10.2, 10.6, 10.1, 10.5), (10, 10.4, 9.9, 10.3), (9.35, 9.45, 8.8, 9.4)],
    "pattern_shooting_star": [(10, 10.3, 9.9, 10.2), (10.2, 10.6, 10.1, 10.5), (10.5, 10.9, 10.4, 10.8),
                              (10.8, 11.1, 10.7, 11.0), (11, 11.3, 10.9, 11.2), (11.25, 11.95, 11.2, 11.3)],
    "pattern_bullish_engulfing": [(10.4, 10.5, 9.9, 10.0), (9.95, 10.45, 9.9, 10.4)],
    "pattern_bearish_engulfing": [(9.9, 10.4, 9.85, 10.3), (10.35, 10.4, 9.85, 9.9)],
    "pattern_morning_star": [(10, 10.05, 9.4, 9.5), (9.35, 9.45, 9.3, 9.35), (9.6, 10.0, 9.55, 9.9)],
    # ---- 单根 ----
    "pattern_gravestone_doji": [(10, 11, 10.0, 10.02)],
    "pattern_dragonfly_doji": [(10, 10.02, 9, 10.0)],
    "pattern_long_legged_doji": [(10, 11, 9, 10.05)],
    "pattern_hanging_man": [(8, 8.3, 7.9, 8.2), (8.2, 8.6, 8.1, 8.5), (8.5, 9, 8.4, 8.9),
                            (8.9, 9.4, 8.8, 9.3), (9.3, 9.8, 9.2, 9.7), (10.35, 10.45, 9.8, 10.4)],
    "pattern_takuri": [(10, 10.05, 9, 10.0)],
    "pattern_inverted_hammer": [(11, 11.3, 10.9, 11.2), (10.8, 11.1, 10.7, 11.0), (10.5, 10.9, 10.4, 10.8),
                                (10.2, 10.6, 10.1, 10.5), (10, 10.4, 9.9, 10.3), (8.95, 9.6, 8.9, 9.0)],
    "pattern_marubozu": [(10, 11, 10, 11)],
    "pattern_closing_marubozu": [(10, 11, 10.02, 10.95)],
    "pattern_spinning_top": [(10, 10.8, 9.2, 10.2)],
    "pattern_high_wave": [(10, 11, 9, 10.2)],
    "pattern_long_line": [(10, 11, 9.98, 10.95)],
    "pattern_short_line": [(10, 10.3, 9.82, 10.12)],
    "pattern_belt_hold_bull": [(10, 11, 10, 10.9)],
    "pattern_belt_hold_bear": [(11, 11, 10, 10.05)],
    "pattern_counterattack_bull": [(11, 11.0, 9.95, 10.0), (9.5, 11.0, 9.4, 10.0)],
    "pattern_counterattack_bear": [(9.5, 10.5, 9.45, 10.4), (10.9, 11.0, 10.3, 10.4)],
    "pattern_separating_lines_bull": [(10.4, 10.5, 9.4, 9.5), (10.4, 10.9, 10.35, 10.8)],
    "pattern_separating_lines_bear": [(9.5, 10.5, 9.45, 10.4), (9.5, 9.55, 9.0, 9.1)],
    "pattern_matching_low": [(10, 10.1, 9, 9.05), (9.1, 9.2, 9.0, 9.06)],
    # ---- 两根 ----
    "pattern_dark_cloud_cover": [(10, 10.5, 9.9, 10.4), (10.5, 10.6, 10.1, 10.15)],
    "pattern_piercing": [(10, 10.1, 9.5, 9.6), (9.5, 9.9, 9.4, 9.85)],
    "pattern_on_neck": [(10, 10.1, 9, 9.05), (8.9, 9.1, 8.85, 9.05)],
    "pattern_in_neck": [(10, 10.1, 9, 9.05), (8.9, 9.2, 8.85, 9.1)],
    "pattern_thrusting_neck": [(10, 10.1, 9, 9.05), (8.9, 9.6, 8.85, 9.4)],
    "pattern_harami": [(10, 10.5, 9.5, 10.8), (10.1, 10.45, 9.6, 10.3)],
    "pattern_harami_cross": [(10, 10.5, 9.5, 10.8), (10.1, 10.45, 9.6, 10.1)],
    "pattern_homing_pigeon": [(10.8, 10.9, 9.9, 10.0), (10.3, 10.35, 10.0, 10.15)],
    "pattern_gap_side_side_white_up": [(10, 10.2, 9.9, 10.1), (10.3, 10.9, 10.25, 10.8), (10.5, 11.0, 10.4, 10.9)],
    "pattern_gap_side_side_white_down": [(10, 10.1, 9.8, 9.9), (9.7, 9.75, 9.1, 9.2), (9.5, 9.6, 9.0, 9.1)],
    "pattern_tasuki_gap_up": [(10, 10.2, 9.9, 10.1), (10.3, 10.9, 10.25, 10.8), (10.5, 10.85, 10.4, 10.45)],
    "pattern_tasuki_gap_down": [(10, 10.1, 9.8, 9.9), (9.7, 9.75, 9.1, 9.2), (9.4, 9.55, 9.35, 9.5)],
    "pattern_upside_gap_two_crows": [(10, 10.5, 9.9, 10.4), (10.6, 10.9, 10.5, 10.55), (10.75, 10.8, 10.45, 10.5)],
    "pattern_xside_gap_3_methods_up": [(10, 10.2, 9.9, 10.1), (10.3, 10.9, 10.25, 10.8), (10.7, 10.75, 10.3, 10.5)],
    "pattern_xside_gap_3_methods_down": [(10, 10.1, 9.8, 9.9), (9.7, 9.75, 9.1, 9.2), (9.3, 9.65, 9.25, 9.5)],
    "pattern_kicking": [(10, 10, 9.05, 9.05), (10.2, 11, 10.2, 11)],
    "pattern_kicking_by_length": [(10, 10, 9.05, 9.05), (10.2, 11.15, 10.2, 11.15)],
    # ---- 三根及以上 ----
    "pattern_morning_doji_star": [(10, 10.1, 9.4, 9.5), (9.2, 9.3, 9.1, 9.2), (9.6, 10.0, 9.55, 9.9)],
    "pattern_evening_star": [(9.5, 10.1, 9.4, 10.0), (10.1, 10.5, 10.05, 10.3), (10.2, 10.3, 9.6, 9.65)],
    "pattern_evening_doji_star": [(9.5, 10.1, 9.4, 10.0), (10.2, 10.4, 10.18, 10.22), (10.15, 10.2, 9.5, 9.6)],
    "pattern_abandoned_baby_bull": [(10, 10.1, 9.5, 9.6), (9.25, 9.35, 9.24, 9.25), (9.4, 10.0, 9.4, 9.95)],
    "pattern_abandoned_baby_bear": [(9.5, 10.1, 9.4, 10.0), (10.22, 10.4, 10.25, 10.23), (10.1, 10.24, 9.5, 9.55)],
    "pattern_three_white_soldiers": [(10, 10.6, 9.9, 10.5), (10.2, 10.9, 10.1, 10.8), (10.5, 11.2, 10.4, 11.1)],
    "pattern_three_black_crows": [(11, 11.1, 10.4, 10.5), (10.8, 10.9, 10.1, 10.2), (10.5, 10.6, 9.8, 9.9)],
    "pattern_identical_three_crows": [(11, 11.1, 10.4, 10.5), (10.5, 10.55, 9.9, 10.0), (10.0, 10.05, 9.4, 9.5)],
    "pattern_advance_block": [(10, 10.5, 9.95, 10.45), (10.1, 10.9, 10.05, 10.8), (10.3, 11.0, 10.25, 10.85)],
    "pattern_stalled_pattern": [(10, 10.5, 9.95, 10.45), (10.1, 10.9, 10.05, 10.8), (10.82, 10.95, 10.7, 10.85)],
    "pattern_three_stars_in_south": [(10, 10.05, 8.0, 9.0), (9.1, 9.15, 8.9, 8.95), (9.0, 9.05, 8.9, 8.98)],
    "pattern_three_inside_up": [(10, 10.1, 9.4, 9.5), (9.6, 9.7, 9.55, 9.65), (9.7, 10.0, 9.65, 9.9)],
    "pattern_three_inside_down": [(9.5, 10.1, 9.4, 10.0), (9.9, 9.95, 9.7, 9.75), (9.7, 9.75, 9.3, 9.4)],
    "pattern_three_outside_up": [(10, 10.1, 9.6, 9.7), (9.65, 10.1, 9.6, 10.05), (10.05, 10.4, 10.0, 10.3)],
    "pattern_three_outside_down": [(9.7, 10.1, 9.6, 10.0), (10.05, 10.1, 9.6, 9.65), (9.6, 9.62, 9.3, 9.35)],
    "pattern_three_line_strike_bull": [(10, 10.05, 9.5, 9.55), (9.6, 9.65, 9.1, 9.15), (9.2, 9.25, 8.7, 8.75), (8.7, 10.3, 8.65, 10.2)],
    "pattern_three_line_strike_bear": [(9, 8.95, 9.5, 9.45), (9.4, 9.35, 9.85, 9.8), (9.8, 9.75, 10.3, 10.25), (10.3, 10.35, 8.7, 8.8)],
    "pattern_rising_three_methods": [(10, 10.6, 9.95, 10.5), (10.4, 10.5, 10.2, 10.3), (10.35, 10.45, 10.15, 10.25), (10.3, 10.4, 10.1, 10.2), (10.25, 10.8, 10.2, 10.7)],
    "pattern_falling_three_methods": [(10.5, 10.05, 9.4, 9.5), (9.6, 9.8, 9.5, 9.7), (9.7, 9.9, 9.6, 9.8), (9.8, 10, 9.7, 9.9), (9.75, 9.8, 9.2, 9.3)],
    "pattern_breakaway_bull": [(10, 10.05, 9.2, 9.3), (9.0, 9.1, 8.5, 8.6), (8.55, 8.65, 8.3, 8.4), (8.35, 8.5, 8.2, 8.45), (8.5, 8.9, 8.45, 8.7)],
    "pattern_breakaway_bear": [(9.2, 10, 9.15, 9.9), (10.1, 10.6, 10.05, 10.5), (10.55, 11.1, 10.5, 11.0), (11.05, 11.2, 10.9, 11.1), (11.0, 11.15, 10.3, 10.4)],
    "pattern_ladder_bottom": [(10, 10.05, 9.5, 9.55), (9.55, 9.6, 9.05, 9.1), (9.1, 9.15, 8.6, 8.65), (8.6, 8.9, 8.45, 8.5), (8.55, 8.9, 8.5, 8.75)],
    "pattern_stick_sandwich": [(10, 10.05, 9.5, 9.55), (10.1, 10.6, 10.05, 10.5), (10.2, 10.25, 9.5, 9.56)],
    "pattern_tri_star": [(10, 10.15, 9.85, 10.0), (10.3, 10.4, 10.2, 10.3), (10.5, 10.65, 10.35, 10.5)],
    "pattern_unique_three_river": [(10, 10.05, 9.0, 9.2), (9.3, 9.35, 8.3, 9.0), (8.9, 9.1, 8.85, 8.95)],
    "pattern_mat_hold": [(10, 10.6, 9.95, 10.5), (10.7, 10.72, 10.4, 10.65), (10.5, 10.6, 10.3, 10.4), (10.4, 10.5, 10.25, 10.3), (10.3, 10.8, 10.25, 10.7)],
    "pattern_hikkake_bull": [(10, 10.5, 9.5, 10.0), (10.2, 10.4, 9.6, 9.9), (9.7, 9.8, 9.4, 9.5), (9.6, 10.5, 9.55, 10.45)],
    "pattern_hikkake_bear": [(10, 10.5, 9.5, 10.0), (10.2, 10.4, 9.6, 9.9), (10.3, 10.6, 10.25, 10.45), (10.4, 10.45, 9.5, 9.55)],
    "pattern_hikkake_mod_bull": [(10, 10.5, 9.5, 10.0), (10.2, 10.4, 9.6, 9.9), (9.7, 9.8, 9.55, 9.58), (9.6, 10.5, 9.55, 10.45)],
    "pattern_hikkake_mod_bear": [(10, 10.5, 9.5, 10.0), (10.2, 10.4, 9.6, 9.9), (10.3, 10.45, 10.42, 10.45), (10.4, 10.45, 9.5, 9.55)],
    "pattern_concealing_baby_swallow": [(10, 10.05, 9.3, 9.4), (9.4, 9.45, 8.7, 8.8), (8.6, 8.65, 7.5, 8.0), (8.75, 8.8, 7.3, 7.9)],
}


# ---------------- 注册元数据 ----------------


def test_patterns_full_registered():
    assert len(PATTERN_NAMES) >= 61, PATTERN_NAMES
    for name in PATTERN_NAMES:
        meta = INDICATORS.meta(name)
        assert meta["category"] == "pattern", name
        assert meta["pane"] == "sub", name
        assert meta["outputs"] == [name], name
        assert set(meta["inputs"]) == {"open", "high", "low", "close"}, name


def test_instock_61_covers():
    """InStock 61 形态清单里的每个 TA-Lib 形态都有对等注册项（按族映射）。"""
    families = {
        "两只乌鸦": "pattern_upside_gap_two_crows",
        "两只乌鸦(低开版)": "pattern_two_crows",
        "十字星(胎星)": "pattern_doji_star",
        "黄包车夫": "pattern_rickshaw_man",
        "三只乌鸦": "pattern_three_black_crows",
        "三个白兵": "pattern_three_white_soldiers",
        "南方三星": "pattern_three_stars_in_south",
        "三线打击": "pattern_three_line_strike_bull",
        "十字晨星": "pattern_morning_doji_star",
        "十字暮星": "pattern_evening_doji_star",
        "晨星": "pattern_morning_star",
        "暮星": "pattern_evening_star",
        "弃婴": "pattern_abandoned_baby_bull",
        "大敌当前": "pattern_advance_block",
        "捉腰带线": "pattern_belt_hold_bull",
        "脱离": "pattern_breakaway_bull",
        "收盘缺影线": "pattern_closing_marubozu",
        "藏婴吞没": "pattern_concealing_baby_swallow",
        "反击线": "pattern_counterattack_bull",
        "乌云压顶": "pattern_dark_cloud_cover",
        "十字星": "pattern_doji",
        "蜻蜓十字": "pattern_dragonfly_doji",
        "吞噬模式": "pattern_bullish_engulfing",
        "墓碑十字": "pattern_gravestone_doji",
        "锤头": "pattern_hammer",
        "上吊线": "pattern_hanging_man",
        "母子线": "pattern_harami",
        "十字孕线": "pattern_harami_cross",
        "风高浪大线": "pattern_high_wave",
        "陷阱": "pattern_hikkake_bull",
        "修正陷阱": "pattern_hikkake_mod_bull",
        "家鸽": "pattern_homing_pigeon",
        "三胞胎乌鸦": "pattern_identical_three_crows",
        "颈内线": "pattern_in_neck",
        "颈上线": "pattern_on_neck",
        "倒锤头": "pattern_inverted_hammer",
        "反冲形态": "pattern_kicking",
        "梯底": "pattern_ladder_bottom",
        "长脚十字": "pattern_long_legged_doji",
        "长蜡烛": "pattern_long_line",
        "光头光脚": "pattern_marubozu",
        "相同低价": "pattern_matching_low",
        "铺垫": "pattern_mat_hold",
        "刺透形态": "pattern_piercing",
        "上升/下降三法": "pattern_rising_three_methods",
        "分离线": "pattern_separating_lines_bull",
        "射击之星": "pattern_shooting_star",
        "短蜡烛": "pattern_short_line",
        "纺锤": "pattern_spinning_top",
        "停顿形态": "pattern_stalled_pattern",
        "条形三明治": "pattern_stick_sandwich",
        "探水竿": "pattern_takuri",
        "跳空并列阴阳线": "pattern_tasuki_gap_up",
        "插入": "pattern_thrusting_neck",
        "三星": "pattern_tri_star",
        "奇特三河床": "pattern_unique_three_river",
        "向上跳空的两只乌鸦": "pattern_upside_gap_two_crows",
        "上升/下降跳空三法": "pattern_xside_gap_3_methods_up",
        "三个方法": "pattern_rising_three_methods",
        "三内部上涨和下跌": "pattern_three_inside_up",
        "三外部上涨和下跌": "pattern_three_outside_up",
    }
    missing = [k for k, v in families.items() if v not in PATTERN_NAMES]
    assert not missing, missing


# ---------------- 教科书正例 ----------------


@pytest.mark.parametrize("name", sorted(TEXTBOOK))
def test_textbook_positive(name: str):
    fn_rows = TEXTBOOK[name]
    df = ohlc(fn_rows)
    out = compute(name, df)
    col = out[name]
    assert col.dtype == pl.Int8, name
    last = col[-1]
    assert last == 1, f"{name} 教科书正例未命中（最后一根 = {last}）"
    # 除确认日外不得出现 1 之外的值
    assert set(v for v in col.to_list()[:-1] if v is not None) <= {0}, name


# ---------------- 门禁：前缀不变性 ----------------


@pytest.mark.parametrize("name", PATTERN_NAMES)
def test_no_lookahead(name: str):
    df = random_ohlc()
    assert_no_lookahead(lambda d: compute(name, d), df, out_cols=[name])


@pytest.mark.parametrize("name", PATTERN_NAMES)
def test_no_lookahead_structured(name: str):
    """教科书正例数据也要过门禁（形态结构数据比随机数据更容易踩 shift 边界）。"""
    rows = TEXTBOOK.get(name)
    if not rows:
        return
    df = ohlc(rows)
    assert_no_lookahead(lambda d: compute(name, d), df, out_cols=[name])


# ---------------- 一字板与缺失数据 ----------------


@pytest.mark.parametrize("name", PATTERN_NAMES)
def test_flat_board_no_signal(name: str):
    """一字板（高=低，A 股涨跌停常见）：输出 0/null，不炸不误报。"""
    n = 10
    df = ohlc([(10.0, 10.0, 10.0, 10.0)] * n)
    out = compute(name, df)
    vals = out[name].to_list()
    assert all(v in (0, None) for v in vals), name


@pytest.mark.parametrize("name", PATTERN_NAMES)
def test_null_ohlc_propagates(name: str):
    df = random_ohlc(20)
    df = df.with_columns(pl.when(pl.arange(0, pl.len()) == 10)
                         .then(None).otherwise(pl.col("close")).alias("close"))
    out = compute(name, df)
    assert out[name][10] is None, name


# ---------------- 参数校验 fail-loudly ----------------


def test_param_validation():
    df = random_ohlc(10)
    with pytest.raises(ValueError):
        add_gravestone_doji(df, body_ratio=-1)
    with pytest.raises(ValueError):
        add_dark_cloud_cover(df, min_range_pct=2.0)
    with pytest.raises(ValueError):
        add_takuri(df, body_max=0)
