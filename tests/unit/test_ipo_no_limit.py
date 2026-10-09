"""IPO 上市初期「无涨跌幅」窗口：纯规则 + 逐日通道 + 撮合端到端。

缺口背景：`no_price_limit` 此前只有消费者（rules/broker）没有生产者，
配置里写着「security 表暂无这三个日期维度」，于是该标记恒为 False ——
新股窗口等于没生效；而且它被烘进 per-instrument 的 InstrumentRules，
就算有数据也表达不了「只有前 5 个交易日」这种**逐日**事实。

本文件锁三件事：
1. 纯规则（`tradability.no_limit_window`）的板块口径与政策生效日；
2. `limit_up/limit_down` 的逐日覆盖语义（与 is_st 参数完全对齐）；
3. 引擎把上市日变成 bar.no_price_limit，撮合按当日值放行/拒单。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from lquant.backtest.broker import Broker
from lquant.backtest.engine import Engine, EngineConfig
from lquant.backtest.events import Bar, Order, Side
from lquant.backtest.rules.loader import load_ruleset
from lquant.backtest.strategy.base import Strategy
from lquant.core.types import parse_symbol
from lquant.data.quality.tradability import (
    BSE_NO_LIMIT_TRADING_DAYS,
    IPO_NO_LIMIT_TRADING_DAYS,
    is_no_limit_day,
    no_limit_window,
    no_limit_window_days,
)

# --------------------------------------------------------------- 纯规则：板块口径

def test_star_window_is_first_five_trading_days():
    """科创板：2019-07-22 起上市前 5 个交易日无涨跌幅（首日=第 1 日）。"""
    sym, listing = "688001.SH", date(2019, 7, 22)
    assert no_limit_window_days(sym, listing) == IPO_NO_LIMIT_TRADING_DAYS
    for rank, want in [(1, True), (5, True), (6, False)]:
        v = no_limit_window(sym, listing, listing + timedelta(days=rank - 1), day_rank=rank)
        assert v.no_limit is want, f"科创板第 {rank} 个交易日应为 {want}"
        assert v.estimated is False and v.resolved is True


def test_gem_window_is_first_five_trading_days():
    """创业板：2020-08-24 起前 5 个交易日。"""
    sym, listing = "300750.SZ", date(2020, 8, 24)
    assert no_limit_window_days(sym, listing) == 5
    assert is_no_limit_day(sym, listing, listing, day_rank=1) is True
    assert is_no_limit_day(sym, listing, listing + timedelta(days=4), day_rank=5) is True
    assert is_no_limit_day(sym, listing, listing + timedelta(days=5), day_rank=6) is False


def test_main_board_window_is_first_five_trading_days():
    """沪深主板：2023-02-17 全面注册制起前 5 个交易日。"""
    for sym in ("600000.SH", "601988.SH", "603000.SH", "605001.SH",
                "000001.SZ", "001979.SZ", "002594.SZ", "003816.SZ"):
        listing = date(2023, 2, 17)
        assert no_limit_window_days(sym, listing) == 5, sym
        assert is_no_limit_day(sym, listing, listing + timedelta(days=4),
                               day_rank=5) is True, sym
        assert is_no_limit_day(sym, listing, listing + timedelta(days=5),
                               day_rank=6) is False, sym


def test_bse_window_is_first_day_only():
    """北交所：开市即 30% 涨跌幅，只有上市首日免约束。"""
    sym, listing = "830799.BJ", date(2022, 1, 4)
    assert no_limit_window_days(sym, listing) == BSE_NO_LIMIT_TRADING_DAYS
    assert is_no_limit_day(sym, listing, listing, day_rank=1) is True
    assert is_no_limit_day(sym, listing, listing + timedelta(days=1), day_rank=2) is False


@pytest.mark.parametrize(
    ("sym", "onset"),
    [("688001.SH", date(2019, 7, 22)),
     ("300750.SZ", date(2020, 8, 24)),
     ("600000.SH", date(2023, 2, 17))],
)
def test_policy_onset_before_listing_only_first_day(sym, onset):
    """政策生效日**之前**上市的老股：只有首日窗口，不是前 5 日。"""
    old = onset - timedelta(days=365)
    assert no_limit_window_days(sym, old) == 1
    assert is_no_limit_day(sym, old, old, day_rank=1) is True
    assert is_no_limit_day(sym, old, old + timedelta(days=1), day_rank=2) is False
    # 同一天上市的新股（生效日当天起）才有 5 日窗口
    assert no_limit_window_days(sym, onset) == 5


def test_unknown_board_has_no_ipo_window():
    """ETF/指数等非股票不套用新股窗口（0 天）。

    000300.SH 是上证 300 指数，代码段像主板但 sec_type=index —— 只看代码段
    会把它误判成有 5 日窗口的次新主板股。
    """
    for sym in ("510300.SH", "000300.SH", "399001.SZ"):
        assert no_limit_window_days(sym, date(2020, 1, 1)) == 0, sym
        assert is_no_limit_day(sym, date(2020, 1, 1), date(2020, 1, 1),
                               day_rank=1) is False, sym


# ------------------------------------------------- 纯规则：缺失上市日 / 保守估算

def test_missing_listing_date_is_conservative_and_visible():
    """上市日缺失/非法：不猜 → False（照常施加约束）且调用方能看到「无法判定」。"""
    for bad in (None, "", "not-a-date", 12345):
        v = no_limit_window("600000.SH", bad, date(2023, 3, 1))
        assert v.no_limit is False, bad
        assert v.resolved is False, bad          # ← 调用方可区分「无法判定」与「不在窗口」
        assert v.window_days == 0


def test_calendar_fallback_is_conservative_over_marking():
    """拿不到交易日序列时按日历天保守估算：窗口只多不少（宁多标勿漏标）。"""
    sym, listing = "600000.SH", date(2023, 2, 17)
    # 15 日历天内 → 标为免限（可能高估）
    v_in = no_limit_window(sym, listing, listing + timedelta(days=15))
    assert v_in.no_limit is True and v_in.estimated is True
    # 超出边际 → 明确不免限
    v_out = no_limit_window(sym, listing, listing + timedelta(days=16))
    assert v_out.no_limit is False and v_out.estimated is True
    # 北交所边际只 3 天
    assert no_limit_window("830799.BJ", date(2022, 1, 4),
                           date(2022, 1, 7)).no_limit is True
    assert no_limit_window("830799.BJ", date(2022, 1, 4),
                           date(2022, 1, 8)).no_limit is False


def test_resume_and_st_change_day_interface_reserved():
    """复牌首日 / ST 变更日：接口先留好（数据层暂未提供这两类逐日标记）。"""
    assert is_no_limit_day("600000.SH", date(2023, 3, 1), date(2023, 6, 1),
                           resume_first_day=True) is True
    assert is_no_limit_day("600000.SH", date(2023, 3, 1), date(2023, 6, 1),
                           st_change_day=True) is True


# --------------------------------------------------- 逐日通道：limit_up/down 覆盖

def _rules(symbol="600000.SH", **kw):
    sym = parse_symbol(symbol)
    return load_ruleset().for_symbol(str(sym), sym.sec_type, sym.board, **kw)


def test_per_day_param_overrides_static_flag_both_ways():
    """no_price_limit 逐日参数与 is_st 完全对齐：None 用静态值，True/False 覆盖。"""
    # 静态 False + 当日 True → 免限
    r = _rules(no_price_limit=False)
    assert r.limit_up(10.0) == pytest.approx(11.0)          # 静态值：有涨停
    assert r.limit_up(10.0, no_price_limit=True) is None    # 当日真实状态覆盖
    assert r.limit_down(10.0, no_price_limit=True) is None

    # 静态 True + 当日 False → 恢复约束（反向覆盖同样必须生效）
    r2 = _rules(no_price_limit=True)
    assert r2.limit_up(10.0) is None
    assert r2.limit_up(10.0, no_price_limit=False) == pytest.approx(11.0)


def _bar(symbol, pre, open_px, day_no_limit):
    return Bar(symbol=symbol, trade_date=date(2023, 3, 1), open=open_px, high=open_px,
               low=open_px, close=open_px, pre_close=pre, volume=1e9,
               amount=open_px * 1e9, no_price_limit=day_no_limit)


def test_broker_fills_on_no_limit_day_and_rejects_next_day():
    """同一标的、同样开盘即涨停：无涨跌幅日可成交，次日（窗口外）拒单。

    这就是逐日通道的意义 —— 静态标记只能整段一致，两种结果不可能同时成立。
    """
    broker = Broker({"600000.SH": _rules(no_price_limit=False)}, slippage=None,
                    price_mode="next_open")

    # 当日免费：开盘 11.0 = 10% 涨停价，也应允许买入
    o = Order(order_id="o1", symbol="600000.SH", side=Side.BUY, qty=100)
    fill = broker.match(o, _bar("600000.SH", 10.0, 11.0, True), date(2023, 3, 1),
                        cash=1e6)
    assert fill is not None, "免涨跌停日必须允许按涨停价成交"

    # 次日（窗口外）：同一价格被拒，reason 明确
    o2 = Order(order_id="o2", symbol="600000.SH", side=Side.BUY, qty=100)
    assert broker.match(o2, _bar("600000.SH", 10.0, 11.0, False), date(2023, 3, 2),
                        cash=1e6) is None
    assert o2.reason == "涨停不可买"


def test_broker_bar_unknown_falls_back_to_static_rule():
    """bar.no_price_limit = None（数据层没给判定）→ 退回静态值，向后兼容。"""
    broker = Broker({"600000.SH": _rules(no_price_limit=True)}, slippage=None,
                    price_mode="next_open")
    o = Order(order_id="o1", symbol="600000.SH", side=Side.BUY, qty=100)
    assert broker.match(o, _bar("600000.SH", 10.0, 11.0, None),
                        date(2023, 3, 1), cash=1e6) is not None


# --------------------------------------------------------------- 引擎：逐日生产者

class _BuyOn(Strategy):
    """指定日期买入固定金额。"""

    params = {}

    def __init__(self, day, symbol, weight=0.5):
        self.day, self.symbol, self.weight = day, symbol, weight

    def on_bar(self, ctx, bars):
        return [(self.symbol, self.weight)] if ctx.trade_date == self.day else []


def _ipo_rows(days, symbol="301001.SZ", listing=None, pre=10.0, px=12.0):
    """上市首日 = days[0]；px=11.0 对 10.0 前收是 +10%（主板涨停），
    px=12.0 对创业板 20% 涨停。"""
    listing = listing or days[0]
    return [dict(trade_date=d, symbol=symbol, open=px, high=px, low=px, close=px,
                 pre_close=pre, volume=1e9, amount=px * 1e9, adj_factor=1.0,
                 listing_date=listing) for d in days]


def _days(n, start=date(2023, 3, 1)):
    return [start + timedelta(days=i) for i in range(n)]


def test_engine_marks_ipo_window_and_fills_inside_it():
    """上市首日窗口内「开盘即涨停」可成交（修复前被误判为涨停不可买）。"""
    days = _days(7)
    rows = _ipo_rows(days, symbol="301001.SZ")   # 创业板，20% 涨停；px=12.0
    eng = Engine(_BuyOn(days[0], "301001.SZ"),
                 config=EngineConfig(initial_cash=1_000_000, slippage="none",
                                     benchmark=None),
                 with_db_meta=False)
    res = eng.run(pl.DataFrame(rows))
    assert len(res.trades) == 1, f"窗口内应能成交，实际 {res.rejected}"


def test_engine_rejects_same_order_outside_ipo_window():
    """同一标的、同一价格，第 6 个交易日（窗口外）必须拒单。"""
    days = _days(7)
    rows = _ipo_rows(days, symbol="301001.SZ")
    # 第 5 个交易日（下标 4）出信号 → 第 6 个交易日（下标 5，rank=6）开盘成交
    eng = Engine(_BuyOn(days[4], "301001.SZ"),
                 config=EngineConfig(initial_cash=1_000_000, slippage="none",
                                     benchmark=None),
                 with_db_meta=False)
    res = eng.run(pl.DataFrame(rows))
    assert res.trades == []
    assert any("涨停" in r[2] for r in res.rejected), res.rejected


def test_engine_bse_only_first_day():
    """北交所只有首日：prepare 出的逐日标记必须在第 2 个交易日就翻成 False。

    （撮合是 T 日出信号、T+1 开盘成交，故引擎路径无法在「首日」成交；
    北交所首日窗口因此用 prepare 的逐日标记 + broker 直接验证。）

    px=13.0 对前收 10.0 恰为 +30%（涨停价）。
    """
    days = _days(3)
    bars = Engine.prepare(pl.DataFrame(_ipo_rows(days, symbol="830799.BJ", px=13.0)),
                          listing_dates={"830799.BJ": days[0]})
    assert bars[days[0]]["830799.BJ"].no_price_limit is True
    assert bars[days[1]]["830799.BJ"].no_price_limit is False

    broker = Broker({"830799.BJ": _rules("830799.BJ")}, slippage=None,
                    price_mode="next_open")
    # 首日：13.0 = +30% 涨停价，也应放行
    o1 = Order(order_id="b1", symbol="830799.BJ", side=Side.BUY, qty=100)
    first = _bar("830799.BJ", 10.0, 13.0, True)
    assert broker.match(o1, first, days[0], cash=1e6) is not None
    # 第 2 个交易日：同一价格按 30% 涨停拒单
    o2 = Order(order_id="b2", symbol="830799.BJ", side=Side.BUY, qty=100)
    second = _bar("830799.BJ", 10.0, 13.0, False)
    assert broker.match(o2, second, days[1], cash=1e6) is None
    assert o2.reason == "涨停不可买"


def test_engine_no_listing_date_keeps_old_behaviour():
    """没有上市日 → bar.no_price_limit 留 None → 退回静态值（不误放行）。"""
    days = _days(3)
    rows = _ipo_rows(days, symbol="600000.SH", px=11.0)
    for r in rows:
        r.pop("listing_date")
    eng = Engine(_BuyOn(days[0], "600000.SH"),
                 config=EngineConfig(initial_cash=1_000_000, slippage="none",
                                     benchmark=None),
                 with_db_meta=False)
    res = eng.run(pl.DataFrame(rows))
    assert res.trades == [], "无法判定上市日时必须保守按涨停拒单"


def test_engine_prepare_exposes_per_day_flag():
    """prepare 产出的 bar 必须带上逐日标记：窗口内 True、窗口外 False。"""
    days = _days(7)
    bars = Engine.prepare(pl.DataFrame(_ipo_rows(days, symbol="301001.SZ")),
                          listing_dates={"301001.SZ": days[0]})
    assert bars[days[0]]["301001.SZ"].no_price_limit is True     # 第 1 个交易日
    assert bars[days[4]]["301001.SZ"].no_price_limit is True     # 第 5 个交易日
    assert bars[days[5]]["301001.SZ"].no_price_limit is False    # 第 6 个交易日


def test_engine_prepare_honours_data_layer_column():
    """数据层若已算好 no_price_limit 列，优先消费该列而非按上市日现算。"""
    days = _days(3)
    rows = _ipo_rows(days, symbol="301001.SZ")
    for i, r in enumerate(rows):
        r["no_price_limit"] = i == 1
    bars = Engine.prepare(pl.DataFrame(rows), listing_dates={"301001.SZ": days[0]})
    assert bars[days[0]]["301001.SZ"].no_price_limit is False
    assert bars[days[1]]["301001.SZ"].no_price_limit is True
    assert bars[days[2]]["301001.SZ"].no_price_limit is False


def test_parse_listing_date_and_board_fallback() -> None:
    """上市日解析兼容 date/datetime/ISO；板块识别失败时按代码段兜底。"""
    from datetime import date, datetime

    from lquant.data.quality.tradability import (
        Board,
        _board_of,
        no_limit_window_days,
        parse_listing_date,
    )

    assert parse_listing_date(datetime(2023, 1, 1, 9, 30)) == date(2023, 1, 1)
    assert parse_listing_date(date(2023, 1, 1)) == date(2023, 1, 1)
    assert parse_listing_date("2023-01-01T09:30:00") == date(2023, 1, 1)
    assert parse_listing_date("不是日期") is None
    assert parse_listing_date(None) is None

    # 非法交易所后缀 → parse_symbol 失败 → 走代码段兜底
    assert _board_of("688001.XX") is Board.STAR
    assert _board_of("689009.XX") is Board.STAR
    assert _board_of("300001.XX") is Board.GEM
    assert _board_of("430001.XX") is Board.BSE
    assert _board_of("830001.XX") is Board.BSE
    assert _board_of("600000.XX") is Board.MAIN
    assert _board_of("000001.XX") is Board.MAIN
    assert _board_of("XXXXXX") is Board.UNKNOWN

    assert no_limit_window_days("600000.SH", None) == 0  # 无上市日 → 不适用
