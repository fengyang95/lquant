"""因子相关性 / 冗余分析 / 合成（方案 F6 + F7，P1）。

- correlation(): 多因子两两横截面相关（Spearman，按日算再取均值），
  并标出 |corr| > 阈值 的冗余对 —— 决定因子能否同时入库。
- synthesize(): 等权 / IC 加权合成。合成前先按日 zscore 标准化，
  避免「日均成交额」这种量纲巨大的因子在等权里淹没其他因子。

因子来源支持两种写法：
- 快捷公式：pct_change_20 / rolling_std_20 / turnover（与研究页一致）
- DSL 表达式：含 `$` 的表达式走 FactorEngine（如 `Rank(Ts_Mean($close,5)/$close-1)`）
"""
from __future__ import annotations

import polars as pl

from lquant.factors.engine import FactorEngine

__all__ = ["compute_factor_col", "build_matrix", "correlation",
           "synthesize", "REDUNDANT_THRESHOLD"]

REDUNDANT_THRESHOLD = 0.8


def _is_quick(formula: str) -> bool:
    return formula.split("_")[0] in ("pct", "rolling", "turnover") and "$" not in formula


def compute_factor_col(df: pl.DataFrame, formula: str, name: str | None = None) -> pl.DataFrame:
    """在日线上现算一个因子列。返回带 name（缺省=formula）列的 DataFrame。

    三种写法（与 ``server/api/factors.py::_compute_factor`` 同口径）：
    快捷公式（pct_change_n / rolling_std_n / turnover）→ Qlib Alpha158 内置因子
    白名单（MA20 / RSV10 / BETA20 …）→ 含 ``$`` 的 DSL 表达式。
    """
    name = name or formula.replace(".", "_")
    if _is_quick(formula):
        if formula.startswith("pct_change_"):
            n = int(formula.rsplit("_", 1)[1])
            return df.with_columns(
                (pl.col("close") / pl.col("close").shift(n).over("symbol") - 1).alias(name))
        if formula.startswith("rolling_std_"):
            n = int(formula.rsplit("_", 1)[1])
            return df.with_columns(
                pl.col("close").pct_change().over("symbol")
                .rolling_std(n).alias(name))
        if formula == "turnover":
            if "amount" not in df.columns:
                raise ValueError("turnover 因子需要 amount 列")
            return df.with_columns((pl.col("amount") / 1e8).alias(name))
        raise ValueError(f"未知快捷公式: {formula}")
    # Qlib Alpha158 内置因子：白名单探测后走 qlib_alpha.compute（它产出 `_factor` 列，
    # 再改名成调用方要的 name）。缺这一步时相关性 / 合成 / prepare_segment 会对内置
    # 因子名抛 FactorError（「字段 'MA20' 不在数据列中」）——因子研究页的
    # 「相关性 · 合成」页签正是把这些名字做成可点标签的，点一下就 500。
    from lquant.factors.qlib_alpha import compute as qlib_compute
    from lquant.factors.qlib_alpha import has_factor

    if has_factor(formula):
        out = qlib_compute(df, formula)
        if name != "_factor":
            if name in out.columns:      # 同名列 = 覆盖重算（与 DSL 路径语义一致）
                out = out.drop(name)
            out = out.rename({"_factor": name})
        return out
    # DSL 表达式
    out = FactorEngine(df.lazy()).compute(formula, name=name)
    return out


def build_matrix(df: pl.DataFrame, formulas: list[str]) -> pl.DataFrame:
    """多因子宽表：[date, symbol, f1...fk]，任一因子缺失的行剔除。"""
    if not formulas:
        raise ValueError("至少需要一个因子")
    wide = df
    for f in formulas:
        wide = compute_factor_col(wide, f)
    cols = [f.replace(".", "_") for f in formulas]
    return wide.drop_nulls(cols)


def correlation(df: pl.DataFrame, formulas: list[str], *,
                threshold: float = REDUNDANT_THRESHOLD) -> dict:
    """横截面 Spearman 相关矩阵（按日算，取均值）+ 冗余对。

    返回 {factors, matrix, redundant_pairs, mean_abs_corr}。
    """
    names = [f.replace(".", "_") for f in formulas]
    if len(names) < 2:
        raise ValueError("相关性分析至少需要两个因子")
    wide = build_matrix(df, formulas)
    if wide.is_empty():
        raise ValueError("因子矩阵为空 —— 检查数据覆盖与公式")

    # 按日秩变换（Spearman = 对秩做 Pearson）
    ranked = wide.with_columns([
        pl.col(c).rank().over("trade_date").alias(c) for c in names])

    # 逐日两两 Pearson，取均值（numpy 小矩阵循环，k 很小可接受）
    import numpy as np

    k = len(names)
    sums = np.zeros((k, k))
    cnt = 0
    for _, g in ranked.group_by("trade_date"):
        arr = g.select(names).to_numpy()
        ok = ~np.isnan(arr).any(axis=1)
        arr = arr[ok]
        if len(arr) < 5:  # 截面太薄的相关不稳定，跳过
            continue
        arr = (arr - arr.mean(axis=0)) / (arr.std(axis=0) + 1e-12)
        sums += arr.T @ arr / len(arr)
        cnt += 1
    if cnt == 0:
        raise ValueError("有效截面不足（每日可用样本 < 5）")
    corr = (sums / cnt).tolist()

    pairs = []
    for i in range(k):
        for j in range(i + 1, k):
            c = corr[i][j]
            if abs(c) >= threshold:
                pairs.append({"a": names[i], "b": names[j],
                              "corr": round(c, 4),
                              "verdict": "高冗余" if abs(c) >= 0.95 else "冗余"})
    return {
        "factors": names,
        "matrix": [[round(v, 4) for v in row] for row in corr],
        "redundant_pairs": pairs,
        "n_dates": cnt,
    }


def synthesize(df: pl.DataFrame, formulas: list[str], *,
               weights: list[float] | None = None,
               method: str = "equal", ic_horizon: int = 5) -> pl.DataFrame:
    """合成因子：按日 zscore 后加权。method: equal / ic_weighted。

    ic_weighted 用各因子与 fwd_ret_{ic_horizon} 的日均 RankIC 为权重
    （负 IC 因子取反向权重前先翻转符号 —— 负 IC 也是信息）。
    返回带 `<syn>` 合成列的 DataFrame。
    """
    names = [f.replace(".", "_") for f in formulas]
    wide = build_matrix(df, formulas)

    if method == "ic_weighted" and weights is None:
        from lquant.factors.evaluate.returns import forward_return

        d = forward_return(wide, "close", periods=[ic_horizon])
        ret_col = f"fwd_ret_{ic_horizon}"
        ics = {}
        for c in names:
            r = (d.drop_nulls([c, ret_col])
                 .with_columns(pl.col(c).rank().over("trade_date"),
                               pl.col(ret_col).rank().over("trade_date"))
                 .select(pl.corr(pl.col(c), pl.col(ret_col)).alias("ic")))
            ics[c] = float(r["ic"][0]) if len(r) else 0.0
        # 方向统一：负 IC 翻转因子符号
        signs = {c: (1.0 if v >= 0 else -1.0) for c, v in ics.items()}
        w = {c: abs(ics[c]) for c in names}
        total = sum(w.values()) or 1.0
        weights = [signs[c] * w[c] / total for c in names]
    elif weights is None:
        weights = [1.0 / len(names)] * len(names)
    if len(weights) != len(names):
        raise ValueError("weights 数量与因子数不一致")

    out = wide
    for c, w in zip(names, weights, strict=False):
        r = pl.col(c).rank().over("trade_date")
        z = (r - r.mean().over("trade_date")) / (r.std().over("trade_date") + 1e-12)
        out = out.with_columns((z * w).alias(f"_z_{c}"))
    zcols = [f"_z_{c}" for c in names]
    out = out.with_columns(
        pl.sum_horizontal([pl.col(c).fill_null(0.0) for c in zcols]).alias("_syn"))
    return out.drop(zcols)
