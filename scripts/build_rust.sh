#!/usr/bin/env bash
# 编译 Rust 扩展并装入 .venv（无 venv 时先跑 ./lquant.sh install）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/crates"

# cargo 不在 PATH 时兜底：rustup 标准位置 / 半路中断的 rustup 残留 toolchain
if ! command -v cargo >/dev/null 2>&1; then
  for b in "$HOME/.cargo/bin" "$HOME"/.rustup/toolchains/*/bin; do
    [ -x "$b/cargo" ] || continue
    export PATH="$b:$PATH"
    break
  done
fi
command -v cargo >/dev/null 2>&1 || { echo "缺少 cargo"; exit 1; }
[ -x "$ROOT/.venv/bin/python" ] || { echo "缺少 .venv，先跑 ./lquant.sh install"; exit 1; }

# pyo3 / pyo3-polars / polars 三者 ABI 强绑定，版本必须一致（见 crates/Cargo.toml 对照表）。
# 版本错配要到运行时才炸，所以编译后必须跑 make smoke。
cargo build --release --workspace

# cdylib 直接拷进 site-packages 即完成"develop 安装"（等价 maturin develop，
# 但省掉 maturin 触发的全量重编译）
SITE="$("$ROOT/.venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
for c in lq-ops lq-backtest lq-metrics; do
  mod="$(echo "$c" | tr '-' '_')"
  lib="$ROOT/crates/target/release/lib${mod}.dylib"
  [ -f "$lib" ] || lib="$ROOT/crates/target/release/lib${mod}.so"
  [ -f "$lib" ] || { echo "未找到编译产物 $mod"; exit 1; }
  cp "$lib" "$SITE/${mod}.so"
  echo "  已安装 ${mod}.so"
done

echo "构建完成，执行 make smoke 验证 ABI"
