"""算子 Python 参考实现 —— 与 Rust（crates/lq-ops 的 polars 插件）对拍。

这些函数不是运行时求值路径，是 Rust 插件的**参照物**：同一组确定性输入，
Rust 插件输出与这里逐位一致，对拍测试（tests/unit/test_rust_alignment.py）据此
拦住「一边改了另一边没改」的漂移。

CRUCIAL：改动 Rust 插件算法时，先改这里的参考实现再跑对拍，保证两侧同义。
窗口语义与 Rust 对齐：输出长度 = len(x)，窗口不足（i+1 < n）或窗口内存在空值
→ 该位置为 None（Rust 用 Option<f64>，空值 null）；零方差 → None（Rust 的
vx<=0/vy<=0 分支，对应 polars 中 NaN 被清成 null）。
"""
from __future__ import annotations

import math


def ts_corr(x: list[float | None], y: list[float | None], n: int) -> list[float | None]:
    """滚动窗口 Pearson 相关（总体协方差口径，与 Rust ts_corr 一致）。"""
    out: list[float | None] = []
    L = len(x)
    for i in range(L):
        if i + 1 < n:
            out.append(None)
            continue
        sx = sy = sxx = syy = sxy = 0.0
        cnt = 0
        for j in range(i + 1 - n, i + 1):
            a = x[j]
            b = y[j]
            if a is not None and b is not None:
                sx += a
                sy += b
                sxx += a * a
                syy += b * b
                sxy += a * b
                cnt += 1
        if cnt < n:
            out.append(None)
            continue
        nf = float(cnt)
        cov = sxy / nf - (sx / nf) * (sy / nf)
        vx = sxx / nf - (sx / nf) ** 2
        vy = syy / nf - (sy / nf) ** 2
        if vx <= 0.0 or vy <= 0.0:
            out.append(None)
        else:
            out.append(cov / math.sqrt(vx * vy))
    return out


def ts_regbeta(y: list[float | None], x: list[float | None],
               n: int) -> list[float | None]:
    """滚动窗口回归 beta（y 对 x，与 Rust ts_regbeta(y, x, n) 一致）。"""
    out: list[float | None] = []
    L = len(y)
    for i in range(L):
        if i + 1 < n:
            out.append(None)
            continue
        sx = sy = sxx = sxy = 0.0
        cnt = 0
        for j in range(i + 1 - n, i + 1):
            a = x[j]
            b = y[j]
            if a is not None and b is not None:
                sx += a
                sy += b
                sxx += a * a
                sxy += a * b
                cnt += 1
        nf = float(cnt)
        cov = sxy / nf - (sx / nf) * (sy / nf)
        vx = sxx / nf - (sx / nf) ** 2
        if cnt < n or vx <= 0.0:
            out.append(None)
        else:
            out.append(cov / vx)
    return out