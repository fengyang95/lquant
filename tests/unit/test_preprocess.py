"""预处理层回归：中性化必须真的剥掉暴露，正交化必须真的消相关。"""
import numpy as np
import polars as pl
import pytest

from lquant.factors.preprocess import list_methods, run


@pytest.fixture()
def cross_section() -> pl.DataFrame:
    """3 个日期 × 20 标的的截面。

    factor 与市值秩（i）强相关；factor_b 是 factor 与独立噪声的混合 ——
    相关性构造必须存在，否则断言空转。
    注意不能写成 factor_b = a*factor + b（精确仿射，矩阵秩 1，任何
    正交化都无法消相关，corr=1 数学上正确）。
    """
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


# ------------------------------------------------------ float_mv 优先（spec §2.4）

def _mv_section(fmv: list, factor: list[float], n: int = 30) -> pl.DataFrame:
    """单日 30 标的最小截面。eff 列 = 预期的有效市值暴露，供断言对照。"""
    eff = [(f if f is not None else (i + 1) * 1e10) for i, f in enumerate(fmv)]
    return pl.DataFrame({
        "trade_date": ["2026-01-05"] * n,
        "symbol": [f"S{i:03d}" for i in range(n)],
        "factor": factor,
        "market_cap": [(i + 1) * 1e10 for i in range(n)],
        "float_mv": fmv,
        "eff": eff,
    })


def _neutralize_cap(df: pl.DataFrame, *, with_fmv: bool) -> pl.DataFrame:
    steps = [{"op": "neutralize", "method": "ols",
              "params": {"factors": ["market_cap"]}}]
    d = df if with_fmv else df.drop("float_mv")
    return run(d, "factor", steps, keep_original=True)


def test_neutralize_prefers_float_mv_when_present():
    """float_mv 有值 -> 市值暴露用 float_mv：剥后与 float_mv 无关；
    若退回 market_cap 则残留显著相关（对照证明确实换了暴露列）。"""
    n = 30
    rng = np.random.default_rng(11)
    fmv = [abs(i - 15) * 2e9 for i in range(n)]      # V 形：与 cap 不共线
    factor = [f / 1e9 + rng.normal(0, 0.005) for f in fmv]
    df = _mv_section(fmv, factor)

    out = _neutralize_cap(df, with_fmv=True)
    out_wo = _neutralize_cap(df, with_fmv=False)
    c_with = out.select(pl.corr("factor_clean", "eff")).item()
    c_wo = out_wo.select(pl.corr("factor_clean", "eff")).item()
    assert abs(c_with) < 0.05
    assert abs(c_wo) > 0.5


def test_neutralize_falls_back_when_float_mv_all_null():
    """float_mv 全 null -> 回退 market_cap，行为与旧路径一致。"""
    n = 30
    rng = np.random.default_rng(11)
    factor = [0.01 * i + rng.normal(0, 0.005) for i in range(n)]
    df = _mv_section([None] * n, factor).with_columns(
        pl.col("float_mv").cast(pl.Float64))
    out = _neutralize_cap(df, with_fmv=True)
    assert abs(out.select(pl.corr("factor_clean", "market_cap")).item()) < 0.05


def test_neutralize_mixed_uses_float_mv_per_row():
    """混合：float_mv 非空行用 float_mv，null 行回退 market_cap。
    对照：退回 market_cap 时残留对 coalesce 暴露显著相关。"""
    n = 30
    rng = np.random.default_rng(13)
    fmv = [(n - i) * 2e9 if i % 2 == 0 else None for i in range(n)]
    eff = [(f if f is not None else (i + 1) * 1e10) for i, f in enumerate(fmv)]
    factor = [e / 1e9 + rng.normal(0, 0.005) for e in eff]
    df = _mv_section(fmv, factor)

    out = _neutralize_cap(df, with_fmv=True)
    out_wo = _neutralize_cap(df, with_fmv=False)
    c_with = out.select(pl.corr("factor_clean", "eff")).item()
    c_wo = out_wo.select(pl.corr("factor_clean", "eff")).item()
    assert abs(c_with) < 0.05
    assert abs(c_wo) > 0.5


# ------------------------------------------------------ 量纲比率告警（复审补充）

def test_neutralize_unit_ratio_warns_and_stays_quiet(monkeypatch):
    """float_mv/market_cap 中位数比率越界 [0.05, 1.2] -> warning 一次；
    比率正常（同一量纲）-> 不触发。只告警，不改数据。"""
    import lquant.factors.preprocess.neutralize as nz

    calls: list[str] = []
    monkeypatch.setattr(nz.logger, "warning",
                        lambda msg, *a, **kw: calls.append(str(msg)))

    def _mk(fmv_scale: float) -> pl.DataFrame:
        n = 10
        return pl.DataFrame({
            "trade_date": ["2026-01-05"] * n,
            "symbol": [f"S{i:03d}" for i in range(n)],
            "factor": [float(i) for i in range(n)],
            "market_cap": [(i + 1) * 1e10 for i in range(n)],
            "float_mv": [(i + 1) * 1e10 * fmv_scale for i in range(n)],
        })

    # 比率 1.0（一致）-> 静默；比率 0.001（float_mv 少 3 个零）-> 告警
    nz._resolve_market_cap(_mk(1.0), ["market_cap"], "auto")
    assert calls == []
    nz._resolve_market_cap(_mk(0.001), ["market_cap"], "auto")
    assert len(calls) == 1
    assert "0.00" in calls[0]
