---
name: dev-workflow
description: 全自动功能开发闭环——新 worktree 同步 origin/main，开发、测试、提交、推送、建 PR、盯 CI、处理 review 意见，迭代直到合入。用法：/dev <功能描述>
---

# /dev 全自动开发闭环

输入 `$ARGUMENTS` 为功能描述。若为空，先向用户询问要开发什么。

严格按以下阶段执行，禁止跳过任何阶段。

## 阶段 0：准备 worktree

1. `git fetch origin main`
2. 创建隔离 worktree：优先用 EnterWorktree 工具（name 取功能的英文 kebab-case，如 `worktree-<name>`）。
   - 若无法用 EnterWorktree，fallback：`git worktree add .claude/worktrees/<name> -b worktree-<name> origin/main`
3. **基线必须是 origin/main 最新提交**，不是本地 main。
4. 若项目有依赖/虚拟环境要求（uv venv 等），在新 worktree 里先装好环境。

## 阶段 1：实现功能

1. 用 superpowers:brainstorming 澄清需求（仅当描述含糊时），然后 superpowers:writing-plans 或直接 TDD。
2. 遵循 superpowers:test-driven-development：先写测试再实现。
3. 遵循全局规则：小文件、不可变模式、显式错误处理、输入校验。
4. 注意已知坑：ruff 全仓有基线违规，pre-commit 会被挡——提交时用 `git commit --no-verify`，但**必须对自己改动过的文件逐个跑 `ruff check`（新文件零违规）**。

## 阶段 2：本地验证（必须全绿才推送）

### 2a. Commit 前 code review（强制）

每次 commit 之前，对本次待提交的 diff 跑 code review 并**修复问题后再提交**：

1. 派发 code-reviewer 子代理（如 everything-claude-code:code-reviewer 或 oh-my-claudecode:code-reviewer）审查 `git diff`（新增文件用完整内容审查）。
2. 修复 CRITICAL / HIGH 问题后再进入提交；MEDIUM 尽量修。
3. 修复本身可能引入新问题——修复后若改动较大，再跑一轮 review 直至收敛（最多 3 轮）。
4. 后续每轮迭代 commit 前同样执行本步骤。

### 2b. 测试与 lint

按序执行，任何一步失败则修复后重跑：

```bash
make test               # pytest（推送前至少跑一次全量）
ruff check <改动文件>    # 仅改动文件
cargo test --workspace  # 若改了 crates/ 下 Rust 代码（在 crates/ 目录内执行）
```

## 阶段 3：提交、推送、建 PR

1. 提交信息遵循 conventional commits：`feat: ...` / `fix: ...` 等，不加 AI 署名。
2. **push 前先 rebase**：`git fetch origin main && git rebase origin/main`；有冲突则逐个解决，解决后重跑阶段 2 验证全绿。
3. `git push -u origin worktree-<name>`
3. `gh pr create --base main`，PR 描述包含：背景 / 改动清单 / 测试计划。
4. 若仓库支持，开启自动合并：`gh pr merge --squash --auto`（失败则跳过，进入盯梢模式）。

## 阶段 4：盯 CI 与 review，迭代直到合入

循环执行（每轮之间 sleep 60~120s 或 ScheduleWakeup 轮询）：

1. **CI**：`gh pr checks <PR号>`。有失败 → 读日志（`gh run view --log-failed`）→ 本地复现修复 → commit + push → 回到步骤 1。
2. **Review 意见**：`gh pr view <PR号> --json reviews,comments` 及
   `gh api repos/{owner}/{repo}/pulls/<PR号>/comments`。逐条处理：
   - 意见合理 → 修改代码、回复说明、push。
   - 意见不合理 → 技术上有据地回复解释，不盲从。
3. **收敛条件**（满足任一即结束循环）：
   - `gh pr view --json state` 返回 `MERGED` → 报告成功，进入阶段 5。
   - `CLOSED` → 向用户报告原因，结束。
   - 循环超过 20 轮仍无进展 → 停下向用户汇报。

## 阶段 5：收尾

- 合入后：删除远端分支 `git push origin --delete worktree-<name>`，移除本地 worktree。
- 若有值得沉淀的坑，更新项目记忆（memory / notepad）。
