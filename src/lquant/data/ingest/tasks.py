"""数据任务：data_task 表 + 执行器（全量回填 / 每日增量）。

职责：
- create_task：前置校验（退市股/单任务互斥）+ 回填池构建 + 落 pending 行
- execute_task：pending→running→ok/partial/failed 状态机；stocks→etf 两个 phase，
  进度逐批 UPDATE 进 data_task（DuckDB 单写者，写收敛 writer()）
- retry_task：unmark 失败标的的 checkpoint 后重新 execute（只补漏）
- mark_interrupted_on_startup：服务重启把 pending/running 残留标 interrupted

消费 T3 的 backfill_pool（逐批回填 + on_progress 累计快照帧），
checkpoint 名 f"daily:{task_id}"，与旧 "daily" 哨兵池隔离。
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta
from typing import Any

import duckdb

from lquant.core.db import reader, writer
from lquant.data.ingest.checkpoint import Checkpoint
from lquant.data.ingest.daily import backfill_pool

_DDL = """
CREATE TABLE IF NOT EXISTS data_task (
    task_id        VARCHAR PRIMARY KEY,
    kind           VARCHAR,
    params         JSON,
    status         VARCHAR,
    phase          VARCHAR,
    total_symbols  INTEGER,
    done_symbols   INTEGER,
    failed_symbols JSON,
    failed_detail  JSON,
    rows_written   INTEGER,
    started_at     TIMESTAMP,
    finished_at    TIMESTAMP,
    message        VARCHAR
)
"""

# 状态集合
_RUNNING_STATES = ("pending", "running")
_RETRIABLE = ("partial", "failed", "interrupted")
_KINDS = ("full_backfill", "daily_update")
PHASES = ("stocks", "etf")

_FULL_START_DEFAULT = "2016-01-01"
_DAILY_DAYS_DEFAULT = 10
_CP_PREFIX = "daily:"


class TaskConflictError(ValueError):
    """任务状态冲突（互斥 / 不可 retry / 正在运行）—— API 层映射 409。

    继承 ValueError：旧调用方按 ValueError 捕获的行为不变。
    """


def _ensure_table(con) -> None:
    con.execute(_DDL)


def _parse_date(raw: Any, label: str) -> date:
    if isinstance(raw, date):
        return raw
    if not isinstance(raw, str):
        raise ValueError(f"{label}格式应为 YYYY-MM-DD，收到: {raw!r}")
    try:
        return date.fromisoformat(raw)
    except ValueError as e:
        raise ValueError(f"{label}格式应为 YYYY-MM-DD，收到: {raw!r}") from e


def _resolve_range(kind: str, p: dict) -> tuple[date, date]:
    """解析时间范围：daily_update 默认回看 days 天；start 显式给出时优先。"""
    today = date.today()
    if "start" in p and p["start"] is not None:
        start = _parse_date(p["start"], "起始日期")
    elif kind == "daily_update":
        days = p.get("days", _DAILY_DAYS_DEFAULT)
        if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
            raise ValueError(f"days 必须为正整数，收到: {days!r}")
        start = today - timedelta(days=days)
    else:
        start = _parse_date(_FULL_START_DEFAULT, "起始日期")
    end_raw = p.get("end")
    end = _parse_date(end_raw, "结束日期") if end_raw else today
    if start > end:
        raise ValueError(f"起始日期 {start} 晚于结束日期 {end}")
    return start, end


def _pool_from_con(con, kind: str, start: date, end: date) -> dict[str, list[tuple[str, date]]]:
    """在给定连接上构建回填池（避免 writer 内嵌套 reader）。

    full_backfill：股票（含退市，end=min(end, delist_date)）+ ETF；
    daily_update：在市股票 + ETF。返回 {"stocks": [...], "etf": [...]}，
    顺序 stocks→etf 与执行 phase 一致。
    """
    if kind == "full_backfill":
        rows = con.execute(
            "SELECT symbol, delist_date FROM security "
            "WHERE sec_type NOT IN ('etf', 'lof') ORDER BY symbol"
        ).fetchall()
        stocks = [(sym, min(end, d) if d else end) for sym, d in rows]
    else:
        rows = con.execute(
            "SELECT symbol FROM security "
            "WHERE sec_type NOT IN ('etf', 'lof') "
            "AND (delist_date IS NULL OR delist_date > CURRENT_DATE) "
            "ORDER BY symbol"
        ).fetchall()
        stocks = [(sym, end) for (sym,) in rows]
    etf_rows = con.execute(
        "SELECT symbol FROM security WHERE sec_type IN ('etf', 'lof') ORDER BY symbol"
    ).fetchall()
    etf = [(sym, end) for (sym,) in etf_rows]
    return {"stocks": stocks, "etf": etf}


def _pool_with_ends(kind: str, start: date, end: date) -> list[tuple[str, date]]:
    """回填池（stocks+etf 顺序列表，退市股 end 截断）。"""
    with reader() as con:
        phases = _pool_from_con(con, kind, start, end)
    return phases["stocks"] + phases["etf"]


def create_task(kind: str, params: dict | None = None) -> dict:
    """创建数据任务（pending）。前置校验失败抛 ValueError。"""
    if kind not in _KINDS:
        raise ValueError(f"未知任务类型: {kind!r}（可选 {_KINDS}）")
    p = dict(params or {})
    start, end = _resolve_range(kind, p)
    stored = {
        "start": str(start),
        "end": str(end),
        "days": p.get("days"),
        "market": p.get("market"),
        "auto_crosscheck": p.get("auto_crosscheck", True),
    }
    with writer() as con:
        _ensure_table(con)
        running = con.execute(
            "SELECT task_id FROM data_task "
            "WHERE status = 'running' LIMIT 1"
        ).fetchone()
        if running:
            raise TaskConflictError(
                f"已有运行中的数据任务（{running[0]}），请等待完成后再创建")
        if kind == "full_backfill":
            n_delisted = con.execute(
                "SELECT count(*) FROM security "
                "WHERE delist_date IS NOT NULL AND sec_type NOT IN ('etf', 'lof')"
            ).fetchone()[0]
            if n_delisted == 0:
                raise ValueError(
                    "security 表没有退市股 —— 全量回填需含退市标的，"
                    "请先跑 `lq data reference` 同步标的清单"
                )
        pool = _pool_from_con(con, kind, start, end)
        total = len(pool["stocks"]) + len(pool["etf"])
        if total == 0:
            raise ValueError("回填池为空 —— security 表没有可用标的，请先跑 `lq data reference`")
        task_id = uuid.uuid4().hex[:12]
        con.execute(
            "INSERT INTO data_task (task_id, kind, params, status, total_symbols, "
            "done_symbols, failed_symbols, failed_detail, rows_written) "
            "VALUES (?, ?, ?::JSON, 'pending', ?, 0, ?::JSON, ?::JSON, 0)",
            [task_id, kind, json.dumps(stored, ensure_ascii=False),
             total, "[]", "[]"],
        )
    return get_task(task_id)


def _reset_running(task_id: str) -> None:
    """进入 running：清零计数（retry 沿用同一行，重新计数）。"""
    with writer() as con:
        con.execute(
            "UPDATE data_task SET status='running', phase=NULL, done_symbols=0, "
            "failed_symbols=?::JSON, failed_detail=?::JSON, rows_written=0, "
            "started_at=?, finished_at=NULL, message=NULL "
            "WHERE task_id=?",
            ["[]", "[]", datetime.now(), task_id],
        )


def _sync_total(task_id: str, total: int) -> None:
    """以执行时重建的池大小为准，收敛与 create 时的口径漂移。"""
    with writer() as con:
        con.execute(
            "UPDATE data_task SET total_symbols=? WHERE task_id=?",
            [total, task_id],
        )


def _progress_update(task_id, phase, done, failed, rows) -> None:
    """每批进度 UPDATE（backfill_pool 批间调用，不嵌套 reader）。

    failed_symbols 列存契约字符串列表 ["SYM"]；failed_detail 存明细 dict 列表。
    """
    syms = [f["symbol"] for f in failed]
    with writer() as con:
        con.execute(
            "UPDATE data_task SET phase=?, done_symbols=?, "
            "failed_symbols=?::JSON, failed_detail=?::JSON, rows_written=? "
            "WHERE task_id=?",
            [phase, done,
             json.dumps(syms, ensure_ascii=False),
             json.dumps(failed, ensure_ascii=False),
             rows, task_id],
        )


def _finalize(task_id, total, failed_all, early, *, done_count=None, error=None) -> str:
    """按结果定终态：早停/全失败/异常 → failed；有失败 → partial；否则 ok。"""
    failed_syms = sorted({f["symbol"] for f in failed_all})
    done_count = done_count if done_count is not None else total - len(failed_syms)
    if error:
        status = "failed"
        msg = f"执行异常终止：{error}"
    elif early:
        status = "failed"
        msg = f"早停：连续失败过多，已失败 {len(failed_syms)} 只（可用 retry 补漏）"
    elif failed_syms and done_count <= 0:
        done_count = 0
        status = "failed"
        msg = f"全部失败：{len(failed_syms)} 只（可用 retry 补漏）"
    elif failed_syms:
        status = "partial"
        msg = f"完成 {done_count}/{total}，失败 {len(failed_syms)} 只（可用 retry 补漏）"
    else:
        status = "ok"
        msg = f"完成 {done_count}/{total}"
    with writer() as con:
        con.execute(
            "UPDATE data_task SET status=?, done_symbols=?, "
            "failed_symbols=?::JSON, failed_detail=?::JSON, "
            "finished_at=?, message=? WHERE task_id=?",
            [status, done_count,
             json.dumps(failed_syms, ensure_ascii=False),
             json.dumps(failed_all, ensure_ascii=False),
             datetime.now(), msg, task_id],
        )
    return status


def execute_task(task_id: str) -> dict:
    """执行任务：pending/interrupted/failed/partial → running → ok/partial/failed。"""
    task = get_task(task_id)
    if task is None:
        raise ValueError(f"任务不存在: {task_id}")
    if task["status"] == "running":
        raise TaskConflictError(f"任务 {task_id} 正在运行中")
    _reset_running(task_id)
    return _run_task(task_id)


def _run_task(task_id: str) -> dict:
    """执行主体（状态已置 running）：跑池 → 收敛终态。"""
    task = get_task(task_id)
    params = task["params"]
    start = _parse_date(params["start"], "起始日期")
    end = (_parse_date(params["end"], "结束日期") if params.get("end")
           else date.today())
    cp = Checkpoint(f"daily:{task_id}")
    with reader() as con:
        phases = _pool_from_con(con, task["kind"], start, end)
    total = len(phases["stocks"]) + len(phases["etf"])
    _sync_total(task_id, total)
    all_syms = phases["stocks"] + phases["etf"]
    base = {
        "done": sum(1 for s, _ in all_syms if cp.is_done(s)),
        "failed": [],
        "rows": 0,
    }
    early = False
    error: str | None = None
    try:
        for name in PHASES:
            remaining = [(s, e) for s, e in phases[name] if not cp.is_done(s)]
            if not remaining:
                continue
            def cb(frame, _base=base, _name=name):
                _progress_update(
                    task_id, _name, _base["done"] + frame["done"],
                    [*_base["failed"], *frame["failed"]],
                    _base["rows"] + frame["rows"])
            res = backfill_pool(remaining, start, end=end, on_progress=cb,
                                cp_name=_CP_PREFIX + task_id)
            failed_set = {f["symbol"] for f in base["failed"]} | {
                f["symbol"] for f in res["failed"]}
            newly = [s for s, _ in remaining if s not in failed_set]
            cp.mark(newly)
            base = {
                "done": base["done"] + res["done"],
                "failed": [*base["failed"], *res["failed"]],
                "rows": base["rows"] + res["rows"],
            }
            if res["early_stopped"]:
                early = True
                break
    except Exception as e:  # noqa: BLE001 — 执行期异常也落到 failed 终态，不卡 running
        error = f"{type(e).__name__}: {e}"
    done = sum(1 for s, _ in all_syms if cp.is_done(s))
    status = _finalize(task_id, total, base["failed"], early, done_count=done,
                       error=error)
    if status in ("ok", "partial") and params.get("auto_crosscheck", True):
        _auto_crosscheck(task_id, start, end)
    return get_task(task_id)


def _auto_crosscheck(task_id: str, start: date, end: date) -> None:
    """任务收尾自动对拍（§T9）：对本次 [start, end] 窗口跑一次抽样对拍。

    失败只 log 不影响任务终态（对拍不是同步链路的单点故障）；
    e2e/单测环境无 peer 源时 run_crosscheck 回退 L0，同样不算失败。
    summary JSON 追加进 message 字段，任务详情接口可直接展示。
    """
    from loguru import logger

    try:
        from lquant.data.ingest.crosscheck import run_crosscheck

        res = run_crosscheck(start=str(start), end=str(end))
        summary = json.dumps(res.get("summary", {}), ensure_ascii=False)
        with writer() as con:
            con.execute(
                "UPDATE data_task SET message = message || ? WHERE task_id=?",
                [f" | crosscheck: {summary}", task_id],
            )
    except Exception as e:  # noqa: BLE001 — 对拍失败不影响任务终态
        logger.warning(f"任务 {task_id} 自动对拍失败（忽略）: {e}")


def claim_retry(task_id: str) -> dict:
    """原子认领 retry：一条 UPDATE check-and-update 到 running（含计数清零），
    并 unmark 失败标的 checkpoint。

    消除端点「先预检再入队」的 TOCTOU：竞争失败（状态已不是 retriable，
    或已被其他请求抢先）抛 TaskConflictError，不存在抛 ValueError。
    返回认领后的 task。
    """
    task = get_task(task_id)
    if task is None:
        raise ValueError(f"任务不存在: {task_id}")
    with writer() as con:
        _ensure_table(con)
        res = con.execute(
            "UPDATE data_task SET status='running', phase=NULL, done_symbols=0, "
            "failed_symbols='[]'::JSON, failed_detail='[]'::JSON, rows_written=0, "
            "started_at=?, finished_at=NULL, message=NULL "
            "WHERE task_id=? AND status IN ('partial', 'failed', 'interrupted')",
            [datetime.now(), task_id],
        ).fetchone()
    if not res or not res[0]:
        raise TaskConflictError(
            f"任务 {task_id} 状态不可 retry（需 partial/failed/interrupted），"
            "或已被其他操作抢先")
    failed = task["failed_symbols"] or []
    if failed:
        Checkpoint(_CP_PREFIX + task_id).unmark(failed)
    return get_task(task_id)


def retry_task(task_id: str) -> dict:
    """unmark 失败标的的 checkpoint 后重新 execute（只补漏）。

    先经 claim_retry 原子认领（不可 retry / 抢先失败 → TaskConflictError），
    队列里重复认领也会安全失败而不是重复执行。
    """
    claim_retry(task_id)
    return _run_task(task_id)


def run_claimed_task(task_id: str) -> dict:
    """执行已认领（running）的任务 —— 端点 claim_retry 成功后由队列调用。

    与 execute_task 的区别：不做状态前置检查（认领即原子置 running）。
    """
    return _run_task(task_id)


def mark_interrupted_on_startup() -> int:
    """服务重启：把 pending/running 残留标 interrupted，返回条数。"""
    with writer() as con:
        _ensure_table(con)
        stale = con.execute(
            "SELECT task_id FROM data_task WHERE status IN ('pending', 'running')"
        ).fetchall()
        if not stale:
            return 0
        con.execute(
            "UPDATE data_task SET status='interrupted', "
            "message='服务重启，任务中断（可 retry 续传）' "
            "WHERE status IN ('pending', 'running')"
        )
        return len(stale)


def _row_to_task(row) -> dict:
    """DuckDB 行 → task dict（JSON 列解析，新建对象返回）。"""
    (task_id, kind, params, status, phase, total, done, failed, detail,
     rows, started, finished, message) = row
    return {
        "task_id": task_id,
        "kind": kind,
        "params": json.loads(params) if isinstance(params, str) else (params or {}),
        "status": status,
        "phase": phase,
        "total_symbols": total,
        "done_symbols": done,
        "failed_symbols": json.loads(failed) if isinstance(failed, str) else (failed or []),
        "failed_detail": json.loads(detail) if isinstance(detail, str) else (detail or []),
        "rows_written": rows,
        "started_at": started,
        "finished_at": finished,
        "message": message,
    }


_TASK_COLS = ("task_id, kind, params, status, phase, total_symbols, done_symbols, "
              "failed_symbols, failed_detail, rows_written, started_at, "
              "finished_at, message")


def get_task(task_id: str) -> dict | None:
    with reader() as con:
        try:
            row = con.execute(
                f"SELECT {_TASK_COLS} FROM data_task WHERE task_id = ?",
                [task_id],
            ).fetchone()
        except duckdb.CatalogException:
            return None
    return _row_to_task(row) if row else None


def list_tasks(limit: int = 50) -> list[dict]:
    with reader() as con:
        try:
            rows = con.execute(
                f"SELECT {_TASK_COLS} FROM data_task "
                "ORDER BY started_at DESC NULLS LAST, task_id LIMIT ?",
                [limit],
            ).fetchall()
        except duckdb.CatalogException:
            return []
    return [_row_to_task(r) for r in rows]

