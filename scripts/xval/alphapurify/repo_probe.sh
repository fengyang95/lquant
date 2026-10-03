#!/usr/bin/env bash
# AlphaPurify 仓库级核对：上游 main vs PyPI wheel、自带测试、API 契约探针。
#
# 用法：bash scripts/xval/alphapurify/repo_probe.sh
# 前置：已建 .venv-alphapurify（见 README）
set -euo pipefail

WORKTREE="${WORKTREE:-/Users/lyp/code/lquant/.claude/worktrees/qlibresearch}"
VENDOR="$WORKTREE/.vendor/AlphaPurify"
AP_PY="$WORKTREE/.venv-alphapurify/bin/python"
INST="$WORKTREE/.venv-alphapurify/lib/python3.12/site-packages/alphapurify"
export UV_CACHE_DIR="$WORKTREE/.uv-cache"

[[ -x "$AP_PY" ]] || { echo "缺 $AP_PY" >&2; exit 1; }

echo "==== 1. 上游 main vs 已安装 wheel ===="
if [[ ! -d "$VENDOR/.git" ]]; then
  echo "clone 上游…"
  git clone --depth 1 https://github.com/eliasswu/AlphaPurify "$VENDOR"
fi
echo "repo  __version__: $(grep -o '__version__ = \"[^\"]*\"' "$VENDOR/alphapurify/__init__.py")"
echo "wheel __version__: $(grep -o '__version__ = \"[^\"]*\"' "$INST/__init__.py")"
echo "-- 源文件差异行数 --"
for f in "$VENDOR"/alphapurify/*.py; do
  b=$(basename "$f")
  [[ -f "$INST/$b" ]] && echo "  $b: $(diff "$f" "$INST/$b" | grep -c '^[<>]' || true) 行"
done
echo "-- 版本元数据一致性 --"
grep -h '^version' "$VENDOR/pyproject.toml" || true
grep -h 'version=' "$VENDOR/setup.py" || true
grep -h '^version' "$VENDOR/setup.ini" || true

echo
echo "==== 2. 上游自带测试 ===="
"$AP_PY" -m pip install -q pytest 2>/dev/null || true
( cd "$VENDOR" && "$AP_PY" -m pytest tests -q --tb=no -rA 2>&1 | grep -E "^(PASSED|FAILED|ERROR)" || true )

echo
echo "==== 3. API 契约探针 ===="
"$AP_PY" - <<'PY'
from datetime import datetime
import polars as pl
from alphapurify import AlphaPurifier

df = pl.DataFrame({
    "trade_date": [datetime(2024, 1, 2)] * 8 + [datetime(2024, 1, 3)] * 8,
    "symbol": [f"s{i}" for i in range(8)] * 2,
    "f": [1.0, 2, 3, 4, 5, 6, 7, 8, 2.0, 3, 4, 5, 6, 7, 8, 9],
    "beta": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8] * 2,
})

def fresh():
    return AlphaPurifier(df, factor_name="f", trade_date_col="trade_date", symbol_col="symbol")

cases = [
    ("examples 文档写法: neutralize(neutralizer_cols=...)",
     lambda: fresh().neutralize("multiOLS", neutralizer_cols=["beta"])),
    ("winsorize('mad', n=3)  关键字传参",
     lambda: fresh().winsorize("mad", n=3)),
    ("winsorize('mad', 3)    位置传参",
     lambda: fresh().winsorize("mad", 3)),
    ("neutralize('random_forest')  注册表里的名字",
     lambda: fresh().neutralize("random_forest", ["beta"])),
    ("neutralize('randomforest')   实际可用的名字",
     lambda: fresh().neutralize("randomforest", ["beta"])),
]
for label, fn in cases:
    try:
        fn()
        print(f"  OK   {label}")
    except Exception as e:
        print(f"  FAIL {label}  ->  {type(e).__name__}: {str(e)[:80]}")
PY
