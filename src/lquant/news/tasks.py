"""news_task 状态机:创建/执行/重试/互斥/启动恢复。

把 news sources 采集器(Task 3/4)串成任务:
- create_task:登记任务(pending),存在 pending/running → TaskConflictError
- execute_task:逐 source 采集 + link 管线 + 去重入库,单 source 失败不扩散
- retry_task:只重跑 sources_status 里 failed 的 source
- mark_interrupted_on_startup:进程启动时把 pending/running 残留标记为 interrupted
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from duckdb import DuckDBPyConnection

from lquant.core.config import load_yaml
from lquant.core.types import today_cn
from lquant.news.link import build_name_to_code, link_industry, link_symbols
from lquant.news.model import NewsItem
from lquant.news.sources.base import get_sources
from lquant.news.store import insert_news

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS news_task (
    task_id        VARCHAR PRIMARY KEY,
    kind           VARCHAR,
    params         JSON,
    status         VARCHAR,
    sources_status JSON,
    rows_written   INTEGER,
    started_at     TIMESTAMP,
    finished_at    TIMESTAMP,
    message        VARCHAR
)
"""

_VALID_KINDS = ("daily", "manual")
Runner = Callable[[date, str], list[NewsItem]]


class TaskConflictError(ValueError):
    """任务互斥冲突或非法状态流转(继承 ValueError 便于 API 层映射 409/400)。"""


def init_news_task_ddl(con: DuckDBPyConnection) -> None:
    con.execute(_DDL)


def get_task(con: DuckDBPyConnection, task_id: str) -> dict:
    """读取任务行,JSON 列反序列化;不存在抛 ValueError。"""
    row = con.execute(
        "SELECT task_id, kind, params, status, sources_status, rows_written,"
        " started_at, finished_at, message FROM news_task WHERE task_id = ?",
        [task_id],
    ).fetchone()
    if row is None:
        raise ValueError(f"task not found: {task_id}")
    return {
        "task_id": row[0],
        "kind": row[1],
        "params": json.loads(row[2]) if row[2] else {},
        "status": row[3],
        "sources_status": json.loads(row[4]) if row[4] else {},
        "rows_written": row[5],
        "started_at": row[6].isoformat() if row[6] else None,
        "finished_at": row[7].isoformat() if row[7] else None,
        "message": row[8],
    }


def create_task(con: DuckDBPyConnection, kind: str, params: dict[str, Any]) -> dict:
    """登记采集任务。存在 pending/running 任务时抛 TaskConflictError。"""
    if kind not in _VALID_KINDS:
        raise ValueError(f"invalid kind: {kind!r}, must be one of {_VALID_KINDS}")

    existing = con.execute(
        "SELECT task_id FROM news_task WHERE status IN ('pending', 'running')"
    ).fetchall()
    if existing:
        raise TaskConflictError(f"another news task is pending/running: {existing[0][0]}")

    task_id = f"news_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    con.execute(
        "INSERT INTO news_task VALUES (?, ?, ?, 'pending', ?, NULL, NULL, NULL, NULL)",
        [task_id, kind, json.dumps(params), json.dumps({})],
    )
    return get_task(con, task_id)


def _default_runner(day: date, src: str) -> list[NewsItem]:
    """默认采集 runner:从注册表构造 source 并 fetch。

    模块级函数,Task 6 测试的 monkeypatch 锚点。
    """
    (source,) = get_sources([src])
    return source.fetch(day)


def _parse_day(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _load_link_inputs(
    con: DuckDBPyConnection,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """一次性加载 link 管线输入:(n2c, kw_map, s2i),全部可降级。

    - n2c:securities() 失败(网络/接口异常)→ 降级 {} + warning,不中断任务
    - kw_map:news.yaml 的 industry_keywords,配置缺失降级 {}
    - s2i:industry_classify 每股 max(std_date) 行;表缺失/查询异常降级 {}
    """
    n2c: dict[str, str] = {}
    try:
        from lquant.data.providers.akshare import AkShareProvider  # noqa: PLC0415

        n2c = build_name_to_code(AkShareProvider().securities())
    except Exception:  # noqa: BLE001 - 词表不可用时降级为空映射
        logger.warning("securities() 加载失败,个股关联降级为空词表", exc_info=True)

    kw_map: dict[str, str] = {}
    try:
        kw_map = load_yaml("schema/news.yaml").get("industry_keywords", {}) or {}
    except (FileNotFoundError, AttributeError, TypeError, ValueError):
        logger.warning("news.yaml 缺失,行业关键词关联降级为空映射")


    s2i: dict[str, str] = {}
    try:
        rows = con.execute(
            """
            SELECT symbol, code FROM industry_classify
            WHERE (symbol, std_date) IN (
                SELECT symbol, max(std_date) FROM industry_classify GROUP BY symbol)
            """
        ).fetchall()
        s2i = {str(r[0]): str(r[1]) for r in rows}
    except Exception:  # noqa: BLE001 - 分类表缺失时降级为空映射
        logger.warning("industry_classify 查询失败,行业反查降级为空映射", exc_info=True)

    return n2c, kw_map, s2i


def _run_sources(  # noqa: PLR0917 - 内部聚合函数,参数按管线语义排列
    con: DuckDBPyConnection,
    day: date,
    sources: list[str],
    runner: Runner,
    n2c: dict[str, str],
    kw_map: dict[str, str],
    s2i: dict[str, str],
) -> tuple[dict[str, dict[str, Any]], int]:
    """逐 source 采集 + link + 入库;单 source 失败记明细,不扩散。"""
    sources_status: dict[str, dict[str, Any]] = {}
    rows_written = 0
    for src in sources:
        try:
            items = runner(day, src)
            linked: list[NewsItem] = []
            for it in items:
                x = it if it.symbols else link_symbols(it, n2c)
                x = x if x.industry_code else link_industry(x, kw_map, s2i)
                linked.append(x)
            rows = insert_news(con, linked)
            sources_status[src] = {"status": "ok", "rows": rows}
            rows_written += rows
        except Exception as e:  # noqa: BLE001 - 单 source 失败记明细,不扩散
            sources_status[src] = {"status": "failed", "rows": 0, "error": str(e)}
    return sources_status, rows_written


def _aggregate(statuses: dict[str, dict[str, Any]]) -> str:
    """聚合状态:全 ok→ok / 全挂→failed / 混合→partial。"""
    flags = {v["status"] for v in statuses.values()}
    if not flags or flags == {"ok"}:
        return "ok"
    if flags == {"failed"}:
        return "failed"
    return "partial"


def execute_task(con: DuckDBPyConnection, task_id: str, runner: Runner | None = None) -> dict:
    """执行任务。仅 pending 可执行;非 pending 抛 TaskConflictError。"""
    task = get_task(con, task_id)
    if task["status"] != "pending":
        raise TaskConflictError(
            f"task {task_id} status is {task['status']!r}, only pending can execute"
        )
    con.execute(
        "UPDATE news_task SET status='running', started_at=? WHERE task_id=?",
        [datetime.now(), task_id],
    )

    params = task["params"]
    sources = list(params.get("sources") or [])
    # 业务日期用 today_cn()：容器/服务器时区非 Asia/Shanghai 时
    # date.today() 会与交易日错位一天（core/types 的业务日期约定）
    day = _parse_day(params.get("date")) or today_cn()
    fetch: Runner = runner if runner is not None else _default_runner

    n2c, kw_map, s2i = _load_link_inputs(con)
    sources_status, rows_written = _run_sources(
        con, day, sources, fetch, n2c, kw_map, s2i
    )
    return _finish(con, task_id, sources_status, rows_written)


def retry_task(con: DuckDBPyConnection, task_id: str, runner: Runner | None = None) -> dict:
    """只重跑 sources_status 里 failed 的 source;非 partial/failed/interrupted 拒绝。"""
    task = get_task(con, task_id)
    if task["status"] not in ("partial", "failed", "interrupted"):
        raise TaskConflictError(
            f"task {task_id} status is {task['status']!r},"
            " only partial/failed/interrupted can retry"
        )
    prev = task.get("sources_status") or {}
    retry_sources = [s for s, v in prev.items() if v.get("status") == "failed"]
    if not retry_sources:
        retry_sources = list((task.get("params") or {}).get("sources") or [])

    con.execute(
        "UPDATE news_task SET status='running', sources_status=? WHERE task_id=?",
        [json.dumps({}), task_id],
    )

    params = task["params"]
    day = _parse_day(params.get("date")) or today_cn()
    fetch: Runner = runner if runner is not None else _default_runner
    n2c, kw_map, s2i = _load_link_inputs(con)
    sources_status, rows_written = _run_sources(
        con, day, retry_sources, fetch, n2c, kw_map, s2i
    )
    return _finish(con, task_id, sources_status, rows_written)


def _finish(con: DuckDBPyConnection, task_id: str, sources_status, rows_written):  # type: ignore[no-untyped-def]
    """聚合状态并落库,返回任务结果 dict。"""
    status = _aggregate(sources_status)
    finished_at = datetime.now()
    con.execute(
        "UPDATE news_task SET status=?, sources_status=?, rows_written=?,"
        " finished_at=?, message=? WHERE task_id=?",
        [status, json.dumps(sources_status), rows_written, finished_at,
         f"{rows_written} rows from {len(sources_status)} sources", task_id],
    )
    return {
        "task_id": task_id,
        "status": status,
        "rows_written": rows_written,
        "sources_status": sources_status,
        "finished_at": finished_at.isoformat(),
    }


def mark_interrupted_on_startup(con: DuckDBPyConnection) -> int:
    """启动恢复:pending/running 残留 → interrupted,返回受影响行数。"""
    n = con.execute(
        "SELECT count(*) FROM news_task WHERE status IN ('pending', 'running')"
    ).fetchone()[0]
    con.execute(
        "UPDATE news_task SET status='interrupted',"
        " finished_at=? WHERE status IN ('pending', 'running')",
        [datetime.now()],
    )
    return n
