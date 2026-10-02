"""因子研究报告（自包含 HTML）。

不引任何外部 JS/CSS/CDN —— 报告要能直接邮件发出去、能归档、能在离线环境打开。
图表用内联 SVG 现画，不依赖 ECharts/plotly。

配色遵循 A 股习惯：涨红跌绿。
"""
from __future__ import annotations

import html
import math
from datetime import datetime
from pathlib import Path

import polars as pl

from lquant.factors.evaluate.attribution import attribution_summary
from lquant.factors.evaluate.costs import cost_matrix, factor_turnover
from lquant.factors.evaluate.decay import decay_profile, half_life, suggest_rebalance
from lquant.factors.evaluate.event_study import event_study_summary
from lquant.factors.evaluate.group_ic import ic_by_group
from lquant.factors.evaluate.ic import ic_by_year, ic_series, ic_summary
from lquant.factors.evaluate.outliers import filter_zscore as filter_zscore_df
from lquant.factors.evaluate.outliers import zscore_filter_stats
from lquant.factors.evaluate.quantile import quantile_nav, quantile_summary
from lquant.factors.evaluate.rolling import rolling_ic

__all__ = ["factor_report", "save_report"]

UP = "#C0392B"        # 涨 / 正
DOWN = "#1E8449"      # 跌 / 负
LINE = "#2C6FB5"


def _esc(x) -> str:
    return html.escape(str(x))


def _fmt(v, pct: bool = False, nd: int = 4) -> str:
    """数值按精度/pct 格式化；非数值（字符串、日期、None）原样输出。"""
    if v is None:
        return "n/a"
    if isinstance(v, str):
        return _esc(v)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return _esc(v)
    if isinstance(v, float) and not math.isfinite(v):
        return "n/a"
    return f"{v*100:.2f}%" if pct else f"{v:.{nd}f}"


def _svg_line(xs: list, ys: list, w: int = 640, h: int = 200,
              color: str = LINE, zero: bool = True, label: str = "") -> str:
    """内联 SVG 折线图。"""
    if not xs or not ys or len(xs) != len(ys):
        return f'<div class="empty">数据不足：{_esc(label)}</div>'
    ysv = [v for v in ys if v is not None and math.isfinite(v)]
    if not ysv:
        return f'<div class="empty">数据不足：{_esc(label)}</div>'
    lo, hi = min(ysv), max(ysv)
    if zero:
        lo, hi = min(lo, 0.0), max(hi, 0.0)
    span = (hi - lo) or 1.0
    pad = 28
    iw, ih = w - pad * 2, h - pad * 2

    def px(i: int) -> float:
        return pad + (iw * i / max(len(xs) - 1, 1))

    def py(v) -> float:
        if v is None or not math.isfinite(v):
            return pad + ih / 2
        return pad + ih * (1 - (v - lo) / span)

    pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(ys))
    zero_y = py(0.0)
    return f'''<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" role="img">
<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.6"/>
<line x1="{pad}" y1="{zero_y:.1f}" x2="{w-pad}" y2="{zero_y:.1f}" stroke="#BBBBBB" stroke-width="0.8" stroke-dasharray="3 3"/>
<text x="{pad}" y="{h-8}" font-size="11" fill="#888">{_esc(xs[0])}</text>
<text x="{w-pad}" y="{h-8}" font-size="11" fill="#888" text-anchor="end">{_esc(xs[-1])}</text>
<text x="{pad-6}" y="{pad+4}" font-size="11" fill="#888" text-anchor="end">{_fmt(hi, nd=3)}</text>
<text x="{pad-6}" y="{pad+ih}" font-size="11" fill="#888" text-anchor="end">{_fmt(lo, nd=3)}</text>
</svg>'''


def _svg_bars(labels: list, values: list, w: int = 640, h: int = 200) -> str:
    """内联 SVG 柱状图，正负分色。"""
    if not labels or not values:
        return '<div class="empty">数据不足</div>'
    vs = [v if v is not None and math.isfinite(v) else 0.0 for v in values]
    lo, hi = min(min(vs), 0.0), max(max(vs), 0.0)
    span = (hi - lo) or 1.0
    pad = 28
    iw, ih = w - pad * 2, h - pad * 2
    bw = iw / len(vs)
    zero_y = pad + ih * (1 - (0 - lo) / span)
    bars = []
    for i, v in enumerate(vs):
        y = pad + ih * (1 - (v - lo) / span)
        top, bot = min(y, zero_y), max(y, zero_y)
        hh = max(bot - top, 0.8)
        color = UP if v >= 0 else DOWN
        bars.append(f'<rect x="{pad + i*bw + bw*0.15:.1f}" y="{top:.1f}" '
                    f'width="{bw*0.7:.1f}" height="{hh:.1f}" fill="{color}" opacity="0.85"/>')
        bars.append(f'<text x="{pad + i*bw + bw/2:.1f}" y="{h-10}" font-size="10" '
                    f'fill="#888" text-anchor="middle">{_esc(labels[i])}</text>')
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" role="img">'
            + "".join(bars) +
            f'<line x1="{pad}" y1="{zero_y:.1f}" x2="{w-pad}" y2="{zero_y:.1f}" '
            f'stroke="#BBBBBB" stroke-width="0.8"/></svg>')


def _palette(n: int) -> list[str]:
    """分位组配色：从绿（低分位）到红（高分位）的渐变，符合 A 股涨红跌绿直觉。"""
    if n <= 1:
        return [LINE]
    out = []
    for i in range(n):
        # 色相 140°（绿）→ 8°（红）
        hue = 140 - 132 * i / (n - 1)
        out.append(f"hsl({hue:.0f},58%,45%)")
    return out


def _svg_multi(xs: list, series: dict[str, list], w: int = 640, h: int = 240,
               *, colors: list[str] | None = None, label: str = "",
               mark_x: float | None = None, mark_label: str = "") -> str:
    """多序列内联 SVG 折线图（带图例）。

    series 为 {名称: 值列表}，与 xs 等长；mark_x 处画一条竖虚线
    （事件式收益图用它标出事件日 0）。
    """
    if not xs or not series:
        return f'<div class="empty">数据不足：{_esc(label)}</div>'
    vals = [v for ys in series.values() for v in ys if v is not None and math.isfinite(v)]
    if not vals:
        return f'<div class="empty">数据不足：{_esc(label)}</div>'
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-12:
        lo, hi = lo - 0.01, hi + 0.01
    span = hi - lo
    pad_l, pad_r, pad_t, pad_b = 46, 10, 12, 26
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b
    colors = colors or _palette(len(series))

    def px(i: int) -> float:
        return pad_l + (iw * i / max(len(xs) - 1, 1))

    def py(v) -> float:
        if v is None or not math.isfinite(v):
            return float("nan")
        return pad_t + ih * (1 - (v - lo) / span)

    parts = []
    for k, (_name, ys) in enumerate(series.items()):
        segs, cur = [], []
        for i, v in enumerate(ys):
            y = py(v)
            if math.isfinite(y):
                cur.append(f"{px(i):.1f},{y:.1f}")
            elif cur:
                segs.append(" ".join(cur))
                cur = []
        if cur:
            segs.append(" ".join(cur))
        for s in segs:
            parts.append(f'<polyline points="{s}" fill="none" stroke="{colors[k % len(colors)]}" '
                         f'stroke-width="1.5"/>')
    if mark_x is not None:
        parts.append(f'<line x1="{mark_x:.1f}" y1="{pad_t}" x2="{mark_x:.1f}" y2="{pad_t+ih}" '
                     f'stroke="#999" stroke-width="0.9" stroke-dasharray="4 3"/>')
    parts.append(f'<line x1="{pad_l}" y1="{py(hi):.1f}" x2="{w-pad_r}" y2="{py(hi):.1f}" '
                 f'stroke="#EEE" stroke-width="0.6"/>')
    parts.append(f'<text x="{pad_l-6}" y="{pad_t+4}" font-size="11" fill="#888" '
                 f'text-anchor="end">{_fmt(hi, nd=3)}</text>')
    parts.append(f'<text x="{pad_l-6}" y="{pad_t+ih}" font-size="11" fill="#888" '
                 f'text-anchor="end">{_fmt(lo, nd=3)}</text>')
    parts.append(f'<text x="{pad_l}" y="{h-8}" font-size="11" fill="#888">{_esc(xs[0])}</text>')
    parts.append(f'<text x="{w-pad_r}" y="{h-8}" font-size="11" fill="#888" '
                 f'text-anchor="end">{_esc(xs[-1])}</text>')
    legend = "".join(
        f'<span class="lg"><i style="background:{colors[k % len(colors)]}"></i>{_esc(name)}</span>'
        for k, name in enumerate(series)
    )
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" role="img">'
            + "".join(parts) + f'</svg><div class="legend">{legend}</div>'
            + (f'<div class="hint">{_esc(mark_label)}</div>' if mark_label else ""))


def _table(df: pl.DataFrame, limit: int = 20, pct_cols: tuple[str, ...] = ()) -> str:
    if df is None or not len(df):
        return '<div class="empty">无数据</div>'
    cols = [c for c in df.columns if c != "nav"]
    head = "".join(f"<th>{_esc(c)}</th>" for c in cols)
    rows = []
    for r in df.head(limit).iter_rows(named=True):
        tds = "".join(f"<td>{_fmt(r[c], pct=(c in pct_cols))}</td>" for c in cols)
        rows.append(f"<tr>{tds}</tr>")
    return f'<table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table>'


def factor_report(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1", *,
                  price_col: str = "close", n_groups: int = 10,
                  date_col: str = "trade_date", symbol_col: str = "symbol",
                  horizons: list[int] | None = None,
                  cat_col: str | None = None,
                  group_col: str | None = None,
                  bps_list: list[float] | None = None,
                  universe: str = "",
                  filter_zscore: float | None = None,
                  outlier_stats: dict | None = None,
                  event_window: tuple[int, int] | None = (10, 15)) -> str:
    """生成因子研究报告 HTML。

    group_col 提供且列存在时输出「分组 IC」节（识破市值/行业暴露）；
    bps_list 提供时输出「成本敏感性」表（net = gross 扣双边换手成本）；
    filter_zscore 提供时先做截面异常收益过滤（口径同 alphalens），
    并在报告里显式交代删掉了多少行；outlier_stats 用于「调用方已过滤」
    的场景（如 API 端先过滤再评价），只展示统计不再重复过滤；
    event_window=(before, after) 输出事件式分层收益图，None 则跳过。
    """
    if factor not in df.columns:
        raise KeyError(f"因子列不存在: {factor}")

    outlier_html = ""
    stats = None
    if filter_zscore is not None:
        stats = zscore_filter_stats(df, threshold=filter_zscore, date_col=date_col)
        df = filter_zscore_df(df, threshold=filter_zscore, date_col=date_col)
    elif outlier_stats is not None:
        stats = outlier_stats
    if stats is not None:
        outlier_html = (
            f'<h2>样本过滤</h2><p class="hint">已按 |z| &gt; {_fmt(stats.get("threshold"), nd=1)} '
            f'做逐日截面异常收益剔除：{stats.get("n_dropped", 0)} / {stats.get("n_in", 0)} 行'
            f'（{_fmt(stats.get("dropped_rate"), pct=True)}）。'
            f'阈值调小可当敏感性测试用 —— 若结论立刻反转，说明因子靠的是少数异常票。</p>'
        )

    ic = ic_summary(df, factor, ret_col, date_col=date_col)
    qs = quantile_summary(df, factor, ret_col, n_groups, date_col=date_col)
    prof = decay_profile(df, factor, horizons, price_col=price_col,
                         date_col=date_col, symbol_col=symbol_col)
    hl = half_life(prof)
    yearly = ic_by_year(df, factor, ret_col, date_col=date_col)
    rw = rolling_ic(df, factor, ret_col, 60, date_col=date_col)

    # 归因维度只认「分类维度」：显式指定 → 原始行业列 → 行业协变量列。
    # 绝不回退到 symbol —— 按个股算出来的「行业暴露」是无意义输出，却被读成
    # 「行业很干净」（历史缺陷：242/242 份 API 生成的报告都是 归因分解 · symbol）。
    # 一个都拿不到时整节不出现，而不是拿个股顶替。
    attr = None
    cc = cat_col or next(
        (c for c in ("industry_sw1", "cov_industry_sw1") if c in df.columns), None)
    if cc is not None:
        try:
            attr = attribution_summary(df, factor, ret_col, date_col=date_col, cat_col=cc)
        except Exception:
            attr = None

    gi = None
    if group_col and group_col in df.columns:
        try:
            gi = ic_by_group(df, factor, ret_col, group_col, date_col=date_col)
        except Exception:
            gi = None
    to = None
    if "symbol" in df.columns:
        try:
            to = factor_turnover(df, factor, n_groups, date_col=date_col)
        except Exception:
            to = None
    cm = None
    if bps_list:
        try:
            cm = cost_matrix(df, factor, ret_col, bps_list=bps_list,
                             n_groups=n_groups, date_col=date_col)
        except Exception:
            cm = None

    icv = ic.get("ic", {})
    ric = ic.get("rank_ic", {})
    s = ic_series(df, factor, ret_col, date_col=date_col)   # ic_summary 不再返回 series
    dates = s[date_col].to_list() if len(s) else []
    ic_vals = s["ic"].to_list() if len(s) else []
    cum_ic = []
    acc = 0.0
    for v in ic_vals:
        acc += v if v is not None and math.isfinite(v) else 0.0
        cum_ic.append(acc)

    ls = qs.get("long_short", {})
    groups = qs.get("groups", [])
    q_labels = [f"Q{g['q']}" for g in groups]
    q_rets = [g["mean_ret"] for g in groups]

    year_labels = [str(y) for y in yearly["year"].to_list()] if len(yearly) else []
    year_ic = yearly["ic_mean"].to_list() if len(yearly) else []

    # 分层净值曲线：一次 pivot 出所有组，不再逐组 filter
    qnav = quantile_nav(df, factor, ret_col, n_groups, date_col=date_col)
    nav_xs, nav_series, nav_html = [], {}, ""
    if len(qnav):
        nav_xs = [str(x) for x in qnav[date_col].to_list()]
        nav_series = {f"Q{q}": qnav[f"q{q}"].to_list() for q in range(1, n_groups + 1)}
        ls_end = qnav["long_short"].to_list()[-1]
        nav_html = (
            f'<h2>分层净值曲线</h2>'
            f'<p class="hint">Q1 为因子值最低组、Q{n_groups} 为最高组；曲线是否"扇形张开"'
            f'比单看多空年化更能说明单调性。末端多空强弱差 {_fmt(ls_end, nd=3)}</p>'
            f'<div class="chart">{_svg_multi(nav_xs, nav_series)}</div>'
        )

    # 事件式分层收益（alphalens 标志图）：curve 的 x 轴是相对交易日
    es_html = ""
    if event_window:
        before, after = event_window
        try:
            es = event_study_summary(df, factor, price_col, n_groups=n_groups,
                                     before=before, after=after,
                                     date_col=date_col, symbol_col=symbol_col)
        except (KeyError, ValueError):
            es = None
        if es and es["curve"]:
            rel = [str(x) for x in es["rel_periods"]]
            zero_px = None
            if 0 in es["rel_periods"]:
                idx = es["rel_periods"].index(0)
                w, pad_l, pad_r = 640, 46, 10
                zero_px = pad_l + ((w - pad_l - pad_r) * idx / max(len(rel) - 1, 1))
            es_html = (
                f'<h2>事件式分层收益（±{after} 交易日）</h2>'
                f'<p class="hint">以各交易日为事件日，横轴为事件日前后相对天数，'
                f'纵轴为各组平均累计收益（已减当日全市场截面均值）。'
                f'虚线左侧就张开 → 因子在描述既成趋势（滞后）；'
                f'左侧收敛、右侧发散 → 才是干净的预测信号。'
                f'事前/事后发散度比 {_fmt(es["look_ahead_ratio"], nd=2)}</p>'
                f'<div class="chart">{_svg_multi(rel, es["curve"], mark_x=zero_px, mark_label="虚线 = 事件日（因子截面日）")}</div>'
            )

    attr_html = ""
    if attr is not None and len(attr["industry_exposure"]):
        attr_html = (f'<h2>归因分解 · {_esc(cc)}</h2>'
                     f'<p class="hint">多空行业暴露总和 {_fmt(attr["gross_exposure"])}'
                     f'（越接近 0 说明中性化越干净）</p>'
                     + _table(attr["industry_exposure"], pct_cols=("weight_long",
                                                                    "weight_short", "exposure")))

    gi_html = ""
    if gi is not None and len(gi):
        gi_html = (f'<h2>分组 IC · {_esc(group_col)}</h2>'
                   f'<p class="hint">按组分别算 IC：若某组（如小市值）独占全部信号，'
                   f'因子收益其实是该组暴露 —— 全样本 IC 会掩盖这一点</p>'
                   + _table(gi))

    to_html = ""
    if to is not None and len(to):
        to_mean = float(to["turnover_avg"].drop_nulls().mean())
        to_html = (f'<h2>换手率</h2>'
                   f'<div class="cards">'
                   f'<div class="card"><div class="k">日均换手（两端均值）</div>'
                   f'<div class="v">{_fmt(to_mean, pct=True)}</div></div>'
                   f'<div class="card"><div class="k">年化换手</div>'
                   f'<div class="v">{_fmt(to_mean * 252, nd=1)}x</div></div>'
                   f'</div>'
                   f'<div class="chart">{_svg_line(to["date"].to_list(), to["turnover_avg"].to_list(), zero=False, label="换手率")}</div>')

    cm_html = ""
    if cm is not None and len(cm):
        cm_html = ('<h2>成本敏感性</h2>'
                   '<p class="hint">净收益 = 毛收益扣换手 × 双边成本。'
                   'viable=False 的行表示该成本下策略不可用 —— 很多高 IC 因子在这里现出原形</p>'
                   + _table(cm))

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>因子研究报告 · {_esc(factor)}</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;padding:32px;background:#F7F7F5;color:#1F1F1D;
font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}}
.wrap{{max-width:960px;margin:0 auto}}
h1{{font-size:22px;font-weight:600;margin:0 0 4px}}
h2{{font-size:16px;font-weight:600;margin:32px 0 8px;padding-bottom:6px;border-bottom:1px solid #E3E3DF}}
.sub{{color:#777;font-size:13px;margin-bottom:24px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:16px 0}}
.card{{background:#fff;border:1px solid #E3E3DF;border-radius:10px;padding:14px 16px}}
.card .k{{font-size:12px;color:#777}}
.card .v{{font-size:20px;font-weight:600;margin-top:4px}}
.pos{{color:{UP}}} .neg{{color:{DOWN}}}
table{{width:100%;border-collapse:collapse;background:#fff;font-size:13px;
border:1px solid #E3E3DF;border-radius:8px;overflow:hidden}}
th,td{{padding:8px 10px;text-align:right;border-bottom:1px solid #F0F0EE}}
th{{background:#FAFAF8;font-weight:600;color:#555;text-align:right}}
th:first-child,td:first-child{{text-align:left}}
.chart{{background:#fff;border:1px solid #E3E3DF;border-radius:10px;padding:8px;margin:8px 0}}
.legend{{display:flex;flex-wrap:wrap;gap:10px;padding:2px 6px 6px;font-size:12px;color:#666}}
.lg{{display:inline-flex;align-items:center;gap:4px}}
.lg i{{width:10px;height:3px;border-radius:2px;display:inline-block}}
.hint{{color:#777;font-size:12px;margin:4px 0}}
.empty{{color:#999;font-size:13px;padding:16px;background:#fff;
border:1px dashed #DDD;border-radius:8px}}
footer{{margin-top:40px;color:#999;font-size:12px}}
</style></head><body><div class="wrap">
<h1>因子研究报告 · {_esc(factor)}</h1>
<div class="sub">股票池 {_esc(universe or "全部")} · 前瞻收益 {_esc(ret_col)} ·
生成于 {datetime.now().strftime("%Y-%m-%d %H:%M")}</div>

{outlier_html}

<h2>核心指标</h2>
<div class="cards">
<div class="card"><div class="k">IC 均值</div><div class="v {_cls(icv.get("mean"))}">{_fmt(icv.get("mean"))}</div></div>
<div class="card"><div class="k">RankIC 均值</div><div class="v {_cls(ric.get("mean"))}">{_fmt(ric.get("mean"))}</div></div>
<div class="card"><div class="k">IR</div><div class="v {_cls(icv.get("ir"))}">{_fmt(icv.get("ir"))}</div></div>
<div class="card"><div class="k">t 值 (NW)</div><div class="v {_cls(icv.get("t_stat_nw"))}">{_fmt(icv.get("t_stat_nw"), nd=2)}</div></div>
<div class="card"><div class="k">IC 正比例</div><div class="v">{_fmt(icv.get("positive_rate"), pct=True)}</div></div>
<div class="card"><div class="k">半衰期</div><div class="v">{_fmt(hl, nd=1)} 天</div></div>
<div class="card"><div class="k">分层单调性</div><div class="v">{_fmt(qs.get("monotonicity"))}</div></div>
<div class="card"><div class="k">多空年化</div><div class="v {_cls(ls.get("annual_return"))}">{_fmt(ls.get("annual_return"), pct=True)}</div></div>
</div>
<p class="hint">建议调仓频率：{_esc(suggest_rebalance(hl))} · 多空夏普 {_fmt(ls.get("sharpe"))}
 · 多空最大回撤 {_fmt(ls.get("max_drawdown"), pct=True)}</p>

<h2>累计 IC</h2>
<div class="chart">{_svg_line(dates, cum_ic, label="累计 IC")}</div>

<h2>滚动窗口（60 交易日）</h2>
<p class="hint">滚动 RankIC / IC / IR —— 全样本指标会掩盖阶段性失效，
滚动线掉头向下甚至转负就是减仓信号</p>
<div class="chart">{_svg_line(
    [str(x) for x in rw["trade_date"].to_list()] if len(rw) else [],
    rw["rank_ic_mean"].to_list() if len(rw) else [],
    label="滚动 RankIC")}</div>

<h2>分层收益（{n_groups} 组，Q{n_groups} 为因子值最高）</h2>
<div class="chart">{_svg_bars(q_labels, q_rets)}</div>
{_table(pl.DataFrame(groups) if groups else None,
        pct_cols=("mean_ret", "annual_return", "max_drawdown"))}

{nav_html}

{es_html}

<h2>IC 衰减</h2>
<div class="chart">{_svg_line(prof["horizon"].to_list() if len(prof) else [],
                              prof["ic"].to_list() if len(prof) else [], label="IC 衰减")}</div>
{_table(prof)}

<h2>分年度 IC</h2>
<div class="chart">{_svg_bars(year_labels, year_ic)}</div>
{_table(yearly, pct_cols=("ic_mean", "ic_std", "positive_rate"))}

{attr_html}

{gi_html}

{to_html}

{cm_html}

<footer>lquant · 因子评价模块自动生成。本报告基于历史数据，不构成投资建议。</footer>
</div></body></html>"""


def _cls(v) -> str:
    if v is None or not isinstance(v, float) or not math.isfinite(v):
        return ""
    return "pos" if v > 0 else "neg" if v < 0 else ""


def save_report(html_str: str, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(html_str, encoding="utf-8")
    return p
