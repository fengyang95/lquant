"""时序算子（TS）：在 symbol 分组内沿时间计算。

**空值口径**：除 ``Ts_Count`` 外全部要求满窗（Polars 默认 ``min_samples=窗口``），
窗口内一旦出现空值结果即为空 —— 与 qlib 的 ``min_periods=1``（部分窗口也出值）
不同。部分窗口出值会把预热期的噪声当信号喂给下游 IC，这边刻意不跟。

**矩估计口径（与 qlib/pandas 不一致，务必知悉）**：``Ts_Skew``/``Ts_Kurt`` 走
Polars 的**总体矩**（有偏），而 qlib 走 pandas 的**无偏修正**版本，同一窗口数值
不同 —— 对拍前必须换算：

    G1 = g1 · sqrt(n(n-1)) / (n-2)                       # 偏度
    G2 = ((n+1)·g2 + 6) · (n-1) / ((n-2)(n-3))           # 超额峰度

（``Ts_Std``/``Ts_Var`` 无此问题：Polars 与 pandas 默认都是 ddof=1 无偏。）
选择跟 Polars 是为了与 ``Ts_Skew`` 既有口径保持家族内一致，而不是跟着 qlib
在同一个注册表里混两套矩定义。

**窗口下界**：偏度需 ≥3 个样本、峰度需 ≥4 个 —— 低于此值时 Polars 返回的是
``NaN``（不是 ``null``），NaN 会沿着算术一路污染到 IC 统计里且不报错，
所以这里直接抛 ``FactorError``（qlib 同样在构造期 raise）。

未移植 qlib 的 ``Mad``（滚动平均绝对差）：qlib 的实现是逐窗口 Python 回调
（其源码里自己写着 ``TODO: implement in Cython``）。lquant 虽有 ``rolling_map``
通道，但 Mad 绕不开逐窗口扫描（窗口内绝对差求和不可分解成累积量），
在全市场面板上代价是 O(样本数 × 窗口)。留给 ``lq-ops`` Rust 侧实现，
届时按 ``rust_bridge`` 的同名覆盖机制注册即可，上层无感。
"""
from __future__ import annotations

import numpy as np
import polars as pl

from lquant.core.errors import FactorError
from lquant.factors.ops.registry import op


def _require_window(n: int | float, least: int, name: str) -> None:
    """矩类算子的窗口下界校验。见模块 docstring「窗口下界」。"""
    if isinstance(n, (int, float)) and not isinstance(n, bool) and n < least:
        raise FactorError(f"{name} 的窗口必须 ≥ {least}（{least} 是矩估计的最低样本数），收到 {n}")


@op("Ts_Mean", "TS", 1, "时序均值")
def ts_mean(x: pl.Expr, n: int) -> pl.Expr:
    return x.rolling_mean(n).over("symbol")


@op("Ts_Std", "TS", 2, "时序标准差")
def ts_std(x: pl.Expr, n: int) -> pl.Expr:
    return x.rolling_std(n).over("symbol")


@op("Ts_Var", "TS", 2, "时序方差")
def ts_var(x: pl.Expr, n: int) -> pl.Expr:
    return x.rolling_var(n).over("symbol")


@op("Ts_Return", "TS", 1, "N 期收益率")
def ts_return(x: pl.Expr, n: int) -> pl.Expr:
    return (x / x.shift(n).over("symbol") - 1).over("symbol")


@op("Ts_Delay", "TS", 1, "N 期前值")
def ts_delay(x: pl.Expr, n: int) -> pl.Expr:
    return x.shift(n).over("symbol")


@op("Ts_Corr", "TS", 2, "时序相关")
def ts_corr(x: pl.Expr, y: pl.Expr, n: int) -> pl.Expr:
    return pl.rolling_corr(x, y, window_size=n).over("symbol")


@op("Ts_Sum", "TS", 1, "时序求和")
def ts_sum(x: pl.Expr, n: int) -> pl.Expr:
    return x.rolling_sum(n).over("symbol")


@op("Ts_Count", "TS", 1, "窗口内有效样本数（允许部分窗口，见下方说明）")
def ts_count(x: pl.Expr, n: int) -> pl.Expr:
    """非空样本计数。**刻意允许部分窗口**（``min_samples=1``）。

    若也要求满窗，这个算子会恒等于 ``n``（有值）或空（窗口内有空值），
    对因子毫无信息量 —— 它的用途恰恰是回答「近 N 日里有几天真的有数据」，
    即停牌/缺失带来的数据完整度。用窗口求和实现而不是 ``rolling_map``，
    全市场面板上也是向量化的。
    """
    return (x.is_not_null().cast(pl.Float64)
            .rolling_sum(n, min_samples=1).over("symbol"))


@op("Ts_Max", "TS", 1, "时序最大")
def ts_max(x: pl.Expr, n: int) -> pl.Expr:
    return x.rolling_max(n).over("symbol")


@op("Ts_Min", "TS", 1, "时序最小")
def ts_min(x: pl.Expr, n: int) -> pl.Expr:
    return x.rolling_min(n).over("symbol")


@op("Ts_ArgMax", "TS", 1, "时序最大值位置（0=窗内最老一根）")
def ts_argmax(x: pl.Expr, n: int) -> pl.Expr:
    return x.rolling_map(lambda s: float(np.argmax(s)), window_size=n).over("symbol")


@op("Ts_ArgMin", "TS", 1, "时序最小值位置（0=窗内最老一根）")
def ts_argmin(x: pl.Expr, n: int) -> pl.Expr:
    return x.rolling_map(lambda s: float(np.argmin(s)), window_size=n).over("symbol")


@op("Ts_Rank", "TS", 1, "时序秩（末值在窗口内的分位，0~1）")
def ts_rank(x: pl.Expr, n: int) -> pl.Expr:
    return x.rolling_map(lambda s: float((s <= s[-1]).mean()), window_size=n).over("symbol")


@op("Ts_Delta", "TS", 1, "N 期差分")
def ts_delta(x: pl.Expr, n: int) -> pl.Expr:
    return (x - x.shift(n).over("symbol")).over("symbol")


@op("Ts_Cov", "TS", 2, "时序协方差")
def ts_cov(x: pl.Expr, y: pl.Expr, n: int) -> pl.Expr:
    return pl.rolling_cov(x, y, window_size=n).over("symbol")


@op("Ts_Skew", "TS", 3, "时序偏度（总体矩；窗口 <3 报错，见模块说明）")
def ts_skew(x: pl.Expr, n: int) -> pl.Expr:
    _require_window(n, 3, "Ts_Skew")
    return x.rolling_skew(n).over("symbol")


@op("Ts_Kurt", "TS", 4, "时序超额峰度（总体矩；窗口 <4 报错，见模块说明）")
def ts_kurt(x: pl.Expr, n: int) -> pl.Expr:
    _require_window(n, 4, "Ts_Kurt")
    return x.rolling_kurtosis(n).over("symbol")


@op("Ts_Med", "TS", 1, "时序中位数（对极端值稳健的位置估计）")
def ts_med(x: pl.Expr, n: int) -> pl.Expr:
    return x.rolling_median(n).over("symbol")


@op("Ts_Prod", "TS", 1, "时序连乘（log 域防溢出）")
def ts_prod(x: pl.Expr, n: int) -> pl.Expr:
    return (x.log1p().rolling_sum(n).over("symbol")).exp() - 1


@op("Ts_EMA", "TS", 1, "指数移动平均（span=n, adjust=False）")
def ts_ema(x: pl.Expr, n: int) -> pl.Expr:
    return x.ewm_mean(span=n, adjust=False).over("symbol")


@op("Ts_Quantile", "TS", 1, "时序分位数（q=0.8 时即 QTLU 口径）")
def ts_quantile(x: pl.Expr, n: int, q: float = 0.8) -> pl.Expr:
    return x.rolling_quantile(quantile=q, window_size=n).over("symbol")


@op("Ts_Slope", "TS", 1, "时序回归斜率")
def ts_slope(x: pl.Expr, n: int) -> pl.Expr:
    return x.rolling_map(lambda s: float(np.polyfit(np.arange(len(s)), s, 1)[0]),
                         window_size=n).over("symbol")


@op("Ts_Rsquare", "TS", 1, "时序回归 R^2")
def ts_rsquare(x: pl.Expr, n: int) -> pl.Expr:
    def _r2(s):
        # rolling_map 回调拿到 polars Series：np.std 会分派到 Series.std(axis=…)
        # 直接 TypeError，必须先转 numpy
        if len(s) < 2 or np.std(s.to_numpy()) < 1e-12:
            return float("nan")
        r = np.corrcoef(np.arange(len(s)), s)[0, 1]
        return float(r * r)
    return x.rolling_map(lambda s: _r2(s), window_size=n).over("symbol")


@op("Ts_Resi", "TS", 1, "时序回归残差标准差")
def ts_resi(x: pl.Expr, n: int) -> pl.Expr:
    def _resi(s):
        if len(s) < 2:
            return float("nan")
        a = s.to_numpy()
        coef = np.polyfit(np.arange(len(a)), a, 1)
        fit = coef[0] * np.arange(len(a)) + coef[1]
        return float(np.sqrt(np.mean((a - fit) ** 2)))
    return x.rolling_map(lambda s: _resi(s), window_size=n).over("symbol")


@op("Ts_WMA", "TS", 1, "加权移动平均（权重 1..n）")
def ts_wma(x: pl.Expr, n: int) -> pl.Expr:
    def _wma(s):
        w = np.arange(1, len(s) + 1, dtype=float)
        return float(np.dot(w, s) / w.sum())
    return x.rolling_map(lambda s: _wma(s), window_size=n).over("symbol")
