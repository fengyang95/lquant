"""NewsSource 协议与来源注册表。"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from lquant.news.model import NewsItem


class NewsSource(Protocol):
    name: str
    category: str

    def fetch(self, day: date) -> list[NewsItem]: ...


_REGISTRY: dict[str, type] = {}


def register(cls: type) -> type:
    """按类的 name 属性注册来源类。"""
    _REGISTRY[cls.name] = cls
    return cls


def get_sources(names: list[str] | None = None) -> list:
    """实例化指定来源;names 为 None 时返回全部。未知 name 抛 KeyError。"""
    if names is None:
        names = list(_REGISTRY)
    missing = set(names) - set(_REGISTRY)
    if missing:
        raise KeyError(f"unknown news source: {missing}")
    return [_REGISTRY[n]() for n in names]
