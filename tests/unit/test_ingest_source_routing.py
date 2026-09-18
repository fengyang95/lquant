"""日线回填的**取数源路由**与回填池口径回归测试（不联网）。

回归的两个真实缺陷（2026-09-18 定位）：

1. 主源被设成 tushare 时（app_setting.providers_order=tushare），回填池
   `backfill_pool` 盲取 ``chain.providers[0]`` → tushare 的 ``pro.daily``
   只含股票，ETF/LOF 段整段零行，被记成 ``empty_response``：
   实测 2026-09-17 任务 1582 只 ETF + 85 只 LOF + 508 只指数全部失败，
   ETF 湖停在 2026-09-11 不再更新。
2. 回填池用 ``sec_type NOT IN ('etf','lof')`` 建「股票段」，把 508 只指数
   一起拉进来 —— 与 ``SecurityRepo.active_symbols`` 的「日线湖只收
   stock/etf/lof」契约冲突，且指数点位超价格护栏（>10000）触发 fatal。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from lquant.core.types import parse_symbol
from lquant.data.capability import Capability

D = date(2024, 1, 5)
START = date(2024, 1, 1)


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LQ_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from lquant.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def no_lake(monkeypatch):
    """绕开真实血缘/入湖：_stamp 恒等，write_daily 只记账。"""
    written: list[pl.DataFrame] = []
    monkeypatch.setattr("lquant.data.ingest.daily._stamp", lambda df, source="baostock": df)
    monkeypatch.setattr("lquant.data.ingest.daily.write_daily", written.append)
    return written


def _frame(symbols: list[str]) -> pl.DataFrame:
    if not symbols:
        return pl.DataFrame()
    return pl.DataFrame({
        "symbol": list(symbols),
        "date": [D] * len(symbols),
        "close": [1.0] * len(symbols),
    })


class _FakeProv:
    """最小 provider 协议：name/source/capability + has（对齐 DataProvider）。"""

    name = "fake"
    source = "fake"
    capability: frozenset = frozenset()

    def has(self, cap) -> bool:
        c = Capability.parse(cap) if isinstance(cap, str) else cap
        return c in self.capability


class _StockOnlyProvider(_FakeProv):
    """tushare 形态：只认股票，基金/指数一律零行（不抛错）。"""

    name = "tushare"
    source = "tushare"
    capability = frozenset({Capability.DAILY, Capability.ETF_DAILY})

    def __init__(self) -> None:
        self.daily_calls: list[list[str]] = []
        self.etf_calls: list[list[str]] = []

    def daily_bars(self, symbols, start, end):
        self.daily_calls.append(list(symbols))
        stocks = [s for s in symbols if parse_symbol(s).sec_type.value == "stock"]
        return _frame(stocks)          # 基金/指数被静默丢掉

    def etf_daily_bars(self, symbols, start, end):
        # 真实 tushare 的 fund_daily 路径：本测试里「只被调用」即达目的
        self.etf_calls.append(list(symbols))
        return _frame(list(symbols))


class _FundCapableProvider(_FakeProv):
    """baostock 形态：daily_bars 一把梭，ETF 也能取（无 etf_daily_bars）。"""

    name = "baostock"
    source = "baostock"
    capability = frozenset({Capability.DAILY, Capability.ETF_DAILY})

    def __init__(self) -> None:
        self.daily_calls: list[list[str]] = []

    def daily_bars(self, symbols, start, end):
        self.daily_calls.append(list(symbols))
        return _frame(list(symbols))


class _Chain:
    def __init__(self, providers) -> None:
        self.providers = list(providers)


def _patch_chain(monkeypatch, chain: _Chain) -> None:
    monkeypatch.setattr("lquant.data.providers.get_provider", lambda *a, **k: chain)


POOL_MIXED = [("600000.SH", D), ("510300.SH", D), ("159915.SZ", D), ("161725.SZ", D)]


# ---------- 基金段路由 ----------


def test_fund_symbols_go_to_etf_capable_source(fake_settings, no_lake, monkeypatch):
    """基金段不得被塞给「只认股票」的链头源（tushare 主源的真实缺陷）。"""
    from lquant.data.ingest.daily import backfill_pool

    head, alt = _StockOnlyProvider(), _FundCapableProvider()
    _patch_chain(monkeypatch, _Chain([head, alt]))

    res = backfill_pool(POOL_MIXED, START, provider=None, cp_name="route1")

    # 股票走链头（providers_order 首位仍生效）
    assert head.daily_calls == [["600000.SH"]]
    # 基金段改走 etf_daily 通道，不再进 daily_bars
    assert [set(c) for c in head.etf_calls] == [{"510300.SH", "159915.SZ", "161725.SZ"}]
    assert res["failed"] == []
    assert res["done"] == 4


def test_fund_falls_back_to_daily_bars_without_etf_method(fake_settings, no_lake, monkeypatch):
    """源声明 etf_daily 但没有 etf_daily_bars（baostock）→ 回落 daily_bars。"""
    from lquant.data.ingest.daily import backfill_pool

    only = _FundCapableProvider()
    _patch_chain(monkeypatch, _Chain([only]))

    backfill_pool([("510300.SH", D)], START, provider=None, cp_name="route2")
    assert only.daily_calls == [["510300.SH"]]


def test_injected_provider_is_not_class_routed(fake_settings, no_lake):
    """显式注入 provider 时保持旧语义：整组一把过，不做类别分流。"""
    from lquant.data.ingest.daily import backfill_pool

    p = _FundCapableProvider()
    backfill_pool(POOL_MIXED, START, provider=p, cp_name="route3")
    assert [set(c) for c in p.daily_calls] == [set(s for s, _ in POOL_MIXED)]


def test_empty_response_reason_names_the_source(fake_settings, no_lake, monkeypatch):
    """零行失败的 reason 必须带实际源名 —— 否则无从判断是「源不具备该类别」。"""
    from lquant.data.ingest.daily import backfill_pool

    # 造一个连股票都不返回的源，且声明 etf_daily（确保基金段也走它）
    class _Empty(_StockOnlyProvider):
        def daily_bars(self, symbols, start, end):
            return pl.DataFrame()

        def etf_daily_bars(self, symbols, start, end):
            return pl.DataFrame()

    _patch_chain(monkeypatch, _Chain([_Empty()]))
    res = backfill_pool([("600000.SH", D)], START, provider=None, cp_name="route4")
    assert res["failed"] == [
        {"symbol": "600000.SH", "reason": "empty_response: 源(tushare)零行返回"}
    ]


def test_resolve_ingest_source_prefers_capability_order(fake_settings, monkeypatch):
    """按能力选源：基金段取链中第一个声明 etf_daily 的源，股票段取 daily 首个。"""
    from lquant.data.ingest.daily import resolve_ingest_source

    class _OnlyDaily(_StockOnlyProvider):
        capability = frozenset({Capability.DAILY})

    daily_first, fund_only = _OnlyDaily(), _FundCapableProvider()
    fund_only.name = fund_only.source = "fundonly"
    _patch_chain(monkeypatch, _Chain([daily_first, fund_only]))

    p_stock, m_stock = resolve_ingest_source(fund=False)
    p_fund, m_fund = resolve_ingest_source(fund=True)
    assert (p_stock.name, m_stock) == ("tushare", "daily_bars")
    assert (p_fund.name, m_fund) == ("fundonly", "daily_bars")


def test_unparseable_symbol_is_not_fund(fake_settings):
    """解析不了的代码按非基金处理 —— 分类失败不能把整批带崩。"""
    from lquant.data.ingest.daily import _by_class, _is_fund

    assert _is_fund("???") is False
    assert _by_class(["600000.SH", "???"]) == [("other", ["600000.SH", "???"])]


def test_resolve_falls_back_to_chain_head_without_capability(
        fake_settings, monkeypatch):
    """链里没有任何源声明该能力时回落链头（保持旧行为，不抛错）。"""
    from lquant.data.ingest.daily import resolve_ingest_source

    class _NoCaps(_FakeProv):
        name = "nocaps"
        source = "nocaps"

    _patch_chain(monkeypatch, _Chain([_NoCaps()]))
    p, method = resolve_ingest_source(fund=True)
    assert (p.name, method) == ("nocaps", "daily_bars")


# ---------- 回填池口径 ----------


def _seed_security() -> None:
    from lquant.core.db import writer

    with writer() as con:
        con.execute(
            "CREATE TABLE security (symbol VARCHAR PRIMARY KEY, sec_type VARCHAR, "
            "list_date DATE, delist_date DATE)"
        )
        con.executemany(
            "INSERT INTO security VALUES (?, ?, ?, ?)",
            [
                ("600000.SH", "stock", date(1999, 11, 10), None),
                ("000001.SZ", "stock", date(1991, 4, 3), None),
                ("300750.SZ", "stock", date(2018, 6, 11), None),
                ("000003.SZ", "stock", date(1991, 1, 1), date(2002, 6, 3)),  # 退市
                ("510300.SH", "etf", date(2012, 5, 28), None),
                ("161725.SZ", "lof", date(2015, 5, 5), None),
                ("000001.SH", "index", None, None),   # 指数不得入日线回填池
                ("399001.SZ", "index", None, None),
            ],
        )


def test_pool_excludes_index_for_daily_update(fake_settings):
    """增量池 = 在市股票 + 全部 ETF/LOF，**不含指数**。"""
    from lquant.core.db import reader
    from lquant.data.ingest.tasks import _pool_from_con

    _seed_security()
    with reader() as con:
        pool = _pool_from_con(con, "daily_update", date(2024, 1, 1), D)

    stock_syms = {s for s, _ in pool["stocks"]}
    etf_syms = {s for s, _ in pool["etf"]}
    assert stock_syms == {"600000.SH", "000001.SZ", "300750.SZ"}
    assert etf_syms == {"510300.SH", "161725.SZ"}
    assert not (stock_syms | etf_syms) & {"000001.SH", "399001.SZ"}


def test_pool_full_backfill_keeps_delisted_excludes_index(fake_settings):
    """全量池含退市股（防幸存者偏差），且同样不含指数，退市股 end 被截断。"""
    from lquant.core.db import reader
    from lquant.data.ingest.tasks import _pool_from_con

    _seed_security()
    with reader() as con:
        pool = _pool_from_con(con, "full_backfill", date(2024, 1, 1), D)

    ends = dict(pool["stocks"])
    assert "000003.SZ" in ends                       # 退市股在池内
    assert ends["000003.SZ"] == date(2002, 6, 3)     # end 被 delist_date 截断
    assert "000001.SH" not in ends and "399001.SZ" not in ends
    assert {s for s, _ in pool["etf"]} == {"510300.SH","161725.SZ"}
