"""再平衡纪律：换手约束（optimizer）+ no-trade band（weighting）。

cost_matrix 已证明高换手是收益杀手，本文件验证权重层的两道刹车：
优化器里的 max_turnover（L1 约束 + 事后核验）与输出层的 band 吸收。
"""

from __future__ import annotations

import numpy as np
import pytest

from lquant.portfolio.optimizer import OptimizerError, enhanced_indexing_weight


def _returns(n_days: int = 260, n_sym: int = 10, seed: int = 5):
    rng = np.random.default_rng(seed)
    M = rng.normal(0.0005, 0.015, size=(n_days, n_sym))
    # 给前两只票稳定正漂移：优化器有明确理由集中
    M[:, 0] += 0.002
    M[:, 1] += 0.0015
    return M


def _syms(n: int = 10) -> list[str]:
    return [f"S{i:02d}" for i in range(n)]


# ---------------- optimizer：换手约束 ----------------


def test_max_turnover_requires_prev_weights():
    with pytest.raises(OptimizerError, match="prev_weights"):
        enhanced_indexing_weight(
            _returns(),
            _syms(),
            scores={f"S{i:02d}": -i for i in range(10)},
            max_turnover=0.05,
            prev_weights=None,
        )


def test_max_turnover_must_be_positive():
    with pytest.raises(OptimizerError, match="max_turnover"):
        enhanced_indexing_weight(
            _returns(),
            _syms(),
            scores={f"S{i:02d}": -i for i in range(10)},
            max_turnover=0.0,
            prev_weights={f"S{i:02d}": 0.1 for i in range(10)},
        )


def test_max_turnover_caps_l1_distance_from_prev():
    n = 10
    syms = _syms(n)
    scores = {s: -i for i, s in enumerate(syms)}  # α 集中在 S00
    prev = {s: 1.0 / n for s in syms}  # 等权上期

    unconstrained = enhanced_indexing_weight(_returns(), syms, scores=scores, max_weight=0.5)
    capped = enhanced_indexing_weight(
        _returns(), syms, scores=scores, max_weight=0.5, max_turnover=0.05, prev_weights=prev
    )

    tu = capped.diagnostics["_turnover_from_prev"]
    assert tu is not None and tu <= 0.05 + 1e-6  # 约束真的生效
    # 有约束时离上期更近（刹车生效，而不是换个方向装样子）
    w_unc = np.array([unconstrained[s] for s in syms])
    w_cap = np.array([capped[s] for s in syms])
    b = np.full(n, 1.0 / n)
    assert np.abs(w_cap - b).sum() <= np.abs(w_unc - b).sum() + 1e-9
    # 无约束路径的诊断字段也在
    assert unconstrained.diagnostics["_turnover_from_prev"] is not None


def test_turnover_constraint_does_not_bind_when_loose():
    n = 10
    syms = _syms(n)
    scores = {s: -i for i, s in enumerate(syms)}
    prev = {s: 1.0 / n for s in syms}
    loose = enhanced_indexing_weight(
        _returns(), syms, scores=scores, max_weight=0.5, max_turnover=2.0, prev_weights=prev
    )
    assert loose.diagnostics["_fallback"] is False
    assert loose.diagnostics["_turnover_from_prev"] > 0.05  # 宽约束不绑


def test_prev_weights_accepts_plain_dict_order_insensitive():
    n = 10
    syms = _syms(n)
    prev = {syms[i]: 0.2 if i < 2 else 0.075 for i in range(n)}  # 和=1
    res = enhanced_indexing_weight(
        _returns(),
        syms,
        scores={s: -i for i, s in enumerate(syms)},
        max_weight=0.5,
        max_turnover=0.1,
        prev_weights=prev,
    )
    assert res.diagnostics["_fallback"] is False


# ---------------- weighting：no-trade band ----------------


def test_band_absorbs_small_deviations():
    from lquant.portfolio.weighting import apply_no_trade_band

    new = {"A": 0.105, "B": 0.40, "C": 0.495}
    prev = {"A": 0.10, "B": 0.35, "C": 0.55}
    out, changed = apply_no_trade_band(new, prev, band=0.02)
    assert out["A"] == 0.10  # 0.005 < band → 不动
    assert out["B"] == 0.40  # 0.05 >= band → 调
    assert out["C"] == 0.495  # 0.055 >= band → 调
    assert changed == 2


def test_band_keeps_tiny_position_instead_of_silently_selling():
    from lquant.portfolio.weighting import apply_no_trade_band

    # prev 独有的小仓位：新权重不含它（目标 0），但 0.01 < band → 不卖
    out, _ = apply_no_trade_band({"A": 0.5, "B": 0.5}, {"A": 0.5, "B": 0.49, "C": 0.01}, band=0.02)
    assert out["C"] == 0.01


def test_band_sells_once_threshold_reached():
    from lquant.portfolio.weighting import apply_no_trade_band

    out, changed = apply_no_trade_band({"A": 1.0}, {"A": 0.9, "B": 0.1}, band=0.05)
    assert out["B"] == 0.0  # 0.1 >= band → 清仓
    assert out["A"] == 1.0
    assert changed == 2  # A 调到位 + B 清仓


def test_band_requires_positive():
    from lquant.portfolio.weighting import apply_no_trade_band

    with pytest.raises(ValueError):
        apply_no_trade_band({"A": 1.0}, {"A": 0.5}, band=0.0)
    with pytest.raises(ValueError):
        apply_no_trade_band({"A": 1.0}, {"A": 0.5}, band=-0.01)


def test_weights_entry_point_band_pairing():
    from lquant.portfolio.weighting import weights

    M = _returns()
    syms = _syms()
    prev = {s: 1.0 / len(syms) for s in syms}
    w = weights(M, "equal", syms, band=0.05, prev_weights=prev)
    assert abs(sum(w.values()) - 1.0) < 1e-9  # band 内全吸收 = 不动

    # 成对校验：只给一个是装样子
    with pytest.raises(ValueError, match="成对"):
        weights(M, "equal", syms, band=0.05)
    with pytest.raises(ValueError, match="成对"):
        weights(M, "equal", syms, prev_weights=prev)


def test_weights_band_actually_changes_target_weights():
    from lquant.portfolio.weighting import weights

    M = _returns()
    syms = _syms()
    n = len(syms)
    scores = {s: float(-i) for i, s in enumerate(syms)}
    prev = {s: 1.0 / n for s in syms}
    banded = weights(
        M, "enhanced_indexing", syms, scores=scores, max_weight=0.5, band=0.03, prev_weights=prev
    )
    # band 不变量：每个标的目标要么停在 prev（不动），要么偏离 >= band
    # （动就动到位）—— 不存在「动了一点点」的中间态
    for s in syms:
        d = abs(banded[s] - prev[s])
        assert d == 0 or d >= 0.03 - 1e-12, f"{s}: d={d}"
