"""行业资讯 API（/news）：items/industries/sources/tasks/summary。

读走 reader()，写走 writer()。两个 DDL（news_item / news_task）懒建：
首次访问在连接上 CREATE TABLE IF NOT EXISTS（仿 ensure_collect_log 的惰性迁移模式），
按 duckdb_path 缓存避免每次请求重复建表。
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from fastapi import HTTPException, Query
from pydantic import BaseModel

from lquant.core.db import reader, writer
from lquant.core.types import today_cn
from lquant.server.envelope import make_router

router = make_router(prefix="/news", tags=["news"])

_LOG = logging.getLogger(__name__)

# 已建表标记，key 为 duckdb_path（测试/多环境切换时重新建表）
_schema_ready: set[str] = set()


def _ensure_schema() -> None:
    """懒建 news_item / news_task 表（幂等，CREATE TABLE IF NOT EXISTS）。

    先在 reader 连接上尝试（同实例缓存下可写）；失败则降级 writer()，
    保持与 ensure_collect_log 等启动迁移一致的容错姿态。
    """
    from lquant.core.config import get_settings

    path_key = str(Path(get_settings().duckdb_path).resolve())
    if path_key in _schema_ready:
        return
    from lquant.news.store import init_news_ddl
    from lquant.news.tasks import init_news_task_ddl

    try:
        with reader() as con:
            init_news_ddl(con)
            init_news_task_ddl(con)
    except Exception:  # noqa: BLE001 - reader 无建表权限 → writer 兜底
        _LOG.warning("news DDL 在 reader 上建表失败，降级 writer()", exc_info=True)
        with writer() as con:
            init_news_ddl(con)
            init_news_task_ddl(con)
    _schema_ready.add(path_key)


def _load_industry_names() -> dict[str, str]:
    """news.yaml 的 industry_names；配置缺失降级空映射。"""
    try:
        from lquant.core.config import load_yaml

        return load_yaml("schema/news.yaml").get("industry_names", {}) or {}
    except (FileNotFoundError, AttributeError, TypeError, ValueError):
        _LOG.warning("news.yaml 缺失，industry_names 降级为空映射")
        return {}


def _registry_rows() -> list[dict]:
    """来源注册表清单（不实例化 fetch）。"""
    from lquant.news.sources.base import get_sources

    return [{"source": s.name, "category": s.category}
            for s in sorted(get_sources(None), key=lambda s: s.name)]


def _parse_day_or_422(day: str | None) -> str | None:
    if day in (None, ""):
        return None
    try:
        return date.fromisoformat(day).isoformat()
    except (TypeError, ValueError):
        raise HTTPException(422, f"day 参数需为 YYYY-MM-DD 格式，收到 {day!r}") from None


# ---------- GET /items ----------

@router.get("/items")
def news_items(  # noqa: PLR0917 - Query 参数按 brief 固定

    source: str | None = Query(default=None),
    industry: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
    day: str | None = Query(default=None),
    keyword: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=0, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """资讯条目多维查询（source/industry/symbol/day/keyword 过滤，分页）。"""
    day = _parse_day_or_422(day)
    with reader() as con:
        _ensure_schema()
        from lquant.news.store import query_news

        return query_news(
            con, source=source, industry=industry, symbol=symbol, day=day,
            keyword=keyword, limit=limit, offset=offset,
        )


# ---------- GET /industries ----------

@router.get("/industries")
def news_industries() -> list[dict]:
    """行业统计（news_item 按 industry_code 计数）+ news.yaml 中文名。"""
    stats = []
    with reader() as con:
        _ensure_schema()
        from lquant.news.store import news_stats_by_industry

        stats = news_stats_by_industry(con)
    names = _load_industry_names()
    return [{**row, "industry_name": names.get(row["industry_code"])}
            for row in stats]


# ---------- GET /sources ----------

@router.get("/sources")
def news_sources() -> list[dict]:
    """来源统计与注册表并集：0 行的注册来源也出现（count=0）。"""
    stats = []
    with reader() as con:
        _ensure_schema()
        from lquant.news.store import news_stats_by_source_name

        stats = news_stats_by_source_name(con)
    by_source = {r["source_name"]: r for r in stats}
    out: list[dict] = []
    for reg in _registry_rows():
        stat = by_source.pop(reg["source"], None)
        if stat is not None:
            out.append({**reg, **stat, "category": reg["category"],
                        "count": stat["count"],
                        "last_collected_at": _ts(stat.get("last_collected_at"))})
        else:
            out.append({**reg, "source_name": None, "count": 0,
                        "last_collected_at": None})
    # 库里有但注册表已下线的来源也保留（历史数据可查）
    out.extend({**r, "source": r["source_name"], "category": None,
                "last_collected_at": _ts(r.get("last_collected_at"))}
               for r in by_source.values())
    return out


def _ts(v) -> str | None:
    return v.isoformat() if v is not None else None


# ---------- GET /tasks ----------

@router.get("/tasks")
def news_tasks(limit: int = Query(default=20, ge=1, le=100)) -> list[dict]:
    """最近采集任务行（started_at 倒序）。"""
    with reader() as con:
        _ensure_schema()
        rows = con.execute(
            "SELECT task_id, kind, params, status, sources_status, rows_written,"
            " started_at, finished_at, message FROM news_task"
            " ORDER BY started_at DESC NULLS LAST LIMIT ?",
            [limit],
        ).fetchall()
    out = []
    for r in rows:
        try:
            params = json.loads(r[2]) if r[2] else {}
            sources_status = json.loads(r[4]) if r[4] else {}
        except (ValueError, TypeError):
            params, sources_status = {}, {}
        out.append({
            "task_id": r[0], "kind": r[1], "params": params, "status": r[3],
            "sources_status": sources_status, "rows_written": r[5],
            "started_at": _ts(r[6]), "finished_at": _ts(r[7]), "message": r[8],
        })
    return out


# ---------- POST /tasks ----------

class NewsTaskCreate(BaseModel):
    kind: str = "manual"
    date: str | None = None
    sources: list[str] | None = None


@router.post("/tasks")
def create_news_task(body: NewsTaskCreate) -> dict:
    """创建并同步执行采集任务（M1 无后台线程）；冲突 409，参数非法 422。"""
    day = _parse_day_or_422(body.date)
    known = {r["source"] for r in _registry_rows()}
    sources = sorted(body.sources) if body.sources else sorted(known)
    unknown = set(sources) - known
    if unknown:
        raise HTTPException(422, f"未知资讯来源: {sorted(unknown)}")

    from lquant.news.tasks import TaskConflictError, create_task, execute_task

    params: dict = {}
    if day is not None:
        params["date"] = day
    params["sources"] = sources
    try:
        with writer() as con:
            _ensure_schema()
            task = create_task(con, body.kind, params)
            result = execute_task(con, task["task_id"])
    except TaskConflictError as e:
        raise HTTPException(409, str(e)) from None
    except ValueError as e:  # invalid kind 等
        raise HTTPException(422, str(e)) from None
    return result


# ---------- POST /tasks/{task_id}/retry ----------

@router.post("/tasks/{task_id}/retry")
def retry_news_task(task_id: str) -> dict:
    """重试任务的 failed source；非 partial/failed/interrupted → 409，不存在 → 404。"""
    from lquant.news.tasks import TaskConflictError, retry_task

    try:
        with writer() as con:
            _ensure_schema()
            return retry_task(con, task_id)
    except TaskConflictError as e:
        raise HTTPException(409, str(e)) from None
    except ValueError as e:  # task not found
        raise HTTPException(404, str(e)) from None


# ---------- GET /summary ----------

@router.get("/summary")
def news_summary(day: str | None = Query(default=None)) -> dict:
    """当日计数：by source / by category / 行业 top10；空数据返回空列表不 500。"""
    # 缺省日走 today_cn()：资讯按业务日归档，服务器时区非 Asia/Shanghai
    # 时 date.today() 会让「今天」整体错位一天（凌晨时段的请求尤其明显）
    day = _parse_day_or_422(day) or today_cn().isoformat()
    with reader() as con:
        _ensure_schema()
        rows = con.execute(
            "SELECT source_name, count(*) FROM news_item"
            " WHERE CAST(published_at AS DATE) = CAST(? AS DATE) GROUP BY source_name",
            [day],
        ).fetchall()
        ind_rows = con.execute(
            "SELECT industry_code, count(*) FROM news_item"
            " WHERE industry_code IS NOT NULL"
            " AND CAST(published_at AS DATE) = CAST(? AS DATE)"
            " GROUP BY industry_code ORDER BY count(*) DESC LIMIT 10",
            [day],
        ).fetchall()

    by_source = [{"source": s, "count": c} for s, c in rows]
    reg_cat = {r["source"]: r["category"] for r in _registry_rows()}
    by_cat: dict[str, int] = {}
    for r in by_source:
        cat = reg_cat.get(r["source"])
        if cat is not None:
            by_cat[cat] = by_cat.get(cat, 0) + r["count"]
    return {
        "day": day,
        "by_source": by_source,
        "by_category": [{"category": c, "count": n}
                        for c, n in sorted(by_cat.items())],
        "top_industries": [{"industry_code": code, "count": n}
                           for code, n in ind_rows],
    }
