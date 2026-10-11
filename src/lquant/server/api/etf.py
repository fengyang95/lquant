"""ETF 专区 API（/etf）：元数据 + 相关性 + **ETF 专属因子**，走统一封套。

附录 A：ETF 元数据字段（折溢价、跟踪指数、T+N）—— etf_meta 表。
correlation：基于日线收益率两两相关，样本不足时返回空并附说明（不 500）。
ETF 源数据由数据采集/同步负责；这里只读，缺数据即空态。

因子子域（/etf/universes、/etf/factors、/etf/evaluate、/etf/backtest）：
ETF 因子与个股因子是两套口径（ETF 池按基金类型细分、无行业中性化、评级阈值不同），
见 ``lquant.factors.etf``。这里只是 HTTP 薄封装，计算全在 factors/backtest 层。
"""
from __future__ import annotations

import math

from fastapi import HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from lquant.core.db import reader
from lquant.factors.evaluate.defaults import DEFAULT_N_GROUPS
from lquant.server.envelope import make_router

router = make_router(prefix="/etf", tags=["etf"])

_META_COLS = ["symbol", "name", "track_index", "fund_type", "is_cross_border",
              "sellable_after_days", "management_fee", "fund_size", "as_of"]


def _meta_rows(q: str | None = None, limit: int = 200,
               symbol: str | None = None) -> list[dict]:
    """读 etf_meta 表（纯逻辑，供 route 与 by-symbol 复用——不可直接调 route 函数，
    会绕过 FastAPI 的 Query 默认值注入，导致 int(Query(...)) 报错）。

    symbol 给定则只按代码前缀精确过滤（by-symbol 命中无须受 200 行 LIMIT 限制）。
    """
    sql = ("SELECT symbol, name, track_index, fund_type, is_cross_border, "
           "sellable_after_days, management_fee, custody_fee, fund_size, source, as_of "
           "FROM etf_meta")
    params: list = []
    if symbol:
        sql += " WHERE symbol LIKE ?"
        params.append(f"{symbol}%")
    elif q:
        sql += " WHERE symbol LIKE ? OR name LIKE ?"
        params += [f"%{q}%", f"%{q}%"]
    # symbol 过滤时只可能命中 ≤1 行，不套 LIMIT，避免第 200 只之后误 404。
    if not symbol:
        sql += " ORDER BY symbol"
        sql += f" LIMIT {int(limit)}"
    try:
        with reader() as con:
            rows = con.execute(sql, params).fetchall()
    except Exception:  # noqa: BLE001 - 表缺失/未同步
        return []
    out = []
    for r in rows:
        out.append({"symbol": r[0], "name": r[1], "track_index": r[2],
                    "fund_type": r[3], "is_cross_border": bool(r[4]),
                    "sellable_after_days": r[5],
                    "management_fee": r[6], "custody_fee": r[7],
                    "fund_size": r[8], "source": r[9],
                    "as_of": str(r[10]) if r[10] else None})
    return out


@router.get("/meta")
def etf_meta(
    q: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=200, le=1000),
) -> list[dict]:
    """ETF 元数据列表（代码/跟踪指数/规模/申赎 T+N）。q 过滤代码或名称前缀。"""
    return _meta_rows(q=q, limit=limit)


@router.get("/by-symbol/{symbol}")
def meta_by_symbol(symbol: str) -> dict:
    """单只 ETF 元数据（个股 ETF 详情页用），无则 404。"""
    from lquant.server.deps import bare_code

    code = bare_code(symbol).upper()
    rows = _meta_rows(symbol=code)          # 代码前缀精确查，不受默认 200 行限制
    hit = next((r for r in rows if bare_code(r["symbol"]).upper() == code), None)
    if hit is None:
        raise HTTPException(404, f"无 {symbol} 的 ETF 元数据（先同步 ETF 元数据）")
    return hit


@router.get("/correlation")
def correlation(
    horizon: int = Query(default=60, ge=30, le=500),
    min_symbols: int = Query(default=2, ge=2, le=100),
) -> dict:
    """ETF 日收益率相关性矩阵。样本不足返回空 pairs + 说明，不 500。"""
    from lquant.data.store.parquet import read_daily

    try:
        df = (read_daily()
              .select(["trade_date", "symbol", "close", "pre_close"])
              .collect())
    except Exception:  # noqa: BLE001 - 湖空/路径缺失
        df = None
    if df is None or not len(df):
        return {"symbols": [], "pairs": [], "note": "数据湖无行情，先同步日线"}
    from lquant.server.deps import bare_code

    def is_etf(sym: str) -> bool:
        c = bare_code(sym)
        # 510/511/512/513/515/516/518 沪 ETF、159 深 ETF、560 深创新 ETF
        return c.startswith(("510", "511", "512", "513", "515", "516", "518",
                             "159", "560"))

    # 先筛到 ETF 再 pivot：避免 O(dates × 全市场) 大宽表。
    etf_syms = sorted(s for s in df["symbol"].unique().to_list() if is_etf(s))
    if len(etf_syms) < min_symbols:
        return {"symbols": etf_syms, "pairs": [],
                "note": f"仅 {len(etf_syms)} 只 ETF 有日线，需 ≥ {min_symbols} 只计算相关"}
    from polars import col

    # pivot 保留首现顺序而非时间序 → 先 sort("trade_date") 再 tail 才是「最近 N 日」。
    ef = (df.filter(col("pre_close") > 0, col("close") > 0)   # close<=0 → pct_change 出 inf
          .filter(col("symbol").is_in(etf_syms)).sort("trade_date"))
    px = ef.pivot(values="close", index="trade_date", on="symbol").tail(horizon)
    cols = [c for c in px.columns if c != "trade_date"]        # 全为 ETF，顺序=pivot 列序
    # polars 1.4x 无 DataFrame.pct_change（仅 Expr）—— 用 select 逐列计算日收益率；
    # corr().row(i)[j] == corr(cols[i], cols[j])，三角展开保持 cols 序。
    ret = px.select([col(c).pct_change() for c in cols]).drop_nulls()
    if len(ret) < horizon // 2:
        return {"symbols": cols, "pairs": [],
                "note": f"有效收益率仅 {len(ret)} 根，不足以计算"}
    import math

    corr = ret.corr()                       # polars DataFrame 相关
    pairs = []
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            v = corr.row(i)[j]
            # 零方差/停牌 → NaN，is not None 挡不住，须 isfinite 否则裸 NaN 破坏 JSON。
            if v is not None and math.isfinite(v):
                pairs.append({"a": cols[i], "b": cols[j], "corr": round(float(v), 4)})
    pairs.sort(key=lambda p: -p["corr"])
    return {"symbols": cols, "pairs": pairs, "note": None}


# ─────────────────────── ETF 因子子域（池/因子/评价/回测）───────────────────────


def _clean_json(obj):
    """NaN/Inf → None：封套最终会 json 序列化，裸 NaN 会让客户端解析失败。"""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _clean_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean_json(v) for v in obj]
    return obj


def _check_etf_universe(v: str) -> str:
    """ETF 子域只接受 ETF 池名 —— 个股池/指数量/净值量混进来一定是误用。"""
    from lquant.factors.etf import is_etf_universe

    if not is_etf_universe(v):
        from lquant.factors.etf import ETF_UNIVERSE_OPTIONS

        allowed = "/".join(k for k, _, _ in ETF_UNIVERSE_OPTIONS)
        raise ValueError(f"universe 需为 ETF 池（{allowed}），收到 {v!r}")
    return v


@router.get("/universes")
def etf_universes() -> list[dict]:
    """ETF 标的池选项（按基金类型细分）。"""
    from lquant.factors.etf import etf_universe_options

    return [{"key": k, "fund_type": ft, "label": label}
            for k, ft, label in etf_universe_options()]


@router.get("/factors")
def etf_factors() -> list[dict]:
    """ETF 因子清单（config/factors/etf.yaml，逐条过 ETF 字段白名单校验）。"""
    from lquant.factors.etf import load_etf_factors

    return [
        {"name": r["name"], "expression": r["expression"], "category": r["category"],
         "description": r["description"], "min_window": r["min_window"]}
        for r in load_etf_factors()
    ]


class EtfEvaluateIn(BaseModel):
    factor: str = Field(min_length=1, max_length=200, description="因子 DSL 表达式")
    universe: str = Field(default="etf_all", description="ETF 池（etf_all/etf_equity/…）")
    start: str | None = Field(default=None, description="数据窗口起点 YYYY-MM-DD")
    end: str | None = Field(default=None, description="数据窗口终点 YYYY-MM-DD")
    n_groups: int = Field(default=DEFAULT_N_GROUPS, ge=2, le=20)
    with_robustness: bool = Field(default=False, description="是否跑 L3 稳健性（较慢）")

    @field_validator("universe")
    @classmethod
    def _v_universe(cls, v: str) -> str:
        return _check_etf_universe(v)


@router.post("/evaluate")
def etf_evaluate(req: EtfEvaluateIn) -> dict:
    """ETF 因子评价：IC/ICIR + 分层 + 衰减 + 归因 + 评级（ETF 口径）。

    数据为空 → 503（缺数据，不是请求非法）；表达式非法 → 422。
    """
    from lquant.core.errors import FactorError
    from lquant.factors.etf import evaluate_etf

    try:
        res = evaluate_etf(
            req.factor, universe=req.universe, start=req.start, end=req.end,
            n_groups=req.n_groups, with_report=False,
            with_robustness=req.with_robustness,
        )
    except ValueError as e:      # 面板为空 / train 段为空 → 数据问题
        raise HTTPException(503, f"ETF 因子评价失败（数据不足）: {e}") from e
    except FactorError as e:     # 表达式/字段非法 → 请求问题
        raise HTTPException(422, f"因子表达式非法: {e}") from e
    res.pop("report", None)
    return _clean_json(res)


class EtfBacktestIn(BaseModel):
    factor: str = Field(min_length=1, max_length=200, description="因子 DSL 表达式")
    universe: str = Field(default="etf_all", description="ETF 池")
    start: str | None = None
    end: str | None = None
    top_n: int = Field(default=10, ge=1, le=200)
    rebalance: str = Field(default="monthly", pattern="^(daily|weekly|monthly|none)$")
    initial_cash: float = Field(default=1_000_000.0, gt=0, le=1e15)
    benchmark: str | None = None
    exit_strategy: str | None = None
    exit_params: dict = Field(default_factory=dict)

    @field_validator("universe")
    @classmethod
    def _v_universe(cls, v: str) -> str:
        return _check_etf_universe(v)


@router.post("/backtest")
def etf_backtest(req: EtfBacktestIn) -> dict:
    """ETF 因子 TopN 等权回测（自动套用 ETF 交易规则），落库并返回 run_id。"""
    import uuid

    from lquant.backtest.etf import etf_factor_backtest
    from lquant.core.errors import FactorError
    from lquant.server.api.backtests import _metrics_response, _persist_result

    try:
        res, meta = etf_factor_backtest(
            req.factor, universe=req.universe, start=req.start, end=req.end,
            top_n=req.top_n, rebalance=req.rebalance, initial_cash=req.initial_cash,
            benchmark=req.benchmark, exit_strategy=req.exit_strategy,
            exit_params=req.exit_params,
        )
    except ValueError as e:
        raise HTTPException(503, f"ETF 回测失败（数据不足）: {e}") from e
    except KeyError as e:        # 未注册的退出策略名
        raise HTTPException(422, f"未注册的退出策略: {e}") from e
    except (TypeError, FactorError) as e:
        raise HTTPException(422, f"因子表达式或参数非法: {e}") from e

    run_id = uuid.uuid4().hex[:12]
    _persist_result(run_id, "etf_factor_topn", meta, res)
    return {
        "run_id": run_id,
        "metrics": _metrics_response(res.metrics),
        "n_nav_points": len(res.nav),
        "n_trades": res.metrics.get("n_trades", 0),
        "universe": meta["universe"],
        "universe_note": meta.get("universe_note"),
        "n_symbols": meta["n_symbols"],
    }