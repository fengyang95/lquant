#!/usr/bin/env bash
# AlphaPurify x lquant 交叉验证 —— 一键运行（也是回归哨兵）。
#
# 设计：两个隔离环境各跑一侧，中间只通过 parquet 交换，谁都不 import 谁。
#   lquant 侧      → 主仓 .venv（有 lquant 依赖，无 alphapurify）
#   AlphaPurify 侧 → worktree .venv-alphapurify（有 alphapurify，无 lquant）
#   compare 侧     → AlphaPurify venv（只需要 polars）
#
# **因子与变体清单的唯一真源是 `variants.py`**：本脚本不写死任何清单，
# 只是遍历它 —— 加因子/加方法只改那一个文件。
#
# 用法：
#   bash scripts/xval/alphapurify/run_xval.sh [START] [END] [MAX_SYMBOLS]
#
# 环境变量：
#   FACTORS="mom20 mom5"   只跑指定因子（默认 variants.py 的全部）
#   CHECK=0                只看数字，不因超差返回非零（默认 1 = 哨兵模式）
#   OUT=...                输出根目录（默认 <worktree>/.xval-out，按因子分子目录）
set -euo pipefail

MAIN_REPO="${MAIN_REPO:-/Users/lyp/code/lquant}"
WORKTREE="${WORKTREE:-$MAIN_REPO/.claude/worktrees/qlibresearch}"
BASE_OUT="${OUT:-$WORKTREE/.xval-out}"
START="${1:-2023-01-01}"
END="${2:-2024-12-31}"
MAX_SYMBOLS="${3:-800}"
CHECK="${CHECK:-1}"

LQ_PY="$MAIN_REPO/.venv/bin/python"
AP_PY="$WORKTREE/.venv-alphapurify/bin/python"
SCRIPTS="$WORKTREE/scripts/xval/alphapurify"

[[ -x "$LQ_PY" ]] || { echo "缺主 venv: $LQ_PY" >&2; exit 1; }
[[ -x "$AP_PY" ]] || { echo "缺 alphapurify venv: $AP_PY（先 uv venv + uv pip install alphapurify）" >&2; exit 1; }

# 清单来自 variants.py（纯数据模块，主 venv 里 import 不需要任何依赖）
FACTOR_LIST="${FACTORS:-$( "$LQ_PY" -c "import sys; sys.path.insert(0, '$SCRIPTS'); from variants import FACTORS; print(' '.join(k for k, _ in FACTORS))" )}"
VARIANT_LIST="$( "$LQ_PY" -c "import sys; sys.path.insert(0, '$SCRIPTS'); from variants import variant_keys; print(', '.join(variant_keys()))" )"

echo "== 输出根目录: $BASE_OUT"
echo "== 窗口: $START → $END, 股票数上限: $MAX_SYMBOLS"
echo "== 因子: $FACTOR_LIST"
echo "== 哨兵模式: $CHECK（1 = 超差返回非零）"
echo "== 变体: $VARIANT_LIST"

FAILED=0
for NAME in $FACTOR_LIST; do
  EXPR="$( "$LQ_PY" -c "import sys; sys.path.insert(0, '$SCRIPTS'); from variants import FACTORS; print(dict(FACTORS)['$NAME'])" )"
  OUT="$BASE_OUT/$NAME"
  mkdir -p "$OUT"

  echo
  echo "########## 因子 $NAME = $EXPR ##########"

  echo "---- [1/3] lquant 侧（主 venv） ----"
  ( cd "$MAIN_REPO" && LQ_DATA_DIR="$MAIN_REPO/data" "$LQ_PY" "$SCRIPTS/lquant_side.py" \
      --out "$OUT" --start "$START" --end "$END" --max-symbols "$MAX_SYMBOLS" \
      --factor-expr "$EXPR" --factor-name "$NAME" )

  echo "---- [2/3] AlphaPurify 侧（隔离 venv） ----"
  "$AP_PY" "$SCRIPTS/alphapurify_side.py" --out "$OUT" --factor-name "$NAME"

  echo "---- [3/3] 对比 ----"
  if [[ "$CHECK" == "1" ]]; then
    "$AP_PY" "$SCRIPTS/compare.py" --out "$OUT" --factor-name "$NAME" --check || FAILED=1
  else
    "$AP_PY" "$SCRIPTS/compare.py" --out "$OUT" --factor-name "$NAME"
  fi
done

echo
if [[ "$FAILED" == "1" ]]; then
  echo "== **存在回归**：某个 expect=match 的变体超差，详见各因子目录的 xval_report.json" >&2
  exit 1
fi
echo "== 完成，全部 match 变体在容差内。报告: $BASE_OUT/<factor>/xval_report.json"
