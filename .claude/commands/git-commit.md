---
description: code review + 修复 + 测试全绿后提交当前改动
allowed-tools: Skill, Agent, Bash, Read, Edit, Write, Glob, Grep
---

按顺序执行：

1. **Code review（强制）**：对 `git diff`（含新增未跟踪文件）派发 code-reviewer 子代理审查。
2. 修复全部 CRITICAL / HIGH 问题，MEDIUM 尽量修；修复后若改动大则再跑一轮 review（最多 3 轮）。
3. **测试**：`make test`；若改了 Rust 代码，在 `crates/` 内 `cargo test --workspace`。
4. **Lint**：`ruff check <改动文件>`（新文件必须零违规；全仓基线违规不必处理）。
5. 全绿后提交：conventional commits 格式（`feat:`/`fix:`/`refactor:` 等），不加 AI 署名；pre-commit 被全仓 ruff 基线挡住时用 `--no-verify`，但 3、4 步不可省略。
6. 不自动 push（如需 push + PR 用 /git-pr）。
