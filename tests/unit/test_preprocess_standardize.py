"""standardize（zscore/minmax/rank）与 Acklam 逆正态近似覆盖。"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from lquant.factors.preprocess import standardize as std


def _df():
    return pl.DataFrame({
        "trade_date": [date(2026, 1, d) for d in (5, 5, 6, 6)],
        "f": [1.0, 2.0, 3.0, 7.0],
    })


def test_inv_norm_extremes_and_center() -> None:
    assert std._inv_norm(0.0) == -8.0
    assert std._inv_norm(1.0) == 8.0
    assert std._inv_norm(0.5) == pytest.approx(0.0, abs=1e-6)
    # 双侧尾部（p<plow / p>phigh）
    assert std._inv_norm(0.001) == pytest.approx(-3.0902, abs=1e-3)
    assert std._inv_norm(0.999) == pytest.approx(3.0902, abs=1e-3)
    # 中段
    assert std._inv_norm(0.1) == pytest.approx(-1.2816, abs=1e-3)
    assert std._inv_norm(0.975) == pytest.approx(1.96, abs=1e-3)


def test_zscore_zero_std_guard() -> None:
    df = pl.DataFrame({
        "trade_date": [date(2026, 1, 5)] * 3,
        "f": [2.0, 2.0, 2.0],
    })
    out = std.zscore(df, "f")
    # std=0 → 除 1.0，得到离差 0
    assert out["f"].to_list() == [0.0, 0.0, 0.0]


def test_zscore_cross_section() -> None:
    out = std.zscore(_df(), "f")
    assert out["f"].mean() == pytest.approx(0.0, abs=1e-9)


def test_minmax_and_degenerate_span() -> None:
    out = std.minmax(_df(), "f")
    assert out["f"].min() == 0.0 and out["f"].max() == 1.0
    # 常数列 → span=1 兜底，值为 lo + (x-mn)
    const = pl.DataFrame({
        "trade_date": [date(2026, 1, 5)] * 2,
        "f": [3.0, 3.0],
    })
    out2 = std.minmax(const, "f")
    assert out2["f"].to_list() == [0.0, 0.0]


def test_minmax_custom_range() -> None:
    out = std.minmax(_df(), "f", lo=-1.0, hi=1.0)
    assert out["f"].min() == -1.0 and out["f"].max() == 1.0


def test_rank_uniform() -> None:
    # 同日 1,2 → 平均秩 1.5/2、2/2 截到 0.75；次日 3,7 → 0.5、0.75
    out = std.rank(_df(), "f")
    assert out["f"].to_list() == pytest.approx([0.5, 0.75, 0.5, 0.75])


def test_rank_normal() -> None:
    out = std.rank(_df(), "f", to="normal")
    assert out["f"][0] == pytest.approx(std._inv_norm(0.5), abs=1e-6)


def test_none_identity() -> None:
    df = _df()
    out = std.none(df, "f")
    assert out is df
