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
import os

from lquant.core.types import today_cn
from lquant.paper import store


class _Manual:
    """内置空策略：不开自动信号，人工经 `lq paper order` 下单。

    模拟盘第一阶段的正确姿势 —— 先让行情/撮合/盯市/对账链路跑稳，
    再挂自动策略。自动策略必须与回测同一份信号源，对拍才有意义。
    """

    def signals(self, broker, quote: dict) -> list[dict]:
        return []


def _strategy_prefixes() -> tuple[str, ...]:
    """可导入策略模块前缀白名单（防 HTTP 入口的任意 import）。

    默认只放行 `lquant` 与 `strategies` 前缀；`LQ_PAPER_STRATEGY_PREFIXES`
    （逗号分隔）可追加项目自有包。
    """
    env = os.getenv("LQ_PAPER_STRATEGY_PREFIXES", "")
    extra = tuple(p.strip() for p in env.split(",") if p.strip())
    return ("lquant", "strategies") + extra


def resolve_strategy(ref: str | None):
    """策略引用："manual" 或 "pkg.module:Attr"（无参可实例化）。

    模块名必须在白名单前缀内：/paper/accounts 是无鉴权 HTTP 端点，
    任意 importlib.import_module 等于把 import 边界暴露给调用方。
    """
    ref = (ref or "manual").strip()
    if ref == "manual":
        return _Manual()
    mod_name, _, attr = ref.partition(":")
    if not mod_name or not attr:
        raise ValueError(f"策略引用格式须为 module:Attr，收到: {ref}")
    if not any(mod_name == p or mod_name.startswith(p + ".") for p in _strategy_prefixes()):
        raise ValueError(
            f"策略模块 {mod_name} 不在白名单内（允许前缀: "
            f"{', '.join(_strategy_prefixes())}，可用 LQ_PAPER_STRATEGY_PREFIXES 扩展）"
        )
    cls = getattr(importlib.import_module(mod_name), attr)
    inst = cls()
    if not callable(getattr(inst, "signals", None)):
        raise TypeError(f"{ref} 缺少 signals(broker, quote) 方法")
    return inst


def create_account(
    name: str, initial_cash: float, strategy: str = "manual", universe: list[str] | None = None
) -> dict:
    s = resolve_strategy(strategy)  # 引用合法性前置校验
    del s
    return store.create_account(name, initial_cash, strategy, universe)


def submit_order(name: str, symbol: str, side: str, qty: int, price: float | None = None) -> dict:
    """人工下单。price 缺省取实时快照最新价（停牌则拒收，要求显式限价）。"""
    if side not in ("buy", "sell"):
        raise ValueError(f"side 须为 buy/sell，收到: {side}")
    with store.account_lock(name):
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
    return {
        "order_id": o.order_id,
        "status": o.status,
        "reason": o.reason,
        "symbol": o.symbol,
        "side": o.side,
        "qty": o.qty,
        "price": o.price,
    }


def cancel_order(name: str, order_id: str) -> dict:
    with store.account_lock(name):
        broker = store.load_broker(name)
        for o in broker.orders:
            if o.order_id == order_id and o.status == "pending":
                o.status = "cancelled"
                store.save_broker(name, broker)
                return {"order_id": order_id, "status": "cancelled"}
    raise ValueError(f"无可撤销的挂单: {order_id}")


def tick(name: str) -> dict:
    """盘中推进一次：拉快照 → 策略信号 → 撮合 → 盯市 → 落净值快照。

    快照拉取（网络，可能秒级）在账户锁外做：锁只护 load→撮合→save，
    不让 submit/cancel 在同一账户上被网络 IO 卡住。
    """
    acct = store.get_account(name)
    probe = store.load_broker(name)
    strategy = resolve_strategy(acct["strategy"])
    pending = {o.symbol for o in probe.orders if o.status == "pending"}
    held = {p.symbol for p in probe.positions.values() if p.qty > 0}
    symbols = sorted(set(acct["universe"]) | held | pending)
    from lquant.paper.quotes import fetch_snapshot

    quotes = fetch_snapshot(symbols) if symbols else []

    with store.account_lock(name):
        broker = store.load_broker(name)

        touched = 0
        for q in quotes:
            if q["suspended"]:
                continue
            for od in strategy.signals(broker, q):
                broker.submit(
                    od["symbol"], od["side"], int(od["qty"]), float(od.get("price") or q["price"])
                )
            touched += len(broker.on_quote(q["symbol"], q["price"], q["limit_up"], q["limit_down"]))
            pos = broker.positions.get(q["symbol"])
            if pos is not None and pos.qty > 0:
                pos.last_price = q["price"]  # 盯市（委托撮合价之外的行情刷新）

        store.save_broker(name, broker)
    # 非交易日（周末/节假日手动跑 tick）不落 intraday 净值：日历上没有这天的
    # 官方日线，曲线里会多一个用陈旧快照捏出来的点，对账时对不上。
    if _is_trading_day(today_cn()):
        store.record_nav(
            name,
            today_cn(),
            broker.nav(),
            broker.cash,
            sum(1 for p in broker.positions.values() if p.qty > 0),
            "intraday",
        )
    return {
        "account": name,
        "n_quotes": len(quotes),
        "n_suspended": sum(1 for q in quotes if q["suspended"]),
        "orders_touched": touched,
        "nav": round(broker.nav(), 2),
        "cash": round(broker.cash, 2),
        "n_orders": len(broker.orders),
        "n_filled": sum(1 for o in broker.orders if o.status == "filled"),
        "n_rejected": sum(1 for o in broker.orders if o.status == "rejected"),
    }


def _is_trading_day(d) -> bool:
    """日历有数据才启用门禁：空日历（未同步）一律当交易日，不拦 tick。"""
    try:
        from lquant.data.store.catalog import TradeCalendarRepo

        repo = TradeCalendarRepo()
        if repo.count() == 0:
            return True
        return repo.is_trading_day(d)
    except Exception:  # noqa: BLE001 - 日历不可用（空库等）不阻断 tick 主链路
        return True


def day_close(name: str, d=None) -> dict:
    """日终：解冻 T+N → 官方日线对账重算 official 净值。

    对账 verdict 非 ok、**或本次对账没有可信基准**（缺 intraday 快照 /
    全部持仓取不到官方收盘价 —— 见 ``_notify_reconcile``）时顺手发通知：
    等第二天看板才发现就晚了。通知旁路永不抛异常、未配置
    LQ_NOTIFY_CHANNELS 时零开销 —— 详见 lquant/notify。
    """
    d = d or today_cn()
    with store.account_lock(name):
        broker = store.load_broker(name)
        broker.on_day_close(d)
        store.save_broker(name, broker)

    from lquant.paper.reconcile import reconcile

    rep = reconcile(name, d)
    _notify_reconcile(name, d, rep)
    return {"account": name, "trade_date": str(d), "reconcile": rep}


def _notify_reconcile(name: str, d, rep: dict) -> None:
    """对账告警旁路：对账结论**不可信或背离**时发，正常 ok 静默。

    只认 verdict 会漏掉两类最该叫醒人的场景 —— verdict 缺省即 "ok"，而
    reconcile 仅在 ``intraday is not None and official_nav`` 时才计算偏差：

    (a) 当天没有 intraday 快照（tick 没跑/失败、非交易日手动 ``lq paper close``）
        → 没有基准可对，rel_dev/detail 全空，旧实现判 ok 静音；
    (b) 全部持仓都取不到官方收盘价（行情源整体没落库 —— 正是对账文案里说的
        「行情源延迟」的最严重形态）→ 官方 NAV 回退 ``last_price``，与盘中
        盯市价**同源**，偏差恒 ≈0 → 旧实现判 ok 静音。

    这两类信号在 service 侧显式升格为告警（warning，静默时段豁免）。
    **部分** stale（个别停牌）属常态，单独不刷屏，只在已经要告警时附注。

    必须 ``category="alert"``：这是告警不是报告 —— 走 alert 路由通道，
    且 severity 达到 warning 才能在深夜静默时段豁免。漏传会退化成
    report/info 被降噪压掉。通知旁路永不抛异常。
    """
    verdict = rep.get("verdict", "ok")
    stale = rep.get("stale_symbols") or []
    n_held = rep.get("n_held")
    # 全部持仓 stale：官方价与盯市价同源，本次对账没有独立性
    all_stale = bool(stale) and (n_held is None or len(stale) >= n_held)

    reasons: list[str] = []
    if verdict == "critical":
        reasons.append("官方净值与盘中盯市价严重背离")
    elif verdict == "warning":
        reasons.append("官方净值与盘中盯市价偏离偏高")
    if rep.get("nav_intraday") is None:
        reasons.append("无盘中基准（当日缺 intraday 快照），本次对账无法给出偏差")
    if all_stale:
        reasons.append(
            f"全部 {len(stale)} 只持仓都取不到官方收盘价（行情源未落库）："
            "官方 NAV 退回盯市价，本次对账无独立性"
        )
    if not reasons:
        return  # 正常 ok：静默，别把群里灌满噪音

    note = ""
    if stale and not all_stale:
        note = f"\n（另有 {len(stale)} 只持仓缺官方收盘价，沿用盯市价）"
    # verdict 字典契约保持 reconcile 原值（无基准时仍为 ok），告警文案里
    # 如实显示 unverified，避免「verdict=ok」与「无法对账」自相矛盾
    shown = "unverified" if verdict == "ok" else verdict
    try:
        from lquant.notify import notify

        notify(
            f"模拟盘对账告警 · {name}",
            f"trade_date={d} verdict={shown}\n"
            + "\n".join(f"· {r}" for r in reasons)
            + (f"\n{rep.get('detail')}" if rep.get("detail") else "")
            + note,
            category="alert",
            severity="critical" if verdict == "critical" else "warning",
        )
    except Exception as e:  # noqa: BLE001 - 通知失败绝不影响对账主链路
        from lquant.core.logging import get_logger

        get_logger(__name__).warning(f"paper reconcile notify failed: {e}")


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
        "pending_orders": [
            {
                "order_id": o.order_id,
                "symbol": o.symbol,
                "side": o.side,
                "qty": o.qty,
                "price": o.price,
            }
            for o in orders
            if o.status == "pending"
        ],
        "recent_orders": [
            {
                "order_id": o.order_id,
                "ts": str(o.ts),
                "symbol": o.symbol,
                "side": o.side,
                "qty": o.qty,
                "status": o.status,
                "reason": o.reason,
            }
            for o in orders[:10]
        ],
    }


def nav_history(name: str, source: str | None = None) -> list[dict]:
    return store.nav_frame(name, source).to_dicts()
