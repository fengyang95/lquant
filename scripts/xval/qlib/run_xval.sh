#!/usr/bin/env bash
# qlib × lquant 基准口径对拍（Phase 1.3 验证，可重复执行）。
#
# 做两件事：
#   1. 从 lquant 日线湖导出一个小窗口 + **真实基准指数**（默认 000300.SH）到 qlib；
#   2. 比较「原生 index_daily」与「qlib bin」的基准序列，要求 float32 精度内一致。
#
# 用法：
#   bash scripts/xval/qlib/run_xval.sh [REPO_ROOT] [QLIB_DIR] [START] [END]
# 缺省：REPO_ROOT=/Users/lyp/code/lquant  QLIB_DIR=<worktree>/data/qlib-xval
#       START=2026-01-01  END=2026-09-30
#
# 注意：脚本只用主 venv（导出 + 对拍），不需要 pyqlib。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKTREE="$(cd "$HERE/../../.." && pwd)"
REPO_ROOT="${1:-/Users/lyp/code/lquant}"
QLIB_DIR="${2:-$WORKTREE/data/qlib-xval}"
START="${3:-2026-01-01}"
END="${4:-2026-09-30}"

PY="${LQ_PYTHON:-$REPO_ROOT/.venv/bin/python}"
export PYTHONPATH="$WORKTREE/src"

echo "==> 1/2 导出（$START ~ $END，含基准）"
# 从 REPO_ROOT 运行：read_daily 走那里的 data/parquet 真实湖；
# 但代码用 worktree 的 src（PYTHONPATH 优先）。
( cd "$REPO_ROOT" && "$PY" -m lquant.cli.main qlib export \
    --out "$QLIB_DIR" --start "$START" --end "$END" --top 60 )

echo "==> 2/2 基准口径对拍（原生 index_daily ↔ qlib bin）"
LQ_DUCKDB_PATH="$REPO_ROOT/data/duckdb/lquant.duckdb" \
  "$PY" "$HERE/benchmark_parity.py" \
    --qlib-dir "$QLIB_DIR" --symbol 000300.SH \
    --repo-root "$REPO_ROOT" \
    --out "$QLIB_DIR/benchmark_parity.json"

echo
echo "==> 完成。qlib 工作流可另跑：
  $REPO_ROOT/.venv-qlib/bin/python $WORKTREE/src/lquant/qlib_io/runner.py \\
      --provider $QLIB_DIR \\
      --config $WORKTREE/config/qlib/workflow_alpha158_smoke.yaml \\
      --market top60 --out $QLIB_DIR/last_metrics.json"
