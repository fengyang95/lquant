"""预处理口径自省：MAD 的 1.4826 修正与零方差语义必须**可被机器读到**。

背景（AlphaPurify 交叉验证暴露的真实口径差）：
- lquant `mad(n)` 的截断点是 `med ± n × 1.4826 × MAD`；AlphaPurify
  `mad_winsorize(n)` 是 `med ± n × MAD`。同名不同义，默认对默认实测
  `max|Δ| ≈ 3.04`。
- lquant zscore 在零方差截面返回 **0**（`_safe_std` 兜底 1.0），AlphaPurify 返回
  **null**。

这些语义原先只存在于源码里，`describe()` 只给 `params={'n': 5.0}`。本测试锁死
「口径必须经注册表/API 透出」，否则后续交叉验证会再次踩同一个坑。
"""
from __future__ import annotations

import polars as pl
import pytest

from lquant.factors.preprocess import (
    MAD_K,
    describe,
    from_alphapurify_n,
    list_methods,
    run,
    to_alphapurify_n,
)


def _method(stage: str, name: str) -> dict:
    for m in list_methods(stage):
        if m["name"] == name:
            return m
    raise AssertionError(f"{stage}.{name} 未注册")


# ----------------------------------------------------------- MAD 口径

def test_mad_k_is_the_normal_consistency_correction():
    """1.4826 = 1/Φ⁻¹(0.75)，是 MAD→σ 的正态一致性修正。"""
    assert abs(MAD_K - 1.4826) < 1e-12


def test_mad_describe_states_the_14826_factor():
    """describe()/list_methods() 必须写明 n 要乘 1.4826，而不只是 params={'n':5.0}。"""
    m = _method("winsorize", "mad")
    assert m["params"] == {"n": 5.0}
    formula = m["formula"]
    assert "1.4826" in formula
    assert "MAD" in formula
    notes = m["notes"]
    assert "等效标准差倍数" in notes          # n 的语义
    assert "AlphaPurify" in notes             # 与上游的关系
    assert "同名不同义" in notes


def test_mad_describe_declares_zero_variance_semantics():
    m = _method("winsorize", "mad")
    assert "zero_variance" in m
    assert "中位数" in m["zero_variance"]     # 塌缩为中位数


def test_alphapurify_n_conversion_matches_documented_examples():
    """文档中的两个换算例子必须由代码给出，不能只是注释。"""
    assert to_alphapurify_n(5.0) == pytest.approx(7.413, abs=1e-3)
    assert from_alphapurify_n(3.0) == pytest.approx(2.0235, abs=1e-4)
    # 往返一致
    assert from_alphapurify_n(to_alphapurify_n(4.2)) == pytest.approx(4.2)


def test_aligned_convention_yields_identical_clip_bounds():
    """口径对齐后（n_lq = n_ap/1.4826）截断点必须逐点相同 —— 证明差的是约定不是内核。"""
    rng = pl.DataFrame({
        "trade_date": ["d"] * 200,
        "v": [-1e6] + [float(i) for i in range(198)] + [1e6],   # 双侧极值
    })
    n_ap = 3.0
    lq = run(rng, "v", [{"op": "winsorize", "method": "mad", "n": from_alphapurify_n(n_ap)}],
             by="trade_date")
    # 手算 AlphaPurify 口径：med ± n_ap × MAD
    med = rng["v"].median()
    mad = (rng["v"] - med).abs().median()
    lo, hi = med - n_ap * mad, med + n_ap * mad
    assert lq["v"].min() == pytest.approx(lo)
    assert lq["v"].max() == pytest.approx(hi)
    # 默认默认（n=5 vs n=3）确实不同 —— 口径差是真实的
    lq_default = run(rng, "v", [{"op": "winsorize", "method": "mad"}], by="trade_date")
    assert lq_default["v"].max() != pytest.approx(hi)


# ----------------------------------------------------------- 零方差语义

def test_zscore_zero_variance_returns_zero_not_null():
    """全截面同值 → σ=0 → 结果恒为 0（不是 null，也不是 1）。"""
    df = pl.DataFrame({"trade_date": ["d"] * 5, "f": [3.0] * 5})
    out = run(df, "f", [{"op": "standardize", "method": "zscore"}], by="trade_date")
    assert out["f"].to_list() == [0.0] * 5
    assert out["f"].null_count() == 0


def test_zscore_describe_states_zero_variance_and_ap_difference():
    m = _method("standardize", "zscore")
    zv = m["zero_variance"]
    assert "0" in zv
    assert "null" in zv          # 明确点出与 AlphaPurify 的差异
    assert "AlphaPurify" in zv


def test_minmax_zero_variance_returns_lo():
    df = pl.DataFrame({"trade_date": ["d"] * 4, "f": [2.0] * 4})
    out = run(df, "f", [{"op": "standardize", "method": "minmax", "lo": 0.0, "hi": 1.0}],
              by="trade_date")
    assert out["f"].to_list() == [0.0] * 4
    assert "zero_variance" in _method("standardize", "minmax")


def test_rank_zero_variance_declares_no_information():
    """Rank 对全值相同仍给出确定序，但不是 null —— 语义必须写明。"""
    m = _method("standardize", "rank")
    assert "zero_variance" in m
    assert "无信号" in m["zero_variance"]
    df = pl.DataFrame({"trade_date": ["d"] * 4, "f": [1.0] * 4})
    out = run(df, "f", [{"op": "standardize", "method": "rank"}], by="trade_date")
    assert out["f"].null_count() == 0


def test_mad_zero_variance_collapses_to_median():
    """MAD=0 且 σ=0 时 scale=0 → 整列塌缩为中位数，不报错、不产生 null。"""
    df = pl.DataFrame({"trade_date": ["d"] * 6, "f": [7.0] * 6})
    out = run(df, "f", [{"op": "winsorize", "method": "mad"}], by="trade_date")
    assert out["f"].to_list() == [7.0] * 6
    assert out["f"].null_count() == 0


# ----------------------------------------------------------- 全体方法口径可见

def test_every_method_exposes_a_formula():
    """所有 winsorize / standardize 方法都要有 formula，否则前端无法解释口径。"""
    for m in describe():
        if m["stage"] in ("winsorize", "standardize"):
            assert m.get("formula"), f"{m['stage']}.{m['name']} 缺 formula"


# ----------------------------------------------------------- API 透出

def test_api_surfaces_mad_convention_and_method_metadata():
    from lquant.server.api.factors import list_preprocess_methods

    payload = list_preprocess_methods(stage=None)
    conv = payload["mad_convention"]
    assert conv["scale_factor"] == pytest.approx(1.4826)
    assert "1.4826" in conv["formula"]
    assert conv["n_semantics"] == "equivalent_sigma_multiple"
    mad = next(m for m in payload["methods"] if m["name"] == "mad")
    assert mad["formula"] == conv["formula"]
