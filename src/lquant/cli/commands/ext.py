"""``lq ext``：扩展数据表（BYO data）的命令行入口。

覆盖最小可用闭环：建表 → 导入（CSV/Excel/JSON）→ 查明细/取值 → HTTP 回补
→ 注册因子。所有能力都只是 ``data.ext`` 包的薄封装 —— CLI 不重写任何口径，
否则「命令行看到的」与「API/研究拿到的」迟早分叉。
"""

from __future__ import annotations

import json
from datetime import date

import click
from rich.console import Console
from rich.table import Table

from lquant.data.ext.ingest import backfill, import_file
from lquant.data.ext.models import ExtConfig, ExtConfigError, ExtField, PullConfig
from lquant.data.ext.query import query_rows, query_values
from lquant.data.ext.storage import existing_dates, snapshot_path
from lquant.data.ext.store import ExtConfigStore

console = Console()


def _parse_fields(raw: tuple[str, ...]) -> list[ExtField]:
    """``--field 名称:类型``（省略类型按 string）。"""
    fields: list[ExtField] = []
    for item in raw:
        name, _, dtype = item.partition(":")
        name = name.strip()
        if not name:
            raise click.ClickException(f"--field 缺少字段名: {item!r}")
        fields.append(ExtField(name, (dtype or "string").strip()))
    return fields


def _load(store: ExtConfigStore, table_id: str) -> ExtConfig:
    cfg = store.get(table_id)
    if cfg is None:
        raise click.ClickException(f"扩展表 {table_id!r} 不存在（`lq ext list` 看已有表）")
    return cfg


def _after_write(cfg: ExtConfig) -> None:
    """写入后的一致性收尾：刷新 DuckDB 视图 + 同步因子注册。

    两步都 best-effort：数据已安全落盘，视图/因子注册是附加派生结果，
    不该让导入命令报失败；但失败必须留日志，不能静默。
    """
    from loguru import logger

    from lquant.data.ext.duckdb import sync_view

    try:
        sync_view(cfg)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"扩展表 {cfg.id} DuckDB 视图刷新失败: {e}")
    try:
        from lquant.factors.ext_bridge import sync_ext_factors

        sync_ext_factors()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"扩展因子注册同步失败（`lq ext sync-factors` 可重试）: {e}")


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as e:
        raise click.ClickException(f"日期格式错误（应为 YYYY-MM-DD）: {raw!r}") from e


@click.group()
def ext() -> None:
    """扩展数据表：自有数据 → parquet → 因子/信号。"""


@ext.command("list")
def list_cmd() -> None:
    """列出全部扩展表及其数据覆盖。"""
    store = ExtConfigStore()
    configs = store.load_all()
    if not configs:
        click.echo("还没有扩展表。用 `lq ext create` 建一张，或 `lq ext import` 导入 CSV。")
        return
    table = Table(title="扩展数据表")
    for col in ("id", "名称", "模式", "市场级", "字段", "数据"):
        table.add_column(col)
    for cfg in configs:
        if cfg.mode == "snapshot":
            exists = snapshot_path(cfg).exists()
            coverage = "有快照" if exists else "无数据"
        else:
            dates = existing_dates(cfg)
            coverage = f"{len(dates)} 个分区" + (f" {dates[0]}~{dates[-1]}" if dates else "")
        fields = ", ".join(f"{f.name}:{f.dtype}" for f in cfg.fields)
        table.add_row(cfg.id, cfg.label, cfg.mode, "是" if cfg.market_level else "否",
                      fields, coverage)
    console.print(table)


@ext.command("create")
@click.argument("table_id")
@click.option("--label", required=True, help="显示名")
@click.option("--mode", type=click.Choice(["timeseries", "snapshot"]), required=True,
              help="timeseries=按交易日分区；snapshot=只有最新值")
@click.option("--field", "raw_fields", multiple=True, required=True,
              help="字段定义 名称:类型（类型 string/int/float/bool），可重复")
@click.option("--symbol-field", default="symbol", show_default=True,
              help="标的列名；market_level 表须传空串")
@click.option("--date-field", default="date", show_default=True, help="日期列名")
@click.option("--date-param", default=None,
              help="HTTP 拉取的日期参数名（如 date）；不配则不能回补历史")
@click.option("--date-format", type=click.Choice(["iso", "compact"]), default="iso",
              show_default=True)
@click.option("--market-level", is_flag=True, default=False,
              help="市场级表：行 = 全市场每日一条，无 symbol 列")
@click.option("--description", default="", help="说明")
@click.option("--pull-url", default="", help="HTTP 拉取地址（配合 --date-param 可回补）")
@click.option("--pull-method", type=click.Choice(["GET", "POST"]), default="GET",
              show_default=True)
@click.option("--pull-response-path", default="",
              help="响应里行数组的 dot-path（如 data.list）")
@click.option("--pull-header", "pull_headers", multiple=True,
              help="请求头 KEY=VALUE，可重复")
@click.option("--pull-field-map", "pull_field_map", multiple=True,
              help="字段映射 外部名=内部名，可重复")
@click.option("--pull-timeout", type=int, default=30, show_default=True)
@click.option("--pull-enabled", is_flag=True, default=False, help="启用定时拉取")
def create_cmd(table_id, label, mode, raw_fields, symbol_field, date_field, date_param,
               date_format, market_level, description, pull_url, pull_method,
               pull_response_path, pull_headers, pull_field_map, pull_timeout,
               pull_enabled) -> None:
    """创建（或覆盖）一张扩展表定义。"""
    headers: dict[str, str] = {}
    for item in pull_headers:
        k, _, v = item.partition("=")
        if not k.strip():
            raise click.ClickException(f"--pull-header 格式应为 KEY=VALUE: {item!r}")
        headers[k.strip()] = v
    field_map: dict[str, str] = {}
    for item in pull_field_map:
        k, _, v = item.partition("=")
        if not k.strip() or not v.strip():
            raise click.ClickException(f"--pull-field-map 格式应为 外部名=内部名: {item!r}")
        field_map[k.strip()] = v.strip()
    try:
        cfg = ExtConfig(
            id=table_id, label=label, mode=mode, fields=_parse_fields(raw_fields),
            description=description,
            symbol_field=None if market_level else symbol_field,
            date_field=date_field, date_param=date_param, date_format=date_format,
            market_level=market_level,
            pull=PullConfig(url=pull_url, method=pull_method, headers=headers,
                            response_path=pull_response_path, field_map=field_map,
                            timeout_seconds=pull_timeout, enabled=pull_enabled)
            if pull_url else None,
        )
    except ExtConfigError as e:
        raise click.ClickException(str(e)) from e
    ExtConfigStore().save(cfg)
    _after_write(cfg)
    click.echo(f"已创建扩展表 {cfg.id}（{cfg.mode}，{len(cfg.fields)} 个字段）")


@ext.command("import")
@click.argument("table_id")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.option("--date", "day", default=None, help="目标日期 YYYY-MM-DD（timeseries 必填）")
def import_cmd(table_id, path, day) -> None:
    """导入 CSV / Excel / JSON 文件。"""
    store = ExtConfigStore()
    cfg = _load(store, table_id)
    try:
        n = import_file(cfg, path, day=_parse_date(day))
    except ExtConfigError as e:
        raise click.ClickException(str(e)) from e
    _after_write(cfg)
    click.echo(f"已导入 {n} 行到扩展表 {cfg.id}")


@ext.command("rows")
@click.argument("table_id")
@click.option("--date", "day", default=None, help="单日（timeseries）")
@click.option("--start", "start_date", default=None, help="起始日期（含）")
@click.option("--end", "end_date", default=None, help="结束日期（含）")
@click.option("--filter", "filters", multiple=True,
              help="过滤 字段:值1|值2 / 字段!=值 / 字段~子串，可重复（AND）")
@click.option("--sort", default=None, help="排序字段（降序加 :desc）")
@click.option("--columns", default=None, help="逗号分隔的列")
@click.option("--limit", type=int, default=50, show_default=True)
@click.option("--offset", type=int, default=0, show_default=True)
@click.option("--json", "as_json", is_flag=True, default=False, help="输出原始 JSON")
def rows_cmd(table_id, day, start_date, end_date, filters, sort, columns, limit, offset,
             as_json) -> None:
    """查扩展表明细（分页/过滤/排序）。"""
    cfg = _load(ExtConfigStore(), table_id)
    try:
        payload = query_rows(
            cfg, day=_parse_date(day), start_date=start_date, end_date=end_date,
            filters=list(filters), sort=sort,
            columns=[c.strip() for c in columns.split(",")] if columns else None,
            offset=offset, limit=limit,
        )
    except ExtConfigError as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        click.echo(json.dumps(payload, ensure_ascii=False, default=str))
        return
    if not payload["rows"]:
        click.echo(f"扩展表 {cfg.id}：无数据（date={payload['date']}）")
        return
    table = Table(title=f"{cfg.label} ({cfg.id}) date={payload['date']} "
                        f"total={payload['total']}")
    cols = list(payload["rows"][0].keys())
    for col in cols:
        table.add_column(str(col))
    for row in payload["rows"]:
        table.add_row(*["" if row[c] is None else str(row[c]) for c in cols])
    console.print(table)


@ext.command("values")
@click.argument("table_id")
@click.option("--field", required=True, help="要枚举的字段")
@click.option("--date", "day", default=None)
@click.option("--start", "start_date", default=None)
@click.option("--end", "end_date", default=None)
@click.option("--limit", type=int, default=50, show_default=True)
def values_cmd(table_id, field, day, start_date, end_date, limit) -> None:
    """枚举字段取值（去重 + 计数）。"""
    cfg = _load(ExtConfigStore(), table_id)
    try:
        payload = query_values(cfg, field, day=_parse_date(day), start_date=start_date,
                               end_date=end_date, limit=limit)
    except ExtConfigError as e:
        raise click.ClickException(str(e)) from e
    for item in payload["values"]:
        click.echo(f"{item['value']}\t{item['count']}")


@ext.command("backfill")
@click.argument("table_id")
@click.option("--start", "start_date", required=True, help="起始日期 YYYY-MM-DD")
@click.option("--end", "end_date", required=True, help="结束日期 YYYY-MM-DD")
@click.option("--sleep", "sleep_seconds", type=float, default=0.3, show_default=True,
              help="相邻请求间隔（秒），对数据源限速")
def backfill_cmd(table_id, start_date, end_date, sleep_seconds) -> None:
    """按交易日回补 HTTP 历史分区（幂等：已有分区跳过）。"""
    cfg = _load(ExtConfigStore(), table_id)
    s, e = _parse_date(start_date), _parse_date(end_date)
    assert s is not None and e is not None
    try:
        report = backfill(cfg, s, e, sleep_seconds=sleep_seconds)
    except ExtConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    _after_write(cfg)
    click.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if report["failed"]:
        # fail-loudly：有失败日期就必须让退出码非零，否则定时任务会把
        # 「部分失败」当成功，缺口永远没人补。
        raise click.ClickException(f"{len(report['failed'])} 个交易日回补失败（见上方 failed）")


@ext.command("sync-factors")
def sync_factors_cmd() -> None:
    """把扩展表数值字段注册进因子库（factor_def）。"""
    from lquant.factors.ext_bridge import ext_field_catalog, sync_ext_factors

    result = sync_ext_factors()
    catalog = ext_field_catalog()
    click.echo(json.dumps({
        **result,
        "factors": [f["name"] for f in catalog["factors"]],
        "string_fields": [f["column"] for f in catalog["string_fields"]],
    }, ensure_ascii=False, indent=2))


@ext.command("delete")
@click.argument("table_id")
@click.option("--yes", is_flag=True, default=False, help="跳过确认")
def delete_cmd(table_id, yes) -> None:
    """删除一张扩展表（配置 + 数据）。"""
    cfg = _load(ExtConfigStore(), table_id)
    if not yes:
        click.confirm(f"确认删除扩展表 {cfg.label} ({cfg.id}) 及其全部数据？", abort=True)
    ExtConfigStore().delete(table_id)
    click.echo(f"已删除扩展表 {cfg.id}")
