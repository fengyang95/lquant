#!/usr/bin/env python
"""AlphaPurify x lquant 交叉验证 —— 对比。

三方对账：
  A. lquant 评价模块        (lquant_ic.parquet)
  B. AlphaPurify FactorAnalyzer (ap_ic.parquet / ap_rank_ic.parquet)
  C. 独立参考实现           (本脚本直接在 panel 上用 pl.corr 重算)

C 的作用：当 A 与 B 不一致时，判断是谁偏离了「同一条数学定义」。

预处理同理，两组对齐比较：
  lquant(lq_default)         vs AlphaPurify(lq_default_aligned)   -> 应一致
  lquant(ap_default_aligned) vs AlphaPurify(ap_default)          -> 应一致
  lquant(lq_default)         vs AlphaPurify(ap_default)          -> 约定差异（预期不一致）

用法：
    .claude/worktrees/qlibresearch/.venv-alphapurify/bin/python \
        .claude/worktrees/qlibresearch/scripts/xval/alphapurify/compare.py \
        --out .claude/worktrees/qlibresearch/.xval-out
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import polars as pl

MAD_K = 1.4826


def _norm_date(df: pl.DataFrame) -> pl.DataFrame:
    """统一 trade_date 到 Date。

    AlphaPurify 内部把日期 cast 成 datetime[μs]，lquant 侧是 Date ——
    直接 join 会 SchemaError，比较前先归一。
    """
    if "trade_date" in df.columns:
        return df.with_columns(pl.col("trade_date").cast(pl.Date))
    return df


def _diff(a: pl.DataFrame, b: pl.DataFrame, col_a: str, col_b: str,
          on: list[str]) -> dict:
    """按 on 对齐后比较两列，返回差异统计。"""
    j = (
        a.select(on + [col_a])
        .join(b.select(on + [col_b]), on=on, how="inner")
        .drop_nulls()
    )
    if j.height == 0:
        return {"n": 0}
    d = (j[col_a].cast(pl.Float64) - j[col_b].cast(pl.Float64)).abs()
    max_d = float(d.max())
    # 两列完全相同 → 相关系数无定义（分母 0），语义上就是 1.0
    corr = 1.0 if max_d == 0.0 else float("nan")
    if max_d != 0.0:
        try:
            corr = float(j[col_a].cast(pl.Float64).corr(j[col_b].cast(pl.Float64)))
        except Exception:
            pass
    return {
        "n": j.height,
        "max_abs_diff": max_d,
        "mean_abs_diff": float(d.mean()),
        "p99_abs_diff": float(d.quantile(0.99, interpolation="nearest") or 0.0),
        "corr": corr,
        "identical": bool(max_d < 1e-12),
    }


def reference_ic(panel: pl.DataFrame, factor: str, min_obs: int) -> pl.DataFrame:
    """独立参考：在 panel 上直接重算逐日 Pearson/Spearman IC。"""
    ref = panel.sort(["symbol", "trade_date"]).with_columns(
        ((pl.col("close").shift(-1).over("symbol") / pl.col("close")) - 1).alias("fwd_ret_1")
    )
    return (
        ref.group_by("trade_date")
        .agg(
            pl.len().alias("n"),
            pl.corr(factor, "fwd_ret_1", method="pearson").alias("ic"),
            pl.corr(factor, "fwd_ret_1", method="spearman").alias("rank_ic"),
        )
        .filter(pl.col("n") >= min_obs)
        .sort("trade_date")
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--factor-name", default="mom20")
    ap.add_argument("--min-obs", type=int, default=5)
    args = ap.parse_args()

    out = Path(args.out)
    f = args.factor_name
    panel = pl.read_parquet(out / "panel.parquet")
    lq_ic = _norm_date(pl.read_parquet(out / "lquant_ic.parquet"))
    ap_ic = _norm_date(pl.read_parquet(out / "ap_ic.parquet"))
    ap_rank = _norm_date(pl.read_parquet(out / "ap_rank_ic.parquet"))
    ref = _norm_date(reference_ic(panel, f, args.min_obs))
    ref.write_parquet(out / "reference_ic.parquet")

    ap_both = ap_ic.join(ap_rank, on="trade_date", how="inner")

    report: dict = {
        "factor_name": f,
        "panel": {
            "rows": panel.height,
            "symbols": panel["symbol"].n_unique(),
            "days": panel["trade_date"].n_unique(),
            "date_min": str(panel["trade_date"].min()),
            "date_max": str(panel["trade_date"].max()),
        },
        "min_obs": args.min_obs,
        "ic": {},
        "prep": {},
    }

    # ---- IC 三方对账 ----
    report["ic"]["days"] = {
        "lquant": lq_ic.height,
        "alphapurify": ap_both.height,
        "reference": ref.height,
    }
    report["ic"]["lquant_vs_reference_ic"] = _diff(lq_ic, ref, "ic", "ic", ["trade_date"])
    report["ic"]["lquant_vs_reference_rank_ic"] = _diff(lq_ic, ref, "rank_ic", "rank_ic", ["trade_date"])
    report["ic"]["alphapurify_vs_reference_ic"] = _diff(ap_both, ref, "ic", "ic", ["trade_date"])
    report["ic"]["alphapurify_vs_reference_rank_ic"] = _diff(ap_both, ref, "rank_ic", "rank_ic", ["trade_date"])
    report["ic"]["lquant_vs_alphapurify_ic"] = _diff(lq_ic, ap_both, "ic", "ic", ["trade_date"])
    report["ic"]["lquant_vs_alphapurify_rank_ic"] = _diff(lq_ic, ap_both, "rank_ic", "rank_ic", ["trade_date"])

    # ---- 预处理对账 ----
    lq_prep = _norm_date(pl.read_parquet(out / "lquant_prep.parquet"))
    ap_prep = _norm_date(pl.read_parquet(out / "ap_prep.parquet"))
    keys = ["trade_date", "symbol"]
    report["prep"]["lquant_default_vs_ap_lq_aligned"] = _diff(
        lq_prep, ap_prep, "lq_default", "lq_default_aligned", keys
    )
    report["prep"]["lquant_ap_aligned_vs_ap_default"] = _diff(
        lq_prep, ap_prep, "ap_default_aligned", "ap_default", keys
    )
    report["prep"]["lquant_default_vs_ap_default_CONVENTION_GAP"] = _diff(
        lq_prep, ap_prep, "lq_default", "ap_default", keys
    )
    report["prep"]["effective_mad_multiplier"] = {
        "lquant_default_n": 5.0,
        "lquant_effective_sigma_units": 5.0 * MAD_K,
        "alphapurify_default_n": 3.0,
        "alphapurify_effective_sigma_units": 3.0,
        "ratio_lquant_over_alphapurify": (5.0 * MAD_K) / 3.0,
    }

    (out / "xval_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str)
    )

    # ---- 人类可读输出 ----
    print("\n=== IC 三方对账（逐日序列） ===")
    for k, v in report["ic"].items():
        if isinstance(v, dict) and v.get("n"):
            print(f"  {k:44s} n={v['n']:5d} max|d|={v['max_abs_diff']:.3e} "
                  f"mean|d|={v['mean_abs_diff']:.3e} corr={v['corr']:.6f}")
        elif isinstance(v, dict):
            print(f"  {k:44s} {v}")

    print("\n=== 预处理对账 ===")
    for k, v in report["prep"].items():
        if isinstance(v, dict) and v.get("n"):
            print(f"  {k:52s} n={v['n']:6d} max|d|={v['max_abs_diff']:.3e} "
                  f"mean|d|={v['mean_abs_diff']:.3e}")
        elif isinstance(v, dict):
            print(f"  {k:52s} {v}")

    print(f"\n报告 → {out / 'xval_report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
