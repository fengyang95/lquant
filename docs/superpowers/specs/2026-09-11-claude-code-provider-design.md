# Ask AI — Claude Code Provider 设计

- 日期：2026-09-11
- 状态：已确认（brainstorming 产出）
- 前置：`docs/superpowers/specs/2026-09-10-ask-ai-design.md`（mock 版已落地）

## 1. 目标

将 ask ai 模块的 agent 实现从 mock 升级为真实接入 Claude Code CLI（无头模式），provider 名 `claude_code`。agent 以全自主方式工作：可执行 Bash / python / `lquant` CLI、读写文件，通过 skill 学会方法论，通过内置 MCP server 获取行情/因子数据。

**适用边界**：全自主权限 + 本地单人环境使用，勿在共享/多用户环境开启。

## 2. 非目标（YAGNI）

- 不做回测/数据任务的 agent 触发集成（二期）。
- 不做多用户并发限流、RQ 队列化（沿用现有 `asyncio.create_task` 模式）。
- 不改前端事件协议——现有 `AgentEvent` 流（`assistant_delta` / `tool_call` / `tool_result` / `done` / `error`）原样复用。

## 3. 架构

```
web /ask ──POST /ask/sessions/{sid}/messages (202)──► AskApi
   ▲                                                    │
   │ WS /ws/ask/{sid} ◄── AskEventBus ◄── ClaudeCodeAgentService
                                                        │
                                    asyncio.create_subprocess_exec(
                                      claude, -p, prompt,
                                      --output-format stream-json --verbose
                                      --dangerously-skip-permissions
                                      --append-system-prompt <角色提示>
                                      --mcp-config <workspace>/mcp.json)
                                    cwd = <workspace>（独立运行时目录）
```

## 4. 组件

### 4.1 `ClaudeCodeAgentService`（`src/lquant/agent/claude_code.py`）

实现现有 `AgentService` ABC（service.py:14-34），在工厂 `get_agent_service()` 注册 `claude_code` 分支。

- `send_message` 后台任务：以 stream-json 逐行读子进程 stdout，映射事件：
  - assistant 文本块 → `assistant_delta`
  - `tool_use` → `tool_call`；`tool_result` → `tool_result`
  - 进程正常结束 → `done`；非零退出/解析致命错误 → `error`（用户友好中文提示）
  - 未知事件类型：记日志、忽略（宽容解析，防 CLI 版本漂移）
- **多轮会话**：取 stream-json 结果里的 claude session id，映射 `ask_sessions` 表（新列 `claude_session_id`），下轮用 `--resume`。拿不到 session id 时降级为重放历史消息。
- **取消**：`cancel` 实现 kill 进程（复用现有 `/cancel` 端点语义）。
- **超时**：可配置（默认 5 分钟），超时 kill 并发 `error` 事件。

### 4.2 运行时工作区

- `AgentConfig.workspace_dir`（默认 `data/agent_workspace`，支持环境变量插值）。
- 首次使用时脚手架化：
  - 生成 `CLAUDE.md`：agent 角色说明 + lquant 数据访问指南（MCP 工具清单、lquant CLI 用法、仓库绝对路径只读参考）
  - 将 `config/skills/` 下 skill 复制到 `<workspace>/.claude/skills/`（claude 原生加载）
  - 生成 `mcp.json`（MCP server 配置）
- 工作区随会话持久化，agent 跨轮保留中间产物（脚本、导出文件等）。

### 4.3 内置只读 MCP server（`src/lquant/agent/mcp_server.py`）

- stdio MCP server，暴露只读数据工具，内部直调 `lquant.data` / `lquant.factors` 现有函数：
  - `get_quotes(symbols)` — 实时/最新行情快照
  - `get_kline(symbol, period, limit)` — K 线
  - `list_factors()` — 因子清单
  - `get_factor_values(...)` — 因子值查询
- 定位：与 skill 互补——skill 教方法论，MCP 给快捷数据接口（省 token、免写临时脚本）。
- 实现用 `mcp` python 包（新增依赖），如不可行则手写 stdio JSON-RPC。

### 4.4 配置（`src/lquant/core/config.py` + `settings_store.py`）

`AgentConfig` 新增字段（均支持 YAML + 环境变量插值）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `provider` | `mock` | 已有；新增可选值 `claude_code` |
| `claude_path` | `claude` | CLI 可执行文件路径 |
| `workspace_dir` | `data/agent_workspace` | 运行时工作区 |
| `timeout_seconds` | `300` | 单轮超时 |

## 5. 错误处理

- CLI 不存在/未登录：快速失败，`error` 事件给中文提示（"未找到 claude CLI，请安装并登录"）。
- 超时：kill 进程 + `error` 事件。
- stdout 单行 JSON 解析失败：记日志、跳过，不中断。
- 进程非零退出：取 stderr 摘要入 `error` 事件。

## 6. 测试

- Fake claude 脚本（输出固定 stream-json 行）做集成测试：事件映射、done/error 路径、cancel（kill）、CLI 缺失报错、resume 降级。
- MCP 工具单测直接调 handler。
- 工作区脚手架单测（重复创建幂等）。
- 遵循 TDD；存量 mock 行为不得回归。

## 7. 风险

| 风险 | 缓解 |
|---|---|
| 全自主权限可改/删文件 | 工作区隔离（cwd 指向 workspace）；仅限本地单人环境 |
| stream-json 格式随 CLI 版本变化 | 宽容解析（未知忽略）；集成测试锁最小字段集 |
| CLI 未安装/未登录 | 启动即检测，中文 error 提示 |
| `mcp` 包依赖冲突 | 备选手写 stdio JSON-RPC |
