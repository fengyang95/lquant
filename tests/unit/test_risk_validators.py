"""事前风控校验器链（backtest/risk）：每条规则 + 闸门语义 + 引擎接线。

三条不变量是这个模块存在的理由，测试优先钉它们：
1. **默认配置必须是 no-op** —— 开了风控不该悄悄改变任何一次正常回测的收益；
2. **只能剔除，不能改单** —— 数量/价格永远不因风控变化；
3. **拿不到输入就报错** —— 开了行业约束却没有行业数据，必须炸而不是跳过。
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from lquant.backtest.engine import Engine, EngineConfig
from lquant.backtest.events import Order, Side
from lquant.backtest.risk import RISK_RULES, PreTradeGate, RiskContext, RiskViolation
from lquant.backtest.risk.validators import _ensure_self_named
from lquant.backtest.strategy.base import Strategy

_D = date(2026, 10, 8)


def _order(oid: str, sym: str, side: Side, qty: float) -> Order:
    return Order(order_id=oid, symbol=sym, side=side, qty=qty)


def _ctx(orders, *, nav=1_000_000.0, cash=1_000_000.0, prices=None, **kw) -> RiskContext:
    px = prices or {"600519.SH": 100.0, "000001.SZ": 50.0, "300750.SZ": 200.0}
    return RiskContext(trade_date=_D, nav=nav, cash=cash, prices=px, orders=orders, **kw)


# ---------- 注册表与闸门语义 ----------

def test_default_gate_is_structural_only():
    gate = PreTradeGate()
    assert gate.names == ["duplicate_side", "insufficient_cash", "valid_order"]
    assert all(RISK_RULES.meta(n)["per_order"] for n in gate.names)
    # 会改变收益的规则一律默认关
    for n in ("sector_exposure", "turnover_cap", "drawdown_breaker",
              "max_position_weight"):
        assert RISK_RULES.meta(n)["default_on"] is False


def test_healthy_batch_passes_untouched():
    """默认闸门对一批合法订单必须是零改动、零违规。"""
    orders = [_order("o1", "600519.SH", Side.BUY, 100),
              _order("o2", "000001.SZ", Side.SELL, 200)]
    res = PreTradeGate().apply(_ctx(orders))
    assert res.orders == orders
    assert res.violations == []


def test_unknown_rule_raises():
    with pytest.raises(ValueError, match="未知风控规则"):
        PreTradeGate(["nope"])


def test_order_scope_rejects_batch_only_rules():
    """逐单场景（模拟盘）显式点名整批规则 → 报错，不许静默少跑。"""
    for name in ("turnover_cap", "sector_exposure", "max_position_weight"):
        with pytest.raises(ValueError, match="需要整批上下文"):
            PreTradeGate([name], scope="order")
    assert PreTradeGate(scope="order").names == [
        "duplicate_side", "insufficient_cash", "valid_order"]


def test_unknown_scope_raises():
    with pytest.raises(ValueError, match="未知 scope"):
        PreTradeGate([], scope="nope")


def test_gate_params_reach_rules():
    """闸门自己的参数必须合进上下文 —— 只挂在 self.params 上规则读不到，
    表现是「开了规则却像没开」，属于最难查的静默失效。"""
    orders = [_order("o1", "600519.SH", Side.BUY, 100)]
    gate = PreTradeGate(["turnover_cap"], {"max_turnover": 0.0})
    res = gate.apply(_ctx(orders))
    assert [v.rule for v in res.violations] == ["turnover_cap"]
    # 规则级参数覆盖全局同名参数
    gate2 = PreTradeGate(["turnover_cap"],
                         {"max_turnover": 0.0, "turnover_cap": {"max_turnover": 99.0}})
    assert gate2.apply(_ctx(orders)).violations == []


def test_rule_must_sign_its_own_violations():
    with pytest.raises(ValueError, match="归属"):
        _ensure_self_named("a", [RiskViolation(rule="b", reason="x")])
    assert _ensure_self_named("a", [RiskViolation(rule="a", reason="x")])


def test_violation_row_shape():
    row = RiskViolation("valid_order", "无有效价格", "600519.SH").to_row(_D)
    assert row == {"trade_date": "2026-10-08", "rule": "valid_order",
                   "symbol": "600519.SH", "scope": "order", "reason": "无有效价格"}


# ---------- 默认开：结构正确性 ----------

def test_valid_order_drops_bad_qty_and_missing_price():
    """剔除精确到单：同一标的的合法单不会被连坐。"""
    orders = [_order("o1", "600519.SH", Side.BUY, 0),
              _order("o2", "600519.SH", Side.BUY, 100),
              _order("o3", "300750.SZ", Side.BUY, 10)]      # 不在 prices 里
    res = PreTradeGate(["valid_order"]).apply(
        _ctx(orders, prices={"600519.SH": 100.0}))
    assert [o.order_id for o in res.orders] == ["o2"]
    assert [v.order_id for v in res.violations] == ["o1", "o3"]


def test_duplicate_side_drops_only_the_buy():
    """同批多空并存：剔买单、留卖单（卖出是降风险方向）。"""
    orders = [_order("o1", "600519.SH", Side.BUY, 100),
              _order("o2", "600519.SH", Side.SELL, 100),
              _order("o3", "000001.SZ", Side.BUY, 10)]
    res = PreTradeGate(["duplicate_side"]).apply(_ctx(orders))
    assert [(o.order_id, o.side) for o in res.orders] == [
        ("o2", Side.SELL), ("o3", Side.BUY)]


def test_insufficient_cash_drops_the_overflowing_buys():
    orders = [_order("o1", "600519.SH", Side.BUY, 900),     # 90000
              _order("o2", "000001.SZ", Side.BUY, 400)]     # 20000 > 余下 10000
    res = PreTradeGate(["insufficient_cash"]).apply(_ctx(orders, cash=100_000.0))
    assert [o.order_id for o in res.orders] == ["o1"]
    assert "超可用资金" in res.violations[0].reason


def test_insufficient_cash_tolerance():
    """容差内放过（浮点/手续费尾差不该被当成风控问题）。"""
    orders = [_order("o1", "300750.SZ", Side.BUY, 500)]     # 100000
    assert PreTradeGate(["insufficient_cash"]).apply(
        _ctx(orders, cash=100_000.0)).violations == []
    # 超出容差才拦
    res = PreTradeGate(["insufficient_cash"], {"cash_tolerance_pct": 0.0}).apply(
        _ctx([_order("o1", "300750.SZ", Side.BUY, 500)], cash=99_999.0))
    assert [v.rule for v in res.violations] == ["insufficient_cash"]


# ---------- 默认关：组合级 ----------

def test_max_position_weight():
    orders = [_order("o1", "600519.SH", Side.BUY, 100)]
    gate = PreTradeGate(["max_position_weight"], {"max_weight": 0.05})
    res = gate.apply(_ctx(orders, targets={"600519.SH": 0.2}))
    assert res.orders == [] and "超上限" in res.violations[0].reason
    # 目标权重取上下文里的 targets，缺失时视为 0（不误拦）
    assert gate.apply(_ctx(orders)).violations == []


def test_sector_exposure_trims_the_overweight_sector():
    orders = [_order("o1", "600519.SH", Side.BUY, 10_000),   # 100 万 → 1.0
              _order("o2", "000001.SZ", Side.BUY, 5_000)]    # 25 万
    ctx = _ctx(orders, sector={"600519.SH": "801080", "000001.SZ": "801080"})
    # 上限 0.5：剔掉最大的那只就落到 0.25，不必把小单也砍掉
    res = PreTradeGate(["sector_exposure"],
                       {"max_sector_weight": 0.5}).apply(ctx)
    assert [o.order_id for o in res.orders] == ["o2"]
    assert res.violations[0].symbol == "600519.SH"
    # 上限 0.1：单剔一只仍超限 → 继续剔到落回上限内（不会"剔一只就收工"）
    res2 = PreTradeGate(["sector_exposure"],
                        {"max_sector_weight": 0.1}).apply(ctx)
    assert res2.orders == []
    assert len(res2.violations) == 2


def test_sector_exposure_fails_loud_without_sector_data():
    """开了行业约束却没有行业归属 = 检查了个寂寞 → 必须报错。"""
    orders = [_order("o1", "600519.SH", Side.BUY, 100)]
    gate = PreTradeGate(["sector_exposure"], {"max_sector_weight": 0.1})
    with pytest.raises(ValueError, match="缺少行业归属"):
        gate.apply(_ctx(orders, sector={}))


def test_sector_exposure_includes_existing_holdings():
    """建仓后权重 = 现有持仓 + 本轮买入；只看买入会漏掉已经超配的行业。"""
    orders = [_order("o1", "000001.SZ", Side.BUY, 100)]      # 5000 元，很小
    ctx = _ctx(orders, held_qty={"600519.SH": 9000},          # 已持 90 万
               sector={"600519.SH": "801080", "000001.SZ": "801080"})
    gate = PreTradeGate(["sector_exposure"], {"max_sector_weight": 0.1})
    res = gate.apply(ctx)
    assert res.orders == [] and "801080" in res.violations[0].reason


def test_turnover_cap_blocks_the_whole_batch_but_keeps_sells():
    orders = [_order("o1", "600519.SH", Side.BUY, 1000),     # 10 万
              _order("o2", "000001.SZ", Side.SELL, 1000)]    # 5 万
    gate = PreTradeGate(["turnover_cap"], {"max_turnover": 0.05})
    res = gate.apply(_ctx(orders, nav=1_000_000.0))
    assert [(o.order_id, o.side) for o in res.orders] == [("o2", Side.SELL)]
    assert res.violations[0].scope == "batch"


def test_drawdown_breaker_only_blocks_buys():
    orders = [_order("o1", "600519.SH", Side.BUY, 100),
              _order("o2", "000001.SZ", Side.SELL, 100)]
    gate = PreTradeGate(["drawdown_breaker"], {"max_drawdown": 0.1})
    res = gate.apply(_ctx(orders, nav=800_000.0, peak_nav=1_000_000.0))
    assert [(o.order_id, o.side) for o in res.orders] == [("o2", Side.SELL)]
    assert res.violations[0].scope == "batch"
    # 回撤未达阈值 / 没有峰值信息 → 不触发
    assert gate.apply(_ctx(orders, nav=950_000.0, peak_nav=1_000_000.0)).violations == []
    assert gate.apply(_ctx(orders)).violations == []


def test_context_drawdown_zero_without_peak():
    assert _ctx([]).drawdown == 0.0
    assert _ctx([], nav=110.0, peak_nav=100.0).drawdown == 0.0


# ---------- 引擎接线 ----------

class _AllIn(Strategy):
    """把全部权重压到第一只票。"""

    def on_bar(self, ctx, bars):
        return [(sorted(bars)[0], 1.0)]


def _panel(n_days: int = 20, symbols=("600519.SH", "000001.SZ")) -> pl.DataFrame:
    rng = np.random.default_rng(11)
    rows = []
    px = {s: 100.0 for s in symbols}
    d0 = date(2026, 1, 5)
    for i in range(n_days):
        d = d0 + timedelta(days=i)
        for s in symbols:
            pre = px[s]
            close = pre * (1 + rng.normal(0, 0.01))
            px[s] = close
            rows.append({"trade_date": d, "symbol": s, "open": pre, "high": close * 1.01,
                         "low": close * 0.99, "close": close, "pre_close": pre,
                         "volume": 1e6, "amount": 1e6 * close})
    return pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))


def test_engine_default_risk_rules_are_noop():
    """默认风控不许改变任何一次正常回测的结果（否则等于悄悄改收益）。"""
    df = _panel()
    base = Engine(_AllIn(), config=EngineConfig(initial_cash=1_000_000)).run(df)
    withrisk = Engine(_AllIn(), config=EngineConfig(
        initial_cash=1_000_000, risk_rules=())).run(df)
    assert [n for _, n in base.nav] == [n for _, n in withrisk.nav]
    assert base.metrics["n_risk_blocked"] == 0
    assert base.risk_events == []


def test_engine_records_risk_events_and_blocks_orders():
    df = _panel()
    res = Engine(_AllIn(), config=EngineConfig(
        initial_cash=1_000_000, risk_rules=("turnover_cap",),
        risk_params={"max_turnover": 0.0})).run(df)
    assert res.risk_events                                   # 留痕
    assert all(e[1] == "turnover_cap" for e in res.risk_events)
    assert res.trades == []                                  # 整批被拦，无成交
    assert res.metrics["n_risk_blocked"] == len(res.risk_events)
    assert res.metrics["risk_rules"] == ["turnover_cap"]


def test_engine_rejects_unknown_rule_at_construction():
    with pytest.raises(ValueError, match="未知风控规则"):
        Engine(_AllIn(), config=EngineConfig(risk_rules=("nope",)))


def test_engine_sector_rule_needs_sector_data(monkeypatch):
    """开了行业约束但库里没有行业分类 → 报错（不静默跳过）。"""
    monkeypatch.setattr("lquant.backtest.security_meta.load_sector_map", lambda *a, **k: {})
    eng = Engine(_AllIn(), config=EngineConfig(
        initial_cash=1_000_000, risk_rules=("sector_exposure",),
        risk_params={"max_sector_weight": 0.1}))
    with pytest.raises(ValueError, match="缺少行业归属"):
        eng.run(_panel())


# ---------- 自省端点 ----------

def test_risk_rules_endpoint_lists_and_filters():
    from fastapi.testclient import TestClient

    from lquant.server.main import create_app

    with TestClient(create_app()) as c:
        batch = c.get("/api/backtests/risk-rules").json()
        assert batch["scope"] == "batch"
        names = {r["name"] for r in batch["rules"]}
        assert {"valid_order", "sector_exposure"} <= names
        assert "insufficient_cash" in batch["defaults"]
        assert "sector_exposure" not in batch["defaults"]

        order = c.get("/api/backtests/risk-rules", params={"scope": "order"}).json()
        assert {r["name"] for r in order["rules"]} == {
            "duplicate_side", "insufficient_cash", "valid_order", "drawdown_breaker"}
        assert c.get("/api/backtests/risk-rules",
                     params={"scope": "nope"}).status_code == 422


def test_gate_result_dropped_and_describe():
    orders = [_order("o1", "600519.SH", Side.BUY, 0)]
    gate = PreTradeGate(["valid_order"])
    res = gate.apply(_ctx(orders))
    assert res.dropped == ["600519.SH"]
    assert gate.describe() == [RISK_RULES.meta("valid_order")]


def test_sector_rule_noop_without_buys_and_under_cap():
    """只有卖单 → 没有建仓可评估；行业在上限内 → 不动。"""
    gate = PreTradeGate(["sector_exposure"], {"max_sector_weight": 0.1})
    sells = [_order("o1", "600519.SH", Side.SELL, 100)]
    assert gate.apply(_ctx(sells, sector={"600519.SH": "801080"})).violations == []
    # 一个行业超限、另一个远低于上限：只动超的那个
    orders = [_order("o1", "600519.SH", Side.BUY, 10_000),   # 100 万 → 1.0
              _order("o2", "000001.SZ", Side.BUY, 100)]      # 5000 元 → 0.005
    ctx = _ctx(orders, sector={"600519.SH": "801080", "000001.SZ": "801780"})
    res = gate.apply(ctx)
    assert [o.order_id for o in res.orders] == ["o2"]
    assert [v.symbol for v in res.violations] == ["600519.SH"]


# ---------- 模拟盘接线 ----------

def test_paper_broker_applies_per_order_risk_chain():
    from lquant.paper.engine import PaperBroker, PaperConfig

    broker = PaperBroker(PaperConfig(initial_cash=1_000_000.0))
    assert broker.gate.names == ["duplicate_side", "insufficient_cash", "valid_order"]
    # valid_order 在模拟盘同样生效：价格为 0 → 风控拒单（带规则名与可读原因）
    o = broker.submit("600519.SH", "buy", 100, 0.0, name="贵州茅台")
    assert o.status == "rejected" and o.rule == "valid_order"
    assert o.reason.startswith("风控: ")


def test_paper_broker_rejects_batch_only_rules():
    """模拟盘逐单场景点名整批规则 → 构造时就报错，不许静默少跑。"""
    from lquant.paper.engine import PaperBroker, PaperConfig

    with pytest.raises(ValueError, match="需要整批上下文"):
        PaperBroker(PaperConfig(risk_rules=("sector_exposure",)))


# ---------- 行业归属加载（load_sector_map） ----------

class _FakeCon:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_a, **_k):
        return self

    def fetchall(self):
        return self._rows


class _FakeReader:
    def __init__(self, rows=None, boom=False):
        self._rows = rows or []
        self._boom = boom

    def __enter__(self):
        if self._boom:
            raise RuntimeError("库连不上")
        return _FakeCon(self._rows)

    def __exit__(self, *_a):
        return False


def _patch_reader(monkeypatch, reader):
    from lquant.data.store import catalog

    monkeypatch.setattr(catalog, "reader", lambda: reader)


def test_load_sector_map_takes_latest_effective(monkeypatch):
    from datetime import date, timedelta

    from lquant.backtest.security_meta import load_sector_map

    d0 = date.today()
    rows = [
        ("600519.SH", "sw1", "801080", "电子", d0 - timedelta(days=10)),
        ("600519.SH", "sw1", "801780", "银行", d0 - timedelta(days=1)),   # 更新
        ("000001.SZ", "sw1", "801780", "银行", None),                     # 无生效日
        ("300750.SZ", "cics", "C25", "电气", d0 - timedelta(days=1)),     # 别的标准
        ("600030.SH", "sw1", "", "空代码", d0),                           # code 空 → 用 name
        ("601398.SH", "sw1", "801780", "银行", d0 + timedelta(days=30)),  # 未来生效
    ]
    _patch_reader(monkeypatch, _FakeReader(rows))
    out = load_sector_map()
    assert out["600519.SH"] == "801780"          # 取生效日最新的那条
    assert out["000001.SZ"] == "801780"
    assert out["600030.SH"] == "空代码"
    assert "300750.SZ" not in out                # std 不匹配
    assert "601398.SH" not in out                # 未来生效的不用（防事后信息）


def test_load_sector_map_degrades_to_empty_on_error(monkeypatch):
    from lquant.backtest.security_meta import load_sector_map

    _patch_reader(monkeypatch, _FakeReader(boom=True))
    assert load_sector_map() == {}


def test_load_sector_map_skips_rows_without_code_and_name(monkeypatch):
    from lquant.backtest.security_meta import load_sector_map

    _patch_reader(monkeypatch, _FakeReader([("600519.SH", "sw1", "", "", None)]))
    assert load_sector_map() == {}
