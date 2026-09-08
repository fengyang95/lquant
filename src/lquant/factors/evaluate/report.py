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
from lquant.factors.evaluate.decay import decay_profile, half_life, suggest_rebalance
from lquant.factors.evaluate.ic import ic_by_year, ic_series, ic_summary
from lquant.factors.evaluate.quantile import quantile_summary

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
                  universe: str = "") -> str:
    """生成因子研究报告 HTML。"""
    if factor not in df.columns:
        raise KeyError(f"因子列不存在: {factor}")

    ic = ic_summary(df, factor, ret_col, date_col=date_col)
    qs = quantile_summary(df, factor, ret_col, n_groups, date_col=date_col)
    prof = decay_profile(df, factor, horizons, price_col=price_col,
                         date_col=date_col, symbol_col=symbol_col)
    hl = half_life(prof)
    yearly = ic_by_year(df, factor, ret_col, date_col=date_col)

    attr = None
    cc = cat_col or ("industry_sw1" if "industry_sw1" in df.columns else "symbol")
    if cc in df.columns:
        try:
            attr = attribution_summary(df, factor, ret_col, date_col=date_col, cat_col=cc)
        except Exception:
            attr = None

    icv = ic.get("ic", {})
    ric = ic.get("rank_ic", {})
    s = ic.get("series")
    dates = s[date_col].to_list() if s is not None and len(s) else []
    ic_vals = s["ic"].to_list() if s is not None and len(s) else []
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

    attr_html = ""
    if attr is not None and len(attr["industry_exposure"]):
        attr_html = (f'<h2>归因分解 · {_esc(cc)}</h2>'
                     f'<p class="hint">多空行业暴露总和 {_fmt(attr["gross_exposure"])}'
                     f'（越接近 0 说明中性化越干净）</p>'
                     + _table(attr["industry_exposure"], pct_cols=("weight_long",
                                                                    "weight_short", "exposure")))

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
.hint{{color:#777;font-size:12px;margin:4px 0}}
.empty{{color:#999;font-size:13px;padding:16px;background:#fff;
border:1px dashed #DDD;border-radius:8px}}
footer{{margin-top:40px;color:#999;font-size:12px}}
</style></head><body><div class="wrap">
<h1>因子研究报告 · {_esc(factor)}</h1>
<div class="sub">股票池 {_esc(universe or "全部")} · 前瞻收益 {_esc(ret_col)} ·
生成于 {datetime.now().strftime("%Y-%m-%d %H:%M")}</div>

<h2>核心指标</h2>
<div class="cards">
<div class="card"><div class="k">IC 均值</div><div class="v {_cls(icv.get("mean"))}">{_fmt(icv.get("mean"))}</div></div>
<div class="card"><div class="k">RankIC 均值</div><div class="v {_cls(ric.get("mean"))}">{_fmt(ric.get("mean"))}</div></div>
<div class="card"><div class="k">IR</div><div class="v {_cls(icv.get("ir"))}">{_fmt(icv.get("ir"))}</div></div>
<div class="card"><div class="k">t 值</div><div class="v {_cls(icv.get("t_stat"))}">{_fmt(icv.get("t_stat"), nd=2)}</div></div>
<div class="card"><div class="k">IC 正比例</div><div class="v">{_fmt(icv.get("positive_rate"), pct=True)}</div></div>
<div class="card"><div class="k">半衰期</div><div class="v">{_fmt(hl, nd=1)} 天</div></div>
<div class="card"><div class="k">分层单调性</div><div class="v">{_fmt(qs.get("monotonicity"))}</div></div>
<div class="card"><div class="k">多空年化</div><div class="v {_cls(ls.get("annual_return"))}">{_fmt(ls.get("annual_return"), pct=True)}</div></div>
</div>
<p class="hint">建议调仓频率：{_esc(suggest_rebalance(hl))} · 多空夏普 {_fmt(ls.get("sharpe"))}
 · 多空最大回撤 {_fmt(ls.get("max_drawdown"), pct=True)}</p>

<h2>累计 IC</h2>
<div class="chart">{_svg_line(dates, cum_ic, label="累计 IC")}</div>

<h2>分层收益（{n_groups} 组，Q{n_groups} 为因子值最高）</h2>
<div class="chart">{_svg_bars(q_labels, q_rets)}</div>
{_table(pl.DataFrame(groups) if groups else None,
        pct_cols=("mean_ret", "annual_return", "max_drawdown"))}

<h2>IC 衰减</h2>
<div class="chart">{_svg_line(prof["horizon"].to_list() if len(prof) else [],
                              prof["ic"].to_list() if len(prof) else [], label="IC 衰减")}</div>
{_table(prof)}

<h2>分年度 IC</h2>
<div class="chart">{_svg_bars(year_labels, year_ic)}</div>
{_table(yearly, pct_cols=("ic_mean", "ic_std", "positive_rate"))}

{attr_html}

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
