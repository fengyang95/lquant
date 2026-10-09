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
from datetime import date

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
    explicit_price = price is not None
    with store.account_lock(name):
        broker = store.load_broker(name)
        q_name, q_is_st = None, None
        if price is None:
            from lquant.paper.quotes import fetch_snapshot

            snaps = fetch_snapshot([symbol])
            px = snaps[0]["price"] if snaps else 0.0
            if not px:
                raise ValueError(f"{symbol} 无有效最新价（可能停牌），请显式指定价格")
            price = px
            # 快照顺手带出 name/is_st：名称决定 ETF 的 T+0/T+1，is_st 决定
            # 涨跌停档 —— 缺了会退化成「一律 T+1 + 非 ST」
            q_name = snaps[0].get("name") if snaps else None
            q_is_st = ("ST" in (q_name or "").upper()) if q_name else None
        o = broker.submit(symbol, side, int(qty), float(price),
                          name=q_name, is_st=q_is_st, limit=explicit_price)
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
            q_name = q.get("name") or ""
            # name/is_st 必须透传（与 engine.push 同一要求）：名称决定 ETF 的
            # T+0/T+1（黄金/债券/货币/QDII），is_st 决定 5% 涨跌停档；丢了就
            # 退化成「一律 T+1 + 非 ST」，T+0 ETF 当日卖出被错误拒单
            q_is_st = ("ST" in q_name.upper()) if q_name else None
            for od in strategy.signals(broker, q):
                has_px = od.get("price") is not None
                broker.submit(
                    od["symbol"], od["side"], int(od["qty"]),
                    float(od["price"] if has_px else q["price"]),
                    name=q_name or od.get("name"),
                    is_st=q_is_st,
                    limit=has_px,
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

    两道防线（2026-10-08 审计修复）：
    - **交易日门禁**：周末/节假日调用（cron 配错、UI 误点）会把「交易日
      已过」的 T+N 冻结提前解冻，还会给非交易日写一条官方净值 —— tick 的
      intraday 净值有门禁，这里此前没有。
    - **幂等标记**：同一交易日重复调用会把 frozen 台账再减一天（T+1 买入
      当日即解冻，T+N 约束被击穿）。以 broker.last_day_close 持久化记账，
      日期不前进就不重复递减。

    对账 verdict 非 ok、**或本次对账没有可信基准**（缺 intraday 快照 /
    全部持仓取不到官方收盘价 —— 见 ``_notify_reconcile``）时顺手发通知：
    等第二天看板才发现就晚了。通知旁路永不抛异常、未配置
    LQ_NOTIFY_CHANNELS 时零开销 —— 详见 lquant/notify。

    非交易日是**显式 skipped**：不落 official 净值、也不解冻 T+N。tick 早已
    用同一 ``_is_trading_day`` 挡住 intraday 落库，这里此前漏了 —— 周末/
    节假日手动 ``lq paper close`` 或 ``POST /paper/close`` 重试会写进一个
    日历上不存在的 official 点，组合日报按 ``trade_date`` 取最后一根当
    「当前」，``prev_nav``/``day_pnl``/``peak``/回撤就全部以幽灵点为基准。
    """
    d = d or today_cn()
    # 先归一成 date：字符串日期既喂给 _is_trading_day，也决定了后续落库的
    # trade_date。放在 load_broker 之前是故意的 —— 非法日期不再先改动
    # broker、再在 reconcile 里才抛错（避免半截副作用）。
    d = date.fromisoformat(d) if isinstance(d, str) else d
    if not _is_trading_day(d):
        # 账户不存在仍要显式报错：跳过不等于「这个账户没问题」，不能吞掉 404。
        store.get_account(name)
        return {
            "account": name,
            "trade_date": str(d),
            "skipped": True,
            "skip_reason": "non_trading_day",
            "detail": f"{d} 非交易日：跳过 T+N 解冻与官方对账，不落 official 净值",
            "reconcile": None,
        }
    # 幂等标记：同一交易日重复调用不能重复递减 T+N 冻结台账
    frozen_released = True
    with store.account_lock(name):
        broker = store.load_broker(name)
        if broker.last_day_close is not None and str(d) <= broker.last_day_close:
            # 幂等：该日已日结（或更早），不再递减冻结台账；对账仍可重跑
            frozen_released = False
        else:
            broker.on_day_close(d)
            broker.last_day_close = str(d)
            store.save_broker(name, broker)

    from lquant.paper.reconcile import reconcile

    rep = reconcile(name, d)
    rep["frozen_released"] = frozen_released
    if not frozen_released:
        rep["detail"] = (rep.get("detail", "") +
                         "（重复 day_close：T+N 冻结未重复递减）").strip()
    _notify_reconcile(name, d, rep)
    return {"account": name, "trade_date": str(d), "reconcile": rep}


def _notify_reconcile(name: str, d, rep: dict) -> None:
    """对账告警旁路：对账结论**不可信或背离**时发，正常 ok 静默。

    只认 verdict 会漏掉两类最该叫醒人的场景 —— verdict 缺省即 "ok"，而
    reconcile 仅在 ``intraday is not None and official_nav`` 时才计算偏差：

    (a) 当天没有 intraday 快照（tick 没跑/失败）→ 没有基准可对，
        rel_dev/detail 全空，旧实现判 ok 静音；
    (b) 全部持仓都取不到官方收盘价（行情源整体没落库 —— 正是对账文案里说的
        「行情源延迟」的最严重形态）→ 官方 NAV 回退 ``last_price``，与盘中
        盯市价**同源**，偏差恒 ≈0 → 旧实现判 ok 静音。

    这两类信号在 service 侧显式升格为告警（warning，静默时段豁免）。
    **部分** stale（个别停牌）属常态，单独不刷屏，只在已经要告警时附注。

    必须 ``category="alert"``：这是告警不是报告 —— 走 alert 路由通道，
    且 severity 达到 warning 才能在深夜静默时段豁免。漏传会退化成
    report/info 被降噪压掉。通知旁路永不抛异常。

    幂等：同账户 + 同日 + 同 verdict 只发一次（见 ``_claim_reconcile_alert``）。
    ``lq paper close`` 重跑、``POST /paper/close`` 重试都会二次进入本函数，
    而 notify 自身的 dedup/cooldown 缺省是关的，挡不住重复告警。
    """
    if rep.get("skipped"):
        return  # 非交易日：本次根本没有对账，发告警只会制造噪音
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

    # 先认领再发：并发/重复 close 只有一个能通过。
    try:
        claimed = _claim_reconcile_alert(name, d, verdict)
    except Exception as e:  # noqa: BLE001 - 幂等状态不可用不能退化成漏告警
        from lquant.core.logging import get_logger

        get_logger(__name__).warning(f"paper reconcile dedup state unavailable: {e}")
        claimed = True  # 宁可重发，不可漏发
    if not claimed:
        return

    note = ""
    if stale and not all_stale:
        note = f"\n（另有 {len(stale)} 只持仓缺官方收盘价，沿用盯市价）"
    # verdict 字典契约保持 reconcile 原值（无基准时仍为 ok），告警文案里
    # 如实显示 unverified，避免「verdict=ok」与「无法对账」自相矛盾
    shown = "unverified" if verdict == "ok" else verdict
    # 证据行：告警的价值全在这几个数上。旧正文只有 trade_date+verdict+detail，
    # 收到告警的人看不到两个净值/偏差/缺口数量，只能回看板上翻 —— 对「立刻
    # 介入」毫无帮助。key=value 形式便于 IM 里目视，也便于日志里 grep。
    evidence = (
        f"nav_official={rep.get('nav_official')} "
        f"nav_intraday={rep.get('nav_intraday')} "
        f"rel_dev={rep.get('rel_dev')}\n"
        f"stale={len(stale)} n_uncovered={rep.get('n_uncovered')} n_held={n_held}"
    )
    text = (
        f"trade_date={d} verdict={shown}\n"
        + "\n".join(f"· {r}" for r in reasons)
        + f"\n{evidence}"
        + (f"\n{rep.get('detail')}" if rep.get("detail") else "")
        + note
    )
    try:
        from lquant.notify import notify

        results = notify(
            f"模拟盘对账告警 · {name}",
            text,
            category="alert",
            severity="critical" if verdict == "critical" else "warning",
        )
    except Exception as e:  # noqa: BLE001 - 通知失败绝不影响对账主链路
        from lquant.core.logging import get_logger

        get_logger(__name__).warning(f"paper reconcile notify failed: {e}")
        _release_reconcile_alert_safely(name, d, verdict)
        return
    # 一片都没送出去（通道未配置/全挂）不登记为已完成，下一次 close 仍可重试；
    # 与 notify 内部「全部失败撤销 dedup 登记」同一语义。
    if not _alert_delivered(results):
        _release_reconcile_alert_safely(name, d, verdict)


# 对账告警幂等状态。挂在 paper 库（LQ_PAPER_DB）而不是 notify/rules.db：
# 告警幂等是账户维度状态，跟 paper_nav 同生共死；测试也用同一个 LQ_PAPER_DB
# 隔离，不必再引第二套路径 + 第二套清理。
_RECONCILE_ALERT_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_reconcile_alert(
  account TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  verdict TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  PRIMARY KEY (account, trade_date, verdict)
);
"""


def _reconcile_alert_conn():
    import sqlite3

    con = sqlite3.connect(store.db_path(), timeout=30)
    con.executescript(_RECONCILE_ALERT_SCHEMA)
    return con


def reset_reconcile_alert_state() -> None:
    """清空对账告警幂等表。

    测试隔离入口（对齐 ``lquant.notify.service.reset_suppress_state``）：
    幂等状态按 ``(account, trade_date, verdict)`` 持久化，若多个用例共用
    同一个 ``LQ_PAPER_DB``，前一个用例登记过的键会把后一个用例的告警压掉
    —— 用例就会变成「整文件跑 FAIL、单跑 PASS」的顺序依赖。生产不需要
    调用：跨进程保留正是幂等的目的。
    """
    con = _reconcile_alert_conn()
    try:
        con.execute("DELETE FROM paper_reconcile_alert")
        con.commit()
    finally:
        con.close()


def _claim_reconcile_alert(name: str, d, verdict: str) -> bool:
    """原子认领 (account, trade_date, verdict)。True = 本次由我发送。

    主键冲突即幂等锁：并发的两个 close（server 线程池重试）只有一个
    ``rowcount == 1``，另一个直接跳过，不会双发。
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    con = _reconcile_alert_conn()
    try:
        cur = con.execute(
            "INSERT OR IGNORE INTO paper_reconcile_alert"
            "(account,trade_date,verdict,sent_at) VALUES (?,?,?,?)",
            [name, str(d), verdict, datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()],
        )
        con.commit()
        return cur.rowcount == 1
    finally:
        con.close()


def _release_reconcile_alert(name: str, d, verdict: str) -> None:
    con = _reconcile_alert_conn()
    try:
        con.execute(
            "DELETE FROM paper_reconcile_alert WHERE account=? AND trade_date=? AND verdict=?",
            [name, str(d), verdict],
        )
        con.commit()
    finally:
        con.close()


def _release_reconcile_alert_safely(name: str, d, verdict: str) -> None:
    try:
        _release_reconcile_alert(name, d, verdict)
    except Exception as e:  # noqa: BLE001 - 撤销失败最坏是少一次重试，不能掀翻主链路
        from lquant.core.logging import get_logger

        get_logger(__name__).warning(f"paper reconcile dedup release failed: {e}")


def _alert_delivered(results) -> bool:
    """notify 返回值 → 是否至少有一个通道真正送出。"""
    if results is None:
        return True  # 注入的 notify_fn 无返回值：按已送出处理，避免重复刷屏
    try:
        return any(bool(getattr(r, "ok", False)) for r in results)
    except TypeError:  # 非可迭代的测试替身：无法判定，宁可当已送出
        return True


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
