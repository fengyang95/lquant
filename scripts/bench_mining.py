#!/usr/bin/env python3
"""GP vs random 同预算对照实验（方案 3.3 纪律 1 / M3d 合并门槛）。

同 seed、同预算、同一门禁管线，比较：
- 幸存者数（G0→G1→G2 全过）
- 幸存者平均 |IC 中性化|（G3 val 段）
- G1 通过率

用法: uv run python scripts/bench_mining.py [--n 200]
结论标准：GP 必须在同预算下跑赢 random，否则生成器不允许合并（方案红线）。
"""
from __future__ import annotations

import argparse
import json

from lquant.factors.engine import FactorEngine
from lquant.factors.mining.gp import GPGenerator
from lquant.factors.mining.random_gen import make_generator
from lquant.factors.mining.runner import run_session
from lquant.factors.mining.submit import _panel_with_covs


def bench(generator_name: str, n: int, seed: int, df, cov_cols):
    if generator_name == "gp":
        gen = GPGenerator(seed=seed, pop_size=40, elite=6, p_mutation=0.35)
    else:
        gen = make_generator(seed=seed)
    eng = FactorEngine(df.lazy())
    res, survivors = run_session(eng, df, gen, agent="bench", n_candidates=n, covs=cov_cols)
    mean_ic = (sum(abs(s["ic_neutral"]) for s in survivors) / len(survivors)
               if survivors else 0.0)
    return {
        "generator": generator_name, "n": n, "seed": seed,
        "n_evaluated": res.n_evaluated,
        "n_static_fail": res.n_static_fail,
        "n_low_ic": res.n_low_ic,
        "n_redundant": res.n_redundant,
        "n_size_proxy": res.n_size_proxy,
        "n_survivors": res.n_survivors,
        "mean_abs_ic_survivors": round(mean_ic, 4),
        "n_g1_pass": res.n_g1_pass,
        "mean_g1_abs_ic": round(res.g1_ic_sum / res.n_g1_pass, 4) if res.n_g1_pass else 0.0,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--signal", action="store_true", help="注入 5 日动量信号")
    args = ap.parse_args()

    df, cov_cols = _panel_with_covs()
    if not len(df):
        raise SystemExit("日线数据为空，先跑 lq data demo")
    if args.signal:
        # 构造可发现信号：下一日收益注入 5 日动量结构（GP 可学）
        import polars as pl

        df = df.sort(["symbol", "trade_date"]).with_columns(
            (pl.col("close").pct_change(1).over("symbol") * 0.3).alias("_mom"))
        df = df.with_columns(
            (pl.col("_mom").shift(1).over("symbol")
             + pl.col("_mom").shift(2).over("symbol")
             + pl.col("_mom").shift(3).over("symbol")
             + pl.col("_mom").shift(4).over("symbol")).rolling_sum(1).over("symbol").alias("_sig"))
        df = df.with_columns(pl.col("close") * (1 + pl.col("_sig").fill_null(0) * 0.01))
    gp = bench("gp", args.n, args.seed, df, cov_cols)
    rnd = bench("random", args.n, args.seed, df, cov_cols)
    # 搜索效率判据（幸存数被 G3 校正门槛噪声主导，不作为主要依据）：
    # 1) G1 通过者平均 |IC| 更高  2) 同 |IC| 下 G1 通过率更高
    win = (gp["mean_g1_abs_ic"], gp["n_g1_pass"]) > (rnd["mean_g1_abs_ic"], rnd["n_g1_pass"])
    print(json.dumps({"gp": gp, "random": rnd, "gp_wins": win}, ensure_ascii=False, indent=1))
    raise SystemExit(0 if win else 1)


if __name__ == "__main__":
    main()
