"""标的池语义：别名 ↔ 指数代码 ↔ 成分解析。

评价/回测里「沪深300」这类标的池选项统一收口在这：前端选项、请求校验、
成分解析共用同一张表，避免各处硬编码标的代码。

两族互不干扰：

- **个股池**（本模块直接维护）：指数代码 → 指数成分股，解析走 ``IndexConsRepo``。
- **ETF 池**（实现在 ``factors/etf.py``，此处仅做选项聚合与标签委派）：
  按基金类型（equity/qdii/bond/commodity/money）细分，没有「指数代码」概念，
  中间一列恒为 ``None``。个股池与 ETF 池要放同一张选项表里（前端/API 只枚举
  ``all_universe_options()``），但解析逻辑必须分开 —— 把 ETF 当成裸指数代码
  送进 ``resolve_index_code`` 只会得到「未知股票池」。
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
    """个股池名/指数代码 → 规范指数代码；不认识的池名抛 ValueError。

    只认个股池 —— ETF 池（``etf_*``）由 ``factors.etf.resolve_etf_universe`` 解析，
    调用方必须先分流。所以这里的候选清单**只列个股池**，不把 ETF 池名混进来，
    否则报错信息会给出「可用 etf_all」这种一用就挂的误导提示。
    """
    key = universe.strip().lower()
    if key in _UNIVERSE_BY_KEY:
        return _UNIVERSE_BY_KEY[key][0]
    if universe.strip() in _UNIVERSE_BY_CODE:
        return universe.strip()
    known = "/".join(k for k, _, _ in UNIVERSE_OPTIONS)
    raise ValueError(f"未知股票池 {universe!r}（可用: all/{known} 或指数代码）")


def universe_label(universe: str) -> str:
    """池名 → 中文展示名；未知代码原样返回。

    ``all``（全市场）不在 ``UNIVERSE_OPTIONS`` 里，但它是 API/CLI 的默认值，
    必须能映射成中文 —— 否则报告里会印出裸的 ``all``。
    ETF 池（``etf_*``）委派给 ``factors.etf``，两族标签各处维护、互不硬编码。
    """
    key = universe.strip().lower()
    if key == "all":
        return "全市场"
    from lquant.factors.etf import etf_universe_label, is_etf_universe

    if is_etf_universe(key):
        return etf_universe_label(key)
    if key in _UNIVERSE_BY_KEY:
        return _UNIVERSE_BY_KEY[key][1]
    return universe.strip()


def all_universe_options() -> list[tuple[str, str, (str | None)]]:
    """全部标的池选项 [(别名, 指数代码/None, 展示名)]，供 API/前端枚举。"""
    from lquant.factors.etf import etf_universe_options

    return [
        ("all", None, "全市场"),
        *[(k, code, label) for k, code, label in UNIVERSE_OPTIONS],
        *etf_universe_options(),
    ]
