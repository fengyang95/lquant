"""ETF 标的池：按基金类型解析 + 三级降级 + 与个股池共存。

全部离线：数据源通过参数注入（etf_meta / security_symbols / daily_symbols），
不碰 DuckDB、不碰日线湖。
"""
from __future__ import annotations

import polars as pl
import pytest

_ETF = ["510300.SH", "510050.SH", "588000.SH", "159915.SZ", "513100.SH",
        "513050.SH", "512880.SH", "512690.SH", "511260.SH", "518880.SH"]
_TYPES = ["equity", "equity", "equity", "equity", "qdii",
          "qdii", "equity", "equity", "bond", "commodity"]


def _meta(symbols=_ETF, types=_TYPES):
    if len(types) != len(symbols):        # 传入自定义 symbols 时按 equity 兜底
        types = ["equity"] * len(symbols)
    return pl.DataFrame(
        {"symbol": symbols, "fund_type": types},
        schema={"symbol": pl.Utf8, "fund_type": pl.Utf8},
    )


def _meta_without_type(symbols=_ETF):
    return pl.DataFrame(
        {"symbol": symbols, "fund_type": [None] * len(symbols)},
        schema={"symbol": pl.Utf8, "fund_type": pl.Utf8},
    )


# ---------------- 基本判定 ----------------

def test_is_etf_universe():
    from lquant.factors.etf import is_etf_universe

    assert is_etf_universe("etf_all")
    assert is_etf_universe("ETF_EQUITY")       # 大小写无关
    assert is_etf_universe(" etf_qdii ")
    assert not is_etf_universe("hs300")
    assert not is_etf_universe("all")
    assert not is_etf_universe("etf_nope")     # 前缀对但不在册
    assert not is_etf_universe(None)


def test_is_etf_symbol_uses_platform_sec_type():
    """必须与回测规则同一口径（core.types.Symbol.sec_type），不能另起代码段表。"""
    from lquant.factors.etf import is_etf_symbol

    for s in _ETF:
        assert is_etf_symbol(s), s
    for s in ("600000.SH", "000001.SZ", "300750.SZ", "688981.SH", "000300.SH"):
        assert not is_etf_symbol(s), s
    assert not is_etf_symbol("这不是代码")


def test_universe_label():
    from lquant.factors.etf import etf_universe_label

    assert etf_universe_label("etf_all") == "全部ETF"
    assert etf_universe_label("etf_qdii") == "跨境ETF"
    assert etf_universe_label("nope") == "nope"


# ---------------- 解析：首选 etf_meta ----------------

def test_resolve_all_from_meta():
    from lquant.factors.etf import resolve_etf_universe

    syms, note = resolve_etf_universe("etf_all", etf_meta=_meta())
    assert note is None
    assert syms == sorted(_ETF)


def test_resolve_by_fund_type():
    from lquant.factors.etf import resolve_etf_universe

    syms, note = resolve_etf_universe("etf_qdii", etf_meta=_meta())
    assert note is None
    assert syms == ["513050.SH", "513100.SH"]

    syms, note = resolve_etf_universe("etf_commodity", etf_meta=_meta())
    assert note is None
    assert syms == ["518880.SH"]

    syms, note = resolve_etf_universe("etf_money", etf_meta=_meta())
    # 该类型一只都没有 → 退化为全 ETF 口径，且必须披露
    assert note is not None and "fund_type=money" in note
    assert syms == sorted(_ETF)


def test_fund_type_all_null_degrades_with_note():
    """demo 数据就是这种：etf_meta 有行但 fund_type 全 NULL。

    不能静默返回空池（那会让 etf_equity 变成 503），必须退化 + 显式披露。
    """
    from lquant.factors.etf import resolve_etf_universe

    syms, note = resolve_etf_universe("etf_equity", etf_meta=_meta_without_type())
    assert note is not None and "fund_type 未同步" in note
    assert syms == sorted(_ETF)


def test_fund_type_column_absent_degrades():
    from lquant.factors.etf import resolve_etf_universe

    meta = pl.DataFrame({"symbol": _ETF})
    syms, note = resolve_etf_universe("etf_bond", etf_meta=meta)
    assert note is not None and "无 fund_type 列" in note
    assert syms == sorted(_ETF)


# ---------------- 解析：逐级降级 ----------------

def test_empty_meta_falls_back_to_security():
    from lquant.factors.etf import resolve_etf_universe

    syms, note = resolve_etf_universe(
        "etf_all", etf_meta=pl.DataFrame(schema={"symbol": pl.Utf8}),
        security_symbols=["510300.SH", "159915.SZ"],
    )
    assert syms == ["159915.SZ", "510300.SH"]
    assert note is not None and "security" in note


def test_meta_and_security_empty_falls_back_to_daily():
    from lquant.factors.etf import resolve_etf_universe

    syms, note = resolve_etf_universe(
        "etf_all",
        etf_meta=pl.DataFrame(schema={"symbol": pl.Utf8}),
        security_symbols=[],
        daily_symbols=["510300.SH", "600000.SH"],   # 湖里混着个股
    )
    assert syms == ["510300.SH"]
    assert note is not None and "日线湖" in note


def test_all_sources_empty_raises():
    from lquant.factors.etf import resolve_etf_universe

    with pytest.raises(ValueError, match="ETF 池为空"):
        resolve_etf_universe(
            "etf_all", etf_meta=pl.DataFrame(schema={"symbol": pl.Utf8}),
            security_symbols=[], daily_symbols=[],
        )


def test_unknown_pool_raises():
    from lquant.factors.etf import resolve_etf_universe

    with pytest.raises(ValueError, match="未知 ETF 池"):
        resolve_etf_universe("etf_nope", etf_meta=_meta())


def test_dedup_and_sorted():
    from lquant.factors.etf import resolve_etf_universe

    syms, _ = resolve_etf_universe(
        "etf_all", etf_meta=_meta(symbols=["510300.SH", "510300.SH", "159915.SZ"])
    )
    assert syms == ["159915.SZ", "510300.SH"]


# ---------------- 与个股池共存 ----------------

def test_pools_merged_into_all_universe_options():
    from lquant.factors.etf import etf_universe_options
    from lquant.factors.universe import all_universe_options

    keys = [k for k, _, _ in all_universe_options()]
    assert keys[0] == "all"
    for k in ("hs300", "zz500"):
        assert k in keys
    for k, _ft, _label in etf_universe_options():
        assert k in keys
    # ETF 池没有指数代码 → 中间一列必须是 None（否则会被误当指数代码送进 IndexConsRepo）
    assert all(code is None for _, code, _ in etf_universe_options())


def test_stock_resolver_does_not_advertise_etf_pools():
    """resolve_index_code 的候选清单不该混入 ETF 池名（给了也一定挂）。"""
    from lquant.factors.universe import resolve_index_code

    with pytest.raises(ValueError) as ei:
        resolve_index_code("etf_all")
    msg = str(ei.value)
    assert "可用: all/hs300" in msg
    assert "etf" not in msg.split("可用", 1)[1]      # 候选清单里没有 ETF 池


def test_universe_label_delegates_to_etf():
    from lquant.factors.universe import universe_label

    assert universe_label("etf_all") == "全部ETF"
    assert universe_label("hs300") == "沪深300"
    assert universe_label("all") == "全市场"
