"""qlib 运行元数据存储（sqlite，独立于 DuckDB主库，模式参照 paper/store.py）。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS qlib_run(
  id TEXT PRIMARY KEY,
  config TEXT NOT NULL,
  market TEXT,
  exp_name TEXT NOT NULL,
  status TEXT NOT NULL,
  metrics TEXT,
  config_snapshot TEXT,
  log_path TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  error TEXT
);
"""


def db_path() -> Path:
    env = os.getenv("LQ_QLIB_DB")
    if env:
        p = Path(env)
    else:
        from lquant.core.config import get_settings

        p = Path(get_settings().parquet_dir).parent / "qlib" / "qlib_runs.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(db_path(), timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    return c


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["metrics"] = json.loads(d["metrics"]) if d["metrics"] else None
    return d


def create_run(config: str, market: str | None, exp_name: str,
               config_snapshot: str) -> dict:
    rid = uuid.uuid4().hex[:12]
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO qlib_run(id, config, market, exp_name, status,"
            " config_snapshot, created_at) VALUES(?,?,?,?,?,?,?)",
            (rid, config, market, exp_name, "queued", config_snapshot, _now()))
    return get_run(rid)


def update_run(run_id: str, *, status: str | None = None,
               metrics: dict | None = None, log_path: str | None = None,
               error: str | None = None) -> None:
    sets, vals = [], []
    if status is not None:
        sets.append("status=?")
        vals.append(status)
    if metrics is not None:
        sets.append("metrics=?")
        vals.append(json.dumps(metrics, ensure_ascii=False))
    if log_path is not None:
        sets.append("log_path=?")
        vals.append(log_path)
    if error is not None:
        sets.append("error=?")
        vals.append(error)
    if status in ("finished", "failed", "canceled"):
        sets.append("finished_at=?")
        vals.append(_now())
    if not sets:
        return
    vals.append(run_id)
    with _LOCK, _conn() as c:
        cur = c.execute(f"UPDATE qlib_run SET {', '.join(sets)} WHERE id=?", vals)
        if cur.rowcount == 0:
            raise ValueError(f"qlib_run 不存在: {run_id}")


def get_run(run_id: str) -> dict | None:
    with _LOCK, _conn() as c:
        row = c.execute("SELECT * FROM qlib_run WHERE id=?", (run_id,)).fetchone()
    return _to_dict(row) if row else None


def list_runs(limit: int = 50, status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM qlib_run"
    args: list = []
    if status:
        sql += " WHERE status=?"
        args.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with _LOCK, _conn() as c:
        rows = c.execute(sql, args).fetchall()
    return [_to_dict(r) for r in rows]
