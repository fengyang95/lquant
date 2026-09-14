"""质量问题的落库（细化方案 §3.8.5 data_quality_issue）。

标记而非删除：一条坏数据不该丢掉整个批次 —— 问题行打 quality_flags
留在湖里，同时在这里留一条可检索、可标记已解决的 issue。

issue_id 用内容指纹（dataset/rule/symbol/trade_date/detail 的哈希）而不是
随机 UUID —— 同一问题重复检查时 INSERT OR REPLACE 覆盖原行，
issue 表不随重跑无限膨胀，resolve 的结果在下次复查前保持有效。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date

from lquant.core.db import reader, writer
from lquant.data.store.ddl import DDL_DATA_QUALITY_ISSUE as _DDL

__all__ = ["Issue", "save_issues", "latest_issues", "query_issues",
           "resolve_issue", "resolve_issues", "ensure_table"]


@dataclass(frozen=True)
class Issue:
    """一条质量检查结论。"""
    rule: str
    severity: str                      # fatal / error / warn / info
    detail: str
    dataset: str = "daily_bar"
    symbol: str | None = None
    trade_date: date | None = None
    count: int = 0
    extra: dict = field(default_factory=dict)

    def to_row(self, data_version: str | None) -> dict:
        # 内容指纹：同一问题（同规则/标的/日期）重复检出时覆盖而非新增。
        # detail 里的数字先掩码（「3 行…」→「N 行…」），否则计数变化会
        # 生成新 issue_id，issue 表随重跑无限膨胀；非数字文本（如 field 名）
        # 保留区分度。extra 只取标量字段（field/level 等参与区分），
        # 列表/嵌套（dates 等 bulk 数据）不进指纹。
        _digits = re.sub(r"\d+", "N", self.detail)
        scalars = {k: v for k, v in self.extra.items()
                   if isinstance(v, (str, int, float, bool)) or v is None}
        fp = "|".join([
            self.dataset, self.rule, self.symbol or "",
            str(self.trade_date or ""),
            _digits,
            json.dumps(scalars, sort_keys=True, ensure_ascii=False),
        ])
        return {
            "issue_id": hashlib.sha1(fp.encode("utf-8")).hexdigest()[:24],
            "dataset": self.dataset,
            "symbol": self.symbol,
            "trade_date": self.trade_date,
            "rule_code": self.rule,
            "severity": self.severity,
            "detail": json.dumps({"message": self.detail, **self.extra}, ensure_ascii=False),
            "count": self.count,
            "data_version": data_version,
        }


def ensure_table(con) -> None:
    con.execute(_DDL)


def save_issues(issues: list[Issue], data_version: str | None = None) -> int:
    """批量落库；空列表直接返回。"""
    if not issues:
        return 0
    rows = [i.to_row(data_version) for i in issues]
    df = pl_from_rows(rows)
    cols = ("issue_id, dataset, symbol, trade_date, rule_code, severity, "
            "detail, count, data_version")
    with writer() as con:
        ensure_table(con)
        con.register("_issues", df)
        con.execute(f"INSERT OR REPLACE INTO data_quality_issue ({cols}) "
                    f"SELECT {cols} FROM _issues")
    return len(rows)


def latest_issues(limit: int = 200, resolved: bool = False) -> list[dict]:
    with reader() as con:
        ensure_table(con)
        rows = con.execute(
            "SELECT issue_id, dataset, symbol, trade_date, rule_code, severity, "
            "detail, count, data_version, resolved, created_at "
            "FROM data_quality_issue WHERE resolved = ? "
            "ORDER BY created_at DESC LIMIT ?",
            [resolved, limit],
        ).fetchall()
    return _issue_rows(rows)


def _issue_rows(rows) -> list[dict]:
    """issue 行 → dict（latest_issues / query_issues 共用映射）。"""
    import json as _json

    return [{
        "issue_id": r[0], "dataset": r[1], "symbol": r[2],
        "trade_date": str(r[3]) if r[3] else None,
        "rule_code": r[4], "severity": r[5],
        "detail": _json.loads(r[6]) if r[6] else {},
        "count": r[7], "data_version": r[8], "resolved": r[9],
        "created_at": str(r[10]),
    } for r in rows]


def query_issues(
    severity: str | None = None,
    dataset: str | None = None,
    resolved: bool = False,
    limit: int = 100,
) -> list[dict]:
    """质量问题检索（按 severity / dataset / resolved 过滤，过滤下推 SQL）。

    /data/crosscheck/issues 与 /data/issues 共用，检索口径只此一份，
    避免两处 SQL 漂移。此前全量拉 10 万行内存过滤 —— O(N) 内存与延迟。
    """
    sql = ("SELECT issue_id, dataset, symbol, trade_date, rule_code, severity, "
           "detail, count, data_version, resolved, created_at "
           "FROM data_quality_issue WHERE resolved = ?")
    params: list = [resolved]
    if severity:
        sql += " AND severity = ?"
        params.append(severity)
    if dataset:
        sql += " AND dataset = ?"
        params.append(dataset)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with reader() as con:
        ensure_table(con)
        rows = con.execute(sql, params).fetchall()
    return _issue_rows(rows)


def resolve_issue(issue_id: str) -> bool:
    with writer() as con:
        ensure_table(con)
        con.execute("UPDATE data_quality_issue SET resolved = TRUE WHERE issue_id = ?", [issue_id])
    return True


def resolve_issues(issue_ids: list[str]) -> int:
    """批量标记已解决；返回实际标记的条数（不存在的 id 自动跳过）。"""
    if not issue_ids:
        return 0
    n = 0
    with writer() as con:
        ensure_table(con)
        for iid in issue_ids:
            n += con.execute(
                "UPDATE data_quality_issue SET resolved = TRUE WHERE issue_id = ?",
                [iid],
            ).fetchone()[0] or 0
    return n


def pl_from_rows(rows: list[dict]):
    import polars as pl
    return pl.DataFrame(rows, schema_overrides={"count": pl.Int64})
