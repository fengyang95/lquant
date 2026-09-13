"""模拟盘运行编排：账户生命周期 + 盘中 tick + 日终 close。

跨进程状态恢复链路（每次 CLI/API 调用都是新进程）：
    load_broker(name) → 推进行情/委托 → save_broker(name, broker)

tick 的三步职责（对应「实时盯市」设计）：
1. 拉实时快照（universe ∪ 持仓 ∪ 挂单标的）
2. 策略看行情产生委托 → 虚拟撮合（涨跌停/资金/T+N 校验与回测同源）
3. 用最新价盯市持仓 → 落 intraday 净值快照

停牌股整只跳过：不撮合、不改盯市价 —— 挂单保持 pending 等复牌，
净值沿用最近可得价，与「last known price 盯市」的业界惯例一致。
"""
from __future__ import annotations

import importlib

from lquant.core.types import today_cn
from lquant.paper import store
from lquant.paper.engine import PaperEngine


class _Manual:
    """内置空策略：不开自动信号，人工经 `lq paper order` 下单。

    模拟盘第一阶段的正确姿势 —— 先让行情/撮合/盯市/对账链路跑稳，
    再挂自动策略。自动策略必须与回测同一份信号源，对拍才有意义。
    """

    def signals(self, broker, quote: dict) -> list[dict]:
        return []


def resolve_strategy(ref: str | None):
    """策略引用："manual" 或 "pkg.module:Attr"（无参可实例化）。"""
    ref = (ref or "manual").strip()
    if ref == "manual":
        return _Manual()
    mod_name, _, attr = ref.partition(":")
    if not mod_name or not attr:
        raise ValueError(f"策略引用格式须为 module:Attr，收到: {ref}")
    cls = getattr(importlib.import_module(mod_name), attr)
    inst = cls()
    if not callable(getattr(inst, "signals", None)):
        raise TypeError(f"{ref} 缺少 signals(broker, quote) 方法")
    return inst


def create_account(name: str, initial_cash: float, strategy: str = "manual",
                   universe: list[str] | None = None) -> dict:
    s = resolve_strategy(strategy)   # 引用合法性前置校验
    del s
    return store.create_account(name, initial_cash, strategy, universe)


def submit_order(name: str, symbol: str, side: str, qty: int,
                 price: float | None = None) -> dict:
    """人工下单。price 缺省取实时快照最新价（停牌则拒收，要求显式限价）。"""
    if side not in ("buy", "sell"):
        raise ValueError(f"side 须为 buy/sell，收到: {side}")
    broker = store.load_broker(name)
    if price is None:
        from lquant.paper.quotes import fetch_snapshot
        snaps = fetch_snapshot([symbol])
        px = snaps[0]["price"] if snaps else 0.0
        if not px:
            raise ValueError(f"{symbol} 无有效最新价（可能停牌），请显式指定价格")
        price = px
    o = broker.submit(symbol, side, int(qty), float(price))
    store.save_broker(name, broker)
    return {"order_id": o.order_id, "status": o.status, "reason": o.reason,
            "symbol": o.symbol, "side": o.side, "qty": o.qty, "price": o.price}


def cancel_order(name: str, order_id: str) -> dict:
    broker = store.load_broker(name)
    for o in broker.orders:
        if o.order_id == order_id and o.status == "pending":
            o.status = "cancelled"
            store.save_broker(name, broker)
            return {"order_id": order_id, "status": "cancelled"}
    raise ValueError(f"无可撤销的挂单: {order_id}")


def tick(name: str) -> dict:
    """盘中推进一次：拉快照 → 策略信号 → 撮合 → 盯市 → 落净值快照。"""
    acct = store.get_account(name)
    broker = store.load_broker(name)
    strategy = resolve_strategy(acct["strategy"])
    eng = PaperEngine(strategy, broker.cfg)
    eng.broker = broker

    pending = {o.symbol for o in broker.orders if o.status == "pending"}
    held = {p.symbol for p in broker.positions.values() if p.qty > 0}
    symbols = sorted(set(acct["universe"]) | held | pending)

    from lquant.paper.quotes import fetch_snapshot
    quotes = fetch_snapshot(symbols) if symbols else []

    touched = 0
    for q in quotes:
        if q["suspended"]:
            continue
        for od in strategy.signals(broker, q):
            broker.submit(od["symbol"], od["side"], int(od["qty"]),
                          float(od.get("price") or q["price"]))
        touched += len(broker.on_quote(q["symbol"], q["price"],
                                       q["limit_up"], q["limit_down"]))
        pos = broker.positions.get(q["symbol"])
        if pos is not None and pos.qty > 0:
            pos.last_price = q["price"]       # 盯市（委托撮合价之外的行情刷新）

    store.save_broker(name, broker)
    store.record_nav(name, today_cn(), broker.nav(), broker.cash,
                     sum(1 for p in broker.positions.values() if p.qty > 0),
                     "intraday")
    return {"account": name, "n_quotes": len(quotes),
            "n_suspended": sum(1 for q in quotes if q["suspended"]),
            "orders_touched": touched,
            "nav": round(broker.nav(), 2), "cash": round(broker.cash, 2),
            "n_orders": len(broker.orders),
            "n_filled": sum(1 for o in broker.orders if o.status == "filled"),
            "n_rejected": sum(1 for o in broker.orders if o.status == "rejected")}


def day_close(name: str, d=None) -> dict:
    """日终：解冻 T+N → 官方日线对账重算 official 净值。"""
    d = d or today_cn()
    broker = store.load_broker(name)
    broker.on_day_close(d)
    store.save_broker(name, broker)

    from lquant.paper.reconcile import reconcile
    return {"account": name, "trade_date": str(d), "reconcile": reconcile(name, d)}


def status(name: str) -> dict:
    acct = store.get_account(name)
    broker = store.load_broker(name)
    orders = sorted(broker.orders, key=lambda o: o.ts, reverse=True)
    return {
        "account": acct,
        "nav": round(broker.nav(), 2),
        "cash": round(broker.cash, 2),
        "total_return": round(broker.nav() / acct["initial_cash"] - 1, 4),
        "positions": broker.positions_frame().to_dicts(),
        "pending_orders": [{"order_id": o.order_id, "symbol": o.symbol,
                            "side": o.side, "qty": o.qty, "price": o.price}
                           for o in orders if o.status == "pending"],
        "recent_orders": [{"order_id": o.order_id, "ts": str(o.ts),
                           "symbol": o.symbol, "side": o.side, "qty": o.qty,
                           "status": o.status, "reason": o.reason}
                          for o in orders[:10]],
    }


def nav_history(name: str, source: str | None = None) -> list[dict]:
    return store.nav_frame(name, source).to_dicts()
