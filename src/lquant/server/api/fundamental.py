"""基本面服务：行业相对分位评分 + 三表勾稽。

设计约定：

- **PIT 优先**：所有取数强制 ``pub_date <= asof``（见 ``lquant.fundamental.panel``）。
  公告日之前查不到就是查不到，绝不返回「最新一期」充数 —— 回测里的前视偏差
  绝大多数就是从这种「顺手取最新」来的。
- **空表是常态不是错误**：财务表没同步过时返回空结构 + ``hint``，
  而不是 500。前端据此提示「先同步财务数据」。
- **分位必须用全市场截面算**：即使只查一只票，也要先把全表喂给分位计算，
  再筛出目标票 —— 否则「行业分位」会退化成「自己跟自己比」。
- **三种取数来源必须分别报告可用性**：报表明细（``financial_pit``）、
  派生比率、日线估值列是三个独立的数据源，任一缺失都会让某些模块恒为 0 分。
  把它们混成一个「无数据」hint 会让用户去修错的东西（历史事故：17 个指标
  键名与库里真实键完全对不上，页面却提示「财务数据为空，请先回填」，
  用户反复重跑回填而问题在代码里）。

性能约定：见 :func:`_build_panel` 的报告期截断与 :data:`_PANEL_CACHE`。
"""
from __future__ import annotations

import threading
import time
from datetime import date

import polars as pl
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from lquant.core.db import reader
from lquant.core.types import today_cn
from lquant.fundamental import (
    METRICS,
    MODULE_LABELS,
    MODULE_WEIGHTS,
    aggregate,
    derive_pit,
    metrics_by_module,
    module_coverage,
    pit_items,
    raw_items,
    reconcile,
    score_universe,
    valuation_long,
)

router = APIRouter(prefix="/fundamental", tags=["fundamental"])

#: 需要从 ``financial_pit`` 读的物理键 = 直接指标 + 派生指标的输入
_PIT_ITEMS: tuple[str, ...] = tuple(dict.fromkeys([*pit_items(), *raw_items()]))

#: 财务表没数据时的统一提示（前端据此引导用户去同步）
_EMPTY_HINT = "财务数据为空：先执行 `lq data financial --symbols ...` 回填 PIT 财务"
_VALUATION_HINT = (
    "日线估值列（pe_ttm/pb_mrq/dv_ttm）为空：估值模块 20 分不可用。"
    "先回填日线，再执行 `lq data daily-basic` 补估值列"
)

#: 只看观察日前 N 年内的报告期。
#:
#: 取数的目的只有两个：(a) 每个指标的**最新**值；(b) 派生比率需要的
#: **同一报告期**的分子分母。原实现把全部历史（2008 年起约 563 万行）
#: 拉进进程内再 reduce，单次请求 15s 以上。
#:
#: 5 年 ≈ 20 个报告期，远超「找到最新一期 + 派生所需的同期输入」所需。
#: 实测 3 年 / 5 年 / 8 年的取数耗时是 5.62s / 5.71s / 8.33s，而 5 年
#: 比 3 年多救回一批「某个指标只在更早报告期有值」的标的（最低覆盖率
#: 0.47 → 0.59）—— 所以取 5 年这个拐点。
#:
#: 它同时是一道**陈旧性闸门**：一家最新报告还是五年前的公司，拿它的 ROE
#: 去和当期披露的公司比行业分位，比出来的是垃圾 —— 宁可不给分。
_LOOKBACK_YEARS = 5


def _load_financial(con, asof: date, items: tuple[str, ...],
                    lookback_years: int = _LOOKBACK_YEARS) -> pl.DataFrame:
    """读取 ``pub_date <= asof`` 且报告期在近 ``lookback_years`` 年内的 PIT 长表。

    两个下推都发生在 SQL 里，而不是拉全量再到 Python 侧砍：

    - ``stat_date >= asof - N 年``：直接少读，563 万行 → 约 98 万行
      （实测 6.5s → 1.8s）；
    - ``arg_max(value, pub_date) GROUP BY (symbol, stat_date, item)``：
      用**哈希聚合**而不是窗口函数做「同报告期取最新修订」。窗口函数要对
      全表排序，实测 28s；哈希聚合 1.8s，结果等价。
    """
    placeholders = ",".join("?" * len(items))
    # 闰年 2/29 往前推 N 年落在平年会 ValueError（2/29 不存在）→ 接口 500；
    # 回退到 2/28（对 5 年回看窗口，差一天不改变「陈旧性闸门」的语义）
    try:
        start = date(asof.year - lookback_years, asof.month, asof.day)
    except ValueError:
        start = date(asof.year - lookback_years, asof.month, 28)
    sql = (
        "SELECT symbol, stat_date, item, max(pub_date) AS pub_date,"
        "       arg_max(value, pub_date) AS value"
        f"  FROM financial_pit WHERE pub_date <= ? AND item IN ({placeholders})"
        "   AND stat_date >= ?"
        "  GROUP BY symbol, stat_date, item"
    )
    try:
        out = con.execute(sql, [asof, *items, start]).pl()
    except Exception:  # noqa: BLE001 - 表未建/未同步：返回空而不是 500
        return pl.DataFrame(schema={"symbol": pl.String, "stat_date": pl.Date,
                                    "pub_date": pl.Date, "item": pl.String,
                                    "value": pl.Float64})
    # 统一列序：pl.concat(how="vertical_relaxed") 要求各帧列序一致，
    # 而 SQL 的 SELECT 顺序容易在改动里漂移（曾经因此整个接口 500）。
    return out.select(["symbol", "stat_date", "pub_date", "item", "value"])


def _load_industry(con, asof: date) -> pl.DataFrame:
    sql = ("SELECT symbol, std, code, name, std_date FROM industry_classify "
           "WHERE std_date <= ?")
    try:
        return con.execute(sql, [asof]).pl()
    except Exception:  # noqa: BLE001
        return pl.DataFrame()


# --------------------------------------------------------------------------
# 面板缓存
# --------------------------------------------------------------------------
# 评分面板的代价几乎全在「读 560 万行 + 派生 + 分位」上，而这些结果在同一天
# 内是稳定的（财报按季更新，估值按日更新）。前端每改一次筛选就重发一次请求，
# 没有缓存时每次都是 15s+。缓存按 (asof, min_samples) 键控，TTL 兜住盘中
# 估值变化；条目数有上限，防止按 asof 遍历把内存吃光。
_CACHE_TTL_SECONDS = 300.0
_CACHE_MAX_ENTRIES = 12
_PANEL_CACHE: dict[tuple, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()


def _cache_get(key: tuple):
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _PANEL_CACHE.get(key)
        if hit is None:
            return None
        stamp, value = hit
        if now - stamp > _CACHE_TTL_SECONDS:
            _PANEL_CACHE.pop(key, None)
            return None
        return value


def _cache_put(key: tuple, value) -> None:
    with _CACHE_LOCK:
        _PANEL_CACHE[key] = (time.monotonic(), value)
        if len(_PANEL_CACHE) > _CACHE_MAX_ENTRIES:
            oldest = min(_PANEL_CACHE, key=lambda k: _PANEL_CACHE[k][0])
            _PANEL_CACHE.pop(oldest, None)


def clear_fundamental_cache() -> None:
    """清空面板缓存（数据同步完成后调用，避免继续用 TTL 内的旧面板）。"""
    with _CACHE_LOCK:
        _PANEL_CACHE.clear()


class PanelBundle:
    """一次评分所需的全部输入 + 可用性诊断。"""

    __slots__ = ("detail", "snapshot", "asof", "availability")

    def __init__(self, detail: pl.DataFrame, snapshot: pl.DataFrame,
                 asof: date, availability: dict) -> None:
        self.detail = detail
        self.snapshot = snapshot
        self.asof = asof
        self.availability = availability


def _data_availability(asof: date, financial_rows: int,
                       valuation_rows: int) -> dict:
    """三个数据源各自的可用性 —— 前端据此给出**正确**的修复指引。"""
    from lquant.data.store.parquet import lake_is_empty

    try:
        daily_empty, basic_empty = lake_is_empty("daily"), lake_is_empty("daily_basic")
    except Exception:  # noqa: BLE001 - 湖目录不可读不该让接口 500
        daily_empty = basic_empty = True
    return {
        "financial_pit": {
            "available": financial_rows > 0,
            "rows": financial_rows,
            "lookback_years": _LOOKBACK_YEARS,
            "hint": None if financial_rows > 0 else _EMPTY_HINT,
        },
        "valuation_lake": {
            "available": valuation_rows > 0,
            "rows": valuation_rows,
            "hint": None if valuation_rows > 0 else _VALUATION_HINT,
            "daily_empty": daily_empty,
            "daily_basic_empty": basic_empty,
        },
    }


def _build_panel(asof: date, min_samples: int) -> PanelBundle:
    """取数 → 派生 → 估值 → 分位 → 聚合（带缓存）。

    **不做股票池过滤**：行业分位必须由全市场截面算出，只喂目标票会让
    「行业相对位置」退化成「自己跟自己比」。调用方拿到结果后再筛，
    见 :func:`score_many` / :func:`score_one`。
    """
    key = (asof, min_samples)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    with reader() as con:
        pit = _load_financial(con, asof, _PIT_ITEMS)
        industry = _load_industry(con, asof)

    derived = derive_pit(pit)
    valuation = valuation_long(asof)

    financial_rows = pit.height
    valuation_rows = valuation.height

    frames = [f for f in (pit, derived, valuation) if f.height]
    if not frames or industry.is_empty():
        bundle = PanelBundle(
            pl.DataFrame(), pl.DataFrame(), asof,
            _data_availability(asof, financial_rows, valuation_rows))
        _cache_put(key, bundle)
        return bundle

    panel = pl.concat(frames, how="vertical_relaxed")
    detail = score_universe(panel, industry, asof, METRICS, min_samples)
    snapshot = aggregate(detail, METRICS)
    bundle = PanelBundle(detail, snapshot, asof,
                         _data_availability(asof, financial_rows, valuation_rows))
    _cache_put(key, bundle)
    return bundle


def _json_safe(obj):
    """NaN/Inf → None（前端 JSON.parse 不接受字面量 NaN）。"""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, bool) or obj is None or isinstance(obj, str):
        return obj
    if hasattr(obj, "item") and not isinstance(obj, (int, float)):
        try:
            return _json_safe(obj.item())
        except Exception:  # noqa: BLE001
            return str(obj)
    if isinstance(obj, float):
        import math
        return obj if math.isfinite(obj) else None
    return obj


@router.get("/metrics")
def list_metrics() -> dict:
    """指标目录 + 模块权重 + 每个指标的取数来源（前端据此渲染分组与满分）。"""
    per_mod = metrics_by_module()
    return {
        "modules": [
            {
                "name": k,
                "label": MODULE_LABELS[k],
                "weight": v,
                "n_metrics": len(per_mod[k]),
            }
            for k, v in MODULE_WEIGHTS.items()
        ],
        "metrics": [
            {
                "item": m.item,
                "label": m.label,
                "module": m.module,
                "max_score": m.max_score,
                "higher_better": m.higher_better,
                "direction": m.direction,
                "source": m.source,
            }
            for m in METRICS
        ],
        "sources": {
            "pit": "financial_pit 报表科目",
            "derived": "由原始报表科目现算",
            "valuation": "日线湖估值列",
        },
    }


@router.get("/industries")
def list_industries(
    asof: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    min_samples: int = Query(default=5, ge=2, le=100),
) -> dict:
    """观察日可用的行业清单（前端行业筛选器的选项来源）。

    只列**真正有标的**的行业，而不是库里的全部行业 —— 否则用户会选中一个
    空行业然后看到「无结果」，误以为是筛选逻辑坏了。

    ``min_samples`` 必须与排名请求用同一个值：两个面板若用不同阈值评分，
    筛选器里列出的行业会出现「点进去一只票都没有」的错配。
    """
    day = date.fromisoformat(asof) if asof else today_cn()
    bundle = _build_panel(day, min_samples)
    if bundle.snapshot.is_empty():
        return {"asof": day.isoformat(), "available": False, "rows": [],
                "availability": bundle.availability}
    rows = (bundle.snapshot.filter(pl.col("industry").is_not_null())
            .group_by("industry")
            .agg(pl.len().alias("n"),
                 pl.col("normalized_score").mean().round(2).alias("avg_score"))
            .sort("n", descending=True))
    return _json_safe({"asof": day.isoformat(), "available": True,
                       "rows": rows.to_dicts(),
                       "availability": bundle.availability})


@router.get("/score")
def score_one(
    symbol: str = Query(min_length=6, max_length=16),
    asof: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    min_samples: int = Query(default=5, ge=2, le=100),
) -> dict:
    """单只股票的基本面评分（含逐指标明细与行业分位）。"""
    from lquant.server.deps import resolve_symbol

    day = date.fromisoformat(asof) if asof else today_cn()
    sym = resolve_symbol(symbol)
    bundle = _build_panel(day, min_samples)
    base = {"symbol": sym, "asof": day.isoformat(),
            "availability": bundle.availability}

    if bundle.snapshot.is_empty():
        return _json_safe({**base, "available": False,
                           "hint": _unavailable_hint(bundle),
                           "score": None, "items": [], "modules": []})

    mine = bundle.snapshot.filter(pl.col("symbol") == sym)
    if mine.is_empty():
        return _json_safe({**base, "available": False,
                           "hint": "该标的没有财务数据，或未被行业分类覆盖",
                           "score": None, "items": [], "modules": []})

    row = mine.row(0, named=True)
    items = bundle.detail.filter(pl.col("symbol") == sym).sort(["module", "item"])
    item_rows = items.to_dicts()
    # 逐指标的报告期 / 公告日（PIT 透明度的关键：让用户看到每个数出自哪一期）
    for d in item_rows:
        d["source"] = _source_of(d["item"])
    return _json_safe({
        **base,
        "available": True,
        "score": row,
        "items": item_rows,
        "modules": module_coverage(bundle.detail),
        "valuation": _valuation_of(day, sym),
    })


class UniverseIn(BaseModel):
    asof: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    symbols: list[str] = Field(default_factory=list, max_length=2000)
    industries: list[str] = Field(default_factory=list, max_length=200)
    min_samples: int = Field(default=5, ge=2, le=100)
    min_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    limit: int = Field(default=100, ge=1, le=5000)
    sort_by: str = Field(default="normalized_score")
    sort_desc: bool = True


_SORTABLE: frozenset[str] = frozenset(
    {"symbol", "industry", "normalized_score", "coverage", "raw_score",
     "available_max", "n_scored"}
    | {f"score_{m}" for m in MODULE_WEIGHTS}
)


def _unavailable_hint(bundle: PanelBundle) -> str:
    """没有结果时给**可操作**的提示，而不是笼统的「暂无数据」。"""
    fin = bundle.availability["financial_pit"]
    if not fin["available"]:
        return fin["hint"] or _EMPTY_HINT
    val = bundle.availability["valuation_lake"]
    if not val["available"]:
        return ("财务表已就绪，但行业分类或分位样本不足；"
                f"另外{val['hint']}")
    return "该观察日无可用财务数据（或行业分位样本不足）"


@router.post("/scores")
def score_many(req: UniverseIn) -> dict:
    """全市场（或指定股票池/行业）基本面评分排名。

    ``min_coverage`` 用来剔除「只匹配到两三个指标就拿了高分」的票 ——
    归一化分数跨覆盖度可比，但低覆盖度的高分不具备可比的可信度。
    """
    day = date.fromisoformat(req.asof) if req.asof else today_cn()
    if req.sort_by not in _SORTABLE:
        raise HTTPException(422, f"sort_by 不支持 {req.sort_by!r}，可选 {sorted(_SORTABLE)}")

    symbols = [s.strip().upper() for s in req.symbols if s.strip()] or None
    bundle = _build_panel(day, req.min_samples)
    base = {"asof": day.isoformat(), "availability": bundle.availability}
    if bundle.snapshot.is_empty():
        return _json_safe({**base, "available": False,
                           "hint": _unavailable_hint(bundle),
                           "n_scored": 0, "n_total": 0, "rows": [],
                           "modules": module_coverage(pl.DataFrame())})

    snap = bundle.snapshot
    if symbols:
        snap = snap.filter(pl.col("symbol").is_in(symbols))
    if req.industries:
        wanted = {s.strip() for s in req.industries if s.strip()}
        snap = snap.filter(pl.col("industry").is_in(list(wanted)))
    n_total = snap.height
    snap = snap.filter(pl.col("coverage") >= req.min_coverage)
    snap = snap.sort(req.sort_by, descending=req.sort_desc,
                     nulls_last=True).head(req.limit)

    # 明细只在筛选后的票上算模块命中率，避免「全市场统计」与看到的表不符
    kept = set(snap["symbol"].to_list())
    detail = (bundle.detail.filter(pl.col("symbol").is_in(list(kept)))
              if kept else bundle.detail.head(0))
    return _json_safe({
        **base,
        "available": True,
        "n_scored": snap.height,
        "n_total": n_total,
        "rows": snap.to_dicts(),
        "modules": module_coverage(detail),
    })


@router.get("/percentiles")
def percentiles(
    asof: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    min_samples: int = Query(default=5, ge=2, le=100),
    industry: str | None = Query(default=None),
) -> dict:
    """各行业 × 各指标的 P25/P50/P75 分位表（用于解释某个分数是怎么来的）。"""
    day = date.fromisoformat(asof) if asof else today_cn()
    bundle = _build_panel(day, min_samples)
    if bundle.detail.is_empty():
        return _json_safe({"asof": day.isoformat(), "available": False,
                           "hint": _unavailable_hint(bundle), "rows": [],
                           "availability": bundle.availability})

    rows = (bundle.detail.group_by(["industry", "item"])
            .agg(pl.col("p25").first(), pl.col("p50").first(),
                 pl.col("p75").first(), pl.col("n").first(),
                 pl.col("label").first(), pl.col("module").first())
            .sort(["industry", "item"]))
    if industry:
        rows = rows.filter(pl.col("industry") == industry)
    return _json_safe({"asof": day.isoformat(), "available": not rows.is_empty(),
                       "rows": rows.to_dicts(),
                       "availability": bundle.availability})


def _source_of(item: str) -> str:
    for m in METRICS:
        if m.item == item:
            return m.source
    return "pit"


def _valuation_of(day: date, symbol: str) -> dict | None:
    from lquant.fundamental import valuation_frame

    try:
        frame = valuation_frame(day, [symbol])
    except Exception:  # noqa: BLE001 - 估值缺失不该让评分接口 500
        return None
    if frame.is_empty() or symbol not in frame["symbol"].to_list():
        return None
    row = frame.filter(pl.col("symbol") == symbol).row(0, named=True)
    return {k: v for k, v in row.items() if k != "symbol"}


class ReconcileIn(BaseModel):
    symbol: str = Field(min_length=6, max_length=16)
    asof: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    auto: bool = Field(
        default=True,
        description="缺项时自动从 financial_pit 取最近两期报表；显式传入的字段优先",
    )
    net_income: float | None = None
    other_comprehensive: float | None = None
    delta_retained: float | None = None
    operating_cashflow: float | None = None
    cashflow_net_change: float | None = None
    balance_cash_change: float | None = None
    deducted_net_income: float | None = None


#: 勾稽项 → financial_pit 物理键。带 Δ 的两项需要最近两期才能算变动。
_RECONCILE_ITEMS: dict[str, str] = {
    "net_income": "income.n_income_attr_p",
    "other_comprehensive": "income.compr_inc_attr_p",
    "operating_cashflow": "cashflow.n_cashflow_act",
    "cashflow_net_change": "cashflow.n_incr_cash_cash_equ",
    "deducted_net_income": "indicator.profit_dedt",
    "retained": "balancesheet.undistr_porfit",
    "cash": "balancesheet.money_cap",
}

#: 需要「本期 − 上期」的项：来源键 → 目标字段名
_RECONCILE_DELTAS: dict[str, str] = {
    "retained": "delta_retained",
    "cash": "balance_cash_change",
}

#: 报告期内**累计**（年初至今）的科目。
#: 利润表与现金流量表的科目是累计口径（Q2 = 上半年累计），资产负债表是时点值。
#: 勾稽要求两边覆盖**同一个期间**，所以累计口径的科目必须先单季化
#: （本期累计 − 上期累计），否则会拿「上半年净利润」去比「二季度留存收益变动」,
#: 差额巨大但完全没有意义 —— 正是本模块最该避免的「看起来很专业的垃圾」。
_RECONCILE_CUMULATIVE: frozenset[str] = frozenset({
    "net_income", "other_comprehensive", "operating_cashflow",
    "cashflow_net_change", "deducted_net_income",
})


def _adjacent_period(cur: date, prev: date) -> bool:
    """prev 是否 cur 的**上一个报告期**（同年前一季，或 Q1 的上年 Q4）。

    勾稽的单季化与变动都假设两期相邻。中间缺一期（某季报漏采/未披露）
    时拿「相隔两季的累计值」相减，得到的差额被当成单季参与勾稽，
    会凭空制造出巨额勾稽差异 —— 必须显式校验。
    """
    if cur.year == prev.year:
        return (cur.month - prev.month) == 3
    return cur.year == prev.year + 1 and cur.month == 3 and prev.month == 12


def _quarterly_value(field: str, cur: float | None, prev: float | None,
                     cur_period: date, prev_period: date | None) -> float | None:
    """把累计口径科目折算成**当期单期**值；时点科目原样返回。"""
    if cur is None:
        return None
    if field not in _RECONCILE_CUMULATIVE:
        return cur
    if prev is None or prev_period is None:
        # 没有上期：只有 Q1 的累计值恰好等于单季值，其余期间无法折算
        return cur if cur_period.month == 3 else None
    if prev_period.year == cur_period.year:
        return cur - prev
    # 跨年说明本期就是 Q1，累计即单季
    return cur if cur_period.month == 3 else None


def _auto_reconcile_inputs(con, symbol: str, asof: date) -> tuple[dict, dict]:
    """从 ``financial_pit`` 取最近两期报表，算出勾稽需要的输入。

    Returns:
        ``(inputs, meta)``。取不到的项**不出现在 ``inputs`` 里** ——
        会被 :func:`reconcile` 记成「未检查」，而不是当成 0 参与判定。
    """
    keys = tuple(_RECONCILE_ITEMS.values())
    placeholders = ",".join("?" * len(keys))
    sql = (
        "SELECT stat_date, item, max(pub_date) AS pub_date,"
        "       arg_max(value, pub_date) AS value"
        "  FROM financial_pit"
        " WHERE symbol = ? AND pub_date <= ?"
        f"   AND item IN ({placeholders})"
        " GROUP BY stat_date, item"
        " ORDER BY stat_date DESC"
    )
    try:
        rows = con.execute(sql, [symbol, asof, *keys]).pl()
    except Exception:  # noqa: BLE001 - 表未建/未同步：全部记未检查
        return {}, {"auto_error": "financial_pit 不可读"}
    if rows.is_empty():
        return {}, {"auto_error": "该标的在观察日前没有可用的报表数据"}

    periods = sorted(set(rows["stat_date"].to_list()), reverse=True)
    cur_period = periods[0]
    # 上期必须与本期相邻才可用于单季化/求变动；缺期时降级为「无上期」
    # （Q1 累计即单季仍可勾稽，其余期该字段记未检查），绝不用隔季数据硬算
    prev_period = (periods[1] if len(periods) > 1
                   and _adjacent_period(periods[0], periods[1]) else None)
    latest = rows.filter(pl.col("stat_date") == cur_period)
    by_item = dict(zip(latest["item"].to_list(), latest["value"].to_list(),
                       strict=True))
    prev_by_item: dict = {}
    if prev_period is not None:
        prev = rows.filter(pl.col("stat_date") == prev_period)
        prev_by_item = dict(zip(prev["item"].to_list(), prev["value"].to_list(),
                                strict=True))

    def raw(field: str) -> tuple[float | None, float | None]:
        key = _RECONCILE_ITEMS[field]
        cur = by_item.get(key)
        old = prev_by_item.get(key)
        return (float(cur) if cur is not None else None,
                float(old) if old is not None else None)

    inputs: dict = {}
    # 直接项：累计口径先单季化
    for field in _RECONCILE_ITEMS:
        if field in _RECONCILE_DELTAS:
            continue
        cur, old = raw(field)
        val = _quarterly_value(field, cur, old, cur_period, prev_period)
        if val is not None:
            inputs[field] = val
    # 变动项：资产负债表是时点值，直接相减即当期变动
    for field, target in _RECONCILE_DELTAS.items():
        cur, old = raw(field)
        if cur is not None and old is not None:
            inputs[target] = cur - old

    meta = {
        "latest_stat_date": cur_period.isoformat(),
        "previous_stat_date": prev_period.isoformat() if prev_period else None,
        "previous_adjacent": prev_period is not None,
        "period_basis": "quarterly",
        "filled": sorted(inputs),
    }
    return inputs, meta


@router.post("/reconcile")
def run_reconcile(req: ReconcileIn) -> dict:
    """三表勾稽校验（留存收益 / 现金变动 / 利润质量）。

    输入缺项一律记为「未检查」，**不计入通过判定也不给分** ——
    数据缺失不能被当成质量优秀。

    ``auto=True``（默认）时，未显式传入的字段自动从 ``financial_pit``
    最近两期报表取数。没有这个自动取数，调用方得自己把七个科目从库里
    手工查出来再填 —— 端点等于不可用（这正是它此前在前端零引用的原因）。
    显式传入的值优先。
    """
    from lquant.server.deps import resolve_symbol

    if not req.symbol.strip():
        raise HTTPException(422, "symbol 不能为空")
    sym = resolve_symbol(req.symbol)
    day = date.fromisoformat(req.asof) if req.asof else today_cn()

    explicit = {
        "net_income": req.net_income,
        "other_comprehensive": req.other_comprehensive,
        "delta_retained": req.delta_retained,
        "operating_cashflow": req.operating_cashflow,
        "cashflow_net_change": req.cashflow_net_change,
        "balance_cash_change": req.balance_cash_change,
        "deducted_net_income": req.deducted_net_income,
    }
    given = {k: v for k, v in explicit.items() if v is not None}
    merged = dict(given)
    meta: dict = {"asof": day.isoformat(), "auto": req.auto,
                  "from_request": sorted(given), "from_financial_pit": []}
    if req.auto:
        with reader() as con:
            auto_inputs, auto_meta = _auto_reconcile_inputs(con, sym, day)
        meta.update(auto_meta)
        for k, v in auto_inputs.items():
            if k not in merged:
                merged[k] = v
        meta["from_financial_pit"] = sorted(k for k in auto_inputs if k not in given)

    report = reconcile(sym, **merged)
    return _json_safe({**report.to_dict(), "symbol": sym, "inputs": merged,
                       "inputs_meta": meta})


__all__ = ["clear_fundamental_cache", "router"]
