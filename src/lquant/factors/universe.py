"""股票池语义：别名 ↔ 指数代码 ↔ 成分股解析。

评价/回测里「沪深300」这类股票池选项统一收口在这：前端选项、请求校验、
成分解析共用同一张表，避免各处硬编码指数代码。
"""

from __future__ import annotations

UNIVERSE_OPTIONS: tuple[tuple[str, str, str], ...] = (
    # (别名, 指数代码, 展示名)
    ("hs300", "000300.SH", "沪深300"),
    ("zz500", "000905.SH", "中证500"),
    ("zz800", "000906.SH", "中证800"),
    ("zz1000", "000852.SH", "中证1000"),
)

_UNIVERSE_BY_KEY = {k: (code, label) for k, code, label in UNIVERSE_OPTIONS}
_UNIVERSE_BY_CODE = {code: (code, label) for _, code, label in UNIVERSE_OPTIONS}


def resolve_index_code(universe: str) -> str:
    """池名/指数代码 → 规范指数代码；不认识的池名抛 ValueError。"""
    key = universe.strip().lower()
    if key in _UNIVERSE_BY_KEY:
        return _UNIVERSE_BY_KEY[key][0]
    if universe.strip() in _UNIVERSE_BY_CODE:
        return universe.strip()
    known = "/".join(k for k, _, _ in all_universe_options())
    raise ValueError(f"未知股票池 {universe!r}（可用: all/{known} 或指数代码）")


def universe_label(universe: str) -> str:
    """池名 → 中文展示名；未知代码原样返回。"""
    key = universe.strip().lower()
    if key in _UNIVERSE_BY_KEY:
        return _UNIVERSE_BY_KEY[key][1]
    return universe.strip()


def all_universe_options() -> list[tuple[str, str, (str | None)]]:
    """全部股票池选项 [(别名, 指数代码, 展示名)]，供 API/前端枚举。"""
    return [
        ("all", None, "全市场"),
        *[(k, code, label) for k, code, label in UNIVERSE_OPTIONS],
    ]
