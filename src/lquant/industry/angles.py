"""行业分析各角度的计算（纯函数：输入数据帧 → 输出角度结果）。

与 :mod:`lquant.security.angles` 是同一种结构、同一种纪律：**不编造信号**。
取不到数就 ``available=False`` + ``hint``，样本不足就不给分（``score=None``），
绝不用一个中性分把空缺填平。

口径上的关键取舍，逐条写在这里，避免以后被「顺手改一下」改坏：

1. **行业指数是等权合成的**。成分股按 PIT 归属选出后等权平均日收益。等权
   ≠ 市值加权，与行情软件的行业指数会有差异；好处是口径透明、可复现、
   不被个别巨头市值碾压，且完全落在已有数据源内。同时输出**中位收益**作为
   对照 —— 等权与中位明显背离时，说明行业内部分化严重（少数大票拉动）。
2. **基本面用中位数聚合，且算「相对全市场」**。一个亏损巨头的净利同比能把
   行业均值拉到毫无意义的位置；而绝对水平也受宏观周期影响（全行业营收下滑
   的年份，同比 -5% 可能已经跑赢市场）。所以景气度既看绝对水平，也看相对
   全市场的中位数超额，还看**环比动能**（在变好还是变坏）。
3. **估值分位「低 = 便宜 = 利多」**，与个股分析的估值角度同一符号约定
   （``higher_better=False`` → 方向取反）。符号写反了分数照样算得出来，
   只是结论完全相反 —— 这类 bug 不会抛异常，只能靠构造已知答案的测试钉死。
4. **拥挤度反向计分**。成交额占比冲到自身历史高位说明交易过度拥挤，是风险
   而不是利好；它是唯一一个「越高越扣分」的资金面指标。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence

import polars as pl

from lquant.core import report as _report
from lquant.industry.contract import (
    angle,
    metric,
    percentile_rank,
    to_score,
    unavailable,
)
from lquant.industry.loader import MIN_MEMBERS

# 通用原语以短名复用（``core.report`` 是唯一实现源，个股与行业分析共用）。
_f = _report.safe_float
_pct = _report.pct_text
_sig = _report.signal_of
_tanh_norm = _report.tanh_norm

#: 趋势角度的回看窗口（交易日）。
TREND_WINDOWS: tuple[tuple[str, int], ...] = (
    ("r20", 20), ("r60", 60), ("r120", 120),
)

#: 「算作满分」的量级：绝对收益 / 相对基准超额（%）。
_RET_SCALE = 12.0
_EXCESS_SCALE = 10.0

#: 景气度：各概念指标的（权重, 绝对水平满分, 相对全市场满分, 环比满分）。
#:
#: 量级来自 A 股行业层面的常见分布：营收同比 15% 已经算高景气、净利同比
#: 波动更大给 25%、ROE 行业间差异大给 8%。
_PROSPERITY_SPECS: tuple[tuple[str, str, float, float, float, float], ...] = (
    ("revenue_yoy", "营收同比", 2.0, 15.0, 8.0, 15.0),
    ("profit_yoy", "净利同比", 2.5, 25.0, 12.0, 25.0),
    ("roe", "净资产收益率", 2.0, 8.0, 3.0, 4.0),
)

#: 概念键 → 候选物理列名（与 ``CANONICAL_FINANCIAL`` 的候选顺序同源）。
_PROSPERITY_CANDIDATES: dict[str, tuple[str, ...]] = {
    "revenue_yoy": ("indicator.or_yoy", "indicator.tr_yoy"),
    "profit_yoy": ("indicator.netprofit_yoy", "indicator.dt_netprofit_yoy"),
    "roe": ("indicator.roe", "indicator.roe_waa", "profit.roeAvg"),
}

#: 估值纵向分位所需的最少历史交易日。
_MIN_VALUATION_HISTORY = 120

#: 一个指标至少要有这么多成员有值，中位数才可信。
_MIN_SAMPLES = 3


# ---------------------------------------------------------------- 小工具


def _median(xs: Iterable[float | None]) -> float | None:
    """中位数（过滤 None/NaN；空集返回 None）。"""
    vals = [float(v) for v in xs if _f(v) is not None]
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _cum_return(rets: Sequence[float | None], n: int) -> float | None:
    """近 n 个交易日的区间收益（有效样本不足以 ``n`` 根时返回 None）。

    先剔除 None 再数长度：行业早期成员少、停牌多时 ``rets`` 里会有空洞，
    直接用 ``len(rets)`` 判断会把「有效样本不够」当成「窗口够长」。
    """
    clean = [float(r) for r in rets if _f(r) is not None]
    if len(clean) <= n:
        return None
    cur = 1.0
    for r in clean[-n:]:
        cur *= (1.0 + r)
    return cur - 1.0


def _level(rets: Sequence[float | None]) -> list[float]:
    """日收益序列 → 合成指数净值（起点 1.0）。"""
    out, cur = [], 1.0
    for r in rets:
        f = _f(r)
        if f is not None:
            cur *= (1.0 + f)
        out.append(cur)
    return out


def _ma_tail(values: Sequence[float], n: int) -> float | None:
    """最后 n 个值的均值（不足 n 个返回 None）。"""
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def pick_values(sub: pl.DataFrame, candidates: Sequence[str],
                value_col: str = "value") -> list[float]:
    """按候选名优先级合并同一概念的多个数据源列 → 每个 symbol 一个值。

    同一指标在不同数据源下命名不同（``indicator.or_yoy`` vs ``indicator.tr_yoy``）。
    对每个 symbol 按 ``candidates`` 的顺序取第一个有值的 —— 与
    ``lquant.security.loader.CANONICAL_FINANCIAL`` 的候选顺序同一约定。
    """
    if sub.is_empty():
        return []
    picks: list[pl.DataFrame] = []
    for i, c in enumerate(candidates):
        s = (sub.filter(pl.col("item") == c)
                .select(["symbol", pl.col(value_col).alias(f"_v{i}")]))
        if not s.is_empty():
            picks.append(s)
    if not picks:
        return []
    wide = picks[0]
    for p in picks[1:]:
        wide = wide.join(p, on="symbol", how="full", coalesce=True)
    expr = pl.col(f"_v{len(picks) - 1}")
    for i in range(len(picks) - 2, -1, -1):
        expr = pl.col(f"_v{i}").fill_null(expr)
    return [float(v) for v in wide.select(expr.alias("v"))["v"].drop_nulls().to_list()
            if _f(v) is not None]


def prosperity_items() -> tuple[str, ...]:
    """景气度用到的物理财务列全集（供一次 SQL 取回，避免逐概念查库）。"""
    return tuple(sorted({c for cands in _PROSPERITY_CANDIDATES.values()
                         for c in cands}))


def concept_market_medians(item_medians: dict[str, float] | None
                           ) -> dict[str, float]:
    """物理列名 → 概念键的中位数映射（同概念取第一个有数据的候选列）。"""
    if not item_medians:
        return {}
    out: dict[str, float] = {}
    for key, candidates in _PROSPERITY_CANDIDATES.items():
        for c in candidates:
            v = _f(item_medians.get(c))
            if v is not None:
                out[key] = v
                break
    return out


# ---------------------------------------------------------------- RRG

#: RRG（相对旋转图）参数。照抄西部证券研报复现实现，**不要随手调参**：
#: 回看 220 日 + MA20 平滑是研报给出敏感性最优的一组。
RRG_LOOKBACK_RATIO = 220
RRG_LOOKBACK_MOM = 60
RRG_SMOOTH = 20

#: 四象限 → (中文名, 方向)。中心是 100，严格不等号（等于 100 归为读不出）。
#:
#: 象限语义：领先（强且仍在走强）/ 改善（弱但边际转强）/ 滞后（弱且仍在走弱）/
#: 疲软（强但边际转弱）。「改善」与「疲软」是**方向相反的两个转折点**，
#: 这正是 RRG 相对单纯动量排名的增量价值。
RRG_QUADRANTS: dict[int, tuple[str, float]] = {
    1: ("领先", 1.0),
    2: ("改善", 0.4),
    3: ("滞后", -1.0),
    4: ("疲软", -0.3),
}


def rrg_state(daily: pl.DataFrame, benchmark: pl.DataFrame) -> dict | None:
    """相对旋转图状态：``RS-Ratio`` / ``RS-Momentum`` / 象限。

    公式（三层递推，照抄研报复现实现）::

        RS          = 行业指数 / 基准指数 × 100
        RS-Ratio    = mean_20( 100 × RS / shift(RS, 220) )
        RS-Momentum = mean_20( 100 × RS-Ratio / shift(RS-Ratio, 60) )

    预热需要 ``220 + 60 + 2×20 = 320`` 个交易日；历史不够时返回 ``None``
    （**不缩窗口凑一个数出来** —— 那会让不同标的算的不是同一个指标）。
    """
    if daily.is_empty() or benchmark.is_empty():
        return None
    d = daily.sort("trade_date")
    lvl = _level(d["ret"].to_list())
    ind = d.select(["trade_date"]).with_columns(pl.Series("ind", lvl))
    b = (benchmark.select(["trade_date", pl.col("close").alias("bench")])
         .sort("trade_date"))
    m = ind.join(b, on="trade_date", how="inner").sort("trade_date")
    need = RRG_LOOKBACK_RATIO + RRG_LOOKBACK_MOM + 2 * RRG_SMOOTH
    if m.height < need:
        return None
    m = m.with_columns((pl.col("ind") / pl.col("bench") * 100.0).alias("rs"))
    m = m.with_columns(
        (100.0 * pl.col("rs") / pl.col("rs").shift(RRG_LOOKBACK_RATIO))
        .rolling_mean(RRG_SMOOTH).alias("rs_ratio"))
    m = m.with_columns(
        (100.0 * pl.col("rs_ratio") / pl.col("rs_ratio").shift(RRG_LOOKBACK_MOM))
        .rolling_mean(RRG_SMOOTH).alias("rs_mom"))
    last = m.tail(1).row(0, named=True)
    ratio, mom = _f(last.get("rs_ratio")), _f(last.get("rs_mom"))
    if ratio is None or mom is None or ratio == 100.0 or mom == 100.0:
        return None
    if ratio > 100.0 and mom > 100.0:
        q = 1
    elif ratio < 100.0 and mom > 100.0:
        q = 2
    elif ratio < 100.0 and mom < 100.0:
        q = 3
    else:
        q = 4
    label, direction = RRG_QUADRANTS[q]
    return {"quadrant": q, "quadrant_label": label, "direction": direction,
            "rs_ratio": round(ratio, 2), "rs_momentum": round(mom, 2),
            "warmup_days": need}



# ---------------------------------------------------------------- 跨行业横截面


def industry_return_table(universe, windows: Sequence[int] = (20, 60, 120)
                          ) -> pl.DataFrame:
    """全行业区间收益表（行业轮动榜的基础）。

    Returns:
        ``industry_code, industry_name, n_members, r{window}..., amount_20d``，
        按第一个窗口的收益降序（收益算不出来的行业排在最后，而不是补 0）。
    """
    daily = getattr(universe, "daily", None)
    if daily is None or daily.is_empty():
        return pl.DataFrame()
    rows = []
    for keys, g in daily.sort("trade_date").group_by(
            ["industry_code", "industry_name"], maintain_order=True):
        code, name = keys[0], keys[1]
        g = g.sort("trade_date")
        rets = g["ret"].to_list()
        row = {"industry_code": code, "industry_name": name,
               "n_members": int(g["n"].max() or 0)}
        for w in windows:
            r = _cum_return(rets, w)
            row[f"r{w}"] = None if r is None else round(r * 100.0, 2)
        amount = [v for v in g["amount"].to_list()[-20:] if _f(v) is not None]
        row["amount_20d"] = float(sum(amount))
        rows.append(row)
    # 非空 daily 经 group_by 必然至少产出一组，这里不需要空分支
    df = pl.DataFrame(rows)
    key = f"r{windows[0]}" if windows else None
    if key and key in df.columns:
        df = df.sort(key, descending=True, nulls_last=True)
    return df


def industry_valuation_cross(universe) -> pl.DataFrame:
    """asof 当日全行业估值中位数（横向比较用）。

    亏损（PE ≤ 0）与净资产为负（PB ≤ 0）的样本**剔除**而不是取绝对值 ——
    负 PE 不是「便宜」，把它算进中位数会把行业估值算成负数。
    """
    cross = getattr(universe, "valuation_cross", None)
    mem = getattr(universe, "membership", None)
    if cross is None or cross.is_empty() or mem is None or mem.is_empty():
        return pl.DataFrame()
    df = mem.select(["symbol", "industry_code", "industry_name"]).join(
        cross, on="symbol", how="inner")
    if df.is_empty():
        return pl.DataFrame()
    aggs = []
    for col, alias in (("pe_ttm", "pe_median"), ("pb_mrq", "pb_median")):
        if col in df.columns:
            aggs.append(pl.col(col).filter(pl.col(col) > 0).median().alias(alias))
    if not aggs:
        return pl.DataFrame()
    return (df.group_by(["industry_code", "industry_name"])
              .agg(aggs)
              .sort("industry_code"))


# ---------------------------------------------------------------- 趋势与轮动


def trend_angle(daily: pl.DataFrame, benchmark: pl.DataFrame,
                benchmark_symbol: str | None,
                cross: pl.DataFrame | None = None) -> dict:
    """趋势与轮动：行业合成指数的收益 / 相对基准超额 / 全行业相对强度排名。"""
    if daily.is_empty():
        return unavailable(
            "trend",
            "没有行业成员日线：先同步日线（`lq data sync`）并补齐行业分类",
            summary="缺少日线，无法计算行业趋势",
        )
    daily = daily.sort("trade_date")
    rets = daily["ret"].to_list()
    if len([r for r in rets if _f(r) is not None]) < 20:
        return unavailable("trend", "行业合成指数有效交易日不足 20 天")

    signals: list[tuple[float, float]] = []
    metrics: list[dict] = []
    n_expected = 0

    # (a) 相对基准超额 —— 行业轮动的核心信号（绝对收益会被大盘带跑）
    b_close: list[float] = []
    if not benchmark.is_empty() and "close" in benchmark.columns:
        b_close = [float(c) for c in benchmark["close"].to_list() if _f(c) is not None]
    excess_ok = 0
    for label, n, key in (("20 日超额", 20, "excess20"),
                          ("60 日超额", 60, "excess60"),
                          ("120 日超额", 120, "excess120")):
        n_expected += 1
        sr = _cum_return(rets, n)
        br = None
        if len(b_close) > n:
            br = b_close[-1] / b_close[-n - 1] - 1.0
        if sr is None or br is None:
            continue
        excess = (1 + sr) / (1 + br) - 1
        direction = _tanh_norm(excess * 100.0, _EXCESS_SCALE) or 0.0
        signals.append((direction, 3.0 if n <= 60 else 2.0))
        excess_ok += 1
        metrics.append(metric(key, label, excess * 100, display=_pct(excess * 100),
                              unit="%", signal=_sig(direction),
                              note=f"行业 {sr * 100:+.1f}% vs 基准 {br * 100:+.1f}%"))

    # (b) 绝对收益 —— 没有基准指数时它是唯一的趋势信号，有基准时降级为展示
    for label, n, key in (("20 日收益", 20, "ret20"),
                          ("60 日收益", 60, "ret60"),
                          ("120 日收益", 120, "ret120")):
        n_expected += 1
        r = _cum_return(rets, n)
        if r is None:
            continue
        direction = _tanh_norm(r * 100.0, _RET_SCALE) or 0.0
        signals.append((direction, 0.5 if excess_ok else 2.0))
        metrics.append(metric(key, label, r * 100, display=_pct(r * 100), unit="%",
                              signal=_sig(direction)))

    # (c) 全行业相对强度排名 —— 「这个行业在全行业里排第几」
    pct20 = None
    n_expected += 1
    if cross is not None and not cross.is_empty() and "r20" in cross.columns:
        vals = [v for v in cross["r20"].to_list() if _f(v) is not None]
        code = daily["industry_code"][0]
        hit = cross.filter(pl.col("industry_code") == code)
        mine = _f(hit["r20"][0]) if not hit.is_empty() else None
        if mine is not None and vals:
            pct20 = percentile_rank(vals, mine)
    if pct20 is not None:
        direction = (pct20 - 50.0) / 50.0
        signals.append((direction, 2.5))
        metrics.append(metric("rank20", "全行业 20 日强度分位", pct20,
                              display=f"{pct20:.0f}%", unit="%",
                              percentile=pct20, signal=_sig(direction),
                              note="100% = 全行业最强"))
    else:
        metrics.append(metric("rank20", "全行业 20 日强度分位", None,
                              note="横截面数据不足，未参与排名"))

    # (d) 均线位置 —— 趋势的「持续 vs 破位」
    level = _level(rets)
    for label, n, key in (("MA20", 20, "ma20_dev"), ("MA60", 60, "ma60_dev")):
        n_expected += 1
        ma = _ma_tail(level, n)
        if ma is None or ma <= 0:
            continue
        dev = level[-1] / ma - 1.0
        direction = _tanh_norm(dev * 100.0, 6.0) or 0.0
        signals.append((direction, 1.5))
        metrics.append(metric(key, f"相对{label}偏离", dev * 100,
                              display=_pct(dev * 100), unit="%",
                              signal=_sig(direction)))

    # (e) RRG 相对旋转图 —— 不只是「强不强」，还有「在变强还是变弱」
    n_expected += 1
    rrg = rrg_state(daily, benchmark)
    if rrg is not None:
        signals.append((rrg["direction"], 2.0))
        metrics.append(metric("rs_ratio", "RS-Ratio（相对强度）", rrg["rs_ratio"],
                              display=f"{rrg['rs_ratio']:.1f}", unit=None,
                              signal=_sig(rrg["direction"]),
                              note=">100 表示相对基准走强；中枢 100"))
        metrics.append(metric("rs_momentum", "RS-Momentum（相对动量）",
                              rrg["rs_momentum"],
                              display=f"{rrg['rs_momentum']:.1f}", unit=None,
                              signal=_sig(rrg["direction"]),
                              note=f"RRG 象限：{rrg['quadrant_label']}"))
    else:
        need = RRG_LOOKBACK_RATIO + RRG_LOOKBACK_MOM + 2 * RRG_SMOOTH
        metrics.append(metric(
            "rs_ratio", "RS-Ratio（相对强度）", None,
            note=("没有基准指数日线（index_daily 为空），RRG 需要基准"
                  if benchmark.is_empty() else
                  f"行业与基准的重叠历史不足 {need} 个交易日，不出 RRG 象限")))

    # 有 >=20 个有效交易日时 MA20 必然算得出，signals 不可能为空
    score = to_score(signals)
    coverage = min(1.0, len(signals) / max(1, n_expected))
    r20 = _cum_return(rets, 20)
    beat = sum(1 for d, _ in signals if d > 0)
    summary = (f"20 日 {_pct(r20 * 100) if r20 is not None else '—'}"
               f" · {beat}/{len(signals)} 项指向强势"
               + (f" · 全行业 {pct20:.0f}% 分位" if pct20 is not None else "")
               + (f" · RRG {rrg['quadrant_label']}" if rrg else ""))
    return angle("trend", available=True, summary=summary, metrics=metrics,
                 score=score, coverage=coverage,
                 extra={"benchmark": benchmark_symbol,
                        "rank20_percentile": pct20,
                        "rrg": rrg,
                        "windows": [w for _, w in TREND_WINDOWS]})


# ---------------------------------------------------------------- 景气度


def prosperity_angle(fin: pl.DataFrame,
                     market_medians: dict[str, float] | None) -> dict:
    """景气度：成分股 PIT 财务的中位数水平 + 相对全市场超额 + 环比动能。

    Args:
        fin: 行业成员的 PIT 财务长表，含 ``rn``（1 最新、2 上一期）。
        market_medians: **按概念键**的全市场中位数（``concept_market_medians``
            的产物），空 dict 时只做绝对水平判断。
    """
    if fin.is_empty():
        return unavailable(
            "prosperity",
            "没有 PIT 财务数据：先执行 `lq data financial --symbols ...` 回填",
            summary="缺少财务数据，无法判断景气度",
        )

    signals: list[tuple[float, float]] = []
    metrics: list[dict] = []
    # 分母 = 真的可能产出的信号数（绝对水平 + 环比动能，每个概念各 1 个；
    # 有全市场对照时再加一组相对超额）—— 用拍脑袋的常数会让「覆盖度」说谎。
    n_expected = 2.0 * len(_PROSPERITY_SPECS)
    if market_medians:
        n_expected += float(len(_PROSPERITY_SPECS))
    latest = fin.filter(pl.col("rn") == 1)
    prev = fin.filter(pl.col("rn") == 2)

    for key, label, weight, abs_scale, rel_scale, _mom_scale in _PROSPERITY_SPECS:
        candidates = _PROSPERITY_CANDIDATES.get(key, ())
        cur = pick_values(latest, candidates)
        if len(cur) < _MIN_SAMPLES:
            metrics.append(metric(key, label, None,
                                  note=f"有效样本 {len(cur)} 家，不足 {_MIN_SAMPLES} 家"))
            continue
        med = _median(cur)   # pick_values 已剔 null，样本够时必有值
        direction = _tanh_norm(med, abs_scale) or 0.0
        signals.append((direction, weight))
        note = f"{len(cur)} 家成分股中位数"
        market_med = _f((market_medians or {}).get(key))
        if market_med is not None:
            rel = med - market_med
            rel_dir = _tanh_norm(rel, rel_scale) or 0.0
            signals.append((rel_dir, 1.5))
            note += f" · 全市场 {market_med:.2f}，超额 {rel:+.2f}"
        metrics.append(metric(key, label, med, display=f"{med:.2f}%", unit="%",
                              signal=_sig(direction), note=note))

    # 环比动能：最新一期中位数 − 上一期中位数。
    #
    # 用「两期中位数之差」而不是逐 symbol 配对：财务披露批次不齐（有的公司
    # 晚一个季度），逐票配对会丢掉一大批样本，样本本身也随后视选择偏移。
    for key, label, _w, _a, _r, mom_scale in _PROSPERITY_SPECS:
        candidates = _PROSPERITY_CANDIDATES.get(key, ())
        cur_vals = pick_values(latest, candidates)
        prev_vals = pick_values(prev, candidates)
        mc, mp = _median(cur_vals), _median(prev_vals)
        if mc is None or mp is None:
            metrics.append(metric(f"{key}_mom", f"{label}环比", None,
                                  note="没有可比的上一期数据"))
            continue
        delta = mc - mp
        direction = _tanh_norm(delta, mom_scale) or 0.0
        signals.append((direction, 1.5))
        metrics.append(metric(f"{key}_mom", f"{label}环比", delta,
                              display=f"{delta:+.2f}pct", unit="pct",
                              signal=_sig(direction),
                              note=f"本期 {mc:.2f}% vs 上期 {mp:.2f}%"))

    if not signals:
        return unavailable("prosperity", "财务数据存在，但没有一个指标有足够样本",
                           summary="财务样本不足，无法判断景气度")

    score = to_score(signals)
    coverage = min(1.0, len(signals) / max(1.0, n_expected))
    named = [f"{m['label']} {m['display']}" for m in metrics[:2] if m.get("display")]
    summary = f"{len(signals)} 项景气指标可用" + (" · " + "、".join(named) if named else "")
    return angle("prosperity", available=True, summary=summary, metrics=metrics,
                 score=score, coverage=coverage)


# ---------------------------------------------------------------- 估值


def valuation_angle(history: pl.DataFrame, cross: pl.DataFrame | None,
                    code: str) -> dict:
    """估值：行业 PE/PB 中位数的**自身历史分位**与**全市场横向分位**。

    符号约定：分位越低越便宜 → 方向为正（利多）。与个股分析一致。
    """
    if history.is_empty():
        return unavailable(
            "valuation",
            "没有估值历史（daily_basic 为空）：先回填日线估值列",
            summary="缺少估值数据，无法判断行业估值位置",
        )
    keep = [c for c in ("pe_ttm", "pb_mrq") if c in history.columns]
    if not keep:
        return unavailable("valuation", "daily_basic 缺 pe_ttm / pb_mrq 列")

    signals: list[tuple[float, float]] = []
    metrics: list[dict] = []
    n_expected = 2.0

    for col, label in (("pe_ttm", "PE(TTM)"), ("pb_mrq", "PB(MRQ)")):
        key = "pe" if col == "pe_ttm" else "pb"
        if col not in history.columns:
            metrics.append(metric(f"{key}_median", f"{label} 中位数", None,
                                  note="daily_basic 无该列"))
            continue
        g = (history.filter(pl.col(col).is_not_null() & (pl.col(col) > 0))
             .group_by("trade_date")
             .agg(pl.col(col).median().alias("med"),
                  pl.len().alias("n"))
             .sort("trade_date"))
        vals = [v for v in g["med"].to_list() if _f(v) is not None]
        if not vals:
            metrics.append(metric(f"{key}_median", f"{label} 中位数", None,
                                  note="无正数样本（全部亏损或净资产为负）"))
            continue
        cur = vals[-1]
        pct = (percentile_rank(vals, cur)
               if len(vals) >= _MIN_VALUATION_HISTORY else None)
        note = (f"{len(vals)} 个交易日历史" if pct is not None
                else f"历史仅 {len(vals)} 个交易日，不足 {_MIN_VALUATION_HISTORY}，不出分位")
        direction = (-(pct - 50.0) / 50.0) if pct is not None else None
        if direction is not None:
            signals.append((direction, 3.0 if col == "pe_ttm" else 2.0))
        metrics.append(metric(f"{key}_median", f"{label} 中位数", cur,
                              display=f"{cur:.2f}", unit="倍",
                              percentile=pct,
                              signal=_sig(direction) if direction is not None else None,
                              note=note))
        if pct is not None:
            metrics.append(metric(f"{key}_hist_pct", f"{label} 历史分位", pct,
                                  display=f"{pct:.0f}%", unit="%",
                                  percentile=pct, signal=_sig(direction),
                                  note="越低越便宜"))
    n_expected += 1

    # 横向：本行业 PE 中位数在全行业中的分位
    if cross is not None and not cross.is_empty() and "pe_median" in cross.columns:
        vals = [v for v in cross["pe_median"].to_list() if _f(v) is not None]
        hit = cross.filter(pl.col("industry_code") == code)
        mine = _f(hit["pe_median"][0]) if not hit.is_empty() else None
        if mine is not None and vals:
            pct = percentile_rank(vals, mine)
            if pct is not None:
                direction = -(pct - 50.0) / 50.0
                signals.append((direction, 1.5))
                metrics.append(metric(
                    "pe_cross_pct", "全行业估值分位", pct, display=f"{pct:.0f}%",
                    unit="%", percentile=pct, signal=_sig(direction),
                    note=f"共 {len(vals)} 个行业的 PE 中位数参与比较"))

    if not signals:
        return unavailable("valuation", "估值历史不足，无法给出可靠分位")

    score = to_score(signals)
    coverage = min(1.0, len(signals) / max(1.0, n_expected))
    named = [f"{m['label']} {m['display']}"
             for m in metrics if m.get("display")][:3]
    return angle("valuation", available=True, summary="、".join(named) or "行业估值已计算",
                 metrics=metrics, score=score, coverage=coverage)


# ---------------------------------------------------------------- 资金与拥挤度


def capital_angle(flow: pl.DataFrame, market_flow: pl.DataFrame | None,
                  daily: pl.DataFrame, market_amount: pl.DataFrame | None
                  ) -> dict:
    """资金与拥挤度：主力净流入（绝对 + 相对净额比）+ 成交额占比的历史分位。

    拥挤度是**反向**指标：成交额占比冲到自身历史高位意味着交易过度拥挤，
    后面对应的是均值回归风险，所以它给负方向。
    """
    if flow.is_empty() and daily.is_empty():
        return unavailable(
            "capital",
            "没有资金流数据（money_flow 未同步）：先执行 `lq market collect`",
            summary="缺少资金流，无法判断资金与拥挤度",
        )

    signals: list[tuple[float, float]] = []
    metrics: list[dict] = []
    n_expected = 4.0

    if not flow.is_empty() and "main_net_inflow" in flow.columns:
        aggs = [pl.col("main_net_inflow").sum().alias("net")]
        if "main_net_ratio" in flow.columns:
            aggs.append(pl.col("main_net_ratio").mean().alias("ratio"))
        agg = flow.group_by("trade_date").agg(aggs).sort("trade_date")
        nets = agg["net"].to_list()
        ratios = agg["ratio"].to_list() if "ratio" in agg.columns else []

        for label, n, key in (("5 日主力净流入", 5, "net5"),
                              ("20 日主力净流入", 20, "net20")):
            if len(nets) < n:
                continue
            net = float(sum(v for v in nets[-n:] if _f(v) is not None))
            ratio = None
            if len(ratios) >= n:
                rs = [v for v in ratios[-n:] if _f(v) is not None]
                if rs:
                    ratio = sum(rs) / len(rs)
            # 净额比（主力净流入 / 成交额）才是可比的 —— 绝对金额跨行业没有意义
            direction = _tanh_norm(ratio, 3.0) if ratio is not None else 0.0
            signals.append((direction or 0.0, 2.5 if n <= 5 else 2.0))
            metrics.append(metric(key, label, net, display=_yi(net), unit="元",
                                  signal=_sig(direction or 0.0),
                                  note=(f"期间主力净额比均值 {ratio:+.2f}%"
                                        if ratio is not None else None)))

        # 相对全市场的资金面强度：本行业净流入占全市场净流入的份额
        if market_flow is not None and not market_flow.is_empty():
            m = market_flow.sort("trade_date")
            mnet = m["main_net_inflow"].to_list()
            if len(mnet) >= 5 and len(nets) >= 5:
                ind5 = sum(v for v in nets[-5:] if _f(v) is not None)
                mkt5 = sum(v for v in mnet[-5:] if _f(v) is not None)
                share = ind5 / mkt5 if mkt5 else None
                if share is not None:
                    direction = _tanh_norm(share - 0.05, 0.05) or 0.0
                    signals.append((direction, 1.5))
                    metrics.append(metric(
                        "net_share5", "占全市场净流入", share * 100,
                        display=f"{share * 100:+.1f}%", unit="%",
                        signal=_sig(direction),
                        note="份额为正说明资金在向这个行业集中"))

    # 拥挤度：成交额占比 + 换手率，各自取自身历史分位后取均值。
    #
    # 这两个维度合起来才叫拥挤：成交额占比说明「钱有多少堆在这里」，换手率
    # 说明「筹码换得有多快」。只用一个容易把「大行业天然成交额高」误判成拥挤。
    # 口径参照开源实现 Level-1-industry-congestion（成交额占比 + 换手率分位，
    # 高/低拥挤阈值 0.8 / 0.2）。
    if not daily.is_empty() and market_amount is not None and not market_amount.is_empty():
        ind = (daily.group_by("trade_date")
                    .agg(pl.col("amount").sum().alias("amt"),
                         pl.col("turnover_rate").mean().alias("turn"))
                    .sort("trade_date"))
        mkt = market_amount.sort("trade_date")
        joined = (ind.join(mkt.rename({"amount": "mkt_amt"}), on="trade_date",
                           how="inner")
                  # Polars 的 join 不保证行序；下面的 ``[-1]`` 取「最新一天」，
                  # 少这一次 sort 就会把任意一天的占比当当前值。
                  .sort("trade_date"))
        pcts: list[float] = []
        if not joined.is_empty():
            rows = [r for r in joined.iter_rows(named=True)
                    if _f(r["amt"]) is not None and (_f(r["mkt_amt"]) or 0.0) > 0]
            shares = [r["amt"] / r["mkt_amt"] * 100.0 for r in rows]
            turns = [_f(r["turn"]) for r in rows]
            if len(shares) >= 20:
                pct = percentile_rank(shares, shares[-1])
                if pct is not None:
                    pcts.append(pct)
                    metrics.append(metric(
                        "amount_share_pct", "成交额占比分位", pct,
                        display=f"{pct:.0f}%", unit="%", percentile=pct,
                        signal="neutral",
                        note=f"当前占全市场 {shares[-1]:.2f}%"))
            else:
                metrics.append(metric("amount_share_pct", "成交额占比分位", None,
                                      note="成交额历史不足 20 个交易日"))
            clean_turn = [v for v in turns if v is not None]
            if len(clean_turn) >= 20 and turns[-1] is not None:
                pct = percentile_rank(clean_turn, turns[-1])
                if pct is not None:
                    pcts.append(pct)
                    metrics.append(metric(
                        "turnover_pct", "换手率分位", pct, display=f"{pct:.0f}%",
                        unit="%", percentile=pct, signal="neutral",
                        note=f"当前行业平均换手 {turns[-1]:.2f}%"))
        if pcts:
            crowd = sum(pcts) / len(pcts)
            direction = -(crowd - 50.0) / 50.0          # 拥挤 → 反向
            signals.append((direction, 2.0))
            metrics.append(metric(
                "crowding_pct", "拥挤度分位", crowd, display=f"{crowd:.0f}%",
                unit="%", percentile=crowd, signal=_sig(direction),
                note="成交额占比与换手率分位的均值；≥80% 视为高度拥挤"))
        elif not any(m["key"] == "amount_share_pct" for m in metrics):
            metrics.append(metric("crowding_pct", "拥挤度分位", None,
                                  note="成交额历史不足 20 个交易日"))
    else:
        metrics.append(metric("crowding_pct", "拥挤度分位", None,
                              note="缺少日线成交额或全市场对照"))

    if not signals:
        return unavailable("capital", "资金流数据不足 5 个交易日")

    score = to_score(signals)
    coverage = min(1.0, len(signals) / max(1.0, n_expected))
    named = [f"{m['label']} {m['display']}" for m in metrics[:2] if m.get("display")]
    return angle("capital", available=True,
                 summary="、".join(named) or "资金面已计算",
                 metrics=metrics, score=score, coverage=coverage)


# ---------------------------------------------------------------- 宽度与情绪


def breadth_angle(daily: pl.DataFrame, bars: pl.DataFrame,
                  limit_up: pl.DataFrame) -> dict:
    """宽度与情绪：上涨家数占比 / 涨停家数 / 创 60 日新高占比 / 分化度。"""
    if daily.is_empty():
        return unavailable("breadth", "没有行业成员日线，无法计算宽度")

    daily = daily.sort("trade_date")
    signals: list[tuple[float, float]] = []
    metrics: list[dict] = []
    # 可打分项恰好 4 个：上涨家数占比 / 创 60 日新高 / NH-NL / 涨停天数。
    # 分化度只做展示（没有可判定的方向），不计入分母。
    n_expected = 3.0

    ups = [v for v in daily["up_ratio"].to_list() if _f(v) is not None]
    if len(ups) >= 5:
        up5 = sum(ups[-5:]) / 5.0
        direction = _tanh_norm(up5 - 0.5, 0.15) or 0.0
        signals.append((direction, 3.0))
        metrics.append(metric("up_ratio5", "5 日上涨家数占比", up5 * 100,
                              display=f"{up5 * 100:.1f}%", unit="%",
                              signal=_sig(direction), note="50% 为多空平衡线"))
    else:
        metrics.append(metric("up_ratio5", "5 日上涨家数占比", None,
                              note="有效交易日不足 5 天"))

    # 创 60 日新高占比与分化度（用成员日线自己算，不依赖外部新高表）
    if not bars.is_empty() and {"symbol", "trade_date", "close"} <= set(bars.columns):
        b = bars.sort(["symbol", "trade_date"]).with_columns(
            (pl.col("close") >= pl.col("close").rolling_max(60).over("symbol"))
            .alias("_nh"))
        agg = (b.group_by("trade_date")
                 .agg(pl.col("_nh").mean().alias("nh_ratio"),
                      pl.col("ret").std().alias("dispersion"))
                 .sort("trade_date"))
        nh = [v for v in agg["nh_ratio"].to_list()[-5:] if _f(v) is not None]
        if nh:
            nh5 = sum(nh) / len(nh)
            direction = _tanh_norm(nh5 - 0.10, 0.15) or 0.0
            signals.append((direction, 1.5))
            metrics.append(metric("new_high_ratio", "5 日创 60 日新高占比", nh5 * 100,
                                  display=f"{nh5 * 100:.1f}%", unit="%",
                                  signal=_sig(direction)))
        disp = [v for v in agg["dispersion"].to_list()[-20:] if _f(v) is not None]
        metrics.append(metric(
            "dispersion", "成员收益分化度(20 日均值)",
            (sum(disp) / len(disp) * 100) if disp else None,
            display=(f"{sum(disp) / len(disp) * 100:.2f}%" if disp else None),
            unit="%", signal="neutral",
            note="日收益横截面标准差；越大越分化" if disp else "数据不足"))

    # NH-NL 净新高占比（华福证券市场情绪指标专题口径）：
    #   NH = 1[high ≥ 滚动 window 日最高价的 shift(offset) 值]
    #   NL = 1[low  ≤ 滚动 window 日最低价的 shift(offset) 值]
    #   NHNL = (ΣNH − ΣNL) / 有效成员数
    # 比「创 60 日新高占比」信息量更大：分子的新高减新低是一个有正负的净额，
    # 只有新高才会把它顶到 +1。shift(offset) 是研报刻意的 5 日偏移。
    nhnl = _nhnl_latest(bars)
    n_expected += 1
    if nhnl is not None:
        value, n_members = nhnl
        direction = _nhnl_direction(value, n_members)
        signals.append((direction, 2.0))
        metrics.append(metric(
            "nhnl", "净新高占比 (NH-NL)", value,
            display=f"{value * 100:+.1f}%", unit="%",
            signal=_sig(direction),
            note=(f"{n_members} 个有效成员；"
                  f"阈值 {'≥40' if n_members >= 40 else '<40'} 家口径")))

    # 涨停家数：只有采集过涨停池才有（源站不提供历史回溯，缺就是缺）
    if not limit_up.is_empty() and "trade_date" in limit_up.columns:
        per_day = limit_up.group_by("trade_date").len().sort("trade_date")
        counts = [int(c) for c in per_day["len"].to_list()]
        if counts:
            recent = counts[-20:]
            freq = len(recent)
            direction = _tanh_norm(float(freq) / 20.0 - 0.05, 0.1) or 0.0
            signals.append((direction, 1.5))
            metrics.append(metric("limit_up_days", "近 20 日出现涨停的天数", freq,
                                  display=f"{freq} 天", unit="天",
                                  signal=_sig(direction),
                                  note=f"合计 {sum(recent)} 家次"))
    else:
        metrics.append(metric("limit_up_days", "近 20 日出现涨停的天数", None,
                              note="涨停池未采集（`lq market collect`）"))

    if not signals:
        return unavailable("breadth", "宽度数据不足 5 个交易日")

    score = to_score(signals)
    coverage = min(1.0, len(signals) / max(1.0, n_expected))
    named = [f"{m['label']} {m['display']}" for m in metrics[:2] if m.get("display")]
    return angle("breadth", available=True,
                 summary="、".join(named) or "宽度已计算",
                 metrics=metrics, score=score, coverage=coverage)


#: NH-NL 的滚动窗口与偏移（华福证券口径：250 日 + 偏移 5 日）。
NHNL_WINDOW = 250
NHNL_OFFSET = 5

#: NH-NL 阈值：(成员数下限, 贪婪, 乐观, 悲观, 恐惧)。
#: 成分股少的行业波动天然更大，阈值相应放宽 —— 这是研报里的硬编码分档。
NHNL_THRESHOLDS: tuple[tuple[int, float, float, float, float], ...] = (
    (40, 0.3, 0.2, -0.2, -0.3),
    (0, 0.4, 0.3, -0.3, -0.4),
)


def _nhnl_latest(bars: pl.DataFrame) -> tuple[float, int] | None:
    """最新交易日的净新高占比 ``(NH-NL)`` 与有效成员数。

    数据不足以形成满窗（``window + offset``）时返回 ``None`` —— 「新高」在
    窗口不满时是伪新高，宁可不出这个数。
    """
    need = NHNL_WINDOW + NHNL_OFFSET
    if bars.is_empty() or not {"symbol", "trade_date", "high", "low"} <= set(bars.columns):
        return None
    b = bars.sort(["symbol", "trade_date"]).drop_nulls(["high", "low"])
    if b.is_empty() or b["trade_date"].n_unique() < need:
        return None
    b = b.with_columns(
        (pl.col("high")
         >= pl.col("high").rolling_max(NHNL_WINDOW).shift(NHNL_OFFSET).over("symbol")
         ).fill_null(False).alias("_nh"),
        (pl.col("low")
         <= pl.col("low").rolling_min(NHNL_WINDOW).shift(NHNL_OFFSET).over("symbol")
         ).fill_null(False).alias("_nl"),
    )
    last_day = b["trade_date"].max()
    tail = b.filter(pl.col("trade_date") == last_day)
    n = tail.height          # max() 来自同一列，tail 必非空
    value = (int(tail["_nh"].sum()) - int(tail["_nl"].sum())) / n
    return float(value), n


def _nhnl_direction(value: float, n_members: int) -> float:
    """净新高占比 → 方向（按成员数分档的阈值体系）。"""
    # 阈值表最后一档的 min_n 是 0，任何成员数都能命中 —— 不需要兜底分支
    _min_n, greed, optimism, pessimism, fear = next(
        t for t in NHNL_THRESHOLDS if n_members >= t[0])
    if value >= greed:
        return 1.0
    if value >= optimism:
        return 0.5
    if value <= fear:
        return -1.0
    if value <= pessimism:
        return -0.5
    return 0.0


def _yi(v: float | None) -> str | None:
    """金额（元）→ 亿/万展示。金额统一元是平台硬约束。"""
    f = _f(v)
    if f is None:
        return None
    if abs(f) >= 1e8:
        return f"{f / 1e8:,.2f} 亿"
    if abs(f) >= 1e4:
        return f"{f / 1e4:,.2f} 万"
    return f"{f:,.0f}"


# ---------------------------------------------------------------- 风险


def risk_block(universe, daily: pl.DataFrame, member_count: int,
               valuation_pct: float | None = None,
               crowding_pct: float | None = None) -> dict:
    """行业层面的风险提示（独立于综合分，只报告事实）。"""
    flags: list[str] = []
    metrics: list[dict] = []

    metrics.append(metric("member_count", "成分股数", member_count,
                          display=f"{member_count}", unit="家"))
    if member_count < MIN_MEMBERS:
        flags.append(
            f"行业仅 {member_count} 个成员，代表性不足 —— 个股异动会主导"
            "「行业」走势，结论稳健性有限")

    if daily.is_empty():
        flags.append("没有行业日线：趋势 / 宽度角度全部不可用")
    else:
        tail = daily.tail(1)
        n_last = _f(tail["n"][0]) if not tail.is_empty() else None
        if n_last is not None:
            metrics.append(metric("active_members", "当日有行情成员数", n_last,
                                  display=f"{n_last:.0f}", unit="家"))

    if valuation_pct is not None and valuation_pct >= 80:
        flags.append(f"行业 PE 处于自身历史 {valuation_pct:.0f}% 分位（偏贵）")
    if crowding_pct is not None and crowding_pct >= 85:
        flags.append(f"成交额占比处于自身历史 {crowding_pct:.0f}% 分位，交易拥挤")

    for note in getattr(universe, "notes", None) or []:
        flags.append(note)

    seen: set[str] = set()
    uniq = [f for f in flags if not (f in seen or seen.add(f))]
    return {"available": bool(metrics), "title": "行业风险提示",
            "metrics": metrics, "flags": uniq, "hint": None}
