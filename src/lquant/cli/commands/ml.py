"""lq ml：模型注册表 / 训练 / 滚动重训 / 推理的运维入口。

与 API（``/api/ml/*``）共用同一套 ``research.ml`` 实现 —— CLI 不另写逻辑，
否则「网页上跑的」和「命令行跑的」会慢慢变成两个东西。

    lq ml status                      # 后端 / 版本 / 线上版本 / 信号覆盖
    lq ml features --universe hs300   # 可用特征清单
    lq ml train --features pct_change_20 --train-end 2025-06-30 \
                --valid-end 2025-12-31 --name mom_line
    lq ml retrain --features MA20,MA60 --name mom_line --start 2020-01-01
    lq ml models [--name mom_line]
    lq ml promote mom_line 3 [--stage production]
    lq ml rollback mom_line
    lq ml production mom_line [--asof 2026-09-30T15:00:00]
    lq ml predict --name mom_line --features MA20 --date 2026-09-30
    lq ml signals --name mom_line [--start ... --end ...]
"""
from __future__ import annotations

import json

import click

from lquant.research.ml import registry
from lquant.research.ml.online import OnlineConfig, daily_inference, load_signals, rolling_retrain
from lquant.research.ml.panel import available_features


def _echo(obj) -> None:
    click.echo(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _load(start: str, end: str | None, universe: str):
    from lquant.data.store.parquet import read_daily
    from lquant.factors.universe import resolve_index_code

    symbols = None
    if universe and universe != "all":
        from lquant.data.store.catalog import IndexConsRepo

        code = resolve_index_code(universe)
        symbols = IndexConsRepo().latest_symbols(code)
        if not symbols:
            raise SystemExit(f"指数 {code} 成分股为空，先跑 `lq data index-cons`")
    df = read_daily(start=start, end=end, symbols=symbols).collect()
    if not len(df):
        raise SystemExit("日线数据为空，先跑 bootstrap 或 `lq data demo`")
    return df


def _features(s: str) -> list[str]:
    out = [x.strip() for x in s.split(",") if x.strip()]
    if not out:
        raise SystemExit("--features 不能为空（逗号分隔，如 MA20,pct_change_20）")
    return out


@click.group()
def ml() -> None:
    """ML 模型：训练 / 滚动重训 / 版本管理 / 推理"""


@ml.command()
def status() -> None:
    """ML 子系统状态：可用后端、版本统计、线上版本、信号覆盖。"""
    from lquant.research.ml.model import available_backends

    models = registry.list_models()
    names = sorted({m.name for m in models})
    click.echo(f"  后端: {', '.join(available_backends()) or '（无可用后端）'}")
    click.echo(f"  模型线: {len(names)}  版本: {len(models)}"
               f"  candidate={sum(1 for m in models if m.stage == 'candidate')}"
               f"  production={sum(1 for m in models if m.stage == 'production')}")
    for n in names:
        p = registry.production(n)
        if p is not None:
            ic = (p.metrics or {}).get("ml", {}).get("test_rank_ic_mean")
            click.echo(f"    线上 {n} v{p.version}"
                       + (f"  rank_ic={ic:.4f}" if isinstance(ic, (int, float)) else ""))
    click.echo(f"  模型目录: {registry.model_root()}")
    if not names:
        click.echo("  （还没有任何版本，跑 `lq ml train ...`）")


@ml.command()
@click.option("--universe", default="all", help="股票池（all 或指数别名 hs300/zz500）")
@click.option("--start", default="2024-01-01")
@click.option("--end", default=None)
@click.option("--limit", default=200, type=int)
def features(universe: str, start: str, end: str | None, limit: int) -> None:
    """可用特征清单（湖列 + Alpha158 内置 + 简单公式）。"""
    try:
        df = _load(start, end, universe)
        out = available_features(df, limit=limit)
    except SystemExit:
        out = available_features(None, limit=limit)
    click.echo(f"  湖列（前 {len(out['columns'])}）: {', '.join(out['columns'][:30])}"
               + (" ..." if len(out["columns"]) > 30 else ""))
    click.echo(f"  Alpha158 内置: {out['alpha158_count']} 个"
               f"（示例 {', '.join(out['alpha158'][:10])} ...）")
    click.echo(f"  简单公式: {', '.join(out['formulas'])}")


@ml.command()
@click.option("--features", "features_s", required=True,
              help="逗号分隔特征名（湖列 / Alpha158 内置 / pct_change_N）")
@click.option("--train-end", required=True, help="训练段右端点 YYYY-MM-DD")
@click.option("--valid-end", required=True, help="验证段右端点 YYYY-MM-DD")
@click.option("--name", "model_name", default=None, help="逻辑模型线名（版本流分组键）")
@click.option("--start", default="2024-01-01")
@click.option("--end", default=None)
@click.option("--universe", default="all")
@click.option("--horizon", "label_horizon", default=5, type=int)
@click.option("--kind", default="auto", help="模型后端 auto/lightgbm/gbrt/ridge")
@click.option("--top-n", default=30, type=int)
@click.option("--processor", "processors", multiple=True,
              help='处理器声明 JSON，如 \'{"kind":"standardize"}\'（可多次）')
@click.option("--no-record", is_flag=True, help="只训练不注册版本（快速试验）")
def train(features_s: str, train_end: str, valid_end: str, model_name: str | None,
          start: str, end: str | None, universe: str, label_horizon: int,
          kind: str, top_n: int, processors: tuple[str, ...], no_record: bool) -> None:
    """一次性训练 + 注册模型版本。"""
    from lquant.research.ml.backtest import run_ml_pipeline

    procs = [json.loads(p) for p in processors] if processors else None
    df = _load(start, end, universe)
    out = run_ml_pipeline(
        df, _features(features_s), label_horizon=label_horizon,
        train_end=train_end, valid_end=valid_end, kind=kind, top_n=top_n,
        processors=procs, model_name=model_name, record=not no_record,
        note="cli: lq ml train")
    _echo({"ml_run_id": out.get("ml_run_id"), "model": out.get("model"),
           "ml": out.get("ml"), "dataset": out.get("dataset"),
           "backtest": out.get("backtest")})


@ml.command()
@click.option("--features", "features_s", required=True, help="逗号分隔特征名")
@click.option("--name", "model_name", required=True, help="逻辑模型线名")
@click.option("--start", default="2020-01-01")
@click.option("--end", default=None)
@click.option("--universe", default="all")
@click.option("--horizon", "label_horizon", default=5, type=int)
@click.option("--kind", default="auto")
@click.option("--top-n", default=30, type=int)
@click.option("--train-months", default=24, type=int)
@click.option("--valid-months", default=6, type=int)
@click.option("--test-months", default=6, type=int)
@click.option("--step-months", default=6, type=int)
@click.option("--no-promote", is_flag=True, help="只产出候选版本，不自动晋级")
@click.option("--min-improvement", default=0.0, type=float,
              help="新版本要超过线上版本多少才晋级")
@click.option("--processor", "processors", multiple=True, help="处理器声明 JSON（可多次）")
def retrain(features_s: str, model_name: str, start: str, end: str | None,
            universe: str, label_horizon: int, kind: str, top_n: int,
            train_months: int, valid_months: int, test_months: int,
            step_months: int, no_promote: bool, min_improvement: float,
            processors: tuple[str, ...]) -> None:
    """滚动重训：逐窗口训练 + 先验证再晋级（失败保持原线上版）。"""
    procs = [json.loads(p) for p in processors] if processors else None
    df = _load(start, end, universe)
    cfg = OnlineConfig(name=model_name, features=_features(features_s),
                       label_horizon=label_horizon, kind=kind,
                       processors=procs, top_n=top_n,
                       train_months=train_months, valid_months=valid_months,
                       test_months=test_months, step_months=step_months,
                       min_improvement=min_improvement)
    out = rolling_retrain(df, cfg, promote=not no_promote)
    _echo(out)


@ml.command()
@click.option("--name", "model_name", default=None)
@click.option("--stage", default=None, help="candidate/staging/production/archived")
def models(model_name: str | None, stage: str | None) -> None:
    """列模型版本。"""
    rows = registry.list_models(model_name, stage)
    if not rows:
        click.echo("  （无匹配版本）")
        return
    for m in rows:
        ic = (m.metrics or {}).get("ml", {}).get("test_rank_ic_mean")
        click.echo(f"  {m.name} v{m.version:<3} {m.stage:<10}"
                   + (f" rank_ic={ic:.4f}" if isinstance(ic, (int, float)) else "")
                   + f"  {m.artifact_path}")


@ml.command()
@click.argument("name")
@click.argument("version", type=int)
@click.option("--stage", default="production")
@click.option("--note", default=None)
def promote(name: str, version: int, stage: str, note: str | None) -> None:
    """把某版本置为目标 stage（晋 production 时旧线上版自动 archived）。"""
    _echo(registry.promote(name, version, stage, note=note or "cli: promote").as_dict())


@ml.command()
@click.argument("name")
@click.option("--note", default=None)
def rollback(name: str, note: str | None) -> None:
    """回滚到上一版线上模型。"""
    mv = registry.rollback(name, note=note or "cli: rollback")
    if mv is None:
        raise SystemExit(f"模型线 {name} 没有可回滚的历史线上版本")
    _echo(mv.as_dict())


@ml.command()
@click.argument("name")
@click.option("--asof", default=None, help="ISO 时刻；给了就重放事件流回答当时在线的版本")
def production(name: str, asof: str | None) -> None:
    """当前线上版本（--asof 时为「当时」的线上版本）。"""
    mv = registry.production_asof(name, asof) if asof else registry.production(name)
    if mv is None:
        raise SystemExit(f"模型线 {name} 没有线上版本"
                         + ("（或该时刻尚无线上版本）" if asof else ""))
    _echo(mv.as_dict())


@ml.command("events")
@click.argument("name")
def events_cmd(name: str) -> None:
    """晋级/回滚事件流（as-of 审计依据）。"""
    _echo(registry.events(name))


@ml.command()
@click.option("--name", required=True, help="逻辑模型线名")
@click.option("--features", "features_s", required=True, help="逗号分隔特征名")
@click.option("--date", default=None, help="只推理该日 YYYY-MM-DD；缺省全区间")
@click.option("--version", default=None, type=int, help="指定版本；缺省用线上版")
@click.option("--start", default="2024-01-01")
@click.option("--end", default=None)
@click.option("--universe", default="all")
@click.option("--no-persist", is_flag=True, help="只算不落 ml_signal")
def predict(name: str, features_s: str, date: str | None, version: int | None,
            start: str, end: str | None, universe: str, no_persist: bool) -> None:
    """用线上版本（或指定版本）产出信号。"""
    df = _load(start, end, universe)
    cfg = OnlineConfig(name=name, features=_features(features_s))
    _echo(daily_inference(df, cfg, date=date, version=version,
                          persist=not no_persist))


@ml.command()
@click.option("--name", required=True)
@click.option("--start", default=None)
@click.option("--end", default=None)
@click.option("--version", default=None, type=int)
@click.option("--limit", default=20, type=int)
def signals(name: str, start: str | None, end: str | None, version: int | None,
            limit: int) -> None:
    """查看已落库的模型信号。"""
    sig = load_signals(name, start=start, end=end, version=version)
    click.echo(f"  {len(sig)} 行，版本 {sorted(sig['model_version'].unique().to_list()) if len(sig) else []}")
    if len(sig):
        click.echo(str(sig.head(limit)))
