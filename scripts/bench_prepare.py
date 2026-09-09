"""Benchmark for Engine.prepare() — run: .venv/bin/python scripts/bench_prepare.py"""
import time
from datetime import date, timedelta

import numpy as np
import polars as pl

from lquant.backtest.engine import Engine


def gen(n_days=250, n_sym=1000):
    rng = np.random.default_rng(0)
    d0 = date(2025, 1, 1)
    dates = [d0 + timedelta(days=i) for i in range(n_days)]
    syms = [f"{600000 + i}.SH" for i in range(n_sym)]
    n = n_days * n_sym
    px = rng.lognormal(0, 0.02, n).cumsum() + 10
    return pl.DataFrame({
        "trade_date": [d for d in dates for _ in syms],
        "symbol": syms * n_days,
        "open": px, "high": px * 1.01, "low": px * 0.99, "close": px,
        "pre_close": px, "volume": rng.integers(1e5, 1e6, n).astype(float),
        "amount": px * 1e5, "adj_factor": 1.0, "mom": rng.normal(0, 1, n),
    })


if __name__ == "__main__":
    df = gen()
    for trial in range(3):
        t = time.perf_counter()
        bars = Engine.prepare(df, extra_fields=["mom"])
        print(f"trial {trial}: {time.perf_counter() - t:.2f}s, days={len(bars)}")
