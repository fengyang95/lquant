"""lq factor：check / eval / submit —— Agent 的操作面（方案 6.2）。

CLI stderr 带结构化淘汰原因码 —— Agent 读错误即自我修正。
"""

from __future__ import annotations

import math

import click

# 平台级默认口径的唯一来源（见 lquant.factors.evaluate.defaults 与
# docs/因子报告内容契约.md）：CLI / API / 报告三处默认值必须同源，
# 否则「同一个生成器产出三种报告」。
from lquant.factors.evaluate.defaults import DEFAULT_BPS, DEFAULT_N_GROUPS, horizons_csv

DECAY_HORIZONS_CSV = horizons_csv()
BPS_CSV = ",".join(str(int(b)) if float(b).is_integer() else str(b) for b in DEFAULT_BPS)


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
    """G0 静态校验（毫秒，永远第一步）。退出码非 0 = 校验失败。

    失败时结构化原因码**同时**写 stderr（方案 6.2 契约：Agent 读 stderr 即自我修正），
    stdout 保持一份完整 JSON 供管道解析。
    """
    import json
    import sys

    from lquant.factors.mining.gates import g0_static
    from lquant.factors.mining.submit import _daily_fields

    r = g0_static(expr, allowed_fields=_daily_fields())
    payload = {"passed": r.passed, "stage": r.stage, "reason_code": r.reason_code, "hint": r.hint}
    line = json.dumps(payload, ensure_ascii=False)
    click.echo(line)
    if not r.passed:
        click.echo(line, err=True)
        sys.exit(1)


@factor.command("eval")
@click.argument("expr")
@click.option("--start", default=None, help="数据窗口起点 YYYY-MM-DD")
@click.option("--neutral/--raw", default=True, help="是否中性化（默认中性化）")
@click.option("--n-groups", default=DEFAULT_N_GROUPS, help="分层组数")
@click.option("--agent", default=None, help="Agent 名（配额记账 + 校正门槛）")
def eval_(expr: str, start: str | None, neutral: bool, n_groups: int, agent: str | None) -> None:
    """L1 快筛：IC/ICIR + 分层 + 换手 + 中性化对照 + 校正门槛/配额（方案 6.2/6.3）。

    train 段（前 70%）上一次算完，与 audit/submit 共用 ``prepare_segment``，
    口径不允许分叉。
    """
    import json

    import polars as pl

    from lquant.factors.evaluate.ic import _summarize, ic_series
    from lquant.factors.mining.fitness import corrected_threshold
    from lquant.factors.mining.runner import split_dates
    from lquant.factors.mining.submit import _panel_with_covs, prepare_segment

    df, cov_cols = _panel_with_covs(start=start)
    if not len(df):
        raise click.ClickException("日线数据为空，先跑 bootstrap 或 lq data demo")
    dates = sorted(df["trade_date"].unique().to_list())
    tr_d, _val_d, _test_d = split_dates(dates)
    train = prepare_segment(df, cov_cols, expr, tr_d)
    try:
        s_tr = ic_series(train, "f", "fwd_ret_1")
    except Exception:  # noqa: BLE001 - 帧不可用（缺列/空帧）统一收敛成同一条出口
        s_tr = None
    if s_tr is None or not len(s_tr):
        raise click.ClickException("train 段 IC 序列为空 —— 数据或表达式问题")
    st = _summarize(s_tr["ic"])
    sr = _summarize(s_tr["rank_ic"])
    ic = st["mean"]
    t = st["t_stat"]

    # 分层 + 换手：同一 train 帧上算，失败不阻断快筛（但要在 errors 里可见）
    errors: dict[str, str] = {}
    quant: dict = {}
    annual_turnover = None
    try:
        from lquant.factors.evaluate.quantile import quantile_summary

        qs = quantile_summary(train, "f", "fwd_ret_1", n_groups)
        quant = {
            "n_groups": n_groups,
            "monotonicity": _clean_num(qs.get("monotonicity")),
            "top_bottom_spread": _clean_num(qs.get("top_bottom_spread")),
            "long_short": {k: _clean_num(v) for k, v in (qs.get("long_short") or {}).items()},
        }
    except Exception as e:  # noqa: BLE001
        errors["quantile"] = f"{type(e).__name__}: {e}"
    try:
        from lquant.factors.evaluate.costs import factor_turnover

        to_df = factor_turnover(train, "f", n_groups)
        mean_to = to_df["turnover_avg"].drop_nulls().mean() if len(to_df) else None
        annual_turnover = round(float(mean_to) * 252, 2) if mean_to is not None else None
    except Exception as e:  # noqa: BLE001
        errors["turnover"] = f"{type(e).__name__}: {e}"

    # 中性化对照：同口径再算一遍 raw IC
    ic_raw = None
    if cov_cols:
        from lquant.factors.analysis import compute_factor_col
        from lquant.factors.evaluate import forward_return as _fr

        raw_sub = df.filter(pl.col("trade_date").is_in(tr_d))
        raw_sub = _fr(raw_sub.sort(["symbol", "trade_date"]), "close", periods=[1])
        d_raw = compute_factor_col(raw_sub, expr, "f").drop_nulls(["f", "fwd_ret_1"])
        raw_ic_s = ic_series(d_raw, "f", "fwd_ret_1")
        if len(raw_ic_s):
            ic_raw = round(float(raw_ic_s["ic"].mean()), 4)
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
            ensure_quota(agent, 1)  # 先判后记：超配额时不该再计数
        except ValueError as e:
            raise click.ClickException(str(e)) from e
        n_trials = record_eval(agent)
        remaining = quota_remaining(agent)
        thr = corrected_threshold(max(n_trials, 2))
        if abs(t) < thr:
            hints.append(f"|t|={abs(t):.2f} 低于校正门槛 {thr:.2f}（n_trials={n_trials}）")
        hints.append(f"剩余配额 {remaining} 次")
    click.echo(
        json.dumps(
            _clean(
                {
                    "ic_mean": round(ic, 4),
                    "rank_ic_mean": round(sr["mean"], 4),
                    "icir": _clean_num(st["ir"]),
                    "rank_icir": _clean_num(sr["ir"]),
                    "t_stat": round(t, 2) if t is not None and math.isfinite(t) else None,
                    "t_stat_nw": _clean_num(st.get("t_stat_nw")),
                    "positive_rate": _clean_num(st.get("positive_rate")),
                    "ic_autocorr": _clean_num(st.get("ic_autocorr")),
                    "n_days": len(s_tr),
                    "quantile": quant,
                    "annual_turnover": annual_turnover,
                    "ic_raw_mean": ic_raw,
                    "neutralized": bool(cov_cols),
                    "n_trials": n_trials,
                    "corrected_threshold": round(thr, 2) if thr else None,
                    "quota_remaining": remaining,
                    "hints": hints,
                    "errors": errors,
                }
            ),
            ensure_ascii=False,
        )
    )


def _clean_num(v):
    """非有限值 → None（CLI JSON 里不能出现 NaN 字面量，Agent 的 json.loads 会炸）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


@factor.command()
@click.argument("spec_path", type=click.Path(exists=True))
def submit(spec_path: str) -> None:
    """★ 唯一入库通道：submit 即重验（M4c）—— 服务端重跑 G0-G3，不信任任何自报数字。"""
    import json
    import sys
    from pathlib import Path

    import yaml

    spec = yaml.safe_load(Path(spec_path).read_text())
    if not isinstance(spec, dict) or not spec.get("expr"):
        raise click.ClickException(
            "spec 结构非法：需要 YAML mapping 且包含 expr 字段（如 {expr: ..., rationale: ...}）"
        )
    from lquant.factors.mining.submit import verify_and_register

    ok, payload = verify_and_register(spec)
    click.echo(json.dumps(payload, ensure_ascii=False, default=str))
    sys.exit(0 if ok else 1)


@factor.command()
@click.option("--name", required=True, help="已注册因子名（factor_def.name）")
@click.option("--start", default=None, help="数据窗口起点 YYYY-MM-DD")
@click.option("--n-groups", default=DEFAULT_N_GROUPS, help="分层组数")
@click.option("--horizons", default=DECAY_HORIZONS_CSV, help="衰减曲线持有期（逗号分隔）")
@click.option("--agent", default=None, help="Agent 名（配额记账 + 校正门槛）")
def run(name: str, start: str | None, n_groups: int, horizons: str, agent: str | None) -> None:
    """按注册名跑 L2 深度校验（从 factor_def 取表达式，与 ``audit`` 同一实现）。

    此前是空壳（只 echo 一行），命令名暗示能算却没有计算也没有入库。
    """
    import json

    from lquant.core.db import reader

    with reader() as con:
        row = con.execute("SELECT expression FROM factor_def WHERE name = ?", [name]).fetchone()
    if not row or not row[0]:
        raise click.ClickException(f"未注册的因子或表达式为空: {name}（见 lq factor add）")
    payload = _audit_payload(row[0], start, n_groups, horizons, agent)
    payload["name"] = name
    click.echo(json.dumps(payload, ensure_ascii=False))


@factor.command()
@click.option("--agent", default="gp-internal")
@click.option("--generator", default="gp", type=click.Choice(["gp", "random", "proposals"]))
@click.option("--n", default=100, help="候选数量（预算）")
@click.option("--proposals", default=None, help="JSONL 提案文件（generator=proposals）")
@click.option("--start", default=None)
def mine(agent: str, generator: str, n: int, proposals: str | None, start: str | None) -> None:
    """平台驱动挖掘会话：G0-G3 门禁 + 记账落 factor_mining_run。"""
    import json

    payload, warning = _mine_session(agent, generator, n, proposals, start)
    if warning:
        click.echo(f"[warn] {warning}")
    click.echo(json.dumps(payload, ensure_ascii=False))


def _mine_session(
    agent: str, generator: str, n: int, proposals: str | None, start: str | None
) -> tuple[dict, str | None]:
    """平台驱动挖掘会话的实现体 —— ``lq factor mine`` 与 ``lq agent run`` 共用。

    返回 (结果载荷, 记账告警或 None)。两条入口共用一套配额账、一套门禁、
    一份台账（方案 6.4 的公共不变量）。
    """
    import json
    import uuid

    from lquant.core.db import writer
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
            '（每行一个 {"expr": "...", "note": "..."} 对象）'
        )

    # 方案红线：G1 快筛与适应度一律用中性化后 IC —— 挖掘会话必须带协变量。
    # 只读一次面板：此前 read_daily 判空 + _panel_with_covs 各翻一遍湖，
    # 全量数据下纯多付一次全湖扫描（实测 2026 年分区 1.3s/次，无 --start 更贵）。
    from lquant.factors.mining.submit import _panel_with_covs

    df, cov_cols = _panel_with_covs(start=start)
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
        from lquant.factors.mining.llm import load_proposals
        from lquant.factors.mining.llm import make_generator as mg

        try:
            gen = mg(load_proposals(proposals))
        except Exception as e:  # noqa: BLE001 - CLI 出口：结构化报错而非 traceback
            raise click.ClickException(f"加载 proposals 失败: {e}") from e

    res, survivors = run_session(eng, df, gen, agent=agent, n_candidates=n, covs=cov_cols)
    # 记账落库
    run_id = uuid.uuid4().hex[:12]
    import datetime as dt

    warning = None
    try:
        with writer() as con:
            con.execute(
                "INSERT OR REPLACE INTO factor_mining_run VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    run_id,
                    agent,
                    generator,
                    res.n_evaluated,
                    res.n_static_fail,
                    res.n_low_ic,
                    res.n_redundant,
                    res.n_size_proxy,
                    res.n_survivors,
                    json.dumps(res.corrections, ensure_ascii=False)[:10000],
                    dt.datetime.now(),
                ],
            )
    except Exception as e:  # noqa: BLE001
        warning = f"记账落库失败（结果仍有效）: {e}"
    return {
        "run_id": run_id,
        "agent": agent,
        "generator": generator,
        "n_evaluated": res.n_evaluated,
        "n_static_fail": res.n_static_fail,
        "n_low_ic": res.n_low_ic,
        "n_redundant": res.n_redundant,
        "n_size_proxy": res.n_size_proxy,
        "n_survivors": res.n_survivors,
        "survivors": survivors[:10],
    }, warning


@factor.command()
@click.argument("report", type=click.Path(exists=True), required=False)
@click.option("--out", default=None, help="提案 JSONL 输出路径（缺省只打印）")
@click.option("--max", "max_proposals", default=10, help="最多保留几条提案")
def propose(report: str | None, out: str | None, max_proposals: int) -> None:
    """研报文本 → LLM 因子提案（RD-Agent 式，需 LQ_LLM_API_KEY）。

    LLM 只产表达式；每条都过 G0 静态门禁，不合格的带原因码进 rejected
    （stderr 可见，不静默）。产出接
    ``lq factor mine --generator proposals --proposals <out>``。
    REPORT 文件路径缺省读 stdin。
    """
    import json
    import sys

    from lquant.research.report_extract import extract_proposals, write_proposals

    text = ""
    if report:
        with open(report, encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    try:
        res = extract_proposals(text, max_proposals=max_proposals)
    except Exception as e:  # noqa: BLE001 - CLI 出口：结构化报错而非 traceback
        raise click.ClickException(f"提取失败: {e}") from e
    for r in res["rejected"]:
        click.echo(f"[rejected] {r['expr']}: {r['reason']}", err=True)
    if not res["accepted"]:
        raise click.ClickException(
            f"没有通过 G0 的提案（{len(res['rejected'])} 条被拒，明细见 stderr）"
        )
    if out:
        write_proposals(res["accepted"], out)
        # stdout 保持结构化 JSON；人类可读的指引一律走 stderr
        click.echo(f"已写 {len(res['accepted'])} 条提案 -> {out}", err=True)
        click.echo(f"下一步: lq factor mine --generator proposals --proposals {out}", err=True)
        click.echo(json.dumps(res, ensure_ascii=False))
    else:
        click.echo(json.dumps(res, ensure_ascii=False))


@factor.command()
@click.argument("expr")
@click.option("--start", default=None)
def series(expr: str, start: str | None) -> None:
    """逐日 IC/RankIC/累计 IC 序列 JSON（图表数据，平台算）。

    train 段（前 70%）上逐日序列；``dates[i]`` 与 ``ic[i]`` / ``rank_ic[i]`` /
    ``cum_ic[i]`` 一一对应。此前只回三个标量，「看什么时候失效」根本做不到。
    """
    import json

    import numpy as np

    from lquant.factors.mining.runner import split_dates
    from lquant.factors.mining.submit import _panel_with_covs, prepare_segment

    df, cov_cols = _panel_with_covs(start=start)
    if not len(df):
        raise click.ClickException("日线数据为空，先跑 bootstrap 或 lq data demo")
    tr_d, _val_d, _test_d = split_dates(sorted(df["trade_date"].unique().to_list()))
    train = prepare_segment(df, cov_cols, expr, tr_d)
    from lquant.factors.evaluate.ic import ic_series

    try:
        s = ic_series(train, "f", "fwd_ret_1")
    except Exception:  # noqa: BLE001 - 帧不可用（缺列/空帧）统一收敛成同一条出口
        s = None
    if s is None or not len(s):
        raise click.ClickException("train 段 IC 序列为空 —— 数据或表达式问题")
    ic = [_clean_num(v) for v in s["ic"].to_list()]
    rank_ic = [_clean_num(v) for v in s["rank_ic"].to_list()]
    cum = np.nancumsum(np.array([v if v is not None else 0.0 for v in ic], dtype=float))
    click.echo(
        json.dumps(
            {
                "expr": expr,
                "n_days": len(s),
                "neutralized": bool(cov_cols),
                "covariates": cov_cols,
                "dates": [str(x) for x in s["trade_date"].to_list()],
                "ic": ic,
                "rank_ic": rank_ic,
                "cum_ic": [round(float(v), 6) for v in cum],
                "ic_mean": _clean_num(s["ic"].mean()),
                "rank_ic_mean": _clean_num(s["rank_ic"].mean()),
            },
            ensure_ascii=False,
        )
    )


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


def _load_segments(start: str | None, expr: str, *, horizons=(1, 5), with_pre: bool = False):
    """读日线 + 协变量 → 70/15/15 切分 → train/val 两段分析就绪面板。

    与 submit 重验共用 ``prepare_segment``，所以 audit 报的 IC 和入库时
    服务端重算的 IC 是同一个数 —— 口径不允许分叉。

    ``with_pre=True`` 额外返回 train 段**中性化之前**的帧（IC 归因阶梯的基线），
    返回 ``(seg, cov_cols, days, pre)``。
    """
    from lquant.factors.mining.runner import split_dates
    from lquant.factors.mining.submit import _panel_with_covs, prepare_segment

    df, cov_cols = _panel_with_covs(start=start)
    if not len(df):
        raise click.ClickException("日线数据为空，先跑 bootstrap 或 lq data demo")
    dates = sorted(df["trade_date"].unique().to_list())
    train_d, val_d, test_d = split_dates(dates)
    train = prepare_segment(df, cov_cols, expr, train_d, horizons=list(horizons), with_pre=with_pre)
    pre = None
    if with_pre:
        train, pre = train
    seg = {
        "train": train,
        "val": prepare_segment(df, cov_cols, expr, val_d, horizons=[1]),
    }
    days = {"train_days": len(train_d), "val_days": len(val_d), "test_days": len(test_d)}
    if with_pre:
        return seg, cov_cols, days, pre
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
        return {"n_trials": None, "corrected_threshold": None, "quota_remaining": None, "hints": []}
    from lquant.factors.agents import (
        ensure_quota,
        find_agent,
        quota_remaining,
        record_eval,
    )

    if not find_agent(agent):
        raise click.ClickException(f"Agent 未注册: {agent}")
    try:
        ensure_quota(agent, 1)  # 先判后记：超配额时不该再计数（与 eval 一致）
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    n_trials = record_eval(agent)
    remaining = quota_remaining(agent)
    thr = corrected_threshold(max(n_trials, 2))
    hints = []
    if abs(t_stat) < thr:
        hints.append(f"|t|={abs(t_stat):.2f} 低于校正门槛 {thr:.2f}（n_trials={n_trials}）")
    hints.append(f"剩余配额 {remaining} 次")
    return {
        "n_trials": n_trials,
        "corrected_threshold": round(thr, 2),
        "quota_remaining": remaining,
        "hints": hints,
    }


@factor.command()
@click.argument("expr")
@click.option("--start", default=None, help="数据窗口起点 YYYY-MM-DD")
@click.option("--n-groups", default=DEFAULT_N_GROUPS, help="分层组数")
@click.option("--horizons", default=DECAY_HORIZONS_CSV, help="衰减曲线持有期（逗号分隔）")
@click.option("--agent", default=None, help="Agent 名（配额记账 + 校正门槛）")
def audit(expr: str, start: str | None, n_groups: int, horizons: str, agent: str | None) -> None:
    """L2 深度校验：IC/ICIR + 分层 + 衰减 + 归因 + 评级 + 样本外衰减（平台算）。

    eval 只回答「有没有信号」，audit 回答「信号长什么样、稳不稳、能不能赚」——
    分级、单调性、半衰期、行业暴露、IS→OOS 衰减一次给全。
    """
    import json

    click.echo(
        json.dumps(_audit_payload(expr, start, n_groups, horizons, agent), ensure_ascii=False)
    )


def _audit_payload(
    expr: str, start: str | None, n_groups: int, horizons: str, agent: str | None
) -> dict:
    """L2 深度校验的载荷构造 —— ``audit`` 与 ``run``（按注册名）共用同一实现，
    保证「按名字跑」和「按表达式跑」拿到的是同一份数字。"""
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
    # val 段 ICIR 须为有限值才做 oos 对比（_icir 缺数据返回 nan，nan is not None 恒真）
    icir_val = _icir(ic_val)
    oos = oos_decay(_icir(ic), icir_val) if len(val) and math.isfinite(icir_val) else None

    cat = next((c for c in ("cov_industry_sw1", "industry_sw1") if c in train.columns), None)
    attr = None
    if cat:
        try:
            a = attribution_summary(train, "f", "fwd_ret_1", cat_col=cat)
            exp = a["industry_exposure"]
            attr = {
                "by": cat,
                "gross_exposure": a["gross_exposure"],
                "industry_exposure": (
                    sorted(exp.to_dicts(), key=lambda r: -abs(r["exposure"]))[:10]
                    if len(exp)
                    else []
                ),
            }
        except Exception as e:  # noqa: BLE001
            # 归因失败必须可见：静默成 null 会被读成「这个因子没有行业暴露」
            attr = {"by": cat, "error": f"{type(e).__name__}: {e}"}

    return _clean(
        {
            "factor": expr,
            "factor_id": canonical_id(expr),
            "n_days": days,
            "neutralized": bool(cov_cols),
            "covariates": cov_cols,
            "rating": rating,
            "ic": {
                k: (ic.get("ic") or {}).get(k)
                for k in (
                    "mean",
                    "std",
                    "ir",
                    "t_stat",
                    "t_stat_nw",
                    "positive_rate",
                    "ic_gt_002_rate",
                    "ic_autocorr",
                    "n_days",
                )
            },
            "rank_ic": {
                k: (ic.get("rank_ic") or {}).get(k)
                for k in (
                    "mean",
                    "std",
                    "ir",
                    "t_stat",
                    "t_stat_nw",
                    "positive_rate",
                    "ic_gt_002_rate",
                    "ic_autocorr",
                    "n_days",
                )
            },
            "quantile": {
                "monotonicity": qs.get("monotonicity"),
                "top_bottom_spread": qs.get("top_bottom_spread"),
                "long_short": qs.get("long_short"),
                "groups": qs.get("groups"),
            },
            "decay": {
                "half_life": dec.get("half_life"),
                "suggested_rebalance": dec.get("suggested_rebalance"),
                "profile": dec["profile"].to_dicts() if len(dec.get("profile")) else [],
            },
            "attribution": attr,
            "oos_decay": oos,
            "n_trials": q["n_trials"],
            "corrected_threshold": q["corrected_threshold"],
            "quota_remaining": q["quota_remaining"],
            "hints": q["hints"],
        }
    )


@factor.command()
@click.argument("expr")
@click.option("--start", default=None, help="数据窗口起点 YYYY-MM-DD")
@click.option("--n-groups", default=DEFAULT_N_GROUPS, help="分层组数（剔除最佳月份用）")
@click.option("--top-months", default=5, help="剔除收益最好的前 N 个月")
@click.option("--deltas", default="0.1,0.2,0.3", help="窗口扰动幅度（逗号分隔）")
@click.option("--agent", default=None, help="Agent 名（配额记账 + 校正门槛）")
def robust(
    expr: str, start: str | None, n_groups: int, top_months: int, deltas: str, agent: str | None
) -> None:
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
        train,
        "f",
        "fwd_ret_1",
        expr=expr,
        covs=cov_cols,
        icir_is=icir_is,
        icir_oos=icir_oos,
        deltas=_parse_floats(deltas),
        top_n=top_months,
        n_groups=n_groups,
    )
    res.update(
        {
            "expr": expr,
            "n_days": days,
            "neutralized": bool(cov_cols),
            "covariates": cov_cols,
            "n_trials": q["n_trials"],
            "quota_remaining": q["quota_remaining"],
            "hints": q["hints"],
        }
    )
    click.echo(json.dumps(_clean(res), ensure_ascii=False))


@factor.command()
@click.argument("expr")
@click.option(
    "--out", default=None, help="输出 HTML 路径（默认 <仓库根>/data/reports/factor_<id>.html）"
)
@click.option("--start", default=None, help="数据窗口起点 YYYY-MM-DD")
@click.option("--n-groups", default=DEFAULT_N_GROUPS, help="分层组数")
@click.option("--bps", default=BPS_CSV, help="成本敏感性 bps 列表（逗号分隔）")
@click.option(
    "--filter-zscore",
    default=None,
    type=float,
    help="截面异常收益过滤阈值（|z| 上限，口径同 alphalens；默认不过滤）",
)
@click.option(
    "--exclude-st",
    is_flag=True,
    default=False,
    help="剔除 ST/*ST（默认否 —— 打开会改变 IC/分层口径）",
)
@click.option("--exclude-suspended", is_flag=True, default=False, help="剔除停牌（默认否）")
def report(
    expr: str,
    out: str | None,
    start: str | None,
    n_groups: int,
    bps: str,
    filter_zscore: float | None,
    exclude_st: bool,
    exclude_suspended: bool,
) -> None:
    """生成自包含 HTML 因子研究报告（离线可看，涨红跌绿）。

    报告含「结论（评级）」+「样本与口径」+ IC/滚动/分层/超额/衰减/分年度/
    归因/分组 IC/换手/成本/容量 —— 单文件、无外部依赖，可直接归档或发出。
    """
    import json

    payload = _build_report(
        expr,
        out=out,
        start=start,
        n_groups=n_groups,
        bps=bps,
        filter_zscore=filter_zscore,
        exclude_st=exclude_st,
        exclude_suspended=exclude_suspended,
    )
    click.echo(json.dumps(payload, ensure_ascii=False))


def _build_report(
    expr: str,
    *,
    out: str | None = None,
    start: str | None = None,
    n_groups: int = DEFAULT_N_GROUPS,
    bps: str = BPS_CSV,
    filter_zscore: float | None = None,
    exclude_st: bool = False,
    exclude_suspended: bool = False,
) -> dict:
    """生成一份报告并返回摘要 dict（``report`` 与 ``reports --rebuild-stale`` 共用）。"""
    from pathlib import Path

    from lquant.core.config import get_settings
    from lquant.factors.dsl.printer import canonical_id
    from lquant.factors.evaluate import factor_report, save_report
    from lquant.factors.evaluate.capacity import capacity_summary
    from lquant.factors.evaluate.defaults import DEFAULT_DECAY_HORIZONS
    from lquant.factors.evaluate.extras import build_report_extras
    from lquant.factors.evaluate.ic import ic_summary
    from lquant.factors.evaluate.quantile import quantile_summary
    from lquant.factors.evaluate.rating import factor_rating
    from lquant.factors.evaluate.sample import apply_sample_filters, describe_sample_filters

    # 衰减阶梯与 API/报告默认同源；with_pre 拿中性化之前的帧做 IC 归因阶梯的基线。
    seg, cov_cols, days, pre_recipe = _load_segments(
        start, expr, horizons=list(DEFAULT_DECAY_HORIZONS), with_pre=True
    )
    train = seg["train"]
    if not len(train):
        raise click.ClickException("train 段为空 —— 数据或表达式问题")

    # 样本过滤（默认关）：与 API 同口径，报告里会写明到底剔没剔
    if exclude_st or exclude_suspended:
        train = apply_sample_filters(
            train, exclude_st=exclude_st, exclude_suspended=exclude_suspended
        )
        if not len(train):
            raise click.ClickException("样本过滤后没有剩余数据 —— 检查 ST/停牌标记")

    cat = next((c for c in ("cov_industry_sw1", "industry_sw1") if c in train.columns), None)

    # 口头诚实：`prepare_segment` 在有协变量时**确实**跑了默认配方
    # （mad 去极值 → zscore → 市值/行业/换手 OLS 中性化）。报告必须披露真实配方，
    # 否则「预处理配方」一栏会写着「原始因子直接评价」—— 那是假的。
    recipe = (
        [
            {"op": "winsorize", "method": "mad", "n": 5},
            {"op": "standardize", "method": "zscore"},
            {"op": "neutralize", "method": "ols", "factors": list(cov_cols)},
        ]
        if cov_cols
        else None
    )
    cov_map = {
        c.removeprefix("cov_"): round(1 - train[c].null_count() / max(len(train), 1), 4)
        for c in cov_cols
        if c in train.columns
    }

    # 归因阶梯 / 中性化视图 / 分组 IC / 研报三件套 —— 与 API 共用同一份编排，
    # 否则 CLI 生成的报告永远比 API 的薄一截（评审 R12）。
    errors: dict[str, str] = {}
    extras = build_report_extras(
        train,
        "f",
        "fwd_ret_1",
        n_groups=n_groups,
        group_col=cat,
        pre_recipe_df=pre_recipe,
        cov_report=cov_map,
        errors=errors,
    )

    # 结论层：CLI 只跑 L2 评级（不跑 L3 稳健性 —— 要重算因子多遍，太贵）
    rating = None
    try:
        rating = factor_rating(
            ic_summary(train, "f", "fwd_ret_1"), quantile_summary(train, "f", "fwd_ret_1", n_groups)
        )
    except Exception as e:  # noqa: BLE001 - 评级失败不该让整份报告生成不了
        rating = None
        errors["rating"] = f"{type(e).__name__}: {e}"

    capacity = None
    try:
        capacity = capacity_summary(train, "f", "fwd_ret_1", n_groups=n_groups)
    except Exception as e:  # noqa: BLE001 - 无成交额列时容量不可算，报告里标注即可
        capacity = None
        errors["capacity"] = f"{type(e).__name__}: {e}"

    # 分组 IC：按行业分组（有行业列时）。此前硬编码 None，导致报告里
    # 「分组 IC」这一节永远不出现 —— 引擎有能力，接线处丢了参数。
    html = factor_report(
        train,
        "f",
        "fwd_ret_1",
        n_groups=n_groups,
        cat_col=cat,
        group_col=cat,
        bps_list=list(_parse_floats(bps)),
        filter_zscore=filter_zscore,
        display_name=expr,
        expr=expr,
        data_start=start,
        n_samples=len(train),
        steps=recipe,
        covariates=cov_map,
        universe="all",
        sample_filters=describe_sample_filters(
            train, exclude_st=exclude_st, exclude_suspended=exclude_suspended
        ),
        rating=rating,
        errors=errors,
        extras={**extras, "capacity": capacity},
    )
    if out:
        path = Path(out)
    else:
        s = get_settings()
        base = Path(getattr(s, "reports_dir", "./data/reports"))
        base = base if base.is_absolute() else (s.root / base)
        path = base / f"factor_{canonical_id(expr)}.html"
    p = save_report(html, path)
    return _clean(
        {
            "report": str(p.resolve()),
            "bytes": p.stat().st_size,
            "factor": expr,
            "factor_id": canonical_id(expr),
            "n_days": days,
            "neutralized": bool(cov_cols),
            "cov_names": cov_cols,
            "rating": (rating or {}).get("rating"),
            "capacity_aum": (capacity or {}).get("capacity_aum"),
            "recipe": recipe,
            "covariates": cov_map,
            "sections": sorted(k for k in extras),
            "errors": errors,
            "exclude_st": exclude_st,
            "exclude_suspended": exclude_suspended,
            "filter_zscore": filter_zscore,
        }
    )


@factor.command("reports")
@click.option("--stale-only", is_flag=True, default=False, help="只列旧口径报告")
@click.option(
    "--rebuild-stale",
    is_flag=True,
    default=False,
    help="重算旧口径报告（能推断出表达式的才重算，其余跳过并给原因）",
)
@click.option(
    "--prune-stale", is_flag=True, default=False, help="删除旧口径报告（不加 --yes 只演练，不真删）"
)
@click.option("--limit", default=0, type=int, help="最多处理 N 份（0 = 不限）")
@click.option("--yes", is_flag=True, default=False, help="确认删除（--prune-stale 需要）")
@click.option("--start", default=None, help="重算时的数据窗口起点 YYYY-MM-DD")
def reports(
    stale_only: bool,
    rebuild_stale: bool,
    prune_stale: bool,
    limit: int,
    yes: bool,
    start: str | None,
) -> None:
    """报告中心维护：列出 / 重算 / 清理旧口径报告。

    报告文件名不含版本，正文也看不出口径 —— 只有 ``<head>`` 里的生成器版本能证明
    「这份是修复前还是修复后的」。所以旧口径报告必须能被**识别 → 重算或清理**，
    否则读者永远在拿旧结论当新结论（评审 R15 的第二半：失效标记 ≠ 重算）。
    """
    import contextlib
    import json
    from pathlib import Path

    from lquant.core.config import get_settings
    from lquant.factors.evaluate.reports_index import list_reports as _list
    from lquant.factors.evaluate.reports_index import resolve_report_expression

    s = get_settings()
    base = Path(getattr(s, "reports_dir", "./data/reports"))
    base = base if base.is_absolute() else (s.root / base)
    all_rows = _list(base)
    # 三个动作都只针对**旧口径**报告：--rebuild-stale 去重算一份当前口径的报告
    # 是纯浪费（还可能用不同的数据窗口把它改掉）。只有纯列表请求才看得到全部。
    scope_stale = stale_only or rebuild_stale or prune_stale
    rows = [r for r in all_rows if r["stale"]] if scope_stale else all_rows
    if limit:
        rows = rows[:limit]

    out: dict = {
        "dir": str(base),
        "n_reports": len(all_rows),
        "scope": "stale" if scope_stale else "all",
        "n_selected": len(rows),
        "reports": [
            {k: r[k] for k in ("name", "generator_version", "generated_at", "size_kb", "stale")}
            for r in rows
        ],
    }

    if rebuild_stale:
        rebuilt, skipped = [], []
        for r in rows:
            expr, why = resolve_report_expression(r["name"], Path(r["path"]))
            if not expr:
                skipped.append({"name": r["name"], "reason": why})
                continue
            try:
                info = _build_report(expr, out=r["path"], start=start)
                rebuilt.append(
                    {
                        "name": r["name"],
                        "expr": expr,
                        "why": why,
                        "version": "current",
                        "bytes": info["bytes"],
                    }
                )
            except Exception as e:  # noqa: BLE001 - 单份失败不阻断整批
                skipped.append({"name": r["name"], "reason": f"{type(e).__name__}: {e}"})
        out["rebuilt"] = rebuilt
        out["skipped"] = skipped
    elif prune_stale:
        out["prune"] = {
            "n": len(rows),
            "names": [r["name"] for r in rows],
            "dry_run": not yes,
        }
        if yes:
            for r in rows:
                with contextlib.suppress(OSError):
                    Path(r["path"]).unlink()
            out["prune"]["deleted"] = len(rows)
    elif stale_only:
        out["hint"] = (
            "旧口径报告不会自动失效：--rebuild-stale 重算（需能推断表达式），"
            "--prune-stale --yes 删除"
        )
    click.echo(json.dumps(_clean(out), ensure_ascii=False))
