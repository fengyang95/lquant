#!/usr/bin/env python
"""AlphaPurify x lquant 交叉验证 —— AlphaPurify 侧。

必须用隔离 venv 运行（AlphaPurify 拖 pandas/plotly/sklearn/scipy）：

    .claude/worktrees/qlibresearch/.venv-alphapurify/bin/python \
        .claude/worktrees/qlibresearch/scripts/xval/alphapurify/alphapurify_side.py \
        --out .claude/worktrees/qlibresearch/.xval-out

读 lquant 侧产出的**同一个** panel.parquet，然后用 AlphaPurify 自己的
FactorAnalyzer / AlphaPurifier 重算 IC/RankIC 与预处理结果。

注意 AlphaPurify 的 API 事实（1.0.6 实测）：
- `analysis_cfg.rank_ic=True` 时算的是 **Spearman（RankIC）**，False 时才是
  Pearson（IC）——一次运行只出其中一种，所以两种都要跑一遍。
- `FactorAnalyzer.ics_dict[h]` 是 **pandas** DataFrame。
- `AlphaPurifier.winsorize("mad", n)` 是 `med ± n * MAD`，**不带** 1.4826 修正。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import polars as pl
from alphapurify import AlphaPurifier, FactorAnalyzer

MAD_K = 1.4826  # 与 lquant winsorize._MAD_K 一致


def make_analyzer(panel: pl.DataFrame, factor_name: str, rank_ic: bool) -> FactorAnalyzer:
    """构造 FactorAnalyzer。max_workers=1 → joblib 退化为串行，避免 loky 进程问题。"""
    return FactorAnalyzer(
        base_df=panel,
        trade_date_col="trade_date",
        symbol_col="symbol",
        price_col="close",
        factor_name=factor_name,
        research_cfg=dict(
            return_horizons=(1,),
            rebalance_periods=("M",),
            bins=5,
            base_rate=(0.0,),
            fee_rate=(0.0,),
            slippage_rate=(0.0,),
            tax_rate=(0.0,),
            overnight="on",
        ),
        analysis_cfg=dict(rank_ic=rank_ic, max_workers=1),
    )


def run_ic(panel: pl.DataFrame, factor_name: str, rank_ic: bool, out: Path) -> dict:
    """跑一遍 FactorAnalyzer，抽出逐日 IC 序列与汇总。"""
    tag = "rank_ic" if rank_ic else "ic"
    fa = make_analyzer(panel, factor_name, rank_ic)
    fa.run()

    # ics_dict[h] 是 pandas；列名随 rank_ic 开关变化
    ic_pd = fa.ics_dict[1]
    ic_df = pl.from_pandas(ic_pd)
    col = "rank_ic" if "rank_ic" in ic_df.columns else "ic"
    keep = ["trade_date", col]
    if "autocorr" in ic_df.columns:
        keep.append("autocorr")
    ic_df = ic_df.select(keep).rename({col: tag})
    ic_df.write_parquet(out / f"ap_{tag}.parquet")

    stats = fa.ic_stats_panel
    stats_row = stats.iloc[0].to_dict() if len(stats) else {}
    print(f"[alphapurify] {tag}: 天数 {ic_df.height}, stats={stats_row}")
    return {"tag": tag, "n_days": ic_df.height, "stats": stats_row}


def run_prep(panel: pl.DataFrame, factor_name: str, n: float, out: Path, tag: str) -> pl.DataFrame:
    """AlphaPurifier: winsorize(mad, n) -> standardize(zscore)。"""
    p = AlphaPurifier(
        panel,
        factor_name=factor_name,
        trade_date_col="trade_date",
        symbol_col="symbol",
    )
    p.winsorize("mad", n).standardize("zscore")
    res = p.to_result()  # pandas，列 = 原始列
    df = pl.from_pandas(res).select(
        [
            pl.col("trade_date"),
            pl.col("symbol"),
            pl.col(factor_name).cast(pl.Float64).alias(tag),
        ]
    )
    print(f"[alphapurify] prep {tag} (n={n:.6g}) 行数 {df.height}")
    return df


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--factor-name", default="mom20")
    args = ap.parse_args()

    out = Path(args.out)
    panel = pl.read_parquet(out / "panel.parquet")
    print(f"[alphapurify] 读入 panel {panel.height} 行 / {panel['symbol'].n_unique()} 只")

    summary: dict = {"side": "alphapurify", "factor_name": args.factor_name}
    for rank_ic in (True, False):
        summary[("rank_ic" if rank_ic else "ic")] = run_ic(
            panel, args.factor_name, rank_ic, out
        )

    # 两个预处理变体：AP 出厂默认(n=3) / 折算到 lquant 默认(n=5*1.4826)
    ap_default = run_prep(panel, args.factor_name, 3.0, out, "ap_default")
    lq_aligned = run_prep(panel, args.factor_name, 5.0 * MAD_K, out, "lq_default_aligned")
    prep = ap_default.join(lq_aligned, on=["trade_date", "symbol"], how="inner")
    prep.write_parquet(out / "ap_prep.parquet")

    (out / "ap_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    )
    print(f"[alphapurify] 完成 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
