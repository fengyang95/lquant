"""行业分析编排：取数 → 各角度 → 综合分 → 报告。

对外入口：

- :func:`analyze_industry` —— 一个行业进，一份多角度报告出（详情页）；
- :func:`industry_rotation` —— 全行业横截面轮动榜（列表页）；
- :func:`list_industry_names` —— 行业清单（下拉框 / 索引）。

与 :mod:`lquant.security.service` 的保证一致：

- **不抛业务异常**：数据缺失走 ``available=False`` + ``hint``，只有匹配不到的
  行业名才由 API 层转 404；
- **PIT 安全**：一切以 ``asof`` 为界（见 :mod:`lquant.industry.loader`）；
- **JSON 安全**：返回值可直接 ``json.dumps``（无 NaN/Infinity）。
"""
from __future__ import annotations

from datetime import date

import polars as pl

from lquant.core.report import json_safe
from lquant.industry import angles as A
from lquant.industry import loader as L
from lquant.industry.contract import ANGLES, SCHEMA_VERSION
from lquant.industry.score import build_verdict, composite

#: 免责声明（随报告返回，前端固定展示）。
DISCLAIMER = (
    "本报告由平台按公开数据自动计算，仅供研究参考，不构成任何投资建议。"
    "行业指数为**成分股等权合成**（非交易所或申万官方指数），与行情软件的"
    "行业指数涨跌幅存在口径差异；所有指标均以观察日为界（PIT），不含未来"
    "数据；数据缺失的角度会明确标注，不会用默认值填充。"
)

#: 展示用：区间涨幅最大的成员数。
_TOP_MEMBERS = 5

#: 分析角度数量（用于报告完整性自检）。
_N_ANGLES = len(ANGLES)


def analyze_industry(identifier: str, asof: date | str | None = None,
                     std: str | None = None) -> dict:
    """对单个行业做多角度分析。

    Args:
        identifier: 行业代码（``801780.SI``）或名称（``银行``）。
        asof: 观察日；None 表示用湖内最新交易日。
        std: 行业分类标准（``SW`` 等）；None 自动挑。

    Returns:
        报告 dict（结构见 :data:`SCHEMA_VERSION`）。

    Raises:
        KeyError: 行业名/代码在湖里匹配不到（由 API 层转 404）。
    """
    day = L.resolve_asof(asof)
    uni = L.build_universe(day, std)

    resolved = _resolve(uni, identifier)
    if resolved is None:
        raise KeyError(identifier)
    code, name = resolved

    members = L.member_symbols(uni, code)
    daily = L.industry_daily(uni, code)
    bars = L.industry_bars(uni, code)
    cross = A.industry_return_table(uni)
    val_cross = A.industry_valuation_cross(uni)

    fin = L.load_financials(members, day)
    market_medians = A.concept_market_medians(
        L.load_market_financial_medians(day, list(A.prosperity_items())))
    history = L.load_valuation_history(members, day)
    flow = L.load_money_flow(members, day)
    market_flow = L.load_flow_market(day)
    limit_up = L.load_limit_up(members, day)
    market_amount = L.market_amount_daily(uni)

    angle_results = [
        A.trend_angle(daily, uni.benchmark, uni.benchmark_symbol, cross),
        A.prosperity_angle(fin, market_medians),
        A.valuation_angle(history, val_cross, code),
        A.capital_angle(flow, market_flow, daily, market_amount),
        A.breadth_angle(daily, bars, limit_up),
    ]
    # 契约顺序（不是计算顺序）——前端渲染稳定
    order = {a.id: i for i, a in enumerate(ANGLES)}
    angle_results.sort(key=lambda a: order.get(a["id"], 99))

    rank = _rank_of(cross, code)
    risk = A.risk_block(
        uni, daily, len(members),
        valuation_pct=_metric_value(angle_results, "pe_hist_pct"),
        crowding_pct=_metric_value(angle_results, "crowding_pct"),
    )
    score = composite(angle_results)
    verdict = build_verdict(angle_results, score, risk,
                            {"rank": rank, "notes": uni.notes})

    report = {
        "schema_version": SCHEMA_VERSION,
        "industry": name,
        "industry_code": code,
        "std": uni.std,
        "asof": day.isoformat(),
        "overview": _overview(uni, daily, members, name, code, rank, bars),
        "score": score,
        "verdict": verdict,
        "angles": angle_results,
        "risk": risk,
        "disclaimer": DISCLAIMER,
    }
    return json_safe(report)


def industry_rotation(asof: date | str | None = None, std: str | None = None,
                      window: int = 20) -> dict:
    """全行业轮动榜（横截面）。

    Returns:
        ``{"asof", "std", "window", "rows": [...]}``；每行含区间收益、成员数、
        20 日成交额、PE/PB 中位数，以及**该行在本榜中的排名**。空湖返回
        ``rows=[]`` 而不是报错。
    """
    day = L.resolve_asof(asof)
    uni = L.build_universe(day, std)
    windows = tuple(dict.fromkeys([20, 60, window]))
    cross = A.industry_return_table(uni, windows)
    val = A.industry_valuation_cross(uni)

    if cross.is_empty():
        return {"asof": day.isoformat(), "std": uni.std, "window": window,
                "rows": [], "notes": uni.notes}

    if not val.is_empty():
        cross = cross.join(val, on=["industry_code", "industry_name"], how="left")
    # 估值表缺失时也要有稳定的列 —— 前端按列取值，缺列会让整页白屏
    for col in ("pe_median", "pb_median"):
        if col not in cross.columns:
            cross = cross.with_columns(pl.lit(None, dtype=pl.Float64).alias(col))

    rows = cross.to_dicts()
    key = f"r{window}"
    rows.sort(key=lambda r: (r.get(key) is None, -(r.get(key) or 0.0)))
    n = len(rows)
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
        r["percentile"] = round((n - i + 1) / n * 100.0, 2)
    return json_safe({"asof": day.isoformat(), "std": uni.std, "window": window,
                      "rows": rows, "notes": uni.notes})


def list_industry_names(asof: date | str | None = None,
                        std: str | None = None) -> dict:
    """行业清单（代码 / 名称 / 成员数 / 最近交易日收益）。"""
    day = L.resolve_asof(asof)
    uni = L.build_universe(day, std)
    table = uni.industries
    if table.is_empty():
        return {"asof": day.isoformat(), "std": uni.std, "industries": [],
                "notes": uni.notes}

    lasts: dict[str, float | None] = {}
    if not uni.daily.is_empty():
        # 显式按 trade_date 排序取最后一条：group_by 不承诺组内行序，
        # 靠「先 sort 再 group_by」隐式依赖顺序是等着出 bug。
        tail = (uni.daily.group_by("industry_code")
                .agg(pl.col("ret").sort_by("trade_date").drop_nulls().last()
                     .alias("last_ret")))
        lasts = {r["industry_code"]: r["last_ret"] for r in tail.to_dicts()}
    rows = [{**r, "last_ret": lasts.get(r["industry_code"])}
            for r in table.to_dicts()]
    return json_safe({"asof": day.isoformat(), "std": uni.std,
                      "industries": rows, "notes": uni.notes})


# ---------------------------------------------------------------- 内部


def _resolve(uni: L.IndustryUniverse, identifier: str) -> tuple[str, str] | None:
    """行业代码/名称 → (code, name)，走 PIT 成员表（与列表页同一口径）。"""
    from lquant.core.db import reader

    with reader() as con:
        return L.resolve_industry(con, identifier, uni.asof, uni.std)


def _metric_value(angles: list[dict], key: str) -> float | None:
    """从角度结果里取某个指标的数值（不存在/为 None 返回 None）。"""
    for a in angles:
        for m in a.get("metrics") or []:
            if m.get("key") == key:
                v = m.get("value")
                return float(v) if isinstance(v, (int, float)) else None
    return None


def _rank_of(cross: pl.DataFrame, code: str) -> dict | None:
    """本行业在横截面里的排名与分位（用于结论与概览）。"""
    if cross is None or cross.is_empty() or "r20" not in cross.columns:
        return None
    rows = [r for r in cross.to_dicts() if r.get("r20") is not None]
    if not rows:
        return None
    rows.sort(key=lambda r: -float(r["r20"]))
    n = len(rows)
    for i, r in enumerate(rows, start=1):
        if r["industry_code"] == code:
            return {"rank": i, "n_industries": n,
                    "percentile": round((n - i + 1) / n * 100.0, 2),
                    "r20": r["r20"]}
    return None


def _overview(uni: L.IndustryUniverse, daily: pl.DataFrame, members: list[str],
              name: str, code: str, rank: dict | None,
              bars: pl.DataFrame) -> dict:
    """行业概览：成员数、活跃成员数、当日收益、龙头成员、数据备注。"""
    active = None
    day_ret = None
    if not daily.is_empty():
        tail = daily.sort("trade_date").tail(1)
        if not tail.is_empty():
            n = tail["n"][0]
            active = int(n) if n is not None else None
            r = tail["ret"][0]
            day_ret = float(r) * 100.0 if r is not None else None

    return {
        "industry": name,
        "industry_code": code,
        "std": uni.std,
        "asof": uni.asof.isoformat(),
        "member_count": len(members),
        "active_members": active,
        "day_ret": day_ret,
        "rank": rank,
        "leaders": _leaders(bars),
        "benchmark": uni.benchmark_symbol,
        "notes": uni.notes,
        "n_angles": _N_ANGLES,
    }


def _leaders(bars: pl.DataFrame, top_n: int = _TOP_MEMBERS) -> list[dict]:
    """区间（近 20 交易日）涨幅最大的成员 —— 「这个行业是谁在拉」。"""
    if bars.is_empty() or "close" not in bars.columns:
        return []
    # ``sort_by("trade_date")`` 让「最后收盘价 / 20 日前收盘价」不依赖组内行序
    rows = (bars.group_by("symbol", maintain_order=True)
            .agg(pl.col("close").sort_by("trade_date").alias("closes"))
            .to_dicts())
    scored: list[tuple[str, float, float | None]] = []
    for r in rows:
        cl = [c for c in r["closes"] if c is not None]
        if len(cl) > 20 and cl[-21] and cl[-21] > 0:
            scored.append((r["symbol"], cl[-1] / cl[-21] - 1.0, cl[-1]))
    if not scored:
        return []
    scored.sort(key=lambda t: -t[1])
    top = scored[:top_n]
    names = L.load_security_names([s for s, _, _ in top])
    return [{"symbol": s, "name": names.get(s),
             "ret20": round(r * 100.0, 2), "close": c} for s, r, c in top]


__all__ = [
    "DISCLAIMER",
    "analyze_industry",
    "industry_rotation",
    "list_industry_names",
]
