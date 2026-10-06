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
@click.option("--symbols", default=None, help="逗号分隔；与 --all 二选一")
@click.option("--all", "use_all", is_flag=True,
              help="全市场股票（含退市，防幸存者偏差）；tushare 下约 0.7s/只×4 接口")
@click.option("--start", default="2016-01-01")
@click.option("--end", default=None)
@click.option("--provider", "provider_name", default=None,
              type=click.Choice(["baostock", "tushare"]),
              help="缺省自动选 tushare（基本面统一源），缺 token 退回 baostock")
def financial(symbols: str | None, use_all: bool, start: str, end: str | None,
              provider_name: str | None) -> None:
    """PIT 财务回填（stat_date + pub_date，防未来函数）。

    断点是「窗口 + 标的」的：同一窗口重复跑只补没覆盖到的标的；
    换了窗口（比如从 2016 起改成只补最近 90 天）会按新窗口重新判定，
    不会因为「标的上次跑过」就静默跳过。
    """
    from lquant.data.ingest.financial import backfill_financial
    from lquant.data.store.catalog import SecurityRepo

    if use_all:
        syms = SecurityRepo().stock_symbols()
    elif symbols:
        syms = symbols.split(",")
    else:
        raise click.UsageError("--symbols 与 --all 必须给一个")

    out = backfill_financial(syms, start=start, end=end, provider_name=provider_name)
    click.echo(f"  窗口 {out['start']} ~ {out['end']}，增量段 {out['groups']} 个")
    click.echo(f"  实拉 {out['done']} 只，覆盖区间跳过 {out['skipped_covered']} 只")
    click.echo(f"  financial_pit 累计 {out['rows']} 行")


@data.command("basic")
@click.option("--start", default="2024-01-01", help="回填区间起点（默认日线湖起点可传 2024）")
@click.option("--end", default=None)
@click.option("--no-merge", is_flag=True, help="只落 daily_basic 湖，不合并回日线")
def basic_cmd(start: str, end: str | None, no_merge: bool) -> None:
    """tushare daily_basic 回填（市值/估值/股本），并合并回日线湖空缺列。

    一天一请求覆盖全市场；checkpoint 按天断点续传；merge 只填 NULL 不覆盖主源。
    """
    from lquant.data.ingest.daily_basic import backfill_daily_basic

    out = backfill_daily_basic(start=start, end=end, merge=not no_merge)
    for k, v in out.items():
        click.echo(f"  {k}: {v}")
    click.echo("basic done")


@data.command("index-cons")
@click.option("--indexes", default=None, help="逗号分隔指数代码（默认沪深300/中证500/800/1000）")
def index_cons_cmd(indexes: str | None) -> None:
    """同步指数成分快照（tushare index_weight → index_cons）。

    因子评价/回测的股票池过滤（沪深300 等）依赖这张表。
    """
    from lquant.data.ingest.index_cons import sync_index_cons

    codes = [c.strip() for c in indexes.split(",")] if indexes else None
    out = sync_index_cons(codes)
    for k, v in out.items():
        click.echo(f"  {k}: {v}")
    click.echo("index-cons done")


@data.command("index")
@click.option("--start", default="2016-01-01", help="起始日（ISO）")
@click.option("--end", default=None, help="结束日（ISO，缺省今天）")
@click.option("--symbols", default=None,
              help="逗号分隔指数代码（缺省 INDEX_POOL 全部）")
def index_cmd(start: str, end: str | None, symbols: str | None) -> None:
    """回填指数日线（回测基准的历史来源）。

    指数走 ``Capability.INDEX_DAILY`` 选源（tushare pro.index_daily /
    akshare index_zh_a_hist / baostock 指数通道），落 DuckDB ``index_daily``，
    **不进日线 parquet 湖**（点位不是价格）。可安全重跑（upsert 幂等）。
    """
    from datetime import date as _date

    from lquant.core.types import today_cn
    from lquant.market.backfill import backfill_index_history

    end_d = _date.fromisoformat(end) if end else today_cn()
    syms = [s.strip() for s in symbols.split(",")] if symbols else None
    rep = backfill_index_history(_date.fromisoformat(start), end_d, symbols=syms)
    click.echo(f"  窗口: {rep['start']} ~ {rep['end']}")
    click.echo(f"  拉取: {rep['fetched']} 行，入库: {rep['persisted']} 行")
    click.echo("  区间内覆盖（回填前 → 后）:")
    for sym in rep["symbols"]:
        click.echo(f"    {sym}: {rep['coverage_before'].get(sym, 0)}"
                   f" → {rep['coverage_after'].get(sym, 0)}")
    click.echo("index done")


@data.command("money-flow")
@click.option("--symbols", default=None, help="逗号分隔标的（裸码或带后缀，如 600519,000001.SZ）")
@click.option("--all", "all_syms", is_flag=True, help="回填日线湖里的全部真实标的")
@click.option("--limit", default=None, type=int, help="配合 --all：只回填前 N 只")
@click.option("--days", default=None, type=int, help="每只只保留最近 N 个交易日")
@click.option("--qps", default=None, type=float,
              help="请求速率上限（默认 1.5；被封 IP 时调小，网络好可调大）")
@click.option("--purge-demo", is_flag=True, help="清理合成数据并补 source（不联网）")
@click.option("--yes", is_flag=True, help="确认执行 --purge-demo 的删除")
@click.option("--demo", is_flag=True, help="生成合成数据（不联网，仅测试）")
def money_flow_cmd(symbols: str | None, all_syms: bool, limit: int | None,
                   days: int | None, qps: float | None, purge_demo: bool,
                   yes: bool, demo: bool) -> None:
    """回填逐日主力资金流（个股分析「资金面」的数据来源）。

    每日横截面采集只有当天，而且东财的榜单式接口只装得下净流入靠前的
    少数标的 —— 普通股票在 money_flow 里一行都没有，分析的资金面永远是
    「样本不足」。本命令走东财单票历史接口，把任意标的补到约 120 个交易日。

    \b
    lq data money-flow --symbols 600519,000001.SZ   # 指定标的
    lq data money-flow --all                        # 全市场（约 7000 只，几十分钟）
    lq data money-flow --purge-demo --yes           # 清掉库里的合成数据
    """
    from lquant.market.backfill import (
        backfill_money_flow_history,
        money_flow_symbols,
        purge_demo_flow,
    )

    if purge_demo:
        preview = purge_demo_flow(dry_run=True)
        click.echo(f"  待清理合成数据: {preview['demo_rows']} 行"
                   f"（另有 {preview['relabel_rows']} 行老数据补 source）")
        if not yes:
            click.echo("  这是删除操作，确认后加 --yes 重跑")
            return
        rep = purge_demo_flow()
        click.echo(f"  已删除: {rep['deleted']} 行，补 source: {rep['relabel_rows']} 行")

    targets: list[str] = []
    if symbols:
        targets = [s.strip() for s in symbols.split(",") if s.strip()]
    elif all_syms:
        targets = money_flow_symbols(limit=limit)
        click.echo(f"  标的池（日线湖）: {len(targets)} 只")
        if not targets:
            click.echo("  日线湖为空，先跑 `lq data sync`")
            return
    elif demo:
        targets = ["600519.SH", "000001.SZ"]
    elif not purge_demo:
        click.echo("  未指定标的。用法：--symbols / --all / --purge-demo")
        return

    if not targets:
        return

    last = {"i": 0}

    def _progress(done: int, total: int, sym: str) -> None:
        # 全市场几千只，逐只刷屏没有意义；每 50 只报一次
        step = max(1, total // 50)
        if done == total or done - last["i"] >= step:
            last["i"] = done
            click.echo(f"  [{done}/{total}] {sym}")

    rep = backfill_money_flow_history(targets, days=days, demo=demo, qps=qps,
                                      progress=_progress)
    click.echo(f"  拉取: {rep['fetched']} 行，入库: {rep['persisted']} 行"
               f"（{rep['qps']} qps）")
    click.echo(f"  有资金流的标的: {rep['covered_before']} → {rep['covered_after']}"
               f" / {rep['symbols']}")
    if rep["failed"]:
        click.echo(f"  ⚠ 失败 {len(rep['failed'])} 只，前几例:")
        for sym, err in list(rep["failed"].items())[:5]:
            click.echo(f"    {sym}: {err}")
    _money_flow_coverage_line()
    click.echo("money-flow done")


def _money_flow_coverage_line() -> None:
    """资金流覆盖一览（个股分析「资金面」的数据源出口）。

    没有这行时「资金面恒为空」只能靠打开某只股票的分析报告才发现 ——
    显式给出「覆盖多少标的 / 多少天 / 有多少是合成数据」。
    """
    from lquant.core.db import reader
    from lquant.market.backfill import money_flow_symbols, real_flow_predicate

    try:
        with reader() as con:
            real = real_flow_predicate(con)
            row = con.execute(
                "SELECT count(*), count(DISTINCT symbol), count(DISTINCT trade_date), "
                f"min(trade_date), max(trade_date) FROM money_flow WHERE {real}"
            ).fetchone()
            demo_n = con.execute(
                f"SELECT count(*) FROM money_flow WHERE NOT ({real})").fetchone()[0]
        # 解包也放在 try 里：status() 的约定是「读库形态异常就降级显示」，
        # 不能因为某行数不对就整个命令挂掉（与上面 _index_coverage_line 一致）。
        n, syms, days, lo, hi = row
    except Exception as e:  # noqa: BLE001  表可能未建 / 结果形态异常
        click.echo(f"  money_flow: - ({type(e).__name__})")
        return

    click.echo(f"  money_flow: {n} 行 / {syms} 只 / {days} 个交易日 {lo} ~ {hi}")
    if demo_n:
        click.echo(f"    ⚠ 另有 {demo_n} 行合成数据（分析已排除，"
                   "`lq data money-flow --purge-demo --yes` 清理）")
    universe = money_flow_symbols()
    if universe:
        pct = syms / len(universe) * 100
        tail = "" if syms >= len(universe) else "（跑 `lq data money-flow --all` 补齐）"
        click.echo(f"    覆盖: {syms}/{len(universe)} 只真实标的 ({pct:.1f}%){tail}")


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
    # 湖路径必须来自配置（settings.parquet_dir），不能用相对 CWD 的
    # "data/parquet/..."：服务进程与 CLI 的 CWD 不同，或配了绝对路径时
    # 这里会静默显示 0 个分区（与 parquet.lake_glob 同一约定）。
    from pathlib import Path

    from lquant.core.config import get_settings
    from lquant.data.store.parquet import lake_is_empty

    root = Path(get_settings().parquet_dir) / "daily"
    files = sorted(root.rglob("*.parquet")) if root.is_dir() else []
    click.echo(f"  daily parquet 年分区: {len(files)}"
               + ("（湖为空，先跑 lq data sync）" if lake_is_empty("daily") else ""))

    _index_coverage_line()
    _money_flow_coverage_line()


def _index_coverage_line() -> None:
    """指数日线覆盖一览（回测基准的数据源，Phase 1.1 的可见性出口）。

    没有这行时「基准缺失」只能靠回测跑出 NaN 才发现 —— 显式给出
    「有哪些指数、各自覆盖到哪天、基准是否可用」。
    """
    from lquant.core.db import reader
    from lquant.market.collectors.index_daily import INDEX_POOL

    try:
        with reader() as con:
            rows = con.execute(
                "SELECT symbol, count(*) AS n, min(trade_date) AS lo, "
                "max(trade_date) AS hi, count(DISTINCT trade_date) AS days "
                "FROM index_daily GROUP BY symbol ORDER BY symbol"
            ).fetchall()
    except Exception as e:  # noqa: BLE001
        click.echo(f"  index_daily: - ({type(e).__name__})")
        return

    if not rows:
        click.echo("  index_daily: 0（基准缺失，跑 `lq data index --start 2016-01-01`）")
        return
    click.echo(f"  index_daily: {len(rows)} 只指数 / {sum(r[1] for r in rows)} 行")
    have = {str(r[0]) for r in rows}
    for sym, _n, lo, hi, days in rows:
        click.echo(f"    {sym} {INDEX_POOL.get(str(sym), '?'):<8} "
                   f"{days:>5} 交易日 {lo} ~ {hi}")
    missing = [s for s in INDEX_POOL if s not in have]
    if missing:
        click.echo(f"    缺: {', '.join(missing)}（`lq data index` 可补）")
    # 回测默认基准是否就绪 —— Phase 1.2 的默认值是 000300.SH
    from lquant.backtest.benchmark import DEFAULT_BENCHMARK

    if DEFAULT_BENCHMARK not in have:
        click.echo(f"    ⚠ 默认基准 {DEFAULT_BENCHMARK} 尚无数据，"
                   "回测超额收益会缺失（跑 `lq data index`）")


@data.command()
@click.option("--start", default="2024-01-01")
def demo(start: str) -> None:
    """生成演示数据（无网络环境的开箱即用；绝不用于真实回测结论）。

    只用于全新/空湖：目标湖已有真实日线时会直接拒绝，避免合成数据覆盖真实
    观测（隔离 worktree 的 data/parquet 常软链到主仓真实湖）。
    """
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
