"""归因分析测试：贡献守恒、Brinson 加总=超额、风险指标。"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from lquant.backtest.attribution import (
    brinson_by_group,
    brinson_monthly,
    group_of_symbol,
    risk_vs_benchmark,
    stock_contribution,
)


def _scenario():
    """两天、两股：day1 买入 A 全仓，day2 A +5% / B -3%。

    nav: day1 = 1_000_000（当日买入，收盘价=开盘价，无浮盈）
         day2 = 1_000_000 + 持仓 9900 股 × 105 - 费用（零费假设 990,000→1,039,500）
    """
    d1, d2 = date(2026, 1, 5), date(2026, 1, 6)
    positions = {d1: {"600000.SH": 9900.0}, d2: {"600000.SH": 9900.0}}
    prices = {"600000.SH": {d1: 100.0, d2: 105.0},
              "000001.SZ": {d1: 50.0, d2: 48.5}}
    nav = [(d1, 1_000_000.0), (d2, 500_000.0 + 9900 * 105.0)]
    return d1, d2, positions, prices, nav


def test_stock_contribution_sums_to_total():
    """个股贡献 + 现金/择时残差 ≈ 当日总收益（守恒）。"""
    d1, d2, positions, prices, nav = _scenario()
    stocks, residual = stock_contribution(positions, prices, nav)
    assert len(stocks) == 1
    total_ret = nav[1][1] / nav[0][1] - 1
    explained = stocks[0]["contribution"] + residual[0]["residual"]
    assert explained == pytest.approx(total_ret, abs=1e-6)
    # 个股贡献本身 = 权重 × 涨幅 = (990000/1000000) × 5%
    assert stocks[0]["contribution"] == pytest.approx(0.99 * 0.05, abs=1e-6)


def test_brinson_group_sum_equals_excess():
    """分组 Brinson：Σ(配置+选股+交互) 应与组合相对基准的总超额一致（同口径）。"""
    days = [date(2026, 1, 5 + i) for i in range(4)]
    prices = {
        "600000.SH": {days[i]: 100 * (1.01 ** i) for i in range(4)},
        "600519.SH": {days[i]: 100 * (1.03 ** i) for i in range(4)},
        "000001.SZ": {days[i]: 50 * (0.99 ** i) for i in range(4)},
        "300750.SZ": {days[i]: 80 * (1.02 ** i) for i in range(4)},
    }
    # 组合：满仓沪市两股；基准池：全部 4 只等权
    positions = {d: {"600000.SH": 5000.0, "600519.SH": 5000.0} for d in days}
    nav = [(days[0], 1_000_000.0)]
    for i in range(1, 4):
        v = 5000 * prices["600000.SH"][days[i]] + 5000 * prices["600519.SH"][days[i]]
        nav.append((days[i], v))
    group_map = {s: group_of_symbol(s) for s in prices}
    out = brinson_by_group(positions, prices, nav, list(prices), group_map)
    assert out["groups"]
    total = sum(g["total"] for g in out["groups"])
    # 手动算组合 vs 基准超额（每日 Σ w_i r_i - Σ wb_i rb_i，日复利差）
    port_ret, bench_ret = 1.0, 1.0
    for i in range(1, 4):
        d = days[i]
        pr = 0.5 * (prices["600000.SH"][d] / prices["600000.SH"][days[i - 1]] - 1) \
           + 0.5 * (prices["600519.SH"][d] / prices["600519.SH"][days[i - 1]] - 1)
        br = sum(prices[s][d] / prices[s][days[i - 1]] - 1 for s in prices) / 4
        port_ret *= 1 + pr
        bench_ret *= 1 + br
    manual_excess = port_ret / bench_ret - 1
    # 算术 Brinson 与几何超额同数量级、符号一致（口径差在 2% 以内）
    assert total == pytest.approx(manual_excess, abs=0.02)


def test_group_of_symbol_boards():
    assert group_of_symbol("600000.SH") == "沪市主板"
    assert group_of_symbol("000001.SZ") == "深市主板"
    assert group_of_symbol("300750.SZ") == "创业板"
    assert group_of_symbol("688981.SH") == "科创板"
    assert group_of_symbol("832000.BJ") == "北交所"


def test_risk_vs_benchmark_alpha_beta():
    """β=1 的完美跟踪：α≈0、β≈1；加恒定日超额 → IR 高。"""
    import random

    random.seed(42)
    bench = [random.gauss(0, 0.01) for _ in range(120)]
    strat = [b + 0.0005 for b in bench]          # β=1，每日恒定超额 5bp
    out = risk_vs_benchmark(strat, bench)
    assert out["beta"] == pytest.approx(1.0, abs=0.01)
    assert out["alpha_annual"] == pytest.approx(0.0005 * 252, rel=0.05)
    # 恒定日超额 → 超额波动 0 → IR 记 None（∞ 语义）；总超额必为正
    assert out["information_ratio"] is None or out["information_ratio"] > 3
    assert out["excess_return"] > 0


# ---------------- 月度 Brinson ----------------

def test_brinson_monthly_sums_to_total():
    """月度分段与全期共用逐日核心：Σ各月 total = 全期 total（加法性）。"""
    days = [date(2026, 1, 5), date(2026, 1, 6),
            date(2026, 2, 2), date(2026, 2, 3)]
    prices = {
        "600000.SH": {d: 100 * (1.01 ** i) for i, d in enumerate(days)},
        "600519.SH": {d: 100 * (1.03 ** i) for i, d in enumerate(days)},
        "000001.SZ": {d: 50 * (0.99 ** i) for i, d in enumerate(days)},
        "300750.SZ": {d: 80 * (1.02 ** i) for i, d in enumerate(days)},
    }
    positions = {d: {"600000.SH": 5000.0, "600519.SH": 5000.0} for d in days}
    nav = [(days[0], 1_000_000.0)]
    for i in range(1, 4):
        v = 5000 * prices["600000.SH"][days[i]] + 5000 * prices["600519.SH"][days[i]]
        nav.append((days[i], v))
    group_map = {s: group_of_symbol(s) for s in prices}

    full = brinson_by_group(positions, prices, nav, list(prices), group_map)
    monthly = brinson_monthly(positions, prices, nav, list(prices), group_map)
    assert [m["month"] for m in monthly["months"]] == ["2026-01", "2026-02"]
    assert sum(m["excess_total"] for m in monthly["months"]) == pytest.approx(
        full["excess_total"], abs=1e-9)
    # 每月内部：Σ组 total = 月超额
    for m in monthly["months"]:
        assert sum(g["total"] for g in m["groups"]) == pytest.approx(
            m["excess_total"], abs=1e-9)


# ---------------- 成本拖累 ----------------

def test_cost_drag_divides_by_prev_nav():
    """费用拖累 = 当日费用 / 前一净值日净值，逐日可加总。"""
    from lquant.backtest.attribution import cost_drag

    d1, d2, d3 = date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)
    nav = [(d1, 1_000_000.0), (d2, 1_100_000.0), (d3, 1_200_000.0)]
    orders = [{"ts": f"{d2} 09:31:00", "fee": 100.0},
              {"ts": str(d2), "fee": 50.0},        # 同日两笔合并
              {"ts": str(d3), "fee": 120.0}]
    out = cost_drag(orders, nav)
    assert len(out["fee_by_day"]) == 2
    assert out["fee_by_day"][0]["fee"] == pytest.approx(150.0)
    assert out["fee_by_day"][0]["drag"] == pytest.approx(150.0 / 1_000_000.0, abs=1e-10)
    assert out["fee_by_day"][1]["drag"] == pytest.approx(120.0 / 1_100_000.0, abs=1e-8)
    assert out["total_fee"] == pytest.approx(270.0)
    assert out["total_drag"] == pytest.approx(
        out["fee_by_day"][0]["drag"] + out["fee_by_day"][1]["drag"], abs=1e-10)


def test_cost_drag_skips_bad_rows():
    from lquant.backtest.attribution import cost_drag

    nav = [(date(2026, 1, 5), 1_000_000.0)]
    out = cost_drag([("垃圾", 10.0), ("2026-01-05", None), ("2026-01-05", 30.0)], nav)
    assert len(out["fee_by_day"]) == 1
    assert out["total_fee"] == pytest.approx(30.0)


# ---------------- 持仓画像 ----------------

def test_portfolio_profile_concentration_and_industry():
    """50/50 两持仓：HHI=0.5、Top5=100%；行业权重按板块聚合。"""
    from lquant.backtest.attribution import portfolio_profile

    d1, d2 = date(2026, 1, 5), date(2026, 1, 6)
    positions = {d1: {"600000.SH": 1000.0, "300750.SZ": 1000.0},
                 d2: {"600000.SH": 1000.0}}
    prices = {"600000.SH": {d1: 10.0, d2: 11.0},
              "300750.SZ": {d1: 10.0, d2: 9.0}}
    nav = [(d1, 20_000.0), (d2, 11_000.0)]
    group_map = {"600000.SH": "沪市主板", "300750.SZ": "创业板"}
    p = portfolio_profile(positions, prices, nav, group_map)

    assert p["industry"]["dates"] == [str(d1), str(d2)]
    ind_d1 = dict(zip(p["industry"]["dates"], p["industry"]["series"]["沪市主板"],
                     strict=False))
    assert ind_d1[str(d1)] == pytest.approx(0.5, abs=1e-9)
    assert ind_d1[str(d2)] == pytest.approx(1.0, abs=1e-9)
    # 创业板 day2 无持仓 → 0
    assert p["industry"]["series"]["创业板"][1] == 0.0

    assert len(p["concentration"]) == 2
    c1 = p["concentration"][0]
    assert c1["hhi"] == pytest.approx(0.5, abs=1e-9)
    assert c1["top5"] == pytest.approx(1.0, abs=1e-9)
    assert c1["n_pos"] == 2
    c2 = p["concentration"][1]
    assert c2["hhi"] == pytest.approx(1.0, abs=1e-9)


def test_portfolio_profile_style_weighted_mean():
    from lquant.backtest.attribution import portfolio_profile

    d1 = date(2026, 1, 5)
    positions = {d1: {"600000.SH": 3000.0, "300750.SZ": 1000.0}}
    prices = {"600000.SH": {d1: 10.0}, "300750.SZ": {d1: 10.0}}
    nav = [(d1, 40_000.0)]
    styles = {"600000.SH": {d1: {"momentum_20d": 0.1}},
              "300750.SZ": {d1: {"momentum_20d": 0.5}}}
    p = portfolio_profile(positions, prices, nav, {}, styles=styles)
    # 权重 0.75/0.25 → 0.1*0.75 + 0.5*0.25 = 0.2
    assert p["style"]["series"]["momentum_20d"][0] == pytest.approx(0.2, abs=1e-9)


# ---------------- 风格收益归因 ----------------

def _synthetic_panel(n=300, t=70, seed=7):
    """n 只 × t 日随机行情面板（含 total_mv），供截面回归。"""
    import random

    import polars as pl

    random.seed(seed)
    rows = []
    for i in range(n):
        px = 10.0 + random.random()
        mv = 1e9 * (1 + random.random())
        for j in range(t):
            d = _bdays(j)
            r = random.gauss(0, 0.02)
            px *= 1 + r
            mv *= 1 + random.gauss(0, 0.01)
            rows.append({"trade_date": d, "symbol": f"{600000 + i}.SH",
                         "close": px, "total_mv": mv})
    return pl.DataFrame(rows)


def _bdays(n: int):
    """从 2026-01-02 起第 n 个工作日（周末跳过，测试够用）。"""
    from datetime import timedelta

    d = date(2026, 1, 2) + timedelta(days=n)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def test_style_return_attribution_conserves():
    """守恒：Σ因子贡献 = common；common + specific = 算术累计收益。"""
    from lquant.backtest.attribution import style_return_attribution

    panel = _synthetic_panel()
    dates = sorted(panel["trade_date"].unique().to_list())
    import polars as pl

    # 持有 5 只等量，价格取自面板
    held = panel.filter(pl.col("symbol").is_in(
        sorted(panel["symbol"].unique().to_list())[:5]))
    prices: dict[str, dict] = {}
    for r in held.iter_rows(named=True):
        prices.setdefault(r["symbol"], {})[r["trade_date"]] = r["close"]
    positions = {d: {s: 100.0 for s in prices} for d in dates}
    first = next(iter(prices))
    nav = [(dates[0], 500.0 * prices[first][dates[0]])]
    for i in range(1, len(dates)):
        v = sum(100.0 * prices[s][dates[i]] for s in prices)
        nav.append((dates[i], v))

    out = style_return_attribution(positions, prices, nav, panel)
    assert out.get("dates"), out
    # totals 各值经 round(6) 再求和，Σ因子 与 common 最多差 (n_factors+1)*5e-7
    fsum = sum(out["totals"][k] for k in out["factors"])
    assert fsum == pytest.approx(out["totals"]["common"], abs=1e-5)
    assert out["totals"]["common"] + out["totals"]["specific"] == pytest.approx(
        out["totals"]["ret_arith"], abs=1e-5)
    # 累计序列长度一致且单调记录
    assert len(out["common_cum"]) == len(out["dates"]) == len(out["specific_cum"])


def test_style_return_attribution_insufficient_sample():
    """截面 < 50 只：明确说「样本不足」，绝不硬算。"""
    from lquant.backtest.attribution import style_return_attribution

    panel = _synthetic_panel(n=30, t=40)
    dates = sorted(panel["trade_date"].unique().to_list())
    import polars as pl

    held = panel.filter(pl.col("symbol").is_in(sorted(panel["symbol"].unique().to_list())[:3]))
    prices = {}
    for r in held.iter_rows(named=True):
        prices.setdefault(r["symbol"], {})[r["trade_date"]] = r["close"]
    positions = {d: {s: 100.0 for s in prices} for d in dates}
    nav = [(dates[0], 300.0)] + [
        (dates[i], sum(100.0 * prices[s][dates[i]] for s in prices))
        for i in range(1, len(dates))]
    out = style_return_attribution(positions, prices, nav, panel)
    assert "note" in out and not out.get("dates")


def test_build_styles_optional_columns():
    """全列 → 6 风格；缺列安静收缩；缺 close 直接空。"""
    import polars as pl

    from lquant.backtest.attribution import build_styles

    rows = []
    for j in range(25):
        d = _bdays(j)
        rows.append({"trade_date": d, "symbol": "600000.SH", "close": 10 + j,
                     "total_mv": 1e9, "pe_ttm": 20.0, "pb_mrq": 2.0,
                     "turnover_rate": 1.5})
    full = build_styles(pl.DataFrame(rows))
    row = full["600000.SH"][_bdays(24)]
    assert set(row) == {"size", "value_ep", "value_bp", "liquidity",
                        "momentum_20d", "volatility_20d"}
    assert row["value_ep"] == pytest.approx(0.05, abs=1e-9)

    slim = build_styles(pl.DataFrame(rows).drop(["total_mv", "pe_ttm", "pb_mrq",
                                                 "turnover_rate"]))
    first_row = next(iter(next(iter(slim.values())).values()))
    assert set(first_row) <= {"momentum_20d", "volatility_20d"}
    # symbols 过滤
    assert build_styles(pl.DataFrame(rows), symbols=["000001.SZ"]) == {}


def test_style_return_attribution_guards():
    """面板缺列 / 因子集为空 / 持仓全在回归截面外 → 明确 note，不硬算。"""
    import polars as pl

    from lquant.backtest.attribution import style_return_attribution

    d1 = date(2026, 1, 5)
    nav = [(d1, 100.0)]
    positions = {d1: {"600000.SH": 100.0}}
    prices = {"600000.SH": {d1: 10.0}}

    # 缺 close → note
    bad = pl.DataFrame({"trade_date": [d1], "symbol": ["600000.SH"]})
    assert "note" in style_return_attribution(positions, prices, nav, bad)
    # 指定的因子都不可用 → note
    panel = _synthetic_panel(n=120, t=30)
    assert "note" in style_return_attribution(positions, prices, nav, panel,
                                              factors=["size"])
    # 持仓标的完全不在面板/价格里 → 全部跳过 → note
    empty_pos = {d1: {"999999.SH": 100.0}}
    assert "note" in style_return_attribution(empty_pos, {}, nav, panel)


# ---------------- 回撤期归因 ----------------

def test_drawdown_periods_identifies_recovered_and_open():
    """收复段与未收复段都要识别；<阈值的小波动不进列表。"""
    from lquant.backtest.attribution import drawdown_periods

    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(8)]
    nav = [(days[0], 100), (days[1], 110), (days[2], 104), (days[3], 99),
           (days[4], 108), (days[5], 111), (days[6], 100), (days[7], 92.4)]
    ps = drawdown_periods(nav, 0.05)
    assert len(ps) == 2
    # 未收复段更深，排前面
    deep, rec = ps
    assert deep["start"] == days[5] and deep["end"] is None
    assert deep["drawdown"] == pytest.approx(1 - 92.4 / 111, abs=1e-9)
    assert rec["start"] == days[1] and rec["end"] == days[5]
    assert rec["drawdown"] == pytest.approx(0.10, abs=1e-9)
    assert rec["ret"] == pytest.approx(111 / 110 - 1, abs=1e-9)
    # 阈值抬高后只剩深的那段
    assert len(drawdown_periods(nav, 0.15)) == 1
    assert drawdown_periods(nav, 0.5) == []


def test_drawdown_attribution_splits_factors_and_stocks():
    """回撤窗口内：个股贡献与因子拆分口径与全期一致（守恒）。"""
    import polars as pl

    from lquant.backtest.attribution import drawdown_attribution, style_regression

    panel = _synthetic_panel()
    dates = sorted(panel["trade_date"].unique().to_list())

    held = panel.filter(pl.col("symbol").is_in(
        sorted(panel["symbol"].unique().to_list())[:5]))
    prices: dict[str, dict] = {}
    for r in held.iter_rows(named=True):
        prices.setdefault(r["symbol"], {})[r["trade_date"]] = r["close"]
    positions = {d: {s: 100.0 for s in prices} for d in dates}
    first = next(iter(prices))
    nav = [(dates[0], 500.0 * prices[first][dates[0]])]
    for i in range(1, len(dates)):
        nav.append((dates[i], sum(100.0 * prices[s][dates[i]] for s in prices)))

    reg = style_regression(panel)
    out = drawdown_attribution(positions, prices, nav, reg=reg, threshold=0.05)
    assert out["periods"], out
    nav_map = dict(nav)
    for p in out["periods"]:
        # 守恒：Σ因子 = common；common+specific = Σ日收益
        # （注意≠累计 ret——算术日收益不 telescoping，差值是波动拖累）
        if "common" in p:
            assert sum(p["factors"].values()) == pytest.approx(p["common"], abs=1e-5)
            d0 = date.fromisoformat(p["start"])
            d1 = date.fromisoformat(p["end"] or str(nav[-1][0]))
            wd = sorted(d for d in nav_map if d0 <= d <= d1)
            ret_daily = sum(nav_map[wd[i]] / nav_map[wd[i - 1]] - 1
                            for i in range(1, len(wd)))
            assert p["common"] + p["specific"] == pytest.approx(ret_daily, abs=1e-6)
        assert p["days"] >= 2


# ---------------- 风险归因（方差分解） ----------------

def test_risk_attribution_variance_additivity():
    """var_total = var_common + var_specific + cross（加法性）。"""
    from lquant.backtest.attribution import risk_attribution, style_regression

    panel = _synthetic_panel(t=70)
    dates = sorted(panel["trade_date"].unique().to_list())
    import polars as pl

    held = panel.filter(pl.col("symbol").is_in(
        sorted(panel["symbol"].unique().to_list())[:5]))
    prices: dict[str, dict] = {}
    for r in held.iter_rows(named=True):
        prices.setdefault(r["symbol"], {})[r["trade_date"]] = r["close"]
    positions = {d: {s: 100.0 for s in prices} for d in dates}
    first = next(iter(prices))
    nav = [(dates[0], 500.0 * prices[first][dates[0]])]
    for i in range(1, len(dates)):
        nav.append((dates[i], sum(100.0 * prices[s][dates[i]] for s in prices)))

    reg = style_regression(panel)
    ra = risk_attribution(positions, prices, nav, reg)
    assert ra.get("n_days", 0) >= 20, ra
    assert ra["var_total"] == pytest.approx(
        ra["var_common"] + ra["var_specific"] + ra["cross_term"], rel=1e-9, abs=1e-12)
    assert ra["vol_total"] > 0
    # Σ因子方差贡献 = systematic_var_barra
    assert sum(f["var_contrib"] for f in ra["factors"]) == pytest.approx(
        ra["systematic_var_barra"], rel=1e-9, abs=1e-8)
    # pct 归一
    pcts = [f["pct"] for f in ra["factors"] if f["pct"] is not None]
    assert pcts and sum(pcts) == pytest.approx(1.0, abs=1e-6)


def test_risk_attribution_insufficient_days():
    from lquant.backtest.attribution import risk_attribution

    d1, d2 = date(2026, 1, 5), date(2026, 1, 6)
    reg = {"factors": ["size"], "f_map": {d2: [0.1]},
           "exp_map": {d1: {"600000.SH": [1.0]}}}
    out = risk_attribution({d1: {"600000.SH": 100.0}},
                           {"600000.SH": {d1: 10.0, d2: 11.0}},
                           [(d1, 1000.0), (d2, 1100.0)], reg)
    assert "note" in out and "20" in out["note"]


# ---------------- HTML 报告 ----------------

def test_render_attribution_html_self_contained():
    """报告含关键章节、无外部资源引用、note 缺失时也不断链。"""
    from lquant.backtest.attribution_report import render_attribution_html

    data = {
        "run_id": "r-1",
        "risk": {"benchmark": "hs300", "alpha_annual": 0.12,
                 "information_ratio": 0.8, "tracking_error": 0.05,
                 "excess_return": 0.1},
        "style_attr": {"dates": ["2026-01-05", "2026-01-06"],
                       "factors": ["size"],
                       "factor_cum": {"size": [0.01, 0.02]},
                       "common_cum": [0.01, 0.02],
                       "specific_cum": [0.0, -0.005],
                       "totals": {"size": 0.02, "common": 0.02,
                                  "specific": -0.005, "ret_arith": 0.015},
                       "note": "口径"},
        "risk_attr": {"factors": [{"factor": "size", "var_contrib": 1.0,
                                   "pct": 1.0, "avg_exposure": 0.5}],
                      "vol_total": 0.15, "vol_common": 0.1,
                      "vol_specific": 0.11, "cross_term": -0.002,
                      "var_total": 0.0225, "var_common": 0.01,
                      "var_specific": 0.0121, "systematic_var_barra": 0.01,
                      "note": "年化"},
        "brinson": {"groups": [{"group": "创业板", "alloc": 0.01, "select": 0.02,
                                "interact": 0.0, "total": 0.03}],
                    "excess_total": 0.03, "note": "算术口径"},
        "brinson_monthly": {"months": [{"month": "2026-01", "groups": [], "excess_total": 0.03}]},
        "cost": {"fee_by_day": [{"date": "2026-01-05", "fee": 30.0, "drag": 3e-5}],
                 "total_fee": 30.0, "total_drag": 3e-5},
        "drawdown": {"periods": [{"start": "2026-01-05", "trough": "2026-01-06",
                                  "end": None, "recovered": False,
                                  "drawdown": 0.08, "days": 2, "ret": -0.08,
                                  "stock_top": [{"symbol": "600000.SH",
                                                 "contribution": 0.01}],
                                  "stock_bottom": [],
                                  "common": -0.05, "specific": -0.03,
                                  "factors": {"size": -0.05}}],
                     "note": "口径"},
        "stock_contribution": {"top": [{"symbol": "600000.SH", "contribution": 0.01}],
                               "bottom": [], "n_stocks": 1},
        "profile": {"concentration": [{"date": "2026-01-05", "hhi": 1.0, "top5": 1.0,
                                       "top10": 1.0, "n_pos": 1}],
                    "industry": {"dates": ["2026-01-05"],
                                 "series": {"沪市主板": [1.0]}},
                    "note": "口径"},
    }
    doc = render_attribution_html(data)
    assert doc.startswith("<!DOCTYPE html>")
    for kw in ("回撤期归因", "风格收益归因", "风险归因", "Brinson", "成本拖累",
               "持仓画像", "个股收益贡献"):
        assert kw in doc, kw
    doc_no_ns = doc.replace('xmlns="http://www.w3.org/2000/svg"', "")
    assert "http" not in doc_no_ns   # 零外部依赖（SVG xmlns 除外）
    assert "<svg" in doc
    # 关键块全缺失 → 仍出骨架，不抛异常
    minimal = render_attribution_html({"run_id": "r-2"})
    assert "回测归因报告" in minimal and "—</p>" not in minimal
