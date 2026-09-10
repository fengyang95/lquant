"""财联社电报 / 新浪 7x24 快讯采集器。

akshare 仅提供最近数据,fetch 忽略 day 参数(保留在签名中以满足 NewsSource 协议)。

列名以 akshare 1.18.94 实测为准:
- ak.stock_info_global_cls(): 标题 / 内容 / 发布日期 / 发布时间
- ak.stock_info_global_sina(): 时间 / 内容
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Any

import akshare as ak
import pandas as pd

from lquant.news.model import NewsItem
from lquant.news.sources.base import register

_CLS_SOURCE = "cls"
_SINA_SOURCE = "sina_7x24"


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
                    url="",
                    published_at=published_at,
                )
            )
        return items

    def _fetch_sina(self) -> list[NewsItem]:
        df = ak.stock_info_global_sina()
        return self._map_df(df, _SINA_SOURCE, is_cls=False)


@register
class ClsTelegraphSource(_TelegraphMixin):
    """财联社电报;接口失败时 fallback 新浪 7x24。"""

    name = "cls_telegraph"

    def fetch(self, day: date) -> list[NewsItem]:
        del day  # akshare 只给最近数据,忽略 day
        try:
            # 仅对 akshare 调用本身(网络/接口错误)做 fallback;
            # 映射阶段的缺列 ValueError 不在此 except 范围内,向上传播
            df = ak.stock_info_global_cls()
        except Exception:  # noqa: BLE001 - 上游任何异常都走 fallback
            return self._fetch_sina()
        return self._map_df(df, _CLS_SOURCE, is_cls=True)


@register
class SinaTelegraphSource(_TelegraphMixin):
    """新浪 7x24 快讯,独立可注册调用。"""

    name = "sina_7x24"

    def fetch(self, day: date) -> list[NewsItem]:
        del day  # akshare 只给最近数据,忽略 day
        return self._fetch_sina()
