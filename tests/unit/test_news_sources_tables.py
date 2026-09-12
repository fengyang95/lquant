"""tables.py 声明式采集器单元测试（全 mock，不打外网）。

列名按 akshare 1.18.94 实测：
- stock_info_global_em / stock_info_cjzc_em -> 标题/摘要/发布时间/链接
- stock_info_global_ths / stock_info_global_futu -> 标题/内容/发布时间/链接
- news_cctv(date=) -> date/title/content
- stock_notice_report(symbol=, date=) -> 代码/名称/公告标题/公告类型/公告日期/网址
- stock_research_report_em(symbol=) -> 股票代码/股票简称/报告名称/东财评级/机构/行业/日期/报告PDF链接
- stock_hot_search_baidu(symbol=, date=) -> 名称/代码/涨跌幅/综合热度
"""

from __future__ import annotations

import hashlib
from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest

from lquant.news.sources import tables
from lquant.news.sources.base import get_sources
from lquant.news.sources.tables import (
    BaiduHotSource,
    CctvNewsSource,
    EmCjzcSource,
    EmGlobalSource,
    EmNoticeSource,
    EmResearchSource,
    FutuGlobalSource,
    SourceColumnError,
    ThsGlobalSource,
    to_symbol,
)

DAY = date(2026, 9, 11)


def _patch_ak(fn_name: str, ret=None, side_effect=None):
    """patch tables.ak 并配置指定函数（返回 (context_manager, mock_ak)）。"""
    ctx = patch("lquant.news.sources.tables.ak")
    mock_ak = ctx.start()
    target = getattr(mock_ak, fn_name)
    if side_effect is not None:
        target.side_effect = side_effect
    else:
        target.return_value = ret
    return ctx, mock_ak, target


# ---------- 代码 → symbol ----------


@pytest.mark.parametrize(
    ("code", "want"),
    [
        ("600519", "600519.SH"),
        ("688981", "688981.SH"),
        ("900901", "900901.SH"),  # 沪市 B 股
        ("000001", "000001.SZ"),
        ("300750", "300750.SZ"),
        ("430047", "430047.BJ"),
        ("830799", "830799.BJ"),
        ("920627", "920627.BJ"),  # 北交所新段
        ("00700", None),  # 港股
        ("60051", None),  # 位数不足
        ("60051A", None),
        ("", None),
    ],
)
def test_to_symbol_maps_exchange_suffix(code: str, want: str | None) -> None:
    assert to_symbol(code) == want


# ---------- telegraph ----------


def test_em_global_maps_columns_and_uses_url_as_id() -> None:
    fake = pd.DataFrame(
        {
            "标题": ["锂矿进口创历史新高"],
            "摘要": ["正文A"],
            "发布时间": ["2026-09-11 20:55:36"],
            "链接": ["https://finance.eastmoney.com/a/1.html"],
        }
    )
    ctx, _, target = _patch_ak("stock_info_global_em", ret=fake)
    try:
        items = EmGlobalSource().fetch(DAY)
    finally:
        ctx.stop()
    assert len(items) == 1
    item = items[0]
    assert item.source == "telegraph"
    assert item.source_name == "em_global"
    assert item.title == "锂矿进口创历史新高"
    assert item.content == "正文A"
    assert item.published_at is not None
    assert item.published_at.strftime("%Y-%m-%d %H:%M:%S") == "2026-09-11 20:55:36"
    # 有原文链接时以链接 hash 作 external_id（跨天稳定、天然唯一）
    assert item.external_id == hashlib.sha1(item.url.encode()).hexdigest()[:16]
    target.assert_called_once()


def test_futu_keeps_content_only_rows() -> None:
    """富途快讯标题列恒为空，只有正文 —— 不能因此丢条目。"""
    fake = pd.DataFrame(
        {
            "标题": [""],
            "内容": ["伊拉克军方：同意联合调查边境无人机发射装置。"],
            "发布时间": ["2026-09-11 20:59:38"],
            "链接": ["https://news.futunn.com/flash/1"],
        }
    )
    ctx, _, _ = _patch_ak("stock_info_global_futu", ret=fake)
    try:
        items = FutuGlobalSource().fetch(DAY)
    finally:
        ctx.stop()
    assert len(items) == 1
    assert items[0].title == ""
    assert items[0].content.startswith("伊拉克军方")
    assert items[0].symbols == ()


def test_blank_rows_are_skipped() -> None:
    fake = pd.DataFrame(
        {
            "标题": ["", None],
            "内容": ["", "   "],
            "发布时间": ["2026-09-11 10:00:00", "2026-09-11 10:00:01"],
            "链接": ["http://a/1", "http://a/2"],
        }
    )
    ctx, _, _ = _patch_ak("stock_info_global_ths", ret=fake)
    try:
        items = ThsGlobalSource().fetch(DAY)
    finally:
        ctx.stop()
    assert items == []


def test_missing_column_raises_with_actual_columns() -> None:
    ctx, _, _ = _patch_ak("stock_info_global_ths", ret=pd.DataFrame({"无关列": [1]}))
    try:
        with pytest.raises(SourceColumnError, match="实际列名"):
            ThsGlobalSource().fetch(DAY)
    finally:
        ctx.stop()
    # ValueError 子类：任务层据此把该来源记为 failed（而非静默 0 行）
    assert issubclass(SourceColumnError, ValueError)


# ---------- news ----------


def test_cctv_passes_day_and_defaults_publish_time() -> None:
    fake = pd.DataFrame(
        {"date": ["2026-09-11"], "title": ["李强主持召开国务院常务会议"], "content": ["正文"]}
    )
    ctx, _, target = _patch_ak("news_cctv", ret=fake)
    try:
        items = CctvNewsSource().fetch(DAY)
    finally:
        ctx.stop()
    target.assert_called_once_with(date="20260911")
    assert items[0].source == "news"
    assert items[0].source_name == "cctv"
    # 接口无时间列 → 用请求日 + 联播播出时刻兜底（避免被 store 打成采集时刻 + bit1）
    assert items[0].published_at.strftime("%Y-%m-%d %H:%M") == "2026-09-11 19:00"
    assert items[0].external_id.startswith("20260911190000")


def test_cjzc_default_limit_and_config_override() -> None:
    assert EmCjzcSource.limit == 30
    assert EmResearchSource.pool_limit == 40
    assert EmNoticeSource.limit == 300

    fake = pd.DataFrame(
        {
            "标题": [f"财经早餐 {i}" for i in range(40)],
            "摘要": ["摘要"] * 40,
            "发布时间": ["2026-09-11 06:00:00"] * 40,
            "链接": [f"http://e/{i}" for i in range(40)],
        }
    )
    ctx, _, _ = _patch_ak("stock_info_cjzc_em", ret=fake)
    try:
        with patch.object(tables, "_cfg_int", side_effect=lambda s, k, d: 5 if s == "em_cjzc" else d):
            items = EmCjzcSource().fetch(DAY)
    finally:
        ctx.stop()
    assert len(items) == 5


# ---------- report ----------


def test_notice_maps_code_to_symbol_and_type_to_content() -> None:
    fake = pd.DataFrame(
        {
            "代码": ["920627", "600519", "00700"],
            "名称": ["力王股份", "贵州茅台", "腾讯控股"],
            "公告标题": ["持续督导跟踪报告", "股东会决议公告", "季度业绩公告"],
            "公告类型": ["保荐/核查意见", "股东大会决议公告", "业绩公告"],
            "公告日期": ["2026-09-11"] * 3,
            "网址": [f"https://data.eastmoney.com/notices/{i}" for i in range(3)],
        }
    )
    ctx, _, target = _patch_ak("stock_notice_report", ret=fake)
    try:
        items = EmNoticeSource().fetch(DAY)
    finally:
        ctx.stop()
    target.assert_called_once_with(symbol="全部", date="20260911")
    assert [i.source for i in items] == ["report"] * 3
    assert items[0].symbols == ("920627.BJ",)
    assert items[1].symbols == ("600519.SH",)
    assert items[2].symbols == ()  # 非 A 股代码不猜后缀，交给名称词表匹配
    assert items[0].content == "保荐/核查意见"
    assert items[0].published_at.strftime("%Y-%m-%d") == "2026-09-11"


def _research_df(symbol: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "股票代码": [symbol],
            "股票简称": ["贵州茅台"],
            "报告名称": [f"{symbol} 中报点评"],
            "机构": ["西南证券"],
            "东财评级": ["买入"],
            "行业": ["白酒Ⅱ"],
            "日期": ["2026-08-21"],
            "报告PDF链接": [f"https://pdf.dfcfw.com/{symbol}.pdf"],
        }
    )


def test_research_iterates_pool_with_bare_codes_and_summarizes() -> None:
    ctx, _, target = _patch_ak("stock_research_report_em", side_effect=_research_df)
    try:
        with patch(
            "lquant.news.sources.tables.get_active_pool",
            return_value=["600519.SH", "000001.SZ"],
        ):
            items = EmResearchSource().fetch(DAY)
    finally:
        ctx.stop()
    # 池内 symbol 带后缀，akshare 需要裸代码
    assert [c.kwargs["symbol"] for c in target.call_args_list] == ["600519", "000001"]
    assert len(items) == 2
    assert items[0].source == "report"
    assert items[0].symbols == ("600519.SH",)  # 逐股来源精确关联，不打 bit4
    assert items[0].quality_flags == 0
    assert items[0].content == "西南证券 · 买入 · 白酒Ⅱ"
    assert items[0].external_id == hashlib.sha1(items[0].url.encode()).hexdigest()[:16]


def test_pooled_source_tolerates_single_symbol_failure() -> None:
    def _flaky(symbol: str) -> pd.DataFrame:
        if symbol == "000001":
            raise RuntimeError("blocked")
        return _research_df(symbol)

    ctx, _, _ = _patch_ak("stock_research_report_em", side_effect=_flaky)
    try:
        with patch(
            "lquant.news.sources.tables.get_active_pool",
            return_value=["600519.SH", "000001.SZ"],
        ):
            items = EmResearchSource().fetch(DAY)
    finally:
        ctx.stop()
    assert len(items) == 1
    assert items[0].symbols == ("600519.SH",)


def test_research_drops_stale_reports_outside_window() -> None:
    """研报接口按股返回全部历史 → recent_days 窗口收口，否则陈年研报冲垮资讯流。"""

    def _mixed(symbol: str) -> pd.DataFrame:
        rows = _research_df(symbol)
        stale = _research_df(symbol)
        stale.loc[0, "报告名称"] = "2017 年老研报"
        stale.loc[0, "日期"] = "2017-01-02"
        stale.loc[0, "报告PDF链接"] = "https://pdf.dfcfw.com/old.pdf"
        return pd.concat([rows, stale], ignore_index=True)

    ctx, _, _ = _patch_ak("stock_research_report_em", side_effect=_mixed)
    try:
        with patch("lquant.news.sources.tables.get_active_pool", return_value=["600519.SH"]):
            items = EmResearchSource().fetch(DAY)
    finally:
        ctx.stop()
    assert [i.title for i in items] == ["600519 中报点评"]
    assert EmResearchSource.recent_days == 90


def test_pool_size_reads_yaml_config() -> None:
    ctx, _, _ = _patch_ak("stock_research_report_em", ret=_research_df("600519"))
    try:
        with (
            patch.object(tables, "_cfg_int", return_value=7),
            patch("lquant.news.sources.tables.get_active_pool", return_value=[]) as pool,
        ):
            EmResearchSource().fetch(DAY)
    finally:
        ctx.stop()
    pool.assert_called_once_with(7)


# ---------- social ----------


def test_baidu_hot_synthesizes_title_and_content() -> None:
    fake = pd.DataFrame(
        {"名称/代码": ["远东股份"], "涨跌幅": ["+5.19%"], "综合热度": [1084000.0]}
    )
    ctx, _, target = _patch_ak("stock_hot_search_baidu", ret=fake)
    try:
        items = BaiduHotSource().fetch(DAY)
        again = BaiduHotSource().fetch(DAY)
    finally:
        ctx.stop()
    target.assert_called_with(symbol="A股", date="20260911")
    item = items[0]
    assert item.source == "social"
    assert item.source_name == "baidu_hot"
    assert item.title == "远东股份 +5.19%"
    assert item.content == "综合热度 1084000"
    assert item.published_at is None  # 榜单无时间列 → 入库时用采集时刻兜底
    # 无 url 无时间 → 按天 + 标题 hash，同日重复采集幂等
    assert again[0].external_id == item.external_id
    assert item.external_id == hashlib.sha1(f"{DAY.isoformat()}|{item.title}".encode()).hexdigest()[:16]


# ---------- 注册表 ----------


def test_registry_covers_four_categories() -> None:
    sources = get_sources(None)
    assert {s.category for s in sources} == {"telegraph", "news", "social", "report"}
    assert {s.name for s in sources} >= {
        "em_news",
        "cls_telegraph",
        "sina_7x24",
        "em_global",
        "ths_global",
        "futu_global",
        "em_cjzc",
        "cctv_news",
        "em_research",
        "em_notice",
        "baidu_hot",
    }
    for src in sources:
        assert src.name and src.category and callable(src.fetch)
