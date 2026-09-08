"""DuckDB 目录读写（标的表、日历、因子定义等小表）。

约定：所有 upsert 都按目标表列自动对齐 —— 上游 DataFrame 少列补 NULL，
多列丢弃。这样 provider 换源时不必保证列完全一致（换源 80% 的坑在这里）。
"""
from __future__ import annotations

from datetime import date

import polars as pl

from lquant.core.db import reader, writer


def _columns(con, table: str) -> list[tuple[str, str]]:
    return [(r[0], r[1]) for r in con.execute(f'DESCRIBE "{table}"').fetchall()]


def _upsert(table: str, df: pl.DataFrame) -> int:
    """按表结构对齐后写入。

    有主键 → INSERT OR REPLACE；无主键 → 同键先删后插（幂等）。
    老版 DDL 的部分看板表没有 PK，不能一刀切用 OR REPLACE。
    """
    if not len(df):
        return 0
    with writer() as con:
        cols = _columns(con, table)
        names = [c for c, _ in cols]
        if not names:
            raise RuntimeError(f"表 {table} 不存在，先跑 scripts/init_db.py")
        projection = []
        for name, typ in cols:
            if name in df.columns:
                projection.append(f'"{name}"')
            else:
                projection.append(f"CAST(NULL AS {typ})")
        has_pk = any(r[3] == "PRI" for r in con.execute(f'DESCRIBE "{table}"').fetchall())
        con.register("_tmp", df)
        if has_pk:
            con.execute(
                f'INSERT OR REPLACE INTO "{table}" '
                f'SELECT {", ".join(projection)} FROM _tmp'
            )
        else:
            if "trade_date" in names and "trade_date" in df.columns:
                con.execute(f'DELETE FROM "{table}" WHERE trade_date IN (SELECT DISTINCT trade_date FROM _tmp)')
            con.execute(f'INSERT INTO "{table}" SELECT {", ".join(projection)} FROM _tmp')
    return len(df)


class TradeCalendarRepo:
    def is_trading_day(self, d: date) -> bool:
        with reader() as con:
            r = con.execute(
                "SELECT is_open FROM trade_calendar WHERE trade_date = ?", [d]
            ).fetchone()
        return bool(r[0]) if r else False

    def range(self, start: date, end: date) -> list[date]:
        with reader() as con:
            rows = con.execute(
                "SELECT trade_date FROM trade_calendar WHERE trade_date BETWEEN ? AND ? "
                "AND is_open ORDER BY trade_date",
                [start, end],
            ).fetchall()
        return [r[0] for r in rows]

    def upsert(self, df: pl.DataFrame) -> int:
        return _upsert("trade_calendar", df)

    def count(self) -> int:
        with reader() as con:
            return con.execute("SELECT count(*) FROM trade_calendar").fetchone()[0]


class SecurityRepo:
    def active_symbols(self) -> list[str]:
        with reader() as con:
            rows = con.execute(
                "SELECT symbol FROM security WHERE delist_date IS NULL "
                "OR delist_date > current_date ORDER BY symbol"
            ).fetchall()
        return [r[0] for r in rows]

    def all_symbols(self) -> list[str]:
        with reader() as con:
            rows = con.execute("SELECT symbol FROM security ORDER BY symbol").fetchall()
        return [r[0] for r in rows]

    def etf_symbols(self) -> list[str]:
        with reader() as con:
            rows = con.execute(
                "SELECT symbol FROM security WHERE sec_type IN ('etf','lof') ORDER BY symbol"
            ).fetchall()
        return [r[0] for r in rows]

    def pending_details(self, limit: int | None = None) -> list[str]:
        """还没补到 list_date 的标的 —— 增量补详情用。"""
        sql = ("SELECT symbol FROM security WHERE list_date IS NULL "
               "AND sec_type <> 'index' ORDER BY symbol")
        if limit:
            sql += f" LIMIT {int(limit)}"
        with reader() as con:
            return [r[0] for r in con.execute(sql).fetchall()]

    def upsert(self, df: pl.DataFrame) -> int:
        return _upsert("security", df)

    def count(self) -> int:
        with reader() as con:
            return con.execute("SELECT count(*) FROM security").fetchone()[0]


class EtfMetaRepo:
    def upsert(self, df: pl.DataFrame) -> int:
        return _upsert("etf_meta", df)

    def all(self) -> pl.DataFrame:
        with reader() as con:
            return con.execute("SELECT * FROM etf_meta").pl()

    def sellable_days(self, symbol: str) -> int:
        """per-instrument T+N；查不到就回落到规则表默认值（调用方处理）。"""
        with reader() as con:
            r = con.execute(
                "SELECT sellable_after_days FROM etf_meta WHERE symbol = ?", [symbol]
            ).fetchone()
        return int(r[0]) if r and r[0] is not None else 1


class FinancialRepo:
    def upsert(self, df: pl.DataFrame) -> int:
        return _upsert("financial_pit", df)

    def count(self) -> int:
        with reader() as con:
            return con.execute("SELECT count(*) FROM financial_pit").fetchone()[0]


class FactorDefRepo:
    def all(self) -> pl.DataFrame:
        with reader() as con:
            return con.execute("SELECT * FROM factor_def WHERE enabled").pl()

    def upsert(self, df: pl.DataFrame) -> int:
        return _upsert("factor_def", df)


def upsert(table: str, df: pl.DataFrame) -> int:
    """公开入口：按目标表列对齐后写入（少列补 NULL，多列丢弃）。

    看板与模拟盘等非行情表也走这里，保证「换源不必保证列一致」的约定统一。
    """
    return _upsert(table, df)
