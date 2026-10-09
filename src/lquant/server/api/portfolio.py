"""组合工具 API：把 ``portfolio/`` 包暴露给服务层（FEATURE-IDEAS C4）。

CLI（``lq portfolio …``）与 API 是同一套能力的两个入口。编排逻辑（读湖 →
造因子 → 选池 / 权重 / 优化）放在 ``lquant.cli.commands.portfolio`` 的纯函数里，
本模块只做三件事：pydantic 校验入参、调用纯函数、把领域异常映射成 HTTP 状态码。

**为什么反向依赖 CLI 模块**：硬约束只允许新增本书面文件与
``cli/commands/portfolio.py``，且不得改 ``portfolio/`` 内部；反过来让 CLI
依赖 server 会让 `lq portfolio --help` 强依赖 fastapi（server 是 optional
extra）。click 是核心依赖，所以「纯函数在 CLI 侧、API 导入」是唯一不引入
额外运行时耦合的方向。

错误语义：
- ``PortfolioDataError`` → 404：数据没有（湖为空 / 该日无行情 / 历史太短）；
- 其余 ``PortfolioError`` / ``ValueError`` → 422：调用方参数不对。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


class FilterIn(BaseModel):
    """选池过滤条件，直接映射 ``portfolio.screener.FilterConfig``。"""

    exclude_st: bool = True
    min_list_days: int = Field(60, ge=0, description="上市天数下限（剔除次新）")
    min_amount: float = Field(5_000_000.0, ge=0, description="成交额下限（元）")
    min_price: float = Field(1.0, ge=0, description="价格下限（元）")
    exclude_suspended: bool = True
    exclude_limit_up: bool = True
    exclude_limit_down: bool = False
    max_count: int | None = Field(None, ge=1)


class ScreenIn(BaseModel):
    date: str | None = Field(None, description="交易日 YYYY-MM-DD；缺省取湖内最新")
    top_n: int = Field(30, ge=1, le=5000)
    factors: list[str] | dict[str, float] | None = Field(
        None, description="打分因子；list=等权、dict=指定权重。缺省 mom20")
    lookback_days: int = Field(180, ge=5, le=2000, description="往前读多少日历日算因子")
    symbols: list[str] | None = None
    filters: FilterIn = Field(default_factory=FilterIn)


class SizeIn(BaseModel):
    model: Literal["atr", "kelly"] = "atr"
    symbol: str | None = Field(None, description="仅用于回显")
    close: float | None = None
    atr: float | None = None
    daily_risk: float = Field(0.01, gt=0)
    max_weight: float = Field(1.0, gt=0, le=1)
    win_rate: float | None = Field(None, gt=0, lt=1)
    win_loss_ratio: float | None = Field(None, gt=0)
    kelly_fraction: float = Field(0.5, gt=0, le=1)


class WeightsIn(BaseModel):
    method: str = Field("equal", description="见 GET /api/portfolio/methods")
    symbols: list[str] | None = None
    scores: dict[str, float] | str | None = Field(
        None, description="分数：dict 或 'SYM=v,SYM2=v2'（enhanced_indexing / score_weight）")
    start: str | None = None
    end: str | None = None
    lookback_days: int = Field(120, ge=2, le=2000)
    band: float | None = Field(None, gt=0, description="no-trade band；须配 prev_weights")
    prev_weights: dict[str, float] | str | None = None
    benchmark: dict[str, float] | str | None = None
    cov_method: str | None = None
    sqrt_cap: bool = False


class OptimizeIn(BaseModel):
    symbols: list[str]
    scores: dict[str, float] | str | None = None
    benchmark: dict[str, float] | str | None = None
    prev_weights: dict[str, float] | str | None = None
    te_target: float = Field(0.05, gt=0, description="年化跟踪误差上限")
    max_weight: float = Field(0.10, gt=0, le=1)
    max_active: float | None = Field(None, gt=0)
    cov_method: str = "shrink_lw"
    max_turnover: float | None = Field(None, gt=0)
    band: float | None = Field(None, gt=0)
    start: str | None = None
    end: str | None = None
    lookback_days: int = Field(180, ge=5, le=2000)


def _handle(fn):
    """跑纯函数并把领域异常映射成 HTTP 语义。"""
    from lquant.cli.commands.portfolio import PortfolioDataError, PortfolioError

    try:
        return fn()
    except PortfolioDataError as e:
        # 数据没有 ≠ 参数错：404 让前端提示「先同步数据」而不是「改参数」
        raise HTTPException(status_code=404, detail=str(e)) from e
    except PortfolioError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/methods")
def list_methods_api() -> dict:
    """可用权重方法（``weighting.METHODS`` + 未注册的两个独立函数）。"""
    from lquant.cli.commands.portfolio import list_methods

    return list_methods()


@router.post("/screen")
def screen_api(req: ScreenIn) -> dict:
    """全市场横截面选股：先过滤，再打分，取 Top-N。"""
    from lquant.cli.commands.portfolio import screen_cross_section

    cfg = None
    if req.filters is not None:
        from lquant.portfolio.screener import FilterConfig

        cfg = FilterConfig(**req.filters.model_dump())
    return _handle(lambda: screen_cross_section(
        trade_date=req.date, top_n=req.top_n, factors=req.factors, cfg=cfg,
        lookback_days=req.lookback_days, symbols=req.symbols,
    ))


@router.post("/size")
def size_api(req: SizeIn) -> dict:
    """单标的仓位：ATR 风险预算 / 半 Kelly。"""
    from lquant.cli.commands.portfolio import size_position

    return _handle(lambda: size_position(
        model=req.model, symbol=req.symbol, close=req.close, atr=req.atr,
        daily_risk=req.daily_risk, max_weight=req.max_weight,
        win_rate=req.win_rate, win_loss_ratio=req.win_loss_ratio,
        kelly_fraction=req.kelly_fraction,
    ))


@router.post("/weights")
def weights_api(req: WeightsIn) -> dict:
    """组合权重：``weighting.weights()`` + ``weight_report`` 关键指标。"""
    from lquant.cli.commands.portfolio import portfolio_weights

    return _handle(lambda: portfolio_weights(
        method=req.method, symbols=req.symbols, scores=req.scores,
        start=req.start, end=req.end, lookback_days=req.lookback_days,
        band=req.band, prev_weights=req.prev_weights, benchmark=req.benchmark,
        cov_method=req.cov_method, sqrt_cap=req.sqrt_cap,
    ))


@router.post("/optimize")
def optimize_api(req: OptimizeIn) -> dict:
    """基准相对优化：TE 约束下最大化预期超额（含换手上限 / no-trade band）。"""
    from lquant.cli.commands.portfolio import optimize_portfolio

    return _handle(lambda: optimize_portfolio(
        symbols=req.symbols, scores=req.scores, benchmark=req.benchmark,
        prev_weights=req.prev_weights, te_target=req.te_target,
        max_weight=req.max_weight, max_active=req.max_active,
        cov_method=req.cov_method, max_turnover=req.max_turnover, band=req.band,
        start=req.start, end=req.end, lookback_days=req.lookback_days,
    ))
