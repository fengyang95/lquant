---
description: 同步当前 worktree 与 origin/main 最新
allowed-tools: Bash
---

将当前分支与 origin/main 最新同步：

1. `git fetch origin main`
2. 若当前分支是 main：`git pull --ff-only origin main` 后结束。
3. 否则 `git rebase origin/main`；若产生冲突，逐个文件解决冲突（保留双方意图），继续 rebase，全部解决后：
   - `ruff check <改动文件>` 确认 lint 无新增违规
   - `make test` 全量回归
   - 全绿后 `git push --force-with-lease`
4. 报告同步结果（领先/落后提交数）。
