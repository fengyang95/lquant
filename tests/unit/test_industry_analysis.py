"""行业分析引擎的单元测试。

重点覆盖三类**容易静默出错**的地方（与个股分析的测试纪律一致）：

1. **方向符号**：估值分位「低 = 便宜 = 利多」、拥挤度「高 = 风险 = 扣分」——
   符号写反了分数照样算得出来，只是结论完全相反，不会抛异常；
2. **PIT 边界**：``std_date > trade_date`` 的行业归属不得生效（用今天的分类
   解释历史涨跌 = 前视偏差）；
3. **降级**：空表 / 缺列 / 样本不足时返回 ``available=False`` + ``hint``，
   而不是抛异常或补一个 50 分的假中性。
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import duckdb
import polars as pl
import pytest

from lquant.industry import angles as A
from lquant.industry import contract as C
from lquant.industry import loader as L
from lquant.industry.score import build_verdict, composite

# ---------------------------------------------------------------- 夹具


def _dates(n: int, start: date = date(2025, 1, 1)) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def make_daily(n: int = 260, daily_ret: float = 0.001,
               code: str = "801780.SI", name: str = "银行",
               start_ret: float = 0.0) -> pl.DataFrame:
    """合成行业日度聚合帧（首日收益用于制造 PIT 边界样本）。"""
    rets = [start_ret] + [daily_ret] * (n - 1)
    return pl.DataFrame({
        "trade_date": _dates(n),
        "industry_code": [code] * n,
        "industry_name": [name] * n,
        "ret": rets,
        "median_ret": rets,
        "n": [10] * n,
        "amount": [1e9] * n,
        "turnover_rate": [1.5] * n,
        "up_ratio": [0.6 if daily_ret > 0 else 0.4] * n,
    })


def make_benchmark(n: int = 400, daily_ret: float = 0.0) -> pl.DataFrame:
    """合成基准指数日线（默认横盘 —— 行业收益即超额收益）。"""
    close, closes = 1000.0, []
    for _ in range(n):
        closes.append(close)
        close *= (1 + daily_ret)
    return pl.DataFrame({"trade_date": _dates(n), "close": closes})


def make_bars(n: int = 300, n_symbols: int = 6, code: str = "801780.SI",
              drift: float = 0.001) -> pl.DataFrame:
    """合成成员日线（``high`` / ``low`` / ``close`` / ``ret`` 齐备）。"""
    rows = []
    for s in range(n_symbols):
        close = 10.0 + s
        for i, d in enumerate(_dates(n)):
            prev = close
            close = close * (1 + drift + 0.005 * math.sin((i + s) / 7.0))
            rows.append({
                "trade_date": d, "symbol": f"{600000 + s:06d}.SH",
                "industry_code": code, "industry_name": "银行",
                "close": close, "high": max(prev, close) * 1.01,
                "low": min(prev, close) * 0.99,
                "amount": 1e8, "turnover_rate": 1.0,
                "ret": close / prev - 1.0,
            })
    return pl.DataFrame(rows)


def make_accelerating_daily(n: int = 400, start_ret: float = 0.0,
                            end_ret: float = 0.008,
                            code: str = "801780.SI") -> pl.DataFrame:
    """收益从 ``start_ret`` 线性加速到 ``end_ret``（用于 RRG 象限判据）。

    RRG 的 RS-Momentum 度量的是「相对强度本身在变强还是变弱」，所以**恒定**
    收益率会落在中枢 100 的边界上，只有加速/减速才能稳定落在某个象限。
    """
    rets = [start_ret + (end_ret - start_ret) * i / max(1, n - 1) for i in range(n)]
    return make_daily(n, code=code).with_columns(
        pl.Series("ret", rets), pl.Series("median_ret", rets))


def make_fin(rows: list[tuple[str, str, float, int]]) -> pl.DataFrame:
    """``[(symbol, item, value, rn)]`` → PIT 财务长表。"""
    return pl.DataFrame({
        "symbol": [r[0] for r in rows],
        "item": [r[1] for r in rows],
        "value": [r[2] for r in rows],
        "stat_date": [date(2025, 3, 31)] * len(rows),
        "pub_date": [date(2025, 4, 30)] * len(rows),
        "rn": [r[3] for r in rows],
    })


# ---------------------------------------------------------------- 契约


def test_nonzero_weights_sum_to_one():
    """权重是综合分的唯一尺度来源，加起来不是 1 会让总分系统性偏移。"""
    total = sum(a.weight for a in C.ANGLES if a.weight > 0)
    assert total == pytest.approx(1.0)


def test_angle_ids_unique_and_registered():
    ids = [a.id for a in C.ANGLES]
    assert len(ids) == len(set(ids))
    assert set(C.ANGLE_BY_ID) == set(ids)


def test_grade_labels_are_industry_wording():
    """行业说「强势/弱势」，不能说成个股的「偏多/偏空」。"""
    assert C.grade(90) == "显著强势"
    assert C.grade(50) == "中性"
    assert C.grade(10) == "显著弱势"
    assert "偏多" not in C.grade(90)


def test_composite_renormalizes_available_angles():
    """缺角度的权重必须重新归一，否则总分会被系统性地拉向中性。"""
    angles = [
        C.angle("trend", available=True, summary="x", score=80.0, coverage=1.0),
        C.angle("prosperity", available=False, summary="缺"),
    ]
    out = composite(angles)
    assert out["score"] == 80.0
    assert out["n_scored"] == 1
    assert out["angle_coverage"] == pytest.approx(0.28)
    assert out["grade"] == "显著强势"


def test_composite_without_data_is_not_neutral():
    out = composite([C.unavailable("trend", "没有日线")])
    assert out["score"] is None
    assert out["grade"] == "无法评分"


def test_verdict_reports_missing_angle_hint():
    angles = [C.unavailable("valuation", "没有估值历史")]
    score = composite(angles)
    risk = {"flags": ["行业成员过少"], "metrics": [], "available": True}
    v = build_verdict(angles, score, risk, {"notes": ["湖是空的"]})
    joined = " ".join(v["risks"])
    assert "没有估值历史" in joined
    assert "行业成员过少" in joined
    assert "湖是空的" in joined


# ---------------------------------------------------------------- 数值工具


def test_cum_return_ignores_holes():
    """有效样本不足时必须返回 None，而不是拿 len(rets) 蒙混过去。"""
    assert A._cum_return([0.01] * 30, 20) == pytest.approx(1.01 ** 20 - 1)
    assert A._cum_return([None] * 25 + [0.01] * 5, 20) is None
    assert A._cum_return([0.01] * 5, 5) is None      # 需要 n+1 个点


def test_level_skips_nulls_without_dropping_rows():
    lvl = A._level([None, 0.10, None, 0.10])
    assert len(lvl) == 4
    assert lvl[-1] == pytest.approx(1.21)


def test_median_filters_nan():
    assert A._median([1.0, float("nan"), 3.0]) == 2.0
    assert A._median([]) is None


# ---------------------------------------------------------------- PIT 归属


def test_attach_industry_is_point_in_time():
    """行业变更生效日之前必须仍算旧行业（用今天的分类回测历史 = 前视偏差）。"""
    bars = pl.DataFrame({
        "trade_date": [date(2025, 1, 1), date(2025, 6, 1)],
        "symbol": ["600000.SH", "600000.SH"],
        "close": [10.0, 11.0],
    })
    ic = pl.DataFrame({
        "symbol": ["600000.SH", "600000.SH"],
        "std_date": [date(2010, 1, 1), date(2025, 5, 1)],
        "industry_code": ["801780.SI", "801080.SI"],
        "industry_name": ["银行", "电子"],
    })
    out = L._attach_industry(bars, ic).sort("trade_date")
    assert out["industry_name"].to_list() == ["银行", "电子"]


def test_attach_industry_drops_rows_before_first_classification():
    bars = pl.DataFrame({
        "trade_date": [date(2009, 1, 1)],
        "symbol": ["600000.SH"], "close": [10.0],
    })
    ic = pl.DataFrame({
        "symbol": ["600000.SH"], "std_date": [date(2010, 1, 1)],
        "industry_code": ["801780.SI"], "industry_name": ["银行"],
    })
    assert L._attach_industry(bars, ic).is_empty()


def test_industry_daily_aggregates_equal_weight():
    bars = pl.DataFrame({
        "trade_date": [date(2025, 1, 2)] * 4,
        "symbol": list("abcd"),
        "industry_code": ["801780.SI"] * 4,
        "industry_name": ["银行"] * 4,
        "close": [1.0] * 4, "high": [1.0] * 4, "low": [1.0] * 4,
        "amount": [100.0, 200.0, 300.0, 400.0],
        "turnover_rate": [1.0, 2.0, 3.0, 4.0],
        "ret": [0.01, 0.02, -0.01, 0.0],
    })
    out = L._industry_daily(bars)
    assert out.height == 1
    row = out.row(0, named=True)
    assert row["ret"] == pytest.approx(0.005)
    assert row["amount"] == pytest.approx(1000.0)
    assert row["turnover_rate"] == pytest.approx(2.5)
    assert row["up_ratio"] == pytest.approx(0.5)
    assert row["n"] == 4


def test_industry_daily_tolerates_missing_optional_columns():
    """provider 换源缺 amount/turnover_rate 时补 None，而不是让整条链炸。"""
    bars = pl.DataFrame({
        "trade_date": [date(2025, 1, 2)],
        "symbol": ["a"], "industry_code": ["X"], "industry_name": ["X"],
        "ret": [0.01],
    })
    out = L._industry_daily(bars)
    assert out["amount"][0] is None
    assert out["turnover_rate"][0] is None


# ---------------------------------------------------------------- 趋势与 RRG


def test_trend_rising_industry_scores_above_neutral():
    out = A.trend_angle(make_daily(200, 0.003), make_benchmark(200, 0.0), "000300.SH")
    assert out["available"] is True
    assert out["score"] > 50


def test_trend_falling_industry_scores_below_neutral():
    out = A.trend_angle(make_daily(200, -0.003), make_benchmark(200, 0.0), "000300.SH")
    assert out["score"] < 50


def test_trend_excess_beats_absolute_when_benchmark_rises_faster():
    """行业涨 0.3%/日但基准涨 0.5%/日 → 绝对收益为正、超额为负。"""
    out = A.trend_angle(make_daily(200, 0.003), make_benchmark(400, 0.005), "000300.SH")
    excess = next(m for m in out["metrics"] if m["key"] == "excess20")
    ret20 = next(m for m in out["metrics"] if m["key"] == "ret20")
    assert excess["value"] < 0 < ret20["value"]
    assert out["score"] < 50


def test_trend_without_benchmark_still_scores_on_absolute():
    out = A.trend_angle(make_daily(200, 0.003), pl.DataFrame(), None)
    assert out["available"] is True
    assert out["score"] > 50


def test_trend_unavailable_without_bars():
    out = A.trend_angle(pl.DataFrame(), pl.DataFrame(), None)
    assert out["available"] is False
    assert out["hint"]


def test_trend_rank_percentile_direction():
    """全行业排名分位越高，趋势分越高（同一个行业换一个横截面）。"""
    daily = make_daily(60, 0.001)
    low = pl.DataFrame({"industry_code": ["801780.SI", "X"], "r20": [1.0, 9.0]})
    high = pl.DataFrame({"industry_code": ["801780.SI", "X"], "r20": [9.0, 1.0]})
    a = A.trend_angle(daily, pl.DataFrame(), None, low)
    b = A.trend_angle(daily, pl.DataFrame(), None, high)
    assert b["score"] > a["score"]


def test_rrg_quadrant_mapping():
    """四象限的判据是 RS-Ratio / RS-Momentum 相对 100 的位置。"""
    assert A.RRG_QUADRANTS[1][0] == "领先"
    assert A.RRG_QUADRANTS[2][0] == "改善"
    assert A.RRG_QUADRANTS[3][0] == "滞后"
    assert A.RRG_QUADRANTS[4][0] == "疲软"
    # 领先 > 改善 > 疲软 > 滞后（方向强度）
    assert (A.RRG_QUADRANTS[1][1] > A.RRG_QUADRANTS[2][1]
            > A.RRG_QUADRANTS[4][1] > A.RRG_QUADRANTS[3][1])


def test_rrg_needs_full_warmup():
    """预热不足必须返回 None，不能偷偷缩窗口凑一个数出来。"""
    assert A.rrg_state(make_daily(100, 0.002), make_benchmark(400)) is None
    assert A.rrg_state(make_daily(400, 0.002), pl.DataFrame()) is None


def test_rrg_strong_industry_is_leading():
    """行业相对横盘基准**持续加速**走强 → RS-Ratio 与 RS-Momentum 都大于 100。"""
    state = A.rrg_state(make_accelerating_daily(400, 0.0, 0.008),
                        make_benchmark(400, 0.0))
    assert state is not None
    assert state["quadrant"] == 1
    assert state["rs_ratio"] > 100
    assert state["rs_momentum"] > 100


def test_rrg_weak_industry_is_lagging():
    """相对强度持续走弱（且还在恶化）→ 滞后象限。"""
    state = A.rrg_state(make_accelerating_daily(400, 0.0, -0.008),
                        make_benchmark(400, 0.0))
    assert state is not None
    assert state["quadrant"] == 3
    assert state["rs_ratio"] < 100


def test_rrg_decelerating_strength_is_weakening():
    """曾经很强但边际转弱 → 疲软（这正是 RRG 相对纯动量排名的增量信息）。"""
    state = A.rrg_state(make_accelerating_daily(400, 0.008, 0.0),
                        make_benchmark(400, 0.0))
    assert state is not None
    assert state["quadrant"] == 4
    assert state["rs_ratio"] > 100
    assert state["rs_momentum"] < 100


# ---------------------------------------------------------------- 景气度


def test_prosperity_high_growth_scores_above_neutral():
    fin = make_fin([
        (f"60000{i}.SH", "indicator.or_yoy", 30.0, 1) for i in range(5)
    ] + [
        (f"60000{i}.SH", "indicator.netprofit_yoy", 60.0, 1) for i in range(5)
    ] + [
        (f"60000{i}.SH", "indicator.roe", 20.0, 1) for i in range(5)
    ])
    out = A.prosperity_angle(fin, {})
    assert out["available"] is True
    assert out["score"] > 50


def test_prosperity_negative_growth_scores_below_neutral():
    fin = make_fin([
        (f"60000{i}.SH", "indicator.or_yoy", -25.0, 1) for i in range(5)
    ] + [
        (f"60000{i}.SH", "indicator.netprofit_yoy", -40.0, 1) for i in range(5)
    ] + [
        (f"60000{i}.SH", "indicator.roe", -3.0, 1) for i in range(5)
    ])
    out = A.prosperity_angle(fin, {})
    assert out["score"] < 50


def test_prosperity_relative_to_market_moves_score():
    """绝对水平一样，跑赢全市场中位数的行业必须得分更高。"""
    fin = make_fin([(f"60000{i}.SH", "indicator.or_yoy", 10.0, 1) for i in range(5)])
    below = A.prosperity_angle(fin, {"revenue_yoy": 30.0})
    above = A.prosperity_angle(fin, {"revenue_yoy": -10.0})
    assert above["score"] > below["score"]


def test_prosperity_momentum_uses_previous_period():
    """净利同比从 -20% 改善到 +10% → 环比动能给正方向。"""
    fin = make_fin(
        [(f"60000{i}.SH", "indicator.netprofit_yoy", 10.0, 1) for i in range(5)]
        + [(f"60000{i}.SH", "indicator.netprofit_yoy", -20.0, 2) for i in range(5)]
    )
    out = A.prosperity_angle(fin, {})
    mom = next((m for m in out["metrics"] if m["key"] == "profit_yoy_mom"), None)
    assert mom is not None
    assert mom["value"] == pytest.approx(30.0)
    assert mom["signal"] == "bullish"


def test_prosperity_unavailable_without_financials():
    out = A.prosperity_angle(pl.DataFrame(), {})
    assert out["available"] is False
    assert "financial" in out["hint"]


def test_prosperity_coverage_full_when_all_signals_available():
    """三组信号（绝对 / 相对 / 环比）齐全时覆盖度必须是 1.0。"""
    fin = make_fin(
        [(f"60000{i}.SH", item, 15.0, 1)
         for i in range(5)
         for item in ("indicator.or_yoy", "indicator.netprofit_yoy", "indicator.roe")]
        + [(f"60000{i}.SH", item, 10.0, 2)
           for i in range(5)
           for item in ("indicator.or_yoy", "indicator.netprofit_yoy", "indicator.roe")]
    )
    out = A.prosperity_angle(
        fin, {"revenue_yoy": 5.0, "profit_yoy": 5.0, "roe": 5.0})
    assert out["available"] is True
    assert out["coverage"] == 1.0


def test_prosperity_insufficient_samples_are_reported_not_scored():
    fin = make_fin([("600000.SH", "indicator.roe", 20.0, 1)])
    out = A.prosperity_angle(fin, {})
    assert out["available"] is False          # 只有 1 个样本 → 没有信号


def test_concept_market_medians_uses_candidate_priority():
    assert A.concept_market_medians({"indicator.tr_yoy": 5.0}) == {"revenue_yoy": 5.0}
    assert A.concept_market_medians({"indicator.or_yoy": 7.0,
                                     "indicator.tr_yoy": 5.0}) == {"revenue_yoy": 7.0}
    assert A.concept_market_medians(None) == {}


# ---------------------------------------------------------------- 估值


def _valuation_history(n: int, pe_start: float, pe_end: float) -> pl.DataFrame:
    rows = []
    for i in range(n):
        f = i / max(1, n - 1)
        pe = pe_start + (pe_end - pe_start) * f
        rows.append({"trade_date": _dates(n)[i], "symbol": "600000.SH",
                     "pe_ttm": pe, "pb_mrq": pe / 10.0})
    return pl.DataFrame(rows)


def test_valuation_cheap_percentile_is_bullish():
    """PE 从 40 一路跌到 10 → 自身历史最低分位 → 便宜 → 方向为正。"""
    out = A.valuation_angle(_valuation_history(300, 40.0, 10.0), None, "801780.SI")
    assert out["available"] is True
    assert out["score"] > 50
    pct = next(m for m in out["metrics"] if m["key"] == "pe_hist_pct")
    assert pct["percentile"] < 10


def test_valuation_expensive_percentile_is_bearish():
    out = A.valuation_angle(_valuation_history(300, 10.0, 40.0), None, "801780.SI")
    assert out["score"] < 50


def test_valuation_negative_pe_excluded_from_median():
    """亏损公司（PE ≤ 0）不能进中位数，否则行业估值会算成负数。"""
    hist = pl.DataFrame({
        "trade_date": _dates(200),
        "symbol": ["600000.SH"] * 200,
        "pe_ttm": [-5.0] * 200,
        "pb_mrq": [1.0] * 200,
    })
    out = A.valuation_angle(hist, None, "801780.SI")
    pe = next((m for m in out["metrics"] if m["key"] == "pe_median"), None)
    assert pe is not None and pe["value"] is None
    assert "正数样本" in (pe["note"] or "")


def test_valuation_short_history_does_not_emit_percentile():
    out = A.valuation_angle(_valuation_history(30, 40.0, 10.0), None, "801780.SI")
    assert out["available"] is False


def test_valuation_cross_section_percentile_direction():
    hist = _valuation_history(300, 20.0, 20.0)
    cheap = pl.DataFrame({"industry_code": ["801780.SI", "X"], "pe_median": [5.0, 50.0]})
    rich = pl.DataFrame({"industry_code": ["801780.SI", "X"], "pe_median": [50.0, 5.0]})
    assert (A.valuation_angle(hist, cheap, "801780.SI")["score"]
            > A.valuation_angle(hist, rich, "801780.SI")["score"])


# ---------------------------------------------------------------- 资金与拥挤度


def _flow(n: int, net: float, ratio: float) -> pl.DataFrame:
    return pl.DataFrame({
        "trade_date": _dates(n), "symbol": ["600000.SH"] * n,
        "main_net_inflow": [net] * n, "main_net_ratio": [ratio] * n,
    })


def test_capital_inflow_is_bullish():
    out = A.capital_angle(_flow(30, 1e8, 5.0), None, pl.DataFrame(), None)
    assert out["available"] is True
    assert out["score"] > 50


def test_capital_outflow_is_bearish():
    out = A.capital_angle(_flow(30, -1e8, -5.0), None, pl.DataFrame(), None)
    assert out["score"] < 50


def test_crowding_is_reverse_scored():
    """成交额占比冲到历史最高分位 = 拥挤 = 扣分（方向为负）。"""
    n = 120
    daily = make_daily(n, 0.001)
    market = pl.DataFrame({"trade_date": _dates(n), "amount": [1e10] * n})
    # 该行业的成交额占比在最后一天创历史新高
    amounts = [1e8] * (n - 1) + [9e9]
    daily = daily.with_columns(pl.Series("amount", amounts))
    out = A.capital_angle(pl.DataFrame(), None, daily, market)
    crowd = next(m for m in out["metrics"] if m["key"] == "crowding_pct")
    assert crowd["percentile"] > 90
    assert out["score"] < 50


def test_capital_unavailable_without_any_flow():
    out = A.capital_angle(pl.DataFrame(), None, pl.DataFrame(), None)
    assert out["available"] is False


# ---------------------------------------------------------------- 宽度与 NH-NL


def test_breadth_high_up_ratio_is_bullish():
    daily = make_daily(60, 0.002).with_columns(pl.lit(0.8).alias("up_ratio"))
    out = A.breadth_angle(daily, make_bars(300), pl.DataFrame())
    assert out["available"] is True
    assert out["score"] > 50


def test_breadth_low_up_ratio_is_bearish():
    daily = make_daily(60, -0.002).with_columns(pl.lit(0.2).alias("up_ratio"))
    out = A.breadth_angle(daily, make_bars(300), pl.DataFrame())
    assert out["score"] < 50


def test_breadth_reports_missing_limit_up_pool():
    daily = make_daily(60, 0.002)
    out = A.breadth_angle(daily, make_bars(300), pl.DataFrame())
    lu = next(m for m in out["metrics"] if m["key"] == "limit_up_days")
    assert lu["value"] is None
    assert "涨停池未采集" in lu["note"]


def test_nhnl_direction_thresholds():
    """阈值按成员数分档：≥40 家用 0.3/0.2/−0.2/−0.3，<40 家放宽一档。"""
    assert A._nhnl_direction(0.35, 50) == 1.0
    assert A._nhnl_direction(0.25, 50) == 0.5
    assert A._nhnl_direction(0.0, 50) == 0.0
    assert A._nhnl_direction(-0.25, 50) == -0.5
    assert A._nhnl_direction(-0.35, 50) == -1.0
    assert A._nhnl_direction(0.25, 10) == 0.0     # <40 家：0.3 才算乐观
    assert A._nhnl_direction(0.35, 10) == 0.5
    assert A._nhnl_direction(0.45, 10) == 1.0


def test_nhnl_none_when_history_too_short():
    assert A._nhnl_latest(make_bars(100)) is None


def test_breadth_coverage_reaches_one_when_all_signals_present():
    """覆盖度分母必须是「真的可能产出的信号数」，不能是拍脑袋的常数。"""
    daily = make_daily(60, 0.002).with_columns(pl.lit(0.8).alias("up_ratio"))
    limit_up = pl.DataFrame({
        "trade_date": _dates(30),
        "symbol": ["600000.SH"] * 30,
        "name": ["x"] * 30,
    })
    out = A.breadth_angle(daily, make_bars(300, drift=0.002), limit_up)
    assert out["available"] is True
    assert out["coverage"] == 1.0


def test_nhnl_computed_with_full_window():
    got = A._nhnl_latest(make_bars(300, n_symbols=6, drift=0.002))
    assert got is not None
    value, n = got
    assert -1.0 <= value <= 1.0
    assert n == 6


# ---------------------------------------------------------------- 跨行业横截面


class _Uni:
    """最小 universe 替身（只测纯函数与服务编排要用的字段）。"""

    def __init__(self, daily=None, membership=None, valuation_cross=None,
                 notes=None, bars=None, industries=None, asof=None, std="SW",
                 benchmark=None, benchmark_symbol=None):
        self.daily = daily if daily is not None else pl.DataFrame()
        self.membership = membership if membership is not None else pl.DataFrame()
        self.valuation_cross = (valuation_cross if valuation_cross is not None
                                else pl.DataFrame())
        self.bars = bars if bars is not None else pl.DataFrame()
        self.industries = (industries if industries is not None
                           else pl.DataFrame())
        self.benchmark = benchmark if benchmark is not None else pl.DataFrame()
        self.benchmark_symbol = benchmark_symbol
        self.notes = notes or []
        self.asof = asof or date(2025, 6, 30)
        self.std = std


def _full_universe(**kw) -> _Uni:
    """够跑通一次完整分析的合成 universe。"""
    daily = make_daily(200, 0.002)
    bars = make_bars(200, n_symbols=6)
    membership = pl.DataFrame({
        "symbol": bars["symbol"].unique().to_list(),
        "industry_code": ["801780.SI"] * 6,
        "industry_name": ["银行"] * 6,
    })
    industries = pl.DataFrame({
        "industry_code": ["801780.SI", "801080.SI"],
        "industry_name": ["银行", "电子"],
        "n_members": [6, 4],
    })
    return _Uni(daily=daily, bars=bars, membership=membership,
                industries=industries, **kw)


def test_industry_return_table_ranks_by_first_window():
    d1 = make_daily(80, 0.004, code="A", name="强")
    d2 = make_daily(80, -0.004, code="B", name="弱")
    table = A.industry_return_table(_Uni(daily=pl.concat([d1, d2])), (20, 60))
    assert table["industry_code"].to_list() == ["A", "B"]
    assert table["r20"][0] > 0 > table["r20"][1]


def test_industry_return_table_empty_universe():
    assert A.industry_return_table(_Uni()).is_empty()


def test_industry_valuation_cross_excludes_non_positive():
    mem = pl.DataFrame({
        "symbol": ["a", "b", "c"], "industry_code": ["I"] * 3,
        "industry_name": ["I"] * 3,
    })
    cross = pl.DataFrame({
        "symbol": ["a", "b", "c"], "pe_ttm": [10.0, 20.0, -5.0],
        "pb_mrq": [1.0, 2.0, 0.5],
    })
    out = A.industry_valuation_cross(_Uni(membership=mem, valuation_cross=cross))
    assert out["pe_median"][0] == pytest.approx(15.0)   # -5 被剔除
    assert out["pb_median"][0] == pytest.approx(1.0)    # median(0.5, 1.0, 2.0)


# ---------------------------------------------------------------- 风险


def test_risk_flags_small_industry():
    uni = _Uni()
    out = A.risk_block(uni, make_daily(60), member_count=3,
                       valuation_pct=90.0, crowding_pct=90.0)
    flags = " ".join(out["flags"])
    assert "代表性不足" in flags
    assert "偏贵" in flags
    assert "拥挤" in flags


def test_risk_merges_universe_notes():
    uni = _Uni(notes=["没有行业分类数据"])
    out = A.risk_block(uni, make_daily(60), member_count=30)
    assert any("没有行业分类数据" in f for f in out["flags"])


# ---------------------------------------------------------------- 归属解析（真实 DuckDB）


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("""CREATE TABLE industry_classify (
        symbol VARCHAR, std VARCHAR, code VARCHAR, name VARCHAR,
        std_date DATE, source VARCHAR)""")
    c.execute("""INSERT INTO industry_classify VALUES
        ('600000.SH','SW','801780.SI','银行',DATE '2010-01-01','demo'),
        ('600001.SH','SW','801780.SI','银行',DATE '2010-01-01','demo'),
        ('600002.SH','SW','801080.SI','电子',DATE '2010-01-01','demo'),
        ('600003.SH','CICS','CI005001','银行',DATE '2010-01-01','demo')""")
    yield c
    c.close()


def test_resolve_std_prefers_shenwan(con):
    assert L.resolve_std(con, date(2025, 1, 1), None) == ("SW", False)
    assert L.resolve_std(con, date(2025, 1, 1), "CICS") == ("CICS", False)
    # 显式请求了湖里没有的标准 → 回退到首选，但必须标记「回退了」，
    # 否则申万/中信口径被静默替换
    assert L.resolve_std(con, date(2025, 1, 1), "NOPE") == ("SW", True)


def test_list_industries_counts_members(con):
    out = L.list_industries(con, date(2025, 1, 1), "SW")
    assert out.height == 2
    bank = out.filter(pl.col("industry_code") == "801780.SI").row(0, named=True)
    assert bank["n_members"] == 2


def test_resolve_industry_by_code_name_and_partial(con):
    assert L.resolve_industry(con, "801780.SI", date(2025, 1, 1), "SW") == \
        ("801780.SI", "银行")
    assert L.resolve_industry(con, "银行", date(2025, 1, 1), "SW") == \
        ("801780.SI", "银行")
    assert L.resolve_industry(con, "电子", date(2025, 1, 1), "SW") == \
        ("801080.SI", "电子")
    assert L.resolve_industry(con, "不存在", date(2025, 1, 1), "SW") is None
    assert L.resolve_industry(con, "", date(2025, 1, 1), "SW") is None


def test_membership_respects_asof(con):
    """生效日之前查不到该标的的行业归属。"""
    assert L.load_membership(con, date(2009, 12, 31), "SW").is_empty()
    assert L.load_membership(con, date(2010, 1, 1), "SW").height == 3


# ---------------------------------------------------------------- 服务编排


@pytest.fixture
def stubbed(monkeypatch):
    """把取数层整体打桩：只测编排、契约与降级，不碰数据湖。"""
    from lquant.industry import service as S

    uni = _full_universe()

    monkeypatch.setattr(L, "build_universe", lambda *a, **k: uni)
    monkeypatch.setattr(L, "resolve_asof", lambda asof: date(2025, 6, 30))
    monkeypatch.setattr(L, "member_symbols", lambda u, code: ["600000.SH",
                                                              "600001.SH"])
    monkeypatch.setattr(L, "industry_daily", lambda u, code: u.daily)
    monkeypatch.setattr(L, "industry_bars", lambda u, code: u.bars)
    monkeypatch.setattr(L, "load_financials", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(L, "load_market_financial_medians", lambda *a, **k: {})
    monkeypatch.setattr(L, "load_valuation_history", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(L, "load_money_flow", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(L, "load_flow_market", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(L, "load_limit_up", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(L, "market_amount_daily", lambda u: pl.DataFrame())
    monkeypatch.setattr(L, "load_security_names", lambda syms: {})
    monkeypatch.setattr(S, "_resolve", lambda u, ident: ("801780.SI", "银行"))
    return uni


def test_analyze_industry_report_contract(stubbed):
    """编排产出的报告必须满足前端契约：字段齐全、角度顺序稳定、JSON 安全。"""
    import json

    from lquant.industry import service as S

    rep = S.analyze_industry("银行")
    assert rep["schema_version"] == S.SCHEMA_VERSION
    assert rep["industry"] == "银行"
    assert rep["industry_code"] == "801780.SI"
    assert rep["std"] == "SW"
    assert rep["asof"] == "2025-06-30"
    assert [a["id"] for a in rep["angles"]] == [
        "trend", "prosperity", "valuation", "capital", "breadth"]
    assert set(rep["score"]) >= {"score", "grade", "angle_coverage", "contributions"}
    assert rep["verdict"]["points"]
    assert rep["risk"]["title"] == "行业风险提示"
    assert "等权合成" in rep["disclaimer"]
    # 契约要求：响应体里绝不出现 NaN/Infinity（前端 JSON.parse 会直接炸）
    assert "NaN" not in json.dumps(rep, ensure_ascii=False)


def test_analyze_industry_marks_missing_angles_and_keeps_score(stubbed):
    """取数层全空时，报告仍然可用：有分的角度算分，缺的给 hint。"""
    from lquant.industry import service as S

    rep = S.analyze_industry("银行")
    by_id = {a["id"]: a for a in rep["angles"]}
    assert by_id["trend"]["available"] is True
    assert by_id["breadth"]["available"] is True
    for missing in ("prosperity", "valuation", "capital"):
        assert by_id[missing]["available"] is False
        assert by_id[missing]["hint"]
    assert rep["score"]["score"] is not None
    assert rep["score"]["n_scored"] == 2
    assert rep["score"]["n_angles"] == 5
    # 只有 2/5 个角度有数据 → 必须在风险里警示，不能让人以为这是全角度结论
    assert any("分析面覆盖" in r for r in rep["verdict"]["risks"])


def test_analyze_industry_unknown_identifier_raises_keyerror(stubbed, monkeypatch):
    from lquant.industry import service as S

    monkeypatch.setattr(S, "_resolve", lambda u, ident: None)
    with pytest.raises(KeyError):
        S.analyze_industry("不存在")


def test_industry_rotation_ranks_rows(stubbed):
    from lquant.industry import service as S

    other = make_daily(200, -0.004, code="801080.SI", name="电子")
    stubbed.daily = pl.concat([stubbed.daily, other])
    out = S.industry_rotation(None, "SW", 20)
    assert out["window"] == 20
    assert [r["industry_code"] for r in out["rows"]] == ["801780.SI", "801080.SI"]
    assert out["rows"][0]["rank"] == 1
    assert out["rows"][0]["percentile"] == 100.0
    # 估值表为空时也必须给出稳定的列（前端按列取值）
    assert "pe_median" in out["rows"][0]


def test_industry_rotation_empty_universe(stubbed):
    from lquant.industry import service as S

    stubbed.daily = pl.DataFrame()
    out = S.industry_rotation(None, "SW", 20)
    assert out["rows"] == []
    assert out["asof"] == "2025-06-30"


def test_list_industry_names_includes_last_return(stubbed):
    from lquant.industry import service as S

    out = S.list_industry_names(None, "SW")
    names = {r["industry_name"] for r in out["industries"]}
    assert names == {"银行", "电子"}
    bank = next(r for r in out["industries"] if r["industry_name"] == "银行")
    assert bank["n_members"] == 6
    assert bank["last_ret"] is not None


def test_list_industry_names_empty(stubbed):
    from lquant.industry import service as S

    stubbed.industries = pl.DataFrame()
    out = S.list_industry_names(None, "SW")
    assert out["industries"] == []


def test_metric_value_helper():
    from lquant.industry import service as S

    angles = [{"metrics": [{"key": "pe_hist_pct", "value": 12.5},
                           {"key": "x", "value": None}]}]
    assert S._metric_value(angles, "pe_hist_pct") == 12.5
    assert S._metric_value(angles, "x") is None
    assert S._metric_value(angles, "missing") is None


def test_rank_of_returns_none_without_cross_section():
    from lquant.industry import service as S

    assert S._rank_of(pl.DataFrame(), "X") is None
    assert S._rank_of(pl.DataFrame({"industry_code": ["X"]}), "X") is None


# ---------------------------------------------------------------- 降级路径
#
# 数据湖可以只有行情、没有财务；可以只有 SW、没有 CICS；可以让 provider 少几列。
# 这些路径都必须「降级 + 说明」，而不是抛异常把整个分析打挂 —— 单列出来钉死。


def test_resolve_std_falls_back_to_largest_when_no_preferred():
    """既不是 SW 也不是 CICS 时，取行数最多的那个标准，而不是报错。"""
    c = duckdb.connect()
    try:
        c.execute("""CREATE TABLE industry_classify (
            symbol VARCHAR, std VARCHAR, code VARCHAR, name VARCHAR,
            std_date DATE, source VARCHAR)""")
        c.execute("""INSERT INTO industry_classify VALUES
            ('1.SH','FOO','A','甲',DATE '2020-01-01','x'),
            ('2.SH','BAR','B','乙',DATE '2020-01-01','x'),
            ('3.SH','BAR','B','乙',DATE '2020-01-01','x')""")
        assert L.resolve_std(c, date(2025, 1, 1), None) == ("BAR", False)
        assert L.available_stds(c, date(2025, 1, 1)) == ["BAR", "FOO"]
    finally:
        c.close()


def test_resolve_asof_accepts_string_and_date():
    assert L.resolve_asof("2025-06-30") == date(2025, 6, 30)
    assert L.resolve_asof(date(2025, 6, 30)) == date(2025, 6, 30)
    # None → 湖内最新交易日（空湖回退到自然日，不抛异常）
    assert L.resolve_asof(None) is not None


def test_resolve_industry_partial_name_needs_unique_hit():
    """只输行业名的一部分时，唯一命中才算数 —— 歧义返回 None 让上层提示。"""
    c = duckdb.connect()
    try:
        c.execute("""CREATE TABLE industry_classify (
            symbol VARCHAR, std VARCHAR, code VARCHAR, name VARCHAR,
            std_date DATE, source VARCHAR)""")
        c.execute("""INSERT INTO industry_classify VALUES
            ('1.SH','SW','A','电子设备',DATE '2020-01-01','x'),
            ('2.SH','SW','B','电子元件',DATE '2020-01-01','x')""")
        assert L.resolve_industry(c, "电子", date(2025, 1, 1), "SW") is None
        assert L.resolve_industry(c, "电子设备", date(2025, 1, 1), "SW") == \
            ("A", "电子设备")
        assert L.resolve_industry(c, "设备", date(2025, 1, 1), "SW") == \
            ("A", "电子设备")
    finally:
        c.close()


def test_read_classify_degrades_when_table_missing():
    """表不存在时返回空帧而不是抛异常（空表是常态不是错误）。"""
    c = duckdb.connect()
    try:
        assert L._read_classify(c, date(2025, 1, 1)).is_empty()
        assert L.available_stds(c, date(2025, 1, 1)) == []
        assert L.resolve_std(c, date(2025, 1, 1), None) == (None, False)
        assert L.resolve_std(c, date(2025, 1, 1), "SW") == (None, False)
        assert L.list_industries(c, date(2025, 1, 1), None).is_empty()
        assert L.resolve_industry(c, "银行", date(2025, 1, 1), None) is None
    finally:
        c.close()


def test_build_universe_is_cached_and_clearable(monkeypatch):
    """全市场面板要缓存（构建一次很贵）；清缓存后必须重新构建。"""
    calls: list[tuple] = []
    uni = _Uni()

    def fake_build(asof, std, lookback_days):
        calls.append((asof, std, lookback_days))
        return uni

    monkeypatch.setattr(L, "_build_universe", fake_build)
    L.clear_industry_cache()
    a = L.build_universe(date(2025, 6, 30), "SW", 720)
    b = L.build_universe(date(2025, 6, 30), "SW", 720)
    assert a is b and len(calls) == 1
    L.clear_industry_cache()
    L.build_universe(date(2025, 6, 30), "SW", 720)
    assert len(calls) == 2


def test_market_amount_daily_empty_universe():
    assert L.market_amount_daily(_Uni()).is_empty()
    assert L.market_amount_daily(_Uni(daily=make_daily(10))).height == 10


def test_industry_bars_and_daily_empty_universe():
    assert L.industry_bars(_Uni(), "X").is_empty()
    assert L.industry_daily(_Uni(), "X").is_empty()
    assert L.member_symbols(_Uni(), "X") == []


def test_load_security_names_handles_empty_and_missing():
    """空输入 → 空映射；查不到的代码不抛异常（缺名字只影响展示，不影响计算）。

    注意断言写法：库里可能有真实的 security 表，不能假设某只票「一定查不到」。
    """
    assert L.load_security_names([]) == {}
    got = L.load_security_names(["ZZZZZZ.SH"])
    assert isinstance(got, dict)
    assert "ZZZZZZ.SH" not in got


def test_index_level_handles_null_returns():
    assert L.index_level(pl.DataFrame({"ret": [None, 0.1, None]})) == [1.0, 1.1, 1.1]


def test_nhnl_requires_high_low_columns():
    bars = make_bars(300).drop(["high", "low"])
    assert A._nhnl_latest(bars) is None


def test_valuation_angle_missing_columns_is_unavailable():
    hist = pl.DataFrame({"trade_date": _dates(200), "symbol": ["a"] * 200})
    out = A.valuation_angle(hist, None, "X")
    assert out["available"] is False


def test_industry_valuation_cross_empty_inputs():
    assert A.industry_valuation_cross(_Uni()).is_empty()
    mem = pl.DataFrame({"symbol": ["a"], "industry_code": ["I"],
                        "industry_name": ["I"]})
    assert A.industry_valuation_cross(_Uni(membership=mem)).is_empty()


def test_prosperity_angle_empty_after_filter_is_unavailable():
    """有 financial_pit 行、但全是不认识的 item → 没有样本 → 不评分。"""
    fin = make_fin([("600000.SH", "unknown.item", 1.0, 1)])
    out = A.prosperity_angle(fin, {})
    assert out["available"] is False


def test_capital_angle_only_crowding_still_works():
    """没有 money_flow 时，拥挤度单独也能给出资金面判断（覆盖度如实降低）。"""
    n = 120
    daily = make_daily(n, 0.001).with_columns(pl.Series("amount", [1e8] * n))
    market = pl.DataFrame({"trade_date": _dates(n), "amount": [1e10] * n})
    out = A.capital_angle(pl.DataFrame(), None, daily, market)
    assert out["available"] is True
    assert out["coverage"] < 1.0
    assert any(m["key"] == "crowding_pct" for m in out["metrics"])


def test_leaders_empty_and_short_history():
    from lquant.industry import service as S

    assert S._leaders(pl.DataFrame()) == []
    # 只有 10 根 K 线 → 算不出 20 日区间收益 → 不给龙头（而不是给个假的第一名）
    assert S._leaders(make_bars(10)) == []


def test_overview_handles_empty_daily():
    from lquant.industry import service as S

    uni = _Uni(asof=date(2025, 6, 30))
    o = S._overview(uni, pl.DataFrame(), [], "银行", "801780.SI", None,
                    pl.DataFrame())
    assert o["active_members"] is None
    assert o["day_ret"] is None
    assert o["leaders"] == []
    assert o["n_angles"] == 5


# ---------------------------------------------------------------- 分支覆盖补强
#
# 下面这些用例只做一件事：把「数据形态不完整」时那些**能走到**的分支钉住。
# 覆盖率门禁（改动行 ≥95%）是其中一半理由，另一半是这些分支恰恰是换数据源
# 时最先被触发的路径，出问题时报错信息必须是对的。


def test_ma_tail_returns_none_when_too_short():
    assert A._ma_tail([1.0, 2.0], 5) is None
    assert A._ma_tail([1.0, 2.0, 3.0], 3) == pytest.approx(2.0)


def test_rrg_improving_quadrant():
    """弱但在转强 → 「改善」（RRG 相对纯动量排名的增量信息）。"""
    state = A.rrg_state(make_accelerating_daily(400, -0.008, 0.0),
                        make_benchmark(400, 0.0))
    assert state is not None
    assert state["quadrant"] == 2
    assert state["quadrant_label"] == "改善"


def test_rrg_none_on_exact_center():
    """RS-Ratio / RS-Momentum 恰好落在中枢 100 时不出象限（不强行归边）。"""
    daily = make_daily(400, 0.0)
    bench = make_benchmark(400, 0.0)
    assert A.rrg_state(daily, bench) is None


def test_industry_return_table_without_windows():
    """windows 为空时不能崩，也不能给不存在的列排序。"""
    table = A.industry_return_table(_Uni(daily=make_daily(30, 0.001)), ())
    assert table.height == 1
    assert "amount_20d" in table.columns


def test_industry_valuation_cross_no_symbol_overlap():
    """行业成员与估值截面没有交集（估值同步滞后）→ 空，而不是编一个 0。"""
    mem = pl.DataFrame({"symbol": ["a"], "industry_code": ["I"],
                        "industry_name": ["I"]})
    cross = pl.DataFrame({"symbol": ["zzz"], "pe_ttm": [10.0], "pb_mrq": [1.0]})
    assert A.industry_valuation_cross(_Uni(membership=mem,
                                           valuation_cross=cross)).is_empty()


def test_industry_valuation_cross_without_valuation_columns():
    """daily_basic 少列时返回空表，而不是抛 KeyError。"""
    mem = pl.DataFrame({"symbol": ["a"], "industry_code": ["I"],
                        "industry_name": ["I"]})
    cross = pl.DataFrame({"symbol": ["a"], "close": [10.0]})
    assert A.industry_valuation_cross(_Uni(membership=mem,
                                           valuation_cross=cross)).is_empty()


def test_trend_unavailable_when_fewer_than_20_trading_days():
    out = A.trend_angle(make_daily(10, 0.01), pl.DataFrame(), None)
    assert out["available"] is False
    assert "20 天" in out["hint"]


def test_trend_skips_ma60_when_history_shorter_than_window():
    """30 个交易日：MA20 算得出、MA60 算不出，覆盖度如实下降而不是补 0。"""
    out = A.trend_angle(make_daily(30, 0.004), pl.DataFrame(), None)
    keys = {m["key"] for m in out["metrics"]}
    assert "ma20_dev" in keys
    assert "ma60_dev" not in keys
    assert out["coverage"] < 1.0


def test_valuation_without_pe_column_reports_which_column_is_missing():
    hist = pl.DataFrame({"trade_date": _dates(200),
                         "symbol": ["a"] * 200,
                         "pb_mrq": [1.0 + i / 200 for i in range(200)]})
    out = A.valuation_angle(hist, None, "X")
    pe = next(m for m in out["metrics"] if m["key"] == "pe_median")
    assert pe["value"] is None
    assert "无该列" in pe["note"]
    assert out["available"] is True     # PB 分位仍然给得出


def test_capital_skips_windows_longer_than_history():
    """只有 3 天资金流：5/20 日窗口都跳过，但拥挤度仍能出结论。"""
    n = 120
    daily = make_daily(n, 0.001).with_columns(pl.Series("amount", [1e8] * n))
    market = pl.DataFrame({"trade_date": _dates(n), "amount": [1e10] * n})
    out = A.capital_angle(_flow(3, 1e8, 5.0), None, daily, market)
    keys = {m["key"] for m in out["metrics"]}
    assert "net5" not in keys and "net20" not in keys
    assert out["available"] is True


def test_capital_relative_to_market_share():
    """有全市场对照时必须给出「占全市场净流入」这一项。"""
    n = 30
    daily = make_daily(n, 0.001).with_columns(pl.Series("amount", [1e8] * n))
    market_amount = pl.DataFrame({"trade_date": _dates(n),
                                  "amount": [1e10] * n})
    market_flow = pl.DataFrame({"trade_date": _dates(n),
                                "main_net_inflow": [1e9] * n})
    out = A.capital_angle(_flow(n, 1e8, 3.0), market_flow, daily, market_amount)
    share = next(m for m in out["metrics"] if m["key"] == "net_share5")
    assert share["value"] == pytest.approx(10.0)      # 1e8*5 / 1e9*5


def test_capital_reports_short_amount_history():
    n = 10
    daily = make_daily(n, 0.001).with_columns(pl.Series("amount", [1e8] * n))
    market = pl.DataFrame({"trade_date": _dates(n), "amount": [1e10] * n})
    out = A.capital_angle(_flow(n, 1e8, 3.0), None, daily, market)
    note = next(m for m in out["metrics"]
                if m["key"] == "amount_share_pct")["note"]
    assert "不足 20" in note


def test_capital_crowding_none_without_market_overlap():
    """行业与全市场成交额没有重叠日期 → 拥挤度给 None + 说明，不编数字。"""
    n = 120
    daily = make_daily(n, 0.001).with_columns(pl.Series("amount", [1e8] * n))
    market = pl.DataFrame({"trade_date": _dates(n, start=date(2020, 1, 1)),
                           "amount": [1e10] * n})
    # 用 30 天资金流保证 net5/net20 出信号，否则整个角度直接 unavailable，
    # 就看不到「拥挤度单独降级」这一层了
    out = A.capital_angle(_flow(30, 1e8, 3.0), None, daily, market)
    crowding = next(m for m in out["metrics"] if m["key"] == "crowding_pct")
    assert crowding["value"] is None
    assert "不足 20" in crowding["note"]


def test_breadth_unavailable_without_daily():
    out = A.breadth_angle(pl.DataFrame(), pl.DataFrame(), pl.DataFrame())
    assert out["available"] is False


def test_breadth_reports_short_up_ratio_history():
    """上涨家数不足 5 天 → 该子项不出值；整角度因为没有任何信号而判不可用。

    注意：``unavailable()`` 不带 metrics（缺失原因在 ``hint``），所以这里断言
    hint 而不是去找那条指标。
    """
    out = A.breadth_angle(make_daily(3, 0.002), pl.DataFrame(), pl.DataFrame())
    assert out["available"] is False
    assert out["score"] is None
    assert out["hint"]


def test_breadth_unavailable_when_nothing_scorable():
    """有日线但样本太少、又没有成员明细 → 不评分，而不是给中性分。"""
    out = A.breadth_angle(make_daily(3, 0.0), pl.DataFrame(), pl.DataFrame())
    assert out["available"] is False
    assert out["score"] is None


def test_yi_formats_across_magnitudes():
    assert A._yi(1.5e8) == "1.50 亿"
    assert A._yi(-2.0e4) == "-2.00 万"
    assert A._yi(123.0) == "123"
    assert A._yi(None) is None


def test_risk_block_flags_missing_daily():
    out = A.risk_block(_Uni(), pl.DataFrame(), member_count=30)
    assert any("没有行业日线" in f for f in out["flags"])


# ---------------------------------------------------------------- 取数层降级
#
# 取数层的异常分支在真实环境里天天发生（表没建、湖缺失、源不可用）。
# 它们的正确行为是「返回空帧 + 由上层给 hint」，不是抛异常把分析打挂 ——
# 所以这里逐个把异常路径点着，确认它们真的被吞掉并转成了空结果。


def test_safe_swallows_exceptions():
    def boom():
        raise RuntimeError("表不存在")

    assert L._safe(boom) is None
    assert L._safe(lambda: 7) == 7


def test_panel_cache_ttl_and_eviction(monkeypatch):
    uni = _Uni()
    monkeypatch.setattr(L, "_CACHE_MAX_ENTRIES", 1)
    L.clear_industry_cache()
    L._cache_put(("a",), uni)
    assert L._cache_get(("a",)) is uni
    # 超出条目上限 → 淘汰最旧的一条
    L._cache_put(("b",), uni)
    assert L._cache_get(("a",)) is None
    assert L._cache_get(("b",)) is uni
    # TTL 过期 → 视为未命中
    monkeypatch.setattr(L, "_CACHE_TTL_SECONDS", 0.0)
    assert L._cache_get(("b",)) is None
    L.clear_industry_cache()
    assert L._cache_get(("b",)) is None


def test_membership_frame_empty_when_std_absent(con):
    assert L.load_membership(con, date(2025, 1, 1), "NOPE").is_empty()


def test_load_benchmark_degrades_when_reader_raises(monkeypatch):
    """读失败不降级到别的基准 —— 直接报「没有基准」，与个股分析同一条纪律。"""
    from lquant.core import db as db_mod

    def boom():
        raise RuntimeError("duckdb lock")

    monkeypatch.setattr(db_mod, "reader", boom)
    bench, symbol = L._load_benchmark(date(2025, 6, 30))
    assert bench.is_empty()
    assert symbol is None


def test_read_market_bars_degrades_when_lake_read_raises(monkeypatch):
    from lquant.data.store import parquet as pq

    def boom(*a, **k):
        raise RuntimeError("湖目录不可读")

    monkeypatch.setattr(pq, "read_daily", boom)
    assert L._read_market_bars(date(2025, 6, 30), 30).is_empty()


def test_load_valuation_cross_degrades(monkeypatch):
    from lquant.data.store import parquet as pq

    def boom(*a, **k):
        raise RuntimeError("daily_basic 缺失")

    monkeypatch.setattr(pq, "read_daily_basic", boom)
    assert L._load_valuation_cross(date(2025, 6, 30)).is_empty()


def test_loader_empty_symbol_guards():
    """空成员列表一律短返回 —— 不要为「没有成员」去查一次库。"""
    assert L.load_financials([], date(2025, 6, 30)).is_empty()
    assert L.load_valuation_history([], date(2025, 6, 30)).is_empty()
    assert L.load_money_flow([], date(2025, 6, 30)).is_empty()
    assert L.load_limit_up([], date(2025, 6, 30)).is_empty()
    assert L.load_security_names([]) == {}
    assert L.load_market_financial_medians(date(2025, 6, 30), []) == {}


def test_loader_queries_degrade_when_reader_raises(monkeypatch):
    from lquant.core import db as db_mod

    def boom():
        raise RuntimeError("表未建")

    monkeypatch.setattr(db_mod, "reader", boom)
    assert L.load_financials(["600000.SH"], date(2025, 6, 30)).is_empty()
    assert L.load_money_flow(["600000.SH"], date(2025, 6, 30)).is_empty()
    assert L.load_limit_up(["600000.SH"], date(2025, 6, 30)).is_empty()
    assert L.load_flow_market(date(2025, 6, 30)).is_empty()
    assert L.load_security_names(["600000.SH"]) == {}
    assert L.load_market_financial_medians(date(2025, 6, 30),
                                           ["indicator.roe"]) == {}


def test_attach_industry_with_empty_classification(con):
    bars = pl.DataFrame({"trade_date": _dates(3), "symbol": ["a"] * 3,
                         "close": [1.0, 2.0, 3.0]})
    assert L._attach_industry(bars, pl.DataFrame()).is_empty()


def test_industry_views_for_unknown_code():
    uni = _full_universe()
    assert L.industry_bars(uni, "NOT-A-CODE").is_empty()
    assert L.industry_daily(uni, "NOT-A-CODE").is_empty()


def test_grade_handles_out_of_range_score():
    """分级表首档是 0，负分（不该出现但可能因上游脏数据出现）要落到最低档。"""
    assert C.grade(-5.0) == "显著弱势"


def test_pick_values_merges_multiple_candidate_columns():
    """同一概念有两个候选列（or_yoy / tr_yoy）时，按优先级合并而非取并集求平均。"""
    fin = make_fin([
        ("600000.SH", "indicator.or_yoy", 10.0, 1),
        ("600001.SH", "indicator.tr_yoy", 20.0, 1),
        ("600002.SH", "indicator.or_yoy", 30.0, 1),
        ("600002.SH", "indicator.tr_yoy", 99.0, 1),   # 同票两个候选都有 → 取 or_yoy
    ])
    vals = sorted(A.pick_values(fin, ("indicator.or_yoy", "indicator.tr_yoy")))
    assert vals == [10.0, 20.0, 30.0]


def test_build_verdict_flags_low_internal_coverage():
    """分析面覆盖够、但角度内部子项缺失 → 单独警示（两种覆盖度不是一回事）。"""
    score = {"score": 60.0, "grade": "强势", "n_scored": 4, "n_angles": 5,
             "angle_coverage": 0.9, "data_coverage": 0.3, "contributions": []}
    out = build_verdict([], score, {"flags": []}, {})
    assert any("角度内部数据覆盖" in r for r in out["risks"])


def test_rank_of_no_match_returns_none():
    from lquant.industry import service as S

    cross = pl.DataFrame({"industry_code": ["A"], "r20": [1.0]})
    assert S._rank_of(cross, "NOT-THERE") is None


def test_rank_of_all_null_returns_none():
    """横截面有列但全是 null（如窗口不够长）→ 不给排名，而不是编一个第 1 名。"""
    from lquant.industry import service as S

    cross = pl.DataFrame({"industry_code": ["A", "B"], "r20": [None, None]})
    assert S._rank_of(cross, "A") is None
