"""归因 HTML 报告导出：把 /attribution 的 JSON 变成一份自包含 HTML。

设计约束：
- **零外部依赖**：不引 CDN/JS 库，图表全部内联 SVG，离线可开、可直接打印；
- 中文市场配色惯例（涨红跌绿）与前端一致；
- 所有块在数据缺失时显示 note，绝不留空白假装正常。
"""
from __future__ import annotations

import html
import math

__all__ = ["render_attribution_html"]

#: 中文市场惯例：涨红跌绿
_UP = "#c23a3a"
_DOWN = "#2e9e63"
_INK = "#1c2330"
_DIM = "#6b7686"
_LINE = "#d8dde6"
_FACTOR_COLORS = ["#4c6ef5", "#c23a3a", "#2e9e63", "#d9930d", "#845ef7", "#0c8599"]


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _pct(v, digits: int = 2) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(f):
        return "—"
    color = _UP if f > 0 else (_DOWN if f < 0 else _INK)
    return f'<span style="color:{color}">{f * 100:+.{digits}f}%</span>'


def _num(v, digits: int = 2) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
        return f"{f:,.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _table(headers: list[str], rows: list[list[str]], note: str | None = None) -> str:
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    note_html = f'<p class="note">{_esc(note)}</p>' if note else ""
    return (f'<table><thead><tr>{head}</tr></thead><tbody>{body or "<tr><td colspan=\'"
            f"{len(headers)}\' class=\\'empty\\'>无数据</td></tr>"}</tbody></table>{note_html}')


def _line_chart(dates: list[str], series: dict[str, list[float]],
                width: int = 860, height: int = 240) -> str:
    """多序列折线 SVG（自动量程，含零轴）。"""
    pts = [(d, v) for name in series for d, v in zip(dates, series[name], strict=False)
           if isinstance(v, (int, float)) and math.isfinite(v)]
    if len(dates) < 2 or not pts:
        return '<p class="note">数据不足，无图</p>'
    lo = min(0.0, min(v for _, v in pts))
    hi = max(0.0, max(v for _, v in pts))
    if hi - lo < 1e-12:
        hi = lo + 1e-6
    pad = (hi - lo) * 0.08
    lo, hi = lo - pad, hi + pad
    ml, mr, mt, mb = 56, 12, 12, 26
    iw, ih = width - ml - mr, height - mt - mb
    n = len(dates)

    def x(i: int) -> float:
        return ml + i * iw / (n - 1)

    def y(v: float) -> float:
        return mt + ih * (1 - (v - lo) / (hi - lo))

    parts = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
             f'role="img" style="width:100%;height:auto;font-family:inherit">']
    # 网格 + 刻度（5 条）
    for g in range(5):
        v = lo + (hi - lo) * g / 4
        yy = y(v)
        parts.append(f'<line x1="{ml}" y1="{yy:.1f}" x2="{width - mr}" y2="{yy:.1f}" '
                     f'stroke="{_LINE}" stroke-width="1"/>')
        parts.append(f'<text x="{ml - 6}" y="{yy + 4:.1f}" text-anchor="end" '
                     f'font-size="10" fill="{_DIM}">{v * 100:.1f}%</text>')
    # 零轴
    if lo < 0 < hi:
        parts.append(f'<line x1="{ml}" y1="{y(0):.1f}" x2="{width - mr}" y2="{y(0):.1f}" '
                     f'stroke="{_DIM}" stroke-width="1"/>')
    # 日期刻度（首/中/尾）
    for i in {0, n // 2, n - 1}:
        parts.append(f'<text x="{x(i):.1f}" y="{height - 8}" text-anchor="middle" '
                     f'font-size="10" fill="{_DIM}">{dates[i]}</text>')
    for j, (_name, vals) in enumerate(series.items()):
        color = _FACTOR_COLORS[j % len(_FACTOR_COLORS)]
        d = " ".join(f"{x(i):.1f},{y(v):.1f}"
                     for i, v in enumerate(vals)
                     if isinstance(v, (int, float)) and math.isfinite(v))
        parts.append(f'<polyline points="{d}" fill="none" stroke="{color}" '
                     f'stroke-width="1.8" stroke-linejoin="round"/>')
    parts.append("</svg>")
    legend = "".join(
        f'<span class="lg"><i style="background:{_FACTOR_COLORS[j % len(_FACTOR_COLORS)]}"></i>'
        f'{_esc(name)}</span>' for j, name in enumerate(series))
    return "".join(parts) + f'<div class="legend">{legend}</div>'


def _h2(title: str) -> str:
    return f"<h2>{_esc(title)}</h2>"


def render_attribution_html(data: dict) -> str:
    """归因 payload → 自包含 HTML 报告。"""
    run_id = _esc(data.get("run_id", ""))
    risk = data.get("risk") or {}
    style_attr = data.get("style_attr") or {}
    totals = style_attr.get("totals") or {}
    cost = data.get("cost") or {}
    dd = data.get("drawdown") or {}
    ra = data.get("risk_attr") or {}
    profile = data.get("profile") or {}

    # ---- 概览标签 ----
    tags = [
        f"基准 {_esc(risk.get('benchmark', '—'))}",
        f"年化 α {_num(risk.get('alpha_annual'), 2)}",
        f"信息比率 {_num(risk.get('information_ratio'), 2)}",
        f"跟踪误差 {_num(risk.get('tracking_error'), 2)}",
        f"总超额 {_num(risk.get('excess_return'), 2)}",
        f"费用拖累 {_num(cost.get('total_drag'), 2)}%",
        f"特异 α {_num(totals.get('specific'), 2)}%",
    ]
    tags_html = "".join(f'<span class="tag">{t}</span>' for t in tags)

    parts = [f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>回测归因报告 {run_id}</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         color: {_INK}; margin: 32px auto; max-width: 920px; line-height: 1.55; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  h2 {{ font-size: 16px; margin: 32px 0 10px; padding-bottom: 6px; border-bottom: 2px solid {_LINE}; }}
  .meta {{ color: {_DIM}; font-size: 12px; }}
  .tag {{ display: inline-block; background: #f2f4f8; border-radius: 4px;
          padding: 2px 8px; margin: 2px 4px 2px 0; font-size: 12px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 12.5px; margin: 8px 0; }}
  th, td {{ border-bottom: 1px solid {_LINE}; padding: 5px 8px; text-align: right; }}
  th:first-child, td:first-child {{ text-align: left; }}
  th {{ color: {_DIM}; font-weight: 600; }}
  .note {{ color: {_DIM}; font-size: 11.5px; margin: 4px 0; }}
  .empty {{ color: {_DIM}; text-align: center; }}
  .legend {{ margin: 6px 0; font-size: 12px; }}
  .legend .lg {{ margin-right: 14px; }}
  .legend i {{ display: inline-block; width: 10px; height: 10px; border-radius: 2px;
               margin-right: 4px; vertical-align: -1px; }}
  .dd-card {{ border: 1px solid {_LINE}; border-radius: 8px; padding: 12px 14px; margin: 10px 0; }}
  .dd-head {{ font-size: 13px; font-weight: 600; }}
  .cols {{ display: flex; gap: 24px; flex-wrap: wrap; }}
  .cols > div {{ flex: 1 1 260px; }}
  @media print {{ body {{ margin: 12px; }} h2 {{ page-break-after: avoid; }} }}
</style></head><body>
<h1>回测归因报告</h1>
<p class="meta">run_id: {run_id} · 生成于报告请求时刻 · 口径说明见各节 note</p>
<p>{tags_html}</p>"""]

    # ---- 回撤期归因 ----
    parts.append(_h2("回撤期归因"))
    if dd.get("periods"):
        for p in dd["periods"]:
            end_label = p.get("end") or "未收复"
            head = (f'{p["start"]} → {end_label} · 回撤 {p["drawdown"] * 100:.1f}% · '
                    f'{p["days"]} 个交易日 · 区间收益 {p["ret"] * 100:+.2f}%')
            card = [f'<div class="dd-card"><div class="dd-head">{_esc(head)}</div>']
            fk = p.get("factors")
            if fk is not None:
                common, specific = p.get("common"), p.get("specific")
                card.append(f'<p>风格因子合计 {_pct(common)} · '
                            f'特异 alpha {_pct(specific)}（'
                            + " · ".join(f"{_esc(k)} {_pct(v)}" for k, v in
                                         sorted(fk.items(), key=lambda x: x[1]))
                            + "）</p>")
            st, sb = p.get("stock_top") or [], p.get("stock_bottom") or []
            if st or sb:
                card.append('<div class="cols"><div><p class="note">正贡献</p><table><tbody>')
                for s in st[:5]:
                    card.append(f"<tr><td>{_esc(s['symbol'])}</td>"
                                f"<td>{_pct(s['contribution'])}</td></tr>")
                card.append("</tbody></table></div><div><p class='note'>负贡献</p><table><tbody>")
                for s in sb[:5]:
                    card.append(f"<tr><td>{_esc(s['symbol'])}</td>"
                                f"<td>{_pct(s['contribution'])}</td></tr>")
                card.append("</tbody></table></div></div>")
            card.append("</div>")
        parts.append("".join(card))
        parts.append(f'<p class="note">{_esc(dd.get("note", ""))}</p>')
    else:
        parts.append(f'<p class="note">{_esc(dd.get("note", "无回撤期数据"))}</p>')

    # ---- 风格收益归因 ----
    parts.append(_h2("风格收益归因（因子 vs 特异 alpha）"))
    if style_attr.get("dates"):
        series = {"common": style_attr["common_cum"], "specific": style_attr["specific_cum"]}
        for k in style_attr.get("factors", []):
            series[k] = style_attr.get("factor_cum", {}).get(k, [])
        parts.append(_line_chart(style_attr["dates"], series))
        rows = [[_esc(k), _pct(totals.get(k))] for k in style_attr.get("factors", [])]
        rows.append(["<b>因子合计</b>", _pct(totals.get("common"))])
        rows.append(["<b>特异 alpha</b>", _pct(totals.get("specific"))])
        rows.append(["<b>算术累计收益</b>", _pct(totals.get("ret_arith"))])
        parts.append(_table(["因子", "累计贡献"], rows))
    else:
        parts.append(f'<p class="note">{_esc(style_attr.get("note", "风格归因不可用"))}</p>')
    parts.append(f'<p class="note">{_esc(style_attr.get("note", ""))}</p>')

    # ---- 风险归因（方差分解） ----
    parts.append(_h2("风险归因（方差分解，年化）"))
    if ra.get("factors"):
        parts.append(
            '<p>总波动 <b>' + _num(ra.get("vol_total"), 2) + '%</b> = 系统性 '
            + _num(ra.get("vol_common"), 2) + "% + 特异 " + _num(ra.get("vol_specific"), 2)
            + "%（cross " + _num(ra.get("cross_term"), 2) + "%²）</p>")
        rows = [[_esc(f["factor"]), _num(f.get("avg_exposure"), 3),
                 _num(f.get("var_contrib"), 2), _num(f.get("pct"), 1) + "%"]
                for f in ra["factors"]]
        parts.append(_table(["因子", "平均暴露", "方差贡献(%²,年化)", "占系统性"],
                            rows, note=ra.get("note")))
    else:
        parts.append(f'<p class="note">{_esc(ra.get("note", "风险归因不可用"))}</p>')

    # ---- Brinson ----
    br = data.get("brinson") or {}
    parts.append(_h2("Brinson 归因（全期）"))
    if br.get("groups"):
        rows = [[_esc(g["group"]), _pct(g["alloc"]), _pct(g["select"]),
                 _pct(g["interact"]), _pct(g["total"])] for g in br["groups"]]
        rows.append(["<b>合计</b>", "", "", "", _pct(br.get("excess_total"))])
        parts.append(_table(["分组", "配置 α", "选股 α", "交互", "合计"], rows, note=br.get("note")))
    else:
        parts.append('<p class="note">无 Brinson 数据</p>')

    bm = (data.get("brinson_monthly") or {}).get("months") or []
    if bm:
        parts.append(_h2("Brinson 月度分段"))
        rows = []
        for m in bm:
            best = m["groups"][0] if m["groups"] else None
            rows.append([_esc(m["month"]), _pct(m["excess_total"]),
                         _esc(best["group"]) if best else "—",
                         _pct(best["total"]) if best else "—"])
        parts.append(_table(["月份", "超额合计", "贡献最大组", "该组贡献"], rows))

    # ---- 个股贡献 ----
    sc = data.get("stock_contribution") or {}
    parts.append(_h2("个股收益贡献"))
    st, sb = sc.get("top") or [], sc.get("bottom") or []
    if st or sb:
        parts.append('<div class="cols"><div><p class="note">正贡献前 15</p><table><tbody>')
        for s in st:
            parts.append(f"<tr><td>{_esc(s['symbol'])}</td>"
                         f"<td>{_pct(s['contribution'])}</td></tr>")
        parts.append('</tbody></table></div><div><p class="note">负贡献前 15</p><table><tbody>')
        for s in sb:
            parts.append(f"<tr><td>{_esc(s['symbol'])}</td>"
                         f"<td>{_pct(s['contribution'])}</td></tr>")
        parts.append(f"</tbody></table></div></div><p class='note'>参与个股 {sc.get('n_stocks', '—')}</p>")
    else:
        parts.append('<p class="note">无持仓贡献数据</p>')

    # ---- 成本 ----
    parts.append(_h2("成本拖累"))
    fee_days = cost.get("fee_by_day") or []
    if fee_days:
        rows = [[r["date"], _num(r["fee"]), _pct(r["drag"], 4)] for r in fee_days[:20]]
        parts.append(_table(["日期", "费用(¥)", "收益率拖累"], rows,
                            note=f"共 {len(fee_days)} 天有费用，仅列前 20 · 合计 ¥"
                                 f"{_num(cost.get('total_fee'))} · 累计拖累 "
                                 f"{_num(cost.get('total_drag'), 2)}%"))
    else:
        parts.append('<p class="note">无费用记录</p>')

    # ---- 持仓画像（最近一日快照） ----
    conc = profile.get("concentration") or []
    parts.append(_h2("持仓画像（最近一日）"))
    if conc:
        last = conc[-1]
        parts.append(f'<p>HHI {_num(last["hhi"], 3)} · Top5 '
                     f'{_num(last["top5"], 1)}% · Top10 {_num(last["top10"], 1)}% · '
                     f'持仓数 {last["n_pos"]}（{len(conc)} 个交易日）</p>')
        ind = (profile.get("industry") or {}).get("series") or {}
        ind_dates = (profile.get("industry") or {}).get("dates") or []
        if ind and ind_dates:
            i = len(ind_dates) - 1
            rows = sorted(((_esc(k), _num(v[i], 1) + "%") for k, v in ind.items()),
                          key=lambda x: -float(x[1].rstrip("%").replace(",", "")
                                               or 0))[:10]
            parts.append(_table(["行业/板块", "权重"], rows))
        parts.append(f'<p class="note">{_esc(profile.get("note", ""))}</p>')
    else:
        parts.append('<p class="note">无持仓画像数据</p>')

    parts.append("</body></html>")
    return "".join(parts)
