"""财联社电报 / 新浪 7x24 快讯采集器。

akshare 仅提供最近数据,fetch 忽略 day 参数(保留在签名中以满足 NewsSource 协议)。

优先直连源站拿**真实原文链接**（akshare 封装会丢弃 id/docurl），
直连失败再退回 akshare + 来源主页兜底链接 —— 保证每条资讯都可点击。

列名以 akshare 1.18.94 实测为准:
- ak.stock_info_global_cls(): 标题 / 内容 / 发布日期 / 发布时间
- ak.stock_info_global_sina(): 时间 / 内容
"""

from __future__ import annotations

import hashlib
import time
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd

from lquant.news.model import NewsItem
from lquant.news.sources.akcompat import ak_call
from lquant.news.sources.base import register

_CLS_SOURCE = "cls_telegraph"  # 与注册名一致，否则 /sources 聚合与 source 过滤对不上
_SINA_SOURCE = "sina_7x24"

_CLS_HOME = "https://www.cls.cn/telegraph"
_SINA_HOME = "https://finance.sina.com.cn/7x24/"

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Referer": "https://www.cls.cn/",
}


def _http_json(url: str, params: dict[str, str], *, referer: str | None = None) -> dict:
    """源站接口 GET → json。测试用 monkeypatch 替换本函数。"""
    import requests

    headers = _HEADERS if referer is None else {**_HEADERS, "Referer": referer}
    resp = requests.get(url, params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _fetch_cls_direct() -> list[NewsItem]:
    """财联社电报直连（保留 id → 原文链接 https://www.cls.cn/detail/{id}）。

    签名算法与 akshare 一致：md5(sha1(urlencode(params)))。
    """
    url = "https://www.cls.cn/v1/roll/get_roll_list"
    params: dict[str, str] = {
        "app": "CailianpressWeb",
        "category": "",
        "last_time": str(int(time.time())),
        "os": "web",
        "refresh_type": "1",
        "rn": "20",
        "sv": "8.4.6",
    }
    sign = hashlib.md5(
        hashlib.sha1(urlencode(params).encode("utf-8")).hexdigest().encode("utf-8")
    ).hexdigest()
    data = _http_json(url, {**params, "sign": sign})
    roll = ((data.get("data") or {}).get("roll_data")) or []
    items: list[NewsItem] = []
    for it in roll:
        content = _clean_str(it.get("content"))
        if not content:
            continue
        ctime = it.get("ctime")
        published = (
            datetime.fromtimestamp(int(ctime), tz=ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
            if ctime else None
        )
        title = _clean_str(it.get("title"))
        article_url = f"https://www.cls.cn/detail/{it['id']}" if it.get("id") else _CLS_HOME
        # external_id 与 akshare 路径同构（日期+时间），两条路径去重键一致
        external_id = (
            published.strftime("%Y-%m-%d%H:%M:%S")
            if published
            else hashlib.sha1(content.encode()).hexdigest()[:16]
        )
        items.append(NewsItem(
            source="telegraph",
            source_name=_CLS_SOURCE,
            external_id=external_id,
            title=title,
            content=content,
            url=article_url,
            published_at=published,
        ))
    return items


def _fetch_sina_direct() -> list[NewsItem]:
    """新浪 7x24 直连（保留 docurl → 原文链接；无 docurl 时用频道页兜底）。"""
    url = "https://zhibo.sina.com.cn/api/zhibo/feed"
    params: dict[str, str] = {
        "page": "1", "page_size": "20", "zhibo_id": "152", "tag_id": "0",
        "dire": "f", "dpc": "1", "pagesize": "20", "type": "1",
    }
    data = _http_json(url, params, referer="https://finance.sina.com.cn/7x24/")
    feed = ((data.get("result") or {}).get("data") or {}).get("feed") or {}
    rows = feed.get("list") or []
    items: list[NewsItem] = []
    for it in rows:
        content = _clean_str(it.get("rich_text"))
        if not content:
            continue
        published = _parse_time(it.get("create_time"))
        # external_id 用 sha1(content)[:16]，与 akshare 路径一致保证去重
        external_id = hashlib.sha1(content.encode()).hexdigest()[:16]
        items.append(NewsItem(
            source="telegraph",
            source_name=_SINA_SOURCE,
            external_id=external_id,
            title=_clean_str(it.get("doc_title")) if it.get("doc_title") else "",
            content=content,
            url=_clean_str(it.get("docurl")) or _SINA_HOME,
            published_at=published,
        ))
    return items


def _require_columns(df: pd.DataFrame, cols: list[str], origin: str) -> None:
    """缺列时抛 ValueError 并带上实际列名,便于上游接口变化时排查。"""
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(
            f"akshare {origin} 缺少列 {missing},实际列名: {df.columns.tolist()}"
        )


def _parse_time(value: Any) -> datetime | None:
    """时间解析失败返回 None(store 层兜底)。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        ts = pd.to_datetime(str(value).strip(), errors="raise")
    except (ValueError, TypeError):
        return None
    if pd.isna(ts):
        return None
    return ts.to_pydatetime()


def _iter_rows(df: pd.DataFrame, cols: list[str]) -> list[dict[str, Any]]:
    _require_columns(df, cols, origin=cols[0])
    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        rows.append({c: row.get(c) for c in cols})
    return rows


def _clean_str(value: Any) -> str:
    """去空白;NaN/NaT/None 归为空串。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "nat", "none"} else text


def _cls_published_at(row: dict[str, Any]) -> datetime | None:
    """发布日期 + 发布时间 拼接解析;任一失败返回 None。"""
    date_part = _clean_str(row.get("发布日期"))
    time_part = _clean_str(row.get("发布时间"))
    if not date_part or not time_part:
        return None
    return _parse_time(f"{date_part} {time_part}")


class _TelegraphMixin:
    """电报类来源共用的映射逻辑。"""

    category = "telegraph"

    def _map_df(
        self,
        df: pd.DataFrame,
        source_name: str,
        is_cls: bool,
        fallback_url: str = "",
    ) -> list[NewsItem]:
        cols = ["内容", "标题", "发布日期", "发布时间"] if is_cls else ["内容", "时间"]
        items: list[NewsItem] = []
        for row in _iter_rows(df, cols):
            content = str(row.get("内容") or "").strip()
            if not content:
                continue
            published_at = (
                _cls_published_at(row)
                if is_cls
                else _parse_time(row.get("时间"))
            )
            title_raw = row.get("标题")
            title = str(title_raw).strip() if title_raw is not None else ""
            if is_cls:
                # 日期+时间拼接;时间缺失时退化为 sha1(content)[:16],
                # 避免同日多条快讯 external_id 碰撞被去重误删
                date_part = _clean_str(row.get("发布日期"))
                time_part = _clean_str(row.get("发布时间"))
                if date_part and time_part:
                    external_id = f"{date_part}{time_part}"
                else:
                    external_id = hashlib.sha1(content.encode()).hexdigest()[:16]
            else:
                # sina: sha1(content)[:16]
                external_id = hashlib.sha1(content.encode()).hexdigest()[:16]
            items.append(
                NewsItem(
                    source="telegraph",
                    source_name=source_name,
                    external_id=external_id,
                    title=title,
                    content=content,
                    url=fallback_url,
                    published_at=published_at,
                )
            )
        return items

    def _fetch_sina(self) -> list[NewsItem]:
        df = ak_call(ak.stock_info_global_sina)
        return self._map_df(df, _SINA_SOURCE, is_cls=False, fallback_url=_SINA_HOME)


@register
class ClsTelegraphSource(_TelegraphMixin):
    """财联社电报;直连优先，接口失败时 fallback akshare / 新浪 7x24。"""

    name = "cls_telegraph"

    def fetch(self, day: date) -> list[NewsItem]:
        del day  # akshare 只给最近数据,忽略 day
        try:
            items = _fetch_cls_direct()
            if items:
                return items
        except Exception as e:  # noqa: BLE001 - 直连任何异常都退回 akshare 路径
            print(f"[warn] cls 直连失败，退回 akshare: {e}")
        try:
            # 仅对 akshare 调用本身(网络/接口错误)做 fallback;
            # 映射阶段的缺列 ValueError 不在此 except 范围内,向上传播
            df = ak_call(ak.stock_info_global_cls)
        except Exception:  # noqa: BLE001 - 上游任何异常都走 fallback
            return self._fetch_sina()
        return self._map_df(df, _CLS_SOURCE, is_cls=True, fallback_url=_CLS_HOME)


@register
class SinaTelegraphSource(_TelegraphMixin):
    """新浪 7x24 快讯,直连优先，独立可注册调用。"""

    name = "sina_7x24"

    def fetch(self, day: date) -> list[NewsItem]:
        del day  # akshare 只给最近数据,忽略 day
        try:
            items = _fetch_sina_direct()
            if items:
                return items
        except Exception as e:  # noqa: BLE001 - 直连失败退回 akshare
            print(f"[warn] sina 直连失败，退回 akshare: {e}")
        return self._fetch_sina()
