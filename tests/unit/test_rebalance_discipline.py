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


def _true_l1(res, prevd: dict[str, float], syms: list[str]) -> float:
    """独立口径的真实成交 L1（含清池外腿与现金腿），用于核验诊断字段。

    = Σ|w - w_prev| + Σ_池外 w_prev + |Σw_prev - Σw|
    """
    w = np.array([res[s] for s in syms])
    p = np.array([prevd.get(s, 0.0) for s in syms])
    off = sum(v for k, v in prevd.items() if k not in syms)
    return float(np.abs(w - p).sum() + off + abs(sum(prevd.values()) - w.sum()))


def test_turnover_reference_keeps_raw_prev_scale_with_cash_buffer():
    """上期含现金缓冲（Σw_prev < 1）时，换手上限必须计入补现金腿的真实成交。

    修复前 ``w0 = _as_weights(prev_weights, syms)`` 会把上期归一化：6 只各
    0.1（和 0.6、40% 现金）被当成满仓组合，补 40% 现金腿的买入完全不进
    ``Σ|w - w_prev|`` —— 声明 0.05 的上限，真实成交 0.8 也照样"满足"。
    """
    n = 6
    syms = _syms(n)
    prev = {s: 0.1 for s in syms}  # Σ = 0.6 → 40% 现金
    r = enhanced_indexing_weight(
        _returns(n_sym=n), syms,
        scores={s: -i for i, s in enumerate(syms)},
        max_weight=0.5, max_turnover=0.05, prev_weights=prev,
    )
    reported = r.diagnostics["_turnover_from_prev"]
    # 诊断必须等于独立口径的真实成交 L1（不是「到归一化组合」的距离）
    assert reported == pytest.approx(_true_l1(r, prev, syms), abs=1e-9)
    # 补现金腿本身就 > 0.05，不可能有满足上限的满仓解 → 必须显式 fallback
    # （修复前它反而"成功"返回归一化后的上期组合，并谎报换手 ~0.05）
    assert r.diagnostics["_fallback"] is True


def test_turnover_counts_out_of_universe_liquidation():
    """上期持有本期符号表之外的票：清仓腿必须计入真实成交 L1。

    修复前 ``prev={"S00".."S04": 0.1, "Z": 0.5}`` 里 Z 被直接丢弃，诊断为
    0.049974；真实需成交 L1 = 0.5（场内调仓）+ 0.5（清 Z + 现金腿）= 1.0。
    """
    n = 6
    syms = _syms(n)
    prev = {s: 0.1 for s in syms[:5]}
    prev["Z"] = 0.5  # 池外持仓
    r = enhanced_indexing_weight(
        _returns(n_sym=n), syms,
        scores={s: -i for i, s in enumerate(syms)},
        max_weight=0.5, max_turnover=0.05, prev_weights=prev,
    )
    reported = r.diagnostics["_turnover_from_prev"]
    assert reported == pytest.approx(_true_l1(r, prev, syms), abs=1e-9)
    # 修复前的 0.049974 必然 < 0.5；清 Z 一条腿就不止 0.5
    assert reported > 0.5


def test_leveraged_prev_weights_raise_instead_of_silent_normalize():
    """Σw_prev > 1（含杠杆）时无法构建现金腿 → 显式报错，不静默归一化。"""
    n = 6
    syms = _syms(n)
    prev = {s: 0.3 for s in syms}  # Σ = 1.8
    with pytest.raises(OptimizerError, match="杠杆"):
        enhanced_indexing_weight(
            _returns(n_sym=n), syms,
            scores={s: -i for i, s in enumerate(syms)},
            max_weight=0.5, max_turnover=0.05, prev_weights=prev,
        )


def test_per_element_bound_conflict_falls_back_not_bare_valueerror():
    """``b_i > max_weight + max_active`` 时 SLSQP 抛裸 ``ValueError``（非
    ``LQuantError``，调用方 ``except LQuantError`` 抓不住）—— 必须预检 + fallback。

    和式预检（``Σhi ≥ 1 ≥ Σlo``）对这种逐元素矛盾恒放行，所以只加和式判断
    不够；本用例即修复前实测命中裸 ValueError 的构型。
    """
    from lquant.core.errors import LQuantError

    n = 5
    syms = _syms(n)
    b = {syms[0]: 0.6, **{s: 0.1 for s in syms[1:]}}  # b0 = 0.6 > 0.2 + 0.05
    try:
        r = enhanced_indexing_weight(
            _returns(n_sym=n), syms,
            scores={s: -i for i, s in enumerate(syms)},
            benchmark_weights=b, max_weight=0.2, max_active=0.05,
        )
    except LQuantError:
        pytest.fail("应走 fallback 契约，而不是抛 LQuantError")
    except ValueError as e:  # 修复前命中这里
        pytest.fail(f"裸 ValueError 绕过 fallback 契约：{e}")
    assert r.diagnostics["_fallback"] is True
    assert "边界" in r.diagnostics["_fallback_reason"]
    assert isinstance(r.diagnostics["_constraint_violations"], list)


def test_fallback_reports_violations_instead_of_claiming_feasible():
    """``_fallback`` 退回的上期持仓可能超出本次声明的 TE/主动上限 ——
    必须写进 ``_constraint_violations``，不能谎称"已知可行"。

    修复前 w0 的 TE 实测可达 ``te_target`` 的 19.4 倍，但诊断里没有任何
    「已违约」的信息，理由却是「退回一个已知可行的权重组合」。
    """
    n = 10
    syms = _syms(n)
    b = {s: 0.1 for s in syms}
    prev = {syms[0]: 0.6, syms[1]: 0.4}  # 集中持仓，必然突破 0.1% 的 TE
    r = enhanced_indexing_weight(
        _returns(), syms, scores={s: -i for i, s in enumerate(syms)},
        benchmark_weights=b, max_weight=1.0, te_target=0.001,
        max_turnover=1e-6, prev_weights=prev,
    )
    assert r.diagnostics["_fallback"] is True
    assert r.diagnostics["_fallback_candidate"] in ("prev", "benchmark")
    assert r.diagnostics["_constraint_violations"], "fallback 必须报告违约"
    assert any("tracking_error" in v for v in r.diagnostics["_constraint_violations"])
    assert "违反" in r.diagnostics["_fallback_reason"]


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


def test_band_never_produces_implicit_leverage():
    """band 吸收后权重和必须 ≤ 1（差额才是现金缓冲）。

    修复前：被吸收标的停在 prev、未吸收取 w_new，直接拼接会把「prev 比
    new 多出来的部分」吞进总和。实测 ``new={A:.36,B:.44,C:.20}``、
    ``prev={A:.40,B:.30,C:.30}``、band=0.06 → 总和 1.04（2 万组随机搜索
    最大 1.5289），即隐含杠杆，与 README「权重和 < 1 即现金缓冲」的单边
    说法不符。超额必须从本次可动的标的里显式再分配掉。
    """
    from lquant.portfolio.weighting import apply_no_trade_band

    out, _ = apply_no_trade_band(
        {"A": 0.36, "B": 0.44, "C": 0.20},
        {"A": 0.40, "B": 0.30, "C": 0.30},
        band=0.06,
    )
    assert sum(out.values()) <= 1.0 + 1e-12
    assert out["A"] == pytest.approx(0.40)  # 被吸收（|0.36-0.40| < 0.06）严格停在 prev
    assert out["B"] < 0.44 and out["C"] < 0.20  # 超额从可动标的里扣


def test_band_random_search_never_exceeds_one():
    """随机构型下 band 输出总和恒 ≤ 1（修复前最大 1.5289）。"""
    from lquant.portfolio.weighting import apply_no_trade_band

    rng = np.random.default_rng(7)
    worst = 0.0
    for _ in range(3000):
        n = int(rng.integers(2, 8))
        nd = {f"S{i}": float(v) for i, v in enumerate(rng.dirichlet(np.ones(n)))}
        pd = {f"S{i}": float(v) for i, v in enumerate(rng.dirichlet(np.ones(n)))}
        out, _ = apply_no_trade_band(nd, pd, band=float(rng.uniform(0.01, 0.2)))
        worst = max(worst, sum(out.values()))
    assert worst <= 1.0 + 1e-9, f"band 产生了隐含杠杆：max sum={worst}"


def test_band_plus_method_owned_prev_weights_both_brakes_work():
    """band 与方法自己的 ``prev_weights`` 可以同时给（README 同节推荐）。

    修复前 ``weights()`` 在 band 模式下把 ``prev_weights`` pop 掉，方法层
    拿不到换手参照点，``max_turnover`` 直接抛「必须配 prev_weights」——
    两条刹车没法配着用。
    """
    from lquant.portfolio.weighting import weights

    syms = _syms()
    scores = {s: float(-i) for i, s in enumerate(syms)}
    prev = {s: 0.1 for s in syms}
    w = weights(
        _returns(), "enhanced_indexing", syms, scores=scores, max_weight=0.5,
        max_turnover=0.05, band=0.03, prev_weights=prev,
    )
    assert abs(sum(w.values())) <= 1.0 + 1e-9  # 无隐含杠杆
    # band 不变量：要么停在 prev（不动），要么偏离 >= band
    for s in syms:
        d = abs(w[s] - prev[s])
        assert d == pytest.approx(0.0, abs=1e-12) or d >= 0.03 - 1e-9, f"{s}: d={d}"


def test_weight_report_survives_riskmodel_error():
    """``weight_report`` 必须抓 ``RiskModelError``（与 ``OptimizerError`` 同族）。

    修复前 ``except (OptimizerError, ValueError)`` 漏了它：``cov_method``
    拼错或 T 太小时整张报告崩掉，而不是记一行 note。
    """
    from lquant.portfolio.weighting import weight_report

    df = weight_report(
        _returns(n_days=5, n_sym=5), methods=["risk_parity"], cov_method="bogus"
    )
    assert len(df) == 1
    assert df["note"][0] and "bogus" in df["note"][0]


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


def test_fallback_holds_prev_weights_when_turnover_capped():
    """不可行解 + 换手上限 → 退回**上期持仓**（换手 0），而不是退回基准。

    修复前 ``_fallback`` 无条件退回基准 b、并把 ``_turnover_from_prev`` 置 None：
    声明的换手上限恰好在自己生效的路径上被绕过（实测换手可达上限的万倍），
    而调用方只看到 ``_fallback=True`` 就以为「退到安全解」了。
    """
    n = 10
    syms = _syms(n)
    scores = {s: -i for i, s in enumerate(syms)}
    prev = {syms[0]: 1.0}  # 上期单票满仓；1e-4 的换手预算下无可行解
    r = enhanced_indexing_weight(
        _returns(), syms, scores=scores, max_weight=1.0,
        max_turnover=1e-4, prev_weights=prev,
    )
    assert r.diagnostics["_fallback"] is True
    # 退回上期持仓：换手恒 0，且诊断字段如实给出（不谎报 TE=0）
    assert r[syms[0]] == pytest.approx(1.0)
    assert r.diagnostics["_turnover_from_prev"] == pytest.approx(0.0)
    w = np.array([r[s] for s in syms])
    w0 = np.array([prev.get(s, 0.0) for s in syms])
    assert np.abs(w - w0).sum() == pytest.approx(0.0)
    assert "保持上期持仓" in r.diagnostics["_fallback_reason"]
    assert r.diagnostics["_tracking_error"] > 0  # w0 vs 基准的真实 TE，不是 0


def test_fallback_without_turnover_cap_still_returns_benchmark():
    """没给换手上限时维持原语义：退回基准（主动权重 0、TE 0、turnover None）。"""
    syms = _syms()
    b = {s: 0.1 for s in syms}
    r = enhanced_indexing_weight(
        _returns(), syms, scores={s: -i for i, s in enumerate(syms)},
        max_weight=0.02,  # 上界和 < 1 → 权重边界与 Σw=1 矛盾 → fallback
        benchmark_weights=b,
    )
    assert r.diagnostics["_fallback"] is True
    assert r.diagnostics["_turnover_from_prev"] is None
    assert r.diagnostics["_tracking_error"] == pytest.approx(0.0)
    assert all(r[s] == pytest.approx(0.1) for s in syms)


def test_weights_entry_point_forwards_prev_weights_to_optimizer():
    """统一入口必须能把 ``prev_weights`` 透传给优化器（它是换手参照点）。

    修复前 ``weights()`` 无条件 pop 掉 prev_weights 并做「band 成对」校验，
    于是 ``weights(..., "enhanced_indexing", max_turnover=…, prev_weights=…)``
    必抛 ValueError —— 注册表入口比直调方法更窄，而这两参数本来就配着用。
    """
    from lquant.portfolio.weighting import weights

    syms = _syms()
    scores = {s: float(-i) for i, s in enumerate(syms)}
    prev = {s: 0.1 for s in syms}
    w = weights(
        _returns(), "enhanced_indexing", syms, scores=scores,
        max_weight=0.5, max_turnover=0.05, prev_weights=prev,
    )
    assert abs(sum(w.values()) - 1.0) < 1e-9
    direct = enhanced_indexing_weight(
        _returns(), syms, scores=scores, max_weight=0.5,
        max_turnover=0.05, prev_weights=prev,
    )
    # 透传路径与直调路径同解（证明 prev_weights 真的到了优化器，而不是被吞掉）
    for s in syms:
        assert w[s] == pytest.approx(direct[s])


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
