"""东财个股新闻采集器(成交额 top N 股票池逐股拉取)。

akshare 接口: ak.stock_news_em(symbol=...)
文档列名: 关键词 / 新闻标题 / 新闻内容 / 新闻链接 / 发布时间
(实现前应实测列名,上游接口变化时 _require_columns 抛 ValueError)。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime
from typing import Any

import akshare as ak
import pandas as pd

from lquant.core.config import load_yaml
from lquant.core.db import reader
from lquant.news.model import NewsItem
from lquant.news.sources.base import register

logger = logging.getLogger(__name__)

_EM_COLS = ["关键词", "新闻标题", "新闻内容", "新闻链接", "发布时间"]
_DEFAULT_POOL_LIMIT = 200


class EmColumnError(ValueError):
    """akshare 返回 DataFrame 缺列(接口契约变化),应向上传播而非按单股失败吞掉。"""


def _pool_limit() -> int:
    """从 config/schema/news.yaml 的 em_news.pool_limit 读;无配置退默认 200。"""
    try:
        cfg = load_yaml("schema/news.yaml")
        return int(cfg.get("em_news", {}).get("pool_limit", _DEFAULT_POOL_LIMIT))
    except (FileNotFoundError, AttributeError, TypeError, ValueError):
        return _DEFAULT_POOL_LIMIT


def get_active_pool(limit: int = 200) -> list[str]:
    """daily_bar 最近交易日成交额 top N;表缺失/查询异常返回 [](不 raise)。"""
    sql = """
        SELECT symbol FROM daily_bar
        WHERE trade_date = (SELECT max(trade_date) FROM daily_bar)
        ORDER BY amount DESC
        LIMIT ?
    """
    try:
        with reader() as con:
            rows = con.execute(sql, [limit]).fetchall()
        return [str(r[0]) for r in rows]
    except Exception:  # noqa: BLE001 - 股票池不可用时降级为空池
        logger.warning("get_active_pool 查询失败,返回空股票池", exc_info=True)
        return []


def _clean_str(value: Any) -> str:
    """去空白;NaN/NaT/None 归为空串。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "nat", "none"} else text


def _parse_time(value: Any) -> datetime | None:
    """时间解析失败返回 None(store 层兜底)。"""
    if not _clean_str(value):
        return None
    try:
        ts = pd.to_datetime(str(value).strip(), errors="raise")
    except (ValueError, TypeError):
        return None
    if pd.isna(ts):
        return None
    return ts.to_pydatetime()


def _map_df(df: pd.DataFrame, symbol: str) -> list[NewsItem]:
    """单股 DataFrame 映射为 NewsItem;缺列抛 EmColumnError。"""
    missing = [c for c in _EM_COLS if c not in df.columns]
    if missing:
        raise EmColumnError(
            f"akshare stock_news_em 缺少列 {missing},实际列名: {df.columns.tolist()}"
        )
    items: list[NewsItem] = []
    for _, row in df.iterrows():
        title = _clean_str(row.get("新闻标题"))
        published_at = _parse_time(row.get("发布时间"))
        if not title:
            continue
        external_id = hashlib.sha1(
            f"{title}{_clean_str(row.get('发布时间'))}".encode()
        ).hexdigest()[:16]
        items.append(
            NewsItem(
                source="news",
                source_name="em",
                external_id=external_id,
                title=title,
                content=_clean_str(row.get("新闻内容")),
                url=_clean_str(row.get("新闻链接")),
                symbols=(symbol,),
                published_at=published_at,
            )
        )
    return items


@register
class EmNewsSource:
    """东财个股新闻;pool=None 时 fetch 内懒加载成交额 top N 股票池。"""

    name = "em_news"
    category = "news"

    def __init__(self, pool: list[str] | None = None) -> None:
        self._pool = pool

    def fetch(self, day: date) -> list[NewsItem]:
        del day  # akshare 只给最近数据,忽略 day
        pool = self._pool if self._pool is not None else get_active_pool(
            _pool_limit()
        )
        items: list[NewsItem] = []
        for symbol in pool:
            try:
                df = ak.stock_news_em(symbol=symbol)
                items.extend(_map_df(df, symbol))
            except EmColumnError:
                # 缺列属接口契约问题,直接向上传播
                raise
            except Exception:  # noqa: BLE001 - 单股失败不中断整个 fetch
                logger.warning("em_news 拉取 %s 失败,跳过", symbol, exc_info=True)
                continue
        return items
