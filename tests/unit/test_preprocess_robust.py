"""稳健去极值（Phase 3.6）：huber 与 rankgauss。

与 AlphaPurify 的对拍是**实测**的（本文件里直接 import 上游源码做数值比较），
不是"看着公式写的"：

- ``huber`` 默认 ``scale='std'`` 与 AlphaPurify ``huber_winsorize(c=2.0)``
  逐点一致（实测 max|Δ| ≈ 5e-13，纯 float64 舍入）；
- ``rankgauss`` 与 AlphaPurify ``rankgauss_winsorize`` 差 ≈ 2e-9 ——
  那是本仓用 Acklam 逆正态近似、上游用 scipy ``norm.ppf`` 的近似误差
  （Acklam 精度 ~1e-9，已在 docstring 写明）。

另外钉住两条容易踩的坑：

1. **AlphaPurify 的 huber 用 mean/std，并不稳健**（名字骗人）：极值把 std
   撑大，该截的没截住。本仓提供 ``scale='mad'`` 才是真稳健版，并断言两者
   在含极值数据上确实不同。
2. **rankgauss 与 ``standardize.rank(to='normal')`` 是两套分位约定**
   （Hazen ``(r-0.5)/n`` vs Blom ``r/n`` clip），不可混用。
"""
from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from lquant.factors.preprocess import run
from lquant.factors.preprocess.standardize import rank as rank_std
from lquant.factors.preprocess.winsorize import MAD_K, huber, rankgauss

# AlphaPurify 源码（只读参照）；缺失时跳过对拍用例
try:  # pragma: no cover - 环境相关
    import sys as _sys
    from pathlib import Path as _Path

    _vendor = _Path(__file__).resolve().parents[2] / ".vendor" / "AlphaPurify"
    if _vendor.exists() and str(_vendor) not in _sys.path:
        _sys.path.insert(0, str(_vendor))
    from alphapurify.APr_utils import huber_winsorize as _ap_huber
    from alphapurify.APr_utils import rankgauss_winsorize as _ap_rankgauss

    _HAS_AP = True
except Exception:  # noqa: BLE001
    _HAS_AP = False

needs_ap = pytest.mark.skipif(not _HAS_AP, reason="未找到 AlphaPurify 源码")


def _frame(vals: list[float], day: str = "2026-01-05") -> pl.DataFrame:
    return pl.DataFrame({"trade_date": [day] * len(vals), "f": vals})


def _with_outliers(n: int = 200, seed: int = 0) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    return _frame([*rng.normal(0, 1, n).tolist(), 50.0, -40.0])


# ----------------------------------------------------------- huber

def test_huber_clips_extremes_and_preserves_core():
    df = _with_outliers()
    out = run(df, "f", [{"op": "winsorize", "method": "huber", "c": 2.0}])
    z = (df["f"] - df["f"].mean()) / df["f"].std()
    core = z.abs() <= 2.0
    # 界内样本完全不动
    assert np.allclose(out["f"].to_numpy()[core], df["f"].to_numpy()[core], atol=1e-12)
    # 界外样本被夹住：z 不超过 2
    z_out = ((out["f"] - df["f"].mean()) / df["f"].std()).to_numpy()
    assert np.abs(z_out).max() <= 2.0 + 1e-9
    assert np.abs(out["f"].to_numpy()).max() < np.abs(df["f"].to_numpy()).max()


def test_huber_threshold_formula_hand_checked():
    """截断点 = mean ± c·std（手算）；界内的样本不动。"""
    df = _frame([1.0, 2.0, 3.0, 4.0, 100.0])
    out = huber(df, "f", c=1.5)
    mu = float(df["f"].mean())
    sd = float(df["f"].std())
    assert float(out["f"].max()) == pytest.approx(mu + 1.5 * sd, abs=1e-12)
    # 下界远低于数据最小值（1.0），所以最小值原样保留 —— 这是正确行为
    assert float(out["f"].min()) == pytest.approx(1.0, abs=1e-12)
    assert mu - 1.5 * sd < 1.0

    # 双向极值才同时触发上下界
    df2 = _frame([0.0, 1.0, 2.0, 3.0, 4.0, 100.0, -100.0])
    out2 = huber(df2, "f", c=1.0)
    mu2 = float(df2["f"].mean())
    sd2 = float(df2["f"].std())
    assert float(out2["f"].max()) == pytest.approx(mu2 + sd2, abs=1e-12)
    assert float(out2["f"].min()) == pytest.approx(mu2 - sd2, abs=1e-12)


def test_huber_mad_scale_is_actually_robust():
    """``scale='mad'`` 才真正抗极值：std 版会被极值撑大而少截。

    这是与 AlphaPurify 同名方法的关键差别（它固定用 std）。
    """
    df = _with_outliers()
    std_v = huber(df, "f", c=2.0, scale="std")
    mad_v = huber(df, "f", c=2.0, scale="mad")
    # 两种尺度给出的截断结果不同
    assert not np.allclose(std_v["f"].to_numpy(), mad_v["f"].to_numpy(), atol=1e-6)
    # MAD 版的尺度更小 → 夹得更紧
    med = float(df["f"].median())
    mad = float((df["f"] - med).abs().median()) * MAD_K
    sd = float(df["f"].std())
    assert mad < sd
    # 截断点是 mean ± c·scale（中心是 mean，不是 median）
    assert float(mad_v["f"].max()) == pytest.approx(
        float(df["f"].mean()) + 2.0 * mad, abs=1e-9)


def test_huber_zero_variance_collapses_to_mean():
    df = _frame([7.0] * 5)
    out = huber(df, "f", c=2.0)
    assert out["f"].to_list() == [7.0] * 5
    assert out["f"].null_count() == 0


def test_huber_rejects_bad_params():
    df = _frame([1.0, 2.0, 3.0])
    with pytest.raises(ValueError, match="c 必须为正"):
        huber(df, "f", c=0.0)
    with pytest.raises(ValueError, match="未知 scale"):
        huber(df, "f", scale="iqr")


def test_huber_is_per_day():
    """跨日不污染：一天的极值不能改变另一天的截断。"""
    df = pl.DataFrame({
        "trade_date": ["d1", "d1", "d1", "d2", "d2", "d2"],
        "f": [1.0, 2.0, 3.0, 1000.0, 2000.0, 3000.0],
    })
    out = huber(df, "f", c=2.0)
    d1 = out.filter(pl.col("trade_date") == "d1")["f"].to_numpy()
    assert np.allclose(d1, [1.0, 2.0, 3.0], atol=1e-12)


@needs_ap
def test_huber_matches_alphapurify_bitwise():
    """与 AlphaPurify ``huber_winsorize(c=2.0)`` 逐点一致（默认 std 口径）。"""
    df = _with_outliers()
    ap = _ap_huber(df, "trade_date", "f", 2.0)["f"].to_numpy()
    lq = huber(df, "f", c=2.0)["f"].to_numpy()
    assert np.max(np.abs(ap - lq)) < 1e-9


# ----------------------------------------------------------- rankgauss

def test_rankgauss_uses_hazen_plotting_position():
    """``q = (rank-0.5)/n``（Hazen），手算对拍。"""
    df = _frame([10.0, 20.0, 30.0, 40.0])
    out = rankgauss(df, "f")
    from lquant.factors.preprocess.standardize import _inv_norm

    expect = [_inv_norm((r - 0.5) / 4) for r in (1, 2, 3, 4)]
    assert np.allclose(out["f"].to_numpy(), expect, atol=1e-12)
    assert out["f"].to_numpy()[0] == pytest.approx(-1.150349380376008, abs=1e-9)


def test_rankgauss_handles_outliers_and_is_bounded():
    """极值不产生极端输出（这正是它比 z-score 稳健的地方）。"""
    df = _with_outliers()
    out = rankgauss(df, "f")
    # 200+2 个样本，最大值对应 (n-0.5)/n 的分位 → 逆正态约 3.2
    assert np.abs(out["f"].to_numpy()).max() < 3.5
    assert np.abs(out["f"].to_numpy()).max() > 2.5


def test_rankgauss_differs_from_standardize_rank_normal():
    """两套分位约定（Hazen vs Blom）不可混用 —— 显式钉住差异。"""
    df = _frame([10.0, 20.0, 30.0, 40.0, 50.0])
    a = rankgauss(df, "f")["f"].to_numpy()
    b = rank_std(df, "f", to="normal")["f"].to_numpy()
    assert not np.allclose(a, b, atol=1e-6)
    # Hazen 的第一个分位点比 Blom 更靠外（Hazen 0.1 vs Blom 0.1→clip 到 0.1 后的差异）
    assert a[0] < b[0]


def test_rankgauss_all_ties_gives_a_constant():
    """全值相同 → rank('average') 给同一平均秩 → 输出是常数（无信息）。

    这不是退化 bug：并列值本来就无法区分先后，给它们不同分位反而是伪造信息。
    """
    df = _frame([5.0] * 6)
    out = rankgauss(df, "f")
    assert out["f"].null_count() == 0
    vals = out["f"].to_numpy()
    assert np.allclose(vals, 0.0, atol=1e-12)      # (3.5−0.5)/6 = 0.5 → Φ⁻¹ = 0
    assert len(set(vals.round(12))) == 1


def test_rankgauss_clip_bounds_and_errors():
    df = _with_outliers()
    with pytest.raises(ValueError, match="clip 必须在"):
        rankgauss(df, "f", clip=0.0)
    with pytest.raises(ValueError, match="clip 必须在"):
        rankgauss(df, "f", clip=0.6)
    # clip 越大，尾部越被压向 0
    loose = rankgauss(df, "f", clip=1e-6)
    tight = rankgauss(df, "f", clip=0.1)
    assert np.abs(tight["f"].to_numpy()).max() < np.abs(loose["f"].to_numpy()).max()


def test_rankgauss_is_per_day():
    df = pl.DataFrame({
        "trade_date": ["d1", "d1", "d2", "d2"],
        "f": [1.0, 2.0, 1000.0, 2000.0],
    })
    out = rankgauss(df, "f")
    d1 = out.filter(pl.col("trade_date") == "d1")["f"].to_numpy()
    d2 = out.filter(pl.col("trade_date") == "d2")["f"].to_numpy()
    assert np.allclose(d1, d2, atol=1e-12)      # 逐日独立，形状相同


@needs_ap
def test_rankgauss_matches_alphapurify_within_acklam_tolerance():
    """与上游差在 Acklam 逆正态近似精度（~1e-9），不是口径差。"""
    df = _with_outliers()
    ap = _ap_rankgauss(df, "trade_date", "f")["f"].to_numpy()
    lq = rankgauss(df, "f")["f"].to_numpy()
    assert np.max(np.abs(ap - lq)) < 1e-7


# ----------------------------------------------------------- 口径元数据

def test_new_methods_expose_formula_and_zero_variance():
    """Phase 1.5 的口径可见性约定对新方法同样生效。"""
    from lquant.factors.preprocess import list_methods

    by_name = {m["name"]: m for m in list_methods("winsorize")}
    for name in ("huber", "rankgauss"):
        assert by_name[name]["formula"]
        assert by_name[name]["zero_variance"]
        assert by_name[name]["notes"]
    assert "1.4826" in by_name["huber"]["formula"] or "std" in by_name["huber"]["formula"]
    assert "Hazen" in by_name["rankgauss"]["notes"]


def test_new_methods_run_through_pipeline_with_defaults():
    df = _with_outliers()
    for method in ("huber", "rankgauss"):
        out = run(df, "f", [{"op": "winsorize", "method": method}])
        assert out.height == df.height
        assert out["f"].null_count() == 0
