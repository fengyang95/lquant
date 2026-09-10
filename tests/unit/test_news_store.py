import duckdb
import pytest

from lquant.news.model import NewsItem
from lquant.news.store import (
    init_news_ddl,
    insert_news,
    news_stats_by_industry,
    news_stats_by_source,
    query_news,
)


@pytest.fixture()
def con():
    c = duckdb.connect(":memory:")
    init_news_ddl(c)
    return c


def _item(**kw):
    base = dict(
        source="telegraph",
        source_name="cls",
        external_id="e1",
        title="t",
        content="c" * 10,
        url="u",
        symbols=(),
        industry_code=None,
        published_at=None,
        collected_at=None,
        quality_flags=0,
        source_tag="cls",
    )
    base.update(kw)
    return NewsItem(**base)


def test_news_id_stable():
    assert _item().news_id == _item().news_id
    assert _item(external_id="e2").news_id != _item().news_id


def test_insert_dedupes(con):
    assert insert_news(con, [_item(), _item()]) == 1
    assert insert_news(con, [_item(external_id="e2")]) == 1
    assert query_news(con)["total"] == 2


def test_query_filters(con):
    insert_news(
        con,
        [
            _item(external_id="a", symbols=("000001.SZ",)),
            _item(external_id="b", industry_code="801010"),
            _item(external_id="c", content="锂矿价格上涨"),
        ],
    )
    assert query_news(con, symbol="000001.SZ")["total"] == 1
    assert query_news(con, industry="801010")["total"] == 1
    assert query_news(con, keyword="锂矿")["total"] == 1
    assert query_news(con, source="news")["total"] == 0


def test_query_pagination(con):
    insert_news(con, [_item(external_id=str(i)) for i in range(5)])
    page = query_news(con, limit=2, offset=2)
    assert page["total"] == 5
    assert len(page["items"]) == 2


def test_content_truncated_and_flagged(con):
    insert_news(con, [_item(external_id="x", content="字" * 3000)])
    row = query_news(con)["items"][0]
    assert len(row["content"]) == 2000
    assert row["quality_flags"] & 2


def test_missing_time_falls_back(con):
    insert_news(con, [_item(external_id="y", published_at=None)])
    row = query_news(con)["items"][0]
    assert row["published_at"] is not None
    assert row["quality_flags"] & 1


def test_stats(con):
    insert_news(
        con,
        [
            _item(external_id="a", industry_code="801010"),
            _item(external_id="b", industry_code="801010"),
            _item(external_id="c", industry_code="801020"),
        ],
    )
    by_ind = {r["industry_code"]: r["count"] for r in news_stats_by_industry(con)}
    assert by_ind == {"801010": 2, "801020": 1}

    by_src = news_stats_by_source(con)
    assert by_src[0]["source"] == "telegraph"
    assert by_src[0]["source_name"] == "cls"
    assert by_src[0]["count"] == 3
    assert by_src[0]["last_collected_at"] is not None
