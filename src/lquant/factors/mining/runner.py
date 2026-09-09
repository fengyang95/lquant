"""挖掘会话 runner：70/15/15 切分 + 门禁流水线 + 幸存者台账 + 记账。

搜索全程只用 train(70%)，val(15%) 只在 G3 复核解锁一次，test(15%) 不碰 ——
多次试验校正门槛随 n_trials 上升，Agent 亲眼看着自己的显著标准水涨船高。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import polars as pl

from lquant.factors.evaluate.ic import _t_stat as tstat, ic_series
from lquant.factors.mining.fitness import corrected_threshold
from lquant.factors.mining.gates import GateResult, g0_static, g1_fast_screen, g2_dedup


@dataclass
class Candidate:
    expr: str
    origin: str = "random"      # random / gp / llm / manual
    gate: GateResult | None = None
    ic_neutral: float = float("nan")
    ic_raw: float = float("nan")
    t_stat: float = float("nan")
    fitness: float = float("nan")


@dataclass
class SessionResult:
    agent: str
    n_evaluated: int = 0
    n_static_fail: int = 0
    n_low_ic: int = 0
    n_redundant: int = 0
    n_size_proxy: int = 0
    n_survivors: int = 0
    corrections: list[dict] = field(default_factory=list)


def split_dates(dates: list, train=0.7, val=0.15) -> tuple[list, list, list]:
    """70/15/15 时间切分：搜索用 train，val 只在 G3 解锁一次，test 不碰。"""
    ds = sorted(set(dates))
    n = len(ds)
    i1 = int(n * train)
    i2 = int(n * (train + val))
    return ds[:i1], ds[i1:i2], ds[i2:]


def run_session(engine, panel, generator, *, agent="builtin", n_candidates=100,
                cov_names=None, cov_build=None, survivors=None, ret_col="fwd_ret_1") -> SessionResult:
    """跑一轮挖掘会话：G0 -> G1(train) -> G2(去重) -> G3(val 解锁一次)。

    generator: 可调用对象，产出候选表达式字符串。能吐字符串就能接。
    """
    from lquant.factors.evaluate import forward_return


    survivors = list(survivors or [])
    dates = panel["trade_date"].unique().to_list()
    train_d, val_d, _test = split_dates(dates)
    train = panel.filter(pl.col("trade_date").is_in(train_d))
    val = panel.filter(pl.col("trade_date").is_in(val_d))
    train = forward_return(train.sort(["symbol", "trade_date"]), "close", periods=[1])
    train = train.drop_nulls(["fwd_ret_1"])
    res = SessionResult(agent=agent)
    n_static = n_ic = n_red = n_proxy = 0
    for _ in range(n_candidates):
        expr = generator()
        res.n_evaluated += 1
        g0 = g0_static(expr, allowed_fields=set(panel.columns))
        if not g0.passed:
            n_static += 1
            res.corrections.append({"expr": expr, "stage": "G0", "reason": g0.reason_code, "hint": g0.hint})
            continue
        g1 = g1_fast_screen(train, expr, ret_col, engine, covs=cov_build)
        if not g1.passed:
            if g1.reason_code == "SIZE_PROXY":
                n_proxy += 1
            else:
                n_ic += 1
            res.corrections.append({"expr": expr, "stage": "G1", "reason": g1.reason_code, "hint": g1.hint})
            continue
        g2 = g2_dedup(train, expr, survivors, engine)
        if not g2.passed:
            n_red += 1
            res.corrections.append({"expr": expr, "stage": "G2", "reason": g2.reason_code, "hint": g2.hint})
            continue
        # ---- G3: val 解锁一次 ----
        val_x = forward_return(val.sort(["symbol", "trade_date"]), "close", periods=[1])
        val_x = val_x.drop_nulls(["fwd_ret_1"])
        try:
            dv = engine.compute(expr, "f")
            val_j = dv.join(val_x, on=["symbol", "trade_date"], how="inner",
                            suffix="_r")
            s = ic_series(val_j.drop_nulls(["f"]), "f", ret_col)
            if not len(s):
                raise ValueError("val IC 序列为空")
            ic_val = float(s["ic"].mean())
            t_val = tstat(float(s["ic"].mean()), float(s["ic"].std()), len(s))
            thr = corrected_threshold(res.n_evaluated)
            if abs(t_val) < thr:
                res.corrections.append({"expr": expr, "stage": "G3", "reason": "LOW_TSTAT",
                                        "hint": f"|t|={abs(t_val):.2f} < {thr:.2f} (n={res.n_evaluated})"})
                continue
        except Exception as e:  # noqa: BLE001
            res.corrections.append({"expr": expr, "stage": "G3", "reason": "OOS_FAIL", "hint": str(e)})
            continue
        survivors.append({"expr": expr, "ic_neutral": round(ic_val, 4),
                          "t_stat": round(t_val, 2), "origin": agent})
    res.n_survivors = len(survivors)
    res.n_static_fail = n_static
    res.n_low_ic = n_ic
    res.n_redundant = n_red
    res.n_size_proxy = n_proxy
    return res, survivors
