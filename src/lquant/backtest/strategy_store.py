"""策略库 / 自定义分析库（DuckDB）。版本化：同名保存 = 插入新行。"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from lquant.core.db import reader, writer


def _now() -> datetime:
    return datetime.now()


def save_strategy(name: str, source: str, *, description: str = "",
                  config: dict | None = None, benchmark: str | None = None) -> dict:
    from lquant.backtest.validation import validate_source
    errs = validate_source(source)
    if errs:
        raise ValueError("；".join(errs))
    with writer() as con:
        row = con.execute(
            "SELECT COALESCE(MAX(version), 0) FROM strategy_def WHERE name = ?",
            [name]).fetchone()
        ver = int(row[0]) + 1
        sid = uuid.uuid4().hex[:12]
        now = _now()
        con.execute("UPDATE strategy_def SET is_latest = FALSE WHERE name = ?", [name])
        con.execute(
            "INSERT INTO strategy_def VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [sid, name, description, "jq", source,
             json.dumps(config or {}), benchmark, json.dumps({}),
             ver, True, False, now, now])
    return get_strategy(sid)


def list_strategies() -> list[dict]:
    with reader() as con:
        rows = con.execute(
            "SELECT id, name, description, kind, version, updated_at "
            "FROM strategy_def WHERE is_latest AND NOT deleted "
            "ORDER BY updated_at DESC").fetchall()
    return [{"id": r[0], "name": r[1], "description": r[2], "kind": r[3],
             "version": r[4], "updated_at": str(r[5])} for r in rows]


def get_strategy(strategy_id: str) -> dict:
    with reader() as con:
        row = con.execute(
            "SELECT id, name, description, kind, source, params_json, benchmark, "
            "config_json, version, is_latest, updated_at FROM strategy_def WHERE id = ?",
            [strategy_id]).fetchone()
    if not row:
        raise KeyError(strategy_id)
    return {"id": row[0], "name": row[1], "description": row[2], "kind": row[3],
            "source": row[4], "config": json.loads(row[5] or "{}"),
            "benchmark": row[6], "config_json": json.loads(row[7] or "{}"),
            "version": row[8], "is_latest": row[9], "updated_at": str(row[10])}


def list_versions(name: str) -> list[dict]:
    with reader() as con:
        rows = con.execute(
            "SELECT id, version, updated_at, is_latest FROM strategy_def "
            "WHERE name = ? AND NOT deleted ORDER BY version DESC", [name]).fetchall()
    return [{"id": r[0], "version": r[1], "updated_at": str(r[2]), "is_latest": r[3]}
            for r in rows]


def delete_strategy(strategy_id: str) -> None:
    with writer() as con:
        con.execute("UPDATE strategy_def SET deleted = TRUE WHERE id = ?", [strategy_id])


def save_analysis(name: str, source: str) -> dict:
    from lquant.backtest.validation import validate_source
    errs = validate_source(source, require_initialize=False)
    if errs:
        raise ValueError("；".join(errs))
    with writer() as con:
        aid = uuid.uuid4().hex[:12]
        now = _now()
        con.execute(
            "INSERT INTO analysis_def (id, name, source, is_builtin, deleted, "
            "created_at, updated_at) VALUES (?,?,?,FALSE,FALSE,?,?)",
            [aid, name, source, now, now])
    return get_analysis(aid)


def list_analyses() -> list[dict]:
    with reader() as con:
        rows = con.execute(
            "SELECT id, name, created_at, updated_at FROM analysis_def "
            "WHERE NOT deleted ORDER BY updated_at DESC").fetchall()
    return [{"id": r[0], "name": r[1], "created_at": str(r[2]),
             "updated_at": str(r[3])} for r in rows]


def get_analysis(analysis_id: str) -> dict:
    with reader() as con:
        row = con.execute(
            "SELECT id, name, source, is_builtin, created_at, updated_at "
            "FROM analysis_def WHERE id = ? AND NOT deleted",
            [analysis_id]).fetchone()
    if not row:
        raise KeyError(analysis_id)
    return {"id": row[0], "name": row[1], "source": row[2], "is_builtin": row[3],
            "created_at": str(row[4]), "updated_at": str(row[5])}


def delete_analysis(analysis_id: str) -> None:
    with writer() as con:
        con.execute("UPDATE analysis_def SET deleted = TRUE WHERE id = ?",
                    [analysis_id])
