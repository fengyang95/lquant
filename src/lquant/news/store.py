"""news_item 存取层:DuckDB DDL、去重插入、多维查询、统计。"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from duckdb import DuckDBPyConnection

from lquant.news.model import NewsItem

# quality_flags 位定义
FLAG_NO_PUBLISHED_AT = 1  # published_at 缺失,入库时用采集时间兜底
FLAG_CONTENT_TRUNCATED = 2  # content 超长被截断

CONTENT_MAX_LEN = 2000

_DDL = """
CREATE TABLE IF NOT EXISTS news_item (
    news_id        VARCHAR PRIMARY KEY,
    source         VARCHAR NOT NULL,
    source_name    VARCHAR NOT NULL,
    external_id    VARCHAR NOT NULL,
    title          VARCHAR,
    content        VARCHAR,
    url            VARCHAR,
    symbols        VARCHAR[],
    industry_code  VARCHAR,
    published_at   TIMESTAMP,
    collected_at   TIMESTAMP,
    quality_flags  INTEGER,
    source_tag     VARCHAR
);
"""


def init_news_ddl(con: DuckDBPyConnection) -> None:
    con.execute(_DDL)


def insert_news(con: DuckDBPyConnection, items: Iterable[NewsItem]) -> int:
    """去重插入,返回精确新增条数(插入前后 count 差)。"""
    items = list(items)
    before = con.execute("SELECT count(*) FROM news_item").fetchone()[0]
    for item in items:
        published_at = item.published_at
        quality_flags = item.quality_flags
        if published_at is None:
            published_at = datetime.now()
            quality_flags |= FLAG_NO_PUBLISHED_AT
        content = item.content
        if len(content) > CONTENT_MAX_LEN:
            content = content[:CONTENT_MAX_LEN]
            quality_flags |= FLAG_CONTENT_TRUNCATED
        con.execute(
            "INSERT OR IGNORE INTO news_item VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                item.news_id,
                item.source,
                item.source_name,
                item.external_id,
                item.title,
                content,
                item.url,
                list(item.symbols),
                item.industry_code,
                published_at,
                item.collected_at or datetime.now(),
                quality_flags,
                item.source_tag,
            ],
        )
    after = con.execute("SELECT count(*) FROM news_item").fetchone()[0]
    return after - before


def query_news(  # noqa: PLR0917 — 接口按 brief 固定
    con: DuckDBPyConnection,
    source: str | None = None,
    industry: str | None = None,
    symbol: str | None = None,
    day: str | None = None,
    keyword: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """多维过滤查询,返回 {total, items}。items 按 published_at 倒序。"""
    limit = min(max(int(limit), 0), 200)
    offset = max(int(offset), 0)
    where: list[str] = []
    params: list[Any] = []

    def _add(cond: str, *values: Any) -> None:
        where.append(cond)
        params.extend(values)

    if source is not None:
        # API 口径：source 参数指「来源名」（registry 的 em_global/cls/...），
        # 落在 source_name 列；DB 的 source 列存的是 category（telegraph/news/...）
        _add("source_name = ?", source)
    if industry is not None:
        _add("industry_code = ?", industry)
    if symbol is not None:
        _add("list_contains(symbols, ?)", symbol)
    if day is not None:
        _add("CAST(published_at AS DATE) = CAST(? AS DATE)", day)

    if keyword is not None:
        # 转义 LIKE 通配符，避免用户输入 %/_ 变成通配（ESCAPE 指定转义符）
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        _add(
            "(title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\')",
            f"%{escaped}%", f"%{escaped}%",
        )

    where_clause = " WHERE " + " AND ".join(where) if where else ""
    total = con.execute(
        f"SELECT count(*) FROM news_item{where_clause}", params
    ).fetchone()[0]
    rows = con.execute(
        f"SELECT news_id, source, source_name, external_id, title, content, url, "
        f"symbols, industry_code, published_at, collected_at, quality_flags, source_tag "
        f"FROM news_item{where_clause} "
        f"ORDER BY published_at DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    return {"total": total, "items": _rows_to_dicts(rows)}


def _rows_to_dicts(rows: Any) -> list[dict[str, Any]]:
    cols = [d[0] for d in rows.description]
    return [dict(zip(cols, row, strict=True)) for row in rows.fetchall()]


def news_stats_by_industry(con: DuckDBPyConnection) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT industry_code, count(*) AS count FROM news_item "
        "WHERE industry_code IS NOT NULL GROUP BY industry_code ORDER BY count DESC"
    )
    return _rows_to_dicts(rows)


def news_stats_by_source(con: DuckDBPyConnection) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT source, source_name, count(*) AS count, "
        "max(collected_at) AS last_collected_at "
        "FROM news_item GROUP BY source, source_name ORDER BY count DESC"
    )
    return _rows_to_dicts(rows)


def news_stats_by_source_name(con: DuckDBPyConnection) -> list[dict[str, Any]]:
    """按来源名聚合（API /sources、/summary 的口径：source_name = registry key）。"""
    rows = con.execute(
        "SELECT source_name, count(*) AS count, "
        "max(collected_at) AS last_collected_at "
        "FROM news_item GROUP BY source_name ORDER BY count DESC"
    )
    return _rows_to_dicts(rows)
