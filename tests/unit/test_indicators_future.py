"""未来函数检测（前缀不变性）—— 含**反向对照**。

反向对照是本文件的核心价值：把通达信居中 XMA 原样实现一遍，
断言检测器**必须**抓出它。否则这个检测器只是个摆设。
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from lquant.indicators import add_tiandao, add_volume_ratio, check_prefix_invariance
from lquant.indicators.future import (
    LookaheadViolation,
    _checkpoint_rows,
    _same,
    assert_no_lookahead,
)


def make_ohlcv(n: int = 90, seed: int = 3) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    return pl.DataFrame({
        "trade_date": [date(2026, 1, 1) + timedelta(days=i) for i in range(n)],
        "open": close,
        "high": close + rng.uniform(0, 1, n),
        "low": close - rng.uniform(0, 1, n),
        "close": close,
        "volume": rng.uniform(1e6, 5e6, n),
    })


# ---------- 反向对照：居中 XMA（未来数据）必须被抓住 ----------

def _xma_centered(expr: str | pl.Expr, k: int) -> pl.Expr:
    """通达信居中对称 XMA（错误示范，仅供对照）。"""
    h = k // 2
    eps = 1 - (k % 2)
    e = pl.col(expr) if isinstance(expr, str) else expr
    return e.rolling_mean(window_size=2 * h + 1 - eps, center=True, min_samples=1)


def _add_tiandao_centered(df: pl.DataFrame, n: int = 25) -> pl.DataFrame:
    hi = _xma_centered(_xma_centered("high", n), n)
    lo = _xma_centered(_xma_centered("low", n), n)
    return df.with_columns(
        (2 * hi - lo).alias("td_jinniu"),
        (2 * lo - hi).alias("td_jinzuan"),
    )


def test_detector_catches_centered_xma():
    """居中 XMA 引用未来数据 → 前缀不变性必然失败。"""
    df = make_ohlcv()
    bad = check_prefix_invariance(_add_tiandao_centered, df,
                                  out_cols=["td_jinniu", "td_jinzuan"])
    assert bad, "检测器漏检了居中 XMA —— 未来函数防线失效"
    assert all(isinstance(v, LookaheadViolation) for v in bad)
    assert {v.column for v in bad} == {"td_jinniu", "td_jinzuan"}


def test_assert_no_lookahead_raises_with_hint():
    df = make_ohlcv()
    with pytest.raises(AssertionError, match="未来函数"):
        assert_no_lookahead(_add_tiandao_centered, df, out_cols=["td_jinniu"])


# ---------- 正向：我们自己的实现必须通过 ----------

def test_truncated_tiandao_passes_prefix_invariance():
    df = make_ohlcv()
    assert check_prefix_invariance(add_tiandao, df,
                                   out_cols=["td_jinniu", "td_jinzuan",
                                             "td_gold_buy", "td_gold_sell"]) == []


def test_volume_ratio_passes_prefix_invariance():
    """量比分母用 shift(1) 且窗口不含当日 —— 同样必须前缀不变。"""
    df = make_ohlcv()
    assert check_prefix_invariance(add_volume_ratio, df,
                                   out_cols=["volume_ratio"]) == []


# ---------- 检测器自身行为 ----------

def test_checkpoint_rows_covers_last_row():
    rows = _checkpoint_rows(1000, None, 1)
    assert rows[-1] == 999                     # 末行必查（居中窗口的漂移在尾部）
    assert rows[0] >= 1
    assert len(rows) <= 25                     # 至多 24 个等距点 + 末行


def test_checkpoint_rows_explicit_sequence():
    assert _checkpoint_rows(50, [2, 10], 1) == [2, 10]
    assert _checkpoint_rows(50, [0, 99], 1) == []      # 越界过滤


def test_checkpoint_rows_empty_when_too_short():
    assert _checkpoint_rows(1, None, 1) == []
    assert _checkpoint_rows(0, None, 1) == []


def test_same_handles_null_and_nan():
    assert _same(None, None, 1e-9)
    assert _same(float("nan"), None, 1e-9)
    assert not _same(None, 1.0, 1e-9)
    assert not _same(1.0, None, 1e-9)
    assert _same(1.0, 1.0 + 1e-12, 1e-9)
    assert not _same(1.0, 1.1, 1e-9)
    assert _same("a", "a", 1e-9)               # 非数值列回退相等比较


def test_missing_out_col_raises_keyerror():
    df = make_ohlcv(30)
    with pytest.raises(KeyError, match="待检列不存在"):
        check_prefix_invariance(add_tiandao, df, out_cols=["nope"])


def test_empty_frame_returns_no_violation():
    assert check_prefix_invariance(add_tiandao, make_ohlcv(30).head(0),
                                   out_cols=["td_jinniu"]) == []


def test_date_col_absent_falls_back_to_row_index():
    df = make_ohlcv(40).drop("trade_date")
    bad = check_prefix_invariance(_add_tiandao_centered, df, out_cols=["td_jinniu"])
    assert bad and isinstance(bad[0].key, int)
