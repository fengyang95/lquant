#!/usr/bin/env bash
# 改动相关测试选择：只跑「被本次改动影响」的测试文件，而不是每次全量 300+ 文件。
#
# 用途两处：
#   1. 开发者日常：make test-changed（或直接跑本脚本）——改完代码秒级反馈；
#   2. pre-push 钩子：默认跑改动相关测试，全量留给 CI（逃生门见下）。
#
# 映射规则（透明优先，宁可多跑不漏跑）：
#   tests/**/test_*.py 改动          → 直接跑该文件
#   tests/**/conftest.py 改动        → 夹具影响面跨文件 → 全量
#   src/lquant/<dir>/[<sub>/]<n>.py  → tests 里按 test_*<n>* 与 test_*<dir>* 文件名匹配
#   全局面改动（pyproject 的 pytest 段 / config/ / core/config.py /
#   scripts/init_db.py 等）          → 全量
#   web/** 与 crates/**              → 有各自测试链路，Python 测试跳过
#
# 全量 / 逃生门：
#   LQ_PUSH_FULL_TEST=1 环境变量 或 --full 参数 → 无视映射直接全量
#   映射结果为空（如只改 docs）→ 只跑 ruff + ABI 冒烟，不跑 pytest
#   --dry-run → 只打印将执行的 pytest 命令，不真正跑（推送前预览）
#
# 已知局限（有意为之，保持零依赖与可预测）：
#   文件名启发式覆盖不了「测试文件名与被测模块名无词根交集」的角落，
#   兜底策略是这些角落靠 CI 全量 + 全局面改动直接全量。要精确请 --full。
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

FULL=0
DRY_RUN=0
if [ "${LQ_PUSH_FULL_TEST:-0}" = "1" ]; then FULL=1; fi
for arg in "$@"; do
  case "$arg" in
    --full) FULL=1 ;;
    --dry-run) DRY_RUN=1 ;;
  esac
done

PY="$(pwd)/.venv/bin/python"
if [ ! -x "$PY" ]; then
  MAIN_ROOT="$(git rev-parse --path-format=absolute --git-common-dir)/.."
  if [ -x "$MAIN_ROOT/.venv/bin/python" ]; then
    PY="$MAIN_ROOT/.venv/bin/python"
    export PYTHONPATH="$(pwd)/src${PYTHONPATH:+:$PYTHONPATH}"
  elif [ "$DRY_RUN" != "1" ]; then
    echo "    无可用 .venv，跳过测试"; exit 0
  fi
fi

# ---- xdist 可用则并行；不可用（旧环境没装）退化为串行，行为不变 --------------
XDIST_ARGS=()
if [ "$DRY_RUN" != "1" ] && "$PY" -c "import xdist" 2>/dev/null; then
  XDIST_ARGS=(-n auto --dist loadgroup)
elif [ "$DRY_RUN" = "1" ]; then
  XDIST_ARGS=(-n auto --dist loadgroup)  # 预览按目标形态展示
fi

run_pytest() {
  if [ "$DRY_RUN" = "1" ]; then
    echo "==> [dry-run] pytest $*"
    return 0
  fi
  echo "==> pytest $*"
  "$PY" -m pytest -x -q "$@" || {
    echo "✗ 测试失败，修复后再继续（全量兜底: make coverage / LQ_PUSH_FULL_TEST=1 bash scripts/test_changed.sh --full）"
    exit 1
  }
}

if [ "$FULL" = "1" ]; then
  echo "==> 改动相关测试：全量模式（LQ_PUSH_FULL_TEST=1 / --full）"
  run_pytest tests "${XDIST_ARGS[@]}"
  exit 0
fi

# ---- 收集改动文件：基线分支的差异 + 工作区（未提交）叠加 ---------------------
BASE=origin/main
git rev-parse --verify --quiet "$BASE" >/dev/null || BASE=HEAD

CHANGED="$( { git diff --name-only --diff-filter=ACMR "$BASE"...HEAD 2>/dev/null || true;
              git diff --name-only --diff-filter=ACMR 2>/dev/null || true;
              git diff --name-only --diff-filter=ACMR --cached 2>/dev/null || true;
              git ls-files --others --exclude-standard 2>/dev/null || true; } \
            | sort -u )"

if [ -z "$CHANGED" ]; then
  echo "==> 无可检测的改动（vs $BASE 与工作区），跳过测试"
  exit 0
fi

# ---- 全局面改动 → 直接全量 ---------------------------------------------------
GLOBAL_TOUCHED=0
while IFS= read -r f; do
  case "$f" in
    pyproject.toml|uv.lock|scripts/init_db.py|scripts/githooks/*|conftest.py) GLOBAL_TOUCHED=1 ;;
    config/*) GLOBAL_TOUCHED=1 ;;
    tests/conftest.py|tests/*/conftest.py) GLOBAL_TOUCHED=1 ;;
    src/lquant/core/config.py|src/lquant/core/logging.py|src/lquant/_rust/*) GLOBAL_TOUCHED=1 ;;
  esac
done <<< "$CHANGED"

if [ "$GLOBAL_TOUCHED" = "1" ]; then
  echo "==> 改动含全局影响面（配置/夹具/核心），回退全量"
  run_pytest tests "${XDIST_ARGS[@]}"
  exit 0
fi

# ---- 文件名启发式映射 --------------------------------------------------------
SELECTED="$(mktemp "${TMPDIR:-/tmp}/lq-selected-tests.XXXXXX")"
trap 'rm -f "$SELECTED"' EXIT

MAPPED=0
while IFS= read -r f; do
  case "$f" in
    tests/*/test_*.py|tests/*/*/test_*.py)
      echo "$f" >> "$SELECTED"; MAPPED=1 ;;
    web/*|crates/*|docs/*|*.md|*.yml|*.yaml|*.toml|Makefile|lquant.sh|scripts/*)
      : ;;  # 非 Python 运行时改动：各有各的门（web vitest / cargo test / CI）
    src/lquant/*)
      [ "$(basename "$f")" = "__init__.py" ] && continue
      base="$(basename "$f" .py)"
      dir="$(basename "$(dirname "$f")")"
      [ "$dir" = "lquant" ] && dir=""
      # 两层词根匹配：模块名 + 所在目录名（如 src/lquant/server/api/data.py
      # 会同时命中 test_api_data*.py 与 test_server* 相关文件）
      { find tests \( -name "test_*${base}*.py" \
          ${dir:+-o -name "test_*${dir}*.py"} \) | sort -u >> "$SELECTED"; } 2>/dev/null || true
      MAPPED=1 ;;
  esac
done <<< "$CHANGED"

if [ "$MAPPED" = "0" ] || [ ! -s "$SELECTED" ]; then
  echo "==> 改动未映射到任何测试文件（$(echo "$CHANGED" | wc -l) 个改动文件，如文档/脚本），跑静态检查兜底"
  if [ "$DRY_RUN" = "1" ]; then
    echo "==> [dry-run] ruff check src tests"
  elif [ -x "$(pwd)/.venv/bin/ruff" ]; then
    .venv/bin/ruff check src tests || true
  fi
  if [ "$DRY_RUN" != "1" ]; then
    echo "==> ABI 冒烟"
    "$PY" -c "from lquant._rust.loader import status; print(status())" || true
  fi
  exit 0
fi

SELECTED_COUNT="$(sort -u "$SELECTED" | wc -l | tr -d ' ')"
echo "==> 改动相关测试：$SELECTED_COUNT 个测试文件（改动 $(echo "$CHANGED" | wc -l | tr -d ' ') 个文件，基线 $BASE）"
# 不用 mapfile：macOS 自带的 bash 3.2 没有它（readarray 同理），而本仓的
# 开发机就是 macOS —— 上一版在这里直接 `mapfile: command not found`，
# 让 push 以「测试失败」的名目被拦下来，真实原因却与测试无关。
FILES=()
while IFS= read -r f; do
  [ -n "$f" ] && FILES+=("$f")
done < <(sort -u "$SELECTED")
run_pytest "${FILES[@]}" "${XDIST_ARGS[@]}"
