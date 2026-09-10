"""telegraph 采集器单元测试。"""

from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest

from lquant.news.sources.base import get_sources
from lquant.news.sources.telegraph import ClsTelegraphSource, SinaTelegraphSource

# 列名按 akshare 1.18.94 实测:
# stock_info_global_cls -> 标题/内容/发布日期/发布时间
# stock_info_global_sina -> 时间/内容

def test_cls_maps_columns():
    fake = pd.DataFrame(
        {
            "标题": [None],
            "内容": ["央行开展XX操作"],
            "发布日期": ["2026-09-10"],
            "发布时间": ["10:00:00"],
        }
    )
    with patch("lquant.news.sources.telegraph.ak") as mock_ak:
        mock_ak.stock_info_global_cls.return_value = fake
        items = ClsTelegraphSource().fetch(date(2026, 9, 10))
    assert items[0].source == "telegraph"
    assert items[0].source_name == "cls"
    assert "央行" in items[0].content
    assert items[0].external_id == "2026-09-1010:00:00"
    assert items[0].published_at is not None


def test_cls_missing_column_raises_valueerror():
    fake = pd.DataFrame({"内容": ["x"], "发布日期": ["2026-09-10"]})
    with patch("lquant.news.sources.telegraph.ak") as mock_ak:
        mock_ak.stock_info_global_cls.return_value = fake
        with pytest.raises(ValueError, match="实际列名"):
            ClsTelegraphSource().fetch(date(2026, 9, 10))
        # 缺列是映射错误,不得触发 sina fallback
        mock_ak.stock_info_global_sina.assert_not_called()


def test_cls_empty_time_falls_back_to_content_hash():
    # time_part 为空时走 sha1(content)[:16] 兜底,避免同日碰撞
    fake = pd.DataFrame(
        {
            "标题": [None, None],
            "内容": ["快讯甲", "快讯乙"],
            "发布日期": ["2026-09-10", "2026-09-10"],
            "发布时间": [None, None],
        }
    )
    with patch("lquant.news.sources.telegraph.ak") as mock_ak:
        mock_ak.stock_info_global_cls.return_value = fake
        items = ClsTelegraphSource().fetch(date(2026, 9, 10))
    assert len({i.external_id for i in items}) == 2
    assert all(len(i.external_id) == 16 for i in items)


def test_cls_unparseable_time_is_none():
    fake = pd.DataFrame(
        {
            "标题": [None],
            "内容": ["快讯"],
            "发布日期": ["bad-date"],
            "发布时间": ["bad-time"],
        }
    )
    with patch("lquant.news.sources.telegraph.ak") as mock_ak:
        mock_ak.stock_info_global_cls.return_value = fake
        items = ClsTelegraphSource().fetch(date(2026, 9, 10))
    assert items[0].published_at is None


def test_cls_fallback_to_sina():
    fake = pd.DataFrame({"时间": ["2026-07-10 10:00:00"], "内容": ["新浪快讯"]})
    with patch("lquant.news.sources.telegraph.ak") as mock_ak:
        mock_ak.stock_info_global_cls.side_effect = RuntimeError("blocked")
        mock_ak.stock_info_global_sina.return_value = fake
        items = ClsTelegraphSource().fetch(date(2026, 9, 10))
    assert items[0].source_name == "sina_7x24"
    assert items[0].published_at is not None
    assert len(items[0].external_id) == 16


def test_sina_direct():
    fake = pd.DataFrame({"时间": ["2026-07-10 10:00:00"], "内容": ["新浪快讯"]})
    with patch("lquant.news.sources.telegraph.ak") as mock_ak:
        mock_ak.stock_info_global_sina.return_value = fake
        items = SinaTelegraphSource().fetch(date(2026, 9, 10))
    assert items[0].source_name == "sina_7x24"
    assert items[0].external_id != ""
    assert len(items[0].external_id) == 16


def test_empty_content_row_skipped():
    fake = pd.DataFrame(
        {
            "时间": ["2026-07-10 10:00:00", "2026-07-10 10:00:01"],
            "内容": ["", "有内容"],
        }
    )
    with patch("lquant.news.sources.telegraph.ak") as mock_ak:
        mock_ak.stock_info_global_sina.return_value = fake
        items = SinaTelegraphSource().fetch(date(2026, 9, 10))
    assert len(items) == 1


def test_get_sources_registry():
    cls_src, sina_src = get_sources(["cls_telegraph", "sina_7x24"])
    assert isinstance(cls_src, ClsTelegraphSource)
    assert isinstance(sina_src, SinaTelegraphSource)
    with pytest.raises(KeyError, match="unknown news source"):
        get_sources(["nope"])


def test_fetch_ignores_day():
    with patch("lquant.news.sources.telegraph.ak") as mock_ak:
        mock_ak.stock_info_global_cls.side_effect = RuntimeError("blocked")
        mock_ak.stock_info_global_sina.return_value = pd.DataFrame(
            {"时间": ["2026-07-10 10:00:00"], "内容": ["x"]}
        )
        ClsTelegraphSource().fetch(date(2020, 1, 1))
        SinaTelegraphSource().fetch(date(2020, 1, 1))
        mock_ak.stock_info_global_sina.assert_called()


def test_all_sources_have_protocol_attrs():
    for src in get_sources():
        assert isinstance(src.name, str) and src.name
        assert src.category == "telegraph"
        assert callable(src.fetch)
