"""任务结果持久化：job_results 表。

评价 / 扫描类队列任务的结果落库（INSERT OR REPLACE，job_id 主键），
跨重启可查 —— 任务中心「查看结果」与 REST 查询共用。
"""
from __future__ import annotations

import json
import threading
from typing import Any

import duckdb

from lquant.core.db import reader, writer

_DDL = """
CREATE TABLE IF NOT EXISTS job_results (
  job_id VARCHAR PRIMARY KEY,
  ts TIMESTAMP,
  kind VARCHAR,
  params JSON,
  result JSON
)
"""

_DDL_DONE = False
_DDL_LOCK = threading.Lock()


def _ensure_table(con) -> None:
    """job_results 建表（幂等）。按连接查 information_schema，而非进程级旗标 ——
    进程内可能先后连接不同的 duckdb 文件（测试 tmp 库 / 多环境），旗标会漏建。"""
    exists = con.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'job_results'"
    ).fetchone()[0]
    if not exists:
        con.execute(_DDL)


def save_result(job_id: str, job_kind: str, params: dict, result: Any) -> None:
    """保存任务结果（幂等覆盖）。"""
    with writer() as con:
        _ensure_table(con)
        con.execute(
            "INSERT OR REPLACE INTO job_results VALUES (?, now(), ?, ?, ?)",
            [job_id, job_kind, json.dumps(params, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False)])


def get_result(job_id: str) -> dict | None:
    """按 job_id 取结果；不存在返回 None。

    只吞「表不存在」（Catalog），锁冲突等 IOException 放行 —— 让 API 层的
    503 映射生效，而不是把数据库错误伪装成 404。
    """
    try:
        with reader() as con:
            rows = con.execute(
                "SELECT job_id, epoch_ms(ts), kind, params, result "
                "FROM job_results WHERE job_id = ?", [job_id]).fetchall()
    except duckdb.CatalogException:
        return None
    if not rows:
        return None
    job_id_, ts, kind, params, result = rows[0]
    return {"job_id": job_id_, "ts": ts / 1000.0 if ts else None, "kind": kind,
            "params": json.loads(params) if params else {},
            "result": json.loads(result) if result else None}


def list_results(kind: str, limit: int = 50) -> list[dict]:
    """最近 N 条结果（按时间倒序），任务中心面板「历史结果」用。"""
    try:
        with reader() as con:
            rows = con.execute(
                "SELECT job_id, epoch_ms(ts), kind, params, result "
                "FROM job_results WHERE kind = ? ORDER BY ts DESC LIMIT ?",
                [kind, limit]).fetchall()
    except duckdb.CatalogException:
        return []  # 表未建：无结果
    return [{"job_id": jid, "ts": (ts / 1000.0 if ts else None), "kind": kind,
             "params": json.loads(p) if p else {},
             "result": json.loads(r) if r else None}
            for jid, ts, _k, p, r in rows]
