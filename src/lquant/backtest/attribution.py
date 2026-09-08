"""归因分析：个股贡献 + 分组 Brinson + 风险指标（α/β/信息比率/跟踪误差）。

设计原则：**归因必须守恒**。
把每日收益分解到个股权重 × 当日涨跌上，分不干净的部分（费用冲击、
开盘成交的择时残差）显式归到「现金/择时」桶，而不是悄悄消失 ——
否则 Σ个股贡献 ≠ 总收益，归因表就是错的。

分组归因用算术 Brinson（配置/选股/交互），基准是全市场等权：
没有指数成分权重数据前，这是唯一自洽可算的口径，会在输出里标注。
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date

import numpy as np

__all__ = ["stock_contribution", "brinson_by_group", "group_of_symbol",
           "risk_vs_benchmark", "industry_map_from_db"]

# 日收益护栏：|r| 超过此值视为数据坏点（demo/真实数据接缝、复权断裂等）。
# A 股单日涨跌停最大 30%（北交所），留足余量。
MAX_DAILY_RET = 0.35


def _clean_ret(px: float, px_prev: float) -> float | None:
    """相邻两价算日收益；越界/非正价返回 None（调用方跳过该点）。"""
    if not px_prev or px_prev <= 0 or not px or px <= 0:
        return None
    r = px / px_prev - 1.0
    return r if abs(r) <= MAX_DAILY_RET else None


def _safe(v) -> float:
    try:
        f = float(v)
        return f if math.isfinite(f) else 0.0
    except (TypeError, ValueError):
        return 0.0


# ---------- 个股贡献 ----------

def stock_contribution(
    positions: dict[date, dict[str, float]],   # {日期: {symbol: 收盘持仓量}}
    prices: dict[str, dict[date, float]],      # {symbol: {日期: 收盘价}}
    nav: list[tuple[date, float]],             # 每日净值
) -> tuple[list[dict], list[dict]]:
    """逐日分解：r_t ≈ Σ_i w_{t-1,i} · ret_{t,i} + cash_t + residual_t。

    w_{t-1,i} = 昨日持仓市值 / 昨日净值（昨日净值 ≤ 0 或缺价时跳过该日）。
    返回 (按累计贡献排序的个股列表, 逐日残差列表)。
    贡献单位是「收益率贡献」（小数），加总 ≈ 总收益。
    """
    nav_map = {d: v for d, v in nav}
    dates = [d for d, _ in nav]
    acc: dict[str, float] = defaultdict(float)
    residual_by_day: list[dict] = []

    for i in range(1, len(dates)):
        d_prev, d = dates[i - 1], dates[i]
        v_prev = nav_map.get(d_prev, 0.0)
        if v_prev <= 0:
            continue
        r_day = nav_map[d] / v_prev - 1.0 if nav_map.get(d, 0.0) > 0 else 0.0
        explained = 0.0
        for sym, qty in positions.get(d_prev, {}).items():
            px_prev = prices.get(sym, {}).get(d_prev)
            px = prices.get(sym, {}).get(d)
            r = _clean_ret(px, px_prev) if px and px_prev else None
            if r is None:
                continue
            w = qty * px_prev / v_prev
            contrib = w * r
            acc[sym] += contrib
            explained += contrib
        residual_by_day.append({"date": str(d), "residual": round(r_day - explained, 8),
                                "return": round(r_day, 8)})

    stocks = sorted(({"symbol": s, "contribution": round(c, 8)} for s, c in acc.items()),
                    key=lambda x: -x["contribution"])
    return stocks, residual_by_day


# ---------- 分组（行业 / 板块） ----------

def group_of_symbol(symbol: str) -> str:
    """无行业映射时的兜底分组：按板块。"""
    code = symbol.split(".")[0]
    if code.startswith("68"):
        return "科创板"
    if code.startswith("30"):
        return "创业板"
    if code.startswith("8") or code.startswith("4") or code.startswith("920"):
        return "北交所"
    return "沪市主板" if symbol.endswith(".SH") else "深市主板"


def industry_map_from_db(con) -> dict[str, str]:
    """industry_classify 表 → {symbol: 行业名}；空表返回 {}（调用方降级板块）。"""
    try:
        rows = con.execute(
            "SELECT symbol, name FROM industry_classify "
            "WHERE name IS NOT NULL AND name != ''").fetchall()
    except Exception:  # noqa: BLE001  表不存在时直接降级
        return {}
    out: dict[str, str] = {}
    for sym, name in rows:
        out.setdefault(sym, str(name))
    return out


def brinson_by_group(
    positions: dict[date, dict[str, float]],
    prices: dict[str, dict[date, float]],
    nav: list[tuple[date, float]],
    universe: list[str],                       # 基准池（全市场等权）
    group_map: dict[str, str],                 # {symbol: 行业/板块}，缺失自动降级
) -> dict:
    """算术 Brinson：按组拆超额收益 = 配置 α + 选股 α + 交互 α。

    基准：universe 内每日等权（无指数成分权重时的自洽口径）。
    组收益 rp/rb 用组内个股日收益等权均值；组合组权重用持仓市值占比。
    返回 {groups: [...], excess_total, note}。
    """
    nav_map = {d: v for d, v in nav}
    dates = [d for d, _ in nav]
    uni = set(universe)
    dates_set = set(dates)

    # 预取基准个股日收益（坏点经 _clean_ret 护栏过滤）
    bench_ret: dict[date, dict[str, float]] = {}
    for sym in uni:
        pxs = prices.get(sym, {})
        ds = sorted(pxs)
        for di, d in enumerate(ds):
            if di == 0:
                continue
            r = _clean_ret(pxs[d], pxs[ds[di - 1]])
            if r is not None:
                bench_ret.setdefault(d, {})[sym] = r

    stat = {g: {"alloc": 0.0, "select": 0.0, "interact": 0.0} for g in set(group_map.values())}
    for i in range(1, len(dates)):
        d_prev, d = dates[i - 1], dates[i]
        v_prev = nav_map.get(d_prev, 0.0)
        if v_prev <= 0 or d not in bench_ret:
            continue
        brs = bench_ret[d]

        # 组合组权重 / 基准组权重
        pv: dict[str, float] = defaultdict(float)
        total_pv = 0.0
        for sym, qty in positions.get(d_prev, {}).items():
            px_prev = prices.get(sym, {}).get(d_prev)
            if px_prev:
                val = qty * px_prev
                pv[group_map.get(sym) or group_of_symbol(sym)] += val
                total_pv += val
        if total_pv <= 0:
            continue
        bw: dict[str, list[float]] = defaultdict(list)   # 组 -> 个股收益
        for sym, r in brs.items():
            bw[group_map.get(sym) or group_of_symbol(sym)].append(r)

        for g, rets in bw.items():
            # 组合组收益：持仓个股（含基准池外）当日收益按市值加权
            num = den = 0.0
            for sym, qty in positions.get(d_prev, {}).items():
                if (group_map.get(sym) or group_of_symbol(sym)) != g:
                    continue
                px_prev = prices.get(sym, {}).get(d_prev)
                px = prices.get(sym, {}).get(d)
                r = brs.get(sym)
                if r is None:
                    r = _clean_ret(px, px_prev)
                if r is None:
                    continue
                num += qty * px_prev * r
                den += qty * px_prev
            rp = num / den if den > 0 else 0.0
            rb = sum(rets) / len(rets) if rets else 0.0
            wp = pv.get(g, 0.0) / total_pv
            wb = len(rets) / max(len(brs), 1)
            stat[g]["alloc"] += (wp - wb) * rb
            stat[g]["select"] += wb * (rp - rb)
            stat[g]["interact"] += (wp - wb) * (rp - rb)

    total_excess = 0.0
    groups = []
    for g, v in sorted(stat.items(), key=lambda kv: -(sum(kv[1].values()))):
        total = v["alloc"] + v["select"] + v["interact"]
        total_excess += total
        groups.append({"group": g, "alloc": round(v["alloc"], 6),
                       "select": round(v["select"], 6),
                       "interact": round(v["interact"], 6),
                       "total": round(total, 6)})
    return {"groups": groups, "excess_total": round(total_excess, 6),
            "note": "基准=全市场等权（缺指数成分权重，Brinson 为算术口径）"}


# ---------- 相对基准的风险指标 ----------

def risk_vs_benchmark(rets: list[float], bench_rets: list[float],
                      periods: int = 252) -> dict:
    """α/β/信息比率/跟踪误差/总超额。两条序列必须逐日对齐（短的对齐）。"""
    n = min(len(rets), len(bench_rets))
    if n < 20:
        return {}
    s = np.asarray(rets[-n:], dtype=float)
    b = np.asarray(bench_rets[-n:], dtype=float)
    var_b = float(np.var(b, ddof=1))
    beta = float(np.cov(s, b, ddof=1)[0, 1] / var_b) if var_b > 1e-15 else 0.0
    # JQ 口径：α 年化 = (组合均值 - β·基准均值) × 252
    alpha_ann = float((s.mean() - beta * b.mean()) * periods)
    excess = s - b
    te = float(np.std(excess, ddof=1) * math.sqrt(periods))
    ir = float(excess.mean() / np.std(excess, ddof=1) * math.sqrt(periods)) \
        if np.std(excess, ddof=1) > 1e-12 else float("nan")
    total_excess = float(np.prod(1 + s) / np.prod(1 + b) - 1)
    return {"alpha_annual": round(alpha_ann, 6), "beta": round(beta, 4),
            "information_ratio": round(ir, 4) if math.isfinite(ir) else None,
            "tracking_error": round(te, 6),
            "excess_return": round(total_excess, 6), "n_days": n}
