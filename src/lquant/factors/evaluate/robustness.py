"""L3 鲁棒性检验：好因子不是「IC 高」，而是「换个窗口 / 换段样本 / 换个起点都还在」。

IC 高只说明「这一组参数、这一段样本上有效」。参数一动就崩、样本一换就反向的因子，
多半是过拟合到特定窗口或特定行情 —— 实盘上表现为「回测很美，上线就废」。

四项检查（对应 knowledge/evaluation-methodology.md 第 3 节）：

- ``param_sensitivity``        窗口参数 ±10/20/30% → ICIR 相对变化
- ``time_stability``           样本等分（默认 2 段）→ 分段 ICIR  min/max 比
- ``start_date_sensitivity``   5 个不同起点 → ICIR 标准差
- ``best_month_removal``       剔掉最好的 N 个月 → 剩余多空收益是否仍为正

外加 ``oos_decay``：样本内/样本外 ICIR 衰减（搜索段 vs 复核段），
是「数据窥探」最直接的照妖镜 —— 前面搜得越狠，这里掉得越惨。

设计约定：全部函数只吃「已对齐的 df（含 fwd_ret_*）」，
``param_sensitivity`` 例外 —— 它要重算因子，所以额外吃 expr + 协变量。
所有数字由平台算，Agent 只读 JSON（方案 6.2）。
"""
from __future__ import annotations

import math
import statistics

import polars as pl

from lquant.factors.dsl.ast_nodes import BinaryOp, Call, Num, UnaryOp
from lquant.factors.dsl.parser import parse
from lquant.factors.dsl.printer import unparse
from lquant.factors.evaluate.ic import _summarize, ic_series

__all__ = [
    "perturbed_expressions", "param_sensitivity",
    "time_stability", "start_date_sensitivity", "best_month_removal",
    "oos_decay", "robustness_summary",
]

# 默认阈值（可被调用方覆盖；判定口径与 knowledge/evaluation-methodology.md 一致）
MAX_PARAM_REL_CHANGE = 0.30      # ICIR 相对变化 < 30% → 参数不脆弱
MIN_SPLIT_ICIR_RATIO = 0.50      # 分段 ICIR min/max > 0.5 → 段间不过分依赖
MAX_START_ICIR_CV = 0.50         # 起点 ICIR 变异系数 < 0.5 → 不依赖起算日
MAX_OOS_DECAY = 0.40             # ICIR 衰减 < 40% → 泛化可接受


# ────────────────────────── 表达式窗口定位 / 扰动 ──────────────────────────

def _collect_nums(node: Node, acc: list) -> list:  # noqa: F821  (Node 仅类型标注)
    """按先序收集 AST 里全部 Num 节点（含其「第几个」位置）。"""
    if isinstance(node, Num):
        acc.append(node)
    elif isinstance(node, Call):
        for a in node.args:
            _collect_nums(a, acc)
    elif isinstance(node, BinaryOp):
        _collect_nums(node.left, acc)
        _collect_nums(node.right, acc)
    elif isinstance(node, UnaryOp):
        _collect_nums(node.arg, acc)
    return acc


def _replace_num(node, target: int, new_value: float, state: list):
    """把先序第 target 个 Num 换成 new_value，重建 AST（不改原对象）。"""
    if isinstance(node, Num):
        i = state[0]
        state[0] += 1
        return Num(new_value) if i == target else node
    if isinstance(node, Call):
        return Call(node.name,
                    [_replace_num(a, target, new_value, state) for a in node.args],
                    node.min_window, node.category)
    if isinstance(node, BinaryOp):
        return BinaryOp(node.op,
                        _replace_num(node.left, target, new_value, state),
                        _replace_num(node.right, target, new_value, state))
    if isinstance(node, UnaryOp):
        return UnaryOp(node.op, _replace_num(node.arg, target, new_value, state))
    return node


def _window_slots(expr: str) -> list[tuple[int, int]]:
    """找出表达式里的「窗口」参数：先序位置 + 整数值。

    只认 ≥2 的整数 —— 这类字面量在 DSL 里就是窗口长度（``Ts_Mean($close,20)``）；
    ``-1`` 这类语义常量不是参数，扰动它只会把因子的定义改掉，
    测出来的是「换个因子」而不是「换个窗口」。
    """
    ast = parse(expr, "robust")
    nums = _collect_nums(ast.root, [])
    return [(i, int(n.value)) for i, n in enumerate(nums)
            if float(n.value).is_integer() and n.value >= 2]


def perturbed_expressions(expr: str, deltas: tuple[float, ...] = (0.1, 0.2, 0.3)
                          ) -> list[dict]:
    """展开窗口扰动：每个窗口 × 上下各 delta → 一批新表达式。

    返回值每项 ``{index, value, delta, perturbed, expr}``，其中 ``delta`` 是
    **实际**发生的相对偏移（小窗口四舍五入后会收敛，5 的 ±10% 和 ±20% 都是 4/6）。

    重复项会被去掉 —— 否则同一条扰动表达式算三遍，「最大变化」这个指标
    会被重复样本带偏。扰动后落回原值或 < 2 的窗口直接丢掉。
    """
    slots = _window_slots(expr)
    ast = parse(expr, "robust")
    out: list[dict] = []
    seen: set[str] = set()
    for idx, value in slots:
        cands: list[float] = []
        for delta in deltas:
            cands.extend([value * (1.0 + delta), value * (1.0 - delta)])
        for new in sorted({int(round(x)) for x in cands} - {value}):
            if new < 2:
                continue
            state = [0]
            root = _replace_num(ast.root, idx, float(new), state)
            try:
                new_expr = unparse(root)
                parse(new_expr, "robust")      # 回环校验：改完必须还是合法表达式
            except Exception:  # noqa: BLE001
                continue
            if new_expr in seen:
                continue
            seen.add(new_expr)
            out.append({"index": idx, "value": value,
                        "delta": (new - value) / value,
                        "perturbed": new, "expr": new_expr})
    return out


# ────────────────────────── 内部：单条表达式的 ICIR ──────────────────────────

def _default_preprocess(covs: list[str]) -> list[dict]:
    return [
        {"op": "winsorize", "method": "mad", "n": 5},
        {"op": "standardize", "method": "zscore"},
        {"op": "neutralize", "method": "ols", "factors": covs},
    ]


def _expr_icir(df: pl.DataFrame, expr: str, ret_col: str, covs: list[str] | None,
               *, date_col: str = "trade_date", symbol_col: str = "symbol",
               sort: bool = True) -> dict:
    """现算一条表达式的中性化后 ICIR —— 与 G1 快筛同一口径（先中性化再算 IC）。"""
    from lquant.factors.analysis import compute_factor_col
    from lquant.factors.evaluate.returns import forward_return
    from lquant.factors.preprocess.pipeline import drop_nonfinite
    from lquant.factors.preprocess.pipeline import run as pipeline_run

    d = df.sort([symbol_col, date_col]) if sort else df
    d = drop_nonfinite(compute_factor_col(d, expr, "f"), "f")
    if covs:
        d = pipeline_run(d, "f", _default_preprocess(covs))
    d = drop_nonfinite(d, "f")
    if ret_col not in d.columns:
        d = forward_return(d, "close", periods=[1])
        ret_col = "fwd_ret_1"
    d = drop_nonfinite(d, ret_col)
    s = ic_series(d, "f", ret_col, date_col=date_col)
    if not len(s):
        return {"icir": float("nan"), "ic_mean": float("nan"), "n_days": 0}
    st = _summarize(s["ic"])
    return {"icir": st["ir"], "ic_mean": st["mean"], "n_days": st["n_days"]}


# ────────────────────────── 1. 参数敏感性 ──────────────────────────

def param_sensitivity(df: pl.DataFrame, expr: str, ret_col: str = "fwd_ret_1", *,
                      covs: list[str] | None = None,
                      deltas: tuple[float, ...] = (0.1, 0.2, 0.3),
                      date_col: str = "trade_date", symbol_col: str = "symbol",
                      max_rel_change: float = MAX_PARAM_REL_CHANGE) -> dict:
    """窗口参数敏感性：每个窗口 ±10/20/30%，看 ICIR 相对变化。

    通过线：全部扰动里 max|ΔICIR|/|ICIR_base| < 30%。
    通不过说明因子是「窗口调出来的」，不是「信号本身就强」——
    下一轮别再试窗口，换字段族。
    """
    base = _expr_icir(df, expr, ret_col, covs, date_col=date_col, symbol_col=symbol_col)
    rows: list[dict] = []
    for spec in perturbed_expressions(expr, deltas):
        r = _expr_icir(df, spec["expr"], ret_col, covs,
                       date_col=date_col, symbol_col=symbol_col)
        rows.append({**spec, **r})

    base_icir = base["icir"]
    worst = float("nan")
    for r in rows:
        r["rel_change"] = (
            abs(r["icir"] - base_icir) / abs(base_icir)
            if math.isfinite(base_icir) and base_icir != 0 and math.isfinite(r["icir"])
            else float("nan")
        )
        if math.isfinite(r["rel_change"]) and (not math.isfinite(worst) or r["rel_change"] > worst):
            worst = r["rel_change"]

    n_slots = len(_window_slots(expr))
    if n_slots == 0:
        return {"baseline": base, "perturbations": [], "max_rel_change": float("nan"),
                "threshold": max_rel_change, "passed": False, "insufficient": True,
                "hint": "表达式里没有窗口参数（无 ≥2 的整数字面量），跳过参数敏感性"}
    passed = bool(math.isfinite(worst) and worst < max_rel_change)
    return {
        "baseline": base,
        "perturbations": rows,
        "max_rel_change": worst,
        "threshold": max_rel_change,
        "passed": passed,
        "insufficient": not math.isfinite(worst),
        "hint": (f"最大 ICIR 相对变化 {worst:.0%}（阈值 {max_rel_change:.0%}）"
                 if math.isfinite(worst) else "窗口扰动无有效 ICIR（样本不足）"),
    }


# ────────────────────────── 2. 时间稳定性 ──────────────────────────

def time_stability(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1", *,
                   n_splits: int = 2, date_col: str = "trade_date",
                   min_ratio: float = MIN_SPLIT_ICIR_RATIO) -> dict:
    """样本等分成 n_splits 个连续时间窗，比较各段 ICIR。

    通过线：min|ICIR|/max|ICIR| > 0.5，且各段符号与全样本一致。
    只有半段有信号 = 行情依赖，「什么行情下会失效」比「平均 IC 多少」更重要。
    """
    if n_splits < 2:
        raise ValueError(f"n_splits 至少为 2，得到: {n_splits}")
    dates = sorted(set(df[date_col].unique().to_list()))
    n = len(dates)
    rows: list[dict] = []
    if n < n_splits * 2:
        return {"splits": [], "icir_ratio": float("nan"), "threshold": min_ratio,
                "passed": False, "insufficient": True,
                "hint": f"交易日不足（{n} 天 < {n_splits * 2}），无法分段"}

    bounds = [round(k * n / n_splits) for k in range(n_splits + 1)]
    for k in range(n_splits):
        chunk = dates[bounds[k]:bounds[k + 1]]
        sub = df.filter(pl.col(date_col).is_in(chunk))
        s = ic_series(sub, factor, ret_col, date_col=date_col)
        st = _summarize(s["ic"]) if len(s) else None
        rows.append({
            "seg": k + 1, "start": str(chunk[0]), "end": str(chunk[-1]),
            "n_days": st["n_days"] if st else 0,
            "ic_mean": st["mean"] if st else float("nan"),
            "icir": st["ir"] if st else float("nan"),
        })

    icirs = [r["icir"] for r in rows if math.isfinite(r["icir"])]
    if not icirs:
        return {"splits": rows, "icir_ratio": float("nan"), "threshold": min_ratio,
                "passed": False, "hint": "各段 ICIR 均无有效值"}
    hi, lo = max(abs(v) for v in icirs), min(abs(v) for v in icirs)
    ratio = lo / hi if hi > 0 else float("nan")
    signs = {1 if v > 0 else -1 if v < 0 else 0 for v in icirs}
    sign_consistent = len(signs) == 1 and 0 not in signs
    passed = bool(math.isfinite(ratio) and ratio > min_ratio and sign_consistent)
    hint = f"分段 ICIR 比 {ratio:.2f}（阈值 {min_ratio}）"
    if not sign_consistent:
        hint += "；各段 IC 符号不一致 —— 因子在部分区间反向"
    return {"splits": rows, "icir_ratio": ratio, "threshold": min_ratio,
            "sign_consistent": sign_consistent, "passed": passed, "hint": hint}


# ────────────────────────── 3. 起始日敏感性 ──────────────────────────

def start_date_sensitivity(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1", *,
                           n_starts: int = 5, date_col: str = "trade_date",
                           max_cv: float = MAX_START_ICIR_CV) -> dict:
    """从 n_starts 个不同起点算 ICIR，看结果的起点依赖有多强。

    判定用的是**变异系数**（std/|mean|）而不是 ICIR 的绝对标准差：
    绝对标准差随 ICIR 量级放大 —— ICIR≈0.5 的因子波动 0.2 叫不稳，
    ICIR≈2.0 的因子波动 0.2 几乎可以忽略，用同一个绝对阈值会误杀后者。
    （参考实现给的是「标准差 < 0.3」，那条线只在 ICIR 量级 ~1 时成立。）

    通过线：变异系数 < 0.5，且各起点 ICIR 同号。
    符号不一致 = 结论「取决于从哪天开始算」，报告里能挑出漂亮的一段，
    但事先没法知道该从哪天开始。
    """
    dates = sorted(set(df[date_col].unique().to_list()))
    n = len(dates)
    min_days = max(20, n // (n_starts * 2))
    if n < n_starts + min_days:
        return {"runs": [], "icir_std": float("nan"), "icir_cv": float("nan"),
                "threshold": max_cv, "passed": False, "insufficient": True,
                "hint": f"交易日不足（{n} 天），无法做起点敏感性"}

    span = n - min_days
    idxs = sorted({int(round(k * span / max(n_starts - 1, 1))) for k in range(n_starts)})
    rows: list[dict] = []
    for i in idxs:
        sub = df.filter(pl.col(date_col) >= dates[i])
        s = ic_series(sub, factor, ret_col, date_col=date_col)
        st = _summarize(s["ic"]) if len(s) else None
        rows.append({"start": str(dates[i]), "n_days": st["n_days"] if st else 0,
                     "ic_mean": st["mean"] if st else float("nan"),
                     "icir": st["ir"] if st else float("nan")})

    icirs = [r["icir"] for r in rows if math.isfinite(r["icir"])]
    if len(icirs) < 2:
        return {"runs": rows, "icir_std": float("nan"), "icir_cv": float("nan"),
                "threshold": max_cv, "passed": False, "hint": "有效起点不足 2 个"}
    std = statistics.pstdev(icirs)
    mean = statistics.fmean(icirs)
    cv = std / abs(mean) if mean != 0 else float("inf")
    signs = {1 if v > 0 else -1 for v in icirs}
    sign_consistent = len(signs) == 1
    passed = bool(math.isfinite(cv) and cv < max_cv and sign_consistent)
    hint = (f"起点 ICIR 变异系数 {cv:.0%}（阈值 < {max_cv:.0%}），"
            f"均值 {mean:.3f}，绝对标准差 {std:.3f}")
    if not sign_consistent:
        hint += "；各起点 ICIR 符号不一致 —— 结论取决于从哪天开始算"
    return {"runs": rows, "icir_std": std, "icir_mean": mean, "icir_cv": cv,
            "sign_consistent": sign_consistent, "threshold": max_cv,
            "passed": passed, "hint": hint}


# ────────────────────────── 4. 剔除最佳月份 ──────────────────────────

def best_month_removal(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1", *,
                       top_n: int = 5, n_groups: int = 10,
                       date_col: str = "trade_date") -> dict:
    """剔掉多空收益最好的 top_n 个月，看剩下的还算不算正。

    通过线：剩余累计收益 > 0。全部 alpha 集中在少数几个月
    = 「历史上恰好赶上了」，不是可持续的信号。

    月份数不足 2×top_n 时返回 ``insufficient=True``（而非判失败）：
    样本只有 4 个月却要剔掉 5 个，剔完什么都不剩，这个结论没有信息量 ——
    和「因子不稳」是两回事，调用方应按「跳过」处理。
    """
    from lquant.factors.evaluate.quantile import long_short_nav

    ls = long_short_nav(df, factor, ret_col, n_groups, date_col=date_col)
    if not len(ls):
        return {"months": [], "total_ret": float("nan"), "remaining_ret": float("nan"),
                "passed": False, "insufficient": True,
                "hint": "多空序列为空，无法做月度剔除"}
    try:
        m = ls.with_columns(pl.col(date_col).dt.strftime("%Y-%m").alias("month"))
    except Exception as e:  # noqa: BLE001
        return {"months": [], "total_ret": float("nan"), "remaining_ret": float("nan"),
                "passed": False, "insufficient": True,
                "hint": f"日期列无法按月聚合（{date_col} 非日期类型）: {e}"}

    monthly = (m.group_by("month")
               .agg(pl.col("ret_long_short").sum().alias("ret"))
               .sort("ret", descending=True))
    months = monthly.to_dicts()
    n_months = len(months)
    if n_months < 2 * top_n:
        return {"months": months, "n_months": n_months, "total_ret": float("nan"),
                "remaining_ret": float("nan"), "passed": False, "insufficient": True,
                "hint": f"月份数不足（{n_months} < {2 * top_n}），"
                        f"剔掉最好的 {top_n} 个月后样本太薄，检验没有意义"}

    total = float(monthly["ret"].sum())
    removed = float(sum(r["ret"] for r in months[:top_n]))
    remaining = total - removed
    passed = bool(remaining > 0)
    dropped = [r["month"] for r in months[:top_n]]
    return {
        "months": months,
        "n_months": n_months,
        "total_ret": total,
        "removed_ret": removed,
        "remaining_ret": remaining,
        "dropped_months": dropped,
        "insufficient": False,
        "passed": passed,
        "hint": (f"剔除最好的 {top_n} 个月（{'、'.join(dropped)}）后剩余 "
                 f"{remaining:.2%}；原始累计 {total:.2%}"),
    }


# ────────────────────────── 5. 样本外衰减 ──────────────────────────

def oos_decay(icir_is: float, icir_oos: float, *, max_decay: float = MAX_OOS_DECAY) -> dict:
    """IS→OOS 的 ICIR 衰减率 = (|ICIR_is| - |ICIR_oos|) / |ICIR_is|。

    通过线：衰减 < 40%。搜索段（train）越狠，复核段（val）掉得越惨 ——
    这是「数据窥探」最直接的量化形态，比看图直观。
    """
    if not math.isfinite(icir_is) or icir_is == 0 or not math.isfinite(icir_oos):
        return {"icir_is": icir_is, "icir_oos": icir_oos, "decay": float("nan"),
                "threshold": max_decay, "passed": False,
                "hint": "IS/OOS ICIR 无效，无法算衰减"}
    decay = (abs(icir_is) - abs(icir_oos)) / abs(icir_is)
    passed = bool(decay <= max_decay)
    return {"icir_is": icir_is, "icir_oos": icir_oos, "decay": decay,
            "threshold": max_decay, "passed": passed,
            "hint": f"ICIR {icir_is:.3f} → {icir_oos:.3f}，衰减 {decay:.0%}"
                    f"（阈值 ≤ {max_decay:.0%}）"}


# ────────────────────────── 汇总 ──────────────────────────

def robustness_summary(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1", *,
                       expr: str | None = None, covs: list[str] | None = None,
                       icir_is: float | None = None, icir_oos: float | None = None,
                       deltas: tuple[float, ...] = (0.1, 0.2, 0.3),
                       n_splits: int = 2, n_starts: int = 5, top_n: int = 5,
                       n_groups: int = 10, date_col: str = "trade_date") -> dict:
    """跑完四项（含可选的参数敏感性 / OOS 衰减），给一个总体判定。

    ``verdict``：全部通过 = ``robust``；任一关键项失败 = ``fragile``；
    一项都没法判 = ``unknown``。

    每项检查都带 ``detail``（哪些窗口/起点/月份出了问题），
    让 Agent 能直接照着改下一轮，而不是只知道「没通过」。

    缺数据（而非失败）的检查记为 ``skipped``，不计入分母 ——
    「样本不够」和「因子不稳」是两件事，混在一起会误导决策。
    """
    checks: list[dict] = []

    if expr:
        c = param_sensitivity(df, expr, ret_col, covs=covs, deltas=deltas,
                              date_col=date_col)
        checks.append({"name": "param_sensitivity", **_status(c),
                       "value": c["max_rel_change"], "threshold": c["threshold"],
                       "hint": c["hint"],
                       "detail": {"baseline": c["baseline"],
                                  "perturbations": c["perturbations"]}})
    else:
        checks.append({"name": "param_sensitivity", "status": "skipped",
                       "hint": "未提供 expr，跳过参数敏感性"})

    c = time_stability(df, factor, ret_col, n_splits=n_splits, date_col=date_col)
    checks.append({"name": "time_stability", **_status(c),
                   "value": c.get("icir_ratio"), "threshold": c.get("threshold"),
                   "hint": c["hint"],
                   "detail": {"splits": c["splits"],
                              "sign_consistent": c.get("sign_consistent")}})

    c = start_date_sensitivity(df, factor, ret_col, n_starts=n_starts, date_col=date_col)
    checks.append({"name": "start_date_sensitivity", **_status(c),
                   "value": c.get("icir_cv"), "threshold": c.get("threshold"),
                   "hint": c["hint"],
                   "detail": {"runs": c["runs"], "icir_mean": c.get("icir_mean"),
                              "icir_std": c.get("icir_std"),
                              "sign_consistent": c.get("sign_consistent")}})

    c = best_month_removal(df, factor, ret_col, top_n=top_n, n_groups=n_groups,
                           date_col=date_col)
    checks.append({"name": "best_month_removal", **_status(c),
                   "value": c.get("remaining_ret"), "threshold": 0.0, "hint": c["hint"],
                   "detail": {"dropped_months": c.get("dropped_months"),
                              "total_ret": c.get("total_ret"),
                              "n_months": c.get("n_months")}})

    if icir_is is not None and icir_oos is not None:
        c = oos_decay(icir_is, icir_oos)
        checks.append({"name": "oos_decay", **_status(c),
                       "value": c["decay"], "threshold": c["threshold"], "hint": c["hint"],
                       "detail": {"icir_is": c["icir_is"], "icir_oos": c["icir_oos"]}})
    else:
        checks.append({"name": "oos_decay", "status": "skipped",
                       "hint": "未提供 IS/OOS ICIR，跳过样本外衰减"})

    judged = [c for c in checks if c["status"] != "skipped"]
    n_passed = sum(1 for c in judged if c["status"] == "passed")
    verdict = ("robust" if n_passed == len(judged)
               else "fragile") if judged else "unknown"
    return {
        "factor": factor,
        "checks": checks,
        "n_passed": n_passed,
        "n_judged": len(judged),
        "verdict": verdict,
    }


def _status(c: dict) -> dict:
    """子检验结果 → 检查状态。``insufficient``（样本不够）归为跳过，不判失败。"""
    if c.get("insufficient"):
        return {"status": "skipped"}
    return {"status": "passed" if c["passed"] else "failed"}
