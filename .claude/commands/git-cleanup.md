---
description: 清理已合入的远端分支与本地 worktree
argument-hint: <分支名或 worktree 名>
allowed-tools: Bash
---

对 `$ARGUMENTS` 指定的分支（缺省为当前分支）：

1. 确认对应 PR 已 MERGED（`gh pr view <分支> --json state`），未合入则拒绝并提示。
2. `git push origin --delete <分支>` 删除远端分支。
3. 若在 worktree 内，从主 checkout 执行 `git worktree remove <路径>`（有未提交改动时提示确认）。
4. 报告清理结果。
