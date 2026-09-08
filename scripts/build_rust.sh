#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../crates"

command -v cargo >/dev/null 2>&1 || { echo "缺少 cargo"; exit 1; }

# pyo3 / pyo3-polars / polars 三者 ABI 强绑定，版本必须一致。
# 版本错配要到运行时才炸，所以编译后必须跑 make smoke。
cargo build --release --workspace

command -v maturin >/dev/null 2>&1 || .venv/bin/pip install -q maturin || pip install -q maturin

for c in lq-ops lq-backtest lq-metrics; do
  (cd "$c" && maturin develop --release)
done

echo "构建完成，执行 make smoke 验证 ABI"
