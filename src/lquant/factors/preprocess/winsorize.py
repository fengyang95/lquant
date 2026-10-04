"""去极值：MAD / 分位数 / 3σ / 硬截断。

A 股原始因子的极值比美股严重得多 —— 一字板、ST、次新都能把均值拉飞。
直接算 IC 的话，IC 基本被这几只票绑架，是典型的假信号。

统一签名：fn(df, col, *, by="trade_date", **kw) -> pl.DataFrame
所有方法按 `by` 分组做**截面**处理，绝不跨日期污染（否则就是未来函数）。
"""
from __future__ import annotations

import polars as pl

from lquant.factors.preprocess.registry import method

_MAD_K = 1.4826  # MAD → 标准差的一致性修正（正态分布下）
MAD_K = _MAD_K   # 公开名：供 API/前端/交叉验证脚本引用，避免各处重复硬编码


def to_alphapurify_n(n: float) -> float:
    """把 lquant 的 ``n``（等效 σ 倍数）换算成 AlphaPurify 的 ``n``（MAD 倍数）。

    两者关系：``med ± n_lq × 1.4826 × MAD == med ± n_ap × MAD``。
    交叉验证对齐口径时用本函数，别在脚本里硬编码 1.4826。
    """
    return n * MAD_K


def from_alphapurify_n(n: float) -> float:
    """把 AlphaPurify 的 ``n``（MAD 倍数）换算成 lquant 的 ``n``（等效 σ 倍数）。"""
    return n / MAD_K


def _safe_scale(scale: pl.Expr, fallback: pl.Expr) -> pl.Expr:
    """离散度为 0 时（如全市场同一个值）退化处理，避免除零产生 inf。"""
    return pl.when(scale > 1e-12).then(scale).otherwise(fallback)


@method("mad", stage="winsorize", label="MAD 去极值", params={"n": 5.0},
        formula="median ± n × 1.4826 × MAD",
        notes=(
            "n 是**等效标准差倍数**，不是 MAD 倍数：内部已乘 1.4826（正态一致性修正），"
            "所以 n=5 对应 ±5σ 量级。"
            "与 AlphaPurify `mad_winsorize(n=3)` 的 `med ± n×MAD` **同名不同义**；"
            "换算：n_lquant = n_alphapurify / 1.4826（AP 默认 3 → 本仓 2.0235），"
            "反之本仓默认 5 → AP 7.413。实测默认对默认 max|Δ|≈3.04（真实口径差）。"
        ),
        zero_variance=(
            "截面 MAD=0（过半同值）时回退到该截面 σ；σ 也为 0（全截面同值）时 scale=0，"
            "整列塌缩为中位数 —— 不报错、不产生 null。"
        ))
def mad(df: pl.DataFrame, col: str, *, by: str = "trade_date", n: float = 5.0) -> pl.DataFrame:
    """中位数 ± n × 1.4826 × MAD。

    比 3σ 稳健：σ 本身会被极值撑大，MAD 不会。默认 n=5（业界常用 3~5）。

    量纲提醒：``n`` 是**等效标准差倍数**。截断点是 ``med ± n × 1.4826 × MAD``，
    其中 ``1.4826 = 1/Φ⁻¹(0.75)`` 是 MAD→σ 的正态一致性修正。
    因此 ``n=5`` 意味着「±5σ（按 MAD 估计的 σ）」。

    与 AlphaPurify 的换算（交叉验证口径对齐用）::

        n_lquant = n_alphapurify / 1.4826
        n_alphapurify = n_lquant * 1.4826

    该方法口径已写入注册表元数据（``describe()`` 的 ``formula``/``notes``/
    ``zero_variance`` 字段），前端与 API 可直接展示，避免「同名不同义」误用。
    """
    med = pl.col(col).median().over(by)
    dev = (pl.col(col) - med).abs().median().over(by) * _MAD_K
    std = pl.col(col).std().over(by)
    scale = _safe_scale(dev, std)
    return df.with_columns(pl.col(col).clip(med - n * scale, med + n * scale))


@method("quantile", stage="winsorize", label="分位数截断", params={"q": 0.01},
        formula="clip(x, Q_q, Q_{1-q})",
        notes="按截面分位数截断，对分布形状无假设；q=0.01 即 1%/99%。",
        zero_variance="截面取值过少时分位数可能重合，截断区间退化为一点，整列塌缩为该点。")
def quantile(df: pl.DataFrame, col: str, *, by: str = "trade_date", q: float = 0.01) -> pl.DataFrame:
    """按分位数双侧截断，q=0.01 即 1%/99%。"""
    lo = pl.col(col).quantile(q).over(by)
    hi = pl.col(col).quantile(1 - q).over(by)
    return df.with_columns(pl.col(col).clip(lo, hi))


@method("three_sigma", stage="winsorize", label="3σ 去极值", params={"n": 3.0},
        formula="mean ± n × σ",
        notes="经典 3σ。σ 本身会被极值撑大（masking），重尾分布下不如 MAD 稳健。",
        zero_variance="σ=0 时回退为 1.0，等价于不截断（阈值退化为 ±n，通常覆盖全部取值）。")
def three_sigma(df: pl.DataFrame, col: str, *, by: str = "trade_date", n: float = 3.0) -> pl.DataFrame:
    """均值 ± n × σ。对正态分布有效，重尾分布下不如 MAD。"""
    mean = pl.col(col).mean().over(by)
    std = _safe_scale(pl.col(col).std().over(by), pl.lit(1.0))
    return df.with_columns(pl.col(col).clip(mean - n * std, mean + n * std))


@method("clip", stage="winsorize", label="硬截断", params={"lo": -10.0, "hi": 10.0},
        formula="clip(x, lo, hi)",
        notes="固定上下界，不含任何截面统计量。适合已标准化、只需兜底的场景。",
        zero_variance="与截面分布无关，恒等生效。")
def clip(df: pl.DataFrame, col: str, *, by: str = "trade_date",
         lo: float = -10.0, hi: float = 10.0) -> pl.DataFrame:
    """固定上下界。适合已经标准化过、只需兜底的场景。"""
    return df.with_columns(pl.col(col).clip(lo, hi))


@method("none", stage="winsorize", label="不去极值",
        formula="x",
        notes="透传。用于对比实验：确认去极值到底贡献了多少。")
def none(df: pl.DataFrame, col: str, *, by: str = "trade_date") -> pl.DataFrame:
    """透传。用于对比实验：确认去极值到底贡献了多少。"""
    return df
