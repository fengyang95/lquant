"""数据管理增强路由（/data 前缀，与 data.py 同挂 /api）。

独立成文件的原因：data.py 已近 800 行上限，管理类端点（issue 检索 /
版本查询 / purge / export / 字典）另立门户，避免单文件超限。
"""
from __future__ import annotations

import io
import re
from datetime import date

import polars as pl
from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from lquant.core.db import reader
from lquant.data.store.parquet import read_daily
from lquant.server.deps import bare_code  # noqa: F401  bare_code_or_full 里也用

router = APIRouter(prefix="/data", tags=["data"])

# purge 与 /data/check 互斥：锁本体在 data.py（模块属性动态取，便于测试打桩）
from lquant.server.api import data as _data_api  # noqa: E402

# 导出行数上限：导出是同步端点，超大盘导出会拖垮 worker
_EXPORT_ROW_CAP = 5_000_000


class IssuesResolveIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500,
                           description="issue_id 列表（批量标记已解决）")

    @field_validator("ids")
    @classmethod
    def _ids_nonempty(cls, v: list[str]) -> list[str]:
        cleaned = [x for x in (s.strip() for s in v) if x]
        if not cleaned:
            raise ValueError("ids 不能为空")
        return cleaned


class PurgeIn(BaseModel):
    dataset: str = Field(default="daily", description="仅支持 daily（日线湖）")
    symbols: list[str] | None = Field(default=None, description="标的列表（如 600519.SH）")
    start: str | None = Field(default=None, max_length=10)
    end: str | None = Field(default=None, max_length=10)
    dry_run: bool = Field(default=False, description="true = 只统计将删行数，不落盘")

    @field_validator("start", "end")
    @classmethod
    def _validate_date(cls, v: str | None) -> str | None:
        if v:
            try:
                date.fromisoformat(v)
            except (TypeError, ValueError):
                raise ValueError(f"日期需为 YYYY-MM-DD 格式，收到 {v!r}") from None
        return v

    @field_validator("symbols")
    @classmethod
    def _validate_symbols(cls, v: list[str] | None) -> list[str] | None:
        if v:
            for s in v:
                if len(bare_code(s)) < 6:
                    raise ValueError(f"标的代码非法: {s!r}")
        return v

    @field_validator("dataset")
    @classmethod
    def _validate_dataset(cls, v: str) -> str:
        if v != "daily":
            raise ValueError(f"dataset 仅支持 daily，收到 {v!r}")
        return v


def _validate_query_date(name: str, val: str | None) -> None:
    if val:
        try:
            date.fromisoformat(val)
        except ValueError as e:
            raise HTTPException(422, f"{name} 日期非法: {val}") from e


# ---------- 1. 质量问题检索 / 批量 resolve ----------

@router.get("/issues")
def list_issues(
    severity: str | None = Query(default=None, max_length=16,
                                 description="fatal/error/warn/info"),
    dataset: str | None = Query(default=None, max_length=32),
    resolved: bool = False,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[dict]:
    """质量问题历史检索（data_quality_issue，与 /data/crosscheck/issues 同源）。"""
    from lquant.data.quality.issues import query_issues

    return query_issues(severity=severity, dataset=dataset,
                        resolved=resolved, limit=limit)


@router.post("/issues/resolve")
def resolve_issues_ep(req: IssuesResolveIn) -> dict:
    """批量标记问题已解决；返回 {requested, resolved}。

    不存在的 id 自动跳过（不整体 404）；请求的 id 全都不存在 → 404，
    让前端能区分「点了个寂寞」和「标记成功了一部分」。
    """
    from lquant.data.quality.issues import resolve_issues

    n = resolve_issues(req.ids)
    if n == 0:
        raise HTTPException(404, "所有 issue 均不存在，未标记任何记录")
    return {"requested": len(req.ids), "resolved": n}


# ---------- 2. data_version 展示 ----------

@router.get("/version/latest")
def version_latest(dataset: str | None = Query(default=None, max_length=32)) -> dict | None:
    """当前最新数据版本（可按 dataset 过滤）；从未登记过 → null（不是错误）。"""
    from lquant.data.lineage import latest as lineage_latest

    if dataset:
        v = lineage_latest(dataset)
        if v is None:
            return None
        with reader() as con:
            row = con.execute(
                "SELECT version, dataset, trade_date, row_count, created_at "
                "FROM data_version WHERE version = ?", [v]).fetchone()
        if row is None:  # 版本号在但行没了，异常态按空处理
            return None
    else:
        with reader() as con:
            row = con.execute(
                "SELECT version, dataset, trade_date, row_count, created_at "
                "FROM data_version ORDER BY created_at DESC LIMIT 1").fetchone()
        if row is None:
            return None
    return {"version": row[0], "dataset": row[1],
            "trade_date": str(row[2]) if row[2] else None,
            "row_count": row[3], "created_at": str(row[4])}


@router.get("/versions")
def version_history(limit: int = Query(default=20, ge=1, le=200)) -> list[dict]:
    """数据版本历史（最新在前）。"""
    with reader() as con:
        try:
            rows = con.execute(
                "SELECT version, dataset, trade_date, row_count, created_at "
                "FROM data_version ORDER BY created_at DESC LIMIT ?",
                [limit]).fetchall()
        except Exception:  # noqa: BLE001 - 表不存在等价于空历史
            return []
    return [{"version": r[0], "dataset": r[1],
             "trade_date": str(r[2]) if r[2] else None,
             "row_count": r[3], "created_at": str(r[4])} for r in rows]


# ---------- 3. 数据删除 / 重刷（purge） ----------

@router.post("/purge")
def purge(req: PurgeIn) -> dict:
    """按标的/日期区间删除日线湖数据（重刷前的清场动作）。

    - dry_run=true 只统计将删行数，不落盘；
    - 与 /data/check 共用互斥锁，409 = 有全湖检查或 purge 在跑；
    - 422 = 无任何过滤条件（防止手滑清全湖）。
    """
    from loguru import logger

    from lquant.data.store.parquet import delete_daily

    if not (req.symbols or req.start or req.end):
        raise HTTPException(422, "purge 必须至少给出一个过滤条件"
                                 "（symbols / start / end），拒绝无差别清湖")

    if not _data_api._lake_check_lock.acquire(blocking=False):
        raise HTTPException(409, "全湖质量检查或删除任务正在执行中，请稍后再试")
    scope = {"symbols": req.symbols, "start": req.start, "end": req.end}
    try:
        result = delete_daily(symbols=req.symbols, start=req.start,
                              end=req.end, dry_run=req.dry_run)
    except Exception as e:
        # 中途失败也要留下审计痕迹：scope + 错误，湖侧 per-file 日志由
        # delete_daily 自己输出（已完成/未完成以日志顺序可辨）
        logger.error(f"数据删除失败 scope={scope} error={type(e).__name__}: {e}")
        raise HTTPException(500, f"删除执行失败: {e}") from e
    finally:
        _data_api._lake_check_lock.release()

    logger.info(f"数据删除 {'(dry_run) ' if req.dry_run else ''}"
                f"rows={result['rows_matched']} scope={scope}")
    return {"dry_run": req.dry_run, "scope": scope, **result}


# ---------- 4. 数据导出 ----------

@router.get("/export")
def export_data(
    dataset: str = Query(default="daily", max_length=32),
    symbols: str | None = Query(default=None, max_length=10_000,
                                description="逗号分隔的标的列表"),
    start: str | None = Query(default=None, max_length=10),
    end: str | None = Query(default=None, max_length=10),
    fmt: str = Query(default="csv", pattern="^(csv|parquet)$",
                     alias="format"),
) -> Response:
    """导出日线湖数据为 CSV（utf-8-sig，Excel 友好）或 Parquet。

    行数上限 5,000,000 —— 超出 422 提示收窄区间（导出是同步端点，
    无上限等于让 worker 一次物化全湖）。
    """
    _validate_query_date("start", start)
    _validate_query_date("end", end)
    if dataset != "daily":
        raise HTTPException(422, f"dataset 仅支持 daily，收到 {dataset!r}")
    syms = None
    if symbols:
        syms = [bare_code_or_full(s) for s in symbols.split(",") if s.strip()]
        if not syms:
            raise HTTPException(422, "symbols 参数为空")

    # 先 lazy 计数（只读 cap+1 行）再物化 —— 超限 422，不先吃满内存
    lf = read_daily(syms, start=start, end=end)
    n = lf.head(_EXPORT_ROW_CAP + 1).select(pl.len()).collect().item()
    if n > _EXPORT_ROW_CAP:
        raise HTTPException(422, f"导出行数 {n:,} 超上限 {_EXPORT_ROW_CAP:,}，"
                                 "请收窄标的或日期区间")
    if not n:
        raise HTTPException(404, "区间内无数据可导出")

    df = lf.collect()

    stem = "daily"
    if start or end:
        stem += f"_{(start or '').replace('-', '')}_{(end or '').replace('-', '')}"
    df = df.sort(["symbol", "trade_date"])

    if fmt == "csv":
        buf = io.BytesIO()
        df.write_csv(buf, include_bom=True)  # utf-8-sig，Excel 直开不乱码
        media, ext = "text/csv; charset=utf-8-sig", "csv"
        payload = buf.getvalue()
    else:
        buf = io.BytesIO()
        df.write_parquet(buf, compression="zstd")
        media, ext = "application/octet-stream", "parquet"
        payload = buf.getvalue()

    fname = re.sub(r"[^A-Za-z0-9._-]", "", f"{stem}.{ext}") or f"export.{ext}"
    return StreamingResponse(
        iter([payload]),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


def bare_code_or_full(s: str) -> str:
    """导出 symbols 接受两种写法：600519 / 600519.SH 都透传成湖内格式。

    湖内 symbol 带交易所后缀；纯 6 位数字按沪深规则补后缀。
    """
    s = s.strip()
    if "." in s:
        return s.upper()
    from lquant.server.deps import bare_code as _bc

    code = _bc(s)
    if code.startswith(("6", "9", "5")):
        return f"{code}.SH"
    return f"{code}.SZ"


# ---------- 5. 数据字典 ----------

@router.get("/dictionary")
def data_dictionary() -> dict:
    """静态数据字典：各表 schema 与中文字段说明，按表分组。"""
    from lquant.data.dictionary import dictionary

    return dictionary()
