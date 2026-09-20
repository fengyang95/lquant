"""因子评估正确性对照验证：我们的指标 vs 聚宽因子分析口径。

选 A 股公认方向明确的经典因子，输出指标应落在聚宽/学术公认的经验区间：
- 动量 pct_change_20：A 股月频动量弱/反转，RankIC 应为负或近零（|IC| < 0.05）
- 波动率 vol_20：低波异象 → 因子=波动率，RankIC 应显著为负（-0.03 ~ -0.08）
- 换手 turnover_rate：高换手溢价为负，RankIC 显著为负
- 规模 size（log 流通市值）：小市值溢价 → RankIC 显著为负
- 价值 value_ep（1/PE）：RankIC 弱正或近零
每个因子算：RankIC 均值/IR/t(NW)/正占比、分层(10 组)多空年化/夏普/单调性、换手率。
"""
import sys

import polars as pl

sys.path.insert(0, "src")
from lquant.data.store.parquet import read_daily  # noqa: E402
from lquant.factors.evaluate.costs import factor_turnover  # noqa: E402
from lquant.factors.evaluate.ic import ic_summary  # noqa: E402
from lquant.factors.evaluate.quantile import quantile_summary  # noqa: E402

UNIVERSE_FILTER = (
    (pl.col("sec_type") == "stock")
    & ~pl.col("is_st").fill_null(True)
    & ~pl.col("is_suspended").fill_null(True)
)


def load(start: str, end: str) -> pl.DataFrame:
    df = (
        read_daily(start=start, end=end)
        .filter(UNIVERSE_FILTER)
        .with_columns(
            log_mv=pl.col("float_mv").log1p(),
            ep=pl.when(pl.col("pe_ttm") > 0).then(1.0 / pl.col("pe_ttm")).otherwise(None),
        )
        .collect()
    )
    # fwd_ret_5：T+1 至 T+5 累计收益（因子用 T 日信息，避免同 bar 未来函数）
    df = df.sort(["symbol", "trade_date"]).with_columns(
        fwd_ret_5=(
            pl.col("close").shift(-5).over("symbol")
            / pl.col("close").shift(-1).over("symbol")
            - 1
        )
    )
    return df


def factor_defs() -> dict[str, pl.Expr]:
    return {
        "momentum_20": (
            pl.col("close").shift(1).over("symbol")
            / pl.col("close").shift(21).over("symbol")
            - 1
        ),
        "vol_20": pl.col("close").pct_change().over("symbol").rolling_std(20),
        "turnover_rate": pl.col("turnover_rate").cast(pl.Float64),
        "size": pl.col("log_mv"),
        "value_ep": pl.col("ep"),
    }


def evaluate(df, name: str, expr: pl.Expr) -> dict:
    d = df.with_columns(expr.alias(name))
    ic = ic_summary(d, name, "fwd_ret_5", method="spearman")
    qs = quantile_summary(d, name, "fwd_ret_5", n_groups=10)
    to = factor_turnover(d, name, n_groups=10)
    to_mean = float(to["turnover_avg"].mean()) if len(to) else float("nan")
    return {"factor": name, "ic": ic["rank_ic"], "qs": qs, "turnover": to_mean}


def main() -> None:
    df = load("2022-01-01", "2026-09-18")
    print(f"universe rows={len(df)} symbols={df['symbol'].n_unique()} "
          f"dates={df['trade_date'].n_unique()}")

    # 聚宽/学术公认经验区间：(lo, hi, direction 预期)
    EXPECT = {
        "momentum_20": (-0.10, 0.02, "≤0"),
        "vol_20": (-0.10, -0.02, "<0"),
        "turnover_rate": (-0.10, -0.02, "<0 流动性/情绪"),
        "size": (-0.10, -0.01, "<0"),
        "value_ep": (-0.02, 0.08, "≥0"),
    }
    for name, expr in factor_defs().items():
        r = evaluate(df, name, expr)
        ic = r["ic"]
        qs = r["qs"]
        lo, hi, tag = EXPECT[name]
        ic_mean = ic["mean"]
        ok = lo <= ic_mean <= hi
        print(
            f"{name:14s} RankIC={ic_mean:+.4f} IR={ic['ir']:+.2f} t(NW)={ic['t_stat_nw']:+.2f} "
            f"正占比={ic['positive_rate']:.0%} 换手/日={r['turnover']:.1%} "
            f"多空年化={qs['long_short'].get('annual_return', float('nan')):+.1%} "
            f"夏普={qs['long_short'].get('sharpe', float('nan')):.2f} "
            f"单调性={qs['monotonicity']:+.2f} 预期[{tag}] {'OK' if ok else 'OUT-OF-RANGE'}"
        )


if __name__ == "__main__":
    main()
