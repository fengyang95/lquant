"""Golden 已知答案测试集（细化方案 §3.8.2 ⑤）。

维护一批人工核验过的「事实」，每次数据版本变更后全量跑：
- 结构性事实：各年末全市场股票数、每年交易日数
- 确定性计算：某股票某年复权收益率（固定值）
- 因子基准：经典因子的 IC 值（冻结）

纪律：首次跑通后人工核验一遍再冻结（freeze）。不要凭记忆填外部数字
—— 填错了这套检验就变成噪音源。跨数据版本比对用 run_all。
"""
from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from lquant.core.db import reader, writer
from lquant.data.quality.issues import Issue
from lquant.data.store.ddl import DDL_GOLDEN_EXPECTED as _DDL

__all__ = ["GoldenCase", "GoldenResult", "freeze", "run_all", "list_cases"]


@dataclass(frozen=True)
class GoldenCase:
    name: str
    kind: str          # structural / calc / factor_ic
    sql: str           # 必须返回单值（首行首列）
    expected: float | None = None
    tolerance: float = 1e-6


@dataclass(frozen=True)
class GoldenResult:
    case: GoldenCase
    actual: float | None
    ok: bool

    def as_issue(self) -> Issue:
        return Issue(
            rule=f"GOLDEN_{self.case.name}", severity="error", dataset="golden",
            detail=f"golden '{self.case.name}' 期望 {self.case.expected}，"
                   f"实际 {self.actual}（tol={self.case.tolerance}）",
            count=1)


def freeze(cases: list[GoldenCase]) -> int:
    """跑一遍并把结果冻结为期望值。已存在的同名 case 会被覆盖。"""
    with writer() as con:
        con.execute(_DDL)
        for c in cases:
            actual = _run_one(con, c)
            if actual is None:
                raise ValueError(f"golden '{c.name}' 查询无结果，不能冻结空值")
            con.execute(
                "INSERT OR REPLACE INTO golden_expected VALUES (?, ?, ?, ?, ?, now())",
                [c.name, c.kind, c.sql, actual, c.tolerance])
    return len(cases)


def run_all() -> list[GoldenResult]:
    """跑全部已冻结 case；任何偏差都是 error 级 issue（调用方落库）。"""
    with reader() as con:
        con.execute(_DDL)
        rows = con.execute("SELECT name, kind, sql, expected, tolerance "
                           "FROM golden_expected ORDER BY name").fetchall()
        out = []
        for name, kind, sql, expected, tol in rows:
            c = GoldenCase(name=name, kind=kind, sql=sql,
                           expected=expected, tolerance=tol)
            actual = _run_one(con, c)
            ok = (actual is not None and expected is not None
                  and abs(actual - expected) <= tol)
            out.append(GoldenResult(case=c, actual=actual, ok=ok))
    return out


def list_cases() -> list[GoldenCase]:
    with reader() as con:
        con.execute(_DDL)
        rows = con.execute("SELECT name, kind, sql, expected, tolerance "
                           "FROM golden_expected ORDER BY name").fetchall()
    return [GoldenCase(name=r[0], kind=r[1], sql=r[2], expected=r[3], tolerance=r[4])
            for r in rows]


def _run_one(con, c: GoldenCase) -> float | None:
    r = con.execute(c.sql).fetchone()
    return float(r[0]) if r and r[0] is not None else None
