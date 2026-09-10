# Claude Code Provider 接入 ask ai — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 `claude_code` provider：无头 Claude Code CLI 子进程驱动 ask ai，agent 全自主（CLI + skill），运行于独立工作区，经内置 stdio MCP server 获取行情/因子数据。

**Architecture:** 沿用 AgentService ABC + 事件总线 + WS 推流；新增 provider 实现；stream-json 逐行解析映射为现有 AgentEvent；--resume 多轮；工作区脚手架 CLAUDE.md / skills / mcp.json。

**Tech Stack:** Python 3.12 / asyncio subprocess / 手写 stdio JSON-RPC（无新依赖）/ pytest / aiosqlite

**Spec:** docs/superpowers/specs/2026-09-11-claude-code-provider-design.md

## Global Constraints

- 事件协议不变：assistant_delta|tool_call|tool_result|done|error
- SessionStore（data/ask.db）为事实源；落库仿 mock.py
- 不新增 Python 依赖
- 测试命令：pytest tests/unit -x -q
- 错误提示一律中文

---
### Task 1: AgentConfig 新字段

**Files:** Modify src/lquant/core/config.py；Test tests/unit/test_agent_config.py
**Produces:** AgentConfig(provider, claude_path="claude", workspace_dir="data/agent_workspace", timeout_seconds=300)；get_settings() 从 app.yaml agent: 段读取
**测试意图:** 默认值断言；timeout_seconds 非整数报 ValidationError
**验收:** pytest tests/unit/test_agent_config.py -v 全绿

### Task 2: 运行时工作区脚手架

**Files:** Create src/lquant/agent/workspace.py；Test tests/unit/test_agent_workspace.py
**Produces:** ensure_workspace(workspace_dir: str, root: Path) -> Path：幂等创建工作区；生成 CLAUDE.md（角色 + 数据访问优先级 MCP→CLI→临时脚本 + 仓库根只读声明）；.claude/skills/（复制 config/skills/，每次同步）；mcp.json（lquant server：python -m lquant.agent.mcp_server，env 带 LQ_ROOT、PYTHONPATH=root/src）
**测试意图:** 创建幂等；mcp.json 结构断言；skills 被复制

### Task 3: 内置只读 MCP server

**Files:** Create src/lquant/agent/mcp_server.py；Test tests/unit/test_mcp_server.py
**Produces:** main() 入口（stdio 换行分隔 JSON-RPC）；handle_request(req)->resp；工具 get_quotes(symbols)、get_daily(symbol,days=60)、list_factors()、get_factor_values(symbol,names,days=60)
**Consumes:** fetch_quotes（lquant.market.ticks）、read_daily（lquant.data.store.parquet）、list_builtin/compute（lquant.factors.qlib_alpha）
**测试意图:** initialize 返回 capabilities+serverInfo；tools/list 含 4 工具带 inputSchema；未知工具 -32602；get_quotes 打桩断言 content[0].text

### Task 4: stream-json 事件解析器

**Files:** Create src/lquant/agent/claude_json.py；Test tests/unit/test_claude_json.py
**Produces:** parse_stream_line(line: str) -> list[dict]，元素 {"kind": delta|tool_call|tool_result|done|error, "text","name","args","summary","session_id"}；坏行/未知类型 → []
**测试意图:** assistant text→delta；tool_use→tool_call；user(tool_result)→tool_result；result success→done+session_id；result 非 success→error；坏行→[]；system/未知→[]

### Task 5: ClaudeCodeAgentService

**Files:** Create src/lquant/agent/claude_code.py；Modify src/lquant/agent/sessions.py（ask_sessions 加 claude_session_id 列 + ALTER 迁移）、src/lquant/agent/service.py（工厂分支）；Test tests/unit/test_ask_agent_claude.py
**Produces:** ClaudeCodeAgentService(store, claude_path=None, claude_args=None, workspace_dir=None, root=None, timeout_seconds=None)，实现 AgentService 全接口
**关键行为:** _run：读 claude_session_id → 拼 CLI 命令（-p content、stream-json、--verbose、--dangerously-skip-permissions、--append-system-prompt、--mcp-config、--resume 可选）→ cwd=工作区起子进程 → 逐行 parse_stream_line → delta 落库+事件、tool_call/tool_result 事件、done 收尾；轮内超时检查→terminate→error“执行超时”；cancel=task.cancel+proc.terminate；claude_args 追加在命令末尾（测试注入 fake 脚本路径）
**测试意图:** fake claude python 脚本（固定 stream-json 输出）驱动：事件齐全 done 收尾、assistant 落库、claude_session_id 落库、第二轮带 --resume、cancel“已中断”、超时“执行超时”、CLI 缺失中文 error

### Task 6: 真实 CLI 冒烟验证

**Files:** 无新文件（验证任务）
**Steps:** 确认 claude --version 可用；临时把 config/app.yaml agent.provider 设为 claude_code 启动服务，建会话提问（含 symbol 上下文），验证 WS 事件流与落库；验证后还原配置
**验收:** /ask 页面真实对话一轮成功，事件流与 mock 版一致
