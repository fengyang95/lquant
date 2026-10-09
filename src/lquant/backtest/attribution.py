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

__all__ = ["stock_contribution", "brinson_by_group", "brinson_monthly",
           "group_of_symbol", "risk_vs_benchmark", "industry_map_from_db",
           "cost_drag", "portfolio_profile", "build_styles",
           "style_return_attribution"]

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


def _brinson_daily(
    positions: dict[date, dict[str, float]],
    prices: dict[str, dict[date, float]],
    nav: list[tuple[date, float]],
    universe: list[str],
    group_map: dict[str, str],
) -> list[tuple[date, dict[str, tuple[float, float, float]]]]:
    """逐日 Brinson 分解：[(日期, {组: (配置α, 选股α, 交互α)})]。

    算术 Brinson 的加法性在这里成立（组内日加总 = 全期），全期口径与
    月度分段口径共用这一个核心，保证两条路数字**必然一致**。
    """
    nav_map = {d: v for d, v in nav}
    dates = [d for d, _ in nav]
    uni = set(universe)

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

    out: list[tuple[date, dict[str, tuple[float, float, float]]]] = []
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

        day_stat: dict[str, tuple[float, float, float]] = {}
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
            day_stat[g] = ((wp - wb) * rb, wb * (rp - rb), (wp - wb) * (rp - rb))
        out.append((d, day_stat))
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
    stat: dict[str, dict[str, float]] = defaultdict(lambda: {"alloc": 0.0, "select": 0.0, "interact": 0.0})
    for _d, day_stat in _brinson_daily(positions, prices, nav, universe, group_map):
        for g, (a, s, it) in day_stat.items():
            stat[g]["alloc"] += a
            stat[g]["select"] += s
            stat[g]["interact"] += it

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


def brinson_monthly(
    positions: dict[date, dict[str, float]],
    prices: dict[str, dict[date, float]],
    nav: list[tuple[date, float]],
    universe: list[str],
    group_map: dict[str, str],
) -> dict:
    """按自然月分段的 Brinson（研报阅读口径：逐月看配置/选股谁在贡献）。

    与 :func:`brinson_by_group` 共用 `_brinson_daily` 核心，
    Σ各月 total = 全期 total（加法性），两处数字可互相校验。
    """
    acc: dict[str, dict[str, dict[str, float]]] = defaultdict(
        lambda: defaultdict(lambda: {"alloc": 0.0, "select": 0.0, "interact": 0.0}))
    for d, day_stat in _brinson_daily(positions, prices, nav, universe, group_map):
        key = f"{d.year:04d}-{d.month:02d}"
        for g, (a, s, it) in day_stat.items():
            acc[key][g]["alloc"] += a
            acc[key][g]["select"] += s
            acc[key][g]["interact"] += it

    months = []
    for key in sorted(acc):
        total_excess = 0.0
        groups = []
        for g, v in acc[key].items():
            total = v["alloc"] + v["select"] + v["interact"]
            total_excess += total
            groups.append({"group": g, "alloc": round(v["alloc"], 6),
                           "select": round(v["select"], 6),
                           "interact": round(v["interact"], 6),
                           "total": round(total, 6)})
        groups.sort(key=lambda x: -x["total"])
        months.append({"month": key, "groups": groups,
                       "excess_total": round(total_excess, 6)})
    return {"months": months,
            "note": "基准=全市场等权（缺指数成分权重，Brinson 为算术口径）"}


# ---------- 相对基准的风险指标 ----------

def risk_vs_benchmark(rets: list[float], bench_rets: list[float],
                      periods: int = 252) -> dict:
    """α/β/信息比率/跟踪误差/总超额。

    两条序列必须逐日对齐且长度一致 —— 日期错位时硬失败（ValueError），
    绝不静默尾部截断：把不同日期的收益率凑成一对算出的 α/β 是纯垃圾。
    调用方（backtests API）已按日期配对构造，长度不一致 = 调用方 bug。
    """
    if len(rets) != len(bench_rets):
        raise ValueError(
            f"risk_vs_benchmark 要求两序列逐日对齐：len(rets)={len(rets)} "
            f"!= len(bench_rets)={len(bench_rets)}（调用方须按日期配对后再传入）")
    n = len(rets)
    if n < 20:
        return {}
    s = np.asarray(rets, dtype=float)
    b = np.asarray(bench_rets, dtype=float)
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


# ---------- 成本拖累 ----------

def cost_drag(orders, nav: list[tuple[date, float]]) -> dict:
    """交易成本归因：每日费用 / 前一净值日净值 → 费用对收益率的拖累。

    orders : 可迭代的 ``(ts, fee)`` 元组或含 ``ts``/``fee`` 键的 dict
             （backtest_order 的 ts 是逐笔时间戳，这里只取日期）。
    nav    : [(date, nav), ...]

    为什么除以前日净值：费用发生在 t 日成交时，对当日净值的影响是
    fee / nav_{t-1}（与日收益同量纲），跨日可直接加总成累计拖累。
    返回 {fee_by_day: [{date, fee, drag}], total_fee, total_drag}。
    """
    from bisect import bisect_left

    nav_map = {d: float(v) for d, v in nav}
    nav_dates = sorted(nav_map)

    def _prev_nav(d: date) -> float:
        i = bisect_left(nav_dates, d)
        return nav_map[nav_dates[i - 1]] if i > 0 else 0.0

    fee_by_day: dict[date, float] = defaultdict(float)
    for o in orders:
        ts, fee = (o["ts"], o.get("fee", 0.0)) if isinstance(o, dict) else (o[0], o[1])
        try:
            d = date.fromisoformat(str(ts)[:10])
            f = float(fee)
        except (ValueError, TypeError):
            continue
        if f:
            fee_by_day[d] += f

    days = sorted(fee_by_day)
    total_fee = 0.0
    total_drag = 0.0
    out = []
    for d in days:
        fee = fee_by_day[d]
        prev = _prev_nav(d)
        drag = fee / prev if prev > 0 else 0.0
        total_fee += fee
        total_drag += drag
        out.append({"date": str(d), "fee": round(fee, 2), "drag": round(drag, 8)})
    return {"fee_by_day": out, "total_fee": round(total_fee, 2),
            "total_drag": round(total_drag, 8)}


# ---------- 持仓画像 ----------

def portfolio_profile(
    positions: dict[date, dict[str, float]],
    prices: dict[str, dict[date, float]],
    nav: list[tuple[date, float]],
    group_map: dict[str, str],
    styles: dict[str, dict[date, dict[str, float]]] | None = None,
) -> dict:
    """逐日持仓画像（收盘权重口径，不含现金）：行业权重 / 集中度 / 风格暴露。

    pyfolio positions tear sheet 的极简版：
    - ``industry``：各行业权重时序（看组合悄悄漂向了哪个行业）；
    - ``concentration``：HHI / Top5 / Top10 权重占比（拥挤度直觉）；
    - ``style``：风格变量市值加权的组合均值时序（大小盘、估值等漂移）。
    缺价个股当日剔除（权重分母只含可定价持仓），产出里带 n_pos 供校对。
    """
    dates = [d for d, _ in nav]
    ind_by_day: dict[date, dict[str, float]] = {}
    style_by_day: dict[date, dict[str, float]] = {}
    conc: list[dict] = []

    for d in dates:
        hold = positions.get(d) or {}
        if not hold:
            continue
        vals: dict[str, float] = {}
        for sym, qty in hold.items():
            px = prices.get(sym, {}).get(d)
            if px and px > 0:
                vals[sym] = qty * px
        total = sum(vals.values())
        if total <= 0:
            continue
        ws = {s: v / total for s, v in vals.items()}

        ind: dict[str, float] = defaultdict(float)
        for s, w in ws.items():
            ind[group_map.get(s) or group_of_symbol(s)] += w
        ind_by_day[d] = dict(ind)

        w_sorted = sorted(ws.values(), reverse=True)
        conc.append({"date": str(d),
                     "hhi": round(sum(w * w for w in ws.values()), 6),
                     "top5": round(sum(w_sorted[:5]), 6),
                     "top10": round(sum(w_sorted[:10]), 6),
                     "n_pos": len(ws)})

        if styles:
            srow: dict[str, tuple[float, float]] = defaultdict(lambda: (0.0, 0.0))
            for s, w in ws.items():
                sv = styles.get(s, {}).get(d) or {}
                for k, v in sv.items():
                    if v is not None and math.isfinite(float(v)):
                        num_, den_ = srow[k]
                        srow[k] = (num_ + w * float(v), den_ + w)
            if srow:
                style_by_day[d] = {k: round(num / den, 6)
                                   for k, (num, den) in srow.items() if den > 1e-12}

    def _series(by_day: dict[date, dict[str, float]], top: int | None = None) -> dict:
        if not by_day:
            return {"dates": [], "series": {}}
        keys = sorted(by_day)
        avg: dict[str, float] = defaultdict(float)
        for row in by_day.values():
            for k, v in row.items():
                avg[k] += v
        order = sorted(avg, key=lambda k: -avg[k])
        if top:
            order = order[:top]
        return {"dates": [str(k) for k in keys],
                "series": {k: [round(by_day[d].get(k, 0.0), 6) for d in keys]
                           for k in order}}

    return {"industry": _series(ind_by_day),
            "style": _series(style_by_day),
            "concentration": conc,
            "note": "收盘持仓权重口径（不含现金）；缺价个股当日剔除"}


def build_styles(panel, symbols: list[str] | None = None) -> dict:
    """从日线面板构建持仓画像风格暴露：{symbol: {date: {style: value}}}。

    六个风格维度（有列才算，缺列安静跳过 —— 画像少一块好过整个挂掉）：
    size=log(total_mv)、value_ep=1/PE、value_bp=1/PB、
    liquidity=turnover_rate、momentum_20d、volatility_20d。
    """
    import polars as pl

    # 可选风格列与必需列一并 select（先 select 后判列会把可选列全裁掉）
    opt = [c for c in ("total_mv", "pe_ttm", "pb_mrq", "turnover_rate")
           if c in panel.columns]
    base = panel.select(["trade_date", "symbol", "close", *opt])
    if symbols:
        base = base.filter(pl.col("symbol").is_in(symbols))
    if "total_mv" in base.columns:
        base = base.with_columns(pl.col("total_mv").clip(lower_bound=0).log1p().alias("_size"))
    if "pe_ttm" in base.columns:
        base = base.with_columns(
            pl.when(pl.col("pe_ttm") > 0).then(1.0 / pl.col("pe_ttm")).alias("_value_ep"))
    if "pb_mrq" in base.columns:
        base = base.with_columns(
            pl.when(pl.col("pb_mrq") > 0).then(1.0 / pl.col("pb_mrq")).alias("_value_bp"))
    if "turnover_rate" in base.columns:
        base = base.with_columns(pl.col("turnover_rate").cast(pl.Float64, strict=False).alias("_liquidity"))
    base = base.with_columns(
        (pl.col("close").pct_change(20).over("symbol")).alias("_momentum_20d"))
    base = base.with_columns(
        pl.col("close").pct_change().over("symbol").alias("_r"))
    base = base.with_columns(
        pl.col("_r").rolling_std(20, min_samples=20).over("symbol").alias("_volatility_20d"))

    out: dict[str, dict[date, dict[str, float]]] = {}
    names = [c[1:] for c in base.columns if c.startswith("_") and c != "_r"]
    for r in base.iter_rows(named=True):
        row = {n: r[f"_{n}"] for n in names if r.get(f"_{n}") is not None
               and math.isfinite(float(r[f"_{n}"]))}
        if not row:
            continue
        out.setdefault(r["symbol"], {})[r["trade_date"]] = row
    return out


# ---------- 风格收益归因 ----------

#: 截面回归的最小样本：因子数的多倍 + 绝对下限，否则该日跳过（宁缺毋假）。
REG_MIN_STOCKS = 50


def style_return_attribution(
    positions: dict[date, dict[str, float]],
    prices: dict[str, dict[date, float]],
    nav: list[tuple[date, float]],
    panel,
    factors: list[str] | None = None,
) -> dict:
    """风格收益归因（pyfolio perf_attrib / CNE5 同族，简化口径）。

    逐日截面回归：``r_i = Σ_k x_ik · f_k + α + ε_i``，
    x 为截面 z 分数（size/value_ep/momentum_20d/volatility_20d/liquidity，
    有列才算），普通最小二乘得当日风格因子收益 f_k。

    组合分解（Barra 时序约定：昨日权重 × 昨日暴露 × 今日因子收益）：
    ``factor_k 贡献 = Σ_i w_{i,t-1} · x_{i,t-1,k} · f_{k,t}``，
    common = Σfactor，specific = 当日策略收益 − common。
    守恒：Σfactor + specific = 算术累计收益（持有但不进回归截面的个股
    收益自动落在 specific —— 这是「解释不掉的部分」，不静默消失）。

    panel 必须含 trade_date/symbol/close，风格列缺失时自动收缩因子集。
    """
    import polars as pl

    need = {"trade_date", "symbol", "close"}
    if panel is None or not need <= set(panel.columns):
        return {"note": "行情面板缺 trade_date/symbol/close，风格归因不可用"}
    avail = []
    if "total_mv" in panel.columns:
        avail.append("size")
    if "pe_ttm" in panel.columns:
        avail.append("value_ep")
    if "turnover_rate" in panel.columns:
        avail.append("liquidity")
    # momentum / volatility 只依赖 close，恒可算
    avail += ["momentum_20d", "volatility_20d"]
    factors = [f for f in (factors or avail) if f in avail]
    if not factors:
        return {"note": "无可用风格列，风格归因不可用"}

    d = panel.select(["trade_date", "symbol", "close", *(
        [c for c in ("total_mv", "pe_ttm", "turnover_rate") if c in panel.columns])])
    d = d.sort(["symbol", "trade_date"]).with_columns(
        pl.col("close").pct_change().over("symbol").alias("_r"))
    if "total_mv" in d.columns:
        d = d.with_columns(pl.col("total_mv").clip(lower_bound=0).log1p().alias("size"))
    if "pe_ttm" in d.columns:
        d = d.with_columns(
            pl.when(pl.col("pe_ttm") > 0).then(1.0 / pl.col("pe_ttm")).alias("value_ep"))
    if "turnover_rate" in d.columns:
        d = d.with_columns(pl.col("turnover_rate").cast(pl.Float64, strict=False).alias("liquidity"))
    if "momentum_20d" in factors:
        d = d.with_columns(pl.col("close").pct_change(20).over("symbol").alias("momentum_20d"))
    if "volatility_20d" in factors:
        d = d.with_columns(
            pl.col("_r").rolling_std(20, min_samples=20).over("symbol").alias("volatility_20d"))

    # 截面 z 分数；截面 std≈0（如全市场同涨跌的坏数据）时置 null
    z = d.with_columns([
        pl.when(pl.col(c).std().over("trade_date") > 1e-12)
        .then((pl.col(c) - pl.col(c).mean().over("trade_date"))
              / pl.col(c).std().over("trade_date"))
        .otherwise(None).alias(c)
        for c in factors])
    z = z.drop_nulls(["_r", *factors]).filter(
        pl.col("_r").is_finite() & (pl.col("_r").abs() <= MAX_DAILY_RET))

    exp_map: dict[date, dict[str, list[float]]] = {}
    f_map: dict[date, list[float]] = {}
    for _key, g in z.group_by("trade_date", maintain_order=True):
        dt = g["trade_date"][0]   # 组内恒为同一日期（避免单/多列键元组差异）
        X = np.asarray(g.select(factors).to_numpy(), dtype=float)
        y = np.asarray(g["_r"].to_numpy(), dtype=float)
        if len(y) < max(REG_MIN_STOCKS, 3 * len(factors)):
            continue
        coef, *_ = np.linalg.lstsq(np.column_stack([np.ones(len(y)), X]), y, rcond=None)
        f_map[dt] = [float(c) for c in coef[1:]]
        syms = g["symbol"].to_list()
        for i, s in enumerate(syms):
            exp_map.setdefault(dt, {})[s] = X[i].tolist()

    nav_map = {dd: v for dd, v in nav}
    dates = [dd for dd, _ in nav]
    cum = {k: 0.0 for k in factors}
    common_cum = specific_cum = 0.0
    ret_sum = 0.0
    out_dates: list[str] = []
    factor_series: dict[str, list[float]] = {k: [] for k in factors}
    common_series: list[float] = []
    specific_series: list[float] = []

    for i in range(1, len(dates)):
        d_prev, d = dates[i - 1], dates[i]
        v_prev = nav_map.get(d_prev, 0.0)
        f_t = f_map.get(d)
        x_t = exp_map.get(d_prev)
        if v_prev <= 0 or not f_t or not x_t:
            continue
        w_sum = contrib = 0.0
        fk = {k: 0.0 for k in factors}
        for sym, qty in positions.get(d_prev, {}).items():
            px_prev = prices.get(sym, {}).get(d_prev)
            if not px_prev or sym not in x_t:
                continue
            w = qty * px_prev / v_prev
            w_sum += w
            xs = x_t[sym]
            for j, k in enumerate(factors):
                fk[k] += w * xs[j] * f_t[j]
                contrib += w * xs[j] * f_t[j]
        if w_sum <= 1e-12:
            continue
        r_day = nav_map[d] / v_prev - 1.0 if nav_map.get(d, 0.0) > 0 else 0.0
        common_cum += contrib
        specific_cum += r_day - contrib
        ret_sum += r_day
        for k in factors:
            cum[k] += fk[k]
            factor_series[k].append(round(cum[k], 8))
        out_dates.append(str(d))
        common_series.append(round(common_cum, 8))
        specific_series.append(round(specific_cum, 8))

    if not out_dates:
        return {"note": "回归样本不足（需全市场日线 + ≥50 只/日），风格归因不可用"}
    return {"dates": out_dates, "factors": factors,
            "factor_cum": factor_series,
            "common_cum": common_series, "specific_cum": specific_series,
            "totals": {"common": round(common_cum, 6),
                       "specific": round(specific_cum, 6),
                       "ret_arith": round(ret_sum, 6),
                       **{k: round(v, 6) for k, v in cum.items()}},
            "note": ("算术累计口径：Σ因子贡献=common，specific=收益−common；"
                     "因子收益来自逐日全市场截面回归（z 分数暴露），"
                     "昨日权重×昨日暴露×今日因子收益")}
