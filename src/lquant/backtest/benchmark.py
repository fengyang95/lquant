"""回测基准：指数日线 → 与组合逐日对齐的基准收益序列。

设计要点：

1. **基准是「可配的指数」，不是写死的常量**。默认 ``000300.SH``（沪深300），
   因为 A 股绝大多数选股策略的考核基准就是它；换策略换基准只需改配置。
2. **逐日对齐而非尾部截断**。组合与基准必须严格按日期配对；基准缺某天
   （指数入库有洞）时，该期**两边一起丢弃**。用 ``shift`` 硬凑会把不同日期的
   收益配成一对，算出的 α/β 是纯垃圾 —— ``risk_vs_benchmark`` 的长度断言
   就是为了拦这个（见 ``attribution.py``）。
3. **读取失败即降级，不抛异常**。回测的主产物是净值；基准缺失时把
   ``benchmark_available`` 标 False 并保留原因，绝不因为「基准表没建」
   把整个回测炸掉。
4. **数据真源是 DuckDB ``index_daily``**（由 `lq data index` 回填）。
   指数点位不是价格，不进 parquet 日线湖 —— 这是仓库既有口径。
"""
from __future__ import annotations

import math
from datetime import date

import polars as pl

__all__ = [
    "DEFAULT_BENCHMARK",
    "BENCHMARK_ALIASES",
    "load_index_series",
    "benchmark_returns_by_date",
    "pair_returns_with_benchmark",
    "benchmark_nav_aligned",
    "equal_weight_benchmark",
    "dedupe_series",
    "series_frame",
    "parse_benchmark",
]

#: 默认基准：沪深300。改这里等于改全仓默认口径，前端与 API 都会跟随。
DEFAULT_BENCHMARK = "000300.SH"

#: 常用别名的规范化（用户输入 "hs300" / "沪深300" / "000300" 都认）。
BENCHMARK_ALIASES: dict[str, str] = {
    "hs300": "000300.SH",
    "沪深300": "000300.SH",
    "000300": "000300.SH",
    "sh000300": "000300.SH",
    "zz500": "000905.SH",
    "中证500": "000905.SH",
    "000905": "000905.SH",
    "zz1000": "000852.SH",
    "中证1000": "000852.SH",
    "000852": "000852.SH",
    "szzz": "399001.SZ",
    "深证成指": "399001.SZ",
    "cyb": "399006.SZ",
    "创业板指": "399006.SZ",
}


def parse_benchmark(raw: str | None) -> str | None:
    """把用户输入的基准写法规范成 ``SYMBOL``；``none``/空 → None（不挂基准）。

    识别：``000300.SH`` 原样；``hs300``/``沪深300`` 走别名；
    ``000300`` 六位数字按指数代码段补后缀。
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s or s.lower() in ("none", "null", "-", "off", "无"):
        return None
    low = s.lower()
    if low in BENCHMARK_ALIASES:
        return BENCHMARK_ALIASES[low]
    if s in BENCHMARK_ALIASES:
        return BENCHMARK_ALIASES[s]
    if "." in s:
        return s.upper()
    if s.isdigit() and len(s) == 6:
        # 指数代码段：沪市 000xxx、深市 399xxx
        return f"{s}.SH" if s.startswith("000") else f"{s}.SZ" if s.startswith("399") else s
    return s


def load_index_series(
    symbol: str = DEFAULT_BENCHMARK,
    *,
    start: date | None = None,
    end: date | None = None,
    con=None,
) -> list[tuple[date, float]]:
    """读 ``index_daily`` 的 ``(trade_date, close)`` 升序序列。

    缺失/空表/未建表统一返回 ``[]``（调用方据此降级）；``pre_close`` 为空的
    行在此剔除，因为无法由它算出当日收益。调用方可注入 ``con``（测试/复用连接）。
    """
    sql = ("SELECT trade_date, close, pre_close FROM index_daily WHERE symbol = ?")
    params: list[object] = [symbol]
    if start is not None:
        sql += " AND trade_date >= ?"
        params.append(start)
    if end is not None:
        sql += " AND trade_date <= ?"
        params.append(end)
    sql += " ORDER BY trade_date"
    try:
        if con is not None:
            rows = con.execute(sql, params).fetchall()
        else:
            from lquant.core.db import reader

            with reader() as rc:
                rows = rc.execute(sql, params).fetchall()
    except Exception:  # noqa: BLE001  表未建 / 库不可用 → 无基准
        return []
    out: list[tuple[date, float]] = []
    for d, c, _pc in rows:
        if d is None or c is None:
            continue
        cf = float(c)
        if not math.isfinite(cf) or cf <= 0:
            continue
        out.append((d, cf))
    return out


def benchmark_returns_by_date(
    series: list[tuple[date, float]],
) -> dict[date, float]:
    """指数收盘序列 → ``{交易日: 当日收益}``（首日无前值，不产出）。

    停牌/缺口导致相邻两点跨多日时，收益按「两点之间的实际涨跌」计，
    调用方对齐后仍只在有组合收益的那天取值 —— 不会伪造中间日。
    """
    out: dict[date, float] = {}
    for i in range(1, len(series)):
        d_prev, c_prev = series[i - 1]
        d, c = series[i]
        if c_prev <= 0:
            continue
        out[d] = c / c_prev - 1.0
    return out


def pair_returns_with_benchmark(
    dates: list[date],
    returns: list[float],
    symbol: str = DEFAULT_BENCHMARK,
    *,
    con=None,
) -> tuple[list[float], list[float], str]:
    """把组合日收益与基准日收益**逐日配对**。

    Returns
    -------
    (port_rets, bench_rets, note) —— 两条等长序列（可能为空），以及一句
    说明（用于结果里的 ``benchmark_note``，解释基准是哪个/为何缺失）。

    ``dates`` 是净值日期（长度 = ``returns`` + 1）：``returns[i]`` 对应
    区间 ``dates[i] → dates[i+1]``，基准收益取 ``dates[i+1]`` 那天。
    """
    if not dates or not returns:
        return [], [], f"{symbol}: 无净值序列"
    series = load_index_series(symbol, start=dates[0], end=dates[-1], con=con)
    if not series:
        return [], [], f"{symbol}: index_daily 无数据（跑 `lq data index` 回填）"
    bmap = benchmark_returns_by_date(series)
    idx_ret = {d: r for d, r in zip(dates[1:], returns, strict=False)}
    port: list[float] = []
    bench: list[float] = []
    for d in dates[1:]:
        if d not in bmap or d not in idx_ret:
            continue
        r = idx_ret[d]
        if r is None or not math.isfinite(float(r)):
            continue
        port.append(float(r))
        bench.append(float(bmap[d]))
    note = f"{symbol}: 对齐 {len(port)}/{len(returns)} 期"
    if len(port) < len(returns):
        note += "（缺口已双边丢弃，不补造）"
    return port, bench, note


def benchmark_nav_aligned(
    run_dates: set[date],
    symbol: str = DEFAULT_BENCHMARK,
    *,
    con=None,
) -> tuple[list[dict], str]:
    """基准**净值**序列（首日归一为 1），只保留 ``run_dates`` 中的日期。

    回测报告要展示「策略 vs 基准」两条净值曲线，本函数产出的是基准那条。
    与 :func:`pair_returns_with_benchmark` 的分工：这里给图，那里给指标。

    Returns ``(benchmark, label)``；无数据时 ``([], reason)``。
    """
    if not run_dates:
        return [], f"{symbol}: 回测日期为空"
    series = load_index_series(symbol, start=min(run_dates), end=max(run_dates), con=con)
    if not series:
        return [], f"{symbol}: index_daily 无数据（跑 `lq data index` 回填）"
    pts = [(d, c) for d, c in series if d in run_dates]
    if len(pts) < 2:
        return [], f"{symbol}: 区间内仅 {len(pts)} 个交易日，无法作基准"
    from lquant.market.collectors.index_daily import INDEX_POOL

    label = INDEX_POOL.get(symbol, symbol)
    base = pts[0][1]
    nav = [{"date": str(d), "nav": round(c / base, 6)} for d, c in pts]
    return nav, label


def equal_weight_benchmark(mean_ret_by_date: dict[date, float],
                           run_dates: set[date]) -> list[dict]:
    """降级基准：全市场等权（指数缺失时的近似，**不是真基准**）。

    保留它是因为「有近似总比没有好」，但调用方**必须**在 label 里标明
    「非真基准」，避免等权数字被当成沪深300 读。
    """
    cur = 1.0
    out: list[dict] = []
    for d in sorted(run_dates):
        r = mean_ret_by_date.get(d)
        if r is not None and math.isfinite(r):
            cur *= 1 + r
        out.append({"date": str(d), "nav": round(cur, 6)})
    return out


def dedupe_series(series: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """同一天多行时取最后一条（upsert 残留/多源混写时的防御）。"""
    seen: dict[date, float] = {}
    for d, c in series:
        seen[d] = c
    return sorted(seen.items())


def series_frame(series: list[tuple[date, float]], symbol: str) -> pl.DataFrame:
    """``index_daily`` 形状的两列表，便于测试与调试输出。"""
    return pl.DataFrame(
        {"trade_date": [d for d, _ in series], "close": [c for _, c in series]},
        schema_overrides={"trade_date": pl.Date, "close": pl.Float64},
    ).with_columns(pl.lit(symbol).alias("symbol"))
