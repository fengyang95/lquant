"""声明式表格型资讯采集器：快讯 / 宏观新闻 / 研报 / 公告 / 榜单。

akshare 大多数接口返回的都是"一张表 + 固定列名"，逐个手写映射既啰嗦，又容易在上游
改列名时静默产出空数据。这里抽出一个声明式的 `TableSource` 基类：子类只声明
akshare 函数名、列映射与抓取范围，公共逻辑（列名解析 / 行映射 / 时间解析 /
external_id 生成 / 截断 / 逐标的循环）由基类统一实现。

列名以 akshare 1.18.94 实测为准；上游改列名时抛 `SourceColumnError`（ValueError
子类）并带上实际列名（与 telegraph.py 的 `_require_columns` 同一约定），采集任务会
把它记成该来源 `failed`，而不是静默 0 行。

覆盖四类来源（见 docs/superpowers/specs/2026-09-10-industry-news-design.md）：
- telegraph: em_global(东财全球快讯) / ths_global(同花顺全球快讯) / futu_global(富途快讯)
- news:      em_cjzc(东财财经早餐) / cctv_news(新闻联播文字稿)
- report:    em_research(东财个股研报) / em_notice(东财公告)
- social:    baidu_hot(百度股市热搜榜)
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

import akshare as ak
import pandas as pd

from lquant.core.config import load_yaml
from lquant.news.model import NewsItem
from lquant.news.sources.akcompat import ak_call
from lquant.news.sources.base import register
from lquant.news.sources.em_news import get_active_pool

logger = logging.getLogger(__name__)

_SUFFIX = {"6": "SH", "0": "SZ", "3": "SZ", "4": "BJ", "8": "BJ"}


def _cfg_int(section: str, key: str, default: int) -> int:
    """读 news.yaml 的 <section>.<key>；配置缺失/非法一律降级 default。"""
    try:
        value = load_yaml("schema/news.yaml").get(section, {}).get(key, default)
        return int(value)
    except (FileNotFoundError, AttributeError, TypeError, ValueError):
        return default


def _clean(value: Any) -> str:
    """去空白；NaN/NaT/None 归为空串。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "nat", "none"} else text


def _parse_time(value: Any) -> datetime | None:
    """时间解析失败返回 None（store 层会用采集时刻兜底 + 打 bit1）。"""
    if not _clean(value):
        return None
    try:
        stamp = pd.to_datetime(str(value).strip(), errors="raise")
    except (ValueError, TypeError):
        return None
    if pd.isna(stamp):
        return None
    return stamp.to_pydatetime()


def to_symbol(code: str) -> str | None:
    """6 位 A 股代码 → 库内 symbol 风格（600519 → 600519.SH）；非 A 股返回 None。

    9 段需要区分：900xxx 是沪市 B 股（.SH），92xxxx 是北交所（.BJ）。
    """
    code = _clean(code)
    if len(code) != 6 or not code.isdigit():
        return None
    if code[0] == "9":
        return f"{code}.SH" if code.startswith("900") else f"{code}.BJ"
    suffix = _SUFFIX.get(code[0])
    return f"{code}.{suffix}" if suffix else None


@dataclass(frozen=True)
class Cols:
    """逻辑字段 → 上游候选列名（按声明顺序取第一个实际存在的列）。"""

    title: tuple[str, ...] = ()
    content: tuple[str, ...] = ()
    time: tuple[str, ...] = ()
    url: tuple[str, ...] = ()
    code: tuple[str, ...] = ()


def _select(df: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    for col in candidates:
        if col in df.columns:
            return col
    return None


class SourceColumnError(ValueError):
    """上游列名与声明不符（接口契约变化）——向上传播，不静默产出空数据。"""


class TableSource:
    """声明式表格采集器基类。子类覆盖 name/category/source_name/fn/cols 即可。"""

    name: str = ""
    category: str = "news"
    source_name: str = ""
    fn: str = ""  # akshare 属性名
    cols: Cols = Cols()
    required: tuple[str, ...] = ("title",)
    kwargs: dict[str, Any] = {}  # 固定请求参数
    day_arg: str | None = None  # 支持日期参数的接口：参数名
    day_format: str = "%Y%m%d"
    default_publish_time: str | None = None  # 无时间列时用请求日 + 该时刻
    pool_arg: str | None = None  # 逐标的接口的参数名
    pool_limit: int = 0
    pool_bare_code: bool = True  # 池内 600519.SH → 600519
    limit: int | None = None  # 单次最多产出条数
    recent_days: int | None = None  # 只保留请求日往前 N 天内的条目（研报接口会返回多年历史）
    fallback_url: str = ""  # 上游无链接列时的兜底链接（保证每条资讯可点击）


    # ---------- 抓取 ----------

    def fetch(self, day: date) -> list[NewsItem]:
        if self.pool_arg is not None:
            return self._fetch_by_pool(day)
        return self._convert(self._call(day), day)

    def _call(self, day: date, extra: dict[str, Any] | None = None) -> pd.DataFrame:
        kwargs = {**self.kwargs, **(extra or {})}
        if self.day_arg is not None:
            kwargs[self.day_arg] = day.strftime(self.day_format)
        return ak_call(getattr(ak, self.fn), **kwargs)

    def _fetch_by_pool(self, day: date) -> list[NewsItem]:
        """逐标的拉取（研报类接口按股查询）。单标的失败不中断整个来源。"""
        items: list[NewsItem] = []
        pool = get_active_pool(self._pool_size())
        if not pool:
            logger.warning("%s 股票池为空（行情湖不可用或 pool_limit=0），本次产出 0 条", self.name)
        for symbol in pool:
            code = symbol.split(".")[0] if self.pool_bare_code else symbol
            try:
                df = self._call(day, {self.pool_arg: code})
            except Exception:  # noqa: BLE001 - 单标的失败跳过，缺列错误由 _convert 抛出
                logger.warning("%s 拉取 %s 失败，跳过", self.name, symbol, exc_info=True)
                continue
            items.extend(self._convert(df, day, symbols=(symbol,)))
        return items

    def _pool_size(self) -> int:
        return _cfg_int(self.name, "pool_limit", self.pool_limit)

    def _row_limit(self) -> int | None:
        return None if self.limit is None else _cfg_int(self.name, "limit", self.limit)

    def _window_days(self) -> int | None:
        return (
            None
            if self.recent_days is None
            else _cfg_int(self.name, "recent_days", self.recent_days)
        )

    # ---------- 映射 ----------

    def _convert(
        self, df: pd.DataFrame, day: date, symbols: tuple[str, ...] = ()
    ) -> list[NewsItem]:
        resolved = {
            key: _select(df, getattr(self.cols, key))
            for key in ("title", "content", "time", "url", "code")
        }
        missing = [key for key in self.required if resolved[key] is None]
        if missing:
            raise SourceColumnError(
                f"akshare {self.fn} 缺少列 {missing}，实际列名: {df.columns.tolist()}"
            )
        row_limit = self._row_limit()
        window_days = self._window_days()
        items: list[NewsItem] = []
        for row in df.to_dict("records"):
            item = self._one(row, resolved, day, symbols)
            if item is None:
                continue
            if (
                window_days is not None
                and item.published_at is not None
                and (day - item.published_at.date()).days > window_days
            ):
                continue  # 研报类接口会带上多年历史，只留近期窗口
            items.append(item)
            if row_limit is not None and len(items) >= row_limit:
                break
        return items

    def _one(
        self,
        row: dict[str, Any],
        resolved: dict[str, str | None],
        day: date,
        symbols: tuple[str, ...],
    ) -> NewsItem | None:
        title_col, content_col = resolved["title"], resolved["content"]
        title = _clean(row.get(title_col)) if title_col else ""
        content = _clean(row.get(content_col)) if content_col else ""
        if not title and not content:
            return None
        title = self._derive_title(title, content, row)
        content = self._derive_content(content, row)

        time_col, url_col = resolved["time"], resolved["url"]
        published_at = _parse_time(row.get(time_col)) if time_col else None
        if published_at is None and self.default_publish_time:
            published_at = datetime.combine(day, time.fromisoformat(self.default_publish_time))
        # 先按"真实 url"算 external_id，再应用兜底链接 —— 否则同一兜底 URL
        # 会让同日全部条目共享同一 sha1(url) 去重键而被误合并
        real_url = _clean(row.get(url_col)) if url_col else ""
        external_id = self._external_id(title, content, real_url, published_at, day)
        url = real_url or self._fallback_url(day)

        result_symbols = tuple(symbols)
        code_col = resolved["code"]
        if not result_symbols and code_col:
            mapped = to_symbol(_clean(row.get(code_col)))
            if mapped:
                result_symbols = (mapped,)

        return NewsItem(
            source=self.category,
            source_name=self.source_name,
            external_id=external_id,
            title=title,
            content=content,
            url=url,
            symbols=result_symbols,
            published_at=published_at,
        )

    def _derive_title(self, title: str, content: str, row: dict[str, Any]) -> str:
        """标题派生钩子（榜单类来源用它补上榜单名/涨跌幅）。"""
        return title

    def _fallback_url(self, day: date) -> str:
        """无链接列时的兜底链接钩子（cctv 覆写为当日列表页）。"""
        return self.fallback_url

    def _derive_content(self, content: str, row: dict[str, Any]) -> str:
        """正文派生钩子（研报/公告类用它把多个字段拼成摘要）。"""
        return content

    def _external_id(
        self,
        title: str,
        content: str,
        url: str,
        published_at: datetime | None,
        day: date,
    ) -> str:
        """去重键：优先原文链接，其次时间+标题，最后按天 + 标题/正文 hash。"""
        if url:
            return hashlib.sha1(url.encode()).hexdigest()[:16]
        if published_at is not None and title:
            return f"{published_at:%Y%m%d%H%M%S}{title}"
        return hashlib.sha1(f"{day.isoformat()}|{title or content}".encode()).hexdigest()[:16]


# ---------- telegraph：全球快讯 ----------


@register
class EmGlobalSource(TableSource):
    """东方财富全球快讯（带原文链接，条目最多）。"""

    name = "em_global"
    category = "telegraph"
    source_name = "em_global"
    fn = "stock_info_global_em"
    cols = Cols(title=("标题",), content=("摘要",), time=("发布时间",), url=("链接",))


@register
class ThsGlobalSource(TableSource):
    """同花顺全球快讯。"""

    name = "ths_global"
    category = "telegraph"
    source_name = "ths_global"
    fn = "stock_info_global_ths"
    cols = Cols(title=("标题",), content=("内容",), time=("发布时间",), url=("链接",))


@register
class FutuGlobalSource(TableSource):
    """富途快讯（多为海外宏观/地缘）。"""

    name = "futu_global"
    category = "telegraph"
    source_name = "futu_global"
    fn = "stock_info_global_futu"
    cols = Cols(title=("标题",), content=("内容",), time=("发布时间",), url=("链接",))


# ---------- news：宏观 / 财经新闻 ----------


@register
class EmCjzcSource(TableSource):
    """东方财富财经早餐（每日一篇，接口返回历史多日，按 limit 截断）。"""

    name = "em_cjzc"
    category = "news"
    source_name = "em_cjzc"
    fn = "stock_info_cjzc_em"
    cols = Cols(title=("标题",), content=("摘要",), time=("发布时间",), url=("链接",))
    limit = 30


@register
class CctvNewsSource(TableSource):
    """新闻联播文字稿（按日抓取，宏观政策面；接口只给 date/title/content）。"""

    name = "cctv_news"
    category = "news"
    source_name = "cctv"
    fn = "news_cctv"
    cols = Cols(title=("title",), content=("content",))
    day_arg = "date"
    default_publish_time = "19:00:00"  # 联播播出时刻，替代缺失的时间列
    fallback_url = "https://tv.cctv.com/lm/xwlb/"  # 兜底 = 新闻联播栏目页

    def _fallback_url(self, day: date) -> str:
        # 接口只有 date/title/content：兜底链接用当日列表页，天然逐日唯一
        return f"https://tv.cctv.com/lm/xwlb/day/{day:%Y%m%d}.shtml"


# ---------- report：研报 / 公告 ----------

_RESEARCH_SUMMARY_FIELDS = ("机构", "东财评级", "行业")


@register
class EmResearchSource(TableSource):
    """东方财富个股研报（活跃池逐股，摘要 = 机构 · 评级 · 行业）。

    接口按个股返回**全部历史**研报（可回溯到 2017 年），因此用 90 天窗口收口，
    否则单个活跃池就能灌进上千条陈年研报把资讯流冲垮。
    """

    name = "em_research"
    category = "report"
    source_name = "em_research"
    fn = "stock_research_report_em"
    cols = Cols(
        title=("报告名称",),
        time=("日期",),
        url=("报告PDF链接",),
        code=("股票代码",),
    )
    pool_arg = "symbol"
    pool_limit = 40
    recent_days = 90

    def _derive_content(self, content: str, row: dict[str, Any]) -> str:
        parts = [_clean(row.get(field)) for field in _RESEARCH_SUMMARY_FIELDS]
        return " · ".join(part for part in parts if part) or content


@register
class EmNoticeSource(TableSource):
    """东方财富公告（全市场按日；标题即公告标题，摘要为公告类型）。"""

    name = "em_notice"
    category = "report"
    source_name = "em_notice"
    fn = "stock_notice_report"
    kwargs = {"symbol": "全部"}
    day_arg = "date"
    cols = Cols(title=("公告标题",), time=("公告日期",), url=("网址",), code=("代码",))
    limit = 300

    def _derive_content(self, content: str, row: dict[str, Any]) -> str:
        return _clean(row.get("公告类型")) or content


# ---------- social：人气榜 ----------


@register
class BaiduHotSource(TableSource):
    """百度股市热搜榜（人气面；接口只有 名称/代码、涨跌幅、综合热度）。"""

    name = "baidu_hot"
    category = "social"
    source_name = "baidu_hot"
    fn = "stock_hot_search_baidu"
    kwargs = {"symbol": "A股"}
    day_arg = "date"
    cols = Cols(title=("名称/代码",))
    fallback_url = "https://gushitong.baidu.com/"  # 热搜榜无单条链接 → 百度股市通首页

    def _derive_title(self, title: str, content: str, row: dict[str, Any]) -> str:
        pct = _clean(row.get("涨跌幅"))
        return f"{title} {pct}".strip()

    def _derive_content(self, content: str, row: dict[str, Any]) -> str:
        heat = row.get("综合热度")
        if isinstance(heat, float) and heat.is_integer():
            heat = int(heat)
        heat_text = _clean(heat)
        return f"综合热度 {heat_text}" if heat_text else content
