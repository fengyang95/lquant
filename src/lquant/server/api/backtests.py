"""回测提交 / 进度 / 结果 / 对比。

M1 阶段是同步执行（数据 3 万行以内秒级出结果）；
数据量上来后把 run 挪进 jobs 队列，接口签名不变 ——
前端已经按「run_id 轮询」的形状开发，切换无感。
"""
from __future__ import annotations

import json
import math
import uuid
from datetime import date, datetime

import polars as pl
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from lquant.backtest.engine import Engine, EngineConfig
from lquant.backtest.strategy.factor_topn import FactorTopNStrategy
from lquant.backtest.sweep import SweepSpec, run_sweep
from lquant.core.db import reader, writer
from lquant.data.store.parquet import read_daily
from lquant.server.jobs import enqueue, get_job

router = APIRouter(prefix="/backtests", tags=["backtests"])


def _persist_result(run_id: str, strategy: str, params: dict, res,
                    metrics: dict | None = None) -> None:
    """回测结果落库：run 头 + nav（带回撤）+ 成交 + 每日持仓（归因用）。"""
    nav_df = res.to_frame()
    if len(nav_df):
        nav_df = nav_df.with_columns(
            pl.col("nav").cum_max().alias("_peak")
        ).with_columns((1 - pl.col("nav") / pl.col("_peak")).alias("drawdown")).drop("_peak")
    m = metrics if metrics is not None else res.metrics

    with writer() as con:
        con.execute(
            "INSERT OR REPLACE INTO backtest_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [run_id, strategy,
             json.dumps(params, ensure_ascii=False),
             nav_df["trade_date"].min() if len(nav_df) else None,
             nav_df["trade_date"].max() if len(nav_df) else None,
             "done", json.dumps(m, default=str), datetime.now(), datetime.now()],
        )
        con.execute("DELETE FROM backtest_nav WHERE run_id = ?", [run_id])
        con.execute("DELETE FROM backtest_order WHERE run_id = ?", [run_id])
        con.execute("DELETE FROM backtest_position WHERE run_id = ?", [run_id])
        if len(nav_df):
            con.register("_nav", nav_df)
            con.execute("INSERT INTO backtest_nav SELECT ?, trade_date, nav, drawdown FROM _nav", [run_id])
        trades = res.trades_frame()
        if len(trades):
            con.register("_tr", trades)
            con.execute(
                "INSERT INTO backtest_order SELECT ?, trade_date, symbol, side, qty, price, fee FROM _tr",
                [run_id])
        pos_rows = [{"run_id": run_id, "trade_date": d, "symbol": s,
                     "qty": float(q), "avg_cost": 0.0}
                    for d, syms in res.positions.items() for s, q in syms.items()]
        if pos_rows:
            con.register("_pos", pl.DataFrame(pos_rows))
            con.execute("INSERT INTO backtest_position SELECT run_id, trade_date, symbol, qty, avg_cost FROM _pos")


def _persist_records(run_id: str, records: dict[str, list[tuple]]) -> None:
    """record(**kv) 自定义曲线落库 backtest_record（同 run 重跑先清后写）。"""
    rows = [{"run_id": run_id, "trade_date": d, "key": k, "value": v}
            for k, series in records.items() for d, v in series]
    with writer() as con:
        con.execute("DELETE FROM backtest_record WHERE run_id = ?", [run_id])
        if rows:
            con.register("_rec", pl.DataFrame(rows))
            con.execute(
                "INSERT INTO backtest_record "
                "SELECT run_id, trade_date, key, value FROM _rec")


def _run_saved_analyses(res) -> list[dict]:
    """回测结果自动执行所有已保存的自定义分析。

    成功条目为 chart/table spec（run_user_analysis 原样返回）；
    某个分析失败只记录 {"name", "error"} 条目，绝不影响回测本身。
    """
    from lquant.backtest.analysis import run_user_analysis
    from lquant.backtest.strategy_store import get_analysis, list_analyses

    dates = [d for d, _ in res.nav]
    navs = [float(n) for _, n in res.nav]
    payload = {
        "dates": dates,
        "nav": navs,
        "returns": [navs[i] / navs[i - 1] - 1 if navs[i - 1] > 0 else 0.0
                    for i in range(1, len(navs))],
        "trades": res.trades_frame(),
        "positions": res.positions,
        "records": res.records,
        "metrics": res.metrics,
    }
    out: list[dict] = []
    try:
        saved = list_analyses()
    except Exception:                          # noqa: BLE001  分析库读不了则跳过
        return out
    for a in saved:
        try:
            out.extend(run_user_analysis(get_analysis(a["id"])["source"], payload))
        except Exception as e:                 # noqa: BLE001  单个分析失败不炸回测
            out.append({"name": a["name"], "error": str(e)[:200]})
    return out


def _benchmark_nav_aligned(run_dates: set, run_nav: dict) -> tuple[list[dict], str, dict[str, float]]:
    """基准净值（优先沪深300 指数，降级全市场等权），与 run 日期对齐且首日归一。

    返回 (benchmark 序列, 标签, {date: 基准净值})。
    """
    benchmark: list[dict] = []
    label = "全市场等权"
    try:
        with reader() as con:
            rows = con.execute(
                "SELECT trade_date, close, pre_close FROM index_daily "
                "WHERE symbol = '000300.SH' ORDER BY trade_date").fetchall()
        if rows:
            cur = 1.0
            for d, c, pc in rows:
                if d in run_dates and pc:
                    cur *= float(c) / float(pc)      # c/pc 是价格比例（日收益 = c/pc - 1）
                    benchmark.append({"date": str(d), "nav": cur})
            if benchmark and benchmark[0]["nav"] > 0:
                base = benchmark[0]["nav"]
                benchmark = [{"date": x["date"], "nav": round(x["nav"] / base, 6)}
                             for x in benchmark]
                label = "沪深300"
    except Exception:  # noqa: BLE001
        benchmark = []

    if not benchmark and run_dates:
        try:
            start = min(run_dates)
            bdf = read_daily(start=str(start)).select(
                ["trade_date", "close", "pre_close"]).collect()
            if len(bdf):
                b = (bdf.with_columns((pl.col("close") / pl.col("pre_close") - 1).alias("r"))
                     .group_by("trade_date").agg(pl.col("r").mean().alias("r"))
                     .sort("trade_date"))
                cur = 1.0
                for d, rr in zip(b["trade_date"].to_list(), b["r"].to_list(), strict=False):
                    if d in run_dates:
                        if rr is not None and math.isfinite(rr):
                            cur *= 1 + float(rr)
                        benchmark.append({"date": str(d), "nav": cur})
                if benchmark and benchmark[0]["nav"] > 0:
                    base = benchmark[0]["nav"]
                    benchmark = [{"date": x["date"], "nav": round(x["nav"] / base, 6)}
                                 for x in benchmark]
        except Exception:  # noqa: BLE001  湖里没数据时基准留空，前端降级
            benchmark = []
    return benchmark, label, {date.fromisoformat(x["date"]): x["nav"] for x in benchmark}


class BacktestIn(BaseModel):
    factor: str = "mom_20"               # 因子列名（现算）
    formula: str = "pct_change_20"       # 与 factors API 同一套公式
    top_n: int = Field(default=5, ge=1, le=100)
    rebalance: str = Field(default="monthly", pattern="^(daily|weekly|monthly|none)$")
    initial_cash: float = Field(default=1_000_000, gt=0)
    start: str = Field(default="2026-01-01", pattern=r"^\d{4}-\d{2}-\d{2}$")


def _compute_factor(df: pl.DataFrame, formula: str) -> pl.DataFrame:
    if formula.startswith("pct_change_"):
        n = int(formula.rsplit("_", 1)[1])
        return df.with_columns(pl.col("close").pct_change(n).over("symbol").alias(formula.replace("_", "")))
    if formula.startswith("rolling_std_"):
        n = int(formula.rsplit("_", 1)[1])
        return df.with_columns(pl.col("close").pct_change().over("symbol")
                               .rolling_std(n).alias(formula.replace("_", "")))
    raise HTTPException(422, f"暂不支持的因子公式: {formula}")


class SweepIn(BaseModel):
    formula: str = "pct_change_20"
    param: str = Field(default="top_n", pattern="^(top_n)$")  # 允许被扫的参数白名单
    values: list[int] = Field(default=[1, 3, 5, 10], min_length=1, max_length=20)
    top_n: int = Field(default=5, ge=1, le=100)   # param=top_n 时的上一档起点（保留余量）
    rebalance: str = Field(default="monthly", pattern="^(daily|weekly|monthly|none)$")
    initial_cash: float = Field(default=1_000_000, gt=0)
    start: str = Field(default="2026-01-01", pattern=r"^\d{4}-\d{2}-\d{2}$")
    tag: str | None = None


def _run_sweep_job(formula: str, param: str, values: list, cfg: dict) -> list[dict]:
    """后台执行体：读数据 → 逐档回测 → 返回网格表（JSON 安全 dict）。"""
    df = read_daily(start=cfg["start"]).collect()
    col = formula.replace("_", "")
    d = _compute_factor(df, formula).drop_nulls([col])
    grid = run_sweep(
        d, param, values,
        SweepSpec(factor=col, rebalance=cfg["rebalance"],
                  initial_cash=cfg["initial_cash"]),
        strategy_kwargs={"top_n": cfg.get("top_n", 5)},
    )
    rows = grid.to_dicts()
    # value 可能是 int，前端要画轴，统一留浮点
    for r in rows:
        r["value"] = float(r["value"])
        r["rebalance"] = cfg["rebalance"]
        r["param"] = param
    return rows


@router.post("/sweep")
def run_sweep_api(req: SweepIn) -> dict:
    """异步参数扫描：入队即返回 sweep_id，客户端轮询 GET /sweep/{id}。

    单档数据量小时逐档秒级；这里统一走 jobs 队列（Redis 或本地降级），
    与 /run 现阶段同步执行的口径不同 —— 扫描档数多，不值得占住请求线程。
    """
    cfg = {"formula": req.formula, "param": req.param, "values": list(req.values),
           "rebalance": req.rebalance, "initial_cash": req.initial_cash,
           "start": req.start, "top_n": req.top_n}
    job = enqueue("lquant-backtest", _run_sweep_job, req.formula, req.param,
                  list(req.values), cfg)
    # 直接用任务 id 当 sweep_id：本地降级（JobRegistry）和 RQ 模式都能 get_job 查到
    return {"sweep_id": job.id, "status": "queued",
            "param": req.param, "n_points": len(req.values), "tag": req.tag}


@router.get("/sweep/{sweep_id}")
def get_sweep(sweep_id: str) -> dict:
    """取扫描结果：queued → done + grid 数据。"""
    job = get_job(sweep_id)
    if job is None:
        raise HTTPException(404, f"未找到扫描任务 {sweep_id}")
    status = job.get_status()
    # 本地降级 Job 有 .error；RQ Job 没有，异常在 .exc_info 里 —— 两种都读，别丢报错
    error = getattr(job, "error", None)
    if error is None and status == "failed":
        exc = getattr(job, "exc_info", None)
        error = exc if isinstance(exc, str) and exc.strip() else (str(exc) if exc else None)
    result = getattr(job, "result", None)
    return {"sweep_id": sweep_id, "status": status, "error": error,
            "grid": list(result) if status in ("finished", "done") and result else None}


@router.post("/run")
def run_backtest(req: BacktestIn) -> dict:
    """同步跑一个 TopN 回测，落库并返回 run_id。"""
    df = read_daily(start=req.start).collect()
    if not len(df):
        raise HTTPException(503, "日线数据为空，先跑 bootstrap 或 lq data demo")
    col = req.formula.replace("_", "")
    d = _compute_factor(df, req.formula).drop_nulls([col])

    run_id = uuid.uuid4().hex[:12]
    res = Engine(
        FactorTopNStrategy(factor=col, top_n=req.top_n),
        config=EngineConfig(initial_cash=req.initial_cash, rebalance=req.rebalance),
    ).run(d, extra_fields=[col])

    _persist_result(run_id, "factor_topn",
                    {"factor": col, "top_n": req.top_n, "rebalance": req.rebalance,
                     "formula": req.formula, "start": req.start}, res)

    m = res.metrics
    return {"run_id": run_id, "metrics": {k: (round(v, 4) if isinstance(v, float) else v)
                                          for k, v in m.items() if not isinstance(v, dict)},
            "n_nav_points": len(res.nav), "n_trades": m.get("n_trades", 0)}


class JQCodeIn(BaseModel):
    code: str = Field(min_length=10, max_length=100_000)
    start: str = "2026-01-01"
    end: str | None = None
    initial_cash: float = Field(default=1_000_000, gt=0)
    benchmark: str = "000300.SH"
    factor_formulas: list[str] = Field(default_factory=list, max_length=10)
    strategy_id: str | None = None
    run_analysis: bool = True


@router.post("/run-code")
def run_jq_code(req: JQCodeIn) -> dict:
    """运行聚宽兼容策略代码（initialize/handle_data/order...），落库返回 run_id。

    同步执行（湖内数据量秒级）；代码异常返回 422 并带堆栈。
    """
    from lquant.backtest.jqapi import JQRunner
    from lquant.backtest.validation import validate_source

    # 这里是用户代码入口，静态闸先过一遍再 exec（此前直接进 JQRunner，白名单形同虚设）
    errs = validate_source(req.code)
    if errs:
        raise HTTPException(422, "；".join(errs))

    df = read_daily(start=req.start, end=req.end).collect()
    if not len(df):
        raise HTTPException(503, "日线数据为空，先跑 bootstrap 或 lq data demo")

    try:
        runner = JQRunner(req.code, initial_cash=req.initial_cash,
                          benchmark=req.benchmark,
                          factor_formulas=req.factor_formulas or None)
        res = runner.run(df)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if res.error:
        raise HTTPException(422, res.error)
    if not res.nav:
        raise HTTPException(422, "策略未产生净值（检查数据区间与标的代码）")

    run_id = uuid.uuid4().hex[:12]
    params = {"code": req.code, "start": req.start, "end": req.end,
              "initial_cash": req.initial_cash,
              "benchmark": runner.benchmark, "engine": "jq_compat",
              "strategy_id": req.strategy_id,
              "factor_formulas": req.factor_formulas,
              "logs": res.logs[-100:]}
    _persist_result(run_id, "jq_custom", params, res)
    _persist_records(run_id, res.records)

    # 自定义分析：默认全量执行已保存的分析片段，结果并入 params_json 落库。
    # 放在回测落库之后，整块兜底 —— 分析全炸也不能回滚回测。
    if req.run_analysis:
        try:
            params["custom_analysis"] = _run_saved_analyses(res)
        except Exception:                      # noqa: BLE001
            params["custom_analysis"] = []
        with writer() as con:
            con.execute("UPDATE backtest_run SET params = ? WHERE run_id = ?",
                        [json.dumps(params, ensure_ascii=False, default=str), run_id])

    m = res.metrics
    return {"run_id": run_id,
            "metrics": {k: (round(v, 4) if isinstance(v, float) else v)
                        for k, v in m.items() if not isinstance(v, dict)},
            "n_nav_points": len(res.nav), "n_trades": m.get("n_trades", 0),
            "n_rejected": m.get("n_rejected", 0), "logs": res.logs[:50]}


@router.get("/{run_id}/code")
def get_run_code(run_id: str) -> dict:
    with reader() as con:
        row = con.execute(
            "SELECT params FROM backtest_run WHERE run_id = ?", [run_id]).fetchone()
    if not row:
        raise HTTPException(404, f"run 不存在: {run_id}")
    params = json.loads(row[0]) if row[0] else {}
    return {"run_id": run_id, "code": params.get("code"),
            "benchmark": params.get("benchmark"), "engine": params.get("engine")}


@router.get("/{run_id}/attribution")
def get_attribution(run_id: str, top: int = Query(default=15, ge=3, le=50)) -> dict:
    """归因分析：个股收益贡献 + 分组 Brinson + α/β/信息比率。

    持仓数据来自 backtest_position（run-code / run 端点都会写）；
    老运行没有持仓数据时返回 404 提示重跑。
    """
    from lquant.backtest.attribution import (
        brinson_by_group,
        group_of_symbol,
        industry_map_from_db,
        risk_vs_benchmark,
        stock_contribution,
    )

    with reader() as con:
        nav = con.execute(
            "SELECT trade_date, nav FROM backtest_nav WHERE run_id = ? ORDER BY trade_date",
            [run_id]).fetchall()
        if not nav:
            raise HTTPException(404, f"run 不存在或无净值: {run_id}")
        pos_rows = con.execute(
            "SELECT trade_date, symbol, qty FROM backtest_position "
            "WHERE run_id = ? ORDER BY trade_date", [run_id]).fetchall()
        industry = industry_map_from_db(con)

    if not pos_rows:
        raise HTTPException(404, "该运行没有每日持仓数据（老版本生成），重跑一次即可")

    nav_map = {r[0]: float(r[1]) for r in nav}
    dates = [r[0] for r in nav]
    positions: dict = {}
    for d, s, q in pos_rows:
        positions.setdefault(d, {})[s] = float(q)

    # 收盘价矩阵：只取持仓涉及 + 基准池，避免全湖展开
    need = set(positions[min(positions)] or [])
    for syms in positions.values():
        need |= set(syms)
    px_df = read_daily(start=str(dates[0]), end=str(dates[-1])).collect()
    if len(px_df) == 0:
        raise HTTPException(503, "行情数据为空")
    prices: dict[str, dict] = {}
    universe: list[str] = []
    for r in px_df.iter_rows(named=True):
        prices.setdefault(r["symbol"], {})[r["trade_date"]] = float(r["close"])
    # 基准池：区间成交额前 300 只（等权 Brinson 基准）
    amt = px_df.group_by("symbol").agg(pl.col("amount").mean().alias("a")) \
        .sort("a", descending=True).head(300)
    universe = amt["symbol"].to_list()

    stocks, residual = stock_contribution(positions, prices, nav)
    group_map = {s: industry.get(s) or group_of_symbol(s) for s in set(universe) | need}
    brinson = brinson_by_group(positions, prices, nav, universe, group_map)

    # α/β/IR/TE：策略日收益 vs 基准日收益（基准净值差分，不能用累计值）
    _, bench_label, bench_map = _benchmark_nav_aligned(set(dates), nav_map)
    s_rets, b_rets = [], []
    if len(bench_map) >= 2:
        bdates = sorted(bench_map)
        bench_ret = {bdates[i]: bench_map[bdates[i]] / bench_map[bdates[i - 1]] - 1
                     for i in range(1, len(bdates))}
        for i in range(1, len(dates)):
            d0, d1 = dates[i - 1], dates[i]
            if nav_map.get(d0, 0) > 0 and nav_map.get(d1, 0) > 0 and d1 in bench_ret:
                s_rets.append(nav_map[d1] / nav_map[d0] - 1)
                b_rets.append(bench_ret[d1])
    risk = risk_vs_benchmark(s_rets, b_rets)
    risk["benchmark"] = bench_label

    return {
        "run_id": run_id,
        "stock_contribution": {"top": stocks[:top],
                               "bottom": list(reversed(stocks[-top:])) if len(stocks) > top else [],
                               "n_stocks": len(stocks)},
        "residual_by_day": residual,
        "brinson": brinson,
        "risk": risk,
    }


@router.get("/{run_id}/holdings")
def get_holdings(run_id: str, day: str | None = Query(default=None)) -> dict:
    """每日持仓 & 收益（聚宽「每日持仓」页）。day 给定返回当日明细，否则返回日期索引。"""
    with reader() as con:
        nav = con.execute(
            "SELECT trade_date, nav, drawdown FROM backtest_nav WHERE run_id = ? ORDER BY trade_date",
            [run_id]).fetchall()
        if not nav:
            raise HTTPException(404, f"run 不存在或无净值: {run_id}")
        if day:
            d = date.fromisoformat(day)
            rows = con.execute(
                "SELECT symbol, qty FROM backtest_position WHERE run_id = ? AND trade_date = ?",
                [run_id, d]).fetchall()
        else:
            rows = None

    nav_map = {r[0]: float(r[1]) for r in nav}
    dates = [r[0] for r in nav]

    if rows is not None:
        syms = [r[0] for r in rows]
        px_df = (read_daily(start=str(dates[0]), end=str(dates[-1]))
                 .filter(pl.col("symbol").is_in(syms)).collect() if syms else pl.DataFrame())
        px = {}
        for r in px_df.iter_rows(named=True):
            px.setdefault(r["trade_date"], {})[r["symbol"]] = float(r["close"])
        p_map = px.get(d, {})
        prev = max((x for x in dates if x < d), default=None)
        nav_prev = nav_map.get(prev, nav_map.get(d, 0)) or 0
        items = []
        for s, q in rows:
            c = p_map.get(s, 0.0)
            items.append({"symbol": s, "qty": float(q), "close": c,
                          "value": round(float(q) * c, 2)})
        total_v = nav_map.get(d, 0) or 1
        for it in items:
            it["weight"] = round(it["value"] / total_v, 6)
        day_ret = (nav_map[d] / nav_prev - 1) if nav_prev > 0 and d in nav_map else None
        return {"run_id": run_id, "date": str(d), "nav": nav_map.get(d),
                "day_return": round(day_ret, 6) if day_ret is not None else None,
                "total_value": round(nav_map.get(d, 0.0), 2),
                "positions": sorted(items, key=lambda x: -x["value"])}

    out = []
    for i, d in enumerate(dates):
        prev = nav_map[dates[i - 1]] if i > 0 else None
        r = (nav_map[d] / prev - 1) if prev else None
        out.append({"date": str(d), "nav": round(nav_map[d], 4),
                    "day_return": round(r, 6) if r is not None else None})
    return {"run_id": run_id, "dates": out}


@router.get("")
def list_runs(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict]:
    """回测运行列表，offset/limit 分页。"""
    with reader() as con:
        rows = con.execute(
            "SELECT run_id, strategy, params, start_date, end_date, status, metrics, created_at "
            f"FROM backtest_run ORDER BY created_at DESC LIMIT {limit} OFFSET {offset}"
        ).fetchall()
    return [{"run_id": r[0], "strategy": r[1], "params": json.loads(r[2]) if r[2] else {},
             "start_date": str(r[3]), "end_date": str(r[4]), "status": r[5],
             "metrics": json.loads(r[6]) if r[6] else {}, "created_at": str(r[7])}
            for r in rows]


@router.get("/compare")
def compare_runs(ids: str) -> dict:
    """多运行对比（B6）：净值按首日归一到 1，逐日对齐（缺日为 null）。

    ids 逗号分隔，2~6 个。必须注册在 /{run_id} 之前，否则被吞。
    """
    run_ids = [x.strip() for x in ids.split(",") if x.strip()]
    if not 2 <= len(run_ids) <= 6:
        raise HTTPException(422, "需要 2~6 个 run_id")
    with reader() as con:
        series: dict[str, dict[str, float]] = {}
        runs: list[dict] = []
        for rid in run_ids:
            row = con.execute(
                "SELECT params, metrics FROM backtest_run WHERE run_id = ?", [rid]).fetchone()
            if not row:
                raise HTTPException(404, f"run 不存在: {rid}")
            runs.append({"run_id": rid,
                         "label": f"{json.loads(row[0]).get('formula', '?')}·top{json.loads(row[0]).get('top_n', '?')}"
                                  if row[0] else rid,
                         "params": json.loads(row[0]) if row[0] else {},
                         "metrics": json.loads(row[1]) if row[1] else {}})
            nav = con.execute(
                "SELECT trade_date, nav FROM backtest_nav WHERE run_id = ? ORDER BY trade_date",
                [rid]).fetchall()
            if not nav:
                raise HTTPException(422, f"run {rid} 无净值数据")
            base = nav[0][1]
            series[rid] = {str(d): (round(n / base, 4) if base else None) for d, n in nav}

    dates = sorted(set().union(*(s.keys() for s in series.values())))
    return {
        "runs": runs,
        "dates": dates,
        "series": {rid: [series[rid].get(d) for d in dates] for rid in run_ids},
    }


@router.get("/validation")
def validation_report() -> dict:
    """引擎自检报告：手算金标准 + 性质测试逐项 pass/fail + 基准策略元数据。

    前端「基准验证」tab 与 CI 之外的线上健康巡检共用 ——
    检查项实现见 lquant.backtest.selfcheck（与单测同源，一处修改两处生效）。
    """
    from lquant.backtest.benchmarks import BENCHMARK_META
    from lquant.backtest.selfcheck import run_selfcheck

    checks = run_selfcheck()
    return {"checks": checks,
            "all_passed": all(c["passed"] for c in checks),
            "benchmarks": BENCHMARK_META}


class BenchmarkRunIn(BaseModel):
    key: str = Field(min_length=2, max_length=40)
    start: str = Field(default="2024-01-01", pattern=r"^\d{4}-\d{2}-\d{2}$")
    end: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    initial_cash: float = Field(default=1_000_000, gt=0)
    symbols: list[str] = Field(default_factory=list, max_length=10)
    params: dict = Field(default_factory=dict)


@router.post("/run-benchmark")
def run_benchmark(req: BenchmarkRunIn) -> dict:
    """运行内置基准策略（公开出处可查证的复杂策略），落库并返回 run_id。

    基准策略内部累积 bar 历史，固定 rebalance="daily"（策略文档约束）。
    """
    from lquant.backtest.benchmarks import BENCHMARK_META, make_benchmark

    meta = BENCHMARK_META.get(req.key)
    if meta is None:
        raise HTTPException(404, f"未知基准策略 {req.key}，可选: {sorted(BENCHMARK_META)}")
    symbols = [s for s in (req.symbols or meta["symbols"]) if s.strip()]
    if not symbols:
        raise HTTPException(422, "标的列表为空")

    df = read_daily(symbols=symbols, start=req.start, end=req.end).collect()
    if not len(df):
        raise HTTPException(503, f"{symbols} 在 {req.start} 之后无日线数据，先同步数据")

    strategy = make_benchmark(req.key, **req.params)
    run_id = uuid.uuid4().hex[:12]
    res = Engine(strategy,
                 config=EngineConfig(initial_cash=req.initial_cash, rebalance="daily")).run(df)

    _persist_result(run_id, f"benchmark:{req.key}",
                    {"benchmark": req.key, "label": meta["label"],
                     "symbols": symbols, "start": req.start, "end": req.end,
                     "initial_cash": req.initial_cash,
                     "reference": meta["reference"], **req.params}, res)

    m = res.metrics
    return {"run_id": run_id, "label": meta["label"], "symbols": symbols,
            "metrics": {k: (round(v, 4) if isinstance(v, float) else v)
                        for k, v in m.items() if not isinstance(v, dict)},
            "n_nav_points": len(res.nav), "n_trades": m.get("n_trades", 0)}


@router.get("/{run_id}")
def get_run(run_id: str) -> dict:
    """回测详情 + 可视化数据包：

    - nav / drawdown 原始序列
    - monthly 月度收益热力（复利环比）
    - rolling 20 日滚动波动 / 夏普
    - return_hist 日收益分布直方图
    - benchmark 全市场等权净值（与 run 日期对齐，首日归一）
    """
    import math

    import numpy as np

    with reader() as con:
        row = con.execute(
            "SELECT strategy, params, status, metrics FROM backtest_run WHERE run_id = ?",
            [run_id]).fetchone()
        if not row:
            raise HTTPException(404, f"run 不存在: {run_id}")
        nav = con.execute(
            "SELECT trade_date, nav, drawdown FROM backtest_nav WHERE run_id = ? ORDER BY trade_date",
            [run_id]).fetchall()
        orders = con.execute(
            "SELECT ts, symbol, side, qty, price, fee FROM backtest_order WHERE run_id = ? ORDER BY ts",
            [run_id]).fetchall()
        rec_rows = con.execute(
            "SELECT trade_date, key, value FROM backtest_record WHERE run_id = ? "
            "ORDER BY trade_date", [run_id]).fetchall()
        params_dict = json.loads(row[1]) if row[1] else {}

    navs = [float(r[1]) for r in nav]
    rets = np.diff(navs) / np.array(navs[:-1]) if len(navs) > 1 else np.array([])

    def _jf(v) -> float | None:
        return round(float(v), 6) if v is not None and math.isfinite(v) else None

    # 月度收益：取每月最后一个净值，复利环比（首月以 run 首日净值为基期）
    month_last: dict[tuple[int, int], tuple[object, float]] = {}
    for r in nav:
        key = (r[0].year, r[0].month)
        if key not in month_last or r[0] > month_last[key][0]:
            month_last[key] = (r[0], float(r[1]))
    monthly, prev = [], (navs[0] if navs else None)
    for key in sorted(month_last):
        _, v = month_last[key]
        monthly.append({"year": key[0], "month": key[1], "ret": _jf(v / prev - 1) if prev else None})
        prev = v

    # 滚动 20 日波动 / 夏普（日频口径，√252 年化）
    rolling = []
    if len(rets) >= 20:
        for i in range(19, len(rets)):
            w = rets[i - 19:i + 1]
            vol = float(np.std(w, ddof=1) * math.sqrt(252))
            sharpe = float(np.mean(w) / np.std(w, ddof=1) * math.sqrt(252)) if vol > 1e-12 else None
            rolling.append({"date": str(nav[i + 1][0]), "vol": round(vol, 6), "sharpe": _jf(sharpe)})

    # 日收益分布直方图（40 桶）
    hist = []
    if len(rets) > 0:
        counts, edges = np.histogram(rets, bins=40)
        hist = [{"lo": round(float(edges[i]), 6), "hi": round(float(edges[i + 1]), 6),
                 "count": int(counts[i])} for i in range(len(counts))]

    # 基准（优先沪深300，降级全市场等权）+ α/β/信息比率
    nav_map = {r[0]: float(r[1]) for r in nav}
    dates = [r[0] for r in nav]
    benchmark, benchmark_label, bench_map = _benchmark_nav_aligned(set(dates), nav_map)
    from lquant.backtest.attribution import risk_vs_benchmark

    s_rets, b_rets = [], []
    if len(bench_map) >= 2:
        bdates = sorted(bench_map)
        bench_ret = {bdates[i]: bench_map[bdates[i]] / bench_map[bdates[i - 1]] - 1
                     for i in range(1, len(bdates))}
        for i in range(1, len(dates)):
            d0, d1 = dates[i - 1], dates[i]
            if nav_map.get(d0, 0) > 0 and nav_map.get(d1, 0) > 0 and d1 in bench_ret:
                s_rets.append(nav_map[d1] / nav_map[d0] - 1)
                b_rets.append(bench_ret[d1])
    risk = risk_vs_benchmark(s_rets, b_rets)
    if risk:
        risk["benchmark"] = benchmark_label

    records: dict[str, list] = {}
    for d, k, v in rec_rows:
        records.setdefault(k, []).append({"date": str(d), "value": v})

    return {
        "run_id": run_id, "strategy": row[0],
        "params": params_dict,
        "status": row[2], "metrics": json.loads(row[3]) if row[3] else {},
        "nav": [{"date": str(r[0]), "nav": r[1], "drawdown": r[2]} for r in nav],
        "orders": [{"ts": str(r[0]), "symbol": r[1], "side": r[2],
                    "qty": r[3], "price": r[4], "fee": r[5]} for r in orders],
        "monthly": monthly, "rolling": rolling,
        "return_hist": hist, "benchmark": benchmark,
        "benchmark_label": benchmark_label,
        "risk_vs_benchmark": risk,
        "records": records,
        "logs": params_dict.get("logs", []),
        "custom_analysis": params_dict.get("custom_analysis", []),
    }
