"""收盘后日报编排（借鉴 TradingAgents-AShare 的定时分析推送）。

TradingAgents-AShare 用 APScheduler 每日收盘后跑分析并推送。lquant 的
对应拼装全部已存在，本模块只做**编排**，当前两份日报：

1. ``run_watchlist_digest``：自选清单逐票多角度分析（security.analyze_security）
2. ``run_portfolio_digest``：模拟盘账户绩效（paper/store 官方净值口径）

→ 汇总文本 → ``notify(category="report")``。

**不引 APScheduler**：定时驱动在 lquant 已有多种形态（外部 cron、
task_center、server 生命周期），job 只是一个纯函数，谁定时谁调用 ——
与 ``market/scheduler.collect_and_save`` 的定位一致。

失败语义（与 collect 同哲学）：
  - 单只标的分析失败不影响其他（失败明细进返回值，绝不静默）；
  - 清单/账户为空 → 显式 skipped，不发送空报告；
  - 全部失败 → 不发正文，改为 notify(category="error") 报错摘要；
  - ``sent`` 按真实送达判定：通知被降噪压制或通道全挂时如实回 False。
"""

from __future__ import annotations

from lquant.core.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "format_portfolio_report",
    "format_report",
    "portfolio_snapshot",
    "run_portfolio_digest",
    "run_watchlist_digest",
]


def _watchlist_symbols() -> list[str]:
    """自选清单（DuckDB 小表，按加入时间稳定排序）。"""
    from lquant.core.db import reader

    with reader() as con:
        rows = con.execute("SELECT symbol FROM watchlist ORDER BY added_at").fetchall()
    return [r[0] for r in rows]


def format_report(rep: dict) -> str:
    """单只标的分析报告 → 紧凑文本。防御性取值：缺字段显示「—」不猜。"""
    score = rep.get("score") or {}
    verdict = rep.get("verdict") or {}
    total = score.get("score")
    cov = score.get("angle_coverage")
    head = (
        f"【{rep.get('symbol')}】{rep.get('asof', '')} 综合分 {total if total is not None else '—'}"
    )
    if score.get("grade"):
        head += f"（{score['grade']}）"
    if cov is not None:
        head += f" · 覆盖 {cov * 100:.0f}%"
    lines = [head]
    for p in (verdict.get("points") or [])[:4]:
        lines.append(f"· {p}")
    for r in (verdict.get("risks") or [])[:3]:
        lines.append(f"⚠ {r}")
    return "\n".join(lines)


def run_watchlist_digest(
    *, symbols: list[str] | None = None, asof: str | None = None, analyze_fn=None, notify_fn=None
) -> dict:
    """对自选清单逐个跑多角度分析 → 汇总日报 → notify(category="report")。

    Args:
        symbols: 显式清单；缺省读 watchlist 表。
        asof: 观察日 YYYY-MM-DD；缺省由 analyze_security 取湖内最新交易日。
        analyze_fn / notify_fn: 注入点（测试用 mock；缺省走真实实现）。

    Returns:
        ``{"sent": bool, "ok": [...], "failed": [{"symbol", "error"}],
        "skipped": "清单为空" | None}`` —— 调用方与日志都能看见明细。
    """
    if analyze_fn is None:
        from lquant.security import analyze_security as analyze_fn
    if notify_fn is None:
        from lquant.notify import notify as notify_fn

    syms = list(symbols) if symbols is not None else _watchlist_symbols()
    if not syms:
        return {"sent": False, "ok": [], "failed": [], "skipped": "清单为空"}

    ok: list[str] = []
    failed: list[dict] = []
    blocks: list[str] = []
    for sym in syms:
        try:
            rep = analyze_fn(sym, asof)
            blocks.append(format_report(rep))
            ok.append(sym)
        except Exception as e:  # noqa: BLE001 - 单只失败不影响其他
            failed.append({"symbol": sym, "error": f"{type(e).__name__}: {e}"})
            # loguru 不做 %s 惰性插值，必须用 f-string，否则失败原因不进日志
            log.warning(f"个股分析失败 symbol={sym} err={e}")

    sent = False
    if ok:
        text = "\n\n".join(blocks)
        if failed:
            text += "\n\n（另有 " + ", ".join(f["symbol"] for f in failed) + " 分析失败，详见日志）"
        results = notify_fn(
            "自选股每日报告",
            f"{asof or '最新交易日'} · {len(ok)}/{len(syms)} 只\n\n{text}",
            category="report",
        )
        # sent 按真实送达判定：被降噪压制 / 通道全挂时 SendResult.ok=False，
        # 如实回报而不是把「已生成」冒充「已送达」；注入式 mock 无返回值
        # （results is None）视为已发，保持测试桩兼容。
        sent = results is None or any(getattr(r, "ok", True) for r in results or [])
    else:
        detail = "\n".join(f"{f['symbol']}: {f['error']}" for f in failed)
        notify_fn(
            "自选股每日报告失败",
            f"{len(failed)} 只全部失败，未发送正文：\n{detail}",
            category="error",
        )
    return {"sent": sent, "ok": ok, "failed": failed, "skipped": None}


# ---------------- 组合日报（paper 账户绩效） ----------------


def _fmt_amt(v: float | None) -> str:
    """金额（元）：千分位两位小数；缺失显示「—」不猜。"""
    return f"{v:,.2f}" if v is not None else "—"


def _fmt_pct(v: float | None) -> str:
    return f"{v * 100:.1f}%" if v is not None else "—"


def portfolio_snapshot(account: str) -> dict:
    """模拟盘账户快照：绩效一律取**官方口径**（paper_nav.source=official）。

    Returns:
        ``{"account", "asof", "nav", "prev_nav", "day_pnl", "day_pct",
        "drawdown", "peak", "cash", "n_positions", "positions"(按市值降序,
        含 weight), "top1_weight", "top3_weight"}``；无净值记录时 nav
        系列为 None，分母回退 cash + Σ持仓市值。

    Raises:
        AccountNotFound: 账户不存在（调用方显式 skipped，不当错误处理）。
    """
    from lquant.paper import store as paper_store

    acct = paper_store.get_account(account)
    broker = paper_store.load_broker(account)
    nav_df = paper_store.nav_frame(account, source="official")

    navs = nav_df["nav"].to_list() if len(nav_df) else []
    nav = navs[-1] if navs else None
    prev = navs[-2] if len(navs) >= 2 else None
    peak = max(navs) if navs else None

    positions = []
    for p in broker.positions.values():
        positions.append(
            {
                "symbol": p.symbol,
                "name": p.name or p.symbol,
                "qty": p.qty,
                "last_price": p.last_price,
                "value": p.qty * p.last_price,
            }
        )
    positions.sort(key=lambda x: x["value"], reverse=True)

    # 权重分母：官方 nav 优先（收盘对账值 = 现金 + 持仓市值，最可信）；
    # 无净值记录时用现金 + Σ市值 兜底；两者皆无则权重不可知（None）。
    # last_price<=0（从未定价的新持仓）权重置 None 而非 0：市值 0 伪装成
    # 「0% 集中度」会让 TOP1/TOP3 风控信号失真 —— 报文里显示「—」。
    total: float | None = nav
    if total is None:
        s = acct["cash"] + sum(p["value"] for p in positions)
        total = s if s > 0 else None
    for p in positions:
        p["weight"] = (p["value"] / total) if (total and p["last_price"] > 0) else None
    weights = [p["weight"] for p in positions if p["weight"] is not None]

    day_pnl = (nav - prev) if (nav is not None and prev is not None) else None
    # nav<=0 是脏数据（对账事故）：原始值照传，但派生比例一律置 None，
    # 「—」优于「+100% 盈亏 / -100% 回撤」这类荒谬数
    day_pct = (nav / prev - 1) if (nav and prev and nav > 0 and prev > 0) else None
    return {
        "account": account,
        "asof": str(nav_df["trade_date"][-1]) if navs else None,
        "nav": nav,
        "prev_nav": prev,
        "day_pnl": day_pnl,
        "day_pct": day_pct,
        "drawdown": (1 - nav / peak) if (nav and peak and nav > 0 and peak > 0) else None,
        "peak": peak,
        "cash": acct["cash"],
        "n_positions": len(positions),
        "positions": positions[:20],  # 快照明细截断；权重统计用全量
        "top1_weight": weights[0] if weights else 0.0,
        "top3_weight": sum(weights[:3]) if weights else 0.0,
    }


def format_portfolio_report(snap: dict) -> str:
    """组合快照 → 紧凑文本。防御取值：缺字段显示「—」不猜。"""
    head = f"【组合日报 · {snap.get('account')}】{snap.get('asof') or '无净值记录'}"
    if snap.get("nav") is not None:
        head += f" 总资产 {_fmt_amt(snap['nav'])} 元"
    lines = [head]
    dp = snap.get("day_pnl")
    if dp is not None:
        sign = "+" if dp >= 0 else ""
        pct = snap.get("day_pct")
        pct_txt = f"{sign}{pct * 100:.1f}%" if pct is not None else "—"
        lines.append(f"当日盈亏 {sign}{_fmt_amt(dp)} 元（{pct_txt}）")
    if snap.get("drawdown") is not None:
        lines.append(f"当前回撤 {_fmt_pct(snap['drawdown'])}（峰值 {_fmt_amt(snap['peak'])} 元）")
    lines.append(
        f"现金 {_fmt_amt(snap.get('cash'))} 元 · 持仓 {snap.get('n_positions', 0)} 只"
        f" · 集中度 TOP1 {_fmt_pct(snap.get('top1_weight'))}"
        f" / TOP3 {_fmt_pct(snap.get('top3_weight'))}"
    )
    items = []
    for p in (snap.get("positions") or [])[:8]:
        items.append(
            f"{p.get('name') or p['symbol']} {p['qty']}股 市值{_fmt_amt(p['value'])}"
            f"（{_fmt_pct(p.get('weight'))}）"
        )
    if items:
        lines.append("持仓：" + "；".join(items))
    return "\n".join(lines)


def run_portfolio_digest(*, account: str, notify_fn=None) -> dict:
    """模拟盘账户绩效日报 → notify(category="report")。

    失败语义与 run_watchlist_digest 一致：
    - 账户不存在 → 显式 skipped，不发送；
    - 快照构建失败 → 不吞错，notify(category="error") 报错摘要；
    - ``sent`` 按真实送达判定（降噪压制/通道全挂如实回 False）。
    """
    if notify_fn is None:
        from lquant.notify import notify as notify_fn

    from lquant.paper.store import AccountNotFound

    try:
        snap = portfolio_snapshot(account)
    except AccountNotFound:
        return {"sent": False, "skipped": f"账户不存在: {account}", "account": account}
    except Exception as e:  # noqa: BLE001 - 快照失败改走 error 通道，绝不静默
        detail = f"{type(e).__name__}: {e}"
        log.warning(f"组合日报快照失败 account={account} err={e}")
        results = notify_fn(
            "组合日报失败",
            f"账户 {account} 快照构建失败，未发送正文：\n{detail}",
            category="error",
        )
        sent = results is None or any(getattr(r, "ok", True) for r in results or [])
        return {"sent": sent, "account": account, "error": detail, "skipped": None}

    results = notify_fn(f"组合日报 · {account}", format_portfolio_report(snap), category="report")
    sent = results is None or any(getattr(r, "ok", True) for r in results or [])
    return {"sent": sent, "account": account, "snapshot": snap, "skipped": None}
