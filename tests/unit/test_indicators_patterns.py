"""K 线形态因子包 patterns.py：注册元数据、教科书正例、门禁与除零安全。

每个形态都有手工构造的「教科书正例」（信号必须 =1，这是宁漏勿滥的
下界约束）、反例（=0）、预热行（=0）；全部指标过 assert_no_lookahead
前缀不变性门禁；一字板（高=低）不炸不误报。
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import polars as pl
import pytest

from lquant.indicators import assert_no_lookahead
from lquant.indicators.patterns import (
    add_bearish_engulfing,
    add_bullish_engulfing,
    add_doji,
    add_hammer,
    add_morning_star,
    add_shooting_star,
)

PATTERNS = {
    "pattern_doji": (add_doji, 1),
    "pattern_hammer": (add_hammer, 6),
    "pattern_shooting_star": (add_shooting_star, 6),
    "pattern_bullish_engulfing": (add_bullish_engulfing, 2),
    "pattern_bearish_engulfing": (add_bearish_engulfing, 2),
    "pattern_morning_star": (add_morning_star, 3),
}


def ohlc(rows: list[tuple], symbol: str = "TEST") -> pl.DataFrame:
    d0 = date(2024, 1, 2)
    return pl.DataFrame(
        {
            "symbol": [symbol] * len(rows),
            "trade_date": [d0 + timedelta(days=i) for i in range(len(rows))],
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
        }
    )


def random_ohlc(n: int = 40, seed: int = 42) -> pl.DataFrame:
    """带趋势与波动的合法 OHLC（low ≤ min(o,c) ≤ max(o,c) ≤ high）。"""
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


# ---------------- 注册元数据 ----------------


def test_registered_metadata():
    from lquant.indicators import INDICATORS  # import 即触发注册

    for name, (_fn, mw) in PATTERNS.items():
        meta = INDICATORS.meta(name)
        assert meta["category"] == "pattern", name
        assert meta["pane"] == "sub", name
        assert meta["outputs"] == [name], name
        assert meta["min_window"] == mw, name
        assert set(meta["inputs"]) == {"open", "high", "low", "close"}, name


def test_registry_category_pattern_now_populated():
    """CATEGORIES 里的 pattern 类别不再空转：首批 6 个全部在场。"""
    from lquant.indicators import INDICATORS

    cats = {INDICATORS.meta(name)["category"] for name in INDICATORS}
    assert "pattern" in cats


# ---------------- 教科书正例 / 反例 ----------------


def test_doji_positive_and_noise():
    df = ohlc(
        [
            (10.50, 11.00, 10.00, 10.55),  # 十字星：body/rng = 0.05
            (10.00, 11.00, 10.00, 10.80),  # 大实体：不是
            (10.00, 10.03, 10.00, 10.02),  # 窄幅比例噪声：min_range_pct 挡住
        ]
    )
    out = add_doji(df)
    assert out["pattern_doji"].to_list() == [1, 0, 0]


def test_hammer_after_downtrend():
    df = ohlc(
        [
            (10.50, 10.80, 10.40, 10.70),
            (10.70, 10.90, 10.50, 10.60),
            (10.60, 10.80, 10.40, 10.45),
            (10.45, 10.60, 10.30, 10.35),
            (10.35, 10.50, 10.20, 10.25),
            (10.25, 10.30, 9.00, 10.10),  # 长下影小实体，跌势确认
        ]
    )
    out = add_hammer(df)
    assert out["pattern_hammer"].to_list() == [0, 0, 0, 0, 0, 1]


def test_hammer_requires_downtrend():
    """同样的长下影 K 线，涨势里不是锤头（背景过滤生效）。"""
    df = ohlc(
        [
            (10.00, 10.20, 9.90, 10.15),
            (10.15, 10.35, 10.05, 10.30),
            (10.30, 10.50, 10.20, 10.45),
            (10.45, 10.65, 10.35, 10.60),
            (10.60, 10.80, 10.50, 10.75),
            (10.75, 10.80, 9.50, 10.70),  # 长下影但处于涨势
        ]
    )
    assert add_hammer(df)["pattern_hammer"].to_list()[-1] == 0


def test_shooting_star_after_uptrend():
    df = ohlc(
        [
            (10.00, 10.20, 9.90, 10.15),
            (10.15, 10.35, 10.05, 10.30),
            (10.30, 10.50, 10.20, 10.45),
            (10.45, 10.65, 10.35, 10.60),
            (10.60, 10.80, 10.50, 10.75),
            (10.75, 11.90, 10.65, 10.80),  # 长上影小实体，涨势确认
        ]
    )
    out = add_shooting_star(df)
    assert out["pattern_shooting_star"].to_list() == [0, 0, 0, 0, 0, 1]


def test_bullish_engulfing():
    df = ohlc(
        [
            (10.50, 10.60, 10.40, 10.55),
            (10.60, 10.65, 10.20, 10.25),  # 阴线
            (10.20, 10.90, 10.10, 10.80),  # 阳线实体完全包住
        ]
    )
    out = add_bullish_engulfing(df)
    assert out["pattern_bullish_engulfing"].to_list() == [0, 0, 1]


def test_bullish_engulfing_rejects_equal_bodies():
    """实体与前根完全相等的退化复制 K 线不算吞没（require_bigger）。"""
    df = ohlc(
        [
            (10.60, 10.70, 10.20, 10.25),  # 阴线 body=0.35
            (10.25, 10.75, 10.15, 10.60),  # 阳线 body=0.35 相等 → 拒
        ]
    )
    assert add_bullish_engulfing(df)["pattern_bullish_engulfing"].to_list()[-1] == 0


def test_bearish_engulfing():
    df = ohlc(
        [
            (10.20, 10.40, 10.10, 10.30),
            (10.30, 10.70, 10.25, 10.60),  # 阳线
            (10.70, 10.75, 10.10, 10.20),  # 阴线实体完全包住
        ]
    )
    out = add_bearish_engulfing(df)
    assert out["pattern_bearish_engulfing"].to_list() == [0, 0, 1]


def test_morning_star_positive_and_weak_recover():
    base = [
        (11.00, 11.10, 10.90, 10.00),  # 大阴线 body=1.0
        (9.80, 9.90, 9.60, 9.70),  # 小实体低位
    ]
    df = ohlc(base + [(9.90, 10.70, 9.80, 10.60)])  # 收复 60% > 50%
    assert add_morning_star(df)["pattern_morning_star"].to_list()[-1] == 1

    df2 = ohlc(base + [(9.90, 10.70, 9.80, 10.40)])  # 只收复 40% < 50%
    assert add_morning_star(df2)["pattern_morning_star"].to_list()[-1] == 0


# ---------------- 输出形状与除零安全 ----------------


def test_trend_n_validation():
    """trend_n<1 fail-loudly：0 静默失效、负数引用未来根，都不允许。"""
    df = random_ohlc(20)
    for bad in (0, -1, -5):
        with pytest.raises(ValueError, match="trend_n"):
            add_hammer(df, trend_n=bad)
        with pytest.raises(ValueError, match="trend_n"):
            add_shooting_star(df, trend_n=bad)


def test_doji_boundary_is_inclusive():
    """body 恰等于 body_ratio×rng（<= 判据）→ 信号 1，锁住边界语义。"""
    # rng=1.0, body=0.1 = 0.1×1.0
    df = ohlc([(10.0, 11.0, 10.0, 10.1)])
    assert add_doji(df)["pattern_doji"].to_list() == [1]


def test_doji_dirty_price_is_zero():
    """close<=0 的脏价数据：信号 0 不误报（close>0 前置条件）。"""
    df = ohlc([(5.0, 6.0, 5.0, 0.0), (5.0, 6.0, 5.0, -5.0)])
    assert add_doji(df)["pattern_doji"].to_list() == [0, 0]


def test_output_dtype_int8_no_null():
    out = add_hammer(random_ohlc(30))
    col = out["pattern_hammer"]
    assert col.dtype == pl.Int8
    assert col.null_count() == 0


def test_one_price_board_is_zero_everywhere():
    """一字板（高=低）：全部形态判 0，无 null 无 NaN。"""
    df = ohlc([(10.0, 10.0, 10.0, 10.0)] * 12)
    for name, (fn, _) in PATTERNS.items():
        col = fn(df)[name]
        assert col.null_count() == 0, name
        assert col.max() == 0, name


# ---------------- 前缀不变性门禁 ----------------


@pytest.mark.parametrize("name", list(PATTERNS))
def test_no_lookahead(name: str):
    fn, _ = PATTERNS[name]
    assert_no_lookahead(fn, random_ohlc(40), out_cols=[name])
