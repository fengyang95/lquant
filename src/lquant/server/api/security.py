"""个股分析 API。

一个代码进，一份多角度报告出：

    GET /api/security/{symbol}/analysis?asof=2026-09-30

契约要点（与 ``lquant.security`` 一致）：

- **PIT 安全**：``asof`` 之后的数据一律不参与计算（财务按 ``pub_date``）。
  ``asof`` 缺省取湖内最新交易日，而不是自然日 —— 数据同步滞后于自然日时，
  用自然日会把「还没同步」误判成「数据缺失」。
- **缺失即标注**：任何角度取不到数都会在响应里带 ``available=false`` + ``hint``，
  并在 ``verdict.risks`` 里列出，绝不静默补默认值。
- **裸码可用**：``600519`` / ``600519.SH`` / ``sh.600519`` 都能进（统一归一）。
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query

from lquant.server.deps import resolve_symbol

router = APIRouter(prefix="/security", tags=["security"])


@router.get("/{symbol}/analysis")
def security_analysis(
    symbol: str,
    asof: str | None = Query(
        default=None, pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="观察日 YYYY-MM-DD；缺省取湖内最新交易日"),
) -> dict:
    """单只标的的多角度分析报告。

    角度：技术面 / 基本面 / 估值 / 资金面 / 行业与相对强度 / 消息面，
    外加独立的风险提示块。综合分按可用角度重新归一化权重，并如实报告覆盖度。
    """
    sym = resolve_symbol(symbol)
    day: date | None = None
    if asof:
        try:
            day = date.fromisoformat(asof)
        except ValueError:
            raise HTTPException(422, f"无效的日期: {asof!r}，应为 YYYY-MM-DD") from None

    from lquant.security import analyze_security

    return analyze_security(sym, day)


@router.get("/{symbol}/chan")
def security_chan(
    symbol: str,
    limit: int = Query(
        default=250, ge=30, le=500,
        description="展示窗口的日线根数；结构按全量历史算，只截展示窗口"),
) -> dict:
    """日线缠论结构（分型 / 笔 / 中枢 / MACD 面积背驰 / 一二三类买卖点）。

    三条契约要点：

    - **只返回已确认结构**：无条件丢弃最新一笔，因此结果不会随后续数据改变
      （前缀不变性，见 ``lquant.indicators.chan`` 的模块说明）。
    - **不参与评分**：缠论只作展示，不影响 ``/analysis`` 的技术面得分。
    - **缺失即降级**：未安装 czsc 或日线不足时返回 ``available=false`` + ``reason``，
      不抛 5xx —— 前端据此显示提示而不是整页报错。

    ``limit`` 只截**展示窗口**：笔/中枢仍用全量历史算出（近期结构才正确），
    信号也只算窗口内（``signal_tail``，纯性能开关，不改变取值）。
    """
    sym = resolve_symbol(symbol)

    from lquant.data.store.parquet import read_daily
    from lquant.indicators.chan import ChanUnavailable, compute_chan, tail_result

    df = read_daily([sym]).collect()
    if df.is_empty():
        return {"symbol": sym, "available": False, "reason": "湖内无该标的日线数据"}
    df = df.sort("trade_date")
    since = df.tail(limit)["trade_date"][0]
    try:
        result = compute_chan(df, symbol=sym, signal_tail=limit)
    except ChanUnavailable as exc:
        return {"symbol": sym, "available": False, "reason": exc.reason}
    return tail_result(result, since).to_dict()


@router.get("/angles")
def security_angles() -> dict:
    """角度注册表（id / 名称 / 权重 / 说明）—— 前端图例与文档用。"""
    from lquant.security import ANGLES, SCHEMA_VERSION

    return {
        "schema_version": SCHEMA_VERSION,
        "angles": [
            {"id": a.id, "label": a.label, "weight": a.weight, "desc": a.desc}
            for a in ANGLES
        ],
    }
