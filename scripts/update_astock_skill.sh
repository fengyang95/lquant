#!/usr/bin/env bash
# 刷新 vendored 的 a-stock-data skill（第三方，Apache-2.0，见同目录 SOURCE.md）。
#
# 用法：scripts/update_astock_skill.sh
#
# 从上游 main 重新拉取 SKILL.md 覆盖 config/skills/a-stock-data/SKILL.md，
# 并把版本 / commit / 日期写回 SOURCE.md。需要网络（GitHub；国内可先 export
# HTTPS_PROXY=http://127.0.0.1:7890）。
set -euo pipefail

REPO="simonlin1212/a-stock-data"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_DIR="$ROOT/config/skills/a-stock-data"
DEST="$DEST_DIR/SKILL.md"
SOURCE="$DEST_DIR/SOURCE.md"
RAW="https://raw.githubusercontent.com/$REPO/main/SKILL.md"
TODAY="$(date +%Y-%m-%d)"

# 临时文件落在目标目录：同一文件系统，最后 mv 才是原子替换
tmp="$(mktemp "$DEST_DIR/.SKILL.md.XXXXXX")"
trap 'rm -f "$tmp"' EXIT

echo "拉取 $RAW …"
# -f：HTTP 错误直接失败；先写临时文件，校验通过才覆盖，避免半截响应写坏好文件
curl -fsSL "$RAW" -o "$tmp"

# 内容校验：frontmatter 必须仍是 a-stock-data，否则上游改了结构、别盲目覆盖
if ! grep -q '^name: a-stock-data$' "$tmp"; then
  echo "✗ 下载内容不含预期的 'name: a-stock-data' frontmatter，已放弃覆盖。" >&2
  exit 1
fi

bytes="$(wc -c < "$tmp" | tr -d ' ')"
lines="$(wc -l < "$tmp" | tr -d ' ')"
# `|| true`：pipefail 下 grep/git 失败会让整个管道非零，set -e 会在 `:-unknown`
# 兜底生效**之前**就把脚本干掉（版本/commit 取不到时应降级为 unknown，不是中止）
version="$(grep -m1 '^version:' "$tmp" | awk '{print $2}' || true)"
version="${version:-unknown}"

# commit SHA 用 ls-remote 拿，免克隆、免 API 鉴权
commit="$(git ls-remote "https://github.com/$REPO.git" refs/heads/main | awk '{print $1}' || true)"
commit="${commit:-unknown}"

mv "$tmp" "$DEST"
trap - EXIT

# 元数据写回 SOURCE.md：用 '#' 作 sed 分隔符，替换串里的 '|' 才是字面量
sed -i.bak -E \
  -e "s#^\| 版本 \| .*#| 版本 | $version |#" \
  -e "s#^\| 快照 commit \| .*#| 快照 commit | $commit |#" \
  -e "s#^\| 拉取日期 \| .*#| 拉取日期 | $TODAY |#" \
  -e "s#^\| 大小 \| .*#| 大小 | $bytes 字节 / $lines 行 |#" \
  "$SOURCE"
rm -f "$SOURCE.bak"

echo "✓ a-stock-data 更新到 v${version}（${commit}）"
echo "  $bytes 字节 / $lines 行 → $DEST"
