"""random 基线生成器（方案 3.3 纪律 1：任何生成器的验收 = 同预算下跑赢 random）。"""
from __future__ import annotations

import random

FIELDS = ["close", "open", "high", "low", "volume", "amount", "turnover_rate"]
WINDOW_CHOICES = (5, 10, 20, 30, 60)
UNARY_EL = ["Abs", "Log", "Sign", "Sqrt"]
MAX_DEPTH = 3


def _rand_window(rng):
    return rng.choice(WINDOW_CHOICES)


def _expr(rng, depth):
    if depth <= 0:
        return f"${rng.choice(FIELDS)}"
    roll = rng.random()
    if roll < 0.55:
        op = rng.choice(["Ts_Mean", "Ts_Std", "Ts_Return", "Ts_Delay", "Ts_Max", "Ts_Min",
                         "Ts_Sum", "Ts_Delta", "Ts_EMA", "Ts_Rank", "Ts_Skew", "Ts_Prod"])
        return f"{op}({_expr(rng, depth - 1)}, {_rand_window(rng)})"
    if roll < 0.75:
        op = rng.choice(["Rank", "ZScore", "Demean", "Scale"])
        return f"{op}({_expr(rng, depth - 1)})"
    if roll < 0.9:
        op = rng.choice(["Ts_Corr", "Ts_Cov"])
        return f"{op}({_expr(rng, depth - 1)}, {_expr(rng, depth - 1)}, {_rand_window(rng)})"
    op = rng.choice(["Abs", "Log", "Sign"])
    return f"{op}({_expr(rng, depth - 1)})"


def make_generator(seed: int | None = None, depth: int = 2):
    """返回 () -> str 的生成器闭包。"""
    rng = random.Random(seed)
    def gen() -> str:
        return _expr(rng, rng.randint(1, depth))
    return gen
