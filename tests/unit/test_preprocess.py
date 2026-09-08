"""预处理层回归：中性化必须真的剥掉暴露，正交化必须真的消相关。"""
import polars as pl
import pytest

from lquant.factors.preprocess import list_methods, run


@pytest.fixture()
def cross_section() -> pl.DataFrame:
    """3 个日期 × 20 标的的截面。

    factor 与市值秩（i）强相关；factor_b 是 factor 与独立噪声的混合 ——
    高度相关但秩为 2。注意不能写成 factor_b = a*factor + b（精确仿射，
    矩阵秩 1，任何正交化都无法消相关，corr=1 数学上正确）。
    """
    import numpy as np

    rng = np.random.default_rng(7)
    rows = []
    for d in ["2026-01-05", "2026-01-06", "2026-01-07"]:
        for i in range(20):
            cap = (i + 1) * 1e10
            factor = 0.01 * i + rng.normal(0, 0.005)   # 与市值秩强相关
            factor_b = 0.8 * factor + 0.2 * rng.normal(0, 0.01)
            rows.append({"trade_date": d, "symbol": f"S{i:03d}",
                         "factor": factor, "factor_b": factor_b,
                         "market_cap": cap,
                         "industry_sw1": f"ind{i % 3}"})
    return pl.DataFrame(rows)


def test_registry_has_core_methods():
    names = {(m["stage"], m["name"]) for m in list_methods()}
    assert ("winsorize", "mad") in names
    assert ("standardize", "zscore") in names
    assert ("neutralize", "ols") in names
    assert ("orthogonalize", "symmetric") in names


def test_neutralize_removes_cap_exposure(cross_section):
    out = run(cross_section, "factor",
              [{"op": "neutralize", "method": "ols",
                "params": {"factors": ["market_cap"]}}],
              keep_original=True)
    before = out.select(pl.corr("factor", "market_cap")).item()
    after = out.select(pl.corr("factor_clean", "market_cap")).item()
    assert abs(before) > 0.5          # 构造的相关性必须存在
    assert abs(after) < 0.05          # 中性化后必须接近 0


def test_orthogonalize_kills_correlation(cross_section):
    out = run(cross_section, ["factor", "factor_b"],
              [{"op": "standardize", "method": "zscore"},
               {"op": "orthogonalize", "method": "symmetric"}])
    corr = out.select(pl.corr("factor", "factor_b")).item()
    assert abs(corr) < 1e-8
