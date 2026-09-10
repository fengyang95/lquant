"""lq backtest。"""
from __future__ import annotations

import click


@click.group()
def backtest() -> None:
    """回测"""


@backtest.command()
@click.option("--factor", "factor_expr", default=None, help="因子 DSL 表达式或已注册名")
@click.option("--spec", "spec_path", default=None, type=click.Path(exists=True),
              help="FactorSpec yaml（研报复现的验证终点）")
@click.option("--start", default=None)
@click.option("--end", default=None)
@click.option("--n-groups", default=5)
def run(factor_expr: str | None, spec_path: str | None, start: str | None,
        end: str | None, n_groups: int) -> None:
    """因子多空分层回测（复现工作流的验证终点；平台算，Agent 不许自算）。"""
    import json

    import polars as pl

    from lquant.data.store.parquet import read_daily
    from lquant.factors.analysis import compute_factor_col
    from lquant.factors.covariates import build_covariates
    from lquant.factors.evaluate import forward_return
    from lquant.factors.evaluate.quantile import quantile_summary

    if spec_path:
        from lquant.factors.replication import load_spec

        spec = load_spec(spec_path)
        factor_expr = spec.expr
        if spec.window and spec.window.get("start") and not start:
            start = spec.window["start"]
        if spec.window and spec.window.get("end") and not end:
            end = spec.window["end"]
    if not factor_expr:
        raise click.ClickException("需要 --factor 表达式 或 --spec yaml")

    from lquant.core.db import reader as db_reader

    df = read_daily(start=start, end=end).collect()
    if not len(df):
        raise click.ClickException("日线数据为空，先跑 bootstrap 或 lq data demo")
    try:
        with db_reader() as con:
            ind = con.execute("SELECT symbol, std, code, std_date FROM industry_classify").pl()
    except Exception:  # noqa: BLE001
        ind = None
    df, report = build_covariates(df, ["market_cap", "industry_sw1", "turnover_1m"],
                                  industry_df=ind)
    cov_cols = [f"cov_{r['covariate']}" for r in report if r["coverage"] > 0]
    d = compute_factor_col(df, factor_expr, "f").drop_nulls(["f"])
    d = forward_return(d, "close", periods=[1])
    ret_col = "fwd_ret_1"
    if cov_cols:
        from lquant.factors.preprocess.pipeline import run as pipeline_run

        d = pipeline_run(d, "f", [
            {"op": "winsorize", "method": "mad", "n": 5},
            {"op": "standardize", "method": "zscore"},
            {"op": "neutralize", "method": "ols", "factors": cov_cols},
        ]).drop_nulls(["f"])
    qsum = quantile_summary(d, "f", ret_col, n_groups)
    if not qsum.get("groups"):
        raise click.ClickException("分层结果为空 —— 样本不足")
    ls = qsum.get("long_short", {})
    click.echo(json.dumps({
        "factor": factor_expr, "n_groups": n_groups,
        "n_days": d["trade_date"].n_unique(),
        "monotonicity": qsum.get("monotonicity"),
        "long_short": ls,
        "note": "多空分层回测（日频、等权、next-day 收益口径）",
    }, ensure_ascii=False, default=str))
