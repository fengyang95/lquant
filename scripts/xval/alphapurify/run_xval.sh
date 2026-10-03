#!/usr/bin/env bash
# AlphaPurify x lquant 交叉验证 —— 一键运行。
#
# 设计：两个隔离环境各跑一侧，中间只通过 parquet 交换，谁都不 import 谁。
#   lquant 侧      → 主仓 .venv（有 lquant 依赖，无 alphapurify）
#   AlphaPurify 侧 → worktree .venv-alphapurify（有 alphapurify，无 lquant）
#   compare 侧     → AlphaPurify venv（只需要 polars）
#
# 用法：
#   bash scripts/xval/alphapurify/run_xval.sh [START] [END] [MAX_SYMBOLS]
set -euo pipefail

MAIN_REPO="${MAIN_REPO:-/Users/lyp/code/lquant}"
WORKTREE="${WORKTREE:-$MAIN_REPO/.claude/worktrees/qlibresearch}"
OUT="${OUT:-$WORKTREE/.xval-out}"
START="${1:-2023-01-01}"
END="${2:-2024-12-31}"
MAX_SYMBOLS="${3:-800}"

LQ_PY="$MAIN_REPO/.venv/bin/python"
AP_PY="$WORKTREE/.venv-alphapurify/bin/python"
SCRIPTS="$WORKTREE/scripts/xval/alphapurify"

[[ -x "$LQ_PY" ]] || { echo "缺主 venv: $LQ_PY" >&2; exit 1; }
[[ -x "$AP_PY" ]] || { echo "缺 alphapurify venv: $AP_PY（先 uv venv + uv pip install alphapurify）" >&2; exit 1; }

mkdir -p "$OUT"
echo "== 输出目录: $OUT"
echo "== 窗口: $START → $END, 股票数上限: $MAX_SYMBOLS"

echo
echo "---- [1/3] lquant 侧（主 venv） ----"
( cd "$MAIN_REPO" && LQ_DATA_DIR="$MAIN_REPO/data" "$LQ_PY" "$SCRIPTS/lquant_side.py" \
    --out "$OUT" --start "$START" --end "$END" --max-symbols "$MAX_SYMBOLS" )

echo
echo "---- [2/3] AlphaPurify 侧（隔离 venv） ----"
"$AP_PY" "$SCRIPTS/alphapurify_side.py" --out "$OUT"

echo
echo "---- [3/3] 对比 ----"
"$AP_PY" "$SCRIPTS/compare.py" --out "$OUT"

echo
echo "== 完成。报告: $OUT/xval_report.json"
