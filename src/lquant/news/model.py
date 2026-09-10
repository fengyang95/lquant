"""资讯条目模型。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class NewsItem:
    source: str  # telegraph / news / social / report
    source_name: str  # cls / sina_7x24 / em / ...
    external_id: str
    title: str
    content: str
    url: str
    symbols: tuple[str, ...] = ()
    industry_code: str | None = None
    published_at: datetime | None = None
    collected_at: datetime | None = None
    quality_flags: int = 0
    source_tag: str = ""

    @property
    def news_id(self) -> str:
        """去重主键:sha1(source_name|external_id)。"""
        return hashlib.sha1(f"{self.source_name}|{self.external_id}".encode()).hexdigest()
