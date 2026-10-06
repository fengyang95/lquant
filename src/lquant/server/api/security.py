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
