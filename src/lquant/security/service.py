"""个股分析编排：取数 → 各角度 → 综合分 → 报告。

对外的唯一入口是 :func:`analyze_security`。它保证：

- **不抛业务异常**：数据缺失走 ``available=False`` + ``hint``，只有非法代码才报错；
- **PIT 安全**：一切以 ``asof`` 为界（见 :mod:`lquant.security.loader`）；
- **JSON 安全**：返回值可直接 ``json.dumps``（无 NaN/Infinity）。
"""
from __future__ import annotations

from datetime import date

from lquant.security import angles as A
from lquant.security.contract import SCHEMA_VERSION, json_safe, metric
from lquant.security.loader import (
    CANONICAL_FINANCIAL,
    MarketData,
    load_all,
    resolve_asof,
)
from lquant.security.score import build_verdict, composite

#: 免责声明（随报告返回，前端固定展示）。
DISCLAIMER = (
    "本报告由平台按公开数据自动计算，仅供研究参考，不构成任何投资建议。"
    "所有指标均以观察日为界（PIT），不含未来数据；数据缺失的角度会明确标注，"
    "不会用默认值填充。"
)


def analyze_security(symbol: str, asof: date | str | None = None) -> dict:
    """对单只标的做多角度分析。

    Args:
        symbol: 已归一化（``600519.SH``）或裸码（``600519``）——由调用方保证合法。
        asof: 观察日；None 表示用湖内最新交易日。

    Returns:
        报告 dict（结构见 :data:`SCHEMA_VERSION`）。
    """
    from lquant.core.types import parse_symbol

    sym = str(parse_symbol(symbol))
    day = resolve_asof(asof)
    md = load_all(sym, day)

    angle_results = [
        A.technical_angle(md.bars),
        A.fundamental_angle(md.financial, md.financial_peers, md.industry,
                            md.peer_count, CANONICAL_FINANCIAL),
        A.valuation_angle(md.valuation, md.valuation_cross),
        A.capital_angle(md.money_flow, sym),
        A.relative_angle(md.bars, md.benchmark, md.benchmark_symbol, md.industry),
        A.news_angle(md.news, day),
    ]
    # 契约顺序（不是计算顺序）——前端渲染稳定
    from lquant.security.contract import ANGLES
    order = {a.id: i for i, a in enumerate(ANGLES)}
    angle_results.sort(key=lambda a: order.get(a["id"], 99))

    risk = A.risk_block(md.bars, md.benchmark, md.benchmark_symbol,
                        {"is_st": md.is_st})
    score = composite(angle_results)
    verdict = build_verdict(angle_results, score, risk,
                            {"notes": md.notes})

    report = {
        "schema_version": SCHEMA_VERSION,
        "symbol": sym,
        "asof": day.isoformat(),
        "overview": _overview(md, day),
        "score": score,
        "verdict": verdict,
        "angles": angle_results,
        "risk": risk,
        "disclaimer": DISCLAIMER,
    }
    return json_safe(report)


def _overview(md: MarketData, day: date) -> dict:
    """行情/估值快照。取不到的项一律 None（前端显示「—」）。"""
    price = change_pct = amount = turnover = None
    bars = md.bars
    if not bars.is_empty():
        last = bars.tail(1).row(0, named=True)
        price = last.get("close")
        amount = last.get("amount")
        turnover = last.get("turnover_rate")
        if bars.height > 1:
            prev = bars.tail(2).head(1).row(0, named=True).get("close")
            if prev:
                change_pct = (price / prev - 1) * 100 if price is not None else None
        day = last.get("trade_date") or day

    valuation = {}
    if not md.valuation.is_empty():
        v = md.valuation.tail(1).row(0, named=True)
        for f in ("pe_ttm", "pb_mrq", "ps_ttm", "dv_ttm", "total_mv", "float_mv"):
            valuation[f] = v.get(f)
        if valuation.get("total_mv") is not None:
            mv = valuation["total_mv"]
            valuation["total_mv_display"] = f"{mv / 1e8:,.0f} 亿"

    metrics = [
        metric("price", "最新价", price,
               display=None if price is None else f"{price:.2f}", unit="元",
               note=None if change_pct is None else f"{change_pct:+.2f}%"),
        metric("change_pct", "涨跌幅", change_pct,
               display=None if change_pct is None else f"{change_pct:+.2f}%", unit="%"),
        metric("amount", "成交额", amount,
               display=None if amount is None else f"{amount / 1e8:.2f} 亿", unit="元"),
        metric("turnover_rate", "换手率", turnover,
               display=None if turnover is None else f"{turnover:.2f}%", unit="%"),
        metric("pe_ttm", "PE(TTM)", valuation.get("pe_ttm"),
               display=None if valuation.get("pe_ttm") is None
               else f"{valuation['pe_ttm']:.2f}", unit="倍"),
        metric("pb_mrq", "PB(MRQ)", valuation.get("pb_mrq"),
               display=None if valuation.get("pb_mrq") is None
               else f"{valuation['pb_mrq']:.2f}", unit="倍"),
        metric("total_mv", "总市值", valuation.get("total_mv"),
               display=valuation.get("total_mv_display"), unit="元"),
    ]
    return {
        "symbol": md.symbol,
        "name": md.name,
        "asof": day.isoformat(),
        "sec_type": md.sec_type,
        "board": md.board,
        "is_st": md.is_st,
        "industry": md.industry,
        "industry_code": md.industry_code,
        "peer_count": md.peer_count,
        "benchmark": md.benchmark_symbol,
        "metrics": metrics,
        "notes": list(md.notes),
    }
