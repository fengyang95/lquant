"""lq backtest。"""

from __future__ import annotations

import json

import click


@click.group()
def backtest() -> None:
    """回测"""


@backtest.command()
@click.option("--factor", "factor_expr", default=None, help="因子 DSL 表达式或已注册名")
@click.option(
    "--spec",
    "spec_path",
    default=None,
    type=click.Path(exists=True),
    help="FactorSpec yaml（研报复现的验证终点）",
)
@click.option("--start", default=None)
@click.option("--end", default=None)
@click.option("--n-groups", default=5)
@click.option(
    "--save/--no-save", default=True, help="实验落库 backtest_run（默认记录；params 附 git_hash）"
)
def run(
    factor_expr: str | None,
    spec_path: str | None,
    start: str | None,
    end: str | None,
    n_groups: int,
    save: bool,
) -> None:
    """因子多空分层回测（复现工作流的验证终点；平台算，Agent 不许自算）。"""
    from lquant.data.store.parquet import read_daily
    from lquant.factors.analysis import compute_factor_col
    from lquant.factors.covariates import build_covariates
    from lquant.factors.evaluate import forward_return
    from lquant.factors.evaluate.quantile import quantile_summary
    from lquant.factors.preprocess.pipeline import drop_nonfinite
    from lquant.factors.preprocess.pipeline import run as pipeline_run

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
    df, report = build_covariates(
        df, ["market_cap", "industry_sw1", "turnover_1m"], industry_df=ind
    )
    cov_cols = [f"cov_{r['covariate']}" for r in report if r["coverage"] > 0]
    d = drop_nonfinite(compute_factor_col(df, factor_expr, "f"), "f")
    d = forward_return(d, "close", periods=[1])
    ret_col = "fwd_ret_1"
    if cov_cols:
        d = pipeline_run(
            d,
            "f",
            [
                {"op": "winsorize", "method": "mad", "n": 5},
                {"op": "standardize", "method": "zscore"},
                {"op": "neutralize", "method": "ols", "factors": cov_cols},
            ],
        )
        d = drop_nonfinite(d, "f")
    qsum = quantile_summary(d, "f", ret_col, n_groups)
    if not qsum.get("groups"):
        raise click.ClickException("分层结果为空 —— 样本不足")
    ls = qsum.get("long_short", {})
    result = {
        "factor": factor_expr,
        "n_groups": n_groups,
        "n_days": d["trade_date"].n_unique(),
        "monotonicity": qsum.get("monotonicity"),
        "long_short": ls,
        "note": "多空分层回测（日频、等权、next-day 收益口径）",
    }
    if save:
        # 记账失败不吞主输出：结果已算出，警告走 stderr，退出码不变
        try:
            from lquant.backtest.runs import record_run

            result["run_id"] = record_run(
                "factor_quantile",
                {"factor": factor_expr, "n_groups": n_groups, "spec": spec_path},
                {
                    "monotonicity": qsum.get("monotonicity"),
                    "long_short": ls,
                    "n_days": result["n_days"],
                },
                start_date=start,
                end_date=end,
            )
        except Exception as e:  # noqa: BLE001
            click.echo(f"实验记录失败（回测结果不受影响）: {type(e).__name__}: {e}", err=True)
    click.echo(json.dumps(result, ensure_ascii=False, default=str))


@backtest.command("list")
@click.option("--limit", default=20, show_default=True)
@click.option("--strategy", default=None, help="按 strategy 过滤（如 factor_quantile）")
def list_cmd(limit: int, strategy: str | None) -> None:
    """历史实验清单（最近优先，CLI 与 Web API 落库同源）"""
    from lquant.backtest.runs import list_runs

    runs = list_runs(limit=limit, strategy=strategy)
    if not runs:
        click.echo("暂无回测记录（跑一次 lq backtest run，或从 Web 提交回测）")
        return
    for r in runs:
        p = {k: v for k, v in (r["params"] or {}).items() if k != "git_hash"}
        summary = ", ".join(f"{k}={v}" for k, v in list(p.items())[:3])
        click.echo(
            f"{r['run_id']}  {r['strategy']:<16} {r['start_date'] or '—'}"
            f"~{r['end_date'] or '—'}  {r['created_at']}  {summary}"
        )
    if len(runs) >= limit:
        click.echo(f"（仅显示最近 {limit} 条，--limit 调整）")


@backtest.command()
@click.argument("run_id")
def show(run_id: str) -> None:
    """单条实验详情（params + metrics，JSON 输出）"""
    from lquant.backtest.runs import get_run

    try:
        r = get_run(run_id)
    except KeyError:
        raise click.ClickException(f"实验不存在: {run_id}（lq backtest list 查看清单）") from None
    click.echo(json.dumps(r, ensure_ascii=False, indent=1, default=str))


@backtest.command()
@click.argument("run_a")
@click.argument("run_b")
def diff(run_a: str, run_b: str) -> None:
    """对比两个实验的 params / metrics 差异（含 git_hash 变化）"""
    from lquant.backtest.runs import diff_runs

    try:
        d = diff_runs(run_a, run_b)
    except KeyError as e:
        raise click.ClickException(
            f"实验不存在: {e.args[0]}（lq backtest list 查看清单）"
        ) from None
    for section in ("params", "metrics"):
        sec = d[section]
        click.echo(f"== {section} ==")
        for k, v in sec["only_a"].items():
            click.echo(f"  仅A  {k} = {v}")
        for k, v in sec["only_b"].items():
            click.echo(f"  仅B  {k} = {v}")
        for k, (va, vb) in sec["changed"].items():
            click.echo(f"  变更 {k}: {va}  ->  {vb}")
    click.echo(f"A: {run_a} ({d['a']['strategy']}, {d['a']['created_at']})")
    click.echo(f"B: {run_b} ({d['b']['strategy']}, {d['b']['created_at']})")
