"""扩展数据 API：建表 / 写入 / 上传 / 回补 / 查询 / 因子同步。

前缀 ``/ext-data``，由 ``server/main.py`` 统一加 ``/api`` 前缀挂载，
与其它路由保持同一形状。

上传刻意**不用** ``UploadFile``：lquant 的核心/服务依赖里没有
``python-multipart``，而 FastAPI 一旦注册 ``File(...)`` 路由就会在启动时
硬依赖它 —— 一个可选的数据导入能力不该让整个服务起不来。这里改为
「原始 body + filename query 参数」，前端/脚本直接 PUT 文件字节即可。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from lquant.data.ext.ingest import backfill, import_file, write_json_rows
from lquant.data.ext.models import ExtConfig, ExtConfigError, ExtField, PullConfig
from lquant.data.ext.query import query_rows, query_values
from lquant.data.ext.storage import existing_dates, snapshot_path
from lquant.data.ext.store import ExtConfigStore

router = APIRouter(prefix="/ext-data", tags=["ext-data"])

#: 上传体积上限（原始 body）。与 ext_data 的写入语义一致：拒绝而不是
#: 悄悄截断 —— 截断后的 parquet 是「成功但少了半截」的静默故障。
MAX_UPLOAD_BYTES = 64 * 1024 * 1024


class FieldDef(BaseModel):
    name: str
    dtype: Literal["string", "int", "float", "bool"] = "string"
    label: str = ""


class PullDef(BaseModel):
    """HTTP 拉取配置（建表时可选带上，与 ``lq ext create`` 同口径）。"""

    url: str = ""
    method: Literal["GET", "POST"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None
    response_path: str = ""
    field_map: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=30, ge=5, le=300)
    page_param: str = ""
    page_size_param: str = ""
    page_size: int = 0
    max_pages: int = Field(default=20, ge=1, le=200)
    enabled: bool = False
    schedule_minutes: int = 1440


class CreateExtReq(BaseModel):
    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_]+$")
    label: str = Field(..., min_length=1, max_length=64)
    mode: Literal["timeseries", "snapshot"]
    fields: list[FieldDef] = Field(..., min_length=1)
    description: str = ""
    symbol_field: str | None = "symbol"
    date_field: str = "date"
    date_param: str | None = None
    date_format: Literal["iso", "compact"] = "iso"
    market_level: bool = False
    pull: PullDef | None = None


class WriteExtReq(BaseModel):
    """JSON 批量写入。``date`` 对 timeseries 必填（缺省=今天只在 CLI 侧兜底）。"""

    date: str | None = None
    rows: list[dict[str, Any]] = Field(..., min_length=1)


class BackfillReq(BaseModel):
    start: str
    end: str
    sleep_seconds: float = Field(default=0.0, ge=0.0, le=10.0)


def _store() -> ExtConfigStore:
    return ExtConfigStore()


def _get(table_id: str) -> ExtConfig:
    try:
        cfg = _store().get(table_id)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if cfg is None:
        raise HTTPException(404, f"扩展表 {table_id!r} 不存在")
    return cfg


def _parse_date(raw: str | None, field: str) -> date | None:
    if raw is None or raw == "":
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as e:
        # 这个值会拼进 `date=<value>` 目录名，非法值不只是格式问题
        raise HTTPException(400, f"{field} 不是合法日期: {raw!r}") from e


def _coverage(cfg: ExtConfig) -> dict[str, Any]:
    if cfg.mode == "snapshot":
        return {"has_data": snapshot_path(cfg).exists()}
    dates = existing_dates(cfg)
    return {"partitions": len(dates), "date_range": [dates[0], dates[-1]] if dates else None}


@router.get("")
def list_tables() -> list[dict]:
    """列出全部扩展表（含数据覆盖）。"""
    out = []
    for cfg in _store().load_all():
        out.append({**cfg.to_dict(), "coverage": _coverage(cfg)})
    return out


@router.post("", status_code=201)
def create_table(req: CreateExtReq) -> dict:
    """创建/覆盖一张扩展表定义。"""
    try:
        cfg = ExtConfig(
            id=req.id, label=req.label, mode=req.mode,
            fields=[ExtField(f.name, f.dtype, f.label) for f in req.fields],
            description=req.description,
            symbol_field=None if req.market_level else req.symbol_field,
            date_field=req.date_field, date_param=req.date_param,
            date_format=req.date_format, market_level=req.market_level,
            pull=PullConfig(**req.pull.model_dump()) if req.pull else None,
        )
    except ExtConfigError as e:
        raise HTTPException(422, str(e)) from e
    _store().save(cfg)
    return cfg.to_dict()


@router.delete("/{table_id}")
def delete_table(table_id: str) -> dict:
    cfg = _get(table_id)
    from lquant.data.ext.duckdb import drop_view

    _store().delete(table_id)
    import contextlib

    with contextlib.suppress(Exception):  # 视图清理失败不该让删除接口报错
        drop_view(cfg.id)
    return {"deleted": table_id}


@router.post("/{table_id}/write")
def write_rows(table_id: str, req: WriteExtReq) -> dict:
    """JSON 行写入。"""
    cfg = _get(table_id)
    try:
        n = write_json_rows(cfg, req.rows, day=_parse_date(req.date, "date"))
    except ExtConfigError as e:
        raise HTTPException(400, str(e)) from e
    _after_write(cfg)
    return {"status": "ok", "rows": n, "date": req.date}


@router.post("/{table_id}/upload")
async def upload_file(
    table_id: str,
    request: Request,
    filename: str = Query(..., description="原始文件名（决定解析器：.csv/.xlsx/.json）"),
    date: str | None = Query(None, alias="date"),
) -> dict:
    """上传 CSV / Excel / JSON（原始 body，不做 multipart）。"""
    cfg = _get(table_id)
    body = await request.body()
    if not body:
        raise HTTPException(400, "上传内容为空")
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"文件过大（上限 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB）")
    import tempfile
    from pathlib import Path

    suffix = Path(filename).suffix.lower()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"upload{suffix}"
        path.write_bytes(body)
        try:
            n = import_file(cfg, path, day=_parse_date(date, "date"))
        except ExtConfigError as e:
            raise HTTPException(400, str(e)) from e
    _after_write(cfg)
    return {"status": "ok", "rows": n, "date": date}


@router.post("/{table_id}/backfill")
def backfill_table(table_id: str, req: BackfillReq) -> dict:
    """按交易日 HTTP 回补历史分区（幂等；日期不符的日子拒绝写入并列入 failed）。"""
    cfg = _get(table_id)
    start = _parse_date(req.start, "start")
    end = _parse_date(req.end, "end")
    if start is None or end is None:
        raise HTTPException(400, "backfill 需要 start 与 end")
    try:
        report = backfill(cfg, start, end, sleep_seconds=req.sleep_seconds)
    except ExtConfigError as e:
        raise HTTPException(400, str(e)) from e
    _after_write(cfg)
    return report


@router.post("/{table_id}/sync-factors")
def sync_factors(table_id: str) -> dict:
    """把（一张表或全部表的）数值字段注册进因子库。"""
    _get(table_id)  # 表不存在时显式 404，而不是顺手同步了别的表
    from lquant.factors.ext_bridge import ext_field_catalog, sync_ext_factors

    result = sync_ext_factors()
    return {**result, **ext_field_catalog()}


@router.get("/{table_id}/rows")
def list_rows(
    table_id: str,
    date_: str | None = Query(None, alias="date"),
    start_date: str | None = None,
    end_date: str | None = None,
    filter: list[str] | None = Query(None, description="字段:值1|值2 / 字段!=值 / 字段~子串"),
    sort: str | None = Query(None, description="排序字段，降序加 :desc"),
    columns: str | None = Query(None, description="逗号分隔的列"),
    offset: int = Query(0, ge=0),
    limit: int = Query(1000, ge=1, le=20000),
) -> dict:
    """查明细（分页/过滤/排序；total 为过滤后、分页前的总数）。"""
    cfg = _get(table_id)
    try:
        return query_rows(
            cfg, day=_parse_date(date_, "date"),
            start_date=start_date, end_date=end_date, filters=filter, sort=sort,
            columns=[c.strip() for c in columns.split(",") if c.strip()] if columns else None,
            offset=offset, limit=limit,
        )
    except ExtConfigError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/{table_id}/values")
def list_values(
    table_id: str,
    field: str = Query(..., min_length=1),
    date_: str | None = Query(None, alias="date"),
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
) -> dict:
    """字段取值枚举（去重 + 计数，次数降序）。"""
    cfg = _get(table_id)
    try:
        return query_values(cfg, field, day=_parse_date(date_, "date"),
                            start_date=start_date, end_date=end_date, limit=limit)
    except ExtConfigError as e:
        raise HTTPException(400, str(e)) from e


def _after_write(cfg: ExtConfig) -> None:
    """写入后的一致性收尾：刷新 DuckDB 视图 + 同步因子注册。

    两步都 best-effort 且留日志：数据已安全落盘，派生结果失败不该让写入
    接口报错，但也不能静默 —— 用户需要知道「SQL 侧/因子库还没更新」。
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
        logger.warning(f"扩展因子注册同步失败: {e}")
