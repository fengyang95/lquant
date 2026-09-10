"""lq data：地基同步 / 日线 / 分钟 / 财务 / 检查 / 对拍。"""
from __future__ import annotations

import click


@click.group()
def data() -> None:
    """数据接入"""


@data.command()
@click.option("--skip-details", is_flag=True, help="跳过逐只补上市日期（慢路径）")
@click.option("--detail-limit", default=None, type=int)
def reference(skip_details: bool, detail_limit: int | None) -> None:
    """同步交易日历 + 标的基础表（地基，先跑这个）。"""
    from lquant.data.ingest.reference import sync_reference

    out = sync_reference(skip_details=skip_details, detail_limit=detail_limit)
    for k, v in out.items():
        click.echo(f"  {k}: {v}")
    click.echo("reference done")


@data.command()
@click.option("--full", is_flag=True, help="全市场；不加则只跑哨兵池前 200 只")
@click.option("--start", default="2016-01-01")
@click.option("--end", default=None)
def sync(full: bool, start: str, end: str | None) -> None:
    """回填日线（断点续传 + 看门狗）。"""
    from lquant.data.ingest.daily import backfill_daily

    n = backfill_daily(full=full, start=start, end=end)
    click.echo(f"done {n}")


@data.command()
@click.option("--symbols", default=None, help="逗号分隔；默认取库里全部 ETF/LOF")
@click.option("--enrich", is_flag=True, help="用东财快照补规模（需 akshare）")
def etf(symbols: str | None, enrich: bool) -> None:
    """同步 ETF 元数据（跟踪指数 / T+N 推断）。"""
    from lquant.data.ingest.etf_meta import enrich_from_akshare, sync_etf_meta

    syms = symbols.split(",") if symbols else None
    n = sync_etf_meta(syms)
    click.echo(f"etf_meta done {n}")
    if enrich:
        m = enrich_from_akshare()
        click.echo(f"enrich done {m}")


@data.command("minute")
@click.option("--symbols", default=None, help="逗号分隔；默认取 security 表前 800 只")
@click.option("--start", default="2020-01-01")
@click.option("--end", default=None)
@click.option("--freq", default="60min", type=click.Choice(["5min", "15min", "30min", "60min"]))
def minute_cmd(symbols: str | None, start: str, end: str | None, freq: str) -> None:
    """回填分钟线（默认 60min，断点续传）。"""
    from lquant.data.ingest.minute import backfill_minute
    from lquant.data.store.catalog import SecurityRepo

    syms = symbols.split(",") if symbols else SecurityRepo().active_symbols()[:800]
    n = backfill_minute(syms, start=start, end=end, freq=freq)
    click.echo(f"done {n}")


@data.command()
@click.option("--symbols", required=True, help="逗号分隔；财务接口无批量，全市场会跑几天")
@click.option("--start", default="2016-01-01")
@click.option("--end", default=None)
def financial(symbols: str, start: str, end: str | None) -> None:
    """PIT 财务回填（stat_date + pub_date，防未来函数）。"""
    from lquant.data.ingest.financial import backfill_financial

    n = backfill_financial(symbols.split(","), start=start, end=end)
    click.echo(f"done {n}")


@data.command()
def status() -> None:
    """数据覆盖度一览。"""
    from lquant.core.db import reader

    rows = [
        ("security", "SELECT count(*) FROM security"),
        ("security 待补详情",
         "SELECT count(*) FROM security WHERE list_date IS NULL AND sec_type <> 'index'"),
        ("etf_meta", "SELECT count(*) FROM etf_meta"),
        ("calendar 开市日", "SELECT count(*) FROM trade_calendar WHERE is_open"),
        ("financial_pit", "SELECT count(*) FROM financial_pit"),
    ]
    for name, sql in rows:
        try:
            with reader() as con:
                n = con.execute(sql).fetchone()[0]
            click.echo(f"  {name}: {n}")
        except Exception as e:  # noqa: BLE001
            click.echo(f"  {name}: - ({type(e).__name__})")
    import glob

    files = glob.glob("data/parquet/daily/**/*.parquet", recursive=True)
    click.echo(f"  daily parquet 年分区: {len(files)}")


@data.command()
@click.option("--start", default="2024-01-01")
def demo(start: str) -> None:
    """生成演示数据（无网络环境的开箱即用；绝不用于真实回测结论）。"""
    from lquant.data.ingest.demo import generate_demo

    out = generate_demo(start=start)
    for k, v in out.items():
        click.echo(f"  {k}: {v}")
    click.echo("demo data done —— 换真实数据跑 `lq data reference && lq data sync` 即可覆盖")


@data.command()
@click.option("--start", default=None, help="只检查该日期之后的数据（ISO）")
@click.option("--end", default=None)
def check(start: str | None, end: str | None) -> None:
    """跑全湖质量校验（涨跌停/覆盖度/僵尸/复权/日历），issue 落库。

    fatal / error 存在时退出码为 2 —— 让调度系统能感知质量恶化，
    「fatal 只是落库不阻断」与「命令成功」是两回事。
    """
    from lquant.data.quality.pipeline import run_lake_checks

    issues = run_lake_checks(start=start, end=end)
    if not issues:
        click.echo("quality: PASS（无 issue）")
        return
    by_sev: dict[str, int] = {}
    for i in issues:
        by_sev[i.severity] = by_sev.get(i.severity, 0) + 1
    click.echo(f"quality: {len(issues)} 条 issue（{by_sev}），已落 data_quality_issue")
    for i in issues[:20]:
        click.echo(f"  [{i.severity}] {i.rule}: {i.detail}")
    if by_sev.get("fatal") or by_sev.get("error"):
        raise SystemExit(2)


@data.command()
@click.option("--peers", default="", help="逗号分隔的同行源；留空用 config/providers.yaml 的 crosscheck.peers")
@click.option("--start", default="2024-01-01")
@click.option("--end", default=None)
@click.option("--limit", default=200, type=int,
              help="抽检标的数（§3.8.4 分层抽样的哨兵层）")
def crosscheck(peers: str, start: str, end: str | None, limit: int) -> None:
    """跨源对拍（抽检，标记与降级，绝不取值）。

    以湖内为主，拉同行实价比对；偏差打 CROSS_SRC_DIFF 标记 + 落
    data_quality_issue。同行源不可用/未启用 → 报 L0 跳过，不报错。
    """
    from lquant.data.ingest.crosscheck import run_crosscheck

    out = run_crosscheck(
        peers=[p.strip() for p in peers.split(",") if p.strip()],
        start=start, end=end, limit=limit,
    )
    click.echo(f"crosscheck: {out['summary']}")
    for lv in ("L1", "L2", "L3"):
        if out["summary"].get(lv):
            click.echo(f"  [{lv}] {len([i for i in out['issues'] if i.rule.endswith(lv)])} 条 issue 待查看")
    click.echo(f"primary 打 CROSS_SRC_DIFF 标记: {out['flagged_rows']} 行")


@data.command("fields")
@click.option("--start", default=None, help="覆盖率统计窗口起点")
def fields(start: str | None) -> None:
    """字段白名单 + 覆盖率（方案 6.2：Agent 接入第一步）。"""
    import json

    from lquant.data.store.parquet import read_daily

    df = read_daily(start=start).collect()
    if not len(df):
        raise click.ClickException("日线数据为空，先跑 bootstrap 或 lq data demo")
    n = len(df)
    out = [{"field": c, "coverage": round(1 - df[c].null_count() / n, 4), "rows": n}
           for c in df.columns]
    click.echo(json.dumps(out, ensure_ascii=False, indent=1))
