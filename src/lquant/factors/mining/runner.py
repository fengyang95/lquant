"""挖掘会话 runner：70/15/15 切分 + 门禁流水线 + 幸存者台账 + 记账。

搜索全程只用 train(70%)，val(15%) 只在 G3 复核解锁一次，test(15%) 不碰 ——
多次试验校正门槛随 n_trials 上升，Agent 亲眼看着自己的显著标准水涨船高。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import polars as pl

from lquant.factors.evaluate.ic import _t_stat as tstat
from lquant.factors.evaluate.ic import ic_series
from lquant.factors.mining.fitness import corrected_threshold
from lquant.factors.mining.gates import GateResult, g0_static, g1_fast_screen, g2_dedup
from lquant.factors.preprocess.pipeline import drop_nonfinite


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
    n_g1_pass: int = 0              # 搜索效率指标：G1 通过数（幸存数被门槛噪声主导）
    g1_ic_sum: float = 0.0          # G1 通过者 |IC| 累计
    corrections: list[dict] = field(default_factory=list)


def horizon_from_ret_col(ret_col: str) -> int:
    """从前瞻收益标签列名 ``fwd_ret_h`` 解析前瞻期数 h。

    purge 必须由标签口径推出来（h 根），不能拍一个魔法常数：换 h 就得换 purge，
    否则训练段尾部照样有 h-1 根样本的标签伸进验证段。解析不了直接报错 ——
    静默退回 0 等于没做防泄漏。
    """
    prefix = "fwd_ret_"
    if not ret_col.startswith(prefix):
        raise ValueError(
            f"无法从列名 {ret_col!r} 解析前瞻期数：期望 fwd_ret_<h> 形式")
    try:
        h = int(ret_col[len(prefix):])
    except ValueError as e:
        raise ValueError(
            f"无法从列名 {ret_col!r} 解析前瞻期数：{ret_col[len(prefix):]!r} 不是整数") from e
    if h < 0:
        raise ValueError(f"前瞻期数不能为负：{ret_col!r}")
    return h


def split_dates(dates: list, train=0.7, val=0.15,
                purge_bars: int = 0, embargo_bars: int = 0) -> tuple[list, list, list]:
    """70/15/15 时间切分：搜索用 train，val 只在 G3 解锁一次，test 不碰。

    ``purge_bars`` / ``embargo_bars``（单位：交易日根数，默认 0 = 历史行为不变）
    的口径与 ``research.ml.dataset.walk_forward_splits`` 完全一致：

    - purge：每个拟合段（train、val）尾部剪掉 N 根。标签是前瞻 h 日收益，
      尾部 N=h 根的标签会伸进下一段，把「未来」带进训练；
    - embargo：每个评估段（val、test）头部再剪 N 根，隔开边界自相关。

    这里返回的是**日期列表**，缺口天然被表达（列表里没有的日期就是缺口），
    调用方按 ``is_in`` 过滤即可，不会像连续边界那样把缺口又并回去。
    """
    if purge_bars < 0 or embargo_bars < 0:
        raise ValueError(
            f"purge_bars/embargo_bars 不能为负："
            f"purge={purge_bars}, embargo={embargo_bars}")
    ds = sorted(set(dates))
    n = len(ds)
    i1 = int(n * train)
    i2 = int(n * (train + val))
    tr, va, te = ds[:i1], ds[i1:i2], ds[i2:]
    if purge_bars or embargo_bars:
        # ``tr[:-0]`` 会返回空列表，所以 purge=0 必须走另一支，不能直接写死。
        tr = tr[:-purge_bars] if purge_bars else tr
        va = va[embargo_bars:len(va) - purge_bars]
        te = te[embargo_bars:]
    return tr, va, te


def run_session(engine, panel, generator, *, agent="builtin", n_candidates=100,
                covs=None, survivors=None, ret_col="fwd_ret_1") -> SessionResult:
    """跑一轮挖掘会话：G0 -> G1(train) -> G2(去重) -> G3(val 解锁一次)。

    generator: 可调用对象，产出候选表达式字符串。能吐字符串就能接。
    """
    from lquant.factors.evaluate import forward_return


    survivors = list(survivors or [])
    dates = panel["trade_date"].unique().to_list()
    # purge 必须等于前瞻期数 h：本会话的标签在下面按 ret_col 计算，训练段尾部
    # h 根的标签会看到 val 段（前瞻收益），不剪掉就是标签泄漏。embargo 再留
    # 1 根隔离边界自相关。两者都从标签口径推出，不是硬编码的常数。
    purge = horizon_from_ret_col(ret_col)
    train_d, val_d, _test = split_dates(dates, purge_bars=purge, embargo_bars=1)
    if not val_d:
        raise ValueError(
            f"purge={purge}/embargo=1 把 val 段剪空了（{len(dates)} 个交易日），"
            f"无法做样本外复核；请先补历史数据")
    train = panel.filter(pl.col("trade_date").is_in(train_d))
    val = panel.filter(pl.col("trade_date").is_in(val_d))
    train = forward_return(train.sort(["symbol", "trade_date"]), "close", periods=[1])
    train = train.drop_nulls(["fwd_ret_1"])
    res = SessionResult(agent=agent)
    n_static = n_ic = n_red = n_proxy = 0
    for _ in range(n_candidates):
        try:
            expr = generator()
        except StopIteration:
            # 有限生成器（如 LLM 提案列表）提前耗尽 —— 是「提案用完了」，不是出错。
            # 让它冒泡会把 n 略大于提案条数这种常见情况变成一次难懂的崩溃。
            break
        res.n_evaluated += 1
        g0 = g0_static(expr, allowed_fields=set(panel.columns))
        if not g0.passed:
            n_static += 1
            res.corrections.append({"expr": expr, "stage": "G0", "reason": g0.reason_code, "hint": g0.hint})
            continue
        g1 = g1_fast_screen(train, expr, ret_col, engine, covs=covs)
        if g1.passed:
            res.n_g1_pass += 1
            _ic = g1.ic if (g1.ic is not None and math.isfinite(g1.ic)) else 0.0
            res.g1_ic_sum += abs(_ic)
        if hasattr(generator, "feedback") and g1.ic is not None:
            from lquant.factors.mining.fitness import fitness

            generator.feedback(expr, fitness(g1.ic, g1.ic_raw or g1.ic))
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
        val_x = drop_nonfinite(val_x, "fwd_ret_1")
        try:
            dv = engine.compute(expr, "f")
            val_j = dv.join(val_x, on=["symbol", "trade_date"], how="inner",
                            suffix="_r")
            s = ic_series(drop_nonfinite(val_j, "f"), "f", ret_col)
            if not len(s):
                raise ValueError("val IC 序列为空")
            ic_val = float(s["ic"].mean())
            if not math.isfinite(ic_val):
                raise ValueError("val IC 非有限值（数据 NaN 残留）")
            t_val = tstat(ic_val, float(s["ic"].std()), len(s))
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

