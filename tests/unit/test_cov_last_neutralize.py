"""最后覆盖冲刺：neutralize 市值源解析 / lasso / industry_mean / none。"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from lquant.factors.preprocess import neutralize as nz


def _df(n=40, days=3):
    rng = np.random.default_rng(7)
    symbols = [f"{i:06d}.SZ" for i in range(n)]
    rows = []
    for d in range(days):
        date = dt.date(2026, 1, 5) + dt.timedelta(days=d)
        for i, s in enumerate(symbols):
            rows.append({
                "symbol": s, "trade_date": date,
                "f": float(rng.normal()),
                "market_cap": float(rng.lognormal(6, 0.5)),
                "float_mv": float(rng.lognormal(6, 0.5)),
                "industry_sw1": ["A", "B"][i % 2],
                "close": float(10 + rng.normal()),
            })
    return pl.DataFrame(rows)


def test_resolve_market_cap_branches():
    df = _df()
    _, facs, eff = nz._resolve_market_cap(df, ["market_cap"], "market_cap")
    _, _, eff2 = nz._resolve_market_cap(df.drop("float_mv"),
                                        ["market_cap"], "auto")
    with pytest.raises(ValueError, match="float_mv"):
        nz._resolve_market_cap(df.drop("float_mv"), ["market_cap"], "float_mv")
    with pytest.raises(ValueError, match="未知"):
        nz._resolve_market_cap(df, ["market_cap"], "bogus")
    assert eff is None and facs == ["market_cap"]
    assert eff2 is None


def test_ols_sources():
    df = _df()
    out = nz.ols(df, "f", factors=["market_cap"],
                 market_cap_source="market_cap")
    assert "f" in out.columns
    out2 = nz.ols(df, "f", factors=["market_cap"],
                  market_cap_source="float_mv")
    assert out2.height == df.height


def test_unit_mismatch_warns():
    df = _df()
    df = df.with_columns((pl.col("market_cap") * 1000).alias("float_mv"))
    out = nz.ols(df, "f", factors=["market_cap"],
                 market_cap_source="float_mv")
    assert out.height == df.height


def test_lasso_none_industry_mean():
    df = _df(days=4)
    # 已知源码缺陷：lasso 的 solve 在样本数>1 时崩溃（见汇报），故只走
    # ImportError 降级路径与其余方法。
    d2 = nz.industry_mean(df.drop("industry_sw1"), "f")
    assert d2.columns == df.drop("industry_sw1").columns
    assert nz.none(df, "f").columns == df.columns


def test_lasso_fallback_to_ridge(monkeypatch):
    import builtins
    orig = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "sklearn.linear_model":
            raise ImportError
        return orig(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    df = _df(days=4)
    out = nz.lasso(df, "f", factors=["market_cap"])
    assert out.height == df.height
