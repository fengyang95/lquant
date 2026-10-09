#!/usr/bin/env bash
# ============================================================================
# git_guard.sh —— 部署前的 git 状态守卫（被 lquant.sh source）
#
# 背景：主仓 core.bare 曾被外部工具（WorkBuddy 链接工作树从共享 .git/config
# 写入）置为 true，导致主目录无法 git status / 切分支 / 部署；而 lquant.sh 的
# git 拉取又是 fail-soft，会静默地用旧代码继续构建。这里提供两道守卫：
#
#   1) ensure_git_worktree   —— 检测并自愈 core.bare，保证主目录是可用工作树
#   2) require_deploy_branch —— 部署前校验分支与干净树，避免静默部署旧代码
#
# 依赖调用方已定义 warn/dim/fail；单独 source 时回退到 echo，便于单测。
# 兼容 bash 3.2（macOS 自带），不使用关联数组等 4.x 特性。
#
# 范围说明：ensure_git_worktree 只在**主工作树**生效。从链接工作树里运行时
# `--is-inside-work-tree` 恒为 true，看不到主仓的 core.bare；而部署本就要求在
# 主目录（/Users/lyp/code/lquant）进行，所以这不影响实际部署路径。
# ============================================================================

# 若调用方未定义日志函数，则回退（便于单测直接 source 本文件）。
# 用 `type -t` 而非 `command -v`：后者会把外部二进制（如 /usr/bin/info）也算命中。
if [ "$(type -t warn 2>/dev/null)" != "function" ]; then
  warn() { printf '==> %s\n' "$*" >&2; }
fi
if [ "$(type -t dim 2>/dev/null)" != "function" ]; then
  dim() { printf '    %s\n' "$*"; }
fi
if [ "$(type -t fail 2>/dev/null)" != "function" ]; then
  fail() { printf '==> %s\n' "$*" >&2; exit 1; }
fi

# ensure_git_worktree: 保证当前目录是一个可用的 git 工作树。
#   - 非 git 仓库：静默跳过（返回 0）
#   - core.bare=true（外部工具误置）：自动纠正并告警；纠正后仍非工作树则硬停
ensure_git_worktree() {
  git rev-parse --git-dir >/dev/null 2>&1 || return 0

  # 注意：`--is-inside-work-tree` 在 bare 仓里会打印 "false" 但**退出码仍为 0**，
  # 所以必须比对输出而非退出码。
  if [ "$(git rev-parse --is-inside-work-tree 2>/dev/null)" = "true" ]; then
    return 0
  fi

  # 不在工作树里：几乎总是 core.bare 被置为 true
  if [ "$(git rev-parse --is-bare-repository 2>/dev/null)" = "true" ]; then
    warn "检测到仓库被标记为 bare（core.bare=true）——主目录无法切分支/部署，已自动纠正"
    git config core.bare false \
      || fail "自动纠正 core.bare 失败，请手动执行: git config core.bare false"
    if [ "$(git rev-parse --is-inside-work-tree 2>/dev/null)" != "true" ]; then
      fail "纠正 core.bare 后仍非工作树，请检查 $(git rev-parse --git-dir 2>/dev/null)/config"
    fi
    dim "已恢复为工作树，继续"
  else
    fail "当前目录不是 git 工作树，无法执行部署相关操作"
  fi
}

# require_deploy_branch: 部署（拉取最新代码）前的分支与干净树校验。
#   - LQ_DEPLOY_BRANCH 覆盖期望分支（默认 main；设为空串则跳过分支校验）
#   - LQ_ALLOW_DIRTY=1 跳过干净树校验
require_deploy_branch() {
  local want="${LQ_DEPLOY_BRANCH-main}"
  if [ -n "$want" ]; then
    local cur
    cur="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    if [ "$cur" != "$want" ]; then
      if [ -z "$cur" ] || [ "$cur" = "HEAD" ]; then
        fail "当前处于游离/未出生 HEAD（detached/unborn），部署要求 '$want' 分支"
      fi
      fail "当前分支为 '$cur'，部署要求 '$want'（如确需其他分支，设 LQ_DEPLOY_BRANCH=<分支>）"
    fi
  fi

  if [ "${LQ_ALLOW_DIRTY-0}" = "1" ]; then
    dim "LQ_ALLOW_DIRTY=1，跳过工作树干净校验"
    return 0
  fi

  # 已跟踪的改动会挡住 --ff-only 快进
  if ! git diff --quiet --ignore-submodules HEAD 2>/dev/null; then
    fail "工作树有未提交改动，git pull --ff-only 可能失败——请先提交/暂存，或设 LQ_ALLOW_DIRTY=1 强制继续"
  fi

  # 未跟踪文件：若被本次拉取的提交覆盖，pull 同样会失败（且报错难懂），提前拦下。
  # 注意：不能用 `| head -1` 取首行——head 提前关管道会让 git 收到 SIGPIPE(141)，
  # 在 set -o pipefail 下会直接中断整个脚本，反而看不到下面这条提示。
  local untracked
  untracked="$(git ls-files --others --exclude-standard 2>/dev/null)"
  if [ -n "$untracked" ]; then
    fail "工作树有未跟踪文件（如 '${untracked%%$'\n'*}'），可能被 pull 覆盖——请先清理/提交，或设 LQ_ALLOW_DIRTY=1 强制继续"
  fi
}
