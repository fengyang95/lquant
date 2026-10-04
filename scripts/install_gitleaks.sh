#!/usr/bin/env bash
# =============================================================================
# 安装固定版本的 gitleaks，装到「所有 worktree 共享」的位置。
#
# 为什么不用 brew：
#   ① 本机 /usr/local 属主是 root，brew install 需要 sudo；
#   ② brew 装在系统前缀、版本随 brew 漂移，本机与 CI 容易对不上；
#   ③ 这个仓库有 60+ 个 worktree，装到 git-common-dir 下装一次就全都有。
#
# 安装位置：$(git rev-parse --git-common-dir)/lq-tools/bin/gitleaks
#   —— 在 .git 里面，天然不被跟踪，也不会被 git clean 清掉。
# scripts/secret_scan.sh 会自动从这里找到它（无需改 PATH）。
#
# 用法：
#   scripts/install_gitleaks.sh              # 装 LQ_GITLEAKS_VERSION（默认 8.30.1）
#   scripts/install_gitleaks.sh --force      # 已存在也重装
#
# 版本升级时同步改：本脚本默认值、.github/workflows/secret-scan.yml 的
# LQ_GITLEAKS_VERSION、docs/SECRET_HYGIENE.md 里写的版本。
#
# CI 也直接调用本脚本（不再在 workflow 里内联抄一遍下载/校验逻辑），
# 「本地过了 CI 挂」的实现漂移从根上没有了。
# =============================================================================
set -euo pipefail

VERSION="${LQ_GITLEAKS_VERSION:-8.30.1}"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
COMMON="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || echo "$ROOT/.git")"
DEST="$COMMON/lq-tools/bin"
BIN="$DEST/gitleaks"

case "$(uname -s)" in
  Darwin) OS="darwin" ;;
  Linux)  OS="linux" ;;
  *) echo "✗ 不支持的平台：$(uname -s)（本脚本只处理 macOS / Linux）" >&2; exit 2 ;;
esac
case "$(uname -m)" in
  x86_64|amd64) ARCH="x64" ;;
  arm64|aarch64) ARCH="arm64" ;;
  *) echo "✗ 不支持的架构：$(uname -m)" >&2; exit 2 ;;
esac

if [ -x "$BIN" ] && [ "$FORCE" -eq 0 ]; then
  _cur="$("$BIN" version 2>/dev/null | head -1 | tr -d 'v')"
  if [ "$_cur" = "$VERSION" ]; then
    echo "✓ gitleaks $VERSION 已就位：$BIN"
    exit 0
  fi
  echo "  已存在版本 $_cur ≠ 目标 $VERSION，重新安装"
fi

ASSET="gitleaks_${VERSION}_${OS}_${ARCH}.tar.gz"
BASE="https://github.com/gitleaks/gitleaks/releases/download/v${VERSION}"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/lq-gitleaks.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

echo "==> 下载 $ASSET"
# -f：HTTP 4xx/5xx 直接失败。不加的话 curl 会把错误页当成功存下来，
# 一路走到 checksum 才炸，报错信息和真实原因对不上。
curl -fsSL --retry 5 --retry-delay 3 --max-time 900 -o "$TMP/$ASSET" "$BASE/$ASSET"
curl -fsSL --retry 5 --retry-delay 3 --max-time 120 -o "$TMP/sums.txt" "$BASE/gitleaks_${VERSION}_checksums.txt"

echo "==> 校验 checksum"
# 不校验的话，release 被投毒 = 在开发机/CI 上跑任意二进制。
# 必须在 $TMP 里按 release 原始文件名校验：checksums.txt 里记的就是这个名字，
# `-c` 会按名字去当前目录找文件（存成别的名字就会 "No such file or directory"）。
# macOS 自带 shasum，Linux(GNU coreutils) 用 sha256sum，两者 -c 语义一致。
if command -v sha256sum >/dev/null 2>&1; then
  ( cd "$TMP" && grep "$ASSET" sums.txt | sha256sum -c - )
else
  ( cd "$TMP" && grep "$ASSET" sums.txt | shasum -a 256 -c - )
fi

echo "==> 安装到 $BIN"
( cd "$TMP" && tar -xzf "$ASSET" gitleaks )
mkdir -p "$DEST"
install -m 0755 "$TMP/gitleaks" "$BIN"
"$BIN" version
echo "✓ 完成。scripts/secret_scan.sh 会自动找到它，无需改 PATH。"
