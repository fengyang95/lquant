"""lq etf：ETF 专属因子的操作面（池 / 因子清单 / 评价 / 回测）。

与 ``lq factor`` 的分工：
- ``lq factor`` 面向**个股**（股票池 + 市值/行业中性化）；
- ``lq etf`` 面向 **ETF/LOF 池**（基金类型分组 + ETF 规模中性化 + 自动套用 ETF 交易规则）。

数字一律由平台算，stdout 只出严格合法 JSON（NaN/Inf 收成 null），
人读的提示走 stderr —— 与 ``lq factor`` 同一纪律。
"""
from __future__ import annotations

import json
import math

import click

from lquant.factors.evaluate.defaults import DEFAULT_N_GROUPS


@click.group()
def etf() -> None:
    """ETF 专属因子（池 / 因子 / 评价 / 回测）"""


def _clean(obj):
    """把 NaN/Inf/date 收成 JSON 原生 —— 让管道解析端拿到的是严格合法 JSON。

    回测 ``metrics`` 里带 ``date``（回测起止/基准区间），``json.dumps`` 默认会炸；
    这里统一转 ISO 字符串，比在 dumps 上挂 ``default=str`` 更可控（后者会把
    意外对象也静默字符串化，掩盖真问题）。
    """
    import datetime as _dt

    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, (_dt.date, _dt.datetime)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (str, int, bool)) or obj is None:
        return obj
    return str(obj)


@etf.command("universes")
def universes() -> None:
    """列出 ETF 标的池。"""
    from lquant.factors.etf import etf_universe_options

    click.echo(json.dumps(
        [{"key": k, "fund_type": ft, "label": label} for k, ft, label in etf_universe_options()],
        ensure_ascii=False))


@etf.command("factors")
def factors() -> None:
    """列出 ETF 因子清单（config/factors/etf.yaml，逐条过 DSL 校验）。"""
    from lquant.factors.etf import load_etf_factors

    rows = load_etf_factors()
    click.echo(json.dumps(
        [
            {
                "name": r["name"],
                "expression": r["expression"],
                "category": r["category"],
                "description": r["description"],
                "min_window": r["min_window"],
            }
            for r in rows
        ],
        ensure_ascii=False))


@etf.command("eval")
@click.argument("expr")
@click.option("--universe", default="etf_all", help="ETF 池：etf_all/etf_equity/etf_qdii/etf_bond/etf_commodity/etf_money")
@click.option("--start", default=None, help="数据窗口起点 YYYY-MM-DD")
@click.option("--end", default=None, help="数据窗口终点 YYYY-MM-DD")
@click.option("--n-groups", default=DEFAULT_N_GROUPS, help="分层组数")
def eval_(expr: str, universe: str, start: str | None, end: str | None, n_groups: int) -> None:
    """ETF 因子评价：IC/ICIR + 分层 + 衰减 + 归因 + 评级（ETF 口径）。"""
    from lquant.factors.etf import evaluate_etf

    try:
        res = evaluate_etf(
            expr, universe=universe, start=start, end=end, n_groups=n_groups,
            with_report=False,
        )
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    res.pop("report", None)
    click.echo(json.dumps(_clean(res), ensure_ascii=False))


@etf.command("audit")
@click.argument("expr")
@click.option("--universe", default="etf_all")
@click.option("--start", default=None)
@click.option("--end", default=None)
@click.option("--n-groups", default=DEFAULT_N_GROUPS)
def audit(expr: str, universe: str, start: str | None, end: str | None, n_groups: int) -> None:
    """ETF 因子深度校验：在 eval 之上加 L3 稳健性（窗口扰动/分段/月度剔除）。"""
    from lquant.factors.etf import evaluate_etf

    try:
        res = evaluate_etf(
            expr, universe=universe, start=start, end=end, n_groups=n_groups,
            with_report=False, with_robustness=True,
        )
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    res.pop("report", None)
    click.echo(json.dumps(_clean(res), ensure_ascii=False))


@etf.command("report")
@click.argument("expr")
@click.option("--universe", default="etf_all")
@click.option("--start", default=None)
@click.option("--end", default=None)
@click.option("--n-groups", default=DEFAULT_N_GROUPS)
@click.option("--out", default=None, help="输出 HTML 路径（默认 <reports>/etf_factor_<id>.html）")
def report(
    expr: str, universe: str, start: str | None, end: str | None,
    n_groups: int, out: str | None,
) -> None:
    """生成自包含 HTML 因子研究报告（ETF 口径，涨红跌绿）。"""
    from pathlib import Path

    from lquant.core.config import get_settings
    from lquant.factors.dsl.printer import canonical_id
    from lquant.factors.etf import evaluate_etf

    if out:
        path = Path(out)
    else:
        s = get_settings()
        base = Path(getattr(s, "reports_dir", "./data/reports"))
        base = base if base.is_absolute() else (s.root / base)
        path = base / f"etf_factor_{canonical_id(expr)}.html"
    try:
        res = evaluate_etf(
            expr, universe=universe, start=start, end=end, n_groups=n_groups,
            with_report=True, out=path,
        )
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    click.echo(json.dumps(_clean({
        "report": res.get("report_path"),
        "factor": expr,
        "universe": universe,
        "rating": (res.get("rating") or {}).get("rating"),
        "etf": res.get("etf"),
    }), ensure_ascii=False))


@etf.command("backtest")
@click.option("--factor", "formula", required=True, help="因子 DSL 表达式（如 Ts_Return($close,20)）")
@click.option("--universe", default="etf_all", help="ETF 池")
@click.option("--start", default=None)
@click.option("--end", default=None)
@click.option("--top-n", default=10, help="持仓只数")
@click.option("--rebalance", default="monthly",
              type=click.Choice(["daily", "weekly", "monthly", "none"]))
@click.option("--initial-cash", default=1_000_000.0)
@click.option("--benchmark", default=None, help="基准指数（缺省不挂）")
@click.option("--exit-strategy", "exit_strategy", default=None, help="叠加退出策略名")
def backtest(
    formula: str, universe: str, start: str | None, end: str | None, top_n: int,
    rebalance: str, initial_cash: float, benchmark: str | None, exit_strategy: str | None,
) -> None:
    """ETF 因子 TopN 等权回测（自动套用 ETF 交易规则：免印花税/过户费、T+N）。"""
    from lquant.backtest.etf import etf_factor_backtest

    try:
        res, meta = etf_factor_backtest(
            formula, universe=universe, start=start, end=end, top_n=top_n,
            rebalance=rebalance, initial_cash=initial_cash, benchmark=benchmark,
            exit_strategy=exit_strategy,
        )
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    except KeyError as e:      # 未注册的退出策略名
        raise click.ClickException(f"未注册的退出策略: {e}") from e
    click.echo(json.dumps(_clean({
        "formula": formula,
        "universe": universe,
        "n_symbols": meta["n_symbols"],
        "top_n": top_n,
        "rebalance": rebalance,
        "metrics": res.metrics,
        "n_nav_points": len(res.nav),
        "n_trades": res.metrics.get("n_trades", 0),
    }), ensure_ascii=False))
