"""个股分析引擎的单元测试。

重点覆盖三类**容易静默出错**的地方：

1. **方向符号**：估值分位「低=便宜=利多」，基本面 ``higher_better=False``
   （负债率）要翻正 —— 符号写反了分数照样算得出来，只是结论完全相反。
   这类 bug 不会抛异常，只能靠「构造已知答案的数据」断言方向。
2. **PIT 边界**：``pub_date > asof`` 的行必须查不到。前视偏差在回测里
   能把垃圾策略捧成圣杯，必须用边界数据钉死。
3. **降级**：空表 / 缺列 / 样本不足时返回 ``available=False`` + ``hint``，
   而不是抛异常或补一个 50 分的假中性。
"""
from __future__ import annotations

from datetime import date, timedelta

import duckdb
import polars as pl
import pytest

from lquant.security import angles as A
from lquant.security import contract as C
from lquant.security import loader as L
from lquant.security.score import build_verdict, composite

# ---------------------------------------------------------------- 夹具 / 工具


def make_bars(n: int = 140, trend: float = 0.6, start: float = 100.0) -> pl.DataFrame:
    """合成日线：``trend`` > 0 造上升趋势，< 0 造下降趋势。

    带轻微正弦扰动，避免出现「所有 close 完全相同」导致指标退化的假数据。
    """
    import math

    d0 = date(2025, 1, 1)
    closes = [start * (1 + trend / 100.0) ** i * (1 + 0.01 * math.sin(i / 3.0))
              for i in range(n)]
    rows = []
    for i, c in enumerate(closes):
        rows.append({
            "trade_date": d0 + timedelta(days=i),
            "open": c * 0.995,
            "high": c * 1.012,
            "low": c * 0.988,
            "close": c,
            "volume": 1_000_000.0 + 1000 * i,
            "turnover_rate": 1.0 + 0.1 * math.sin(i / 5.0),
        })
    return pl.DataFrame(rows)


def make_valuation_history(n: int = 300, pe_start: float = 40.0,
                           pe_end: float = 10.0) -> pl.DataFrame:
    """估值历史：PE 从 ``pe_start`` 线性走到 ``pe_end``。

    ``pe_end < pe_start`` → 最新 PE 处历史低位（便宜）；反之处高位。
    """
    d0 = date(2023, 1, 1)
    rows = []
    for i in range(n):
        f = i / (n - 1)
        pe = pe_start + (pe_end - pe_start) * f
        rows.append({
            "trade_date": d0 + timedelta(days=i),
            "pe_ttm": pe, "pb_mrq": pe / 10.0, "ps_ttm": pe / 20.0,
            "dv_ttm": 2.0, "total_mv": 1e11, "turnover_rate": 1.0,
        })
    return pl.DataFrame(rows)


# ---------------------------------------------------------------- 契约


def test_nonzero_weights_sum_to_one():
    """权重是综合分的唯一尺度来源，加起来不是 1 会让总分系统性偏移。"""
    total = sum(a.weight for a in C.ANGLES if a.weight > 0)
    assert total == pytest.approx(1.0)


def test_angle_ids_unique():
    ids = [a.id for a in C.ANGLES]
    assert len(ids) == len(set(ids))


def test_to_score_bounds_and_neutral():
    assert C.to_score([]) is None              # 无信号 ≠ 中性
    assert C.to_score([(1.0, 1.0)]) == 100.0
    assert C.to_score([(-1.0, 1.0)]) == 0.0
    assert C.to_score([(0.0, 1.0)]) == 50.0
    # 加权：权重大的方向主导
    assert C.to_score([(1.0, 3.0), (-1.0, 1.0)]) > 50.0
    # 全零权重 → 无有效信号
    assert C.to_score([(1.0, 0.0)]) is None


def test_to_score_clamps_out_of_range_direction():
    assert C.to_score([(5.0, 1.0)]) == 100.0
    assert C.to_score([(-5.0, 1.0)]) == 0.0


def test_metric_nan_and_inf_become_none():
    """JSON 里出现 NaN/Infinity 会让前端 JSON.parse 直接抛错、整页白屏。"""
    m = C.metric("k", "标签", float("nan"))
    assert m["value"] is None
    assert C.metric("k", "标签", float("inf"))["value"] is None
    assert C.metric("k", "标签", 1.5)["value"] == 1.5


def test_json_safe_recurses():
    out = C.json_safe({"a": [float("nan"), 1.0], "b": {"c": float("inf")}})
    assert out == {"a": [None, 1.0], "b": {"c": None}}


def test_percentile_rank():
    assert C.percentile_rank([1, 2, 3, 4], 4) == 100.0
    assert C.percentile_rank([1, 2, 3, 4], 1) == 25.0
    assert C.percentile_rank([1, 2, 3, 4], 2.5) == 50.0
    assert C.percentile_rank([], 1) is None
    assert C.percentile_rank([1, 2], float("nan")) is None


def test_stance_of_band():
    assert C.stance_of(50.0) == C.NEUTRAL_SIGNAL
    assert C.stance_of(60.0) == C.BULLISH
    assert C.stance_of(40.0) == C.BEARISH
    assert C.stance_of(None) is None


def test_grade_monotonic():
    assert C.grade(90) == "显著偏多"
    assert C.grade(50) == "中性"
    assert C.grade(10) == "显著偏空"


# ---------------------------------------------------------------- 技术面


def test_technical_uptrend_is_bullish():
    """单边上涨必须给出偏多结论 —— 方向反了是最难发现的错误。"""
    out = A.technical_angle(make_bars(trend=0.8))
    assert out["available"] is True
    assert out["score"] is not None and out["score"] > 55, out["summary"]
    assert out["stance"] == C.BULLISH
    keys = {m["key"] for m in out["metrics"]}
    assert "ma_trend" in keys and "macd" in keys


def test_technical_downtrend_is_bearish():
    out = A.technical_angle(make_bars(trend=-0.8))
    assert out["available"] is True
    assert out["score"] is not None and out["score"] < 45, out["summary"]
    assert out["stance"] == C.BEARISH


def test_technical_insufficient_history_degrades():
    out = A.technical_angle(make_bars(n=10))
    assert out["available"] is False
    assert out["score"] is None
    assert out["hint"]


def test_technical_empty_frame_degrades():
    out = A.technical_angle(pl.DataFrame())
    assert out["available"] is False and out["hint"]


# ---------------------------------------------------------------- 估值


def test_valuation_cheap_history_is_bullish():
    """PE 落到自身历史低位 = 便宜 = 利多。

    这条是回归测试：最初实现把方向写反了（低位→利空），分数照算不误，
    只有构造「已知便宜」的数据才能发现。
    """
    own = make_valuation_history(pe_start=40.0, pe_end=10.0)
    out = A.valuation_angle(own, pl.DataFrame())
    assert out["available"] is True
    assert out["score"] is not None and out["score"] > 55, out["summary"]
    pe = next(m for m in out["metrics"] if m["key"] == "val_pe_ttm")
    assert pe["percentile"] is not None and pe["percentile"] <= 10
    assert pe["signal"] == C.BULLISH


def test_valuation_expensive_history_is_bearish():
    own = make_valuation_history(pe_start=10.0, pe_end=40.0)
    out = A.valuation_angle(own, pl.DataFrame())
    assert out["score"] is not None and out["score"] < 45, out["summary"]
    pe = next(m for m in out["metrics"] if m["key"] == "val_pe_ttm")
    assert pe["signal"] == C.BEARISH


def test_valuation_negative_pe_not_scored():
    """亏损股的 PE 是负数，分位没有意义 —— 要标注不适用而不是算成「极便宜」。"""
    own = make_valuation_history(pe_start=40.0, pe_end=10.0).with_columns(
        pl.when(pl.int_range(pl.len()) >= pl.len() - 5)
        .then(pl.lit(-5.0)).otherwise(pl.col("pe_ttm")).alias("pe_ttm"))
    out = A.valuation_angle(own, pl.DataFrame())
    pe = next(m for m in out["metrics"] if m["key"] == "val_pe_ttm")
    assert pe["percentile"] is None
    assert "亏损" in (pe["note"] or "")


def test_valuation_empty_degrades():
    out = A.valuation_angle(pl.DataFrame(), pl.DataFrame())
    assert out["available"] is False and out["hint"]


# ---------------------------------------------------------------- 基本面

_CANON = (
    ("roe", "净资产收益率", ("indicator.roe",), True),
    ("debt_to_assets", "资产负债率", ("indicator.debt_to_assets",), False),
)


def _fin_frame(pairs: list[tuple[str, str, float, date]]) -> pl.DataFrame:
    """``[(symbol, item, value, stat_date)]`` → 长表。"""
    return pl.DataFrame([
        {"symbol": s, "item": i, "value": v, "stat_date": d, "pub_date": d}
        for s, i, v, d in pairs
    ])


def test_fundamental_high_roe_beats_peers():
    """高 ROE + 低负债 → 两个方向都该偏多。"""
    d = date(2026, 4, 1)
    own = _fin_frame([("600519.SH", "indicator.roe", 30.0, d),
                      ("600519.SH", "indicator.debt_to_assets", 15.0, d)])
    peers = _fin_frame(
        [("600519.SH", "indicator.roe", 30.0, d),
         ("600519.SH", "indicator.debt_to_assets", 15.0, d)]
        + [(f"00000{i}.SZ", "indicator.roe", 5.0, d) for i in range(1, 8)]
        + [(f"00000{i}.SZ", "indicator.debt_to_assets", 70.0, d) for i in range(1, 8)]
    )
    out = A.fundamental_angle(own, peers, "食品饮料", 8, _CANON)
    assert out["available"] is True
    assert out["score"] is not None and out["score"] > 60, out["summary"]
    roe = next(m for m in out["metrics"] if m["key"] == "fund_roe")
    debt = next(m for m in out["metrics"] if m["key"] == "fund_debt_to_assets")
    assert roe["percentile"] == 100.0
    # 负债率 higher_better=False：原始分位是 12.5%（8 家中只有自己 <= 15%），
    # 翻正后是 87.5 —— 低负债被正确解读成「好」。
    assert debt["percentile"] == 87.5
    assert roe["signal"] == C.BULLISH and debt["signal"] == C.BULLISH


def test_fundamental_low_roe_worse_than_peers():
    d = date(2026, 4, 1)
    own = _fin_frame([("600519.SH", "indicator.roe", 2.0, d)])
    peers = _fin_frame(
        [("600519.SH", "indicator.roe", 2.0, d)]
        + [(f"00000{i}.SZ", "indicator.roe", 25.0, d) for i in range(1, 8)]
    )
    out = A.fundamental_angle(own, peers, "食品饮料", 8, _CANON)
    assert out["score"] is not None and out["score"] < 45, out["summary"]


def test_fundamental_insufficient_peers_not_scored():
    """同业样本太少时分位不可信 —— 要给 hint，不能硬算。"""
    d = date(2026, 4, 1)
    own = _fin_frame([("600519.SH", "indicator.roe", 30.0, d)])
    peers = _fin_frame([("600519.SH", "indicator.roe", 30.0, d),
                        ("000001.SZ", "indicator.roe", 5.0, d)])
    out = A.fundamental_angle(own, peers, "食品饮料", 2, _CANON)
    assert out["available"] is False
    assert out["hint"]
    assert out["metrics"]           # 原始值仍然展示


def test_fundamental_empty_degrades():
    out = A.fundamental_angle(pl.DataFrame(), pl.DataFrame(), None, 0, _CANON)
    assert out["available"] is False and out["hint"]


# ---------------------------------------------------------------- 资金面


def _flow(n: int, ratio: float = 6.0) -> pl.DataFrame:
    d0 = date(2026, 9, 1)
    return pl.DataFrame([
        {"trade_date": d0 + timedelta(days=i), "main_net_inflow": ratio * 1e7,
         "main_net_ratio": ratio, "super_large_net": 0.0, "large_net": 0.0,
         "medium_net": 0.0, "small_net": 0.0}
        for i in range(n)
    ])


def test_capital_few_days_not_scored():
    """1 天资金流折成 99 分是过度自信 —— 样本不足必须不评分。"""
    out = A.capital_angle(_flow(1))
    assert out["available"] is True
    assert out["score"] is None
    assert out["hint"]
    assert "样本不足" in out["summary"]


def test_capital_sustained_inflow_bullish():
    out = A.capital_angle(_flow(20, ratio=6.0))
    assert out["score"] is not None and out["score"] > 55, out["summary"]


def test_capital_sustained_outflow_bearish():
    out = A.capital_angle(_flow(20, ratio=-6.0))
    assert out["score"] is not None and out["score"] < 45, out["summary"]


def test_capital_empty_degrades():
    assert A.capital_angle(pl.DataFrame())["available"] is False


# ---------------------------------------------------------------- 相对强度


def test_relative_outperform_is_bullish():
    bars = make_bars(trend=1.0)
    bench = make_bars(trend=0.0)
    out = A.relative_angle(bars, bench, "000300.SH", "食品饮料")
    assert out["available"] is True
    assert out["score"] is not None and out["score"] > 55, out["summary"]


def test_relative_underperform_is_bearish():
    bars = make_bars(trend=0.0)
    bench = make_bars(trend=1.0)
    out = A.relative_angle(bars, bench, "000300.SH", "食品饮料")
    assert out["score"] is not None and out["score"] < 45, out["summary"]


def test_relative_without_benchmark_degrades():
    out = A.relative_angle(make_bars(), pl.DataFrame(), None, None)
    assert out["available"] is False and out["hint"]


def test_relative_ignores_benchmark_rows_after_observation_date():
    """基准里观察日之后的数据绝不能混进超额收益。

    指数日线（index_daily）通常比个股日线更新：指数当天就有，个股要等回填。
    如果只按 ``>= start`` 截取，基准的「近 20 日」窗口会滑到观察日之后，
    超额收益就吃进了未来数据 —— 既是前视偏差，也会让同一观察日的结论
    随着指数继续同步而漂移。
    """
    bars = make_bars(n=140, trend=0.5)
    end = bars["trade_date"].max()
    aligned = bars.select("trade_date").with_columns(pl.lit(100.0).alias("close"))
    future = pl.DataFrame({
        "trade_date": [end + timedelta(days=d) for d in (1, 2, 3, 30, 60)],
        "close": [10.0, 20.0, 30.0, 5.0, 1.0],      # 观察日之后的暴涨暴跌
    })
    clean = A.relative_angle(bars, aligned, "000300.SH", "食品饮料")
    dirty = A.relative_angle(bars, pl.concat([aligned, future]), "000300.SH", "食品饮料")

    assert clean["score"] == dirty["score"]
    assert ({m["key"]: m["value"] for m in clean["metrics"]}
            == {m["key"]: m["value"] for m in dirty["metrics"]})


def test_load_benchmark_does_not_silently_switch_index(monkeypatch):
    """基准读失败时必须判为不可用，不能悄悄换一个指数（结论会不可比且漂移）。"""
    calls: list[str] = []

    def boom(index_symbol, start, end):
        calls.append(index_symbol)
        raise RuntimeError("DuckDB 锁冲突")

    monkeypatch.setattr(L, "_read_index", boom)
    df, code = L.load_benchmark(date(2026, 9, 30))
    assert df.is_empty() and code is None
    assert calls == [L.BENCHMARK_CANDIDATES[0]], "读失败后不该继续尝试其他基准"


def test_load_benchmark_falls_back_only_on_missing_data(monkeypatch):
    """该指数确实没数据（空结果）时才降级到下一个候选。"""
    def only_second(index_symbol, start, end):
        if index_symbol == L.BENCHMARK_CANDIDATES[0]:
            return pl.DataFrame(schema={"trade_date": pl.Date, "close": pl.Float64})
        return pl.DataFrame({"trade_date": [date(2026, 9, 30)], "close": [4000.0]})

    monkeypatch.setattr(L, "_read_index", only_second)
    df, code = L.load_benchmark(date(2026, 9, 30))
    assert code == L.BENCHMARK_CANDIDATES[1]
    assert df.height == 1


# ---------------------------------------------------------------- 消息面


def _news(n: int) -> pl.DataFrame:
    from datetime import datetime
    return pl.DataFrame([
        {"news_id": str(i), "title": f"标题 {i}", "source_name": "测试",
         "published_at": datetime(2026, 9, 20, 10, 0), "url": None}
        for i in range(n)
    ])


def test_news_never_scores():
    """没有情感模型时新闻不能折算成多空分 —— 只报热度。"""
    out = A.news_angle(_news(5), date(2026, 9, 30))
    assert out["available"] is True
    assert out["score"] is None
    assert out["extra"]["scored"] is False
    assert "不参与评分" in out["summary"]


def test_news_empty_degrades():
    assert A.news_angle(pl.DataFrame(), date(2026, 9, 30))["available"] is False


# ---------------------------------------------------------------- 综合分


def _angle(aid: str, score: float | None, weight: float, coverage: float = 1.0) -> dict:
    return {"id": aid, "label": aid, "weight": weight, "available": True,
            "score": score, "stance": C.stance_of(score), "coverage": coverage,
            "summary": "s", "metrics": [], "hint": None, "extra": {}}


def test_composite_renormalizes_over_available_angles():
    """只有一个角度有分时，综合分应等于该角度分（而不是被拉向 0）。"""
    angles = [_angle("technical", 80.0, 0.24), _angle("fundamental", None, 0.26)]
    out = composite(angles)
    assert out["score"] == pytest.approx(80.0)
    assert out["n_scored"] == 1
    assert out["angle_coverage"] == pytest.approx(0.24)


def test_composite_weights_angles():
    angles = [_angle("a", 100.0, 0.5), _angle("b", 0.0, 0.5)]
    out = composite(angles)
    assert out["score"] == pytest.approx(50.0)


def test_composite_zero_weight_angle_ignored():
    """消息面权重 0：即使给了分也不该影响综合分。"""
    angles = [_angle("technical", 100.0, 0.5), _angle("news", 0.0, 0.0)]
    out = composite(angles)
    assert out["score"] == pytest.approx(100.0)


def test_composite_no_data():
    out = composite([_angle("technical", None, 0.24)])
    assert out["score"] is None
    assert out["grade"] == "无法评分"
    assert out["angle_coverage"] == 0.0


def test_composite_tracks_data_coverage():
    angles = [_angle("a", 60.0, 0.5, coverage=0.5), _angle("b", 60.0, 0.5, coverage=1.0)]
    out = composite(angles)
    assert out["data_coverage"] == pytest.approx(0.75)


def test_verdict_lists_missing_angles_as_risks():
    angles = [A.unavailable("capital", "没有资金流数据"),
              _angle("technical", 70.0, 0.24)]
    score = composite(angles)
    risk = {"available": True, "flags": ["波动偏高"], "metrics": []}
    v = build_verdict(angles, score, risk, {"notes": ["湖是空的"]})
    joined = " ".join(v["risks"])
    assert "资金流" in joined
    assert "波动偏高" in joined
    assert "湖是空的" in joined
    assert v["points"]


def test_verdict_warns_on_low_angle_coverage():
    angles = [_angle("technical", 90.0, 0.24)]      # 只有 1/5 个角度有分
    score = composite(angles)
    v = build_verdict(angles, score, {"flags": []}, {})
    assert any("角度有数据" in r for r in v["risks"])


# ---------------------------------------------------------------- PIT（无未来函数）


def _pit_con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE financial_pit (symbol VARCHAR, stat_date DATE, pub_date DATE, "
        "report_type VARCHAR, item VARCHAR, value DOUBLE, unit VARCHAR, source VARCHAR)"
    )
    rows = [
        # 公告日早于观察日 → 可见
        ("600519.SH", "2026-03-31", "2026-04-20", "2026Q1", "indicator.roe", 30.0, None, "t"),
        # 公告日晚于观察日 → 不可见（未来函数）
        ("600519.SH", "2026-06-30", "2026-08-15", "2026Q2", "indicator.roe", 99.0, None, "t"),
        ("000001.SZ", "2026-03-31", "2026-04-20", "2026Q1", "indicator.roe", 10.0, None, "t"),
        ("000001.SZ", "2026-06-30", "2026-08-15", "2026Q2", "indicator.roe", 88.0, None, "t"),
    ]
    con.executemany("INSERT INTO financial_pit VALUES (?,?,?,?,?,?,?,?)", rows)
    return con


def test_load_financial_own_excludes_future_pub_date():
    con = _pit_con()
    df = L.load_financial_own(con, "600519.SH", date(2026, 6, 1))
    vals = df["value"].to_list()
    assert vals == [30.0], f"未来公告日的数据泄漏进来了: {vals}"


def test_load_financial_cross_excludes_future_pub_date():
    con = _pit_con()
    df = L.load_financial_cross(con, ["600519.SH", "000001.SZ"], date(2026, 6, 1))
    assert set(df["value"].to_list()) == {30.0, 10.0}


def test_load_financial_cross_picks_latest_visible_period():
    con = _pit_con()
    df = L.load_financial_cross(con, ["600519.SH"], date(2026, 9, 1))
    assert df["value"].to_list() == [99.0]          # Q2 已公告 → 取最新一期


def test_load_financial_own_missing_table_degrades():
    con = duckdb.connect()
    assert L.load_financial_own(con, "600519.SH", date(2026, 6, 1)).is_empty()


def test_load_financial_cross_empty_symbols_returns_empty():
    """空标的列表必须短路 —— 退化成全市场 77M 行扫描会拖垮交互路径。"""
    con = _pit_con()
    assert L.load_financial_cross(con, [], date(2026, 6, 1)).is_empty()


def test_load_financial_own_dedups_duplicate_rows():
    """数据源重复入库会让同比类派生计算被放大 —— 必须去重。"""
    con = _pit_con()
    con.execute("INSERT INTO financial_pit VALUES "
                "('600519.SH','2026-03-31','2026-04-20','2026Q1','indicator.roe',30.0,NULL,'t')")
    df = L.load_financial_own(con, "600519.SH", date(2026, 6, 1))
    assert df.height == 1


# ---------------------------------------------------------------- 端到端（打桩取数）


def test_analyze_security_report_shape(monkeypatch):
    """完整报告的形状与 JSON 安全性（用打桩数据，不依赖真实湖）。"""
    from lquant.security import service as S

    md = L.MarketData(symbol="600519.SH", asof=date(2026, 9, 30),
                      name="贵州茅台", sec_type="stock", industry="食品饮料",
                      peer_count=8)
    md.bars = make_bars(trend=0.8)
    md.benchmark = make_bars(trend=0.2)
    md.benchmark_symbol = "000300.SH"
    md.valuation = make_valuation_history(pe_start=40.0, pe_end=10.0)
    monkeypatch.setattr(S, "load_all", lambda sym, day: md)

    report = S.analyze_security("600519.SH", date(2026, 9, 30))
    assert report["symbol"] == "600519.SH"
    assert report["asof"] == "2026-09-30"
    assert report["schema_version"] == C.SCHEMA_VERSION
    assert [a["id"] for a in report["angles"]] == [a.id for a in C.ANGLES]
    assert report["score"]["score"] is not None
    assert report["overview"]["name"] == "贵州茅台"
    assert report["disclaimer"]
    assert "risk" in report and report["risk"]["scored"] is False
    import json
    json.dumps(report, allow_nan=False)      # 必须能过严格 JSON


def test_analyze_security_with_no_data_still_returns_report(monkeypatch):
    """数据全空时也要返回结构完整的报告 + 明确的缺口说明，而不是 500。"""
    from lquant.security import service as S

    md = L.MarketData(symbol="600519.SH", asof=date(2026, 9, 30))
    md.notes.append("空湖")
    monkeypatch.setattr(S, "load_all", lambda sym, day: md)

    report = S.analyze_security("600519.SH", date(2026, 9, 30))
    assert report["score"]["score"] is None
    assert report["score"]["grade"] == "无法评分"
    assert all(a["available"] is False for a in report["angles"])
    assert report["verdict"]["risks"]
    import json
    json.dumps(report, allow_nan=False)


def test_resolve_asof_explicit_passthrough():
    assert L.resolve_asof("2026-09-30") == date(2026, 9, 30)
    assert L.resolve_asof(date(2026, 1, 5)) == date(2026, 1, 5)


def test_resolve_asof_defaults_to_lake_latest(monkeypatch):
    """缺省 asof 必须用湖内最新交易日，不是自然日 —— 否则会把「没同步」当「缺失」。"""
    import lquant.data.store.parquet as pq
    monkeypatch.setattr(pq, "latest_trade_date", lambda: date(2026, 9, 30))
    assert L.resolve_asof(None) == date(2026, 9, 30)
