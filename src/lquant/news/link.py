"""资讯与个股/行业关联:词表匹配 + 关键词映射 + 反查。"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from lquant.news.model import NewsItem

_BIT_INFERRED = 4

_MATCHER_CACHE: dict[tuple[str, ...], re.Pattern[str]] = {}


def _matcher(names) -> re.Pattern[str]:
    ordered = sorted(set(names), key=len, reverse=True)
    return re.compile("|".join(re.escape(n) for n in ordered))


def link_symbols(item: NewsItem, name_to_code: dict[str, str]) -> NewsItem:
    """按证券简称词表在标题/正文里匹配,命中则合并 symbols 并打推断位。"""
    if not name_to_code:
        return item
    key = tuple(sorted(name_to_code))
    if key not in _MATCHER_CACHE:
        _MATCHER_CACHE[key] = _matcher(name_to_code.keys())
    pat = _MATCHER_CACHE[key]
    text = f"{item.title or ''}{item.content or ''}"
    hits = list(
        dict.fromkeys(m.group(0) for m in pat.finditer(text) if m.group(0) in name_to_code)
    )
    if not hits:
        return item
    merged = tuple(dict.fromkeys([*item.symbols, *(name_to_code[n] for n in hits)]))
    return replace(item, symbols=merged, quality_flags=item.quality_flags | _BIT_INFERRED)


def link_industry(
    item: NewsItem, kw_map: dict[str, str], symbol_to_industry: dict[str, str]
) -> NewsItem:
    """关键词优先定行业,否则用 symbols 反查;都没有则原样返回。"""
    text = f"{item.title or ''}{item.content or ''}"
    for kw, code in sorted(kw_map.items(), key=lambda kv: -len(kv[0])):
        if kw in text:
            return replace(
                item, industry_code=code, quality_flags=item.quality_flags | _BIT_INFERRED
            )
    for sym in item.symbols:
        if sym in symbol_to_industry:
            return replace(
                item,
                industry_code=symbol_to_industry[sym],
                quality_flags=item.quality_flags | _BIT_INFERRED,
            )
    return item


def build_name_to_code(securities_df: Any) -> dict[str, str]:
    """从证券主表 (name, symbol) 构建 {name: symbol} 词表,丢弃空名。"""
    return {
        str(r["name"]).strip(): str(r["symbol"])
        for r in securities_df.iter_rows(named=True)
        if r["name"]
    }
