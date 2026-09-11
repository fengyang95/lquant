---
description: 推送并创建 PR，开启自动合并，盯 CI/review 直到合入
argument-hint: [PR 标题]
allowed-tools: Skill, Agent, Bash, Read, Edit, Write, Glob, Grep, ScheduleWakeup
---

1. **push 前先 rebase**：`git fetch origin main && git rebase origin/main`；有冲突则逐个解决，解决后重跑测试与 lint 全绿再 push；后续更新用 `git push --force-with-lease`。
2. `gh pr create --base main`，描述含背景 / 改动清单 / 测试计划。若分支已有 open PR 则跳过创建。
3. `gh pr merge --squash --auto` 开启自动合并（不支持则跳过）。
4. 循环盯梢（间隔 60~120s）：
   - `gh pr checks`：有失败 → `gh run view --log-failed` 定位 → 本地修复 → /git-commit 流程提交 push。
   - review 意见（`gh pr view --json reviews,comments` + `gh api repos/{owner}/{repo}/pulls/<N>/comments`）：逐条处理，合理的修改代码并回复，不合理的有据反驳。
   - PR 状态 `MERGED` → 报告成功；`CLOSED` → 报告原因。
   - 超过 20 轮无进展 → 停下汇报。
