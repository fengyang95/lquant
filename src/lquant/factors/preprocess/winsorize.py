"""去极值：MAD / 分位数 / 3σ / 硬截断。

A 股原始因子的极值比美股严重得多 —— 一字板、ST、次新都能把均值拉飞。
直接算 IC 的话，IC 基本被这几只票绑架，是典型的假信号。

统一签名：fn(df, col, *, by="trade_date", **kw) -> pl.DataFrame
所有方法按 `by` 分组做**截面**处理，绝不跨日期污染（否则就是未来函数）。
"""
from __future__ import annotations

import polars as pl

from lquant.factors.preprocess.registry import method
from lquant.factors.preprocess.standardize import _inv_norm

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


@method("huber", stage="winsorize", label="Huber 截断", params={"c": 2.0, "scale": "std"},
        formula="mean ± c × scale，其中 scale=std（默认）或 1.4826×MAD",
        notes=(
            "在 z 空间按 sign(z)·min(|z|, c) 截断再映射回原量纲。"
            "**默认 scale='std' 与 AlphaPurify huber_winsorize(c=2.0) 完全对齐**"
            "（它用截面 mean/std，见 APr_utils.huber_winsorize）。"
            "但要注意：用 std 时这个方法**并不稳健** —— 极值会把 std 撑大，"
            "截断点随之被拉远（masking 效应），与它的名字/文档宣称的『robust』不符。"
            "要真正稳健请显式传 scale='mad'（用 1.4826×MAD 估尺度）。"
            "c 的常用区间 1.5~3；越小压缩越强。"
        ),
        zero_variance="scale≈0（全截面同值）时用 1.0 兜底 → 输出恒等于截面均值（z=0 被截断到 0 后映射回均值）。")
def huber(df: pl.DataFrame, col: str, *, by: str = "trade_date", c: float = 2.0,
          scale: str = "std") -> pl.DataFrame:
    """Huber 型截断：z 空间裁到 ±c 再映射回原量纲。

    ``scale="std"``（默认）与 AlphaPurify 口径一致；
    ``scale="mad"`` 用 1.4826×MAD 估尺度，才是真正抗极值的版本
    （std 会被极值撑大，导致该截的没截住）。
    """
    if c <= 0:
        raise ValueError(f"c 必须为正，收到 {c}")
    if scale not in ("std", "mad"):
        raise ValueError(f"未知 scale {scale!r}（可选 std/mad）")
    mean = pl.col(col).mean().over(by)
    if scale == "mad":
        med = pl.col(col).median().over(by)
        dev = (pl.col(col) - med).abs().median().over(by) * MAD_K
        raw = _safe_scale(dev, pl.col(col).std().over(by))
    else:
        raw = pl.col(col).std().over(by)
    sd = _safe_scale(raw, pl.lit(1.0))
    z = (pl.col(col) - mean) / sd
    clipped = pl.when(z.abs() > c).then(z.sign() * c).otherwise(z)
    return df.with_columns((clipped * sd + mean).alias(col))


@method("rankgauss", stage="winsorize", label="RankGauss（分位正态化）",
        params={"clip": 1e-6},
        formula="q = clip((rank_avg − 0.5) / n, clip, 1−clip); x' = Φ⁻¹(q)",
        notes=(
            "即分位正态化（quantile normalization）。分位点用 **Hazen 绘图位置**"
            "``(rank−0.5)/n``，与 AlphaPurify rankgauss_winsorize 完全一致。"
            "逆正态用本仓既有的 Acklam 近似（``standardize._inv_norm``，精度 ~1e-9），"
            "**不引 scipy**。"
            "与 ``standardize.rank(to='normal')`` 的区别：后者用 ``rank/n`` 并 clip 到"
            "``[0.5/n, 1−0.5/n]``（Blom 风格），同一个 rank 会得到不同的分位点"
            "（n=10、rank=1 时 Hazen 0.05 vs Blom 0.10）。两者都是合法约定，"
            "但**不可混用**；要复现 AlphaPurify 请用本方法。"
        ),
        zero_variance="全截面同值时 rank('average') 给所有样本**同一个**平均秩 → "
                      "分位点相同 → 输出恒为该分位对应的常数（n=6 时 (3.5−0.5)/6=0.5 → 0.0）。"
                      "结果是常量、**不含信息**，是真·无信号，而非数值退化。")
def rankgauss(df: pl.DataFrame, col: str, *, by: str = "trade_date",
              clip: float = 1e-6) -> pl.DataFrame:
    """RankGauss：截面排名 → Hazen 分位 → 逆正态。

    同时完成标准化与非线性压缩，对重尾分布比 z-score 稳健得多。
    """
    if not (0.0 < clip < 0.5):
        raise ValueError(f"clip 必须在 (0, 0.5)，收到 {clip}")
    n = pl.col(col).count().over(by)
    q = ((pl.col(col).rank("average").over(by) - 0.5) / n).clip(clip, 1 - clip)
    return df.with_columns(
        q.map_elements(_inv_norm, return_dtype=pl.Float64).alias(col))


@method("none", stage="winsorize", label="不去极值",
        formula="x",
        notes="透传。用于对比实验：确认去极值到底贡献了多少。")
def none(df: pl.DataFrame, col: str, *, by: str = "trade_date") -> pl.DataFrame:
    """透传。用于对比实验：确认去极值到底贡献了多少。"""
    return df
