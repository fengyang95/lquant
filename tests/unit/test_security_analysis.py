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

import math
from datetime import date, datetime, timedelta

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
            "amount": 5e8,          # 充足流动性（低流动性分支由专门用例覆盖）
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


def test_capital_empty_hint_is_actionable_when_symbol_known():
    """缺口要给出可执行的补救命令，而不是笼统的「表为空」。

    每日采集只装当天净流入榜前列，普通标的必须靠历史回填 ——
    提示里直接写出命令，用户才知道下一步做什么。
    """
    out = A.capital_angle(pl.DataFrame(), "600519.SH")
    assert out["available"] is False
    assert "lq data money-flow --symbols 600519.SH" in out["hint"]
    assert "600519.SH" in out["hint"]
    # 没给 symbol 时保持原来的通用文案（调用方不总是知道标的）
    generic = A.capital_angle(pl.DataFrame())
    assert "lq data money-flow" not in generic["hint"]


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


# ---------------------------------------------------------------- 取数层（loader）


def _industry_con(rows: list[tuple]) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE industry_classify (symbol VARCHAR, std VARCHAR, "
                "code VARCHAR, name VARCHAR, std_date DATE, source VARCHAR)")
    if rows:
        con.executemany("INSERT INTO industry_classify VALUES (?,?,?,?,?,?)", rows)
    return con


def test_load_industry_returns_industry_and_peers():
    con = _industry_con([
        ("600519.SH", "SW", "SP", "食品饮料", date(2020, 1, 1), "t"),
        ("000858.SZ", "SW", "SP", "食品饮料", date(2020, 1, 1), "t"),
        ("000001.SZ", "SW", "BK", "银行", date(2020, 1, 1), "t"),
    ])
    code, name, peers = L.load_industry(con, "600519.SH", date(2026, 9, 30))
    assert (code, name) == ("SP", "食品饮料")
    assert sorted(peers) == ["000858.SZ", "600519.SH"]


def test_load_industry_respects_asof_and_picks_latest_classification():
    """行业分类是**时点**数据：asof 之后生效的调整不能提前用上。"""
    con = _industry_con([
        ("600519.SH", "SW", "OLD", "旧行业", date(2020, 1, 1), "t"),
        ("600519.SH", "SW", "NEW", "新行业", date(2027, 1, 1), "t"),
    ])
    assert L.load_industry(con, "600519.SH", date(2026, 9, 30))[:2] == ("OLD", "旧行业")
    assert L.load_industry(con, "600519.SH", date(2027, 6, 1))[:2] == ("NEW", "新行业")


def test_load_industry_unknown_symbol_and_missing_table():
    con = _industry_con([])
    assert L.load_industry(con, "600519.SH", date(2026, 9, 30)) == (None, None, [])
    assert L.load_industry(duckdb.connect(), "600519.SH", date(2026, 9, 30)) \
        == (None, None, [])


def _flow_con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE money_flow (trade_date DATE, symbol VARCHAR, "
                "main_net_inflow DOUBLE, main_net_ratio DOUBLE, super_large_net DOUBLE, "
                "large_net DOUBLE, medium_net DOUBLE, small_net DOUBLE, change_pct DOUBLE)")
    con.executemany("INSERT INTO money_flow VALUES (?,?,?,?,?,?,?,?,?)", [
        (date(2026, 9, 1), "600519.SH", 1e7, 3.0, 0, 0, 0, 0, 1.0),
        (date(2026, 9, 20), "600519.SH", 2e7, 5.0, 0, 0, 0, 0, 1.0),
        (date(2026, 12, 1), "600519.SH", 9e7, 9.0, 0, 0, 0, 0, 1.0),  # asof 之后
        (date(2026, 9, 20), "000001.SZ", 1e7, 1.0, 0, 0, 0, 0, 1.0),
    ])
    return con


def test_load_money_flow_window_and_asof():
    con = _flow_con()
    df = L.load_money_flow(con, "600519.SH", date(2026, 9, 30), days=60)
    assert df.height == 2, "asof 之后（12-01）与窗口外（60 天前）的行都不该进来"
    assert df["trade_date"].max() == date(2026, 9, 20)


def test_load_money_flow_missing_table_degrades():
    assert L.load_money_flow(duckdb.connect(), "600519.SH", date(2026, 9, 30)).is_empty()


def test_load_money_flow_excludes_demo_rows():
    """合成数据不能进资金面评分。

    demo 采集写进 money_flow 的 200 个代码里，有 75 个能对上真实上市公司
    （如 300408.SZ），主键同为 (trade_date, symbol) —— 不过滤就是拿伪造的
    净流入给真实标的下结论。
    """
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE money_flow (trade_date DATE, symbol VARCHAR, name VARCHAR, "
        "main_net_inflow DOUBLE, main_net_ratio DOUBLE, super_large_net DOUBLE, "
        "large_net DOUBLE, medium_net DOUBLE, small_net DOUBLE, change_pct DOUBLE, "
        "source VARCHAR)")
    con.executemany("INSERT INTO money_flow VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
        # 带 source 的合成行
        (date(2026, 10, 6), "300408.SZ", "样例088", 9e7, 20.0, 0, 0, 0, 0, 1.0, "demo"),
        # 老库没有 source 列那一代的合成行（只能靠 name 认）
        (date(2026, 10, 2), "300408.SZ", "样例088", 8e7, 19.0, 0, 0, 0, 0, 1.0, None),
        # 真实历史回填行
        (date(2026, 9, 30), "300408.SZ", "三环集团", 1e7, 3.0, 0, 0, 0, 0, 1.0, "history"),
        # 老库的真实行（source 为 NULL）
        (date(2026, 9, 29), "300408.SZ", "三环集团", 2e7, 4.0, 0, 0, 0, 0, 1.0, None),
    ])
    df = L.load_money_flow(con, "300408.SZ", date(2026, 10, 6), days=60)
    assert df.height == 2, "两代合成行都必须被排除"
    assert df["trade_date"].to_list() == [date(2026, 9, 29), date(2026, 9, 30)]
    assert df["main_net_inflow"].to_list() == [2e7, 1e7]


def _news_con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE news_item (news_id VARCHAR, source VARCHAR, "
                "source_name VARCHAR, external_id VARCHAR, title VARCHAR, content VARCHAR, "
                "url VARCHAR, symbols VARCHAR[], industry_code VARCHAR, "
                "published_at TIMESTAMP, collected_at TIMESTAMP, quality_flags INTEGER, "
                "source_tag VARCHAR)")
    con.executemany("INSERT INTO news_item VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        ("n1", "s", "src", "e1", "标题1", "内容", "http://x", ["600519.SH"], None,
         datetime(2026, 9, 20, 10, 0), datetime(2026, 9, 20), 0, None),
        ("n2", "s", "src", "e2", "标题2", "内容", "http://y", ["600519.SH"], None,
         datetime(2026, 12, 1, 10, 0), datetime(2026, 12, 1), 0, None),   # asof 之后
        ("n3", "s", "src", "e3", "标题3", "内容", "http://z", ["000001.SZ"], None,
         datetime(2026, 9, 20, 10, 0), datetime(2026, 9, 20), 0, None),
    ])
    return con


def test_load_news_filters_by_symbol_and_asof():
    df = L.load_news(_news_con(), "600519.SH", date(2026, 9, 30))
    assert df["news_id"].to_list() == ["n1"]


def test_load_news_missing_table_degrades():
    assert L.load_news(duckdb.connect(), "600519.SH", date(2026, 9, 30)).is_empty()


def _sec_con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE security (symbol VARCHAR, name VARCHAR, sec_type VARCHAR, "
                "board VARCHAR, list_date DATE, delist_date DATE, is_st BOOLEAN, "
                "source VARCHAR, ingested_at TIMESTAMP)")
    con.execute("INSERT INTO security VALUES "
                "('600519.SH','贵州茅台','stock','主板',NULL,NULL,false,'t',NULL)")
    return con


def test_load_meta_found_missing_and_no_table():
    con = _sec_con()
    assert L.load_meta(con, "600519.SH")["name"] == "贵州茅台"
    assert L.load_meta(con, "999999.SH") == {}
    assert L.load_meta(duckdb.connect(), "600519.SH") == {}


def test_meta_tuple_handles_null_is_st():
    assert L._meta_tuple({"name": "甲", "sec_type": "stock"}) == ("甲", "stock", None, False)
    assert L._meta_tuple({"is_st": None}) == (None, None, None, False)
    assert L._meta_tuple({"is_st": True})[-1] is True


def test_load_bars_degrades_when_lake_missing(monkeypatch):
    import lquant.data.store.parquet as pq
    monkeypatch.setattr(pq, "read_daily",
                        lambda *a, **k: pl.DataFrame(schema={"symbol": pl.String,
                                                             "trade_date": pl.Date,
                                                             "close": pl.Float64}).lazy())
    assert L.load_bars("600519.SH", date(2026, 9, 30)).is_empty()


def test_load_bars_sorts_and_selects(monkeypatch):
    import lquant.data.store.parquet as pq
    raw = pl.DataFrame({
        "symbol": ["600519.SH"] * 2,
        "trade_date": [date(2026, 9, 30), date(2026, 9, 29)],
        "open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0],
        "close": [10.0, 9.0], "volume": [1.0, 2.0], "amount": [1.0, 2.0],
        "turnover_rate": [1.0, 2.0],
    })
    monkeypatch.setattr(pq, "read_daily", lambda *a, **k: raw.lazy())
    out = L.load_bars("600519.SH", date(2026, 9, 30))
    assert out["trade_date"].to_list() == [date(2026, 9, 29), date(2026, 9, 30)]
    assert "turnover_rate" in out.columns


def test_load_valuation_splits_own_history_and_cross_section(monkeypatch):
    """截面只保留最后一个交易日 —— 否则「全市场分位」会混进多个交易日。"""
    import lquant.data.store.parquet as pq

    own = pl.DataFrame({
        "symbol": ["600519.SH"] * 2, "trade_date": [date(2026, 9, 29), date(2026, 9, 30)],
        "pe_ttm": [30.0, 31.0],
    })
    cross = pl.DataFrame({
        "symbol": ["600519.SH", "000001.SZ", "600519.SH"],
        "trade_date": [date(2026, 9, 29), date(2026, 9, 29), date(2026, 9, 30)],
        "pe_ttm": [20.0, 5.0, 31.0],
    })
    calls: list[dict] = []

    def fake(start=None, end=None, symbols=None):
        calls.append({"start": start, "end": end, "symbols": symbols})
        return own if symbols else cross

    monkeypatch.setattr(pq, "read_daily_basic", fake)
    a, b = L.load_valuation("600519.SH", date(2026, 9, 30))
    assert a.height == 2, "本票历史应完整保留"
    # 截面只留最后一个交易日：09-29 的两行必须被丢掉
    assert b.height == 1
    assert b["trade_date"].unique().to_list() == [date(2026, 9, 30)]
    assert b["symbol"].to_list() == ["600519.SH"]
    assert calls[0]["symbols"] == ["600519.SH"], "本票历史必须带 symbols 过滤"


def test_load_valuation_degrades_on_read_error(monkeypatch):
    import lquant.data.store.parquet as pq

    def boom(*a, **k):
        raise RuntimeError("湖读不了")

    monkeypatch.setattr(pq, "read_daily_basic", boom)
    a, b = L.load_valuation("600519.SH", date(2026, 9, 30))
    assert a.is_empty() and b.is_empty()


def test_load_benchmark_returns_empty_when_no_candidate_has_data(monkeypatch):
    monkeypatch.setattr(L, "_read_index",
                        lambda s, a, b: pl.DataFrame(schema={"trade_date": pl.Date,
                                                             "close": pl.Float64}))
    df, code = L.load_benchmark(date(2026, 9, 30))
    assert df.is_empty() and code is None


def test_load_all_orchestrates_and_records_notes(monkeypatch):
    """load_all 把各数据源拼成一个 MarketData，并把缺口写成 notes。"""
    from lquant.core import db

    con = duckdb.connect()
    monkeypatch.setattr(db, "reader", lambda: _ctx(con))
    monkeypatch.setattr(L, "load_bars", lambda s, d: pl.DataFrame())
    monkeypatch.setattr(L, "load_benchmark", lambda d: (pl.DataFrame(), None))
    monkeypatch.setattr(L, "load_valuation", lambda s, d: (pl.DataFrame(), pl.DataFrame()))
    monkeypatch.setattr(L, "load_financial_own", lambda c, s, d: pl.DataFrame())
    monkeypatch.setattr(L, "load_financial_cross", lambda c, s, d: pl.DataFrame())
    monkeypatch.setattr(L, "load_money_flow", lambda c, s, d: pl.DataFrame())
    monkeypatch.setattr(L, "load_news", lambda c, s, d: pl.DataFrame())

    md = L.load_all("600519.SH", date(2026, 9, 30))
    assert md.symbol == "600519.SH" and md.bars.is_empty()
    # 行情与财务都缺 → 两条 note 都必须出现（前端据此提示怎么补数据）
    joined = " ".join(md.notes)
    assert "没有日线数据" in joined and "财务数据" in joined


def test_load_all_uses_industry_peers_plus_self(monkeypatch):
    """行业截面必须含本票自己 —— 分位是「自己在同业中的位置」。"""
    from lquant.core import db

    captured: list[list[str]] = []
    monkeypatch.setattr(db, "reader", lambda: _ctx(duckdb.connect()))
    monkeypatch.setattr(L, "load_bars", lambda s, d: pl.DataFrame())
    monkeypatch.setattr(L, "load_benchmark", lambda d: (pl.DataFrame(), None))
    monkeypatch.setattr(L, "load_valuation", lambda s, d: (pl.DataFrame(), pl.DataFrame()))
    monkeypatch.setattr(L, "load_financial_own", lambda c, s, d: pl.DataFrame())
    monkeypatch.setattr(L, "load_money_flow", lambda c, s, d: pl.DataFrame())
    monkeypatch.setattr(L, "load_news", lambda c, s, d: pl.DataFrame())
    monkeypatch.setattr(L, "load_industry",
                        lambda c, s, d: ("SP", "食品饮料", ["000858.SZ"]))
    monkeypatch.setattr(L, "load_financial_cross",
                        lambda c, syms, d: (captured.append(list(syms)), pl.DataFrame())[1])

    md = L.load_all("600519.SH", date(2026, 9, 30))
    assert captured[0] == ["000858.SZ", "600519.SH"]
    assert md.industry == "食品饮料" and md.peer_count == 2


class _ctx:
    """把连接包成 contextmanager（load_all 用 ``with reader() as con``）。"""

    def __init__(self, con):
        self.con = con

    def __enter__(self):
        return self.con

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------- 数学辅助函数


def test_sig_explicit_signal_wins():
    assert A._sig(0.9, C.NEUTRAL_SIGNAL) == C.NEUTRAL_SIGNAL
    assert A._sig(0.9) == C.BULLISH
    assert A._sig(-0.9) == C.BEARISH
    assert A._sig(0.0) == C.NEUTRAL_SIGNAL


def test_f_guards_none_nan_and_nonnumeric():
    assert A._f(None) is None
    assert A._f("不是数字") is None
    assert A._f(float("nan")) is None
    assert A._f(float("inf")) is None
    assert A._f("3.5") == 3.5


def test_tanh_norm_guards():
    assert A._tanh_norm(None, 1.0) is None
    assert A._tanh_norm(1.0, 0.0) is None        # scale 非法
    assert A._tanh_norm(0.0, 1.0) == 0.0


def test_pct_none_passthrough():
    assert A._pct(None) is None
    assert A._pct(1.234) == "1.23%"
    assert A._pct(1.234, digits=1, suffix="倍") == "1.2倍"


def test_ret_guards_short_series_and_zero_base():
    assert A._ret([], 5) is None
    assert A._ret([1.0] * 3, 5) is None          # 长度不足
    assert A._ret([0.0, 1.0], 1) is None         # 分母 <= 0
    assert A._ret([10.0, 11.0], 1) == pytest.approx(0.1)


def test_annualized_vol_and_drawdown_guards():
    assert A._annualized_vol([], 60) is None
    assert A._annualized_vol([0.01] * 5, 60) is None
    assert A._max_drawdown([]) is None
    assert A._max_drawdown([1.0]) is None
    assert A._max_drawdown([1.0, 0.5]) == pytest.approx(-50.0)


# ---------------------------------------------------------------- 技术面分支


def _stub_indicators(monkeypatch, **over):
    """把指标引擎的产出钉死，专测技术面的**判断分支**（而不是指标本身）。

    指标正确性由 ``tests/unit/test_indicators*.py`` 负责，这里只关心
    「RSI=60 该判多、RSI=40 该判空」这类映射有没有写反。
    """
    import lquant.indicators as ind

    def fake(df, names):
        last = float([c for c in df["close"].to_list() if c is not None][-1])
        base = {
            "ma5": last, "ma20": last, "ma60": last,
            "macd_dif": 0.0, "macd_dea": 0.0, "macd_hist": 0.0,
            "rsi14": 50.0, "kdj_j": 50.0,
            "boll_upper": last * 1.1, "boll_lower": last * 0.9,
            "volume_ratio": 1.0, "turnover_ma5": 1.0,
        }
        base.update(over)
        return df.with_columns([pl.lit(v).alias(k) for k, v in base.items()])

    monkeypatch.setattr(ind, "compute_many", fake)


@pytest.mark.parametrize(("rsi", "expect"), [(60.0, 1), (40.0, -1), (50.0, 0)])
def test_technical_rsi_bands(monkeypatch, rsi, expect):
    _stub_indicators(monkeypatch, rsi14=rsi)
    out = A.technical_angle(make_bars(n=80, trend=0.0))
    m = next(x for x in out["metrics"] if x["key"] == "rsi14")
    assert m["signal"] == (C.BULLISH if expect > 0 else
                           C.BEARISH if expect < 0 else C.NEUTRAL_SIGNAL)


def test_technical_kdj_overbought_and_oversold(monkeypatch):
    _stub_indicators(monkeypatch, kdj_j=120.0)
    hot = A.technical_angle(make_bars(n=80, trend=0.0))
    assert next(m for m in hot["metrics"] if m["key"] == "kdj_j")["signal"] == C.BEARISH

    _stub_indicators(monkeypatch, kdj_j=-20.0)
    cold = A.technical_angle(make_bars(n=80, trend=0.0))
    assert next(m for m in cold["metrics"] if m["key"] == "kdj_j")["signal"] == C.BULLISH


def test_technical_boll_breakout_branches(monkeypatch):
    """突破上轨要判超买、跌破下轨要判超卖（方向写反会给出相反建议）。"""
    import lquant.indicators as ind

    def fake_factory(upper_mult, lower_mult):
        def fake(df, names):
            last = float([c for c in df["close"].to_list() if c is not None][-1])
            return df.with_columns([
                pl.lit(last * upper_mult).alias("boll_upper"),
                pl.lit(last * lower_mult).alias("boll_lower"),
                pl.lit(50.0).alias("rsi14"), pl.lit(50.0).alias("kdj_j"),
                pl.lit(last).alias("ma5"), pl.lit(last).alias("ma20"),
                pl.lit(last).alias("ma60"),
                pl.lit(0.0).alias("macd_dif"), pl.lit(0.0).alias("macd_dea"),
                pl.lit(0.0).alias("macd_hist"),
                pl.lit(1.0).alias("volume_ratio"), pl.lit(1.0).alias("turnover_ma5"),
            ])
        return fake

    monkeypatch.setattr(ind, "compute_many", fake_factory(0.9, 0.8))   # %B > 1
    above = A.technical_angle(make_bars(n=80, trend=0.0))
    assert next(m for m in above["metrics"] if m["key"] == "boll_pctb")["signal"] == C.BEARISH

    monkeypatch.setattr(ind, "compute_many", fake_factory(1.2, 1.1))   # %B < 0
    below = A.technical_angle(make_bars(n=80, trend=0.0))
    assert next(m for m in below["metrics"] if m["key"] == "boll_pctb")["signal"] == C.BULLISH


def test_technical_degrades_when_indicator_engine_raises(monkeypatch):
    import lquant.indicators as ind

    def boom(df, names):
        raise RuntimeError("指标引擎炸了")

    monkeypatch.setattr(ind, "compute_many", boom)
    out = A.technical_angle(make_bars(n=80))
    assert out["available"] is False
    assert "技术指标" in (out["hint"] or "")


def test_technical_degrades_when_close_all_null(monkeypatch):
    bars = make_bars(n=80).with_columns(pl.lit(None, dtype=pl.Float64).alias("close"))
    _stub_indicators(monkeypatch)
    out = A.technical_angle(bars)
    assert out["available"] is False


def test_technical_skips_momentum_without_enough_history(monkeypatch):
    """只有 26 根日线时 60 日动量取不到 —— 该分支跳过而不是报错。"""
    _stub_indicators(monkeypatch)
    out = A.technical_angle(make_bars(n=26))
    keys = {m["key"] for m in out["metrics"]}
    assert "mom20" in keys and "mom60" not in keys


# ---------------------------------------------------------------- 基本面分支


def test_latest_financials_empty_frame():
    assert A._latest_financials(pl.DataFrame()) == {}


def test_fundamental_unsupported_items_not_scored():
    """有财务数据、但没有平台支持的口径 → 给 hint，不硬算。"""
    d = date(2026, 4, 1)
    fin = _fin_frame([("600519.SH", "income.total_revenue", 1e9, d)])
    out = A.fundamental_angle(fin, pl.DataFrame(), "食品饮料", 1, _CANON)
    assert out["available"] is False
    assert "口径" in out["hint"]


def test_fundamental_merges_multiple_candidate_names():
    """同一口径的多个候选名（不同数据源命名）要合并成同一个比较池。"""
    d = date(2026, 4, 1)
    canon = (("roe", "ROE", ("indicator.roe", "profit.roeAvg"), True),)
    fin = _fin_frame([("600519.SH", "profit.roeAvg", 30.0, d)])
    peers = _fin_frame(
        [("600519.SH", "profit.roeAvg", 30.0, d)]
        + [(f"00000{i}.SZ", "indicator.roe", 5.0, d) for i in range(1, 8)]
    )
    out = A.fundamental_angle(fin, peers, "食品饮料", 8, canon)
    m = out["metrics"][0]
    assert m["percentile"] == 100.0, "候选名各自的样本要合并后才能算满分位"


# ---------------------------------------------------------------- 估值 / 资金 / 相对


def test_valuation_uses_market_cross_section_percentile():
    own = make_valuation_history(pe_start=40.0, pe_end=10.0)
    cross = pl.DataFrame({
        "symbol": [f"{i:06d}.SZ" for i in range(10)],
        "trade_date": [date(2026, 9, 30)] * 10,
        "pe_ttm": [5.0, 8.0, 12.0, 20.0, 25.0, 30.0, 40.0, 50.0, 60.0, 80.0],
        "pb_mrq": [1.0] * 10, "ps_ttm": [1.0] * 10, "dv_ttm": [1.0] * 10,
    })
    out = A.valuation_angle(own, cross)
    pe = next(m for m in out["metrics"] if m["key"] == "val_pe_ttm")
    assert pe["note"] and "全市场" in pe["note"], "有截面时必须报全市场分位"


def test_valuation_insufficient_history_not_scored():
    own = make_valuation_history(n=10, pe_start=40.0, pe_end=10.0)
    out = A.valuation_angle(own, pl.DataFrame())
    assert out["available"] is True
    assert out["score"] is None
    assert out["hint"] and "历史不足" in out["hint"]


def test_capital_without_ratio_field_not_scored():
    """只有净流入额、没有净占比时不能评分（量纲不可比）。"""
    flow = _flow(20).with_columns(pl.lit(None, dtype=pl.Float64).alias("main_net_ratio"))
    out = A.capital_angle(flow)
    assert out["available"] is False
    assert out["metrics"], "原始金额仍应展示"


def test_relative_benchmark_too_short_degrades():
    bars = make_bars(n=140)
    bench = pl.DataFrame({"trade_date": [date(2026, 9, 30)], "close": [4000.0]})
    out = A.relative_angle(bars, bench, "000300.SH", "食品饮料")
    assert out["available"] is False and out["hint"]


def test_relative_insufficient_overlap_degrades():
    """基准只有 5 天，任何窗口都算不出超额 → 明说数据不足。"""
    bars = make_bars(n=140)
    end = bars["trade_date"].max()
    bench = pl.DataFrame({
        "trade_date": [end - timedelta(days=d) for d in range(5)],
        "close": [4000.0] * 5,
    })
    out = A.relative_angle(bars, bench, "000300.SH", None)
    assert out["available"] is False
    assert out["hint"]


# ---------------------------------------------------------------- 消息面分支


def test_news_counts_recent_and_skips_null_published_at():
    df = pl.DataFrame({
        "news_id": ["a", "b", "c"],
        "title": ["近的", "远的", "无时间"],
        "source_name": ["s"] * 3,
        "published_at": [datetime(2026, 9, 28, 9, 0), datetime(2026, 9, 1, 9, 0), None],
        "url": [None] * 3,
    })
    out = A.news_angle(df, date(2026, 9, 30))
    keys = {m["key"]: m["value"] for m in out["metrics"]}
    assert keys["news_total"] == 3
    assert keys["news_recent"] == 1, "只有 09-28 那条在 7 天内"
    assert out["score"] is None


# ---------------------------------------------------------------- 风险提示


def _risky_bars(n=260, vol=False, decline=False, amount=5e8, jump=False):
    rows = []
    d0 = date(2025, 1, 1)
    c = 100.0
    for i in range(n):
        if decline:
            c *= 0.99
        elif vol:
            c *= 1.12 if i % 2 == 0 else 0.85
        elif jump:
            c *= 1.10 if i % 3 == 0 else 0.985
        else:
            c *= 1.001
        rows.append({"trade_date": d0 + timedelta(days=i), "open": c, "high": c * 1.01,
                     "low": c * 0.99, "close": c, "volume": 1e6,
                     "turnover_rate": 1.0, "amount": amount})
    return pl.DataFrame(rows)


def _beta_pair(mult: float = 2.0, n: int = 200):
    """构造「个股日收益 = mult × 基准日收益」的一对序列（可解析验证 Beta）。"""
    d0 = date(2025, 1, 1)
    sb = bb = 100.0
    brows, mrows = [], []
    for i in range(n):
        r = 0.01 * math.sin(i / 3.0)          # 基准收益，有正有负、方差非零
        sb *= (1 + mult * r)
        bb *= (1 + r)
        d = d0 + timedelta(days=i)
        brows.append({"trade_date": d, "open": sb, "high": sb, "low": sb,
                      "close": sb, "volume": 1e6, "turnover_rate": 1.0,
                      "amount": 5e8})
        mrows.append({"trade_date": d, "close": bb})
    return pl.DataFrame(brows), pl.DataFrame(mrows)
    assert A.risk_block(pl.DataFrame(), pl.DataFrame(), None)["available"] is False
    short = A.risk_block(_risky_bars(n=30), pl.DataFrame(), None)
    assert short["available"] is True
    assert any("长期指标" in f for f in short["flags"]), "日线不足 120 根要提示"


def test_risk_block_flags_high_volatility_and_drawdown():
    vol = A.risk_block(_risky_bars(vol=True), pl.DataFrame(), None)
    assert any("波动" in f and "偏高" in f for f in vol["flags"])

    dd = A.risk_block(_risky_bars(decline=True), pl.DataFrame(), None)
    assert any("回撤" in f for f in dd["flags"])


def test_risk_block_flags_low_liquidity_and_st():
    illiquid = A.risk_block(_risky_bars(amount=5e6), pl.DataFrame(), None)
    assert any("流动性偏弱" in f for f in illiquid["flags"])
    assert any(m["key"] == "liquidity" for m in illiquid["metrics"])

    st = A.risk_block(_risky_bars(), pl.DataFrame(), None, {"is_st": True})
    assert any("ST" in f for f in st["flags"])


def test_risk_block_counts_limit_days():
    out = A.risk_block(_risky_bars(jump=True), pl.DataFrame(), None)
    m = next((x for x in out["metrics"] if x["key"] == "limit_days"), None)
    assert m is not None and m["value"] > 0


def test_risk_block_computes_beta_and_flags_high_exposure():
    """个股日收益恰为基准的两倍 → Beta≈2，且触发高敞口警示。

    基准收益必须有波动：恒定收益的基准方差为 0，Beta 无定义（会被正确跳过）。
    """
    bars, bench = _beta_pair(mult=2.0)
    out = A.risk_block(bars, bench, "000300.SH")
    beta = next(m for m in out["metrics"] if m["key"] == "beta")
    assert beta["value"] == pytest.approx(2.0, abs=0.15)
    assert any("Beta" in f for f in out["flags"])


def test_risk_block_skips_beta_when_benchmark_has_no_variance():
    """基准完全不动时 Beta 无定义 —— 不能除零，也不能报一个假值。"""
    bars = _risky_bars(n=260)
    flat = bars.select(["trade_date"]).with_columns(pl.lit(100.0).alias("close"))
    out = A.risk_block(bars, flat, "000300.SH")
    assert not any(m["key"] == "beta" for m in out["metrics"])


def test_risk_block_not_scored_by_contract():
    out = A.risk_block(_risky_bars(), pl.DataFrame(), None)
    assert out["scored"] is False, "风险块只做警示，不能混进综合分"


# ---------------------------------------------------------------- 结论补充分支


def test_verdict_mentions_unscored_angle_and_flags_low_data_coverage():
    angles = [_angle("news", None, 0.0), _angle("technical", 60.0, 1.0, coverage=0.2)]
    score = composite(angles)
    v = build_verdict(angles, score, {"flags": []}, {})
    assert any("不评分" in p for p in v["points"]), "未评分角度也要出现在结论里"
    assert any("数据覆盖" in r for r in v["risks"])


def test_grade_handles_negative_score():
    assert C.grade(-1.0) == "显著偏空"


def test_num_handles_numpy_scalars_and_unconvertible_objects():
    import numpy as np

    assert C._num(np.float64(3.5)) == 3.5
    assert C._num(np.array(7)) == 7
    # 多元素数组 .item() 抛 ValueError → 退化成字符串而不是 500
    assert isinstance(C._num(np.array([1, 2])), str)
    # 无法 float() 的对象 → 字符串
    assert isinstance(C._num(object()), str)


def test_percentile_rank_nonnumeric_x():
    assert C.percentile_rank([1.0, 2.0], "abc") is None
    assert C.percentile_rank([1.0, 2.0], None) is None


# ---------------------------------------------------------------- loader 兜底分支


class _BrokenCon:
    """所有查询都抛：模拟表未建 / 库被锁。"""

    def execute(self, *a, **k):
        raise RuntimeError("表不存在")


def test_load_financial_cross_degrades_on_read_error():
    assert L.load_financial_cross(_BrokenCon(), ["600519.SH"], date(2026, 9, 30)).is_empty()


def test_load_industry_peers_read_error_keeps_industry():
    """行业查到了、但同业列表查询失败 → 保留行业，同业退化为空。"""

    class _HalfBroken:
        def __init__(self):
            self.n = 0

        def execute(self, sql, params=None):
            self.n += 1
            if self.n == 1:
                return _Rows([("SP", "食品饮料")])
            raise RuntimeError("同业查询失败")

    code, name, peers = L.load_industry(_HalfBroken(), "600519.SH", date(2026, 9, 30))
    assert (code, name, peers) == ("SP", "食品饮料", [])


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


def test_read_index_queries_index_daily(monkeypatch):
    from lquant.core import db

    con = duckdb.connect()
    con.execute("CREATE TABLE index_daily (symbol VARCHAR, trade_date DATE, close DOUBLE)")
    con.executemany("INSERT INTO index_daily VALUES (?,?,?)", [
        ("000300.SH", date(2026, 9, 29), 4000.0),
        ("000300.SH", date(2026, 9, 30), 4010.0),
        ("000300.SH", date(2026, 12, 1), 9999.0),      # 区间外
    ])
    monkeypatch.setattr(db, "reader", lambda: _ctx(con))
    out = L._read_index("000300.SH", date(2026, 9, 1), date(2026, 9, 30))
    assert out.height == 2 and out["close"].max() == 4010.0


def test_load_financial_own_returns_empty_frame_on_empty_table():
    con = duckdb.connect()
    con.execute("CREATE TABLE financial_pit (symbol VARCHAR, stat_date DATE, "
                "pub_date DATE, report_type VARCHAR, item VARCHAR, value DOUBLE, "
                "unit VARCHAR, source VARCHAR)")
    out = L.load_financial_own(con, "600519.SH", date(2026, 9, 30))
    assert out.is_empty()
