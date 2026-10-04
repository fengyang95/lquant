#!/usr/bin/env python
"""AlphaPurify x lquant 交叉验证 —— 对比。

三方对账：
  A. lquant 评价模块        (lquant_ic.parquet)
  B. AlphaPurify FactorAnalyzer (ap_ic.parquet / ap_rank_ic.parquet)
  C. 独立参考实现           (本脚本直接在 panel 上用 pl.corr 重算)

C 的作用：当 A 与 B 不一致时，判断是谁偏离了「同一条数学定义」。

预处理按 `variants.py` 的清单**逐 key 对齐**（两侧同一个列名），并按变体声明的
`expect` 判定：
  match    → max|Δ| 必须 ≤ `tol`，超了就是**回归**（`--check` 下退出码 1）
  diverge  → 已解释的上游口径差，只记录实测幅度与相关性，不计失败

用法：
    .claude/worktrees/qlibresearch/.venv-alphapurify/bin/python \
        .claude/worktrees/qlibresearch/scripts/xval/alphapurify/compare.py \
        --out .claude/worktrees/qlibresearch/.xval-out --check
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))  # variants.py 同目录

from variants import PREP_VARIANTS  # noqa: E402

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
    """按 on 对齐后比较两列，返回差异统计。

    **两侧列名相同时必须先 alias 成不同名字**：直接
    ``a.select([...col_a]).join(b.select([...col_b]))`` 在 ``col_a == col_b``
    时，join 后右侧列被加上 ``_right`` 后缀，而 ``j[col_a]`` 与 ``j[col_b]``
    都解析到**左列** —— 于是 ``d = left - left ≡ 0``，任何两侧名称相同的比较
    都会「永远通过」。这个自比较 bug 真实存在过，把所有 match 变体都变成
    假的 0.000e+00。
    """
    j = (
        a.select(on + [pl.col(col_a).cast(pl.Float64).alias("__left")])
        .join(b.select(on + [pl.col(col_b).cast(pl.Float64).alias("__right")]),
              on=on, how="inner")
        .drop_nulls()
    )
    if j.height == 0:
        return {"n": 0}
    d = (j["__left"] - j["__right"]).abs()
    max_d = float(d.max())
    # 两列完全相同 → 相关系数无定义（分母 0），语义上就是 1.0
    corr = 1.0 if max_d == 0.0 else float("nan")
    if max_d != 0.0:
        # 常数序列（分母为 0）会让相关系数变 NaN；这里不致命，保持 NaN 即可
        with contextlib.suppress(Exception):
            corr = float(j["__left"].corr(j["__right"]))
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
    ap.add_argument("--check", action="store_true",
                    help="哨兵模式：任何 expect=match 的变体超差即退出码 1")
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

    # ---- 预处理对账（逐变体对齐，判定按 variants.py 声明的 expect） ----
    lq_prep = _norm_date(pl.read_parquet(out / "lquant_prep.parquet"))
    ap_prep = _norm_date(pl.read_parquet(out / "ap_prep.parquet"))
    keys = ["trade_date", "symbol"]
    ap_summary = json.loads((out / "ap_summary.json").read_text())
    diag = {d["variant"]: d for d in ap_summary.get("diagnostics", [])}
    report["prep"]["variants"] = {}
    failures: list[dict] = []
    divergences: list[dict] = []
    for v in PREP_VARIANTS:
        k = v["key"]
        if v["expect"] == "upstream_broken":
            d = diag.get(k)
            if d is None:
                failures.append({"variant": k, "reason": "缺少上游诊断记录（AP 侧漏跑）"})
                continue
            d.update({"label": v["label"], "expect": v["expect"], "tol": None,
                      "note": v["note"],
                      "passed": bool(d.get("bug_reproduced"))})
            if not d["passed"]:
                divergences.append({"variant": k,
                                    "reason": "上游 bug 消失了 —— 可能已修复，请复核 variants.py"})
            report["prep"]["variants"][k] = d
            continue
        if k not in lq_prep.columns or k not in ap_prep.columns:
            failures.append({"variant": k, "reason": "产物缺列（某一侧漏跑）"})
            continue
        d = _diff(lq_prep, ap_prep, k, k, keys)
        d.update({"label": v["label"], "expect": v["expect"],
                  "tol": v["tol"], "note": v["note"]})
        if v["expect"] == "match":
            d["passed"] = bool(d.get("n") and d["max_abs_diff"] <= v["tol"])
            if not d["passed"]:
                failures.append({"variant": k,
                                 "reason": f"max|Δ|={d.get('max_abs_diff')} > tol={v['tol']}",
                                 "note": v["note"]})
        else:
            # 预期分歧也要**确认它真的分歧**：若哪天变得一致，说明上游修了 bug，
            # 该把这条从 diverge 改成 match，而不是继续挂着一个过时的说明。
            d["passed"] = True
            d["still_diverges"] = bool(d.get("n") and d["max_abs_diff"] > 1e-6)
            if not d["still_diverges"]:
                divergences.append({"variant": k,
                                    "reason": "预期分歧消失了 —— 上游可能已修复，请复核 variants.py"})
        report["prep"]["variants"][k] = d

    report["prep"]["effective_mad_multiplier"] = {
        "lquant_default_n": 5.0,
        "lquant_effective_sigma_units": 5.0 * MAD_K,
        "alphapurify_default_n": 3.0,
        "alphapurify_effective_sigma_units": 3.0,
        "ratio_lquant_over_alphapurify": (5.0 * MAD_K) / 3.0,
    }
    report["verdict"] = {
        "check": args.check,
        "n_variants": len(PREP_VARIANTS),
        "failed": failures,
        "stale_divergences": divergences,
        "ok": not failures,
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

    print("\n=== 预处理对账（逐变体） ===")
    print(f"  {'variant':28s} {'expect':16s} {'n':>7s} {'max|d|':>11s} {'mean|d|':>11s}  verdict")
    for k, v in report["prep"]["variants"].items():
        verdict = "OK" if v.get("passed") else "**FAIL**"
        if v["expect"] == "diverge":
            verdict = "diverge(已解释)" if v.get("still_diverges") else "!! 分歧消失，请复核"
        elif v["expect"] == "upstream_broken":
            verdict = ("上游仍坏(已确认)" if v.get("bug_reproduced")
                       else "!! 上游 bug 消失，请复核")
        print(f"  {k:28s} {v['expect']:16s} {v.get('n', v.get('rows', 0)):7d} "
              f"{v.get('max_abs_diff', float('nan')):11.3e} "
              f"{v.get('mean_abs_diff', float('nan')):11.3e}  {verdict}")
        if v["expect"] == "upstream_broken":
            print(f"      └ {v['clobbered_col']} 被写成 {v['dtype']}；{v['note']}")

    for f in failures:
        print(f"\n  ✗ {f['variant']}: {f['reason']}\n    {f.get('note', '')}")
    for d in divergences:
        print(f"\n  ! {d['variant']}: {d['reason']}")

    ok = report["verdict"]["ok"]
    print(f"\n判定: {'通过' if ok else '**存在回归**'}（match 变体 {len(PREP_VARIANTS) - sum(1 for v in PREP_VARIANTS if v['expect'] == 'diverge')} 项）")
    print(f"报告 → {out / 'xval_report.json'}")

    if args.check and not ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
