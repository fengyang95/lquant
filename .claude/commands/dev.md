---
description: 全自动开发闭环：worktree → 开发 → 测试 → review → PR → 盯 CI/review → 合入
argument-hint: <功能描述>
allowed-tools: Skill, Agent, Bash, Read, Write, Edit, Glob, Grep, TaskCreate, TaskUpdate, TaskList, TaskGet, WebFetch, WebSearch, AskUserQuestion, EnterWorktree, ExitWorktree, ScheduleWakeup, SendMessage, CronCreate, CronDelete, CronList, TaskOutput, TaskStop, NotebookEdit, LSP, mcp__plugin_oh-my-claudecode_t__*, mcp__tools__*
---

按 `.claude/skills/dev-workflow/SKILL.md` 的完整流程执行功能开发闭环。

功能描述：$ARGUMENTS

执行要点：
1. 若功能描述为空，先向用户确认需求。
2. 严格走 skill 的阶段 0–5：worktree（基于 origin/main 最新）→ TDD 实现 → commit 前 code review 并修复 → 本地测试/lint 全绿 → push + `gh pr create` → 盯 CI 与 review 意见并迭代 → 合入后清理分支与 worktree。
3. 全自动处理 CI 失败与 review 意见，直到 PR 状态为 MERGED 或触发收敛/汇报条件。CI 因 GitHub Actions 账单问题未启动时（job 秒挂 + "payments have failed" 注释），按 skill 阶段 4 的本地等价复跑流程验证后再合入。
