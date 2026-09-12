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
    from lquant.factors.mining.submit import _daily_fields

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

    import polars as pl

    from lquant.factors.mining.fitness import corrected_threshold
    from lquant.factors.mining.submit import _panel_with_covs, _split_eval

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
        from lquant.factors.mining.runner import split_dates

        tr_d, _v, _t = split_dates(df["trade_date"].unique().to_list())
        raw_sub = df.filter(pl.col("trade_date").is_in(tr_d))
        raw_sub = _fr(raw_sub.sort(["symbol", "trade_date"]), "close", periods=[1])
        d_raw = compute_factor_col(raw_sub, expr, "f").drop_nulls(["f", "fwd_ret_1"])
        ic_raw = round(float(ic_series(d_raw, "f", "fwd_ret_1")["ic"].mean()), 4)
    # 预算内建：n_trials（eval+挖掘评估总账）、校正门槛、剩余配额
    n_trials, remaining, hints, thr = 0, None, [], None
    if agent:
        from lquant.factors.agents import (
            ensure_quota,
            find_agent,
            quota_remaining,
            record_eval,
        )

        a = find_agent(agent)
        if not a:
            raise click.ClickException(f"Agent 未注册: {agent}")
        try:
            ensure_quota(agent, 1)      # 先判后记：超配额时不该再计数
        except ValueError as e:
            raise click.ClickException(str(e)) from e
        n_trials = record_eval(agent)
        remaining = quota_remaining(agent)
        thr = corrected_threshold(max(n_trials, 2))
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

    # 参数校验要前置：漏传 --proposals 时不该先花几十秒把整块面板读进内存，
    # 更不该以 Path(None) 的 TypeError 收场（Agent 拿到的必须是可操作的报错）。
    if generator == "proposals" and not proposals:
        raise click.ClickException(
            "generator=proposals 需要 --proposals <JSONL 路径>"
            "（每行一个 {\"expr\": \"...\", \"note\": \"...\"} 对象）")

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
        from lquant.factors.mining.llm import load_proposals
        from lquant.factors.mining.llm import make_generator as mg

        gen = mg(load_proposals(proposals))

    res, survivors = run_session(eng, df, gen, agent=agent, n_candidates=n,
                                 covs=cov_cols)

    # 记账落库
    run_id = uuid.uuid4().hex[:12]
    import datetime as dt


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
# ─────────────────────── L2/L3：深度校验 · 鲁棒性 · 报告 ───────────────────────
#
# 三级评估面对应参考实现（alpha-skills）的 L0-L3：
#   L0 `lq factor check`  —— 静态校验（毫秒，G0）
#   L1 `lq factor eval`   —— IC 快筛 + 中性化对照 + 校正门槛
#   L2 `lq factor audit`  —— IC/ICIR + 分层 + 衰减 + 归因 + 评级 + 样本外衰减
#   L3 `lq factor robust` —— 参数/时间/起点稳健性 + 剔除最佳月份
# 同一条纪律：数字由平台算，Agent 只读 JSON（方案 6.2）。


def _clean(obj):
    """把 NaN/Inf 收成 null —— 让 Agent 拿到的是严格合法 JSON，而不是 NaN 字面量。"""
    import math

    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def _parse_floats(s: str) -> tuple[float, ...]:
    return tuple(float(x) for x in s.split(",") if x.strip())


def _load_segments(start: str | None, expr: str, *, horizons=(1, 5)):
    """读日线 + 协变量 → 70/15/15 切分 → train/val 两段分析就绪面板。

    与 submit 重验共用 ``prepare_segment``，所以 audit 报的 IC 和入库时
    服务端重算的 IC 是同一个数 —— 口径不允许分叉。
    """
    from lquant.factors.mining.runner import split_dates
    from lquant.factors.mining.submit import _panel_with_covs, prepare_segment

    df, cov_cols = _panel_with_covs(start=start)
    if not len(df):
        raise click.ClickException("日线数据为空，先跑 bootstrap 或 lq data demo")
    dates = sorted(df["trade_date"].unique().to_list())
    train_d, val_d, test_d = split_dates(dates)
    seg = {
        "train": prepare_segment(df, cov_cols, expr, train_d, horizons=list(horizons)),
        "val": prepare_segment(df, cov_cols, expr, val_d, horizons=[1]),
    }
    days = {"train_days": len(train_d), "val_days": len(val_d), "test_days": len(test_d)}
    return seg, cov_cols, days


def _icir(summ: dict, key: str = "rank_ic") -> float:
    """从 ic_summary 输出里取 ICIR（优先 RankIC 口径，A 股极端值多）。"""
    sub = summ.get(key) or summ.get("ic") or {}
    v = sub.get("ir")
    return float(v) if v is not None else float("nan")


def _quota(agent: str | None, expr: str, t_stat: float) -> dict:
    """配额记账 + 校正门槛提示（与 `lq factor eval --agent` 同一套账）。"""
    from lquant.factors.mining.fitness import corrected_threshold

    if not agent:
        return {"n_trials": None, "corrected_threshold": None,
                "quota_remaining": None, "hints": []}
    from lquant.factors.agents import (
        ensure_quota,
        find_agent,
        quota_remaining,
        record_eval,
    )

    if not find_agent(agent):
        raise click.ClickException(f"Agent 未注册: {agent}")
    try:
        ensure_quota(agent, 1)      # 先判后记：超配额时不该再计数（与 eval 一致）
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    n_trials = record_eval(agent)
    remaining = quota_remaining(agent)
    thr = corrected_threshold(max(n_trials, 2))
    hints = []
    if abs(t_stat) < thr:
        hints.append(f"|t|={abs(t_stat):.2f} 低于校正门槛 {thr:.2f}（n_trials={n_trials}）")
    hints.append(f"剩余配额 {remaining} 次")
    return {"n_trials": n_trials, "corrected_threshold": round(thr, 2),
            "quota_remaining": remaining, "hints": hints}


@factor.command()
@click.argument("expr")
@click.option("--start", default=None, help="数据窗口起点 YYYY-MM-DD")
@click.option("--n-groups", default=10, help="分层组数")
@click.option("--horizons", default="1,2,3,5,10,20", help="衰减曲线持有期（逗号分隔）")
@click.option("--agent", default=None, help="Agent 名（配额记账 + 校正门槛）")
def audit(expr: str, start: str | None, n_groups: int, horizons: str, agent: str | None) -> None:
    """L2 深度校验：IC/ICIR + 分层 + 衰减 + 归因 + 评级 + 样本外衰减（平台算）。

    eval 只回答「有没有信号」，audit 回答「信号长什么样、稳不稳、能不能赚」——
    分级、单调性、半衰期、行业暴露、IS→OOS 衰减一次给全。
    """
    import json

    from lquant.factors.dsl.printer import canonical_id
    from lquant.factors.evaluate import (
        attribution_summary,
        decay_summary,
        factor_rating,
        ic_summary,
        oos_decay,
        quantile_summary,
    )

    seg, cov_cols, days = _load_segments(start, expr)
    train, val = seg["train"], seg["val"]
    if not len(train):
        raise click.ClickException("train 段为空 —— 数据或表达式问题")

    ic = ic_summary(train, "f", "fwd_ret_1")
    ic_val = ic_summary(val, "f", "fwd_ret_1") if len(val) else {}
    qs = quantile_summary(train, "f", "fwd_ret_1", n_groups)
    hs = [int(h) for h in horizons.split(",") if h.strip()]
    dec = decay_summary(train, "f", hs)

    t_nw = (ic.get("rank_ic") or ic.get("ic") or {}).get("t_stat_nw")
    q = _quota(agent, expr, float(t_nw) if t_nw is not None else float("nan"))
    rating = factor_rating(ic, qs, n_trials=q["n_trials"])
    oos = (oos_decay(_icir(ic), _icir(ic_val))
           if len(val) and _icir(ic_val) is not None else None)

    cat = next((c for c in ("cov_industry_sw1", "industry_sw1") if c in train.columns), None)
    attr = None
    if cat:
        try:
            a = attribution_summary(train, "f", "fwd_ret_1", cat_col=cat)
            exp = a["industry_exposure"]
            attr = {
                "by": cat,
                "gross_exposure": a["gross_exposure"],
                "industry_exposure": (sorted(exp.to_dicts(),
                                             key=lambda r: -abs(r["exposure"]))[:10]
                                      if len(exp) else []),
            }
        except Exception as e:  # noqa: BLE001
            # 归因失败必须可见：静默成 null 会被读成「这个因子没有行业暴露」
            attr = {"by": cat, "error": f"{type(e).__name__}: {e}"}

    click.echo(json.dumps(_clean({
        "factor": expr,
        "factor_id": canonical_id(expr),
        "n_days": days,
        "neutralized": bool(cov_cols),
        "covariates": cov_cols,
        "rating": rating,
        "ic": {k: (ic.get("ic") or {}).get(k) for k in
               ("mean", "std", "ir", "t_stat", "t_stat_nw", "positive_rate",
                "ic_gt_002_rate", "ic_autocorr", "n_days")},
        "rank_ic": {k: (ic.get("rank_ic") or {}).get(k) for k in
                    ("mean", "std", "ir", "t_stat", "t_stat_nw", "positive_rate",
                     "ic_gt_002_rate", "ic_autocorr", "n_days")},
        "quantile": {"monotonicity": qs.get("monotonicity"),
                     "top_bottom_spread": qs.get("top_bottom_spread"),
                     "long_short": qs.get("long_short"),
                     "groups": qs.get("groups")},
        "decay": {"half_life": dec.get("half_life"),
                  "suggested_rebalance": dec.get("suggested_rebalance"),
                  "profile": dec["profile"].to_dicts() if len(dec.get("profile")) else []},
        "attribution": attr,
        "oos_decay": oos,
        "n_trials": q["n_trials"],
        "corrected_threshold": q["corrected_threshold"],
        "quota_remaining": q["quota_remaining"],
        "hints": q["hints"],
    }), ensure_ascii=False))


@factor.command()
@click.argument("expr")
@click.option("--start", default=None, help="数据窗口起点 YYYY-MM-DD")
@click.option("--n-groups", default=10, help="分层组数（剔除最佳月份用）")
@click.option("--top-months", default=5, help="剔除收益最好的前 N 个月")
@click.option("--deltas", default="0.1,0.2,0.3", help="窗口扰动幅度（逗号分隔）")
@click.option("--agent", default=None, help="Agent 名（配额记账 + 校正门槛）")
def robust(expr: str, start: str | None, n_groups: int, top_months: int,
           deltas: str, agent: str | None) -> None:
    """L3 鲁棒性检验：窗口扰动 / 分段稳定 / 起点敏感 / 剔除最佳月份 / 样本外衰减。

    通过 ≠ 因子好，只说明「它不是因为某个特定窗口或某一段行情才成立」。
    任一关键项失败 → 下一轮别调参数，换字段族（门禁的提示会指方向）。
    """
    import json

    from lquant.factors.evaluate import ic_summary, robustness_summary

    seg, cov_cols, days = _load_segments(start, expr, horizons=[1])
    train, val = seg["train"], seg["val"]
    if not len(train):
        raise click.ClickException("train 段为空 —— 数据或表达式问题")

    icir_is = _icir(ic_summary(train, "f", "fwd_ret_1"))
    icir_oos = _icir(ic_summary(val, "f", "fwd_ret_1")) if len(val) else float("nan")
    q = _quota(agent, expr, float("nan"))

    res = robustness_summary(
        train, "f", "fwd_ret_1", expr=expr, covs=cov_cols,
        icir_is=icir_is, icir_oos=icir_oos,
        deltas=_parse_floats(deltas), top_n=top_months, n_groups=n_groups,
    )
    res.update({"expr": expr, "n_days": days, "neutralized": bool(cov_cols),
                "covariates": cov_cols, "n_trials": q["n_trials"],
                "quota_remaining": q["quota_remaining"], "hints": q["hints"]})
    click.echo(json.dumps(_clean(res), ensure_ascii=False))


@factor.command()
@click.argument("expr")
@click.option("--out", default=None, help="输出 HTML 路径（默认 data/reports/factor_<id>.html）")
@click.option("--start", default=None, help="数据窗口起点 YYYY-MM-DD")
@click.option("--n-groups", default=10, help="分层组数")
@click.option("--bps", default="0,5,10,15,30", help="成本敏感性 bps 列表（逗号分隔）")
def report(expr: str, out: str | None, start: str | None, n_groups: int, bps: str) -> None:
    """生成自包含 HTML 因子研究报告（离线可看，涨红跌绿）。

    报告含 IC/滚动/分层/衰减/分年度/归因/换手/成本敏感性 ——
    单文件、无外部依赖，可直接归档或发出。
    """
    import json
    from pathlib import Path

    from lquant.factors.dsl.printer import canonical_id
    from lquant.factors.evaluate import factor_report, save_report

    seg, cov_cols, days = _load_segments(start, expr, horizons=[1, 5, 10, 20])
    train = seg["train"]
    if not len(train):
        raise click.ClickException("train 段为空 —— 数据或表达式问题")

    cat = next((c for c in ("cov_industry_sw1", "industry_sw1") if c in train.columns), None)
    html = factor_report(train, "f", "fwd_ret_1", n_groups=n_groups,
                         cat_col=cat, group_col=None,
                         bps_list=list(_parse_floats(bps)))
    path = Path(out) if out else Path("data/reports") / f"factor_{canonical_id(expr)}.html"
    p = save_report(html, path)
    click.echo(json.dumps(_clean({
        "report": str(p.resolve()), "bytes": p.stat().st_size,
        "factor": expr, "factor_id": canonical_id(expr), "n_days": days,
        "neutralized": bool(cov_cols), "covariates": cov_cols,
    }), ensure_ascii=False))
