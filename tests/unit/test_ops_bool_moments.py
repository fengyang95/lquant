"""新增逻辑/比较算子与矩类时序算子的口径回归。

钉三件容易漂移的事：
  1. 逻辑算子必须是**逻辑**语义（真值 x>0 → 0/1 浮点），不是 qlib 的按位语义；
  2. Ts_Kurt/Ts_Skew 走 Polars 的总体矩（有偏），与 pandas/qlib 的无偏修正不等，
     换算公式写在 ts_ops 的模块 docstring 里 —— 对拍前必须换算，别以为是 bug；
  3. 窗口低于矩估计最低样本数时必须报错，而不是产出 NaN 污染 IC。
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import polars as pl
import pytest

from lquant.core.errors import FactorError
from lquant.factors.dsl.parser import parse
from lquant.factors.ops import bool_ops, cs_ops, el_ops, ts_ops  # noqa: F401  注册
from lquant.factors.ops.registry import OPS

NEW_OPS = {"Not", "And", "Or", "Eq", "Ne", "Ge", "Le"}
NEW_TS = {"Ts_Var", "Ts_Kurt", "Ts_Med", "Ts_Count"}


def _df(vals, y=None) -> pl.DataFrame:
    n = len(vals)
    data = {
        "trade_date": [dt.date(2026, 1, 1) + dt.timedelta(days=i) for i in range(n)],
        "symbol": ["S000"] * n,
        "x": vals,
    }
    if y is not None:
        data["y"] = y
    return pl.DataFrame(data, schema_overrides={"x": pl.Float64})


def _apply(df: pl.DataFrame, name: str, *args) -> list:
    """按算子的位置参数直接调用（字符串 = 列名，数字 = 标量实参）。

    标量必须是 Python 数：窗口参数给 ``pl.lit`` 会被当作 Expr，
    ``rolling_mean`` 直接 TypeError（DSL 编译器同样只传裸数）。
    """
    cols = [pl.col(a) if isinstance(a, str) else a for a in args]
    return df.with_columns(OPS.get(name)(*cols).alias("out"))["out"].to_list()


# ---------------- 注册与目录 ----------------

def test_new_ops_registered():
    for n in NEW_OPS | NEW_TS:
        assert n in OPS, f"算子未注册: {n}"


def test_new_ts_categories_and_windows():
    for n in NEW_TS:
        assert OPS.meta(n)["category"] == "TS"
    for n in NEW_OPS:
        assert OPS.meta(n)["category"] == "EL"
    assert OPS.meta("Ts_Count")["min_window"] == 1
    # 矩估计的最低样本数必须写进注册表 —— 画布与预热期都读它
    assert OPS.meta("Ts_Skew")["min_window"] == 3
    assert OPS.meta("Ts_Kurt")["min_window"] == 4


def test_new_ops_are_canvas_usable():
    """画布按「序列在前、标量在后」自省元数；新算子不能是 UNKNOWN_ARITY。"""
    from lquant.factors.ops.catalog import op_catalog

    by_name = {o["name"]: o for o in op_catalog()}
    assert by_name["Eq"]["series_arity"] == 2
    assert by_name["Not"]["series_arity"] == 1
    assert by_name["Ts_Kurt"]["series_arity"] == 1
    assert by_name["Ts_Kurt"]["params"][0]["type"] == "window"
    assert all(by_name[n]["series_arity"] >= 0 for n in NEW_OPS | NEW_TS)


# ---------------- 逻辑 / 比较 ----------------

def test_logic_is_logical_not_bitwise():
    """qlib 用 np.bitwise_not：~2.5 == -3.0。我们必须给逻辑非（0/1）。"""
    df = _df([2.5, 1.0, 0.0, -2.0, None])
    assert _apply(df, "Not", "x") == [0.0, 0.0, 1.0, 1.0, None]


def test_comparisons_output_zero_one_float():
    df = _df([1.0, 0.0, -2.0, None, 3.0])
    assert _apply(df, "Eq", "x", 1.0) == [1.0, 0.0, 0.0, None, 0.0]
    assert _apply(df, "Ne", "x", 1.0) == [0.0, 1.0, 1.0, None, 1.0]
    assert _apply(df, "Ge", "x", 1.0) == [1.0, 0.0, 0.0, None, 1.0]
    assert _apply(df, "Le", "x", 1.0) == [1.0, 1.0, 1.0, None, 0.0]
    out = df.with_columns(OPS.get("Ge")(pl.col("x"), 1.0).alias("out"))
    assert out["out"].dtype == pl.Float64, "必须是数值列，布尔列进不了 rolling_mean"


def test_and_or_use_kleene_null_logic():
    """null 不当 False（Kleene / SQL 三值逻辑）：

    ``True & null = null``、``False & null = False``、
    ``True | null = True``、``False | null = null``。
    把 null 直接当 False 会让「数据缺失」和「条件不成立」在下游混成同一件事。
    """
    df = _df([1.0, 0.0, None, 1.0], y=[None, 0.0, 1.0, 1.0])
    assert _apply(df, "And", "x", "y") == [None, 0.0, None, 1.0]
    assert _apply(df, "Or", "x", "y") == [1.0, 0.0, 1.0, 1.0]


def test_truth_helper_is_the_single_definition():
    """If 与 Not/And/Or 必须共用真值判据（`>0`），否则 0.5 这类输入会分叉。"""
    from lquant.factors.ops.bool_ops import truth

    df = _df([0.5, 0.0, -0.5])
    assert _apply(df, "Not", "x") == [0.0, 1.0, 1.0]
    expected = df.with_columns(truth(pl.col("x")).alias("t"))["t"].to_list()
    assert expected == [True, False, False]
    # 中缀 `>`/`<`、If、Not 在 0.5 上方向必须一致
    eng_if = df.with_columns(
        OPS.get("If")(pl.col("x"), pl.lit(1.0), pl.lit(-1.0)).alias("out"))["out"].to_list()
    assert eng_if == [1.0, -1.0, -1.0]


def test_bool_ops_through_dsl_engine():
    """走一遍 parse→check→compile→exec，确认函数式比较能在 DSL 里用。"""
    from lquant.factors.engine import FactorEngine

    df = _df([1.0, 2.0, 3.0, 4.0])
    eng = FactorEngine(df.lazy())
    out = eng.compute("Eq(Ts_Mean($x, 2), $x)", "f")
    assert out["f"].dtype == pl.Float64
    assert out["f"].to_list()[1:] == [0.0, 0.0, 0.0]


def test_bool_ops_canonical_roundtrip():
    """函数式算子必须能 canonical→parse→canonical 稳定（画布保存表达式依赖它）。"""
    from lquant.factors.dsl.printer import canonical

    expr = "Le(Ts_Med($close, 5), $open)"
    c1 = canonical(parse(expr, "f").root)
    assert canonical(parse(c1, "f").root) == c1
    assert c1.startswith("Le(Ts_Med(")


# ---------------- 矩类 / 滚动统计 ----------------

def _series(n: int = 40, seed: int = 3) -> list[float]:
    rng = np.random.default_rng(seed)
    return list(1.0 + np.cumsum(rng.normal(0, 1, n)))


def test_ts_var_matches_unbiased_sample_variance():
    vals = _series()
    got = _apply(_df(vals), "Ts_Var", "x", 5)
    for i in range(5, len(vals)):
        assert got[i] == pytest.approx(float(np.var(vals[i - 4:i + 1], ddof=1)), rel=1e-12)


def test_ts_med_matches_numpy_median():
    vals = _series()
    got = _apply(_df(vals), "Ts_Med", "x", 4)
    for i in range(4, len(vals)):
        assert got[i] == pytest.approx(float(np.median(vals[i - 3:i + 1])), rel=1e-12)


def _biased_kurt(w) -> float:
    """总体（有偏）超额峰度 m4/m2^2 - 3 —— Polars rolling_kurtosis 的口径。"""
    a = np.asarray(w, dtype=float)
    d = a - a.mean()
    m2 = float(np.mean(d ** 2))
    m4 = float(np.mean(d ** 4))
    return m4 / m2 ** 2 - 3.0


def _biased_skew(w) -> float:
    a = np.asarray(w, dtype=float)
    d = a - a.mean()
    m2 = float(np.mean(d ** 2))
    m3 = float(np.mean(d ** 3))
    return m3 / m2 ** 1.5


def test_ts_kurt_is_biased_while_pandas_is_unbiased():
    """Polars 给总体（有偏）超额峰度，pandas/qlib 给无偏修正版 —— 两者不相等。

    对拍 qlib 的 Kurt 必须按 docstring 的换算公式还原，否则会误判成「实现不一致」。
    """
    vals = _series(30)
    got = _apply(_df(vals), "Ts_Kurt", "x", 8)
    for i in (7, 15, 29):
        w = vals[i - 7:i + 1]
        n = len(w)
        g2 = got[i]
        assert g2 == pytest.approx(_biased_kurt(w), rel=1e-9)
        unbiased = ((n + 1) * g2 + 6) * (n - 1) / ((n - 2) * (n - 3))
        assert unbiased == pytest.approx(float(pd.Series(w).kurt()), rel=1e-9)
        assert g2 != pytest.approx(float(pd.Series(w).kurt()), rel=1e-6)


def test_ts_skew_biased_conversion_to_unbiased():
    """偏度同理：G1 = g1·sqrt(n(n-1))/(n-2)，对拍 qlib 前要换。"""
    vals = _series(30, seed=11)
    got = _apply(_df(vals), "Ts_Skew", "x", 8)
    w = vals[22:30]
    n = len(w)
    g1 = got[29]
    assert g1 == pytest.approx(_biased_skew(w), rel=1e-9)
    unbiased = g1 * np.sqrt(n * (n - 1)) / (n - 2)
    assert unbiased == pytest.approx(float(pd.Series(w).skew()), rel=1e-9)


def test_moment_ops_reject_degenerate_windows():
    """窗口过小给 NaN（不是 null）会静默污染 IC，必须报错。"""
    df = _df(_series(12))
    with pytest.raises(FactorError, match="窗口必须 ≥ 4"):
        _apply(df, "Ts_Kurt", "x", 3)
    with pytest.raises(FactorError, match="窗口必须 ≥ 3"):
        _apply(df, "Ts_Skew", "x", 2)
    # 合法窗口不许误报
    assert _apply(df, "Ts_Kurt", "x", 4)[-1] is not None


def test_moment_error_surfaces_from_engine():
    from lquant.factors.engine import FactorEngine

    eng = FactorEngine(_df(_series(12)).lazy())
    with pytest.raises(FactorError):
        eng.compute("Ts_Kurt($x, 2)", "f")


def test_ts_count_counts_valid_samples_with_partial_window():
    """Ts_Count 是数据完整度指标：允许部分窗口，否则恒等于 n 毫无信息。"""
    df = _df([1.0, None, 3.0, None, None, 6.0])
    got = _apply(df, "Ts_Count", "x", 3)
    assert got == [1.0, 1.0, 2.0, 1.0, 1.0, 1.0]


def test_ts_count_on_dense_panel_equals_window():
    got = _apply(_df(_series(10)), "Ts_Count", "x", 3)
    assert got[:2] == [1.0, 2.0]        # 部分窗口：1、2
    assert got[2:] == [3.0] * 8


def test_ts_ops_full_window_convention_holds():
    """与 Ts_Count 相反：其余 TS 算子遇到窗口内空值必须给 null（满窗口径）。"""
    df = _df([1.0, None, 3.0, 4.0])
    assert _apply(df, "Ts_Mean", "x", 3) == [None, None, None, None]
