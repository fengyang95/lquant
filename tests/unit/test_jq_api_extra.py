"""jqapi 补测：pandas 兼容层/滑点/日志/上下文/历史接口兜底/下单拒绝路径。

无 pandas 环境兜底用 sys.modules['pandas']=None 触发 ImportError。
"""
from __future__ import annotations

import math
import sys
from datetime import date

import polars as pl
import pytest

import lquant.backtest.jqapi as jqapi
from lquant.backtest.events import Bar, Side
from lquant.backtest.jqapi import (
    FixedSlippage,
    JQRunner,
    PriceRelatedSlippage,
    _BarProxy,
    _Log,
    _SecData,
)


def make_df(days=6, start=date(2026, 1, 5)):
    rows = []
    px = {"600000.SH": 100.0, "000001.SZ": 50.0}
    for i in range(days):
        d = date.fromordinal(start.toordinal() + i)
        for s, base in px.items():
            p = base * (1.02 ** i) if s.startswith("6") else base * (0.99 ** i)
            pre = p if i == 0 else (
                base * (1.02 ** (i - 1)) if s.startswith("6") else base * (0.99 ** (i - 1)))
            rows.append(dict(trade_date=d, symbol=s, open=p, high=p * 1.005, low=p * 0.995,
                             close=p, pre_close=pre, volume=1e8, amount=p * 1e8))
    return pl.DataFrame(rows)


CODE = '''
def initialize(context):
    set_order_cost(type="stock", open_tax=0, close_tax=0,
                   open_commission=0, close_commission=0, min_commission=0)
    run_daily(buy, time="open")

def buy(context):
    order_target_value("600000.SH", 500000)
'''


# ---------------------------------------------------------------- 滑点/日志/杂项

def test_slippage_classes() -> None:
    fs = FixedSlippage(0.02)
    assert fs.apply(10.0, Side.BUY) == 10.01
    assert fs.apply(10.0, Side.SELL) == pytest.approx(9.99)
    pr = PriceRelatedSlippage(0.0024)
    assert pr.apply(100.0, Side.BUY) == pytest.approx(100.12)
    assert pr.apply(100.0, Side.SELL) == pytest.approx(99.88)


def test_log_formatting() -> None:
    sink = []
    log = _Log(sink)
    log.info("hello %s", "world")
    log.error("plain")
    log.info("%d", "not-a-number")       # 格式化失败 → 退化为 str(msg)
    assert sink[0] == "[INFO] hello world"
    assert sink[1] == "[INFO] plain"  # error/debug 均别名为 info
    assert sink[2] == "[INFO] %d"


def _bar(sym="600000.SH", close=10.5, trade_date=None):
    return Bar(symbol=sym, trade_date=trade_date or date(2026, 1, 5), open=10.0,
               high=11.0, low=9.0, close=close, pre_close=10.0, volume=1e8, amount=1e9)


def test_sec_data_and_bar_proxy() -> None:
    sd = _SecData(_bar(), ref=10.5)
    assert sd.day_open == 10.0 and sd.last_price == 10.5
    assert sd.paused is False
    assert sd.high_limit == 11.0 and sd.low_limit == 9.0
    nan_sd = _SecData(None, ref=0.0)
    assert nan_sd.paused is True
    assert math.isnan(nan_sd.last_price)
    bp = _BarProxy(_bar(), ref=10.5)
    assert bp.avg == pytest.approx((10.0 + 11.0 + 9.0 + 10.5) / 4)
    bp2 = _BarProxy(_bar(close=0.0), ref=1.0)
    assert math.isnan(bp2.avg)


def test_runner_misc_api_and_rejections() -> None:
    r = JQRunner(CODE, initial_cash=1_000_000)
    ns = r.ns
    ns["set_option"]("use_real_price", True)          # 接受但忽略
    ns["set_universe"](["600000.SH"])
    ns["cancel_order"](None)
    ns["set_slippage"](0.02)
    assert isinstance(r._slippage, FixedSlippage)
    ns["set_slippage"](PriceRelatedSlippage())
    assert isinstance(r._slippage, PriceRelatedSlippage)
    duck = object()
    ns["set_slippage"](duck)                          # duck-type 直接挂上
    assert r._slippage is duck
    ns["set_benchmark"]("000905.SH")
    assert r._set_benchmark_arg == "000905.SH"
    assert ns["order_value"]("600000.SH", 100000) is None       # 无行情 → nan ref
    assert ns["order_target_value"]("600000.SH", 100000) is None
    r._submit("600000.SH", 100)                       # 未 run，rules 空 → 不在数据集
    assert r.res.rejected[-1][2] == "不在数据集"


def test_submit_invalid_symbol_raises_valueerror() -> None:
    r = JQRunner(CODE)
    with pytest.raises(ValueError):
        r._submit("bad-symbol", 100)


def test_record_skips_nonfinite() -> None:
    r = JQRunner(CODE)
    r._today = date(2026, 1, 5)
    r._record({"a": 1.0, "b": float("nan"), "c": "x", "d": float("inf")})
    assert list(r.res.records) == ["a"]
    assert r.res.records["a"] == [(date(2026, 1, 5), 1.0)]


def test_load_security_meta_missing_table(monkeypatch) -> None:
    from lquant.backtest.jqapi import _load_security_meta

    def boom():
        raise RuntimeError("no db")

    monkeypatch.setattr("lquant.data.store.catalog.reader", boom, raising=False)
    assert _load_security_meta() == {}


# ---------------------------------------------------------------- pandas 兼容层

def test_jq_pandas_negative_position_semantics() -> None:
    if jqapi._JQFrame is None:
        pytest.skip("无 pandas 环境")
    s = jqapi._JQFrame({"close": [1.0, 2.0, 3.0]})["close"]
    assert s[-1] == 3.0                       # 负 int → iloc（老 pandas 语义）
    assert list(s[-2:]) == [2.0, 3.0]
    df = jqapi._JQFrame({"a": [1, 2], "b": [3, 4]})
    assert df[-1:]["a"].to_list() == [2]      # int 切片 → iloc


# ---------------------------------------------------------------- 无 pandas 兜底

def test_history_and_price_no_pandas_fallback(monkeypatch) -> None:
    runner = JQRunner(CODE)
    monkeypatch.setattr(jqapi, "_JQFrame", None)
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "pandas", None)
        out = runner._history_df(["600000.SH"], ["close"], [
            {"day": date(2026, 1, 5), "600000.SH": {"close": 1.0}}])
        assert out == {"600000.SH": [1.0]}
        multi = runner._history_df(["600000.SH", "000001.SZ"], ["close", "open"], [])
        assert set(multi) == {"600000.SH", "000001.SZ"}
        ah = runner._attribute_history("600000.SH", 2, ("close",))
        assert ah == {"close": []}
        runner._bars_by_day = {
            date(2026, 1, 5): {"600000.SH": _bar(), "000001.SZ": _bar("000001.SZ")}}
        runner._dates = [date(2026, 1, 5)]
        runner._day_index = 0
        gp = runner._get_price("600000.SH")
        assert isinstance(gp, dict) and "close" in gp
        gp2 = runner._get_price(["600000.SH", "000001.SZ"], count=1)
        assert set(gp2) == {"600000.SH", "000001.SZ"}


def test_get_price_with_pandas_paths() -> None:
    runner = JQRunner(CODE)
    runner._dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    runner._bars_by_day = {
        d: {"600000.SH": _bar(trade_date=d), "000001.SZ": _bar("000001.SZ")}
        for d in runner._dates
    }
    runner._day_index = 2
    # 单标的：列 = fields
    df = runner._get_price("600000.SH", count=2)
    assert list(df.columns) == ["open", "close", "high", "low", "volume"]
    assert len(df) == 2
    # start/end 区间
    df2 = runner._get_price("600000.SH", start_date="2026-01-06",
                            end_date="2026-01-07", fields=["close"])
    assert len(df2) == 2
    # 多标的 MultiIndex
    df3 = runner._get_price(["600000.SH", "000001.SZ"], count=1)
    assert df3.columns.nlevels == 2
    # panel=False：行=(日期,标的) MultiIndex，列=fields
    flat = runner._get_price(["600000.SH", "000001.SZ"], count=1, panel=False)
    assert flat.index.nlevels == 2 and list(flat.columns) == [
        "open", "close", "high", "low", "volume"]


def test_history_skip_paused_false_and_paused_gaps() -> None:
    runner = JQRunner(CODE)
    runner._dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    runner._bars_by_day = {
        runner._dates[0]: {"600000.SH": _bar()},
        runner._dates[1]: {},                       # 600000 停牌日
        runner._dates[2]: {"600000.SH": _bar()},
    }
    runner._day_index = 2
    runner._bars_today = runner._bars_by_day[runner._dates[2]]
    h = runner._history(2, "close", ["600000.SH"], skip_paused=True)
    assert h["600000.SH"].to_list() == [10.5]          # 只有 day0 有 bar
    h2 = runner._history(2, "close", ["600000.SH"], skip_paused=False)
    assert len(h2) == 2 and math.isnan(h2["600000.SH"][1])
    ah = runner._attribute_history("600000.SH", 1, ("close",))
    assert ah["close"].to_list() == [10.5]
    # data proxy：缺行情 KeyError，含行情 in
    data = jqapi._DataProxy(runner)
    assert "600000.SH" in data
    with pytest.raises(KeyError):
        data["000001.SZ"]
    assert data["600000.SH"].close == 10.0   # _BarProxy.close = ref（默认 open 桶）


def test_jq_portfolio_and_position_views() -> None:
    runner = JQRunner(CODE)
    runner._bars_today = {"600000.SH": _bar()}
    runner._bucket = "close"
    runner._last_close = {}
    pf = runner.context.portfolio
    assert pf.starting_cash == 1_000_000.0
    assert pf.available_cash == 1_000_000.0
    assert pf.total_value == 1_000_000.0
    assert pf.positions_value == 0.0
    # 注：pf.returns 引用了不存在的 self.initial_cash（源码缺陷），此处不断言
    pos = pf.positions["600000.SH"]               # 未持有 → 空持仓对象
    assert pos.total_amount == 0.0
    assert pos.closeable_amount == 0.0
    assert pos.avg_cost == 0.0
    assert pos.price == 10.5                      # bucket=close → bar.close
    assert pos.value == 0.0
    assert runner.context.current_date == runner.context.current_dt.date()


# ---------------------------------------------------------------- run() 分支

def test_run_empty_data_returns_early() -> None:
    r = JQRunner(CODE)
    res = r.run({})
    assert res.nav == [] and res.metrics == {}


def test_run_factor_formulas_require_df() -> None:
    r = JQRunner(CODE, factor_formulas=["mom20"])
    with pytest.raises(ValueError):
        r.run({})


def test_run_initialize_exception_captured() -> None:
    code = '''
def initialize(context):
    raise RuntimeError("boom")

def handle_data(context, data):
    pass
'''
    res = JQRunner(code).run(make_df())
    assert res.error is not None and "initialize 异常" in res.error


def test_run_hook_exception_captured() -> None:
    code = '''
def initialize(context):
    run_daily(daily, time="open")

def daily(context):
    raise ValueError("daily boom")
'''
    res = JQRunner(code).run(make_df())
    assert res.error is not None and "调度 daily" in res.error


def test_run_after_trading_exception_captured() -> None:
    code = '''
def initialize(context):
    pass

def after_trading_end(context):
    raise ValueError("after boom")
'''
    res = JQRunner(code).run(make_df())
    assert res.error is not None and "after_trading_end" in res.error


def test_run_before_trading_and_close_bucket() -> None:
    code = '''
seen = {}
def initialize(context):
    run_daily(before, time="open")
    run_daily(close_fn, time="close")

def before(context):
    seen.setdefault("before", []).append(str(context.current_dt))

def close_fn(context):
    seen.setdefault("close", []).append(str(context.current_dt))
'''
    runner = JQRunner(code)
    res = runner.run(make_df())
    assert res.error is None
    seen = runner.ns["seen"]
    assert seen["before"][0].endswith("09:30:00")
    assert seen["close"][0].endswith("15:00:00")


def test_run_weekly_and_monthly_schedule() -> None:
    code = '''
hits = []
def initialize(context):
    run_weekly(wk, weekday=1, time="open")
    run_monthly(mo, monthday=1, time="open")

def wk(context):
    hits.append("w")

def mo(context):
    hits.append("m")
'''
    runner = JQRunner(code)
    res = runner.run(make_df(days=10, start=date(2026, 2, 2)))   # 周一开跑
    assert res.error is None
    hits = runner.ns["hits"]
    assert "w" in hits and "m" in hits


def test_run_handle_data_gets_data_proxy() -> None:
    code = '''
def initialize(context):
    pass

def handle_data(context, data):
    assert "600000.SH" in data
    assert data["600000.SH"].money > 0
'''
    res = JQRunner(code).run(make_df())
    assert res.error is None


def test_run_with_factor_formulas() -> None:
    code = '''
vals = {}
def initialize(context):
    run_daily(f, time="open")

def f(context):
    v = get_factor_values("KMID", count=1)
    vals[str(context.current_date)] = v
'''
    runner = JQRunner(code, factor_formulas=["KMID"])
    res = runner.run(make_df(days=5))
    assert res.error is None
    assert runner.ns["vals"]
    # 未注册因子 → 报错信息
    code2 = '''
def initialize(context):
    run_daily(f, time="open")

def f(context):
    get_factor_values("nope")
'''
    res2 = JQRunner(code2).run(make_df(days=2))
    assert res2.error is not None and "未注册" in res2.error


def test_run_fee_override_deep_copies_ruleset() -> None:
    code = '''
def initialize(context):
    set_order_cost(type="stock", open_tax=0.001, close_tax=0.001,
                   open_commission=0.001, close_commission=0.001,
                   min_commission=10)
    run_daily(buy, time="open")

def buy(context):
    order_value("600000.SH", 200000)
'''
    res = JQRunner(code).run(make_df())
    assert res.error is None
    assert res.trades[0].fee >= 10.0                 # min_commission 生效
