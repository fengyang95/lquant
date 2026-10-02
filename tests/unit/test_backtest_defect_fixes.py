"""回测缺陷锁定测试：docs/BACKTEST_BASELINE_E2E.md 缺口表 G11/G15–G20
以及本轮双路审查新发现的引擎缺陷，每项一条命名用例。

这些缺陷的共同特征是**静默算错**：不抛异常、指标齐全，但数字是错的。
因此每条修复都必须有独立用例锁住，否则可以再悄悄回归。
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from lquant.backtest.account import Position
from lquant.backtest.broker import Broker
from lquant.backtest.engine import Engine, EngineConfig, build_rules
from lquant.backtest.events import Bar, Order, Side
from lquant.backtest.jqapi import JQRunner
from lquant.backtest.rules.loader import default_slippage, load_ruleset
from lquant.backtest.slippage import PctSlippage
from lquant.backtest.strategy.base import Strategy
from lquant.core.types import parse_symbol

D0 = date(2026, 1, 5)


def _dates(n: int, start: date = D0) -> list[date]:
    return [date.fromordinal(start.toordinal() + i) for i in range(n)]


def _rows(days, symbol="600000.SH", px=10.0, pre=None, adj=1.0, open_px=None):
    return [dict(trade_date=d, symbol=symbol, open=open_px if open_px is not None else px,
                 high=px, low=px, close=px,
                 pre_close=pre if pre is not None else px,
                 volume=1e9, amount=px * 1e9, adj_factor=adj) for d in days]


class _Target(Strategy):
    """按 {日期: [(symbol, weight)]} 出目标权重。"""

    params = {}

    def __init__(self, plan):
        self.plan = plan

    def on_bar(self, ctx, bars):
        return self.plan.get(ctx.trade_date, [])


# ---------------------------------------------------------------- 除权日建仓欠配


def test_ex_dividend_entry_not_undersized():
    """除权日**新建仓**的挂单必须按因子比缩放，否则只建到 1/ratio 仓位。

    历史 bug：挂单缩放嵌在「遍历持仓」循环里，除权日还没持仓 → 永不执行。
    ratio=1.3 时 100% 目标只成交 76.8%。
    """
    days = _dates(3)
    rows = []
    for i, d in enumerate(days):
        px, adj = (10.0, 1.0) if i == 0 else (10.0 / 1.3, 1.3)
        rows += _rows([d], px=px, adj=adj)
    eng = Engine(_Target({days[0]: [("600000.SH", 1.0)]}),
                 config=EngineConfig(initial_cash=1_000_000, slippage="none"),
                 with_db_meta=False)
    res = eng.run(pl.DataFrame(rows))
    invested = sum(f.qty * f.price for f in res.trades)
    assert invested == pytest.approx(1_000_000, rel=0.01), (
        f"除权日建仓应接近满仓，实际只投 {invested/1e6:.1%}")


# ------------------------------------------------------- 涨跌停按 tick 取整（G18）


def test_limit_up_price_is_tick_rounded_and_rejects_fill():
    """前收 3.63 的 10% 涨停价 = 3.99（不是 3.993）。

    用 3.993 当阈值会把「开盘即涨停」放行 —— 把买不进去的收益算进回测。
    """
    rs = load_ruleset()
    sym = parse_symbol("600000.SH")
    r = rs.for_symbol("600000.SH", sym.sec_type, sym.board)
    assert r.limit_up(3.63) == 3.99
    assert r.limit_down(3.63) == 3.27

    days = _dates(3)
    rows = _rows(days, px=3.99, pre=3.63)
    eng = Engine(_Target({days[0]: [("600000.SH", 0.5)]}),
                 config=EngineConfig(initial_cash=1_000_000, slippage="none"),
                 with_db_meta=False)
    res = eng.run(pl.DataFrame(rows))
    assert res.trades == [], "开盘价等于挂牌涨停价时买单必须被拒"
    assert any("涨停" in r[2] for r in res.rejected)


def test_limit_up_tick_rounding_in_jq_path():
    """JQ 路径共用同一套涨跌停口径（同一 broker）。"""
    rows = _rows(_dates(3), px=3.99, pre=3.63)
    code = '''
def initialize(context):
    set_order_cost(type="stock", open_tax=0, close_tax=0, open_commission=0,
                   close_commission=0, min_commission=0)
    set_slippage(FixedSlippage(0))
    run_daily(trade, time="open")
def trade(context):
    if context.portfolio.positions["600000.SH"].total_amount == 0:
        order_value("600000.SH", 300000)
'''
    res = JQRunner(code, initial_cash=1_000_000).run(pl.DataFrame(rows))
    assert res.error is None, res.error
    assert res.trades == []
    assert any("涨停" in r[2] for r in res.rejected)


# --------------------------------------------------- ST 分板涨跌停 / ETF 跟踪指数


def test_st_price_limit_is_per_board_not_always_5pct():
    """ST 只在主板收窄到 5%；创业板/科创板 ST 仍 20%，北交所 ST 仍 30%。"""
    rs = load_ruleset()
    expect = {"600000.SH": 0.05, "300750.SZ": 0.20,
              "688981.SH": 0.20, "832000.BJ": 0.30}
    for s, want in expect.items():
        sym = parse_symbol(s)
        r = rs.for_symbol(s, sym.sec_type, sym.board, is_st=True)
        assert r.limit_ratio() == pytest.approx(want), f"{s} ST 涨跌幅应为 {want}"


def test_etf_price_limit_resolved_by_code_not_always_10pct():
    """ETF 涨跌停跟随跟踪指数：科创/创业板 ETF 是 20%，不是一律 10%。"""
    rs = load_ruleset()
    # 588 段 = 科创板 ETF；159915 等 = 创业板 ETF（yaml by_code 显式登记）
    for s, want in [("588000.SH", 0.20), ("159915.SZ", 0.20),
                    ("510300.SH", 0.10), ("159919.SZ", 0.10)]:
        sym = parse_symbol(s)
        r = rs.for_symbol(s, sym.sec_type, sym.board)
        assert r.limit_ratio() == pytest.approx(want), f"{s} 涨跌幅应为 {want}"


def test_etf_t0_inferred_from_name_for_gold_and_qdii():
    """fund_type 不可得时按名称推断：黄金/QDII/债券/货币 ETF 都是 T+0。"""
    rs = load_ruleset()
    cases = [("518880.SH", "黄金ETF", 0), ("513100.SH", "纳斯达克100ETF", 0),
             ("511010.SH", "国债ETF", 0), ("511990.SH", "华宝添益货币ETF", 0),
             ("510300.SH", "沪深300ETF", 1)]
    for s, name, want in cases:
        sym = parse_symbol(s)
        r = rs.for_symbol(s, sym.sec_type, sym.board, name=name)
        assert r.sellable_after_days == want, f"{s}({name}) 应为 T+{want}"


def test_native_engine_injects_security_meta(monkeypatch):
    """原生 Engine 路径必须自动带上 is_st / 名称元数据（此前只有 JQ 路径有）。"""
    import lquant.backtest.engine as eng_mod

    monkeypatch.setattr(eng_mod, "load_security_meta",
                        lambda: {"600000.SH": {"is_st": True, "name": "ST测试"},
                                 "518880.SH": {"name": "黄金ETF"}})
    rules = build_rules(["600000.SH", "518880.SH"])
    assert rules["600000.SH"].is_st is True
    assert rules["600000.SH"].limit_ratio() == pytest.approx(0.05)
    assert rules["518880.SH"].sellable_after_days == 0   # 黄金 ETF T+0


def test_no_price_limit_flag_disables_limit_check():
    """no_price_limit（IPO 首日/复牌首日/ST 变更日）必须真正免掉涨跌停约束。"""
    rs = load_ruleset()
    sym = parse_symbol("600000.SH")
    r = rs.for_symbol("600000.SH", sym.sec_type, sym.board, no_price_limit=True)
    assert r.limit_up(10.0) is None and r.limit_down(10.0) is None

    # 撮合侧：开盘涨停价也能成交
    days = _dates(2)
    rows = _rows(days, px=11.0, pre=10.0)
    eng = Engine(_Target({days[0]: [("600000.SH", 0.5)]}),
                 config=EngineConfig(initial_cash=1_000_000, slippage="none"),
                 meta={"600000.SH": {"no_price_limit": True}}, with_db_meta=False)
    res = eng.run(pl.DataFrame(rows))
    assert len(res.trades) == 1, "no_price_limit 生效时应能按涨停价成交"


# ------------------------------------------------------------ yaml 滑点活配置


def test_yaml_slippage_config_is_live():
    """cn_a_share.yaml 的 default.slippage 必须是活配置（曾经是死键）。"""
    mode, params = default_slippage()
    assert mode == "pct" and params.get("rate") == pytest.approx(0.0005)

    eng = Engine(_Target({}), with_db_meta=False)
    assert isinstance(eng.slippage, PctSlippage)
    assert eng.slippage.rate == pytest.approx(0.0005)

    # 显式模型与规则表模型不同时，不得把 rate 塞给别的模型
    eng2 = Engine(_Target({}), config=EngineConfig(slippage="none"), with_db_meta=False)
    assert eng2.slippage.apply(10.0, Side.BUY) == 10.0


# ------------------------------------------------------------- 印花税历史区间


def test_stamp_duty_history_covers_both_sides_and_2023_cut():
    """2008-09-19 起才单边征收；此前双边都收。早期区间不得崩。"""
    rs = load_ruleset()
    sym = parse_symbol("600000.SH")
    r = rs.for_symbol("600000.SH", sym.sec_type, sym.board)
    both = date(2005, 1, 3)
    assert r.tax_rate(both, "buy") > 0 and r.tax_rate(both, "sell") > 0
    sell_only = date(2020, 1, 2)
    assert r.tax_rate(sell_only, "buy") == 0
    assert r.tax_rate(sell_only, "sell") == pytest.approx(0.001)
    assert r.tax_rate(date(2023, 8, 28), "sell") == pytest.approx(0.0005)


# ------------------------------------------------------------------ 退市核销


def test_delisted_position_is_written_off_not_frozen():
    """退市股必须按残值核销；否则按最后收盘价永久冻结，长回测 NAV 虚高。"""
    days = _dates(5)
    rows = []
    for i, d in enumerate(days):
        if i < 2:
            rows += _rows([d])                     # 600000 只在前两天有行情
        rows += _rows([d], symbol="000001.SZ", px=5.0)
    eng = Engine(_Target({days[0]: [("600000.SH", 0.5)]}),
                 config=EngineConfig(initial_cash=1_000_000, slippage="none",
                                     delist_recovery=0.0),
                 meta={"600000.SH": {"delist_date": days[2]}}, with_db_meta=False)
    res = eng.run(pl.DataFrame(rows))
    navs = [v for _, v in res.nav]
    assert navs[-1] < navs[1] - 400_000, "退市后持仓应被核销，净值不得冻结在最后收盘价"
    assert any("退市核销" in r[2] for r in res.rejected)
    assert not eng.account.positions["600000.SH"].qty


def test_delisting_recovery_rate_is_configurable():
    """残值率可配（默认 0=全额损失，保守口径）。

    买入信号在 day0、成交在 day1；退市日 day2 —— 持仓已存在才会走核销分支。
    """
    days = _dates(4)
    rows = []
    for i, d in enumerate(days):
        if i < 2:
            rows += _rows([d])
        rows += _rows([d], symbol="000001.SZ", px=5.0)
    eng = Engine(_Target({days[0]: [("600000.SH", 0.5)]}),
                 config=EngineConfig(initial_cash=1_000_000, slippage="none",
                                     delist_recovery=0.5),
                 meta={"600000.SH": {"delist_date": days[2]}}, with_db_meta=False)
    res = eng.run(pl.DataFrame(rows))
    assert any("残值率=0.5" in r[2] for r in res.rejected), res.rejected
    # 残值 0.5：持仓市值一半变现回来，NAV 只损失一半仓位市值
    navs = [v for _, v in res.nav]
    assert navs[-1] > navs[1] - 600_000 * 0.75


def test_delisted_symbol_cannot_be_bought_on_or_after_delist_date():
    """退市日起不可再交易：即使当日有行情，买单也必须被拒。"""
    days = _dates(3)
    rows = []
    for d in days:
        rows += _rows([d])                          # 600000 每天都有行情
        rows += _rows([d], symbol="000001.SZ", px=5.0)
    eng = Engine(_Target({days[0]: [("600000.SH", 0.5)]}),
                 config=EngineConfig(initial_cash=1_000_000, slippage="none"),
                 meta={"600000.SH": {"delist_date": days[1]}}, with_db_meta=False)
    res = eng.run(pl.DataFrame(rows))
    assert res.trades == [], "退市日不可成交"
    assert any(r[2] == "已退市" for r in res.rejected), res.rejected


# -------------------------------------------------------------------- 零股清仓


def test_full_liquidation_sells_odd_lot():
    """A 股零股必须能一次性全部卖出（10 送 9 后剩的零股不能永远卡住）。"""
    days = _dates(5)
    rows = []
    for i, d in enumerate(days):
        px = 10.0 if i < 2 else 10.0 / 1.9
        adj = 1.0 if i < 2 else 1.9
        rows += _rows([d], px=px, adj=adj)
    plan = {days[0]: [("600000.SH", 1.0)], days[3]: [("600000.SH", 0.0)]}
    eng = Engine(_Target(plan), config=EngineConfig(initial_cash=1_000_000,
                                                    slippage="none"),
                 with_db_meta=False)
    res = eng.run(pl.DataFrame(rows))
    leftover = {s: p.qty for s, p in eng.account.positions.items() if p.qty}
    assert leftover == {}, f"清仓后不应残留零股: {leftover}"
    sell = [f for f in res.trades if f.side == Side.SELL]
    assert sell and sell[-1].qty % 100 != 0, "清仓单应包含零股（非整手）"


# ------------------------------------------------------- T+N 按交易日而非自然日


def test_t_plus_n_uses_trading_days_not_calendar_days():
    """周五买入 T+2：自然日口径周一就「到期」，交易日口径要到周二。"""
    rs = load_ruleset()
    sym = parse_symbol("600000.SH")
    r = rs.for_symbol("600000.SH", sym.sec_type, sym.board, sellable_after_days=2)
    fri, mon, tue = date(2026, 1, 9), date(2026, 1, 12), date(2026, 1, 13)
    days = [date(2026, 1, 8), fri, mon, tue]
    idx = {d: i for i, d in enumerate(days)}
    pos = Position("600000.SH", qty=1000, avg_cost=10.0, lots=[(fri, 1000, 10.0)])

    assert pos.available_at(mon, r, idx) == 0          # 交易日：周一仍不可卖
    assert pos.available_at(mon, r) == 1000            # 自然日：错误地放行
    assert pos.available_at(tue, r, idx) == 1000       # 周二可卖


def test_etf_t0_sellable_same_day():
    """T+0 品种（黄金 ETF）当日买入当日可卖。"""
    rs = load_ruleset()
    sym = parse_symbol("518880.SH")
    r = rs.for_symbol("518880.SH", sym.sec_type, sym.board, name="黄金ETF")
    d = date(2026, 1, 5)
    pos = Position("518880.SH", qty=1000, avg_cost=3.0, lots=[(d, 1000, 3.0)])
    assert pos.available_at(d, r, {d: 0}) == 1000


# ------------------------------------------------------ 资金不足口径显式可配


def test_insufficient_cash_modes_are_explicit_and_documented():
    """reject（真实券商/backtrader）vs truncate（聚宽 order_value）显式可选。

    此前两条路径各写一套、且没有任何配置项 —— 同策略两路径不可比。
    """
    rs = load_ruleset()
    sym = parse_symbol("600000.SH")
    r = rs.for_symbol("600000.SH", sym.sec_type, sym.board)
    bar = Bar("600000.SH", D0, 10.0, 10.5, 9.5, 10.0, 10.0, 1e9, 1e10)

    b_reject = Broker({"600000.SH": r}, price_mode="next_open",
                      insufficient_cash="reject")
    o = Order("r1", "600000.SH", Side.BUY, 200_000)
    assert b_reject.match(o, bar, D0, cash=1000) is None
    assert o.reason == "资金不足"

    b_trunc = Broker({"600000.SH": r}, price_mode="next_open",
                     insufficient_cash="truncate")
    o2 = Order("t1", "600000.SH", Side.BUY, 200_000)
    fill = b_trunc.match(o2, bar, D0, cash=100_000)
    assert fill is not None and fill.qty == 9900, "truncate 应按可用资金截量成交"

    with pytest.raises(ValueError):
        Broker({"600000.SH": r}, insufficient_cash="whatever")


# ------------------------------------------------------------- JQ 路径公司行为


def test_jq_path_applies_corporate_actions():
    """JQ 路径此前完全没有公司行为处理：同一份 2:1 拆股数据净值凭空跳空。"""
    days = _dates(4)
    rows = []
    for i, d in enumerate(days):
        adj = 1.0 if i < 2 else 2.0
        px = 10.0 if i < 2 else 5.0
        rows += _rows([d], px=px, adj=adj)
    code = '''
def initialize(context):
    set_order_cost(type="stock", open_tax=0, close_tax=0, open_commission=0,
                   close_commission=0, min_commission=0)
    set_slippage(FixedSlippage(0))
    run_daily(trade, time="open")
def trade(context):
    if context.portfolio.positions["600000.SH"].total_amount == 0:
        order_value("600000.SH", 100000)
'''
    res = JQRunner(code, initial_cash=1_000_000).run(pl.DataFrame(rows))
    assert res.error is None, res.error
    navs = [v for _, v in res.nav]
    assert navs[-1] == pytest.approx(navs[1], rel=1e-6), (
        f"拆股不应改变净值（份额按因子比放大）: {navs}")


def test_jq_path_engine_path_agree_on_split_nav():
    """两条路径在同一份拆股数据上必须给出同量级净值（不可比是缺陷）。"""
    days = _dates(4)
    rows = []
    for i, d in enumerate(days):
        adj = 1.0 if i < 2 else 2.0
        px = 10.0 if i < 2 else 5.0
        rows += _rows([d], px=px, adj=adj)
    code = '''
def initialize(context):
    set_order_cost(type="stock", open_tax=0, close_tax=0, open_commission=0,
                   close_commission=0, min_commission=0)
    set_slippage(FixedSlippage(0))
    run_daily(trade, time="open")
def trade(context):
    if context.portfolio.positions["600000.SH"].total_amount == 0:
        order_target_percent("600000.SH", 0.1)
'''
    jq = JQRunner(code, initial_cash=1_000_000).run(pl.DataFrame(rows))
    assert jq.error is None, jq.error
    eng = Engine(_Target({days[0]: [("600000.SH", 0.1)]}),
                 config=EngineConfig(initial_cash=1_000_000, slippage="none"),
                 with_db_meta=False).run(pl.DataFrame(rows))
    assert jq.nav[-1][1] == pytest.approx(eng.nav[-1][1], rel=0.02), (
        f"JQ 路径 {jq.nav[-1][1]:.0f} vs Engine 路径 {eng.nav[-1][1]:.0f}")


# ------------------------------------------------------------------ G15 fq 复权


def test_history_fq_pre_adjusts_across_ex_dividend():
    """G15：跨除权日的 history 必须可复权，否则动量/均线系统性发散。"""
    days = _dates(3)
    rows = []
    for i, d in enumerate(days):
        adj = 1.0 if i == 0 else 2.0
        px = 10.0 if i == 0 else 5.0
        rows += _rows([d], px=px, adj=adj)
    code = '''
def initialize(context):
    run_daily(f, time="open")
def f(context):
    if context.current_dt.date().isoformat() != "2026-01-07":
        return
    pre = history(2, "1d", "close", ["600000.SH"], fq="pre")
    raw = history(2, "1d", "close", ["600000.SH"], fq=None)
    log.info("PRE=%s" % list(pre["600000.SH"]))
    log.info("RAW=%s" % list(raw["600000.SH"]))
'''
    res = JQRunner(code, initial_cash=1_000_000).run(pl.DataFrame(rows))
    assert res.error is None, res.error
    pre = next(x for x in res.logs if "PRE=" in x)
    raw = next(x for x in res.logs if "RAW=" in x)
    assert "[5.0, 5.0]" in pre, f"pre 复权后序列应连续: {pre}"
    assert "10.0" in raw, "fq=None 应返回原始价（跨除权日跳空）"


def test_fq_param_accepted_on_all_three_data_apis():
    """G15：get_price/history/attribute_history 都必须接受 fq，不能再 TypeError。"""
    rows = _rows(_dates(4))
    code = '''
def initialize(context):
    run_daily(f, time="open")
def f(context):
    for call in [lambda: history(2, "1d", "close", fq="pre"),
                 lambda: attribute_history("600000.SH", 2, "1d", ["close"], fq="post"),
                 lambda: get_price("600000.SH", count=2, fq="pre")]:
        call()
    log.info("ALL_FQ_OK")
'''
    res = JQRunner(code, initial_cash=1_000_000).run(pl.DataFrame(rows))
    assert res.error is None, res.error
    assert any("ALL_FQ_OK" in x for x in res.logs)


# ------------------------------------------------------- G11 attribute_history


def test_attribute_history_string_fields_not_split_into_chars():
    """G11：fields 传字符串必须当单字段，不能拆成 ['c','l','o','s','e']。"""
    rows = _rows(_dates(4))
    code = '''
def initialize(context):
    run_daily(f, time="open")
def f(context):
    df = attribute_history("600000.SH", 2, "1d", "close")
    log.info("COLS=%s" % list(df.columns))
'''
    res = JQRunner(code, initial_cash=1_000_000).run(pl.DataFrame(rows))
    assert res.error is None, res.error
    cols = next(x for x in res.logs if "COLS=" in x)
    assert "close" in cols and "'c'" not in cols, f"字符串字段被拆成字符: {cols}"


# ------------------------------------------------------------ G16 API 注入补齐


def test_order_target_percent_injected_and_targets_percent_of_portfolio():
    """G16：order_target_percent 必须可用（聚宽最高频下单 API）。"""
    rows = _rows(_dates(3))
    code = '''
def initialize(context):
    set_order_cost(type="stock", open_tax=0, close_tax=0, open_commission=0,
                   close_commission=0, min_commission=0)
    set_slippage(FixedSlippage(0))
    run_daily(trade, time="open")
def trade(context):
    if context.portfolio.positions["600000.SH"].total_amount == 0:
        order_target_percent("600000.SH", 0.5)
'''
    res = JQRunner(code, initial_cash=1_000_000).run(pl.DataFrame(rows))
    assert res.error is None, res.error
    assert len(res.trades) == 1
    assert res.trades[0].qty * res.trades[0].price == pytest.approx(500_000, rel=0.01)


def test_get_trade_days_uses_backtest_calendar():
    """G16：get_trade_days 必须可用，且用回测自身日历（与引擎推进一致）。"""
    days = _dates(5)
    rows = _rows(days)
    code = '''
def initialize(context):
    run_daily(f, time="open")
def f(context):
    if context.current_dt.date().isoformat() != "2026-01-09":
        return
    log.info("DAYS=%d" % len(get_trade_days(start_date="2026-01-06")))
    log.info("LAST=%s" % get_trade_days(count=1)[0])
'''
    res = JQRunner(code, initial_cash=1_000_000).run(pl.DataFrame(rows))
    assert res.error is None, res.error
    assert any("DAYS=4" in x for x in res.logs), res.logs
    assert any("LAST=2026-01-09" in x for x in res.logs)


# --------------------------------------------------- G17 调度：日频时刻 / 月末


def test_run_daily_with_clock_time_stays_daily():
    """G17：run_daily(time='14:50') 曾被静默改写成「每月第 4 个交易日」。"""
    days = _dates(50, start=date(2026, 1, 5))
    rows = []
    for d in days:
        if d.weekday() < 5:
            rows += _rows([d])
    code = '''
def initialize(context):
    run_daily(f, time="14:50")
def f(context):
    log.info("FIRED")
'''
    res = JQRunner(code, initial_cash=1_000_000).run(pl.DataFrame(rows))
    assert res.error is None, res.error
    n_fired = sum(1 for x in res.logs if "FIRED" in x)
    n_days = len({r["trade_date"] for r in rows})
    assert n_fired == n_days, f"日频任务应每个交易日触发：{n_fired}/{n_days}"


def test_run_daily_invalid_time_raises():
    """非法 time 必须明确报错，而不是被当成月频静默跑偏。"""
    rows = _rows(_dates(2))
    code = '''
def initialize(context):
    run_daily(f, time="25:99")
def f(context):
    pass
'''
    res = JQRunner(code, initial_cash=1_000_000).run(pl.DataFrame(rows))
    # 调度键解析在 run() 内，异常进 res.error（与其它钩子异常同口径）
    assert res.error is not None and "run_daily" in res.error


def test_run_monthly_negative_monthday_fires_on_last_trading_day():
    """G17：run_monthly(monthday=-1) 是月末倒数，曾永不触发。"""
    days = []
    d = date(2026, 1, 5)
    while len(days) < 70:
        if d.weekday() < 5:
            days.append(d)
        d = date.fromordinal(d.toordinal() + 1)
    rows = []
    for x in days:
        rows += _rows([x])
    code = '''
def initialize(context):
    run_monthly(f, monthday=-1)
def f(context):
    log.info("MONTH_END %s" % context.current_dt.date())
'''
    res = JQRunner(code, initial_cash=1_000_000).run(pl.DataFrame(rows))
    assert res.error is None, res.error
    fired = [x for x in res.logs if "MONTH_END" in x]
    months = {d.month for d in days}
    assert len(fired) >= len(months) - 1, f"月末任务应每月触发一次，实际 {fired}"


# ------------------------------------------------- G20a 字符串 date / G20b 限价显示


def test_get_fundamentals_string_date_does_not_crash():
    """G20a：聚宽用户习惯传 '2025-08-01' 字符串，此前 TypeError。"""
    rows = _rows(_dates(3))
    code = '''
from datetime import date
def initialize(context):
    run_daily(f, time="open")
def f(context):
    for d in ["2025-08-01", date(2025, 8, 1), None]:
        get_fundamentals(query(valuation.pe_ratio), date=d)
    log.info("DATES_OK")
'''
    res = JQRunner(code, initial_cash=1_000_000).run(pl.DataFrame(rows))
    assert res.error is None, res.error
    assert any("DATES_OK" in x for x in res.logs)


def test_get_current_data_st_limit_matches_matching_rule():
    """G20b：get_current_data 的 high_limit 必须与撮合同源（ST 是 5% 不是 10%）。"""
    rows = _rows(_dates(2), px=10.0, pre=10.0)
    code = '''
def initialize(context):
    run_daily(f, time="open")
def f(context):
    d = get_current_data()["600000.SH"]
    log.info("LIMITS %s %s" % (d.high_limit, d.low_limit))
'''
    res = JQRunner(code, initial_cash=1_000_000,
                   security_meta={"600000.SH": {"is_st": True}}).run(
        pl.DataFrame(rows))
    assert res.error is None, res.error
    assert any("LIMITS 10.5 9.5" in x for x in res.logs), res.logs


def test_fundamentals_filter_rejects_non_condition_with_clear_error():
    """`表.字段 == 值` 会退化成 bool → 必须给出可操作的报错，而非迷惑异常。"""
    from lquant.research.dialect.fundamentals import query, valuation

    with pytest.raises(TypeError, match="in_"):
        query(valuation.code).filter(valuation.code == "600000.SH")


def test_zero_volume_day_rejected_with_accurate_reason():
    """零成交(volume=0)不可成交，但必须与「停牌」区分归因。

    Bar.halted 把两者合并了；reason 混用会让「为什么这单没成交」永远查不清。
    """
    days = _dates(3)
    rows = []
    for i, d in enumerate(days):
        if i == 1:
            rows.append(dict(trade_date=d, symbol="600000.SH", open=10.0, high=10.0,
                             low=10.0, close=10.0, pre_close=10.0,
                             volume=0.0, amount=0.0, adj_factor=1.0))
        else:
            rows += _rows([d])
    eng = Engine(_Target({days[0]: [("600000.SH", 0.5)]}),
                 config=EngineConfig(initial_cash=1_000_000, slippage="none"),
                 with_db_meta=False)
    res = eng.run(pl.DataFrame(rows))
    reasons = [r[2] for r in res.rejected]
    assert "无成交量" in reasons, f"零成交日应记「无成交量」，实际 {reasons}"
    assert "停牌或无行情" not in reasons, f"零成交不得误记为停牌：{reasons}"


def test_suspended_day_keeps_suspended_reason():
    """真停牌仍记 suspended/停牌，不能被零成交改写。"""
    days = _dates(3)
    rows = []
    for i, d in enumerate(days):
        if i == 1:
            rows.append(dict(trade_date=d, symbol="600000.SH", open=10.0, high=10.0,
                             low=10.0, close=10.0, pre_close=10.0,
                             volume=0.0, amount=0.0, adj_factor=1.0,
                             is_suspended=True))
        else:
            rows += _rows([d])
    eng = Engine(_Target({days[0]: [("600000.SH", 0.5)]}),
                 config=EngineConfig(initial_cash=1_000_000, slippage="none"),
                 with_db_meta=False)
    res = eng.run(pl.DataFrame(rows))
    reasons = [r[2] for r in res.rejected]
    assert "suspended" in reasons, f"停牌应记 suspended，实际 {reasons}"
    assert "无成交量" not in reasons


# ------------------------------------------------------------------ 限价单语义


def _limit_broker(**kw):
    rs = load_ruleset()
    sym = parse_symbol("600000.SH")
    r = rs.for_symbol("600000.SH", sym.sec_type, sym.board)
    return Broker({"600000.SH": r}, **kw), r


def test_limit_order_not_filled_when_price_worse_than_limit():
    """Order.limit_price 此前从未被读取 —— 设了限价仍按市价成交。"""
    b, _ = _limit_broker(price_mode="next_open", slippage=None)
    bar = Bar("600000.SH", D0, 10.0, 10.5, 9.5, 10.0, 10.0, 1e9, 1e10)

    o = Order("l1", "600000.SH", Side.BUY, 100, limit_price=9.5)
    assert b.match(o, bar, D0) is None
    assert o.reason.startswith("限价未触及")

    o2 = Order("l2", "600000.SH", Side.SELL, 100, limit_price=10.5)
    assert b.match(o2, bar, D0) is None
    assert o2.reason.startswith("限价未触及")


def test_limit_order_fills_at_limit_or_better():
    """可成交的限价单：成交价绝不劣于限价（含滑点时封顶到限价）。"""
    b, _ = _limit_broker(price_mode="next_open")
    from lquant.backtest.slippage import PctSlippage
    b.slippage = PctSlippage(0.01)          # 买抬 1% → 10.1
    bar = Bar("600000.SH", D0, 10.0, 10.5, 9.5, 10.0, 10.0, 1e9, 1e10)

    # 限价 10.05：基准价 10.0 可通过，但含滑点 10.1 超出 → 成交价封顶到 10.05
    f = b.match(Order("l3", "600000.SH", Side.BUY, 100, limit_price=10.05), bar, D0)
    assert f is not None and f.price == pytest.approx(10.05)

    # 限价 10.5：成交价取市价（10.1），不人为抬到限价
    f2 = b.match(Order("l4", "600000.SH", Side.BUY, 100, limit_price=10.5), bar, D0)
    assert f2 is not None and f2.price == pytest.approx(10.1)


def test_jq_limit_order_style_is_available_and_honored():
    """聚宽 `order(s, n, style=LimitOrder(price))`：此前沙箱里没有这两个 style。"""
    rows = _rows(_dates(3), px=10.0)
    code_bad = '''
def initialize(context):
    set_order_cost(type="stock", open_tax=0, close_tax=0, open_commission=0,
                   close_commission=0, min_commission=0)
    set_slippage(FixedSlippage(0))
    run_daily(trade, time="open")
def trade(context):
    if context.portfolio.positions["600000.SH"].total_amount == 0:
        order("600000.SH", 100, style=LimitOrder(9.0))
'''
    bad = JQRunner(code_bad, initial_cash=1_000_000).run(pl.DataFrame(rows))
    assert bad.error is None, bad.error
    assert bad.trades == [], "限价 9.0 低于市价 10.0，买单不应成交"
    assert any("限价未触及" in r[2] for r in bad.rejected), bad.rejected

    code_ok = code_bad.replace("LimitOrder(9.0)", "LimitOrder(10.5)")
    ok = JQRunner(code_ok, initial_cash=1_000_000).run(pl.DataFrame(rows))
    assert ok.error is None, ok.error
    assert len(ok.trades) == 1 and ok.trades[0].price == pytest.approx(10.0)

    code_mkt = code_bad.replace("style=LimitOrder(9.0)", "style=MarketOrder()")
    mkt = JQRunner(code_mkt, initial_cash=1_000_000).run(pl.DataFrame(rows))
    assert mkt.error is None, mkt.error
    assert len(mkt.trades) == 1, "MarketOrder 应等同默认市价单"
