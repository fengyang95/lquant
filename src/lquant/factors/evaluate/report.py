"""因子研究报告（自包含 HTML）。

不引任何外部 JS/CSS/CDN —— 报告要能直接邮件发出去、能归档、能在离线环境打开。
图表用内联 SVG 现画，不依赖 ECharts/plotly。

配色遵循 A 股习惯：涨红跌绿（评级徽章除外，它不表示涨跌）。

设计要点（上一轮评审的结论，逐条落地）：

- **报告要能自证身份**：写清因子表达式、股票池、数据区间、样本量、预处理配方、
  协变量覆盖率、样本过滤是否生效 —— 归档三个月后还能读懂。
- **报告要有结论**：开头是「结论」节（评级 + 稳健性），而不是一上来堆指标。
- **不许静默**：任何可选小节算炸了都记进 ``errors`` 并在报告顶部渲染横幅，
  而不是无声消失。
- **口径统一**：IC 族指标一律按原值（4 位小数），比率一律按 ``%``，
  倍数一律 ``x``，布尔一律 是/否。
- **表格不静默截断**：超出上限时显式写「仅显示前 N 行（共 M 行）」。
"""
from __future__ import annotations

import contextlib
import html
import math
from datetime import datetime
from pathlib import Path

import polars as pl

from lquant.factors.evaluate.attribution import attribution_summary
from lquant.factors.evaluate.costs import cost_matrix, factor_turnover
from lquant.factors.evaluate.decay import decay_profile, half_life, suggest_rebalance
from lquant.factors.evaluate.defaults import (
    DEFAULT_EVENT_WINDOW,
    DEFAULT_N_GROUPS,
    DEFAULT_WINDOW,
)
from lquant.factors.evaluate.defaults import decay_horizons as _decay_horizons
from lquant.factors.evaluate.event_study import event_study_summary
from lquant.factors.evaluate.group_ic import ic_by_group
from lquant.factors.evaluate.ic import ic_by_year, ic_series, ic_summary
from lquant.factors.evaluate.outliers import filter_zscore as filter_zscore_df
from lquant.factors.evaluate.outliers import zscore_filter_stats
from lquant.factors.evaluate.quantile import quantile_nav, quantile_summary
from lquant.factors.evaluate.rolling import rolling_ic
from lquant.factors.evaluate.sample import describe_sample_filters
from lquant.factors.universe import universe_label

__all__ = ["factor_report", "save_report", "REPORT_GENERATOR_VERSION"]

# 报告生成器版本：口径或版式发生不兼容变化时递增。
# 归档报告靠它 + 生成时间判断「这份是不是旧口径」。
REPORT_GENERATOR_VERSION = "2.0"

UP = "#C0392B"        # 涨 / 正
DOWN = "#1E8449"      # 跌 / 负
LINE = "#2C6FB5"

_RATING_LABEL = {"strong": "强", "moderate": "中", "weak": "弱"}
_RATING_CLASS = {"strong": "r-strong", "moderate": "r-moderate", "weak": "r-weak"}
_VERDICT_LABEL = {"robust": "稳健", "fragile": "脆弱", "unknown": "未评估"}
_ROBUST_LABELS = {
    "param_sensitivity": "参数扰动",
    "time_stability": "分段稳定",
    "start_date_sensitivity": "起点敏感",
    "best_month_removal": "月度剔除",
    "oos_decay": "样本外衰减",
}
_STATUS_LABEL = {"passed": "通过", "failed": "未过", "skipped": "跳过"}
_SAMPLE_STATUS_LABEL = {
    "applied": "已剔除",
    "unavailable": "数据缺列，未能剔除",
    "available_not_applied": "未剔除（本次口径包含）",
    "not_applied": "未剔除",
}
_STEP_LABELS = {
    "winsorize": "去极值", "standardize": "标准化",
    "neutralize": "中性化", "orthogonalize": "正交化",
}
# 内部列名 → 用户面文案（R14）。报告是给人看的，`cov_industry_sw1`
# 这种 DataFrame 内部列名不该出现在正文里。
_CAT_LABELS = {
    "cov_industry_sw1": "申万一级行业",
    "industry_sw1": "申万一级行业",
    "cov_industry_sw2": "申万二级行业",
    "industry_sw2": "申万二级行业",
    "symbol": "个股",
    "market_cap": "市值分组",
    "cov_market_cap": "市值分组",
    "float_mv": "流通市值",
    "total_mv": "总市值",
    "amount": "成交额",
    "turnover_1m": "换手率分组",
    "cov_turnover_1m": "换手率分组",
    "momentum_1m": "动量分组",
    "cov_momentum_1m": "动量分组",
}


def _cat_label(col: str | None) -> str:
    """归因/分组维度的用户面名称。"""
    return _CAT_LABELS.get(str(col or ""), str(col or ""))


def _ret_label(ret_col: str) -> str:
    """``fwd_ret_5`` → ``5 日前瞻收益``（未知列名原样返回）。"""
    s = str(ret_col)
    if s.startswith("fwd_ret_"):
        h = s[len("fwd_ret_"):]
        if h.isdigit():
            return f"{h} 日前瞻收益"
    return s


# --------------------------------------------------------------------------- #
# 基础格式化
# --------------------------------------------------------------------------- #
def _esc(x) -> str:
    return html.escape(str(x))


def _fmt(v, pct: bool = False, nd: int = 4, *, ratio: bool = False,
         boolean: bool = False) -> str:
    """数值格式化。

    - 布尔 → ``是/否``（``boolean=True``）或原样（兼容旧行为）
    - 整数值 → 不带小数（``2026`` 而不是 ``2026.0000``）
    - ``pct`` → ``12.34%``；``ratio`` → ``41.9x``
    """
    if v is None:
        return "n/a"
    if isinstance(v, bool):
        return ("是" if v else "否") if boolean else _esc(v)
    if isinstance(v, str):
        return _esc(v)
    if not isinstance(v, (int, float)):
        return _esc(v)
    if isinstance(v, float) and not math.isfinite(v):
        return "n/a"
    if ratio:
        return f"{v:.1f}x"
    if pct:
        return f"{v*100:.2f}%"
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e12:
        return str(int(v))
    return f"{v:.{nd}f}"


def _fmt_int(v) -> str:
    """整数值列专用：任何能取整的数都渲染成整数。"""
    if v is None:
        return "n/a"
    if isinstance(v, bool):
        return _esc(v)
    if isinstance(v, (int, float)):
        if isinstance(v, float) and not math.isfinite(v):
            return "n/a"
        return str(int(v))
    return _esc(v)


def _cls(v) -> str:
    if v is None or not isinstance(v, float) or not math.isfinite(v):
        return ""
    return "pos" if v > 0 else "neg" if v < 0 else ""


def _safe(errors: dict, name: str, fn, *args, **kwargs):
    """跑一个可选小节：失败记进 errors 并返回 None，绝不静默吞掉。"""
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        errors[name] = f"{type(e).__name__}: {e}"
        return None


# --------------------------------------------------------------------------- #
# SVG 绘图
# --------------------------------------------------------------------------- #
def _svg_open(w: int, h: int, title: str) -> str:
    t = f"<title>{_esc(title)}</title>" if title else ""
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" '
            f'role="img" aria-label="{_esc(title or "图表")}">{t}')


def _svg_line(xs: list, ys: list, w: int = 640, h: int = 200,
              color: str = LINE, zero: bool = True, label: str = "",
              title: str = "") -> str:
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
    return (_svg_open(w, h, title) + f'''
<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.6"/>
<line x1="{pad}" y1="{zero_y:.1f}" x2="{w-pad}" y2="{zero_y:.1f}" stroke="#BBBBBB" stroke-width="0.8" stroke-dasharray="3 3"/>
<text x="{pad}" y="{h-8}" font-size="11" fill="#888">{_esc(xs[0])}</text>
<text x="{w-pad}" y="{h-8}" font-size="11" fill="#888" text-anchor="end">{_esc(xs[-1])}</text>
<text x="{pad-6}" y="{pad+4}" font-size="11" fill="#888" text-anchor="end">{_fmt(hi, nd=3)}</text>
<text x="{pad-6}" y="{pad+ih}" font-size="11" fill="#888" text-anchor="end">{_fmt(lo, nd=3)}</text>
</svg>''')


def _svg_bars(labels: list, values: list, w: int = 640, h: int = 200,
              title: str = "") -> str:
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
    return (_svg_open(w, h, title) + "".join(bars) +
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
               mark_x: float | None = None, mark_index: int | None = None,
               mark_label: str = "", title: str = "") -> str:
    """多序列内联 SVG 折线图（带图例）。

    ``mark_index`` 按数据下标画竖虚线（事件式收益图用它标事件日 0）——
    由函数自己换算像素，调用方不必复刻布局常量。
    ``mark_x`` 是绝对像素，保留给老调用方。
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
    if mark_index is not None and 0 <= mark_index < len(xs):
        mark_x = px(mark_index)
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
    return (_svg_open(w, h, title) + "".join(parts) + '</svg><div class="legend">' + legend
            + '</div>'
            + (f'<div class="hint">{_esc(mark_label)}</div>' if mark_label else ""))


# --------------------------------------------------------------------------- #
# 表格 / 卡片
# --------------------------------------------------------------------------- #
def _table(df: pl.DataFrame | None, limit: int | None = 20,
           pct_cols: tuple[str, ...] = (), *,
           ratio_cols: tuple[str, ...] = (), bool_cols: tuple[str, ...] = (),
           int_cols: tuple[str, ...] = (), drop_cols: tuple[str, ...] = (),
           caption: str = "") -> str:
    """渲染表格。

    ``limit=None`` 表示不截断；截断时**显式**写出「仅显示前 N 行（共 M 行）」，
    不再静默丢行。``drop_cols`` 由调用方显式指定，不再无条件吞掉 ``nav``。
    """
    if df is None or not len(df):
        return '<div class="empty">无数据</div>'
    drop = set(drop_cols)
    cols = [c for c in df.columns if c not in drop]
    pct, ratio, bools = set(pct_cols), set(ratio_cols), set(bool_cols)
    ints = set(int_cols)
    for c in cols:
        try:
            if df.schema[c].is_integer():
                ints.add(c)
        except Exception:  # noqa: BLE001 - schema 取不到就不做整数优化
            pass

    head = "".join(f"<th>{_esc(c)}</th>" for c in cols)
    shown = df.head(limit) if limit else df
    rows = []
    for r in shown.iter_rows(named=True):
        tds = []
        for c in cols:
            v = r[c]
            if c in bools:
                tds.append(f"<td>{_fmt(v, boolean=True)}</td>")
            elif c in ratio:
                tds.append(f"<td>{_fmt(v, ratio=True)}</td>")
            elif c in ints:
                tds.append(f"<td>{_fmt_int(v)}</td>")
            else:
                tds.append(f"<td>{_fmt(v, pct=(c in pct))}</td>")
        rows.append(f"<tr>{''.join(tds)}</tr>")

    cap = f'<div class="hint">{_esc(caption)}</div>' if caption else ""
    note = (f'<div class="hint">仅显示前 {limit} 行（共 {len(df)} 行）</div>'
            if limit and len(df) > limit else "")
    return (cap + note
            + f'<table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table>')


def _rows_table(rows, **kw) -> str:
    """list[dict] → 表格。"""
    if not rows:
        return ""
    return _table(pl.DataFrame(rows), **kw)


def _card(k: str, v: str, cls: str = "") -> str:
    c = f' class="v {cls}"' if cls else ' class="v"'
    return f'<div class="card"><div class="k">{_esc(k)}</div><div{c}>{v}</div></div>'


def _cards(items: list[str]) -> str:
    return '<div class="cards">' + "".join(items) + "</div>"


def _hint(text: str) -> str:
    return f'<p class="hint">{text}</p>'


# --------------------------------------------------------------------------- #
# 各内容块
# --------------------------------------------------------------------------- #
def _conclusion_html(rating: dict | None, robustness: dict | None,
                     summary: str) -> str:
    """结论节：评级 + 稳健性 + 一句话总结。放在报告最前面。"""
    if not rating and not robustness:
        return ""
    parts = ["<h2>结论</h2>"]
    if summary:
        parts.append(f'<p class="lead">{_esc(summary)}</p>')

    if rating:
        r = str(rating.get("rating") or "weak")
        label = _RATING_LABEL.get(r, r)
        cls = _RATING_CLASS.get(r, "r-weak")
        parts.append(
            '<div class="rating">'
            f'<span class="badge {cls}">{_esc(label)}</span>'
            f'<span class="rating-meta">综合评级 · 依据 {_esc(rating.get("source") or "ic")}'
            f' · n_trials={_fmt_int(rating.get("n_trials"))}</span></div>'
        )
        cards = [
            _card("IC 均值", _fmt(rating.get("ic_mean"))),
            _card("ICIR", _fmt(rating.get("icir"))),
            _card("t 值 (NW)", _fmt(rating.get("t_stat_nw"), nd=2)),
            _card("校正门槛", _fmt(rating.get("t_threshold"), nd=2)),
            _card("分组单调性", _fmt(rating.get("monotonicity"))),
            _card("多空夏普", _fmt(rating.get("ls_sharpe"))),
        ]
        parts.append(_cards(cards))
        if rating.get("reasons"):
            items = "".join(f"<li>{_esc(x)}</li>" for x in rating["reasons"])
            parts.append(f'<div class="why"><b>达标项</b><ul>{items}</ul></div>')
        if rating.get("blockers"):
            items = "".join(f"<li>{_esc(x)}</li>" for x in rating["blockers"])
            parts.append(f'<div class="why why-bad"><b>未达标项</b><ul>{items}</ul></div>')

    if robustness:
        verdict = str(robustness.get("verdict") or "unknown")
        n_passed = robustness.get("n_passed")
        n_judged = robustness.get("n_judged")
        cls = "r-strong" if verdict == "robust" else "r-weak" if verdict == "fragile" \
            else "r-moderate"
        parts.append(
            '<div class="rating">'
            f'<span class="badge {cls}">{_esc(_VERDICT_LABEL.get(verdict, verdict))}</span>'
            f'<span class="rating-meta">L3 稳健性 · 通过 '
            f'{_fmt_int(n_passed)}/{_fmt_int(n_judged)} 项</span></div>'
        )
        rows = []
        for c in robustness.get("checks") or []:
            name = c.get("name") or ""
            rows.append({
                "检验": _ROBUST_LABELS.get(name, name),
                "状态": _STATUS_LABEL.get(str(c.get("status")), str(c.get("status"))),
                "值": c.get("value"),
                "阈值": c.get("threshold"),
                "说明": c.get("hint") or "",
            })
        if rows:
            parts.append(_rows_table(rows))
    elif rating:
        # L3 稳健性没跑 ≠ 稳健性通过。不写清楚，读者会默认「没提就是没问题」。
        parts.append(_hint(
            "L3 稳健性（参数扰动 / 分段稳定 / 起点敏感 / 月度剔除 / OOS 衰减）"
            "本次<b>未运行</b> —— 上面的评级只基于 L2 判据（IC / 分层单调性 / 多空）。"
            "需要 L3 证据请在请求里打开 with_robustness。"))
    return "\n".join(parts)


def _provenance_html(*, display_name: str, expr: str, universe: str,
                     data_start, data_end, n_samples, ret_col: str,
                     steps, covariates, sample_filters, window: int,
                     n_groups: int, generator_version: str,
                     decay_horizons: list[int] | None = None,
                     description: str = "") -> str:
    """样本与口径：报告的身份与前提条件。"""
    uni = universe_label(universe) if universe else "全市场"
    if data_start is not None and data_end is not None:
        rng = f"{data_start} ~ {data_end}"
    elif data_start is not None:
        rng = f"{data_start} 起"
    elif data_end is not None:
        rng = f"至 {data_end}"
    else:
        rng = "未提供"

    if steps:
        recipe = " → ".join(
            f"{_STEP_LABELS.get(str(s.get('op')), str(s.get('op')))}"
            f"({s.get('method') or ''})" for s in steps
        )
    else:
        recipe = "原始因子直接评价（未应用预处理配方）"

    cov = "、".join(f"{k} {_fmt(v, pct=True)}" for k, v in (covariates or {}).items()) \
        or "未提供"

    filter_rows = []
    for f in (sample_filters or []):
        filter_rows.append({
            "过滤项": f.get("label"),
            "状态": _SAMPLE_STATUS_LABEL.get(str(f.get("status")), str(f.get("status"))),
            "数据列": f.get("column"),
        })

    kv = [
        ("因子表达式", expr or display_name),
        ("经济含义", description or "未提供（因子未注册或未填写描述）"),
        ("股票池", uni),
        ("数据区间", rng),
        ("样本量", _fmt_int(n_samples) if n_samples is not None else "未提供"),
        ("前瞻收益列", _ret_label(ret_col)),
        ("分层组数", f"{n_groups} 组"),
        ("衰减阶梯", "、".join(f"{h} 日" for h in decay_horizons)
         if decay_horizons else "未提供"),
        ("滚动窗口", f"{window} 交易日"),
        ("预处理配方", recipe),
        ("协变量覆盖率", cov),
        ("生成器版本", generator_version),
    ]
    rows = "".join(f'<dt>{_esc(k)}</dt><dd>{_esc(v)}</dd>' for k, v in kv)
    html_parts = ["<h2>样本与口径</h2>", f'<dl class="kv">{rows}</dl>']
    if filter_rows:
        html_parts.append("<h3>样本过滤</h3>")
        html_parts.append(_hint(
            "ST / 停牌剔除的开关状态与依据列 —— 打开会改变 IC 与分层口径，"
            "因此默认关闭，且必须在此披露到底剔没剔。"))
        html_parts.append(_rows_table(filter_rows))
    else:
        html_parts.append(_hint("样本过滤：未做 ST / 停牌剔除。"))
    return "\n".join(html_parts)


def _errors_html(errors: dict) -> str:
    """故障可见：把算炸的小节列出来，而不是让它无声消失。"""
    if not errors:
        return ""
    items = "".join(
        f"<li><code>{_esc(k)}</code>：{_esc(v)}</li>" for k, v in sorted(errors.items())
    )
    return ('<h2>本节生成失败</h2>'
            '<div class="errbox"><p>以下小节在生成时抛错，内容缺失 —— '
            '这不是「没有数据」，是「算炸了」：</p>'
            f'<ul>{items}</ul></div>')


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def factor_report(df: pl.DataFrame, factor: str, ret_col: str = "fwd_ret_1", *,
                  price_col: str = "close", n_groups: int = DEFAULT_N_GROUPS,
                  date_col: str = "trade_date", symbol_col: str = "symbol",
                  horizons: list[int] | None = None,
                  cat_col: str | None = None,
                  group_col: str | None = None,
                  bps_list: list[float] | None = None,
                  universe: str = "",
                  filter_zscore: float | None = None,
                  outlier_stats: dict | None = None,
                  event_window: tuple[int, int] | None = DEFAULT_EVENT_WINDOW,
                  # ---- 身份与口径 ----
                  display_name: str | None = None,
                  expr: str = "",
                  data_start=None, data_end=None, n_samples: int | None = None,
                  steps: list[dict] | None = None,
                  covariates: dict | None = None,
                  sample_filters: list[dict] | None = None,
                  window: int = DEFAULT_WINDOW,
                  description: str = "",
                  # ---- 结论 ----
                  rating: dict | None = None,
                  robustness: dict | None = None,
                  # ---- 调用方已算好的内容块 ----
                  extras: dict | None = None,
                  generator_version: str = REPORT_GENERATOR_VERSION,
                  generated_at: str | None = None) -> str:
    """生成因子研究报告 HTML。

    ``factor`` 是 DataFrame 里的列名；``display_name`` / ``expr`` 是**用户面**
    的因子名与表达式（缺省回退到 ``factor``）—— 报告标题不再印内部列名。

    ``group_col`` 提供且列存在时输出「分组 IC」节（识破市值/行业暴露）；
    ``bps_list`` 提供时输出「成本敏感性」表；
    ``filter_zscore`` 提供时先做截面异常收益过滤（口径同 alphalens）；
    ``event_window=(before, after)`` 输出事件式分层收益图，None 则跳过。

    ``steps`` / ``covariates`` / ``sample_filters`` / ``description`` 用于
    「样本与口径」披露；``rating`` / ``robustness`` 用于「结论」节 ——
    ``robustness=None`` 时报告会写明「L3 未运行」（没跑 ≠ 通过）。

    ``extras`` 承载调用方已经算好的内容块（避免报告重算）：
    ``excess`` / ``top_n`` / ``style_corr`` / ``neutral_ladder`` /
    ``neutral_views`` / ``group_ic_size`` / ``capacity``。

    ``horizons`` 是 IC 衰减的**持有期阶梯**（``None`` → 平台默认，见
    ``defaults.DEFAULT_DECAY_HORIZONS``）。``generated_at`` 可注入以便测试
    与「陈旧报告」判定；缺省取当前时间。

    不变量：``cat_col`` 不得等于 ``symbol_col`` —— 按个股做「行业暴露」没有
    可解释含义，报告会跳过该节并留痕，而不是产出无意义表格。

    任何可选小节抛错都会被记录并在报告顶部渲染「本节生成失败」横幅。
    """
    if factor not in df.columns:
        raise KeyError(f"因子列不存在: {factor}")

    errors: dict[str, str] = {}
    extras = dict(extras or {})
    display_name = display_name or factor
    # 生成时间可控（便于测试与「陈旧报告」判定）：默认取当前本地时间到分钟。
    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M")
    steps = list(steps) if steps else []
    cov = dict(covariates) if covariates else {}
    # sample_filters 既接受「开关 spec」（dict，由报告自己描述），
    # 也接受调用方已经描述好的行（list）。
    if isinstance(sample_filters, dict):
        filters = describe_sample_filters(df, **sample_filters)
    else:
        filters = list(sample_filters) if sample_filters else []

    # ---- 样本过滤（可选，先过滤再算，口径三处一致） ----
    outlier_html = ""
    stats = None
    if filter_zscore is not None:
        stats = _safe(errors, "outlier_stats", zscore_filter_stats,
                      df, threshold=filter_zscore, date_col=date_col)
        filtered = _safe(errors, "outlier_filter", filter_zscore_df,
                         df, threshold=filter_zscore, date_col=date_col)
        if filtered is not None:
            df = filtered
    elif outlier_stats is not None:
        stats = outlier_stats
    if stats is not None:
        outlier_html = (
            f'<h2>样本过滤</h2><p class="hint">已按 |z| &gt; {_fmt(stats.get("threshold"), nd=1)} '
            f'做逐日截面异常收益剔除：{stats.get("n_dropped", 0)} / {stats.get("n_in", 0)} 行'
            f'（{_fmt(stats.get("dropped_rate"), pct=True)}）。'
            f'阈值调小可当敏感性测试用 —— 若结论立刻反转，说明因子靠的是少数异常票。</p>'
        )

    # ---- 核心计算（失败即抛：这些算不出来报告没有意义） ----
    ladder = _decay_horizons(horizons)
    # 数据区间：调用方没给就用帧里实际的起止日 —— 「2026-01-01 起」
    # 不如「2026-01-01 ~ 2026-10-04」能自证样本范围（A3）。
    if date_col in df.columns and len(df):
        with contextlib.suppress(Exception):
            if data_end is None:
                data_end = str(df[date_col].max())
            if data_start is None:
                data_start = str(df[date_col].min())
    ic = ic_summary(df, factor, ret_col, date_col=date_col)
    qs = quantile_summary(df, factor, ret_col, n_groups, date_col=date_col)
    prof = decay_profile(df, factor, ladder, price_col=price_col,
                         date_col=date_col, symbol_col=symbol_col)
    hl = half_life(prof)
    yearly = ic_by_year(df, factor, ret_col, date_col=date_col)
    rw = rolling_ic(df, factor, ret_col, window, date_col=date_col)

    # ---- 可选小节：算炸了要留痕 ----
    # 不变量：归因维度必须是**分类**维度。按个股做「行业暴露」是上一轮
    # A1 的静默错误（写着「越接近 0 说明中性化越干净」，算的却是个股维度），
    # 这里由报告自己守住，而不是靠调用方自觉。
    attr = None
    cc = cat_col or next(
        (c for c in ("industry_sw1", "cov_industry_sw1") if c in df.columns), None)
    if cc is not None:
        if str(cc) == symbol_col:
            errors["attribution"] = (
                f"归因维度 {cc!r} 是个股维度，不是分类维度 —— 已跳过归因分解"
                "（按个股算「行业暴露」没有可解释含义；请传行业/市值等分类列）")
            cc = None
        else:
            attr = _safe(errors, "attribution", attribution_summary,
                         df, factor, ret_col, date_col=date_col, cat_col=cc)

    gi = None
    if group_col and group_col in df.columns:
        gi = _safe(errors, "group_ic", ic_by_group,
                   df, factor, ret_col, group_col, date_col=date_col)
        if gi is not None and len(gi):
            # 按 |IC| 降序：可复现，且把最重要的组排在最前（不再被截断丢掉）
            gi = gi.with_columns(pl.col("ic_mean").abs().alias("_k")).sort(
                "_k", descending=True).drop("_k")

    to = None
    if "symbol" in df.columns:
        to = _safe(errors, "turnover", factor_turnover,
                   df, factor, n_groups, date_col=date_col)
    cm = None
    if bps_list:
        cm = _safe(errors, "cost_matrix", cost_matrix,
                   df, factor, ret_col, bps_list=bps_list, n_groups=n_groups,
                   date_col=date_col)

    # ---- 序列与卡片数据 ----
    icv = ic.get("ic", {})
    ric = ic.get("rank_ic", {})
    s = ic_series(df, factor, ret_col, date_col=date_col)
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

    # ---- 结论的一句话总结（评级 + 稳健性收敛成人话） ----
    summary = _summarize(rating, robustness, ls, hl)

    # ---- 分层净值曲线 ----
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
            f'<div class="chart">{_svg_multi(nav_xs, nav_series, title="分层净值曲线")}</div>'
        )

    # ---- 超额收益（研报三件套之一） ----
    excess_html = _excess_html(extras.get("excess"))

    # ---- Top-N 持仓收缩 ----
    topn_html = _topn_html(extras.get("top_n"))

    # ---- 事件式分层收益（mark_index 由函数自己换算，不再复刻布局常量） ----
    es_html = ""
    if event_window:
        before, after = event_window
        es = _safe(errors, "event_study", event_study_summary,
                   df, factor, price_col, n_groups=n_groups,
                   before=before, after=after,
                   date_col=date_col, symbol_col=symbol_col)
        if es and es["curve"]:
            rel = [str(x) for x in es["rel_periods"]]
            idx0 = es["rel_periods"].index(0) if 0 in es["rel_periods"] else None
            es_svg = _svg_multi(rel, es["curve"], mark_index=idx0,
                                mark_label="虚线 = 事件日（因子截面日）",
                                title="事件式分层收益")
            es_html = (
                f'<h2>事件式分层收益（±{after} 交易日）</h2>'
                f'<p class="hint">以各交易日为事件日，横轴为事件日前后相对天数，'
                f'纵轴为各组平均累计收益（已减当日全市场截面均值）。'
                f'虚线左侧就张开 → 因子在描述既成趋势（滞后）；'
                f'左侧收敛、右侧发散 → 才是干净的预测信号。'
                f'事前/事后发散度比 {_fmt(es["look_ahead_ratio"], nd=2)}</p>'
                f'<div class="chart">{es_svg}</div>'
            )

    # ---- 中性化归因阶梯（方案 §5.3 的「页面核心」） ----
    ladder_html = _ladder_html(extras.get("neutral_ladder"))
    views_html = _views_html(extras.get("neutral_views"))

    # ---- 归因分解（暴露 + 收益贡献 + 分组特征，后两块此前被白算） ----
    attr_html = ""
    exp = attr.get("industry_exposure") if isinstance(attr, dict) else None
    if exp is not None and len(exp):
        attr_html = (
            f'<h2>归因分解 · {_esc(_cat_label(cc))}</h2>'
            f'<p class="hint">多空行业暴露总和 {_fmt(attr["gross_exposure"])}'
            f'（越接近 0 说明中性化越干净）。按 |暴露| 降序，全量展示不截断。</p>'
            + _table(exp, limit=None,
                     pct_cols=("weight_long", "weight_short", "exposure"))
        )
        contrib = attr.get("contribution")
        if contrib is not None and len(contrib):
            attr_html += ('<h3>收益贡献</h3>'
                          '<p class="hint">组内权重 × 组内平均收益 —— 这个因子的收益'
                          '到底来自哪个行业。</p>'
                          + _table(contrib, limit=None, pct_cols=("contribution",)))
        profile = attr.get("group_profile")
        if profile is not None and len(profile):
            attr_html += ('<h3>分组特征</h3>'
                          '<p class="hint">各分位组的市值/换手均值 —— 用来判断因子'
                          '是不是在偷偷赌某个风格。</p>'
                          + _table(profile, limit=None, int_cols=("q",)))

    # ---- 分组 IC（行业） ----
    gi_html = ""
    if gi is not None and len(gi):
        gi_html = (f'<h2>分组 IC · {_esc(_cat_label(group_col))}</h2>'
                   f'<p class="hint">按组分别算 IC：若某组（如小市值）独占全部信号，'
                   f'因子收益其实是该组暴露 —— 全样本 IC 会掩盖这一点。'
                   f'按 |IC| 降序，全量展示不截断。</p>'
                   + _table(gi, limit=None, pct_cols=("positive_rate",)))

    # ---- 分组 IC（市值） ----
    gis_html = _size_ic_html(extras.get("group_ic_size"))

    # ---- 风格相关性 ----
    style_html = _style_html(extras.get("style_corr"))

    # ---- 换手率（含分端） ----
    to_html = ""
    if to is not None and len(to):
        def _mean(col):
            if col not in to.columns:
                return float("nan")
            v = to[col].drop_nulls().mean()
            return float(v) if v is not None else float("nan")

        avg = _mean("turnover_avg")
        long_m = _mean("turnover_long")
        short_m = _mean("turnover_short")
        to_html = ('<h2>换手率</h2>'
                   + _cards([
                       _card("日均换手（两端均值）", _fmt(avg, pct=True)),
                       _card("多头端", _fmt(long_m, pct=True)),
                       _card("空头端", _fmt(short_m, pct=True)),
                       _card("年化换手", _fmt(avg * 252, ratio=True)),
                   ])
                   + '<div class="chart">'
                   + _svg_multi(
                       [str(x) for x in to["date"].to_list()],
                       {"两端均值": to["turnover_avg"].to_list(),
                        "多头端": to["turnover_long"].to_list(),
                        "空头端": to["turnover_short"].to_list()},
                       colors=[LINE, UP, DOWN], title="换手率")
                   + '</div>')

    # ---- 成本敏感性（按 % / x / 是-否 格式化） ----
    cm_html = ""
    if cm is not None and len(cm):
        cm_html = ('<h2>成本敏感性</h2>'
                   '<p class="hint">净收益 = 毛收益扣换手 × 双边成本。'
                   'viable=否 的行表示该成本下策略不可用 —— '
                   '很多高 IC 因子在这里现出原形</p>'
                   + _table(cm, limit=None,
                            pct_cols=("gross_annual", "net_annual"),
                            ratio_cols=("annual_turnover",),
                            bool_cols=("viable",), int_cols=("bps",)))

    # ---- 容量 / 流动性 ----
    cap_html = _capacity_html(extras.get("capacity"))

    # ---- 组装 ----
    body = [
        _conclusion_html(rating, robustness, summary),
        _provenance_html(
            display_name=display_name, expr=expr, universe=universe,
            data_start=data_start, data_end=data_end, n_samples=n_samples,
            ret_col=ret_col, steps=steps, covariates=cov,
            sample_filters=filters, window=window, n_groups=n_groups,
            generator_version=generator_version, decay_horizons=ladder,
            description=description),
        _errors_html(errors),
        outlier_html,
        _core_html(icv, ric, hl, qs, ls),
        f'<h2>累计 IC</h2><div class="chart">{_svg_line(dates, cum_ic, label="累计 IC", title="累计 IC")}</div>',
        _rolling_html(rw, window),
        f'<h2>分层收益（{n_groups} 组，Q{n_groups} 为因子值最高）</h2>'
        f'<div class="chart">{_svg_bars(q_labels, q_rets, title="分层收益")}</div>'
        + _table(pl.DataFrame(groups) if groups else None,
                 pct_cols=("mean_ret", "annual_return", "max_drawdown"),
                 int_cols=("q", "n_periods")),
        nav_html,
        excess_html,
        topn_html,
        es_html,
        f'<h2>IC 衰减</h2>'
        f'<div class="chart">{_svg_line(prof["horizon"].to_list() if len(prof) else [], prof["ic"].to_list() if len(prof) else [], label="IC 衰减", title="IC 衰减")}</div>'
        + _table(prof, pct_cols=("positive_rate",), int_cols=("horizon", "n_days")),
        f'<h2>分年度 IC</h2>'
        f'<div class="chart">{_svg_bars(year_labels, year_ic, title="分年度 IC")}</div>'
        + _table(yearly, pct_cols=("positive_rate",), int_cols=("year", "n_days")),
        ladder_html,
        views_html,
        attr_html,
        gi_html,
        gis_html,
        style_html,
        to_html,
        cm_html,
        cap_html,
    ]
    sections = "\n".join(x for x in body if x)

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="lquant-report-generator" content="{_esc(generator_version)}">
<meta name="lquant-report-generated-at" content="{_esc(generated_at)}">
<title>因子研究报告 · {_esc(display_name)}</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;padding:32px;background:#F7F7F5;color:#1F1F1D;
font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}}
.wrap{{max-width:960px;margin:0 auto}}
h1{{font-size:22px;font-weight:600;margin:0 0 4px}}
h2{{font-size:16px;font-weight:600;margin:32px 0 8px;padding-bottom:6px;border-bottom:1px solid #E3E3DF}}
h3{{font-size:14px;font-weight:600;margin:20px 0 6px;color:#444}}
.sub{{color:#777;font-size:13px;margin-bottom:24px}}
.lead{{font-size:14px;margin:8px 0 12px;color:#333}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:16px 0}}
.card{{background:#fff;border:1px solid #E3E3DF;border-radius:10px;padding:14px 16px}}
.card .k{{font-size:12px;color:#777}}
.card .v{{font-size:20px;font-weight:600;margin-top:4px}}
.pos{{color:{UP}}} .neg{{color:{DOWN}}}
.rating{{display:flex;align-items:center;gap:10px;margin:12px 0 4px}}
.badge{{display:inline-block;padding:3px 12px;border-radius:999px;color:#fff;
font-size:14px;font-weight:600}}
.r-strong{{background:{UP}}} .r-moderate{{background:#B7791F}} .r-weak{{background:#8A8A88}}
.rating-meta{{color:#777;font-size:12px}}
.why{{background:#fff;border:1px solid #E3E3DF;border-radius:8px;padding:10px 14px;margin:8px 0}}
.why ul{{margin:6px 0 0;padding-left:20px}} .why li{{font-size:13px}}
.why-bad{{border-color:#E7C9C4}}
.errbox{{background:#FDF3F2;border:1px solid #E7C9C4;border-radius:8px;
padding:12px 16px;margin:12px 0}}
.errbox ul{{margin:6px 0 0;padding-left:20px}} .errbox li{{font-size:13px}}
.errbox code{{background:#fff;padding:1px 5px;border-radius:4px}}
dl.kv{{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;margin:12px 0;
background:#fff;border:1px solid #E3E3DF;border-radius:8px;padding:14px 16px}}
dl.kv dt{{color:#777;font-size:13px}} dl.kv dd{{margin:0;font-size:13px;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-all}}
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
@media print{{
  body{{padding:0;background:#fff}}
  .wrap{{max-width:none}}
  .card,.chart,table,dl.kv,.why,.errbox{{break-inside:avoid}}
  h2{{break-after:avoid}}
}}
</style></head><body><div class="wrap">
<h1>因子研究报告 · {_esc(display_name)}</h1>
<div class="sub">股票池 {_esc(universe_label(universe) if universe else "全市场")} ·
{_esc(_ret_label(ret_col))} · 生成于 {_esc(generated_at)} ·
报告版本 {_esc(generator_version)}</div>

{sections}

<footer>lquant · 因子评价模块自动生成（报告版本 {_esc(generator_version)}）。
本报告基于历史数据，不构成投资建议。</footer>
</div></body></html>"""


# --------------------------------------------------------------------------- #
# 组装辅助
# --------------------------------------------------------------------------- #
def _summarize(rating: dict | None, robustness: dict | None, ls: dict,
               hl) -> str:
    """把评级 + 稳健性收敛成一句人话。"""
    bits = []
    if rating:
        r = str(rating.get("rating") or "")
        label = _RATING_LABEL.get(r, r)
        if label:
            bits.append(f"综合评级 **{label}**")
        if rating.get("reasons"):
            bits.append("；".join(str(x) for x in rating["reasons"][:2]))
        if rating.get("blockers"):
            bits.append("未达标：" + "；".join(str(x) for x in rating["blockers"][:2]))
    if robustness:
        verdict = str(robustness.get("verdict") or "")
        if verdict in _VERDICT_LABEL:
            bits.append(f"稳健性 {_VERDICT_LABEL[verdict]}"
                        f"（{_fmt_int(robustness.get('n_passed'))}/"
                        f"{_fmt_int(robustness.get('n_judged'))}）")
    if not bits:
        return ""
    txt = "。".join(bits)
    if hl is not None and isinstance(hl, (int, float)) and math.isfinite(hl):
        txt += f"。半衰期 {_fmt(hl, nd=1)} 天"
    ar = ls.get("annual_return") if isinstance(ls, dict) else None
    if ar is not None and isinstance(ar, float) and math.isfinite(ar):
        txt += f"，多空年化 {_fmt(ar, pct=True)}"
    # 用 <b> 替换 markdown 星号（报告不引 markdown）
    return txt.replace("**", "")


def _core_html(icv: dict, ric: dict, hl, qs: dict, ls: dict) -> str:
    cards = [
        _card("IC 均值", _fmt(icv.get("mean")), _cls(icv.get("mean"))),
        _card("RankIC 均值", _fmt(ric.get("mean")), _cls(ric.get("mean"))),
        _card("IR", _fmt(icv.get("ir")), _cls(icv.get("ir"))),
        _card("t 值 (NW)", _fmt(icv.get("t_stat_nw"), nd=2), _cls(icv.get("t_stat_nw"))),
        _card("IC 正比例", _fmt(icv.get("positive_rate"), pct=True)),
        _card("IC 自相关", _fmt(icv.get("ic_autocorr"), nd=3)),
        _card("半衰期", f'{_fmt(hl, nd=1)} 天'),
        _card("分层单调性", _fmt(qs.get("monotonicity"))),
        _card("首尾组收益差", _fmt(qs.get("top_bottom_spread"), pct=True),
              _cls(qs.get("top_bottom_spread"))),
        _card("多空年化", _fmt(ls.get("annual_return"), pct=True), _cls(ls.get("annual_return"))),
        _card("多空波动率", _fmt(ls.get("annual_vol"), pct=True)),
        _card("多空夏普", _fmt(ls.get("sharpe")), _cls(ls.get("sharpe"))),
        _card("多空最大回撤", _fmt(ls.get("max_drawdown"), pct=True)),
        _card("多空 Calmar", _fmt(ls.get("calmar"))),
        _card("多空胜率", _fmt(ls.get("win_rate"), pct=True)),
        _card("多空累计收益", _fmt(ls.get("total_return"), pct=True),
              _cls(ls.get("total_return"))),
    ]
    hint = (f'建议调仓频率：{_esc(suggest_rebalance(hl))} · '
            f'多空夏普 {_fmt(ls.get("sharpe"))} · '
            f'多空最大回撤 {_fmt(ls.get("max_drawdown"), pct=True)}')
    return ('<h2>核心指标</h2>' + _cards(cards) + _hint(hint))


def _rolling_html(rw, window: int) -> str:
    """滚动 IC / RankIC / IR 三条线（文案怎么说就怎么画）。"""
    xs = [str(x) for x in rw["trade_date"].to_list()] if len(rw) else []
    if not len(rw):
        return (f'<h2>滚动窗口（{window} 交易日）</h2>'
                f'<div class="chart"><div class="empty">数据不足：滚动指标</div></div>')
    series = {}
    if "ic_mean" in rw.columns:
        series["滚动 IC"] = rw["ic_mean"].to_list()
    if "rank_ic_mean" in rw.columns:
        series["滚动 RankIC"] = rw["rank_ic_mean"].to_list()
    if "ir" in rw.columns:
        series["滚动 IR"] = rw["ir"].to_list()
    return (f'<h2>滚动窗口（{window} 交易日）</h2>'
            f'<p class="hint">滚动 IC / RankIC / IR —— 全样本指标会掩盖阶段性失效，'
            f'滚动线掉头向下甚至转负就是减仓信号</p>'
            f'<div class="chart">{_svg_multi(xs, series, title="滚动窗口指标")}</div>')


def _excess_html(excess) -> str:
    if not excess:
        return ""
    parts = ['<h2>超额收益</h2>']
    metrics = excess.get("metrics") or {}
    if metrics:
        parts.append(_cards([
            _card("年化超额", _fmt(metrics.get("annual_excess"), pct=True),
                  _cls(metrics.get("annual_excess"))),
            _card("超额夏普", _fmt(metrics.get("excess_sharpe")),
                  _cls(metrics.get("excess_sharpe"))),
            _card("超额最大回撤", _fmt(metrics.get("excess_mdd"), pct=True)),
        ]))
    bench = excess.get("benchmark") or "股票池等权"
    parts.append(_hint(f"基准：{_esc(bench)} · 几何超额口径。"
                       f"稳定上行才是真超额；跟着基准一起跌说明只是 beta。"))
    xs = excess.get("dates") or []
    curves = excess.get("curves") or {}
    if xs and curves:
        parts.append(f'<div class="chart">{_svg_multi([str(x) for x in xs], curves, title="超额净值曲线")}</div>')
    return "\n".join(parts)


def _topn_html(top_n) -> str:
    if not top_n:
        return ""
    return ('<h2>Top-N 持仓收缩</h2>'
            '<p class="hint">只买因子值最高的 N 只：IC 好看不代表集中持仓还能赚钱，'
            'N 越小越考验因子的头部区分度</p>'
            + _rows_table(top_n, limit=None,
                          pct_cols=("annual_return", "annual_excess", "max_drawdown"),
                          ratio_cols=("annual_turnover",), int_cols=("n",)))


def _ladder_html(ladder) -> str:
    if not ladder:
        return ""
    return ('<h2>IC 归因阶梯</h2>'
            '<p class="hint">逐段叠加协变量看 IC 怎么掉：原始 → +市值 → +行业 → +换手率。'
            '如果叠加后 IC 归零，说明这个因子赚的是风格暴露，不是选股能力。</p>'
            + _rows_table(ladder, limit=None, int_cols=("n_days",)))


def _views_html(views) -> str:
    if not views or not isinstance(views, dict):
        return ""
    view = views.get("view")
    parts = ['<h2>中性化视图</h2>']
    if view:
        parts.append(_hint(f"本次口径：{_esc(view)}"))
    rendered = False
    for k, v in views.items():
        if k == "view":
            continue
        if isinstance(v, list) and v and isinstance(v[0], dict):
            parts.append(f"<h3>{_esc(k)}</h3>" + _rows_table(v, limit=None))
            rendered = True
    if not rendered and not view:
        return ""
    return "\n".join(parts)


def _size_ic_html(block) -> str:
    if not block:
        return ""
    rows = block.get("rows") or []
    if not rows:
        return ""
    col = block.get("size_col") or ""
    # 已知列给用户面文案（市值分组 / 成交额…），未知列原样保留以便追溯。
    label = _cat_label(col) if col else "市值分组"
    return (f'<h2>分组 IC · {_esc(label)}</h2>'
            f'<p class="hint">按市值分组算 IC：若信号只来自小市值组，'
            f'说明这个因子其实是市值暴露。</p>'
            + _rows_table(rows, limit=None, int_cols=("n_days",)))


def _style_html(style_corr) -> str:
    if not style_corr or not isinstance(style_corr, dict):
        return ""
    max_abs = style_corr.get("max_abs")
    passed = style_corr.get("passed")
    threshold = style_corr.get("threshold")
    if max_abs is None and not style_corr.get("styles"):
        return ""
    parts = ['<h2>风格相关性体检</h2>']
    parts.append(_cards([
        _card("max|ρ|", _fmt(max_abs, nd=3)),
        _card("阈值", _fmt(threshold, nd=3)),
        _card("判定", _fmt(passed, boolean=True)),
    ]))
    parts.append(_hint("中性化后与市值/换手/动量等风格因子的最大相关性。"
                       "超过阈值说明配方没把风格洗干净，IC 里还混着风格收益。"))
    styles = style_corr.get("styles")
    if isinstance(styles, list) and styles and isinstance(styles[0], dict):
        parts.append(_rows_table(styles, limit=None))
    return "\n".join(parts)


def _capacity_html(capacity) -> str:
    if not capacity or not isinstance(capacity, dict):
        return ""
    parts = ['<h2>容量与流动性</h2>']
    parts.append(_cards([
        _card("组合日均成交额", _fmt(capacity.get("portfolio_adv"), nd=0)),
        _card("日均换手", _fmt(capacity.get("turnover_avg"), pct=True)),
        _card("年化换手", _fmt(capacity.get("annual_turnover"), ratio=True)),
        _card("容量上限（估算）", _fmt(capacity.get("capacity_aum"), nd=0)),
    ]))
    parts.append(_hint(
        f"容量 = 参与率上限 {_fmt(capacity.get('max_participation'), pct=True)} × "
        f"组合日均成交额 / 日均换手。冲击成本用平方根模型"
        f"（系数 {_fmt(capacity.get('impact_coef'), nd=2)}，"
        f"显性成本 {_fmt(capacity.get('base_bps'), nd=1)} bps 单边）。"
        f"这是**量级估算**，不是实测冲击 —— 用来找「多大规模开始明显吃亏」的拐点。"))
    rows = capacity.get("rows") or []
    if rows:
        parts.append(_table(pl.DataFrame(rows), limit=None,
                            pct_cols=("participation", "annual_cost", "net_annual"),
                            bool_cols=("viable",), int_cols=("aum",)))
    return "\n".join(parts)


def save_report(html_str: str, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(html_str, encoding="utf-8")
    return p
