"""依赖注入。"""
from __future__ import annotations

from functools import lru_cache

from fastapi import HTTPException

from lquant.core.config import Settings, get_settings


@lru_cache(maxsize=1)
def settings() -> Settings:
    return get_settings()


def provider():
    from lquant.data.providers import get_provider

    return get_provider()


def resolve_symbol(raw: str) -> str:
    """各种输入（600519 / 600519.SH / sh.600519）→ 统一 `600519.SH`。

    前端搜索框给的是库里的完整代码，但用户手输裸 6 位是高频操作，
    在 API 边界统一归一，内部层永远拿规范格式。
    """
    from lquant.core.types import parse_symbol

    try:
        return str(parse_symbol(raw))
    except ValueError:
        raise HTTPException(422, f"无效的标的代码: {raw!r}") from None


def bare_code(symbol: str) -> str:
    """`600519.SH` → `600519`（东财等外部源的 secid 用裸码）。"""
    return symbol.split(".", 1)[0]
