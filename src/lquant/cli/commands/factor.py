"""lq factor：check / eval / submit —— Agent 的操作面（方案 6.2）。

CLI stderr 带结构化淘汰原因码 —— Agent 读错误即自我修正。
"""
from __future__ import annotations

import click


@click.group()
def factor() -> None:
    """因子"""


@factor.command()
@click.argument("expr")
@click.option("--name", default=None)
def add(expr: str, name: str | None) -> None:
    """注册因子表达式（会做 AST 静态检查）。"""
    from lquant.factors.dsl.analyzer import check
    from lquant.factors.dsl.parser import parse

    ast = parse(expr, name or "tmp")
    check(ast)
    click.echo(f"OK {ast.name} min_window={ast.min_window} fields={sorted(ast.fields)}")


@factor.command("check")
@click.argument("expr")
def check_expr(expr: str) -> None:
    """G0 静态校验（毫秒，永远第一步）。退出码非 0 = 校验失败。"""
    import json
    import sys

    from lquant.factors.mining.gates import g0_static

    r = g0_static(expr)
    payload = {"passed": r.passed, "stage": r.stage,
               "reason_code": r.reason_code, "hint": r.hint}
    click.echo(json.dumps(payload, ensure_ascii=False))
    if not r.passed:
        sys.exit(1)


@factor.command()
@click.argument("expr")
@click.option("--start", default=None, help="数据窗口起点 YYYY-MM-DD")
@click.option("--neutral/--raw", default=True, help="是否中性化（默认中性化）")
def eval_(expr: str, start: str | None, neutral: bool) -> None:
    """IC/ICIR/分层/换手 JSON + 中性化对照。所有指标平台算，Agent 不许自算。"""
    import json

    from lquant.data.store.parquet import read_daily
    from lquant.factors.analysis import compute_factor_col
    from lquant.factors.evaluate import forward_return
    from lquant.factors.evaluate.ic import ic_summary
    from lquant.factors.preprocess.pipeline import run as pipeline_run
    from lquant.core.db import reader as db_reader

    df = read_daily(start=start).collect()
    if not len(df):
        raise click.ClickException("日线数据为空，先跑 bootstrap 或 lq data demo")
    covs = None
    if neutral:
        try:
            with db_reader() as con:
                ind = con.execute(
                    "SELECT symbol, std, code, std_date FROM industry_classify").pl()
        except Exception:  # noqa: BLE001
            ind = None
        from lquant.factors.covariates import build_covariates

        df, report = build_covariates(df, ["market_cap", "industry_sw1", "turnover_1m"],
                                      industry_df=ind)
        covs = [f"cov_{r['covariate']}" for r in report if r["coverage"] > 0]
    d = compute_factor_col(df, expr, "f")
    d = forward_return(d, "close", periods=[1, 5])
    d = d.drop_nulls(["f", "fwd_ret_1"])
    if covs:
        d = pipeline_run(d, "f", [
            {"op": "winsorize", "method": "mad", "n": 5},
            {"op": "standardize", "method": "zscore"},
            {"op": "neutralize", "method": "ols", "factors": covs},
        ])
        d = d.drop_nulls(["f"])
    s = ic_summary(d, "f", "fwd_ret_1")
    click.echo(json.dumps({"ic": s["ic"], "rank_ic": s["rank_ic"],
                           "neutralized": bool(covs)}, ensure_ascii=False, default=str))


@factor.command()
@click.argument("spec_path", type=click.Path(exists=True))
def submit(spec_path: str) -> None:
    """★ 唯一入库通道：submit 即重验（M4c）—— 服务端重跑 G0-G3，不信任任何自报数字。"""
    import json
    import sys
    from pathlib import Path

    import yaml

    spec = yaml.safe_load(Path(spec_path).read_text())
    from lquant.factors.mining.submit import verify_and_register

    ok, payload = verify_and_register(spec)
    click.echo(json.dumps(payload, ensure_ascii=False, default=str))
    sys.exit(0 if ok else 1)


@factor.command()
@click.option("--name", required=True)
def run(name: str) -> None:
    click.echo(f"compute {name}")


@factor.command()
@click.option("--agent", default="gp-internal")
@click.option("--generator", default="gp", type=click.Choice(["gp", "random", "proposals"]))
@click.option("--n", default=100, help="候选数量（预算）")
@click.option("--proposals", default=None, help="JSONL 提案文件（generator=proposals）")
@click.option("--start", default=None)
def mine(agent: str, generator: str, n: int, proposals: str | None, start: str | None) -> None:
    """平台驱动挖掘会话：G0-G3 门禁 + 记账落 factor_mining_run。"""
    import json
    import uuid

    from lquant.core.db import writer
    from lquant.data.store.parquet import read_daily
    from lquant.factors.agents import find_agent
    from lquant.factors.engine import FactorEngine
    from lquant.factors.evaluate import forward_return
    from lquant.factors.mining.runner import run_session

    a = find_agent(agent)
    if not a:
        raise click.ClickException(f"Agent 未注册: {agent}（见 lq agent list）")
    if not a.enabled:
        raise click.ClickException(f"Agent 已冻结: {agent}")
    if n > a.quota_eval:
        raise click.ClickException(f"超出配额: n={n} > quota={a.quota_eval}")

    df = read_daily(start=start).collect()
    if not len(df):
        raise click.ClickException("日线数据为空，先跑 bootstrap 或 lq data demo")
    eng = FactorEngine(df.lazy())

    if generator == "gp":
        from lquant.factors.mining.gp import GPGenerator

        gen = GPGenerator(seed=None)
    elif generator == "random":
        from lquant.factors.mining.random_gen import make_generator

        gen = make_generator()
    else:
        from lquant.factors.mining.llm import load_proposals, make_generator as mg

        gen = mg(load_proposals(proposals))

    res, survivors = run_session(eng, df, gen, agent=agent, n_candidates=n)

    # 记账落库
    run_id = uuid.uuid4().hex[:12]
    import datetime as dt

    import polars as pl

    try:
        with writer() as con:
            con.execute(
                "INSERT OR REPLACE INTO factor_mining_run VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [run_id, agent, generator, res.n_evaluated, res.n_static_fail,
                 res.n_low_ic, res.n_redundant, res.n_size_proxy, res.n_survivors,
                 str(res.corrections)[:10000], dt.datetime.now()])
    except Exception as e:  # noqa: BLE001
        click.echo(f"[warn] 记账落库失败（结果仍有效）: {e}")
    click.echo(json.dumps({
        "run_id": run_id, "agent": agent, "generator": generator,
        "n_evaluated": res.n_evaluated, "n_static_fail": res.n_static_fail,
        "n_low_ic": res.n_low_ic, "n_redundant": res.n_redundant,
        "n_size_proxy": res.n_size_proxy, "n_survivors": res.n_survivors,
        "survivors": survivors[:10],
    }, ensure_ascii=False))
