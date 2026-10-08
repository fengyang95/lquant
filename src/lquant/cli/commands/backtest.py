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


@backtest.command("confidence")
@click.option("--returns", "returns_csv", required=True,
              type=click.Path(exists=True),
              help="日收益 CSV（单列；首行为表头或直接数值）")
@click.option("--n-trials", "n_trials", default=None,
              help="试验次数 N：整数，或 auto=数实验台账（配合 --strategy）")
@click.option("--strategy", default=None, help="n-trials=auto 时按策略名数台账")
@click.option("--benchmark-sr", "bench_annual", default=0.0,
              help="年化夏普基准（换算为日频后做 PSR）")
@click.option("--freq", default=252, show_default=True, help="年化频率")
def confidence(returns_csv: str, n_trials: str | None, strategy: str | None,
               bench_annual: float, freq: int) -> None:
    """回答「这个 Sharpe 是本事还是运气」：PSR + DSR + E[maxSR]。

    台账纪律：n_trials 要数**所有**试过的配置（含放弃的）。auto 模式从
    实验记录器统计；只有 1 次试验时只报 PSR（DSR 需 N>=2，别自欺）。
    网格级别的过拟合检验（PBO）见 lquant.backtest.confidence.cscv_pbo。
    """
    import json
    import math

    import polars as pl

    from lquant.backtest import confidence as conf

    try:
        df = pl.read_csv(returns_csv)
        # drop_nulls 只滤 null：CSV 里的字面量 nan/inf 会被 polars 解析成
        # NaN/Inf 原样留下，交给 confidence 的输入校验去 fail-loudly。
        rets = df[df.columns[0]].drop_nulls().to_list()
        sr_annual = conf.sharpe_ratio(rets, freq=freq)
        psr_v = conf.psr(rets, sr_benchmark=bench_annual / math.sqrt(freq))

        out = {
            "n_obs": len(rets),
            "sharpe_annual": round(sr_annual, 4),
            "benchmark_sr_annual": bench_annual,
            "psr": round(psr_v, 4),
        }
        if n_trials is None:
            out["note"] = "未给 --n-trials，跳过 DSR（多重检验校正需要 N>=2）"
        else:
            if n_trials == "auto":
                n = conf_runs_count(strategy)
            else:
                try:
                    n = int(n_trials)
                except ValueError as e:
                    raise click.BadParameter(
                        f"--n-trials 需为整数或 auto，收到 {n_trials!r}") from e
            out["n_trials"] = n
            if n < 2:
                if n_trials == "auto":
                    # auto=0 的语义是「台账没数到 N」，不是「无需校正」：
                    # 被分析的那条收益本身就是一次试验，真实 N 只会 ≥1。
                    # 把前者说成「无选择偏差可校正」会把结论方向说反，
                    # 而 DSR 恰恰是本模块存在的意义。
                    scope = f"匹配 {strategy!r} 的" if strategy else ""
                    out["note"] = (
                        f"台账里没有{scope}试验记录（n_trials=0）：N 未知，DSR 不可用"
                        "（这不等于「无选择偏差」）。请确认 --strategy 与落库口径，"
                        "或显式传 --n-trials <N>"
                    )
                else:
                    out["note"] = f"n_trials={n} < 2，无选择偏差可校正，跳过 DSR"
            else:
                d = conf.deflated_sharpe(rets, n_trials=n)
                out["dsr"] = round(d["dsr"], 4)
                out["expected_max_sharpe_annual"] = round(
                    d["expected_max_sharpe_daily"] * math.sqrt(freq), 4)
    except ValueError as e:
        # confidence.py 的输入校验（CSV 含 NaN/Inf、零方差、样本过短）报的是
        # **用户数据问题**，不是程序 bug。裸抛会打一整段 traceback 淹没有效
        # 信息，也与 show/diff 的 "Error: 实验不存在: …" 风格不一致。
        # 这里只换呈现方式：仍非 0 退出、仍把原因原样说清（fail-loudly）。
        raise click.ClickException(str(e)) from None
    click.echo(json.dumps(out, ensure_ascii=False))


def conf_runs_count(strategy: str | None) -> int:
    from lquant.backtest.runs import count_runs

    return count_runs(strategy)
