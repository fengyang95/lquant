#!/usr/bin/env bash
# =============================================================================
# 密钥 / 敏感信息扫描统一入口
#
# 本地钩子（pre-commit / pre-push）、CI、手工审计共用这一份实现与规则 ——
# 三处口径必须一致。否则「本地过了、CI 挂」或反过来，门禁迟早被 --no-verify 绕过。
#
# 用法：
#   scripts/secret_scan.sh staged            暂存区（pre-commit 调用）
#   scripts/secret_scan.sh push [reflog]     推送范围（pre-push 调用；缺省读 stdin）
#   scripts/secret_scan.sh range <A..B>      指定提交范围
#   scripts/secret_scan.sh history           全部历史 + 所有分支（公开前必跑）
#   scripts/secret_scan.sh dir               工作区「已跟踪文件」快照
#   scripts/secret_scan.sh hygiene           内部文件 / 个人路径卫生检查
#   scripts/secret_scan.sh public            公开前体检 = history + dir + hygiene(严格)
#   scripts/secret_scan.sh all               以上全部
#
# 退出码：0 通过；1 发现风险；2 环境不满足（缺 gitleaks / 版本过旧 / 缺配置）
#
# 设计取舍（三条都是刻意的，不要"顺手"改掉）：
#   1) 缺 gitleaks 时**失败关闭**（exit 2），不静默放行。
#      「扫描器没跑起来」和「扫了没问题」是两件事，混淆它们等于没有门禁。
#   2) 默认 --redact：连报告里都不回显明文，避免扫描日志本身变成泄露渠道。
#      本地要看原文可以 LQ_SECRET_SCAN_NO_REDACT=1。
#   3) 白名单放在 .gitleaks.toml，尽量按「值」精确放行而不是整目录跳过 ——
#      跳过 tests/ 会让「测试里塞真密钥」这种最常见的泄露方式失去保护。
#
# 环境变量：
#   LQ_GITLEAKS                    指定 gitleaks 可执行文件
#   LQ_SECRET_SCAN_NO_REDACT=1     报告里回显明文（仅本地排查用）
#   LQ_SECRET_REPORT=<path>        额外输出 JSON 报告（CI 当 artifact 用）
#   LQ_SECRET_SCAN_ALLOW_MISSING=1 缺 gitleaks 时降级为警告并放行（不推荐）
#   LQ_HYGIENE_STRICT=1            卫生检查把"个人路径"也算失败（public 模式默认开）
#
# 兼容性：macOS 自带 bash 3.2，本脚本刻意不用 mapfile / ${var,,} / 关联数组。
# =============================================================================
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

# 配置解析：优先当前工作树；没有就退回主仓的。
# 为什么需要回退：core.hooksPath 指向主仓，64 个 worktree 共用同一套钩子，
# 而旧分支的 worktree 里根本没有 .gitleaks.toml。不回退的话，那些 worktree
# 的 push 会以「缺少扫描配置」直接失败 —— 报错完全指不到真正原因。
CONFIG="$ROOT/.gitleaks.toml"
if [ ! -f "$CONFIG" ]; then
  _common_root="$(cd "$(git rev-parse --path-format=absolute --git-common-dir)/.." 2>/dev/null && pwd)" || _common_root=""
  if [ -n "$_common_root" ] && [ -f "$_common_root/.gitleaks.toml" ]; then
    CONFIG="$_common_root/.gitleaks.toml"
  fi
fi

MIN_VERSION="8.19.0"
GL=""
#: 脚本自己创建的临时报告路径（非空时退出前删除；调用方指定报告时保持为空）
REPORT_TMP=""

# 「内部路径」：不应进入公开仓的本机文件。staged / hygiene 两处共用同一份 ——
# 口径不一致正是门禁被绕过的主因。注意**不能**写成裸 `\.claude/`：
# .claude/commands/ 与 .claude/skills/dev-workflow/ 是有意入库的。
INTERNAL_RE='^(\.claude/worktrees/|\.claude/CLAUDE\.md|\.claude/skills/wiki/|\.omc/|\.idea/|lquant/|\.tmp_|logs/|dist/)'

# --- 输出小工具 --------------------------------------------------------------
info() { printf '    %s\n' "$*"; }
ok()   { printf '  ✓ %s\n' "$*"; }
bad()  { printf '  ✗ %s\n' "$*" >&2; }
warn() { printf '  ! %s\n' "$*" >&2; }
indent() { while IFS= read -r _line; do [ -n "$_line" ] && printf '      %s\n' "$_line"; done; }

usage() {
  cat <<'EOF'
用法: scripts/secret_scan.sh <模式> [参数]

模式:
  staged            扫描暂存区（pre-commit 用）
  push [reflog]     扫描本次推送的提交范围（pre-push 用；缺省从 stdin 读 ref 列表）
  range <A..B>      扫描指定提交范围
  history           扫描全部历史 + 所有分支（公开前必跑）
  dir               扫描工作区「已跟踪文件」快照
  hygiene           内部文件 / 个人路径卫生检查（不需要 gitleaks）
  public            公开前体检 = history + dir + hygiene(严格)
  all               以上全部

示例:
  make secrets                 # 暂存区
  make secrets-history         # 全历史
  make public-ready            # 公开前体检

退出码: 0 通过 / 1 发现风险 / 2 环境不满足
EOF
}

# --- gitleaks 定位与版本校验 -------------------------------------------------
version_ge() {  # $1 >= $2 ?
  local _a _b
  _a="$(printf '%s' "$1" | awk -F. '{printf "%d%03d%03d", $1, $2, $3}')"
  _b="$(printf '%s' "$2" | awk -F. '{printf "%d%03d%03d", $1, $2, $3}')"
  [ "$_a" -ge "$_b" ]
}

find_gitleaks() {
  if [ -n "${LQ_GITLEAKS:-}" ]; then
    if [ -x "$LQ_GITLEAKS" ]; then printf '%s' "$LQ_GITLEAKS"; return 0; fi
    warn "LQ_GITLEAKS 指向的文件不可执行：$LQ_GITLEAKS"
  fi
  if command -v gitleaks >/dev/null 2>&1; then
    command -v gitleaks; return 0
  fi
  # 仓库自带的共享安装位（make secrets-install 装到这里，所有 worktree 共用）
  local _common _c
  _common="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
  for _c in \
      "${_common:+$_common/lq-tools/bin/gitleaks}" \
      "$ROOT/.tools/bin/gitleaks" \
      "$ROOT/.venv/bin/gitleaks"; do
    if [ -n "$_c" ] && [ -x "$_c" ]; then printf '%s' "$_c"; return 0; fi
  done
  return 1
}

require_gitleaks() {
  if ! GL="$(find_gitleaks)"; then
    if [ "${LQ_SECRET_SCAN_ALLOW_MISSING:-0}" = "1" ]; then
      warn "未找到 gitleaks；LQ_SECRET_SCAN_ALLOW_MISSING=1 生效，本次放行（不推荐）"
      exit 0
    fi
    bad "未找到 gitleaks，密钥门禁无法执行（失败关闭）"
    cat >&2 <<'EOF'
    安装（免 sudo，装一次所有 worktree 共用）:
        make secrets-install
    或自行安装并确保在 PATH 中:
        brew install gitleaks
    临时放行（不推荐）:
        LQ_SECRET_SCAN_ALLOW_MISSING=1 git push
EOF
    exit 2
  fi
  local _ver
  _ver="$("$GL" version 2>/dev/null | head -1 | tr -d 'v')"
  if ! version_ge "$_ver" "$MIN_VERSION"; then
    bad "gitleaks 版本过旧：${_ver}（需要 >= ${MIN_VERSION}，本脚本用的是 git/dir 子命令）"
    exit 2
  fi
  if [ ! -f "$CONFIG" ]; then
    bad "缺少扫描配置：$CONFIG"
    exit 2
  fi
  # 报告固定写到临时文件：① 让 --report-path 生效，CI 里可当 artifact；
  # ② 失败时 print_findings 从里面摘出「只有位置、没有值」的摘要。
  # 调用方若自己设了 LQ_SECRET_REPORT 就用它的（CI 想留档时用得上）。
  if [ -z "${LQ_SECRET_REPORT:-}" ]; then
    LQ_SECRET_REPORT="$(mktemp "${TMPDIR:-/tmp}/lq-secret-report.XXXXXX")"
    REPORT_TMP="$LQ_SECRET_REPORT"
  fi
}

# --- gitleaks 调用封装 -------------------------------------------------------
gl() {
  local _sub="$1"; shift
  local -a _extra
  _extra=(--config "$CONFIG" --no-banner --no-color)
  [ "${LQ_SECRET_SCAN_NO_REDACT:-0}" = "1" ] || _extra=( "${_extra[@]}" --redact )
  # -v 才会打印 File/Line/RuleID；不带 -v 只有一句 "leaks found: N"，定位不了。
  # 但公开仓的 CI 日志是**公开可见**的，-v 的 Finding 行会把密钥尾部片段一并
  # 打出来（--redact 只覆盖规则捕获到的 secret group，捕获边界外的字符会留在
  # 上下文里）。所以 CI 里关掉 -v，改由 print_findings 输出「只有位置、没有值」。
  local _verbose="${LQ_SECRET_SCAN_VERBOSE:-}"
  if [ -z "$_verbose" ]; then
    if [ -n "${CI:-}${GITHUB_ACTIONS:-}" ]; then _verbose=0; else _verbose=1; fi
  fi
  if [ "$_verbose" = "1" ]; then
    _extra=( "${_extra[@]}" -v )
  fi
  if [ -n "${LQ_SECRET_REPORT:-}" ]; then
    _extra=( "${_extra[@]}" --report-format json --report-path "$LQ_SECRET_REPORT" )
  fi
  "$GL" "$_sub" "${_extra[@]}" "$@"
}

# run_scan <标签> <gitleaks 参数...>
# 返回 0=干净 / 1=发现风险 / 2=扫描器异常
run_scan() {
  local _label="$1"; shift
  local _rc=0
  gl "$@" || _rc=$?
  case "$_rc" in
    0) ok "${_label}：未发现密钥" ;;
    1) bad "${_label}：发现疑似密钥" ;;
    *) bad "${_label}：扫描器异常退出（rc=${_rc}）" ;;
  esac
  return "$_rc"
}

# 从 JSON 报告里摘出「只有位置、没有值」的摘要。
# 刻意不读 Match/Secret 字段 —— 这个函数的输出会进 CI 日志，而公开仓的
# CI 日志是公开的。gitleaks 在 --redact 下这两个字段本身就是 REDACTED，
# 但少读一个字段就少一条泄露路径。
print_findings() {
  local _r="$1" _out=""
  [ -s "$_r" ] || return 0
  if command -v jq >/dev/null 2>&1; then
    _out="$(jq -r '.[]? | "      \(.File):\(.StartLine)  \(.RuleID)"' "$_r" 2>/dev/null || true)"
  elif command -v python3 >/dev/null 2>&1; then
    _out="$(python3 -c '
import json, sys
try:
    items = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
if isinstance(items, list):
    for it in items:
        print("      %s:%s  %s" % (it.get("File", "?"), it.get("StartLine", "?"), it.get("RuleID", "?")))
' "$_r" 2>/dev/null || true)"
  fi
  if [ -n "$_out" ]; then
    echo "    命中位置（值已隐藏）："
    printf '%s\n' "$_out"
  fi
  return 0
}

# --- 各扫描模式 --------------------------------------------------------------
# 暂存区里是否混进了「内部文件」。这类东西提交进公开仓后只能靠改写历史清理，
# 所以在最早的位置（pre-commit）就拦住。
check_staged_hygiene() {
  local _x
  _x="$(git diff --cached --name-only --diff-filter=ACMR 2>/dev/null \
        | grep -E "$INTERNAL_RE|(^|/)\.env" \
        | grep -vE '(^|/)\.env\.(example|sample|template)$' || true)"
  if [ -n "$_x" ]; then
    bad "暂存区含内部文件（不应进入公开仓）："
    printf '%s\n' "$_x" | indent >&2
    info "修法：git restore --staged <path>，并把该模式加进 .gitignore"
    return 1
  fi
  ok "暂存区没有内部文件"
  return 0
}

scan_staged() {
  echo "==> secret-scan: 暂存区"
  local _rc=0
  check_staged_hygiene || _rc=1
  run_scan "暂存区" git --staged || _rc=$?
  return "$_rc"
}

scan_range() {
  local _range="${1:-}"
  if [ -z "$_range" ]; then bad "range 模式需要参数，例如 origin/main..HEAD"; return 2; fi
  echo "==> secret-scan: 提交范围 $_range"
  run_scan "范围 $_range" git --log-opts="$_range"
}

scan_history() {
  echo "==> secret-scan: 全部历史（所有分支）"
  info "仓库：$ROOT"
  # --diff-merges=on 是必须的：`git log -p` 默认**不输出 merge 提交的 diff**，
  # 于是「evil merge」——两个父提交都没有、只在 merge 里出现的内容——成为盲区。
  # 实测本仓：默认 433 个提交 → 加该选项 485 个，扫描量 47MB → 59MB。
  run_scan "全部历史" git --log-opts="--all --diff-merges=on"
}

scan_dir() {
  echo "==> secret-scan: 工作区「已跟踪文件」快照"
  # 为什么导出而不是直接 gitleaks dir . ：工作区里有 .venv / node_modules /
  # data 湖，直接扫又慢又吵；而"公开后会被人看到的内容"恰好就是 git 跟踪的那批。
  local _tmp _rc=0
  _tmp="$(mktemp -d "${TMPDIR:-/tmp}/lq-secret-dir.XXXXXX")"
  mkdir -p "$_tmp/tree"
  if git ls-files > "$_tmp/list" 2>/dev/null && [ -s "$_tmp/list" ]; then
    if command -v rsync >/dev/null 2>&1; then
      # -a 保留符号链接（不跟进 data 湖）
      rsync -a --files-from="$_tmp/list" "$ROOT/" "$_tmp/tree/" >/dev/null 2>&1 || true
    else
      warn "未找到 rsync，回退到 git archive HEAD（不含未提交改动）"
      git archive HEAD 2>/dev/null | tar -x -C "$_tmp/tree" 2>/dev/null || true
    fi
    run_scan "工作区已跟踪文件" dir "$_tmp/tree" || _rc=$?
  else
    info "没有已跟踪文件，跳过"
  fi
  rm -rf "$_tmp"
  return "$_rc"
}

scan_push() {
  local _reflog="${1:-}"
  local _tmp=""
  if [ -z "$_reflog" ]; then
    _tmp="$(mktemp "${TMPDIR:-/tmp}/lq-push-refs.XXXXXX")"
    cat > "$_tmp"
    _reflog="$_tmp"
  fi
  echo "==> secret-scan: 推送范围"
  local _rc=0 _scanned=0
  local _lref _lsha _rref _rsha _range
  # git 传给 pre-push 的每行：<local ref> <local sha> <remote ref> <remote sha>
  while read -r _lref _lsha _rref _rsha; do
    [ -n "${_lsha:-}" ] || continue
    case "$_lsha" in
      0000000000000000000000000000000000000000) info "跳过删除分支：$_rref"; continue ;;
    esac
    if [ -z "${_rsha:-}" ] || [ "$_rsha" = "0000000000000000000000000000000000000000" ]; then
      # 远端还没有这个分支：扫「本地有、任何远端都没有」的提交（保守取大集）
      _range="$_lsha --not --remotes"
    else
      [ "$_rsha" = "$_lsha" ] && continue
      _range="$_rsha..$_lsha"
    fi
    info "扫描 ${_lref} → ${_rref}（${_range}）"
    run_scan "推送范围 $_rref" git --log-opts="$_range" || _rc=$?
    _scanned=1
  done < "$_reflog"
  [ "$_scanned" -eq 0 ] && info "没有需要扫描的新提交"
  [ -n "$_tmp" ] && rm -f "$_tmp"
  return "$_rc"
}

check_hygiene() {
  echo "==> secret-scan: 卫生检查（内部文件 / 个人路径）"
  local _strict="${LQ_HYGIENE_STRICT:-0}"
  local _fail=0 _x

  # 1) 未跟踪且未被忽略的内部路径 —— git add -A 会把它们一起提交进公开仓
  _x="$(git status --porcelain --untracked-files=all 2>/dev/null \
          | sed 's/^...//' \
          | grep -E "$INTERNAL_RE" || true)"
  if [ -n "$_x" ]; then
    bad "以下内部路径未被 .gitignore 覆盖，git add -A 会扫进公开仓："
    printf '%s\n' "$_x" | indent >&2
    info "修法：加进 .gitignore（见 docs/SECRET_HYGIENE.md）"
    _fail=1
  else
    ok "没有会被 git add -A 误提交的内部路径"
  fi

  # 2) 嵌套 git 仓 —— git add -A 会提交成 gitlink，内容对公开仓读者不可见
  _x="$(find . -mindepth 2 -maxdepth 5 -type d -name .git \
          -not -path './.git/*' -not -path './.claude/worktrees/*' \
          -not -path './.venv/*' -not -path './node_modules/*' 2>/dev/null | head -10 || true)"
  if [ -n "$_x" ]; then
    bad "发现嵌套 git 仓（会被提交成 gitlink，且内容对公开仓不可见）："
    printf '%s\n' "$_x" | indent >&2
    _fail=1
  else
    ok "没有嵌套 git 仓"
  fi

  # 3) .env 之类绝不该被跟踪的文件
  _x="$(git ls-files | grep -E '(^|/)\.env($|\.)' || true)"
  if [ -n "$_x" ]; then
    bad "以下 .env 文件被 git 跟踪（应只存在于本地）："
    printf '%s\n' "$_x" | indent >&2
    _fail=1
  else
    ok ".env 未被跟踪"
  fi

  # 4) 已跟踪文件里的本机绝对路径（隐私信息，不是密钥）
  local _n
  _x="$(git grep -lIE '/(Users|home)/[A-Za-z0-9._-]+/' -- . 2>/dev/null | head -30 || true)"
  _n="$(printf '%s' "$_x" | grep -c . || true)"
  if [ "$_n" -gt 0 ]; then
    if [ "$_strict" = "1" ]; then
      bad "已跟踪文件含本机绝对路径（$_n 个）—— 公开后会暴露用户名与目录结构："
      _fail=1
    else
      warn "已跟踪文件含本机绝对路径（$_n 个）—— 公开前建议处理："
    fi
    printf '%s\n' "$_x" | indent >&2
  else
    ok "未发现本机绝对路径"
  fi

  return "$_fail"
}

# --- 入口 --------------------------------------------------------------------
main() {
  local _mode="${1:-help}"
  [ $# -gt 0 ] && shift || true

  case "$_mode" in
    help|-h|--help) usage; exit 0 ;;
    staged|push|range|history|dir|hygiene|public|all) ;;
    *) bad "未知模式：$_mode"; echo; usage >&2; exit 2 ;;
  esac

  # hygiene 本身不需要 gitleaks
  case "$_mode" in
    hygiene) ;;
    *) require_gitleaks ;;
  esac

  local _rc=0
  case "$_mode" in
    staged)  scan_staged         || _rc=$? ;;
    push)    scan_push "${1:-}"  || _rc=$? ;;
    range)   scan_range "${1:-}" || _rc=$? ;;
    history) scan_history        || _rc=$? ;;
    dir)     scan_dir            || _rc=$? ;;
    hygiene) check_hygiene       || _rc=$? ;;
    public)  { scan_history && scan_dir && LQ_HYGIENE_STRICT=1 check_hygiene; } || _rc=$? ;;
    all)     { scan_staged && scan_history && check_hygiene; } || _rc=$? ;;
  esac

  if [ "$_rc" -eq 0 ]; then
    echo "==> secret-scan 通过（${_mode}）"
  else
    echo "==> secret-scan 未通过（${_mode}，rc=${_rc}）" >&2
    if [ -n "${LQ_SECRET_REPORT:-}" ]; then
      print_findings "$LQ_SECRET_REPORT" >&2
    fi
    # 只有调用方自己指定了报告路径才提示它 —— 脚本自建的临时报告马上会被删掉，
    # 把它的路径打出来只会让人去 cat 一个不存在的文件。
    if [ -z "$REPORT_TMP" ] && [ -n "${LQ_SECRET_REPORT:-}" ]; then
      info "完整报告（值已脱敏）：$LQ_SECRET_REPORT" >&2
    fi
  fi
  if [ -n "$REPORT_TMP" ]; then
    rm -f "$REPORT_TMP"
  fi
  return "$_rc"
}

main "$@"
