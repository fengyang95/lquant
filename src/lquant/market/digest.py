"""收盘后自选股日报（借鉴 TradingAgents-AShare 的定时个股分析）。

TradingAgents-AShare 用 APScheduler 每日收盘后对关注股跑多 Agent 分析并
推送。lquant 的对应拼装全部已存在：``security.analyze_security``（多角度
报告）+ ``notify``（分类路由）+ watchlist 表 —— 本模块只做**编排**：
读清单 → 逐个分析 → 汇总文本 → ``notify(category="report")``。

**不引 APScheduler**：定时驱动在 lquant 已有多种形态（外部 cron、
task_center、server 生命周期），job 只是一个纯函数，谁定时谁调用 ——
与 ``market/scheduler.collect_and_save`` 的定位一致。

失败语义（与 collect 同哲学）：
  - 单只标的分析失败不影响其他（失败明细进返回值，绝不静默）；
  - 清单为空 → 显式 skipped，不发送空报告；
  - 全部失败 → 不发正文，改为 notify(category="error") 报错摘要；
  - ``sent`` 按真实送达判定：通知被降噪压制或通道全挂时如实回 False。
"""

from __future__ import annotations

from lquant.core.logging import get_logger

log = get_logger(__name__)

__all__ = ["run_watchlist_digest", "format_report"]


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
            log.warning("个股分析失败 symbol=%s err=%s", sym, e)

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
