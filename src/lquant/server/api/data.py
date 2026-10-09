"""数据服务：覆盖度 / 标的搜索 / 日线 / 实时行情。

设计约定：
- 「没数据」是常态而不是错误 —— 空湖、空表返回空结构，不抛 500；
- 实时行情走东财 push2（经 em_get 令牌桶限流），网络不通时降级为
  available=false，前端用日线最后一根兜底显示；
- 日线一律读 Parquet 湖（研究态事实源），DuckDB 只存小表。
"""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import date

import polars as pl
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from lquant.core.db import reader
from lquant.data.ingest import tasks as ingest_tasks
from lquant.data.ingest.reference import sync_reference
from lquant.data.ingest.tasks import (  # noqa: F401  execute_task/run_claimed_task 保留模块属性供测试打桩
    TaskConflictError,
    claim_retry,
    create_task,
    execute_task,
    get_task,
    list_tasks,
    run_claimed_task,
)
from lquant.data.store.parquet import read_daily
from lquant.server.deps import bare_code, resolve_symbol
from lquant.server.jobs import enqueue

router = APIRouter(prefix="/data", tags=["data"])

#: /data/indicators 的默认指标集 —— 与迁移前的输出保持一致（前端旧契约不破）
DEFAULT_INDICATORS = "ma,macd,rsi,boll"

# 全湖质量检查互斥锁：防并发双跑竞写 data_quality_issue
_lake_check_lock = threading.Lock()


class TaskIn(BaseModel):
    kind: str = Field(min_length=1, max_length=32)
    params: dict = Field(default_factory=dict)


class CrosscheckIn(BaseModel):
    start: str | None = Field(default=None, max_length=10)
    end: str | None = Field(default=None, max_length=10)
    peers: list[str] | None = None
    limit: int = Field(default=200, ge=1, le=1000)

    @field_validator("start", "end")
    @classmethod
    def _validate_date(cls, v: str | None) -> str | None:
        if v:
            try:
                date.fromisoformat(v)
            except (TypeError, ValueError):
                raise ValueError(f"日期需为 YYYY-MM-DD 格式，收到 {v!r}") from None
        return v


class ResolveIn(BaseModel):
    issue_id: str = Field(min_length=1, max_length=64)

# 覆盖度要看的表：名称 → (说明, 是否按 trade_date 取最新)
_COVER_TABLES: list[tuple[str, str]] = [
    ("security", "标的主档"),
    ("trade_calendar", "交易日历"),
    ("financial_pit", "财务 PIT"),
    ("factor_def", "因子定义"),
    ("limit_up_pool", "涨停池"),
    ("limit_down_pool", "跌停池"),
    ("money_flow", "个股资金流"),
    ("sector_daily", "板块日度"),
    ("sentiment_daily", "市场情绪"),
    ("dragon_tiger", "龙虎榜"),
    ("northbound_flow", "北向成交额"),
    ("northbound_top10", "北向前十大活跃"),
    ("backtest_run", "回测记录"),
    ("ml_run", "ML 实验记录"),
]


@router.get("/ping")
def ping() -> dict[str, str]:
    return {"pong": "data"}


@router.get("/coverage")
def coverage() -> dict:
    """数据覆盖度：湖 + 库一次看全，数据页/排障首屏。"""
    tables: list[dict] = []
    with reader() as con:
        existing = {r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables").fetchall()}
        for t, label in _COVER_TABLES:
            if t not in existing:
                tables.append({"table": t, "label": label, "rows": None, "latest": None})
                continue
            try:
                rows = con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
                latest = None
                cols = {r[0] for r in con.execute(f'DESCRIBE "{t}"').fetchall()}
                if rows and "trade_date" in cols:
                    latest = con.execute(
                        f'SELECT max(trade_date) FROM "{t}"').fetchone()[0]
                tables.append({"table": t, "label": label, "rows": int(rows),
                               "latest": str(latest) if latest else None})
            except Exception:  # noqa: BLE001 - 单表坏了不影响整页
                tables.append({"table": t, "label": label, "rows": None,
                               "latest": None, "error": True})

    lake: dict = {"rows": 0, "symbols": 0, "start": None, "end": None}
    try:
        df = read_daily().select(
            ["symbol", "trade_date"]).collect()
        if len(df):
            lake = {"rows": len(df), "symbols": df["symbol"].n_unique(),
                    "start": str(df["trade_date"].min()),
                    "end": str(df["trade_date"].max())}
    except Exception:  # noqa: BLE001
        lake["error"] = True
    return {"tables": tables, "daily_lake": lake}


@router.get("/coverage/monthly")
def coverage_monthly(
    start: str | None = Query(default=None, max_length=10),
    end: str | None = Query(default=None, max_length=10),
) -> dict:
    """覆盖度按月聚合：每月实际标的数趋势（spec §7.3）。

    口径：avg_symbols = 当月各交易日 distinct symbol 数的算术平均，
    days = 当月有数据的交易日数。「应有标的数」无可靠 PIT 来源
    （security 是当前快照），不做对比 —— 缺口由前端按环比大幅下降标橙，
    告警阈值 coverage_drop_warn_pct 走 settings（响应附 threshold 字段，
    前端不再硬编码 30）。空湖返回空数组不报错。
    """
    from lquant.core.settings_store import SettingsStore

    def _threshold() -> int:
        for item in SettingsStore().all():
            if item["key"] == "coverage_drop_warn_pct":
                return int(item["value"])
        return 30

    threshold = _threshold()
    from datetime import date as _date

    for name, val in (("start", start), ("end", end)):
        if isinstance(val, str) and val:
            try:
                _date.fromisoformat(val)
            except ValueError as e:
                raise HTTPException(422, f"{name} 日期非法: {val}") from e
    try:
        lf = read_daily(start=start, end=end).select(["symbol", "trade_date"])
        if not lf.collect_schema().names():
            return {"rows": []}
        per_day = lf.group_by("trade_date").agg(
            pl.col("symbol").n_unique().alias("n_symbols"))
        monthly = (
            per_day.group_by(
                pl.col("trade_date").dt.strftime("%Y-%m").alias("month"))
            .agg(pl.col("n_symbols").mean().alias("avg_symbols"),
                 pl.col("trade_date").len().alias("days"))
            .sort("month"))
        rows = [{"month": r["month"], "avg_symbols": round(r["avg_symbols"], 1),
                 "days": int(r["days"])} for r in monthly.collect().to_dicts()]
        return {"rows": rows, "threshold": threshold}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 - 湖读取异常降级为空，不打断首屏
        from loguru import logger

        logger.warning(f"coverage monthly 聚合失败: {e}")
        return {"rows": [], "threshold": threshold}


_DATASET_LABELS = {"daily": "日线湖", "daily_basic": "估值指标湖"}


def _gaps_report(days: int = 30) -> dict:
    """缺口报告：scan_coverage(repair=False) 的结果映射成前端友好结构。

    scan_coverage 同时把 COVERAGE_GAP issue 落库（与监控页 issues 共享），
    这里不需要 repair —— 补采由 /gaps/repair 显式触发。
    """
    from lquant.data.quality.coverage import scan_coverage

    rep = scan_coverage(days=days, repair=False)
    window = rep["window"]
    from lquant.data.store.catalog import TradeCalendarRepo

    trade_days = TradeCalendarRepo().range(
        date.fromisoformat(str(window["start"])),
        date.fromisoformat(str(window["end"])))
    datasets: list[dict] = []
    for ds, table in rep["tables"].items():
        missing = [d.isoformat() for d in table["missing_dates"]]
        sparse = table["sparse_symbols"]
        datasets.append({
            "dataset": ds,
            "label": _DATASET_LABELS.get(ds, ds),
            "expected_days": len(trade_days),
            "actual_days": len(trade_days) - len(missing),
            "missing": missing,
            "sparse_symbols": sparse,
            "sparse_total": len(sparse),
        })
    return {
        "window": {"start": str(window["start"]), "end": str(window["end"])},
        "datasets": datasets,
        "missing_dates": sorted(
            d for ds in datasets for d in ds["missing"]),
    }


@router.get("/gaps")
def get_gaps(days: int = Query(default=30, ge=1, le=400)) -> dict:
    """数据缺口：窗口内「日历有交易日、湖里没数据」的日期清单（按 dataset）。"""
    try:
        return _gaps_report(days)
    except Exception as e:  # noqa: BLE001 - 降级不炸首屏
        from loguru import logger

        logger.warning(f"数据缺口查询失败: {e}")
        raise HTTPException(502, f"缺口查询失败: {e}") from e


class GapsRepairIn(BaseModel):
    days: int = Field(default=30, ge=1, le=400)


@router.post("/gaps/repair", status_code=202)
def repair_gaps(req: GapsRepairIn = GapsRepairIn()) -> dict:
    """一键补采：对当前缺口建 daily_update 任务（start=最早缺口日）。

    有活跃数据任务时返回 created=false（不 500，前端提示稍后再试）。
    """
    rep = _gaps_report(req.days)
    # 只按日线湖缺口补采：daily_basic 空湖是常态（info 级），拿它的缺口
    # 当补采范围会把 start 拉到窗口最早期，回填一整窗
    daily = next((d for d in rep["datasets"] if d["dataset"] == "daily"), None)
    missing = daily["missing"] if daily else []
    if not missing:
        return {"created": False, "task_id": None, "reason": "no_gap"}
    try:
        from lquant.core.types import today_cn

        task = create_task("daily_update", {
            "start": missing[0],
            "end": today_cn().isoformat(),  # 与 scan_coverage 窗口同一时区口径
            "note": "manual_gap_repair",
            "auto_crosscheck": True,
        })
    except TaskConflictError as e:
        return {"created": False, "task_id": None,
                "reason": "active_task_exists", "message": str(e)}
    # 与 POST /data/tasks 一致：job_id = task_id，任务中心 cancel 靠它找取消目标；
    # 不 enqueue 执行器的话任务永远 pending —— PR#66 漏了这步，补采按钮实际不跑
    enqueue("lquant-ingest", ingest_tasks.execute_task, task["task_id"],
            job_id=task["task_id"])
    return {"created": True, "task_id": task["task_id"], "reason": None}


def _security_where(
    q: str = "",
    sec_type: str | None = None,
    board: str | None = None,
    include_delisted: bool = False,
) -> tuple[str, list]:
    """``security`` 表的 WHERE 片段与绑定参数。

    ``/securities``（搜索框）与 ``/securities/universe``（全量清单）共用同一套
    过滤口径 —— 各写一遍 SQL 迟早出现「搜索能搜到、列表筛不到」的不一致。
    代码/名称都走 ILIKE：用户输入 ``600519.sh`` 或 ``tcl`` 也应该命中。
    """
    clauses: list[str] = []
    params: list = []
    if not include_delisted:
        # 退市股默认不进清单；研究幸存者偏差时显式 include_delisted=true
        clauses.append("(delist_date IS NULL OR delist_date > current_date)")
    if q:
        clauses.append("(symbol ILIKE ? OR name ILIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if sec_type:
        clauses.append("sec_type = ?")
        params.append(sec_type)
    if board:
        clauses.append("board = ?")
        params.append(board)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


@router.get("/securities")
def securities(
    q: str = Query(default="", max_length=20, description="代码/名称关键字"),
    sec_type: str | None = Query(default=None, description="stock/etf/lof/index"),
    limit: int = Query(default=50, le=500),
) -> list[dict]:
    """标的搜索：代码前缀或名称包含，自选股/个股页搜索框用。"""
    where, params = _security_where(q=q, sec_type=sec_type)
    sql = ("SELECT symbol, name, sec_type, board, list_date, is_st FROM security"
           f"{where} ORDER BY symbol LIMIT {int(limit)}")
    try:
        with reader() as con:
            rows = con.execute(sql, params).fetchall()
    except Exception:  # noqa: BLE001
        return []
    return [{"symbol": r[0], "name": r[1], "sec_type": r[2], "board": r[3],
             "list_date": str(r[4]) if r[4] else None, "is_st": r[5]} for r in rows]


#: 全量清单单页上限。比搜索框的 500 小：这是给页面翻页用的，一次拉几千行
#: 只会让浏览器渲染大量不可见的行。
_UNIVERSE_MAX_LIMIT = 200


@router.get("/securities/universe")
def securities_universe(
    q: str = Query(default="", max_length=20, description="代码/名称关键字"),
    industry: str | None = Query(default=None, max_length=40,
                                 description="行业名称或代码（如 银行 / 801780.SI）"),
    sec_type: str | None = Query(default=None, description="stock/etf/lof/index"),
    board: str | None = Query(default=None, max_length=16,
                              description="main/gem/star/bse/unknown"),
    include_delisted: bool = Query(default=False, description="是否含已退市标的"),
    std: str | None = Query(default=None, max_length=16,
                            description="行业分类标准 SW/CICS/em，缺省自动挑"),
    limit: int = Query(default=50, ge=1, le=_UNIVERSE_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """全量标的清单：默认返回全部（分页），支持行业 / 代码 / 名称 / 类型 / 板块筛选。

    与 ``/securities`` 的分工：那个是搜索框的轻量关键字搜索，返回裸列表；这个是
    「全量清单」页面的取数口 —— 需要**总数**（翻页）、**行业归属**（列 + 下拉）
    和**分页**，所以返回 ``{items,total,limit,offset,industries,notes}`` 信封。

    行业归属走 PIT（``industry_classify.std_date <= asof``，每只取生效日最晚的
    一条），与行业分析 / 因子中性化同一口径。``industries`` 是**不随 q/类型/板块
    过滤收缩**的全量行业清单 —— 否则筛完一个行业后下拉框自己就空了。
    """
    from lquant.core.types import today_cn
    from lquant.industry.loader import (
        load_membership,
        resolve_asof,
        resolve_industry,
        resolve_std,
    )

    def _envelope(**extra) -> dict:
        return {"asof": None, "std": None, "total": 0, "limit": limit,
                "offset": offset, "items": [], "industries": [],
                "notes": [], **extra}

    where, params = _security_where(q=q, sec_type=sec_type, board=board,
                                    include_delisted=include_delisted)
    sql = ("SELECT symbol, name, sec_type, board, list_date, is_st FROM security"
           f"{where} ORDER BY symbol")
    try:
        with reader() as con:
            sec = con.execute(sql, params).pl()
            try:
                day = resolve_asof(None)
            except Exception:  # noqa: BLE001 - 日历/湖缺失时退回自然日
                day = today_cn()
            used_std, fell_back = resolve_std(con, day, std)
            # used_std=None 表示没有可用的分类标准：此时不能再按 std=None 读
            # membership（那会退化成「不过滤 std」，把 std 为空的行也当归属用）
            mem = (load_membership(con, day, used_std) if used_std is not None
                   else pl.DataFrame())
    except Exception as e:  # noqa: BLE001 - 空湖/空表是常态，不 500
        from loguru import logger

        logger.warning(f"全量标的清单读取失败（降级为空）: {e}")
        return _envelope(notes=[f"标的列表读取失败：{type(e).__name__}"])

    notes: list[str] = []
    if used_std is None:
        notes.append("行业分类数据为空（industry_classify）—— 行业列与行业筛选不可用，"
                     "先执行 `lq data reference` / `lq data industry`")
    elif fell_back:
        notes.append(f"请求的行业标准 {std!r} 不可用，已回退到 {used_std!r}")

    if mem.is_empty():
        sec = sec.with_columns(
            pl.lit(None, dtype=pl.String).alias("industry_code"),
            pl.lit(None, dtype=pl.String).alias("industry_name"),
        )
        industries: list[dict] = []
    else:
        # 显式再排一次：polars join 不承诺保持左表行序，而分页必须稳定
        sec = (sec.join(mem.select(["symbol", "industry_code", "industry_name"]),
                        on="symbol", how="left")
                  .sort("symbol"))
        industries = [
            {"code": r["industry_code"], "name": r["industry_name"],
             "n_members": int(r["n_members"])}
            for r in (mem.drop_nulls("industry_name")
                         .group_by(["industry_code", "industry_name"])
                         .agg(pl.len().alias("n_members"))
                         .sort(["n_members", "industry_code"],
                               descending=[True, False])
                         .to_dicts())
        ]

    if industry and industry.strip():
        key = industry.strip()
        try:
            with reader() as con:
                hit = resolve_industry(con, key, day, used_std)
        except Exception as e:  # noqa: BLE001 - 分类表缺失按无匹配处理
            from loguru import logger

            logger.warning(f"行业筛选解析失败（按无匹配处理）: {e}")
            hit = None
        if hit is None:
            return _envelope(asof=day.isoformat(), std=used_std,
                             industries=industries,
                             notes=[*notes, f"行业 {key!r} 未匹配到任何分类，列表为空"])
        sec = sec.filter(pl.col("industry_code") == hit[0])

    total = sec.height
    items = [
        {
            "symbol": r["symbol"],
            "name": r["name"],
            "sec_type": r["sec_type"],
            "board": r["board"],
            "list_date": str(r["list_date"]) if r["list_date"] else None,
            "is_st": r["is_st"],
            "industry": r["industry_name"],
            "industry_code": r["industry_code"],
        }
        for r in sec.slice(offset, limit).iter_rows(named=True)
    ]
    return {"asof": day.isoformat(), "std": used_std, "total": total,
            "limit": limit, "offset": offset, "items": items,
            "industries": industries, "notes": notes}


@router.get("/daily")
def daily(
    symbol: str = Query(min_length=6, max_length=12),
    start: str | None = None,
    end: str | None = None,
    limit: int = Query(default=250, le=2500),
) -> list[dict]:
    """个股日线（Parquet 湖）。K 线图与研究取数共用一个入口。"""
    sym = resolve_symbol(symbol)
    df = read_daily([sym], start=start, end=end).collect()
    if not len(df):
        return []
    cols = [c for c in ["trade_date", "open", "high", "low", "close",
                        "volume", "amount"] if c in df.columns]
    df = df.select(cols).sort("trade_date").tail(limit)
    out = df.to_dicts()
    for r in out:
        r["trade_date"] = str(r["trade_date"])
    return out


@router.get("/indicators/registry")
def indicators_registry() -> dict:
    """枚举已注册的技术指标（前端据此生成指标选择器，无需硬编码）。"""
    from lquant.indicators import INDICATORS

    return {"indicators": INDICATORS.describe(),
            "default": list(DEFAULT_INDICATORS.split(","))}


@router.get("/indicators")
def indicators(
    symbol: str = Query(min_length=6, max_length=16),
    limit: int = Query(default=120, le=500),
    names: str = Query(default=DEFAULT_INDICATORS,
                       description="逗号分隔的指标注册名，见 /data/indicators/registry"),
) -> list[dict]:
    """个股日线 + 指定技术指标。

    指标有 warmup 期，所以**先在全量历史上算再截尾** —— 返回的每行指标都由完整
    历史算出，而不是只喂 limit 根。未注册的名字返回 422 而不是静默忽略，
    否则前端会拿到一列不存在的字段而不知道哪里错了。
    """
    from lquant.indicators import INDICATORS, compute_many

    requested = [n.strip() for n in names.split(",") if n.strip()]
    if not requested:
        raise HTTPException(422, "names 不能为空")
    unknown = [n for n in requested if n not in INDICATORS]
    if unknown:
        raise HTTPException(422, f"未注册的指标: {unknown}；可选: {INDICATORS.keys()}")

    sym = resolve_symbol(symbol)
    df = read_daily([sym]).collect()
    if not len(df):
        return []
    cols = [c for c in ["trade_date", "open", "high", "low", "close",
                        "volume", "turnover_rate"] if c in df.columns]
    df = df.select(cols).sort("trade_date")
    df = compute_many(df, requested)
    out = df.tail(limit).to_dicts()
    for r in out:
        r["trade_date"] = str(r["trade_date"])
        for k, v in r.items():
            if isinstance(v, float):
                r[k] = round(v, 4)
    return out


# 东财 push2 字段：fltt=2/invt=2 直接返回浮点
_QT_URL = ("https://push2.eastmoney.com/api/qt/stock/get?"
           "secid={secid}&fltt=2&invt=2&fields="
           "f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f116,f169,f170")


def _secid(symbol: str) -> str:
    """`600519.SH` → `1.600519`（沪），`000001.SZ` → `0.000001`（深）。"""
    code = bare_code(symbol)
    if code.startswith(("6", "9", "5")):
        return f"1.{code}"
    return f"0.{code}"


@router.get("/quote")
def quote(symbol: str = Query(min_length=6, max_length=16)) -> dict:
    """实时行情（东财）。不可用时 available=false，前端兜底日线末价。"""
    sym = resolve_symbol(symbol)
    try:
        from lquant.market.em_client import em_get

        r = em_get(_QT_URL.format(secid=_secid(sym)), qps=3.0)
        data = r.json().get("data") or {}
    except Exception:  # noqa: BLE001 - 代理/断网都走这里
        return {"symbol": sym, "available": False}
    if not data.get("f57"):
        return {"symbol": sym, "available": False}
    return {
        "symbol": sym,
        "name": data.get("f58"),
        "available": True,
        "price": data.get("f43"),
        "change": data.get("f169"),
        "change_pct": data.get("f170"),
        "open": data.get("f46"),
        "high": data.get("f44"),
        "low": data.get("f45"),
        "prev_close": data.get("f60"),
        "volume": data.get("f47"),
        "amount": data.get("f48"),
        "turnover_rate": data.get("f50"),
        "market_cap": data.get("f116"),
    }


# ---------- 参考数据（标的清单含退市）同步 ----------

# 一次只允许一个参考数据同步在后台跑（详情慢路径要 20-40 分钟）
_ref_lock = threading.Lock()


class ReferenceSyncIn(BaseModel):
    sync_details: bool = Field(
        default=False,
        description="是否顺带逐只补 list_date/delist_date（慢路径，约 20-40 分钟）")


@router.post("/reference/sync", status_code=202)
def sync_reference_ep(req: ReferenceSyncIn) -> dict:
    """同步参考数据（日历/标的/退市名单，可选详情）→ 202 后台执行。

    409：上一次同步尚未结束。完成后 security 表含退市股，
    全量回填（full_backfill）的前置校验即可通过。
    """
    if not _ref_lock.acquire(blocking=False):
        raise HTTPException(409, "上一次参考数据同步尚未结束，请稍后再试")

    def _job() -> None:
        try:
            sync_reference(skip_details=not req.sync_details)
        except Exception as e:  # noqa: BLE001 - 后台任务失败只记日志
            from loguru import logger

            logger.exception(f"参考数据同步失败: {e}")
        finally:
            _ref_lock.release()

    enqueue("lquant-ingest", _job)
    return {"accepted": True, "sync_details": req.sync_details}


# ---------- 数据任务（全量回填 / 每日增量，T4 执行器） ----------

class IndexConsSyncIn(BaseModel):
    indexes: list[str] | None = Field(default=None, description="指数代码清单，缺省 = 默认宽基")


@router.post("/index-cons/sync")
def sync_index_cons_ep(req: IndexConsSyncIn | None = None) -> dict:
    """同步指数成分快照（沪深300/中证500/800/1000）→ index_cons。

    因子评价股票池过滤依赖这张表；tushare index_weight 4 次请求，同步返回。
    """
    from lquant.data.ingest.index_cons import sync_index_cons

    try:
        return sync_index_cons(req.indexes if req else None)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"指数成分同步失败: {e}") from e


@router.post("/tasks", status_code=202)
def create_data_task(req: TaskIn) -> dict:
    """创建数据任务并入队执行 → 202 + task_id。

    409：已有运行中任务（单任务互斥）；422：参数/前置校验失败（未知类型、
    日期非法、full_backfill 无退市股等）。
    """
    try:
        task = create_task(req.kind, req.params)
    except TaskConflictError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    # job_id = task_id：任务中心 cancel 靠它找到队列里的取消目标
    enqueue("lquant-ingest", ingest_tasks.execute_task, task["task_id"],
            job_id=task["task_id"])
    return {"task_id": task["task_id"]}


@router.get("/tasks")
def list_data_tasks(limit: int = Query(default=50, ge=1, le=500)) -> list[dict]:
    """数据任务列表（最新在前）。"""
    return list_tasks(limit)


@router.get("/tasks/{task_id}/events")
async def stream_task_events(task_id: str) -> StreamingResponse:
    """数据任务进度 SSE 流：`event: progress|done` 帧 + 15s 心跳注释行。

    连接即推一帧当前任务快照；之后转发任务线程 publish() 的每批进度。
    终态（terminal=true）推一帧 done 后关闭。任务不存在 → 404。
    事件是进程内存总线（core/task_events.py）—— RQ/redis 分支任务跑在
    worker 进程收不到，前端保留轮询兜底。
    """
    task = get_task(task_id)
    if task is None:
        raise HTTPException(404, f"任务不存在: {task_id}")
    from lquant.core.task_events import subscribe  # noqa: PLC0415

    q, unsubscribe = subscribe(task_id)

    async def gen():
        yield (
            'event: snapshot\ndata: '
            + json.dumps({
                "task_id": task_id,
                "status": task["status"],
                "phase": task.get("phase"),
                "done": task.get("done_symbols") or 0,
                "total": task.get("total_symbols"),
                "rows": task.get("rows_written") or 0,
            }, ensure_ascii=False)
            + "\n\n"
        )
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15.0)
                except TimeoutError:
                    yield ": heartbeat\n\n"  # 心跳防代理断空闲连接
                    continue
                terminal = ev.get("terminal")
                yield (
                    f"event: {'done' if terminal else 'progress'}\ndata: "
                    + json.dumps(ev, ensure_ascii=False)
                    + "\n\n"
                )
                if terminal:
                    return
        finally:
            unsubscribe()

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/tasks/{task_id}")
def get_data_task(task_id: str) -> dict:
    """任务详情：状态 / 进度 / 失败明细。WS 断开时前端轮询兜底。"""
    task = get_task(task_id)
    if task is None:
        raise HTTPException(404, f"任务不存在: {task_id}")
    return task


@router.post("/tasks/{task_id}/retry", status_code=202)
def retry_data_task(task_id: str) -> dict:
    """retry 补漏：unmark 失败标的 checkpoint 后重新执行 → 202 + task_id。

    端点同步原子认领（claim_retry，消除预检-入队的 TOCTOU）：
    认领成功任务即置 running，再入队执行已认领任务。
    409：pending/running 或竞争抢先；422：终态 ok 不可 retry；404：不存在。
    """
    task = get_task(task_id)
    if task is None:
        raise HTTPException(404, f"任务不存在: {task_id}")
    if task["status"] == "pending":
        # 排队中的任务走任务中心重跑；本端点保留旧口径（pending → 409）
        raise HTTPException(409, f"任务 {task_id} 排队中（pending），不可 retry")
    if task["status"] == "ok":
        raise HTTPException(422, f"任务 {task_id} 状态 ok 不可 retry")
    try:
        claim_retry(task_id)
    except TaskConflictError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    enqueue("lquant-ingest", ingest_tasks.run_claimed_task, task_id,
            job_id=task_id)
    return {"task_id": task_id}


# ---------- 全湖质量检查（`lq data check` 的 HTTP 入口） ----------

class LakeCheckIn(BaseModel):
    start: str | None = Field(default=None, max_length=10)
    end: str | None = Field(default=None, max_length=10)


@router.post("/check")
def run_lake_check_ep(req: LakeCheckIn) -> dict:
    """全湖质量校验（涨跌停/覆盖度/僵尸/复权/日历）→ 同步返回摘要。

    issue 一律落 data_quality_issue（与 CLI/同步链路共用一张表）。
    同步端点：validators 全是向量化计算，百万行秒级。
    422：日期格式非法；409：已有一次全湖检查在跑（防并发双跑竞写）。
    """
    from datetime import date as _date

    for name, val in (("start", req.start), ("end", req.end)):
        if isinstance(val, str) and val:
            try:
                _date.fromisoformat(val)
            except ValueError as e:
                raise HTTPException(422, f"{name} 日期非法: {val}") from e

    if not _lake_check_lock.acquire(blocking=False):
        raise HTTPException(409, "已有全湖质量检查在执行中，请稍后再试")
    try:
        from lquant.data.quality.pipeline import run_lake_checks

        issues = run_lake_checks(start=req.start, end=req.end)
    finally:
        _lake_check_lock.release()
    by_sev = {"fatal": 0, "error": 0, "warn": 0, "info": 0}
    for i in issues:
        by_sev[i.severity] = by_sev.get(i.severity, 0) + 1
    # fatal/error 排前面 —— 前端表格第一屏就该看到要紧的
    rank = {"fatal": 0, "error": 1, "warn": 2, "info": 3}
    issues.sort(key=lambda i: rank.get(i.severity, 9))
    return {
        "summary": {"total": len(issues), **by_sev},
        "issues": [{
            "rule": i.rule, "severity": i.severity, "detail": i.detail,
            "dataset": i.dataset, "symbol": i.symbol,
            "trade_date": str(i.trade_date) if i.trade_date else None,
            "count": i.count,
        } for i in issues[:100]],
    }


# ---------- 断点（checkpoint）管理 ----------

@router.get("/checkpoints")
def list_checkpoints() -> list[dict]:
    """断点续传文件列表：名称/已完成数/更新时间/元信息（含已归档 .done-*.json）。"""
    import json
    from pathlib import Path

    from lquant.core.config import get_settings

    d = Path(get_settings().cache_dir) / "checkpoints"
    out: list[dict] = []
    if not d.exists():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 坏文件跳过，不阻塞列表
            continue
        if not isinstance(data, dict):
            continue
        meta = data.get("meta") or {}
        out.append({
            "name": p.stem,
            "done": len(data.get("done") or []),
            "updated_at": data.get("updated_at"),
            "meta": meta if isinstance(meta, dict) else {},
        })
    return out


@router.delete("/checkpoints/{name}")
def archive_checkpoint(name: str) -> dict:
    """归档断点（重命名为 <name>.done-<ts>.json，保留数据可追溯）。

    换日期区间回填前清断点用 —— 归档后该断点不再参与续传。
    404：断点不存在；422：名称非法（防路径穿越）。
    """
    import time
    from pathlib import Path

    from lquant.core.config import get_settings

    if (not name or "/" in name or "\\" in name or ".." in name
            or name.startswith(".")):
        raise HTTPException(422, f"断点名非法: {name!r}")
    src = Path(get_settings().cache_dir) / "checkpoints" / f"{name}.json"
    if not src.exists():
        raise HTTPException(404, f"断点不存在: {name}")
    ts = time.strftime("%Y%m%d-%H%M%S")
    dst = src.with_name(f"{name}.done-{ts}.json")
    src.rename(dst)
    return {"name": name, "archived_to": dst.name}


# ---------- 跨源对拍 ----------

@router.post("/crosscheck")
def run_crosscheck_ep(req: CrosscheckIn) -> dict:
    """跨源对拍（同步端点：哨兵抽样小窗口，秒级）。返回 {summary, issues, flagged_rows}。"""
    from lquant.data.ingest.crosscheck import run_crosscheck

    try:
        return run_crosscheck(peers=req.peers, start=req.start,
                              end=req.end, limit=req.limit)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@router.get("/crosscheck/issues")
def crosscheck_issues(
    limit: int = Query(default=200, ge=1, le=1000),
    resolved: bool = False,
) -> list[dict]:
    """质量问题检索（data_quality_issue，默认未解决）。

    与 /data/issues 共用 query_issues 检索口径（无 severity/dataset 过滤）。
    """
    from lquant.data.quality.issues import query_issues

    return query_issues(limit=limit, resolved=resolved)


@router.post("/crosscheck/issues/resolve")
def resolve_crosscheck_issue(req: ResolveIn) -> dict:
    """标记问题已处理；不存在 → 404。"""
    from lquant.data.quality.issues import resolve_issue

    with reader() as con:
        try:
            row = con.execute(
                "SELECT issue_id FROM data_quality_issue WHERE issue_id = ?",
                [req.issue_id]).fetchone()
        except Exception as e:  # noqa: BLE001 - 表不存在等价于没有这条 issue
            from loguru import logger

            logger.warning(f"quality issue 查询失败（按不存在处理）: {e}")
            row = None
    if row is None:
        raise HTTPException(404, f"issue 不存在: {req.issue_id}")
    resolve_issue(req.issue_id)
    return {"issue_id": req.issue_id, "resolved": True}
