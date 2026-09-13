"""em_news 采集器单元测试(全 mock,不打外网)。"""

from __future__ import annotations

import hashlib
from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest

from lquant.news.model import NewsItem
from lquant.news.sources.em_news import EmColumnError, EmNewsSource, get_active_pool


def _fake_df(symbol: str = "平安银行") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "关键词": [symbol, symbol],
            "新闻标题": ["平安银行年报", "平安银行分红"],
            "新闻内容": ["内容A", "内容B"],
            "新闻链接": ["http://x/1", "http://x/2"],
            "发布时间": ["2026-09-10 10:00:00", "2026-09-10 09:00:00"],
        }
    )


def test_em_news_maps_and_tags_symbol() -> None:
    with (
        patch("lquant.news.sources.em_news.ak") as mock_ak,
        patch(
            "lquant.news.sources.em_news.get_active_pool",
            return_value=["000001.SZ"],
        ),
    ):
        mock_ak.stock_news_em.return_value = _fake_df()
        items = EmNewsSource(pool=["000001.SZ"]).fetch(date(2026, 9, 10))

    assert len(items) == 2
    first = items[0]
    assert isinstance(first, NewsItem)
    assert first.source == "news"
    assert first.source_name == "em_news"  # source_name 与注册名统一
    assert first.symbols == ("000001.SZ",)
    # 来源精确关联,不打 bit4
    assert not (first.quality_flags & 4)
    assert first.title == "平安银行年报"
    assert first.content == "内容A"
    assert first.url == "http://x/1"
    assert first.published_at is not None
    assert first.published_at.strftime("%Y-%m-%d %H:%M:%S") == "2026-09-10 10:00:00"
    # external_id = sha1(标题+发布时间)[:16]
    expected = hashlib.sha1("平安银行年报2026-09-10 10:00:00".encode()).hexdigest()[:16]
    assert first.external_id == expected


def test_em_news_lazy_pool_when_none() -> None:
    with (
        patch("lquant.news.sources.em_news.ak") as mock_ak,
        patch(
            "lquant.news.sources.em_news.get_active_pool",
            return_value=["000001.SZ", "600000.SH"],
        ) as mock_pool,
    ):
        mock_ak.stock_news_em.return_value = _fake_df()
        items = EmNewsSource().fetch(date(2026, 9, 10))

    mock_pool.assert_called_once()
    assert len(items) == 4
    assert {i.symbols for i in items} == {("000001.SZ",), ("600000.SH",)}


def test_em_news_symbol_failure_does_not_abort() -> None:
    with patch("lquant.news.sources.em_news.ak") as mock_ak:
        ok = _fake_df()
        mock_ak.stock_news_em.side_effect = [RuntimeError("network"), ok]
        items = EmNewsSource(pool=["000001.SZ", "600000.SH"]).fetch(date(2026, 9, 10))

    # 第一只失败记 warning 后 continue,第二只正常产出
    assert len(items) == 2
    assert items[0].symbols == ("600000.SH",)


def test_em_news_missing_column_raises_value_error() -> None:
    bad = _fake_df().drop(columns=["新闻标题"])
    with patch("lquant.news.sources.em_news.ak") as mock_ak:
        mock_ak.stock_news_em.return_value = bad
        with pytest.raises(EmColumnError, match="新闻标题"):
            EmNewsSource(pool=["000001.SZ"]).fetch(date(2026, 9, 10))


def test_em_news_plain_value_error_is_per_symbol_failure() -> None:
    """拉取阶段的普通 ValueError 不再向传播,按单股失败跳过。"""
    with patch("lquant.news.sources.em_news.ak") as mock_ak:
        mock_ak.stock_news_em.side_effect = [ValueError("bad param"), _fake_df()]
        items = EmNewsSource(pool=["000001.SZ", "600000.SH"]).fetch(date(2026, 9, 10))

    assert len(items) == 2
    assert items[0].symbols == ("600000.SH",)


def _seed_lake(tmp_path):
    """在临时 parquet 湖里写两天的日线。

    活跃池读的是湖（parquet），不是 DuckDB `daily_bar` 表 ——
    那张表只有 DDL、没有写入路径，恒空（曾经这里就是按幽灵表造的假）。
    最近日 top1 应为 600000.SH；历史日 000001.SZ 成交额更大，不该被选中。
    """
    import polars as pl

    root = tmp_path / "lake"
    p = root / "daily" / "year=2026" / "part-0.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": ["000001.SZ", "600000.SH", "000001.SZ"],
        "trade_date": [date(2026, 9, 10), date(2026, 9, 10), date(2026, 9, 9)],
        "amount": [100.0, 200.0, 999.0],
    }).write_parquet(p)
    return root


def test_get_active_pool_top_amount_last_date(tmp_path, monkeypatch) -> None:
    import lquant.data.store.parquet as pq

    monkeypatch.setattr(pq, "_root", lambda: _seed_lake(tmp_path))

    assert get_active_pool(limit=1) == ["600000.SH"]  # 最近交易日 + 成交额 top1,非历史 999


def test_get_active_pool_empty_lake_returns_empty(tmp_path, monkeypatch) -> None:
    import lquant.data.store.parquet as pq

    monkeypatch.setattr(pq, "_root", lambda: tmp_path / "empty-lake")

    assert get_active_pool(limit=5) == []
