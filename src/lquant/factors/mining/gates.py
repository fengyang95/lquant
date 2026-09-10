"""Gate pipeline G0-G3 (spec section 3.2).

Every rejection returns a structured reason code + actionable hint --
scores alone turn human-in-the-loop mining into random search.
"""
from __future__ import annotations

from dataclasses import dataclass

from lquant.core.errors import FactorError, LookaheadError
from lquant.factors.dsl.analyzer import check
from lquant.factors.dsl.parser import parse

REASON_CODES = {
    "STATIC_FAIL": "parse/静态校验失败",
    "COMPUTE_FAIL": "计算失败",
    "LOW_IC": "中性化后 IC 不足",
    "REDUNDANT": "与已入库因子相关性过高",
    "SIZE_PROXY": "中性化后 IC 衰减过大（风格代理）",
    "LOW_TSTAT": "校正后 t 不足",
    "OOS_FAIL": "样本外复核失败",
}


@dataclass(frozen=True)
class GateResult:
    passed: bool
    stage: str
    reason_code: str | None = None
    hint: str = ""
    ic: float | None = None        # G1 通过时回传，供 fitness/GP feedback 用
    ic_raw: float | None = None


def g0_static(expr, allowed_fields=None):
    """G0: parse + 字段白名单 + 未来函数 + 算子合法性（微秒级）。"""
    try:
        ast = parse(expr, "candidate")
        check(ast, allowed_fields=allowed_fields)
        return GateResult(True, "G0")
    except (SyntaxError, FactorError, LookaheadError, TypeError) as e:
        return GateResult(False, "G0", "STATIC_FAIL", str(e))


def g1_fast_screen(train, expr, ret_col, engine, covs=None, min_abs_ic=0.02):
    """G1 快筛：训练段 + 中性化 IC（方案 3.2/5.4：快筛一律用中性化后 IC）。

    数据范围硬约束：只在传入的 train 子集上计算（engine 可能持有全量 panel，
    直接 engine.compute 会把 val/test 泄漏进快筛 —— 静默破坏 70/15/15）。
    """
    from lquant.factors.analysis import compute_factor_col
    from lquant.factors.evaluate import forward_return
    from lquant.factors.evaluate.ic import ic_series
    from lquant.factors.preprocess.pipeline import run as pipeline_run

    try:
        d = compute_factor_col(train, expr, "f")
        if covs:
            d = pipeline_run(d, "f", [
                {"op": "winsorize", "method": "mad", "n": 5},
                {"op": "standardize", "method": "zscore"},
                {"op": "neutralize", "method": "ols", "factors": covs},
            ])
        d = d.drop_nulls(["f"])
        if "fwd_ret_1" not in d.columns:
            d = forward_return(d, "close", periods=[1])
            ret_col = "fwd_ret_1"
        s = ic_series(d, "f", ret_col)
        if not len(s):
            return GateResult(False, "G1", "COMPUTE_FAIL", "IC 序列为空")
        ic = float(s["ic"].mean())
        rank = float(s["rank_ic"].mean())
        raw_ic = ic
        if covs:
            s_raw = ic_series(compute_factor_col(train, expr, "f").drop_nulls(["f"]),
                              "f", ret_col)
            raw_ic = float(s_raw["ic"].mean())
        decay = 1 - abs(ic) / max(abs(raw_ic), 1e-12)
        size_proxy = covs is not None and decay > 0.8
        if size_proxy:
            return GateResult(False, "G1", "SIZE_PROXY",
                              f"中性化后 IC 衰减 {decay:.0%} —— 风格暴露代理，别再往动量/市值撞")
        if abs(ic) < min_abs_ic:
            return GateResult(False, "G1", "LOW_IC",
                              f"中性化 IC={ic:.4f} < {min_abs_ic}；"
                              f"建议换字段族或加截面变换（Rank/ZScore）")
        return GateResult(True, "G1", hint=f"IC={ic:.4f} rank={rank:.4f} decay={decay:.0%}",
                          ic=ic, ic_raw=raw_ic)
    except Exception as e:  # noqa: BLE001
        return GateResult(False, "G1", "COMPUTE_FAIL", f"{type(e).__name__}: {e}")


def g2_dedup(train_values, expr, survivors, engine, max_corr=0.7):
    """G2 去重：与幸存者相关性 |rho| >= max_corr 淘汰（方案 3.2: |rho|<0.7）。"""

    from lquant.factors.analysis import correlation

    if not survivors:
        return GateResult(True, "G2", hint="首个幸存者")
    exprs = [s["expr"] for s in survivors] + [expr]
    try:
        res = correlation(train_values, exprs, threshold=max_corr)
        pairs = [p for p in res.get("redundant_pairs", [])
                 if expr.replace(".", "_") in (p["a"], p["b"])]
    except Exception:  # noqa: BLE001
        pairs = []
    if pairs:
        p0 = pairs[0]
        other = p0["b"] if p0["a"] == expr.replace(".", "_") else p0["a"]
        return GateResult(False, "G2", "REDUNDANT",
                          f"与 {other[:40]} 相关 {p0['corr']:.2f}，建议换字段族或持有期")
    return GateResult(True, "G2", hint="与现有幸存者相关性可接受")
