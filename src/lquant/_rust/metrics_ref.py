"""指标 Python 参考实现（IC / 绩效）。

与 Rust（crates/lq-metrics）单测对拍：同一组确定性输入，两边输出逐位一致。
这些函数是 lq_metrics 的**参照物**（reference truth），不是运行时求值路径 ——
CRUCIAL：改动 Rust 一侧算法时，先在这里改参考实现再跑对拍测试，保证两侧同义。

注意保持与 Rust 逐行同语义：
- rank_ic 用平均秩处理并列（ties），样本 <3 或方差 <=0 返回 NaN；
- rank_ic 在配对前**成对剔除任一侧非有限的样本**（与 Rust lib.rs 的
  `filter(|(f, r)| f.is_finite() && r.is_finite())` 一致）—— 此前 NaN 直接
  进入排序，与 Rust 及生产 IC 路径（ic.py 的 is_finite）双重背离；
- max_drawdown 返回最大峰值回撤比值。
"""
from __future__ import annotations

import math


def rank_ic(factor: list[float], fwd_ret: list[float]) -> float:
    """截面 IC：factor 与 fwd_ret 的 Spearman 秩相关。与 lq_metrics.rank_ic 对拍。"""
    # 与 Rust 同构：zip 截短 + 成对剔除任一侧非有限值（NaN/±inf）
    pairs = [(f, r) for f, r in zip(factor, fwd_ret, strict=False)
             if math.isfinite(f) and math.isfinite(r)]
    n = len(pairs)
    if n < 3:
        return math.nan
    rf = _rank([p[0] for p in pairs])
    rr = _rank([p[1] for p in pairs])
    mf = sum(rf) / n
    mr = sum(rr) / n
    cov = v1 = v2 = 0.0
    for a, b in zip(rf, rr, strict=True):
        cov += (a - mf) * (b - mr)
        v1 += (a - mf) * (a - mf)
        v2 += (b - mr) * (b - mr)
    if v1 <= 0.0 or v2 <= 0.0:
        return math.nan
    return cov / math.sqrt(v1 * v2)


def _rank(v: list[float]) -> list[float]:
    """平均秩：并列元素取相同均值秩（对应 Rust 的 rank()）。"""
    order = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def max_drawdown(nav: list[float]) -> float:
    """最大回撤：(峰 - 谷) / 峰 的最大值。与 lq_metrics.max_drawdown 对拍。"""
    peak = float("-inf")
    mdd = 0.0
    for v in nav:
        peak = max(peak, v)
        if peak > 0.0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd