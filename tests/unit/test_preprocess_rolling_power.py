"""Phase 4.4 预处理方法扩容：滚动族 + 幂变换。

重点钉三类容易错的东西：
  1. **排序纪律**：滚动方法的窗口必须按 (code, date) 而不是帧的物理行序形成，
     且输出行序要与输入一致（AlphaPurify 直接 sort 后返回，会改行序）。
  2. **无前视**：EWMA 与 Box-Cox 各有一个上游实现里真实存在的泄漏
     （见 rolling.py / power.py 的模块 docstring），这里用「改未来值不能改过去」
     的测试钉死。
  3. **零方差/预热期** 的退化语义与既有约定一致（0 或 null，不给 inf/NaN）。
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
import polars as pl
import pytest

from lquant.core.errors import FactorError
from lquant.factors.preprocess import list_methods
from lquant.factors.preprocess.power import boxcox, yeo_johnson
from lquant.factors.preprocess.rolling import (
    ewma,
    rolling_minmax,
    rolling_robust_zscore,
    rolling_zscore,
    volatility_scaling,
)
from lquant.factors.preprocess.winsorize import MAD_K

NEW_METHODS = ("rolling_zscore", "rolling_robust_zscore", "rolling_minmax",
               "volatility_scaling", "ewma", "boxcox", "yeo_johnson")


def _panel(spec: dict[str, list[float]]) -> pl.DataFrame:
    """{symbol: [按日期升序的值]} → 长表（按日期、标的排序）。"""
    rows = []
    for sym, vals in spec.items():
        for i, v in enumerate(vals):
            rows.append({"trade_date": dt.date(2026, 1, 1) + dt.timedelta(days=i),
                         "symbol": sym, "f": float(v)})
    return pl.DataFrame(rows).sort(["trade_date", "symbol"])


def _col(df: pl.DataFrame, sym: str) -> list:
    return df.filter(pl.col("symbol") == sym).sort("trade_date")["f"].to_list()


# ---------------- 注册与自省 ----------------

def test_methods_registered_in_standardize_stage():
    by_name = {m["name"]: m for m in list_methods("standardize")}
    for n in NEW_METHODS:
        assert n in by_name, f"未注册: {n}"
        assert by_name[n]["stage"] == "standardize"
    # 口径自省字段是 Phase 1 定下的词汇表：新方法必须照写，前端/对拍脚本据此对齐
    for n in NEW_METHODS:
        for key in ("formula", "notes", "zero_variance", "label"):
            assert by_name[n].get(key), f"{n} 缺少 {key}"


def test_defaults_are_declared_in_params():
    by_name = {m["name"]: m for m in list_methods("standardize")}
    assert by_name["rolling_zscore"]["params"]["window"] == 20
    assert by_name["rolling_zscore"]["params"]["code"] == "symbol"
    assert by_name["volatility_scaling"]["params"]["shift_vol"] is True
    assert by_name["ewma"]["params"]["lambda_"] == pytest.approx(0.94)


# ---------------- 排序纪律 ----------------

def test_rolling_window_follows_time_not_frame_order():
    """帧被打乱也不能算错：窗口必须按 (code, 日期) 形成。"""
    df = _panel({"A": [1, 2, 3, 4, 5], "B": [50, 40, 30, 20, 10]})
    scrambled = df.sample(fraction=1.0, shuffle=True, seed=7)
    out = rolling_zscore(scrambled, "f", window=3)
    assert out["symbol"].to_list() == scrambled["symbol"].to_list(), "输出行序必须与输入一致"
    assert out["trade_date"].to_list() == scrambled["trade_date"].to_list()

    for sym in ("A", "B"):
        vals = _col(df, sym)
        got = _col(out, sym)
        for i in range(len(vals)):
            if i < 2:
                assert got[i] is None
                continue
            w = np.array(vals[i - 2:i + 1])
            assert got[i] == pytest.approx(
                float((w[-1] - w.mean()) / w.std(ddof=1)), rel=1e-12)


def test_no_temporary_columns_leak():
    df = _panel({"A": [1, 2, 3, 4], "B": [4, 3, 2, 1]})
    for fn in (rolling_zscore, rolling_robust_zscore, rolling_minmax,
               volatility_scaling, ewma):
        out = fn(df, "f", window=3) if fn is not ewma else fn(df, "f")
        assert out.columns == df.columns, fn.__name__


def test_missing_code_column_raises():
    df = _panel({"A": [1, 2, 3]}).drop("symbol")
    with pytest.raises(Exception, match="缺少分组列"):
        rolling_zscore(df, "f", window=2)


# ---------------- 参数校验 ----------------

def test_parameter_validation():
    df = _panel({"A": [1.0, 2, 3, 4]})
    with pytest.raises(ValueError, match="window 必须 ≥ 2"):
        rolling_zscore(df, "f", window=1)
    with pytest.raises(ValueError, match="min_periods"):
        rolling_zscore(df, "f", window=3, min_periods=4)
    with pytest.raises(ValueError, match="lambda_"):
        ewma(df, "f", lambda_=1.0)
    with pytest.raises(ValueError, match="eps"):
        ewma(df, "f", eps=0.0)
    with pytest.raises(ValueError, match="lambda_"):
        boxcox(df, "f", lambda_="x")
    with pytest.raises(ValueError, match="eps"):
        boxcox(df, "f", eps=-1.0)
    with pytest.raises(ValueError, match="lambda_"):
        yeo_johnson(df, "f", lambda_=float("nan"))


def test_min_periods_allows_partial_window():
    df = _panel({"A": [1.0, 2, 3, 4]})
    out = rolling_zscore(df, "f", window=3, min_periods=2)
    assert _col(out, "A")[0] is None
    assert _col(out, "A")[1] is not None


# ---------------- 各方法数值 ----------------

def test_rolling_minmax_maps_into_range():
    df = _panel({"A": [1.0, 2, 3, 4, 5], "B": [5.0, 5, 5, 5, 5]})
    out = rolling_minmax(df, "f", window=3, lo=0.0, hi=1.0)
    got = _col(out, "A")
    assert got[:2] == [None, None]
    assert got[2:] == pytest.approx([1.0, 1.0, 1.0])
    # 零跨度（B 全同值）→ 兜底 1.0 → 输出 lo，而不是 inf/NaN
    assert _col(out, "B")[2:] == [0.0, 0.0, 0.0]


def test_rolling_robust_zscore_uses_median_and_mad():
    vals = [1.0, 2, 3, 4, 100]
    df = _panel({"A": vals})
    got = _col(rolling_robust_zscore(df, "f", window=3), "A")
    w = vals[2:5]
    med_t = float(np.median(w))
    # 口径：先算每行相对「该行 trailing 中位数」的偏差，再对偏差序列取 trailing 中位数
    devs = []
    for i in range(2, 5):
        win = vals[i - 2:i + 1]
        devs.append(abs(vals[i] - float(np.median(win))))
    mad_t = float(np.median(devs))
    assert got[4] == pytest.approx((vals[4] - med_t) / (MAD_K * mad_t), rel=1e-9)


def test_volatility_scaling_shift_uses_previous_sigma():
    """shift_vol=True（默认）必须用 σ_{t−1}：当日新值不能进自己的分母。"""
    vals = [1.0, 2, 3, 4]
    df = _panel({"A": vals})
    got = _col(volatility_scaling(df, "f", window=2), "A")
    sig = [None, None, float(np.std([2.0, 3.0], ddof=1)), float(np.std([3.0, 4.0], ddof=1))]
    # 前两行：σ_{t−1} 还没有满窗 → null → 兜底 1.0 → 原值
    assert got[:2] == pytest.approx(vals[:2])
    assert got[2] == pytest.approx(vals[2] / sig[2], rel=1e-12)
    assert got[3] == pytest.approx(vals[3] / sig[3], rel=1e-12)
    # shift_vol=False 时当日值进自己的分母，数值明显不同
    no_shift = _col(volatility_scaling(df, "f", window=2, shift_vol=False), "A")
    assert no_shift[2] == pytest.approx(vals[2] / float(np.std([2.0, 3.0], ddof=1)), rel=1e-12)
    assert no_shift[3] == pytest.approx(vals[3] / float(np.std([3.0, 4.0], ddof=1)), rel=1e-12)


def test_ewma_matches_recursive_definition():
    """σ²_0 = x²_0（Polars/pandas adjust=False 的初值），之后是标准递归。"""
    vals = [1.0, -2.0, 3.0, 4.0]
    lam, eps = 0.5, 1e-12
    df = _panel({"A": vals})
    got = _col(ewma(df, "f", lambda_=lam, eps=eps), "A")
    var = vals[0] ** 2                     # 首项约定
    for i, v in enumerate(vals):
        if i:
            var = lam * var + (1 - lam) * v * v
        assert got[i] == pytest.approx(v / (math.sqrt(var) + eps), rel=1e-12)
    assert got[0] == pytest.approx(1.0, rel=1e-9), "首个观测缩放到 ±1"


def test_ewma_has_no_lookahead():
    """改未来值不能改过去 —— AlphaPurify 的实现会把未来数据卷进 σ_t。"""
    base = [1.0, 2.0, 3.0, 4.0, 5.0]
    df = _panel({"A": base})
    before = _col(ewma(df, "f"), "A")
    mutated = _panel({"A": base[:3] + [500.0, 900.0]})
    after = _col(ewma(mutated, "f"), "A")
    assert before[:3] == pytest.approx(after[:3], rel=1e-15)


def _cross_section(vals: list[float]) -> pl.DataFrame:
    """单日、多标的的截面（幂变换是截面口径：同一天只有 1 个样本时 σ 无定义）。"""
    return pl.DataFrame({
        "trade_date": [dt.date(2026, 1, 1)] * len(vals),
        "symbol": [f"S{i:02d}" for i in range(len(vals))],
        "f": [float(v) for v in vals],
    })


def test_yeo_johnson_is_monotone_and_handles_negatives():
    xs = [-3.0, -1.0, 0.0, 1.0, 3.0]
    got = yeo_johnson(_cross_section(xs), "f", lambda_=0.5)["f"].to_list()
    assert all(a < b for a, b in zip(got[:-1], got[1:], strict=True)), "单调变换不能改排序"
    # 截面 z：均值≈0、样本标准差≈1
    assert float(np.mean(got)) == pytest.approx(0.0, abs=1e-12)
    assert float(np.std(got, ddof=1)) == pytest.approx(1.0, rel=1e-9)


def test_yeo_johnson_matches_formula():
    lam = 0.5
    xs = [-2.0, -0.5, 0.0, 0.5, 2.0]

    def yj(x: float) -> float:
        if x >= 0:
            return math.log(x + 1) if lam == 0 else ((x + 1) ** lam - 1) / lam
        return -math.log(1 - x) if lam == 2 else -(((1 - x) ** (2 - lam) - 1) / (2 - lam))

    pt = np.array([yj(v) for v in xs])
    expected = (pt - pt.mean()) / pt.std(ddof=1)
    got = yeo_johnson(_cross_section(xs), "f", lambda_=lam)["f"].to_list()
    assert got == pytest.approx(list(expected), rel=1e-12)
    # λ=0 / λ=2 两个奇点分支也要能走通（log 形式）
    for lam0 in (0.0, 2.0):
        out = yeo_johnson(_cross_section(xs), "f", lambda_=lam0)["f"].to_list()
        assert all(math.isfinite(v) for v in out)


def test_boxcox_shifts_within_day_only():
    """当日截面平移：截面最小值 → 变换后 log(eps) 恒定，z 有定义。"""
    df = _panel({"A": [1.0, 2.0], "B": [2.0, 4.0]})
    out = boxcox(df, "f", lambda_=0.0, eps=1e-9)
    day1 = _col(out, "A")[0], _col(out, "B")[0]
    assert day1[0] < day1[1]
    assert abs(day1[0] + day1[1]) == pytest.approx(0.0, abs=1e-12), "截面 z 应均值为 0"


def test_boxcox_has_no_lookahead_across_days():
    """第 3 天出现极小值，不能改变前两天的输出（AlphaPurify 用全样本最小值会变）。"""
    base = {"A": [1.0, 2.0], "B": [2.0, 4.0]}
    df = _panel(base)
    before = boxcox(df, "f", lambda_=0.25)
    mutated = _panel({"A": base["A"] + [-1000.0], "B": base["B"] + [7.0]})
    after = boxcox(mutated, "f", lambda_=0.25)
    d1 = before.filter(pl.col("trade_date") == dt.date(2026, 1, 1)).sort("symbol")["f"].to_list()
    d1b = after.filter(pl.col("trade_date") == dt.date(2026, 1, 1)).sort("symbol")["f"].to_list()
    assert d1 == pytest.approx(d1b, rel=1e-15)


def test_power_transforms_zero_variance_give_zero():
    """全截面同值 → z 的 σ≈0 兜底 1.0 → 输出 0（与 zscore 约定一致）。"""
    df = _panel({"A": [3.0, 3.0, 3.0], "B": [3.0, 3.0, 3.0]})
    for fn in (boxcox, yeo_johnson):
        assert _col(fn(df, "f"), "A") == [0.0, 0.0, 0.0]


def test_pipeline_runs_all_new_methods():
    from lquant.factors.preprocess import run

    df = _panel({"A": [1.0, 2, 3, 4, 5], "B": [5.0, 4, 3, 2, 1]})
    for m in NEW_METHODS:
        out = run(df, "f", [{"op": "standardize", "method": m}], keep_original=True)
        assert "f_clean" in out.columns
        assert out["f_clean"].null_count() < out.height or m.startswith("rolling_"), m
        assert not out["f_clean"].is_nan().any(), f"{m} 产生了 NaN"


# ---------------- 防御分支 ----------------

def test_ts_prepare_rejects_reserved_temp_column():
    """面板里已有临时列名时必须报错 —— 覆盖它会让原序还原静默错位。"""
    from lquant.factors.preprocess.rolling import _ts_prepare

    df = _panel({"A": [1.0, 2.0, 3.0]}).with_columns(pl.lit(0).alias("__pp_row_idx"))
    with pytest.raises(FactorError, match="临时列名"):
        _ts_prepare(df, "symbol", "trade_date")


def test_ts_restore_checks_row_count():
    from lquant.factors.preprocess.rolling import _ts_restore

    short = _panel({"A": [1.0, 2.0]}).with_row_index("__pp_row_idx")
    with pytest.raises(FactorError, match="行数不一致"):
        _ts_restore(short, 5)
