"""各分析角度的计算（纯函数：输入数据帧 → 输出角度结果）。

设计取舍：

- **技术面复用平台指标引擎**（``lquant.indicators``）而不是另写一份 MA/MACD/RSI。
  同一指标体系有两份实现，就会出现「K 线图上的 MACD 和分析页的 MACD 不一致」
  这种查不出来的偏差 —— 单一实现源是硬约束。
- **基本面用行业相对分位，不用绝对阈值**。A 股的比率跨行业不可比（银行 90% 负债率
  是常态，白酒 20% 都算高），而且不同数据源对同一指标的单位约定不一致
  （``financial_pit`` 的 ``unit`` 列常常是 NULL）。分位是**尺度无关**的：
  它绕开了单位歧义，又保住了可比性。
- **消息面不打分**。只有新闻条数、没有情感模型时，任何「利好/利空」打分都是编造。
  该角度只报热度，``score=None`` 不参与综合分。
"""
from __future__ import annotations

import math
from datetime import date

import polars as pl

from lquant.security.contract import (
    BEARISH,
    BULLISH,
    NEUTRAL_SIGNAL,
    angle,
    metric,
    percentile_rank,
    to_score,
    unavailable,
)

#: 技术指标集合（注册名，见 /data/indicators/registry）。
_TECH_INDICATORS = ("ma", "macd", "rsi", "boll", "kdj", "volume_ratio", "turnover_ma")

#: 基本面各指标的评分权重（相对重要度，不是绝对分）。
_FUND_WEIGHTS: dict[str, float] = {
    "roe": 3.0, "roa": 2.0, "roic": 1.5,
    "net_margin": 2.0, "gross_margin": 1.5,
    "debt_to_assets": 1.5, "current_ratio": 1.0, "quick_ratio": 1.0,
    "assets_turn": 1.0,
    "revenue_yoy": 2.5, "profit_yoy": 2.5, "ocf_yoy": 1.5,
}

#: 算行业分位所需的最少同业样本（含自己）。样本太少 → 分位不可信，不评。
_MIN_PEERS = 5

#: 估值自身历史分位所需的最少数据点。
_MIN_VALUATION_HISTORY = 60


def _sig(direction: float, signal: str | None = None) -> str:
    """方向值 → 信号标签。"""
    if signal:
        return signal
    if direction > 0.15:
        return BULLISH
    if direction < -0.15:
        return BEARISH
    return NEUTRAL_SIGNAL


def _f(v) -> float | None:
    """安全转 float（None/NaN/非数 → None）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def _tanh_norm(x: float | None, scale: float) -> float | None:
    """把无界量（收益、比率）压到 -1..1。``scale`` 是「算作满分」的量级。"""
    if x is None or scale <= 0:
        return None
    return math.tanh(x / scale)


def _pct(v: float | None, digits: int = 2, suffix: str = "%") -> str | None:
    if v is None:
        return None
    return f"{v:.{digits}f}{suffix}"


def _ret(close: list[float], n: int) -> float | None:
    """近 n 个交易日的区间收益（不足以 ``n`` 根时返回 None）。"""
    if len(close) <= n:
        return None
    prev, cur = close[-n - 1], close[-1]
    if prev <= 0:
        return None
    return cur / prev - 1.0


def _annualized_vol(returns: list[float], n: int) -> float | None:
    """近 n 日收益率年化标准差（%）。"""
    if len(returns) < max(20, n // 4):
        return None
    win = returns[-n:]
    if len(win) < 2:
        return None
    mean = sum(win) / len(win)
    var = sum((r - mean) ** 2 for r in win) / (len(win) - 1)
    return math.sqrt(var) * math.sqrt(252) * 100.0


def _max_drawdown(close: list[float], n: int | None = None) -> float | None:
    """最大回撤（负数，%）。"""
    seq = close[-n:] if n else close
    if len(seq) < 2:
        return None
    peak = seq[0]
    worst = 0.0
    for c in seq:
        peak = max(peak, c)
        if peak > 0:
            worst = min(worst, c / peak - 1.0)
    return worst * 100.0


# ---------------------------------------------------------------- 技术面


def technical_angle(bars: pl.DataFrame) -> dict:
    """技术面：趋势 / 动量 / 波动 / 量能。"""
    if bars.is_empty() or bars.height < 25:
        return unavailable("technical", "日线不足 25 根，技术指标无法预热")

    from lquant.indicators import compute_many

    df = bars
    required = [c for c in ("trade_date", "open", "high", "low", "close",
                            "volume", "turnover_rate") if c in df.columns]
    df = df.select(required)
    try:
        df = compute_many(df, list(_TECH_INDICATORS))
    except Exception:  # noqa: BLE001 - 指标引擎异常不该打挂整个分析
        return unavailable("technical", "技术指标计算失败", summary="技术指标引擎异常")

    last = df.tail(1).row(0, named=True)
    close_series = [c for c in df["close"].to_list() if c is not None]
    close = close_series[-1] if close_series else None
    if close is None:
        return unavailable("technical", "最新收盘价缺失")

    signals: list[tuple[float, float]] = []
    metrics: list[dict] = []

    def g(key: str) -> float | None:
        return _f(last.get(key))

    # 1) 均线排列：close / ma5 / ma20 / ma60 三对比较
    ma5, ma20, ma60 = g("ma5"), g("ma20"), g("ma60")
    if None not in (ma5, ma20, ma60):
        pairs = [close > ma5, ma5 > ma20, ma20 > ma60]
        direction = (sum(pairs) - 1.5) / 1.5
        signals.append((direction, 3.0))
        arrangement = ("多头排列" if all(pairs) else
                       "空头排列" if not any(pairs) else "均线纠缠")
        metrics.append(metric(
            "ma_trend", "均线排列", display=arrangement, signal=_sig(direction),
            note=f"MA5 {ma5:.2f} · MA20 {ma20:.2f} · MA60 {ma60:.2f}"))
        metrics.append(metric("ma20", "MA20", ma20, display=f"{ma20:.2f}",
                              signal=BULLISH if close > ma20 else BEARISH))
        metrics.append(metric("ma60", "MA60", ma60, display=f"{ma60:.2f}",
                              signal=BULLISH if close > ma60 else BEARISH))

    # 2) MACD：DIF/DEA 相对位置 + 柱状体符号
    dif, dea, hist = g("macd_dif"), g("macd_dea"), g("macd_hist")
    if None not in (dif, dea, hist):
        direction = (0.5 if dif > dea else -0.5) + (0.5 if hist > 0 else -0.5)
        signals.append((direction, 3.0))
        state = "金叉" if dif > dea else "死叉"
        metrics.append(metric("macd", "MACD", display=f"{state} · DIF {dif:.3f}",
                              signal=_sig(direction),
                              note=f"DEA {dea:.3f} · 柱 {hist:+.3f}"))

    # 3) RSI(14)：50 为多空分界，70/30 为超买超卖
    rsi = g("rsi14")
    if rsi is not None:
        if rsi >= 70:
            direction, note = -0.3, "超买区，追高风险"
        elif rsi <= 30:
            direction, note = 0.3, "超卖区，有反弹诉求但趋势偏弱"
        elif rsi >= 55:
            direction, note = 0.6, "多方占优"
        elif rsi <= 45:
            direction, note = -0.6, "空方占优"
        else:
            direction, note = 0.0, "多空均衡"
        signals.append((direction, 2.0))
        metrics.append(metric("rsi14", "RSI(14)", rsi, display=f"{rsi:.1f}",
                              signal=_sig(direction), note=note))

    # 4) KDJ
    j = g("kdj_j")
    if j is not None:
        if j > 100:
            direction, note = -0.6, "J 值超买"
        elif j < 0:
            direction, note = 0.5, "J 值超卖"
        else:
            direction, note = max(-1.0, min(1.0, (j - 50) / 50)), "以 50 为中枢"
        signals.append((direction, 1.5))
        metrics.append(metric("kdj_j", "KDJ-J", j, display=f"{j:.1f}",
                              signal=_sig(direction), note=note))

    # 5) 布林带位置 %B
    upper, lower = g("boll_upper"), g("boll_lower")
    if None not in (upper, lower) and upper > lower:
        pct_b = (close - lower) / (upper - lower)
        if pct_b > 1:
            direction, note = -0.5, "突破上轨，短线超买"
        elif pct_b < 0:
            direction, note = 0.5, "跌破下轨，短线超卖"
        else:
            direction, note = (pct_b - 0.5) * 2, "轨道内运行"
        signals.append((direction, 1.5))
        metrics.append(metric("boll_pctb", "布林带位置", pct_b * 100,
                              display=f"{pct_b * 100:.1f}%", unit="%",
                              signal=_sig(direction), note=note))

    # 6) 动量：20 / 60 日区间收益
    r20, r60 = _ret(close_series, 20), _ret(close_series, 60)
    for label, ret, scale, weight, key in (
        ("20 日收益", r20, 0.15, 2.0, "mom20"),
        ("60 日收益", r60, 0.30, 2.0, "mom60"),
    ):
        if ret is None:
            continue
        direction = _tanh_norm(ret, scale) or 0.0
        signals.append((direction, weight))
        metrics.append(metric(key, label, ret * 100, display=_pct(ret * 100),
                              unit="%", signal=_sig(direction)))

    # 7) 量能：量比 + 换手率相对均线，方向由当日涨跌决定
    vol_ratio, to_ma5 = g("volume_ratio"), g("turnover_ma5")
    to_rate = _f(last.get("turnover_rate"))
    chg = None
    if close_series and len(close_series) > 1 and close_series[-2] > 0:
        chg = close_series[-1] / close_series[-2] - 1.0
    if vol_ratio is not None and chg is not None:
        intensity = min(1.0, abs(vol_ratio - 1.0))
        direction = math.copysign(intensity, chg) if abs(vol_ratio - 1.0) > 0.2 else 0.0
        signals.append((direction, 1.5))
        metrics.append(metric("volume_ratio", "量比", vol_ratio,
                              display=f"{vol_ratio:.2f}",
                              signal=_sig(direction),
                              note="放量" if vol_ratio > 1.2 else
                                   "缩量" if vol_ratio < 0.8 else "平量"))
    if to_rate is not None and to_ma5 is not None:
        metrics.append(metric("turnover_rate", "换手率", to_rate,
                              display=f"{to_rate:.2f}%", unit="%",
                              signal=BULLISH if to_rate > to_ma5 else NEUTRAL_SIGNAL,
                              note=f"5 日均 {to_ma5:.2f}%"))

    score = to_score(signals)
    if score is None:
        return unavailable("technical", "技术指标均未预热完成")
    coverage = min(1.0, len(signals) / 8.0)
    bull = sum(1 for d, _ in signals if d > 0.15)
    bear = sum(1 for d, _ in signals if d < -0.15)
    summary = (f"{len(signals)} 项技术信号：{bull} 多 / {bear} 空 / "
               f"{len(signals) - bull - bear} 中性")
    return angle("technical", available=True, summary=summary, metrics=metrics,
                 score=score, coverage=coverage,
                 extra={"n_signals": len(signals), "bullish": bull, "bearish": bear})


# ---------------------------------------------------------------- 基本面


def _latest_financials(fin: pl.DataFrame) -> dict[str, dict]:
    """本票 PIT 财务 → ``{item: {value, stat_date, pub_date}}``（每 item 取最新一期）。"""
    if fin.is_empty():
        return {}
    out: dict[str, dict] = {}
    for item, grp in fin.group_by("item"):
        key = item[0]
        row = grp.sort(["stat_date", "pub_date"]).tail(1).row(0, named=True)
        out[key] = {"value": _f(row.get("value")),
                    "stat_date": row.get("stat_date"),
                    "pub_date": row.get("pub_date")}
    return out


def fundamental_angle(fin: pl.DataFrame, peers: pl.DataFrame,
                      industry: str | None, peer_count: int,
                      canonical: tuple = ()) -> dict:
    """基本面：行业相对分位（PIT）。

    ``canonical`` 是 :data:`lquant.security.loader.CANONICAL_FINANCIAL`，
    由调用方注入以避免本模块反向依赖 loader。
    """
    if fin.is_empty():
        return unavailable(
            "fundamental",
            "没有可用的 PIT 财务数据：先执行 `lq data financial --symbols ...` 回填",
            summary="财务数据为空，基本面无法评估",
        )

    latest = _latest_financials(fin)
    if not latest:
        return unavailable("fundamental", "财务数据缺少可解析的指标项")

    # 同业截面：{item: [值...]}
    peer_values: dict[str, list[float]] = {}
    if not peers.is_empty():
        for item, grp in peers.group_by("item"):
            peer_values[item[0]] = [v for v in grp["value"].to_list() if _f(v) is not None]

    signals: list[tuple[float, float]] = []
    metrics: list[dict] = []
    n_scored = 0

    for key, label, names, higher_better in canonical:
        weight = _FUND_WEIGHTS.get(key, 1.0)
        # 同一口径可能有多个候选名，取第一个有值的
        item = next((n for n in names if n in latest and latest[n]["value"] is not None), None)
        if item is None:
            continue
        val = latest[item]["value"]
        ref = peer_values.get(item, [])
        # 候选名可能各自都有数据，合并所有候选的同伴样本以扩大比较面
        for n in names:
            if n != item:
                ref = ref + peer_values.get(n, [])
        pct = percentile_rank(ref, val) if len(ref) >= _MIN_PEERS else None
        if pct is not None:
            pct = pct if higher_better else 100.0 - pct
            direction = (pct - 50.0) / 50.0
            signals.append((direction, weight))
            n_scored += 1
        metrics.append(metric(
            f"fund_{key}", label, val,
            display=_pct(val) if abs(val) < 1000 else f"{val:,.0f}",
            unit="%",
            percentile=pct,
            signal=_sig((pct - 50.0) / 50.0) if pct is not None else None,
            note=(f"行业分位 {pct:.0f}%" if pct is not None
                  else f"同业样本不足（{len(ref)} < {_MIN_PEERS}）"),
        ))

    if not metrics:
        return unavailable("fundamental", "财务数据里没有本平台支持的口径",
                           summary="财务数据口径不匹配")

    period = latest.get("indicator.roe", {}).get("stat_date")
    score = to_score(signals)
    if score is None:
        hint = f"同业样本不足，无法计算{industry or '行业'}相对分位"
        return angle("fundamental", available=False, summary=hint, hint=hint,
                     metrics=metrics, coverage=0.0,
                     extra={"period": str(period) if period else None})

    coverage = n_scored / max(1, len(canonical))
    top = sorted(
        ((m["label"], m["percentile"]) for m in metrics if m.get("percentile") is not None),
        key=lambda t: t[1], reverse=True)
    best = top[0] if top else None
    worst = top[-1] if top else None
    bits = []
    if best:
        bits.append(f"最强 {best[0]}（行业 {best[1]:.0f}%）")
    if worst and worst != best:
        bits.append(f"最弱 {worst[0]}（{worst[1]:.0f}%）")
    summary = (f"基于 {n_scored} 项指标的{industry or '行业'}相对分位"
               + ("：" + " · ".join(bits) if bits else ""))
    return angle("fundamental", available=True, summary=summary, metrics=metrics,
                 score=score, coverage=coverage,
                 extra={"n_scored": n_scored, "n_peer_symbols": peer_count,
                        "industry": industry,
                        "period": str(period) if period else None,
                        "pub_date": str(latest.get("indicator.roe", {}).get("pub_date") or "")})


# ---------------------------------------------------------------- 估值

_VALUATION_FIELDS = ("pe_ttm", "pb_mrq", "ps_ttm", "dv_ttm", "total_mv", "turnover_rate")


def valuation_angle(own: pl.DataFrame, cross: pl.DataFrame) -> dict:
    """估值：自身历史分位 + 当日全市场分位。"""
    if own.is_empty():
        return unavailable(
            "valuation",
            "没有估值数据（daily_basic 湖为空）：先同步估值/市值数据",
            summary="估值数据为空",
        )
    own = own.sort("trade_date")
    last = own.tail(1).row(0, named=True)
    metrics: list[dict] = []
    signals: list[tuple[float, float]] = []

    # 亏损股 PE 无意义 → 不评分（但如实显示）

    for field, label, weight, cheaper_is_better, unit in (
        ("pe_ttm", "PE(TTM)", 2.0, True, "倍"),
        ("pb_mrq", "PB(MRQ)", 2.0, True, "倍"),
        ("ps_ttm", "PS(TTM)", 1.0, True, "倍"),
        ("dv_ttm", "股息率(TTM)", 1.5, False, "%"),
    ):
        val = _f(last.get(field))
        series = [v for v in own[field].to_list() if _f(v) is not None] \
            if field in own.columns else []
        # PE 为负（亏损）时分位无意义
        usable = not (field == "pe_ttm" and val is not None and val <= 0)
        own_pct = (percentile_rank(series, val)
                   if usable and val is not None and len(series) >= _MIN_VALUATION_HISTORY
                   else None)
        mkt_pct = None
        if usable and val is not None and not cross.is_empty() and field in cross.columns:
            mkt_vals = [_f(v) for v in cross[field].to_list()]
            mkt_pct = percentile_rank([v for v in mkt_vals if v is not None], val)
        if own_pct is not None:
            # 分位低 ≠ 差：PE 处在自身历史 10% 分位意味着**便宜**，是利多。
            # 所以要先按「越大越好」翻正，再映射成方向。
            eff = (100.0 - own_pct) if cheaper_is_better else own_pct
            signals.append(((eff - 50.0) / 50.0, weight))
        metrics.append(metric(
            f"val_{field}", label, val,
            display=(f"{val:.2f}" if val is not None else None),
            unit=unit,
            percentile=own_pct,
            signal=(_sig((((100.0 - own_pct) if cheaper_is_better else own_pct) - 50.0) / 50.0)
                    if own_pct is not None else None),
            note=(f"自身历史分位 {own_pct:.0f}%"
                  + (f" · 全市场 {mkt_pct:.0f}%" if mkt_pct is not None else "")
                  if own_pct is not None else
                  ("亏损，PE 不适用" if field == "pe_ttm" and val is not None and val <= 0
                   else "历史样本不足")),
        ))

    total_mv = _f(last.get("total_mv"))
    if total_mv is not None:
        metrics.append(metric("total_mv", "总市值", total_mv,
                              display=f"{total_mv / 1e8:,.0f} 亿", unit="元",
                              note="规模参考，不参与估值打分"))

    score = to_score(signals)
    if score is None:
        hint = (f"估值历史不足 {_MIN_VALUATION_HISTORY} 个交易日，无法算分位")
        return angle("valuation", available=True, summary=hint, hint=hint,
                     metrics=metrics, score=None, coverage=0.0,
                     extra={"data_date": str(last.get("trade_date") or "")})

    coverage = min(1.0, len(signals) / 4.0)
    lower = sum(1 for m in metrics
                if m.get("percentile") is not None and m["percentile"] <= 30)
    upper = sum(1 for m in metrics
                if m.get("percentile") is not None and m["percentile"] >= 70)
    summary = (f"4 项估值指标中 {lower} 项处自身历史低位 · {upper} 项处高位"
               if (lower or upper) else "估值处于自身历史中枢附近")
    return angle("valuation", available=True, summary=summary, metrics=metrics,
                 score=score, coverage=coverage,
                 extra={"data_date": str(last.get("trade_date") or "")})


# ---------------------------------------------------------------- 资金面


#: 资金面最少需要几个交易日才评分。主力资金流日间噪声极大，1 天数据折成
#: 「主力持续净流入 99 分」是典型的过度自信 —— 宁可报「样本不足」。
_MIN_FLOW_DAYS = 3


def capital_angle(flow: pl.DataFrame, symbol: str | None = None) -> dict:
    """资金面：主力净流入的强度与持续性。

    ``symbol`` 只用于把「没数据」写成可执行的补救命令 ——
    money_flow 的每日采集只装得下当天净流入榜前列的标的，
    普通标的要么靠历史回填，要么永远看不到资金面。
    """
    if flow.is_empty():
        if symbol:
            hint = (f"资金流未覆盖 {symbol}：每日采集只装当天净流入榜前列的标的，"
                    f"普通标的需回填历史 —— 跑 "
                    f"`lq data money-flow --symbols {symbol} --days 120` 后重看")
            return unavailable("capital", hint, summary="资金流数据为空")
        return unavailable("capital", "没有资金流数据（money_flow 表为空或未覆盖该标的）",
                           summary="资金流数据为空")
    flow = flow.sort("trade_date")
    ratios = [_f(v) for v in flow["main_net_ratio"].to_list()]
    ratios = [v for v in ratios if v is not None]
    inflows = [_f(v) for v in flow["main_net_inflow"].to_list()]
    inflows = [v for v in inflows if v is not None]
    days = flow.height

    signals: list[tuple[float, float]] = []
    metrics: list[dict] = []

    # 观测天数不足 → 只展示事实，不给分（噪声会被当成信号）
    if days < _MIN_FLOW_DAYS:
        metrics.append(metric("flow_days", "资金流观测天数", days,
                              display=f"{days} 天",
                              note=f"少于 {_MIN_FLOW_DAYS} 天，样本不足，不评分"))
        if inflows:
            metrics.append(metric("flow_cum", "累计主力净流入", sum(inflows),
                                  display=f"{sum(inflows) / 1e8:+.2f} 亿", unit="元"))
        hint = (f"资金流仅 {days} 个交易日，样本不足 {_MIN_FLOW_DAYS} 天，"
                "单日噪声大，不参与评分")
        return angle("capital", available=True, summary=hint, hint=hint,
                     metrics=metrics, score=None, coverage=days / 20.0,
                     extra={"days": days, "last_date": str(flow["trade_date"].max())})

    if ratios:
        win = ratios[-5:] if len(ratios) >= 5 else ratios
        mean_ratio = sum(win) / len(win)
        # 置信度随观测天数上升：3 天只给一半权重，10 天以上给满
        confidence = min(1.0, days / 10.0)
        direction = (_tanh_norm(mean_ratio, 5.0) or 0.0) * confidence
        signals.append((direction, 3.0))
        metrics.append(metric("flow_ratio", f"主力净占比（{len(win)} 日均）", mean_ratio,
                              display=f"{mean_ratio:+.2f}%", unit="%",
                              signal=_sig(direction),
                              note="净流入" if mean_ratio > 0 else "净流出"))
        pos = sum(1 for r in win if r > 0)
        consistency = pos / len(win) * 100
        metrics.append(metric("flow_consistency", "净流入天数占比", consistency,
                              display=f"{consistency:.0f}%", unit="%",
                              signal=(BULLISH if consistency >= 60 else
                                      BEARISH if consistency <= 40 else NEUTRAL_SIGNAL),
                              note=f"{pos}/{len(win)} 天为正"))

    if inflows:
        cum5 = sum(inflows[-5:]) if len(inflows) >= 5 else sum(inflows)
        metrics.append(metric("flow_cum", "近 5 日主力净流入", cum5,
                              display=f"{cum5 / 1e8:+.2f} 亿", unit="元",
                              signal=BULLISH if cum5 > 0 else BEARISH))

    score = to_score(signals)
    if score is None:
        return angle("capital", available=False,
                     summary="资金流缺少净占比字段，无法评分",
                     hint="资金流缺少净占比字段", metrics=metrics, coverage=0.0)
    coverage = min(1.0, days / 20.0)
    verb = "净流入" if score > 55 else "净流出" if score < 45 else "进出均衡"
    summary = f"近 {days} 个交易日主力{verb}"
    return angle("capital", available=True, summary=summary, metrics=metrics,
                 score=score, coverage=coverage,
                 extra={"days": days,
                        "last_date": str(flow["trade_date"].max())})


# ---------------------------------------------------------------- 相对强度


def relative_angle(bars: pl.DataFrame, benchmark: pl.DataFrame,
                   benchmark_symbol: str | None, industry: str | None) -> dict:
    """行业与相对强度：相对基准指数的区间超额收益。"""
    if bars.is_empty():
        return unavailable("relative", "没有日线数据，无法计算相对强度")
    if benchmark.is_empty():
        return unavailable(
            "relative",
            "没有基准指数数据（index_daily 为空）：先同步指数日线",
            summary="缺少基准指数，无法算超额收益",
        )
    s_close = [c for c in bars["close"].to_list() if c is not None]
    # 基准严格夹在个股的交易日区间内。
    # **上界必须夹**：index_daily 往往比个股日线更新（指数当天就有，个股日线要等
    # 回填），只用 ``>= start`` 会让基准的「近 20 日」窗口滑到观察日**之后**，
    # 于是超额收益里混进未来数据 —— 既是前视偏差，又会让同一观察日的分析结果
    # 随着指数继续同步而漂移（同一请求两个答案）。
    start = bars["trade_date"].min()
    end = bars["trade_date"].max()
    b = benchmark.filter(
        (pl.col("trade_date") >= start) & (pl.col("trade_date") <= end)
    ).sort("trade_date")
    b_close = [c for c in b["close"].to_list() if c is not None]
    if len(b_close) < 2:
        return unavailable("relative", "基准指数区间数据不足")

    signals: list[tuple[float, float]] = []
    metrics: list[dict] = []
    for label, n, scale, weight, key in (
        ("20 日超额", 20, 0.10, 2.0, "excess20"),
        ("60 日超额", 60, 0.15, 3.0, "excess60"),
        ("120 日超额", 120, 0.25, 2.0, "excess120"),
    ):
        sr, br = _ret(s_close, n), _ret(b_close, n)
        if sr is None or br is None:
            continue
        excess = (1 + sr) / (1 + br) - 1
        direction = _tanh_norm(excess, scale) or 0.0
        signals.append((direction, weight))
        metrics.append(metric(key, label, excess * 100, display=_pct(excess * 100),
                              unit="%", signal=_sig(direction),
                              note=f"个股 {sr * 100:+.1f}% vs 基准 {br * 100:+.1f}%"))

    if not signals:
        return unavailable("relative", "个股与基准的重叠区间不足")

    score = to_score(signals)
    coverage = min(1.0, len(signals) / 3.0)
    beat = sum(1 for d, _ in signals if d > 0)
    summary = (f"相对 {benchmark_symbol or '基准'}：{beat}/{len(signals)} 个区间跑赢"
               + (f" · 所属{industry}" if industry else ""))
    return angle("relative", available=True, summary=summary, metrics=metrics,
                 score=score, coverage=coverage,
                 extra={"benchmark": benchmark_symbol, "industry": industry})


# ---------------------------------------------------------------- 消息面


def news_angle(news: pl.DataFrame, asof: date) -> dict:
    """消息面：**只报热度，不评分**。

    没有情感模型时，「新闻多 = 利好」是编造。本角度返回 ``score=None``，
    综合分自动跳过它（权重按可用角度重新归一），前端明确标注「不参与评分」。
    """
    if news.is_empty():
        return unavailable(
            "news",
            "近 30 天没有关联该标的的新闻（news_item 未覆盖或未采集）",
            summary="无关联新闻",
        )
    metrics: list[dict] = []
    n7 = 0
    dates = []
    for row in news.iter_rows(named=True):
        pub = row.get("published_at")
        if pub is None:
            continue
        d = pub.date() if hasattr(pub, "date") else pub
        dates.append(d)
        if (asof - d).days <= 7:
            n7 += 1
    metrics.append(metric("news_total", "近 30 天新闻", news.height,
                          display=f"{news.height} 条"))
    metrics.append(metric("news_recent", "近 7 天新闻", n7, display=f"{n7} 条"))
    newest = max(dates) if dates else None
    if newest:
        metrics.append(metric("news_latest", "最近一条", display=str(newest),
                              note=f"{(asof - newest).days} 天前"))

    titles = []
    for row in news.head(5).iter_rows(named=True):
        t = row.get("title")
        if t:
            titles.append({"title": t[:120],
                           "published_at": str(row.get("published_at") or "")[:16],
                           "url": row.get("url")})
    return angle(
        "news", available=True,
        summary=f"近 30 天 {news.height} 条关联新闻（近 7 天 {n7} 条）· 仅热度，不参与评分",
        metrics=metrics, score=None, coverage=0.5,
        extra={"recent": titles, "scored": False,
               "note": "缺少情感模型，新闻只做热度统计，不生成多空评分"},
    )


# ---------------------------------------------------------------- 风险（不评分）


def risk_block(bars: pl.DataFrame, benchmark: pl.DataFrame,
               benchmark_symbol: str | None, meta: dict | None = None) -> dict:
    """风险提示：波动 / 回撤 / Beta / 流动性。**不参与打分**，只做警示。"""
    if bars.is_empty() or bars.height < 20:
        return {"available": False, "metrics": [],
                "hint": "日线不足 20 根，无法评估风险", "flags": []}
    close = [c for c in bars["close"].to_list() if c is not None]
    returns = [close[i] / close[i - 1] - 1 for i in range(1, len(close))
               if close[i - 1] > 0]
    metrics: list[dict] = []
    flags: list[str] = []

    vol60 = _annualized_vol(returns, 60)
    vol250 = _annualized_vol(returns, 250)
    if vol60 is not None:
        metrics.append(metric("vol60", "年化波动率（60 日）", vol60,
                              display=f"{vol60:.1f}%", unit="%",
                              note="越高波动越大"))
        if vol60 >= 60:
            flags.append(f"近 60 日年化波动 {vol60:.0f}%，波动显著偏高")
    if vol250 is not None:
        metrics.append(metric("vol250", "年化波动率（250 日）", vol250,
                              display=f"{vol250:.1f}%", unit="%"))

    mdd = _max_drawdown(close)
    if mdd is not None:
        metrics.append(metric("mdd", "区间最大回撤", mdd, display=f"{mdd:.1f}%",
                              unit="%", note="窗口内峰值到谷底"))
        if mdd <= -40:
            flags.append(f"区间最大回撤 {mdd:.0f}%，回撤幅度较大")

    # Beta：与基准日收益的协方差 / 基准方差（按日期对齐）
    if not benchmark.is_empty():
        bm = benchmark.select(["trade_date", "close"]).rename({"close": "bclose"})
        j = bars.select(["trade_date", "close"]).join(bm, on="trade_date", how="inner") \
            .sort("trade_date")
        if j.height >= 30:
            sc = j["close"].to_list()
            bc = j["bclose"].to_list()
            sr = [sc[i] / sc[i - 1] - 1 for i in range(1, len(sc)) if sc[i - 1] > 0]
            br = [bc[i] / bc[i - 1] - 1 for i in range(1, len(bc)) if bc[i - 1] > 0]
            n = min(len(sr), len(br))
            if n >= 30:
                sr, br = sr[-n:], br[-n:]
                mb = sum(br) / n
                ms = sum(sr) / n
                cov = sum((sr[i] - ms) * (br[i] - mb) for i in range(n)) / (n - 1)
                var = sum((x - mb) ** 2 for x in br) / (n - 1)
                if var > 0:
                    beta = cov / var
                    metrics.append(metric("beta", f"Beta（vs {benchmark_symbol}）", beta,
                                          display=f"{beta:.2f}",
                                          note=">1 波动放大，<1 相对抗跌"))
                    if beta >= 1.5:
                        flags.append(f"Beta {beta:.2f}，系统性风险敞口偏高")

    # 流动性：近 20 日日均成交额
    if "amount" in bars.columns:
        amts = [_f(a) for a in bars["amount"].to_list()]
        amts = [a for a in amts if a is not None]
        if amts:
            recent = amts[-20:]
            avg = sum(recent) / len(recent)
            metrics.append(metric("liquidity", "日均成交额（近 20 日）", avg,
                                  display=f"{avg / 1e8:.2f} 亿", unit="元"))
            if avg < 2e7:
                flags.append(f"日均成交额 {avg / 1e8:.2f} 亿，流动性偏弱")

    # 涨跌停 / ST
    if meta and meta.get("is_st"):
        flags.append("ST 标的，退市与波动风险显著高于普通股")
    if "close" in bars.columns and bars.height >= 2:
        limit_days = 0
        cl = bars["close"].to_list()
        for i in range(1, len(cl)):
            if cl[i - 1] and cl[i - 1] > 0 and abs(cl[i] / cl[i - 1] - 1) >= 0.095:
                limit_days += 1
        if limit_days:
            metrics.append(metric("limit_days", "涨跌停天数（窗口内）", limit_days,
                                  display=f"{limit_days} 天",
                                  note="含科创板/创业板 20% 制度差异，仅作参考"))

    if bars.height < 120:
        flags.append(f"仅 {bars.height} 根日线，长期指标（250 日波动/回撤）不可用")

    return {
        "available": True,
        "title": "风险提示",
        "scored": False,
        "metrics": metrics,
        "flags": flags,
        "hint": None,
    }
