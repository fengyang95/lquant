"""按标的的时间序列滚动标准化（AlphaPurify ``rolling_*`` / ``volatility_scaling`` / ``EWMA``）。

与 ``standardize.py`` 的截面口径不同：这些方法的参照系是**该标的自己的历史**。

**排序纪律**：面板默认按 ``(trade_date, symbol)`` 排列，此时每个 symbol 内部
恰好也是时间升序，直接 ``over(code)`` 看似正确 —— 但只要调用方换了排序
（按 symbol 排、或从别处拼来的乱序帧），窗口就会跨到错误的行上，且不报错。
所以这里统一先 ``with_row_index`` 记录原序 → 按 ``(code, by)`` 排序算 → 再按
原序还原：结果与输入顺序无关，输出顺序与输入一致。AlphaPurify 的做法是
``.sort()`` 后**直接返回** —— 副作用是悄悄改了行序，调用方若用位置对齐就会错。

**与 AlphaPurify 的两处口径差异**（交叉验证前须知道）：
- 零方差/零波动时本仓用 ``_safe_scale`` 退化为 1.0、输出有限值；AlphaPurify
  注明"返回 null"，但其实现实际会给 ``inf``（``x / 0``）。
- ``EWMA`` 见 ``ewma()`` 的 docstring：AlphaPurify 的实现**用了未来数据**。
"""
from __future__ import annotations

import polars as pl

from lquant.core.errors import FactorError
from lquant.factors.preprocess.registry import method

# `_safe_scale` 是「离散度≈0 时退化兜底」这一约定的既有实现（winsorize 与
# standardize 各有一份同义代码）。这里直接复用而不是再抄第三份 —— 下划线只表示
# 「不在包外承诺」，包内跨模块复用是刻意选择；真要抽公共模块，连同
# `standardize._safe_std` 一起抽，别只搬一半。
from lquant.factors.preprocess.winsorize import MAD_K, _safe_scale

#: 临时列名。带前缀避免与真实因子列撞车；每次调用结束都会 drop。
_IDX = "__pp_row_idx"
_MED = "__pp_med"
_SCALE = "__pp_scale"


def _ts_prepare(df: pl.DataFrame, code: str, by: str) -> tuple[pl.DataFrame, int]:
    """记录原序 → 按 (code, by) 排序。返回 (排序后帧, 行数)。"""
    for c in (code, by):
        if c not in df.columns:
            raise FactorError(f"时间序列标准化缺少分组列 {c!r}；"
                              f"可用列: {sorted(df.columns)}")
    if _IDX in df.columns:
        raise FactorError(f"面板里已有临时列名 {_IDX}，请改名后重试")
    return df.with_row_index(_IDX).sort([code, by]), df.height


def _ts_restore(out: pl.DataFrame, height: int) -> pl.DataFrame:
    """按原序还原并丢掉临时列。"""
    if out.height != height:  # 防御：过程中若发生行数变化，还原会静默错位
        raise FactorError(f"滚动标准化前后行数不一致（{height} → {out.height}）")
    return out.sort(_IDX).drop(_IDX)


def _window(window: int, min_periods: int | None) -> int:
    if window < 2:
        raise ValueError(f"window 必须 ≥ 2（1 个点的滚动标准差恒为 null），收到 {window}")
    mp = window if min_periods is None else int(min_periods)
    if not (1 <= mp <= window):
        raise ValueError(f"min_periods 必须在 [1, window={window}]，收到 {mp}")
    return mp


@method("rolling_zscore", stage="standardize", label="滚动 Z-Score",
        params={"window": 20, "min_periods": None, "code": "symbol"},
        formula="z_t = (x_t − mean(x_{t−w+1..t})) / std(x_{t−w+1..t})，按 code 分组",
        notes=(
            "时间序列口径：参照系是该标的自己的历史，不是当日截面。"
            "窗口**含当日**（不使用未来数据，但当日值参与自己的均值/方差 —— "
            "若要求 σ 只用 t−1 及之前，用 volatility_scaling 的 shift_vol=True）。"
            "与 AlphaPurify rolling_standardize 口径一致（默认 min_periods=window）。"
            "零波动窗口：AP 会给 inf/null，本仓用 _safe_scale 兜底 1.0 → 输出 0。"
        ),
        zero_variance="窗口内 std≈0 时兜底 1.0 → 输出 0（与截面 zscore 同约定）。")
def rolling_zscore(df: pl.DataFrame, col: str, *, by: str = "trade_date",
                   code: str = "symbol", window: int = 20,
                   min_periods: int | None = None) -> pl.DataFrame:
    """个股自身历史的滚动 Z-Score（时间序列口径，非截面）。"""
    mp = _window(window, min_periods)
    out, height = _ts_prepare(df, code, by)
    mean = pl.col(col).rolling_mean(window, min_samples=mp).over(code)
    std = _safe_scale(pl.col(col).rolling_std(window, min_samples=mp).over(code),
                      pl.lit(1.0))
    out = out.with_columns(((pl.col(col) - mean) / std).alias(col))
    return _ts_restore(out, height)


@method("rolling_robust_zscore", stage="standardize", label="滚动稳健 Z-Score",
        params={"window": 20, "min_periods": None, "code": "symbol"},
        formula="z_t = (x_t − med_t) / (1.4826 × mad_t)，mad_t = rolling_median(|x − med|)",
        notes=(
            "用滚动中位数与 MAD 替代均值/标准差，重尾或含跳空时比 rolling_zscore 稳。"
            "**口径注意**：mad 的实现是「先算每行相对该行 trailing 中位数的偏差，"
            "再对这个偏差序列取 trailing 中位数」，与 AlphaPurify "
            "rolling_robust_standardize 的实现逐位一致；"
            "它**不等于**教科书滚动 MAD（后者要对每个窗口内的偏差取中位数，"
            "需要逐窗口排序，无法向量化）。AP 的 docstring 写的是教科书口径 —— "
            "文档与实现不一致，对拍以本公式为准。"
        ),
        zero_variance="mad≈0（窗口内至少一半点重合）时兜底 1.0 → 输出 x − med。")
def rolling_robust_zscore(df: pl.DataFrame, col: str, *, by: str = "trade_date",
                          code: str = "symbol", window: int = 20,
                          min_periods: int | None = None) -> pl.DataFrame:
    """滚动中位数/MAD 版 Z-Score。口径细节见 registry 的 notes。"""
    mp = _window(window, min_periods)
    out, height = _ts_prepare(df, code, by)
    # 分步物化：单个表达式里叠两层 .over() 属于 Polars #25691 的雷区，
    # 而且这里第二层要读第一层的结果，物化后语义也清楚得多。
    out = out.with_columns(
        pl.col(col).rolling_median(window, min_samples=mp).over(code).alias(_MED))
    dev = (pl.col(col) - pl.col(_MED)).abs()
    out = out.with_columns(
        dev.rolling_median(window, min_samples=mp).over(code).alias(_SCALE))
    scale = _safe_scale(pl.col(_SCALE) * MAD_K, pl.lit(1.0))
    out = out.with_columns(((pl.col(col) - pl.col(_MED)) / scale).alias(col))
    return _ts_restore(out.drop(_MED, _SCALE), height)


@method("rolling_minmax", stage="standardize", label="滚动 Min-Max",
        params={"window": 20, "min_periods": None, "lo": 0.0, "hi": 1.0, "code": "symbol"},
        formula="lo + (x_t − min_w) / (max_w − min_w) × (hi − lo)，按 code 分组",
        notes=(
            "把标的自身历史窗口映射到固定区间，适合震荡型/比率型因子。"
            "与 AlphaPurify rolling_minmax_standardize 同口径（含当日、默认满窗），"
            "但零跨度时本仓给 lo（兜底 1.0），AP 声称 null、实现给 inf。"
        ),
        zero_variance="窗口内 max−min≈0 时分母兜底 1.0 → 输出恒为 lo。")
def rolling_minmax(df: pl.DataFrame, col: str, *, by: str = "trade_date",
                   code: str = "symbol", window: int = 20,
                   min_periods: int | None = None, lo: float = 0.0,
                   hi: float = 1.0) -> pl.DataFrame:
    """标的自身滚动窗口的 Min-Max 缩放。"""
    mp = _window(window, min_periods)
    out, height = _ts_prepare(df, code, by)
    out = out.with_columns(
        pl.col(col).rolling_min(window, min_samples=mp).over(code).alias(_MED))
    out = out.with_columns(
        pl.col(col).rolling_max(window, min_samples=mp).over(code).alias(_SCALE))
    span = _safe_scale(pl.col(_SCALE) - pl.col(_MED), pl.lit(1.0))
    out = out.with_columns(
        (lo + (pl.col(col) - pl.col(_MED)) / span * (hi - lo)).alias(col))
    return _ts_restore(out.drop(_MED, _SCALE), height)


@method("volatility_scaling", stage="standardize", label="波动率缩放",
        params={"window": 20, "min_periods": None, "shift_vol": True, "code": "symbol"},
        formula="x'_t = x_t / σ_{t−1}，σ 为按 code 分组的滚动标准差（shift_vol=True）",
        notes=(
            "只做尺度归一（不减均值）：把不同标的/不同时期的波动量纲对齐，"
            "常与信号加权配合。**默认 shift_vol=True 用 σ_{t−1}** —— 用含当日的 σ_t "
            "会把当日值算进自己的分母（当日极值被自动压缩），AP 也默认 shift。"
            "零波动→兜底 1.0（AP 给 inf/null）。"
        ),
        zero_variance="σ≈0 或前一日无有效窗口时兜底 1.0 → 输出原值。")
def volatility_scaling(df: pl.DataFrame, col: str, *, by: str = "trade_date",
                       code: str = "symbol", window: int = 20,
                       min_periods: int | None = None,
                       shift_vol: bool = True) -> pl.DataFrame:
    """按滚动波动率缩放（不减均值）。``shift_vol=True`` 用 σ_{t−1}。"""
    mp = _window(window, min_periods)
    out, height = _ts_prepare(df, code, by)
    out = out.with_columns(
        pl.col(col).rolling_std(window, min_samples=mp).over(code).alias(_MED))
    if shift_vol:
        # 物化后再 shift：shift 也要 over(code)，与 rolling 的 over 叠在一层
        # 表达式里同样踩 #25691 的雷
        out = out.with_columns(pl.col(_MED).shift(1).over(code).alias(_MED))
    scale = _safe_scale(pl.col(_MED), pl.lit(1.0))
    out = out.with_columns((pl.col(col) / scale).alias(col))
    return _ts_restore(out.drop(_MED), height)


@method("ewma", stage="standardize", label="EWMA 波动缩放",
        params={"lambda_": 0.94, "eps": 1e-12, "code": "symbol"},
        formula="σ²_t = λσ²_{t−1} + (1−λ)x²_t（首项 σ²_0 = x²_0）；x'_t = x_t / (σ_t + eps)",
        notes=(
            "RiskMetrics 口径的指数加权波动率缩放（只做尺度归一）。"
            "λ 越大越平滑（日频常用 0.94）。"
            "**首项约定**：Polars/pandas 的 ewm_mean(adjust=False) 取 σ²_0 = x²_0 而不是 "
            "(1−λ)x²_0（等价于假设 σ²_{−1}=x²_0）。跟随它是有意的：另一选择会让"
            "首个观测除以一个很小（≈0.245|x_0|）的波动率，产出一个数量级偏大的假极值。"
            "**与 AlphaPurify EWMA_standardize 的差异是实质性的**："
            "它把 (1−λ)λ^k 的权重挂在**绝对时间下标**上再做反向累加，"
            "等价于 σ²_t = Σ_{u≥t} (1−λ)λ^u x²_u —— 即**用到了 t 之后的数据**"
            "（前视泄漏），且权重随绝对下标而非距离衰减，不是真正的 EWMA。"
            "本实现用递归形式（Polars ewm_mean adjust=False），只用当前与历史。"
        ),
        zero_variance="σ 极小时靠 eps 兜底，输出有限值。")
def ewma(df: pl.DataFrame, col: str, *, by: str = "trade_date",
         code: str = "symbol", lambda_: float = 0.94,
         eps: float = 1e-12) -> pl.DataFrame:
    """EWMA 波动率归一（RiskMetrics 口径，首项取 σ²_0=x²_0）。"""
    if not (0.0 < lambda_ < 1.0):
        raise ValueError(f"lambda_ 必须在 (0, 1)，收到 {lambda_}")
    if eps <= 0:
        raise ValueError(f"eps 必须为正，收到 {eps}")
    out, height = _ts_prepare(df, code, by)
    var = (pl.col(col) ** 2).ewm_mean(alpha=1.0 - lambda_, adjust=False).over(code)
    out = out.with_columns((pl.col(col) / (var.sqrt() + eps)).alias(col))
    return _ts_restore(out, height)
