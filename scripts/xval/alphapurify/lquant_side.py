#!/usr/bin/env python
"""AlphaPurify x lquant 交叉验证 —— lquant 侧。

在主 venv（lquant 依赖）下、从**主仓根目录**运行：

    LQ_DATA_DIR=$PWD/data .venv/bin/python \
        .claude/worktrees/qlibresearch/scripts/xval/alphapurify/lquant_side.py \
        --out .claude/worktrees/qlibresearch/.xval-out \
        --start 2023-01-01 --end 2024-12-31 --max-symbols 800

设计要点：
- 数据走 lquant 自己的湖读取器 `read_daily`，不另写一套读取逻辑。
  **数据源**由 `LQ_DATA_DIR` 指定（通常是主仓），**被测代码**由本脚本所在
  worktree 的 `src/` 指定（可用 `LQ_SRC` 覆盖）—— 两者刻意分开。
- 因子用 lquant 的 DSL 引擎算（默认 `Ts_Return($close, 20)`）。
- 前瞻收益 / IC / RankIC 全部调 lquant 的评价模块。
- 预处理变体清单来自 `variants.py`（三方共用的唯一真源，见该文件 docstring）。

产出（供 AlphaPurify 侧与 compare.py 使用）：
  panel.parquet       trade_date/symbol/close(复权)/<factor> —— 两侧的共同输入
  lquant_ic.parquet   逐日 IC / RankIC（lquant 计算）
  lquant_summary.json lquant 的 IC/RankIC 汇总 + 元信息
  lquant_prep.parquet 全部预处理变体的结果（一列一个变体 key）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import polars as pl

# 与 src/lquant/factors/preprocess/winsorize.py::_MAD_K 保持一致
MAD_K = 1.4826
STOCK_RE = re.compile(r"^(60|00|30|68)\d{4}\.(SH|SZ)$")


def _add_src() -> None:
    """把**被测代码**的 src/ 挂上 sys.path。

    顺序：``$LQ_SRC`` → 本脚本所在 worktree 的 src → ``CWD/src``。
    **worktree 必须优先于 CWD**：本脚本从主仓根目录跑（为了读主仓的数据湖，
    数据源由 ``LQ_DATA_DIR`` 单独指定），但被测代码是 worktree 这个分支 ——
    若 CWD/src 抢先，主仓 main 的**旧代码**会冒充被测对象，新增的预处理方法
    会直接报「未注册」（这个坑真实踩过一次）。
    """
    here = Path(__file__).resolve()
    cands: list[Path] = []
    if os.environ.get("LQ_SRC"):
        cands.append(Path(os.environ["LQ_SRC"]))
    cands += [here.parents[3] / "src", Path("src")]
    for cand in cands:
        if (cand / "lquant").is_dir():
            sys.path.insert(0, str(cand))
            print(f"[lquant] 被测代码源: {cand}")
            return
    raise SystemExit("找不到 src/lquant，请设置 LQ_SRC 指向被测 checkout 的 src/")


_add_src()

sys.path.insert(0, str(Path(__file__).resolve().parent))  # variants.py 同目录

from variants import PREP_VARIANTS  # noqa: E402

from lquant.data.store.parquet import read_daily  # noqa: E402
from lquant.factors.engine import FactorEngine  # noqa: E402
from lquant.factors.evaluate.ic import ic_series, ic_summary  # noqa: E402
from lquant.factors.evaluate.returns import forward_return  # noqa: E402
from lquant.factors.preprocess.pipeline import run as pipeline_run  # noqa: E402


def pick_symbols(lf: pl.LazyFrame, max_symbols: int) -> list[str]:
    """按窗口内成交额取流动性最好的 max_symbols 只**股票**（排除 ETF/北交所）。"""
    agg = (
        lf.select(["symbol", "amount"])
        .group_by("symbol")
        .agg(pl.col("amount").sum().alias("amt"))
        .collect()
    )
    agg = agg.filter(pl.col("symbol").str.contains(STOCK_RE.pattern))
    return agg.sort("amt", descending=True).head(max_symbols)["symbol"].to_list()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default="2024-12-31")
    ap.add_argument("--max-symbols", type=int, default=800)
    ap.add_argument("--min-obs", type=int, default=5, help="lquant ic_series 的 min_obs")
    ap.add_argument("--factor-expr", default="Ts_Return($close, 20)")
    ap.add_argument("--factor-name", default="mom20")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # ---------- 1. 取数 ----------
    lf_all = read_daily(start=args.start, end=args.end)
    syms = pick_symbols(lf_all, args.max_symbols)
    if not syms:
        raise SystemExit("湖里没有可用的股票数据（检查 LQ_DATA_DIR / data/parquet/daily）")
    print(f"[lquant] 股票池 {len(syms)} 只（按成交额取头部）")

    raw = (
        read_daily(symbols=syms, start=args.start, end=args.end)
        .select(["symbol", "trade_date", "close", "adj_factor"])
        .collect()
    )
    # 复权：同一标的内的收益率与复权基准无关，统一用 close*adj_factor
    df = (
        raw.with_columns((pl.col("close") * pl.col("adj_factor")).alias("close"))
        .filter(pl.col("close").is_not_null() & (pl.col("close") > 0))
        .select(["symbol", "trade_date", "close"])
    )
    print(f"[lquant] 原始行数 {df.height}")

    # ---------- 2. 用 lquant DSL 引擎算因子 ----------
    fdf = FactorEngine(df.lazy()).compute(args.factor_expr, args.factor_name)
    fdf = fdf.filter(pl.col(args.factor_name).is_not_null())
    print(f"[lquant] 因子 {args.factor_name} = {args.factor_expr}，行数 {fdf.height}")

    panel = fdf.select(["trade_date", "symbol", "close", args.factor_name]).sort(
        ["symbol", "trade_date"]
    )
    panel.write_parquet(out / "panel.parquet")

    # ---------- 3. lquant 的 IC / RankIC ----------
    ret = forward_return(fdf, "close", [1, 5])
    ic = ic_series(ret, args.factor_name, "fwd_ret_1", min_obs=args.min_obs)
    ic.write_parquet(out / "lquant_ic.parquet")
    summary = ic_summary(ret, args.factor_name, "fwd_ret_1", min_obs=args.min_obs)
    print(f"[lquant] IC 天数 {ic.height}；IC 均值 {summary['ic'].get('mean')}")

    # ---------- 4. 预处理变体（清单见 variants.py） ----------
    base = fdf.select(["trade_date", "symbol", args.factor_name])
    prep = None
    for v in PREP_VARIANTS:
        r = pipeline_run(base, args.factor_name, v["lq"], keep_original=True)
        cur = r.select(
            [
                pl.col("trade_date"),
                pl.col("symbol"),
                pl.col(f"{args.factor_name}_clean").alias(v["key"]),
            ]
        )
        prep = cur if prep is None else prep.join(cur, on=["trade_date", "symbol"], how="inner")
        print(f"[lquant] prep {v['key']:28s} 行数 {cur.height}")
    prep.write_parquet(out / "lquant_prep.parquet")

    meta = {
        "side": "lquant",
        "factor_expr": args.factor_expr,
        "factor_name": args.factor_name,
        "start": args.start,
        "end": args.end,
        "max_symbols": args.max_symbols,
        "min_obs": args.min_obs,
        "mad_k": MAD_K,
        "variants": [v["key"] for v in PREP_VARIANTS],
        "n_symbols": panel["symbol"].n_unique(),
        "n_rows": panel.height,
        "n_days": panel["trade_date"].n_unique(),
        "date_min": str(panel["trade_date"].min()),
        "date_max": str(panel["trade_date"].max()),
        "ic_summary": summary,
    }
    (out / "lquant_summary.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, default=str)
    )
    print(f"[lquant] 完成 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
