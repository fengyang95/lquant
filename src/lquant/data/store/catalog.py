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


def _upsert(table: str, df: pl.DataFrame, *, epoch_cols: tuple[str, ...] | None = None) -> int:
    """按表结构对齐后写入。

    有主键 → INSERT OR REPLACE（每行幂等，常用于带变迁史的表）。
    无主键 → 先按 epoch_cols 删掉同批次旧行再插（快照替换语义）：
       epoch_cols 唯一标识一个"发布批次" —— 日期型小表默认 ('trade_date',)，
       快照型大表（如指数成分）传 (index_code, eff_date) 实现整日整组替换。
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
            keys = epoch_cols or ("trade_date",)
            keys = [k for k in keys if k in names and k in df.columns]
            if keys:
                deps = ", ".join(keys)
                sel = ", ".join(f"_tmp.{k}" for k in keys)
                con.execute(
                    f'DELETE FROM "{table}" WHERE ({deps}) IN '
                    f'(SELECT DISTINCT {sel} FROM _tmp)'
                )
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


class IndustryClassifyRepo:
    """申万行业分类（D5）。

    std_date 是分类生效日 —— as_of 查询严格取「该日已生效」的分类，
    防止用今天的行业回测十年前（中性化前视偏差）。
    """

    def as_of(self, d: date) -> dict[str, str]:
        """按生效日取当日已生效的 {symbol: std}。

        若同一标的在 d 前有多次行业变更，只取生效日最晚的那次 ——
        否则第一版代码会把所有历史 std 都吐出来，dict 靠插入序碰运气。
        """
        with reader() as con:
            rows = con.execute(
                "SELECT symbol, std FROM industry_classify "
                "WHERE (symbol, std_date) IN ("
                "  SELECT symbol, max(std_date) FROM industry_classify "
                "  WHERE std_date <= ? GROUP BY symbol)",
                [d],
            ).fetchall()
        return {s: std for s, std in rows}

    def latest(self) -> dict[str, str]:
        """取每个标的生效日最新的分类（研究用当下分类，勿用于历史回溯）。"""
        with reader() as con:
            rows = con.execute(
                "SELECT symbol, std FROM industry_classify "
                "WHERE (symbol, std_date) IN ("
                "  SELECT symbol, max(std_date) FROM industry_classify GROUP BY symbol)"
            ).fetchall()
        return {s: std for s, std in rows}

    def upsert(self, df: pl.DataFrame) -> int:
        return _upsert("industry_classify", df)

    def count(self) -> int:
        with reader() as con:
            return con.execute("SELECT count(*) FROM industry_classify").fetchone()[0]


class IndexConsRepo:
    """指数成分与权重历史快照（D6）。

    **数据模型：一个 eff_date = 一次完整调仓快照。** 真实指数按调仓日发布
    整张成分表，所以每批 (index_code, eff_date) 都是当时全量成员 ——
    as_of 取「截至 d 的最近一次快照」的整体，而不是逐 symbol 拼凑。

    这样新增和移出都被同一张快照覆盖：某股在 06-01 调仓被移出指数后，
    最新快照里就没有它，as_of(07-01) 不会把它翻出来 —— 避免幸存者偏差。
    （如果按「逐 symbol 各自生效日」去 max，那么被移出的股票旧行永远存活，
    历史回测会拿到「持续至今的僵尸成分」。）

    get_index_stocks 就是用它反喂 JQ shim。
    """

    def _batch_eff_date(self, con, index_code: str, d: date) -> date | None:
        row = con.execute(
            "SELECT max(eff_date) FROM index_cons "
            "WHERE index_code = ? AND eff_date <= ?", [index_code, d],
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def as_of(self, index_code: str, d: date) -> pl.DataFrame:
        with reader() as con:
            eff = self._batch_eff_date(con, index_code, d)
            if eff is None:
                return pl.DataFrame({"symbol": [], "weight": []})
            return con.execute(
                "SELECT symbol, weight FROM index_cons "
                "WHERE index_code = ? AND eff_date = ? ORDER BY symbol",
                [index_code, eff],
            ).pl()

    def symbols_as_of(self, index_code: str, d: date) -> list[str]:
        return sorted(self.as_of(index_code, d)["symbol"].to_list())

    def latest_symbols(self, index_code: str) -> list[str]:
        """当下成分 = 最近一次快照（研究用当下口径；回测走 as_of 防前视）。"""
        with reader() as con:
            eff = self._batch_eff_date(con, index_code, date.max)
            if eff is None:
                return []
            rows = con.execute(
                "SELECT symbol FROM index_cons WHERE index_code = ? AND eff_date = ?",
                [index_code, eff],
            ).fetchall()
        return sorted(r[0] for r in rows)

    def upsert(self, df: pl.DataFrame) -> int:
        # 快照替换：同一 (index_code, eff_date) 批次整体重写，旧成分不留僵尸
        return _upsert("index_cons", df, epoch_cols=("index_code", "eff_date"))

    def count(self) -> int:
        with reader() as con:
            return con.execute("SELECT count(*) FROM index_cons").fetchone()[0]


def upsert(table: str, df: pl.DataFrame) -> int:
    """公开入口：按目标表列对齐后写入（少列补 NULL，多列丢弃）。

    看板与模拟盘等非行情表也走这里，保证「换源不必保证列一致」的约定统一。
    """
    return _upsert(table, df)
