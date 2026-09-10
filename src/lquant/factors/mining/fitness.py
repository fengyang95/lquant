"""适应度与多重检验校正（方案 3.3）。

fitness = |IC_neutral| - ic_decay hinge 惩罚；
门槛 = sqrt(2*ln(n_trials))（2000 次试验 -> 3.90 而非 1.96）。
"""
from __future__ import annotations

import math


def corrected_threshold(n_trials: int) -> float:
    """Bonferroni 风格校正门槛 sqrt(2*ln(n_trials))。n<2 时退化为 1.96。"""
    if n_trials < 2:
        return 1.96
    return math.sqrt(2 * math.log(n_trials))


def fitness(ic_neutral: float, ic_raw: float, decay_hinge: float = 0.5) -> float:
    """适应度 = |IC 中性化| - hinge 惩罚（衰减>50% 开始罚，>80% 已被 G1 打 SIZE_PROXY）。"""
    decay = 1 - abs(ic_neutral) / max(abs(ic_raw), 1e-12)
    penalty = max(0.0, decay - decay_hinge) * 2
    return abs(ic_neutral) - penalty
