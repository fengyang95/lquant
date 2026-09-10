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

    from lquant.factors.mining.submit import _daily_fields
    from lquant.factors.mining.gates import g0_static

    r = g0_static(expr, allowed_fields=_daily_fields())
    payload = {"passed": r.passed, "stage": r.stage,
               "reason_code": r.reason_code, "hint": r.hint}
    click.echo(json.dumps(payload, ensure_ascii=False))
    if not r.passed:
        sys.exit(1)


@factor.command("eval")
@click.argument("expr")
@click.option("--start", default=None, help="数据窗口起点 YYYY-MM-DD")
@click.option("--neutral/--raw", default=True, help="是否中性化（默认中性化）")
@click.option("--agent", default=None, help="Agent 名（配额记账 + 校正门槛）")
def eval_(expr: str, start: str | None, neutral: bool, agent: str | None) -> None:
    """IC JSON + 中性化对照 + n_trials/校正门槛/剩余配额（方案 6.2/6.3）。"""
    import json

    from lquant.factors.mining.submit import _panel_with_covs, _split_eval
    import polars as pl

    from lquant.factors.mining.fitness import corrected_threshold

    df, cov_cols = _panel_with_covs(start=start)
    if not len(df):
        raise click.ClickException("日线数据为空，先跑 bootstrap 或 lq data demo")
    splits = _split_eval(df, cov_cols, expr)
    s_tr = splits["train"]
    if not len(s_tr):
        raise click.ClickException("train 段 IC 序列为空 —— 数据或表达式问题")
    from lquant.factors.evaluate.ic import _t_stat as tstat

    ic = float(s_tr["ic"].mean())
    t = tstat(ic, float(s_tr["ic"].std()), len(s_tr))
    # 中性化对照：同口径再算一遍 raw IC
    ic_raw = None
    if cov_cols:
        from lquant.factors.analysis import compute_factor_col
        from lquant.factors.evaluate import forward_return as _fr
        from lquant.factors.evaluate.ic import ic_series

        raw_sub = df.filter(
            pl.col("trade_date").is_in(sorted(df["trade_date"].unique().to_list())[:70]))
        raw_sub = _fr(raw_sub.sort(["symbol", "trade_date"]), "close", periods=[1])
        d_raw = compute_factor_col(raw_sub, expr, "f").drop_nulls(["f", "fwd_ret_1"])
        ic_raw = round(float(ic_series(d_raw, "f", "fwd_ret_1")["ic"].mean()), 4)
    # 预算内建：n_trials（eval+挖掘评估总账）、校正门槛、剩余配额
    n_trials, remaining, hints, thr = 0, None, [], None
    if agent:
        from lquant.factors.agents import eval_usage, find_agent, quota_remaining, record_eval

        a = find_agent(agent)
        if not a:
            raise click.ClickException(f"Agent 未注册: {agent}")
        n_trials = record_eval(agent)
        remaining = quota_remaining(agent)
        thr = corrected_threshold(max(n_trials, 2))
        if a.quota_eval <= 0 or remaining <= 0:
            raise click.ClickException(f"配额已用尽: {agent}")
        if abs(t) < thr:
            hints.append(f"|t|={abs(t):.2f} 低于校正门槛 {thr:.2f}（n_trials={n_trials}）")
        hints.append(f"剩余配额 {remaining} 次")
    click.echo(json.dumps({
        "ic_mean": round(ic, 4), "rank_ic_mean": round(float(s_tr["rank_ic"].mean()), 4),
        "t_stat": round(t, 2), "n_days": len(s_tr),
        "ic_raw_mean": ic_raw, "neutralized": bool(cov_cols),
        "n_trials": n_trials, "corrected_threshold": round(thr, 2) if thr else None,
        "quota_remaining": remaining, "hints": hints,
    }, ensure_ascii=False))


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
    from lquant.factors.agents import quota_remaining

    remaining = quota_remaining(agent)
    if n > remaining:
        raise click.ClickException(f"超出剩余配额: n={n} > remaining={remaining}")
    if n > a.quota_eval:
        raise click.ClickException(f"超出配额: n={n} > quota={a.quota_eval}")

    df = read_daily(start=start).collect()
    if not len(df):
        raise click.ClickException("日线数据为空，先跑 lq data demo")
    # 方案红线：G1 快筛与适应度一律用中性化后 IC —— 挖掘会话必须带协变量
    from lquant.factors.mining.submit import _panel_with_covs

    df, cov_cols = _panel_with_covs(start=start)
    if not len(df):
        df, cov_cols = df, []
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

    res, survivors = run_session(eng, df, gen, agent=agent, n_candidates=n,
                                 covs=cov_cols)

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
                 json.dumps(res.corrections, ensure_ascii=False)[:10000], dt.datetime.now()])
    except Exception as e:  # noqa: BLE001
        click.echo(f"[warn] 记账落库失败（结果仍有效）: {e}")
    click.echo(json.dumps({
        "run_id": run_id, "agent": agent, "generator": generator,
        "n_evaluated": res.n_evaluated, "n_static_fail": res.n_static_fail,
        "n_low_ic": res.n_low_ic, "n_redundant": res.n_redundant,
        "n_size_proxy": res.n_size_proxy, "n_survivors": res.n_survivors,
        "survivors": survivors[:10],
    }, ensure_ascii=False))


@factor.command()
@click.argument("expr")
@click.option("--start", default=None)
def series(expr: str, start: str | None) -> None:
    """逐日 IC/RankIC/累计 IC 序列 JSON（图表数据，平台算）。"""
    import json

    from lquant.factors.mining.submit import _panel_with_covs, _split_eval

    df, cov_cols = _panel_with_covs(start=start)
    splits = _split_eval(df, cov_cols, expr)
    s = splits["train"]
    if not len(s):
        raise click.ClickException("train 段 IC 序列为空")
    click.echo(json.dumps({
        "n_days": len(s),
        "ic_mean": round(float(s["ic"].mean()), 4),
        "rank_ic_mean": round(float(s["rank_ic"].mean()), 4),
    }, ensure_ascii=False))


@factor.command()
@click.argument("exprs", nargs=-1, required=True)
@click.option("--start", default=None)
@click.option("--threshold", default=0.7)
def corr(exprs: tuple, start: str | None, threshold: float) -> None:
    """库内查重/自查：表达式两两横截面 Spearman 相关 + 冗余对（平台算）。"""
    import json

    from lquant.data.store.parquet import read_daily
    from lquant.factors.analysis import correlation

    df = read_daily(start=start).collect()
    if not len(df):
        raise click.ClickException("日线数据为空，先跑 bootstrap 或 lq data demo")
    res = correlation(df, list(exprs), threshold=threshold)
    click.echo(json.dumps(res, ensure_ascii=False))
