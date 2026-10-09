"""行业分析 API。

一个行业名/代码进，一份多角度报告出：

    GET /api/industry/{identifier}/analysis?asof=2026-09-30&std=SW
    GET /api/industry/rotation?window=20&asof=2026-09-30
    GET /api/industry/list

契约要点（与 ``lquant.industry`` 一致）：

- **PIT 安全**：``asof`` 之后的数据一律不参与计算（行业归属按 ``std_date``，
  财务按 ``pub_date``）。``asof`` 缺省取湖内最新交易日 —— 数据同步滞后于
  自然日时，用自然日会把「还没同步」误判成「数据缺失」。
- **缺失即标注**：任何角度取不到数都会在响应里带 ``available=false`` + ``hint``，
  并在 ``verdict.risks`` 里列出，绝不静默补默认值。
- **行业指数口径**：成分股按 PIT 归属等权合成，**不是**官方行业指数。
  响应里 ``disclaimer`` 与 ``overview`` 都会写明，避免被当成申万指数用。
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/industry", tags=["industry"])


def _parse_asof(asof: str | None) -> date | None:
    if not asof:
        return None
    try:
        return date.fromisoformat(asof)
    except ValueError:
        raise HTTPException(422, f"无效的日期: {asof!r}，应为 YYYY-MM-DD") from None


@router.get("/angles")
def industry_angles() -> dict:
    """角度注册表（id / 名称 / 权重 / 说明）—— 前端图例与文档用。"""
    from lquant.industry import ANGLES, SCHEMA_VERSION

    return {
        "schema_version": SCHEMA_VERSION,
        "angles": [
            {"id": a.id, "label": a.label, "weight": a.weight, "desc": a.desc}
            for a in ANGLES
        ],
    }


@router.get("/list")
def industry_list(
    std: str | None = Query(default=None, max_length=16,
                            description="行业分类标准（SW / CICS / em）；缺省自动挑"),
    asof: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$",
                             description="观察日 YYYY-MM-DD；缺省取湖内最新交易日"),
) -> dict:
    """行业清单：代码 / 名称 / 成员数 / 最近交易日收益（评级行情的索引）。"""
    from lquant.industry import list_industry_names

    return list_industry_names(_parse_asof(asof), std)


@router.get("/rotation")
def industry_rotation(
    window: int = Query(default=20, ge=5, le=250,
                        description="排名用的区间窗口（交易日）"),
    std: str | None = Query(default=None, max_length=16),
    asof: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
) -> dict:
    """全行业轮动榜：区间收益、成员数、成交额、估值中位数 + 排名。

    这是行业分析最常用的一屏 —— 「钱在往哪个行业走、谁在领跑、贵不贵」。
    """
    from lquant.industry import industry_rotation as _rotation

    return _rotation(_parse_asof(asof), std, window)


@router.get("/{identifier}/analysis")
def industry_analysis(
    identifier: str,
    std: str | None = Query(default=None, max_length=16),
    asof: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
) -> dict:
    """单个行业的多角度分析报告。

    角度：趋势与轮动 / 景气度 / 估值 / 资金与拥挤度 / 宽度与情绪，
    外加独立的行业风险提示块。综合分按可用角度重新归一化权重，并如实报告覆盖度。

    ``identifier`` 可以是行业代码（``801780.SI``）或中文名（``银行``）。
    """
    from lquant.industry import analyze_industry

    day = _parse_asof(asof)
    try:
        return analyze_industry(identifier, day, std)
    except KeyError:
        raise HTTPException(404, _not_found_detail(identifier, std)) from None


def _not_found_detail(identifier: str, std: str | None) -> str:
    """404 时把「湖里到底有什么」告诉用户 —— 少一次来回猜。"""
    from lquant.core.db import reader
    from lquant.industry.loader import (
        available_stds,
        list_industries,
        resolve_asof,
    )

    try:
        day = resolve_asof(None)
        with reader() as con:
            stds = available_stds(con, day)
            names = list_industries(con, day, std)["industry_name"].to_list() \
                if stds else []
    except Exception:  # noqa: BLE001 - 诊断信息失败不该盖掉 404 本身
        return (f"找不到行业 {identifier!r}；行业分类数据可能尚未同步"
                "（先执行 `lq data industry` 或全量同步）")
    if not stds:
        return (f"找不到行业 {identifier!r}：行业分类数据为空"
                "（industry_classify 为空），先执行 `lq data industry` 或全量同步")
    head = "、".join(names[:12]) + ("…" if len(names) > 12 else "")
    return (f"找不到行业 {identifier!r}（标准 {std or stds[0]}）。"
            f"可用行业：{head}")
