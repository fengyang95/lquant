# 模型接入（问 AI / A2A）

「问 AI」和 A2A 端点背后都是**同一个无头 CLI 执行体**，具体是哪一个由
`agent.provider` 决定（默认 `claude_code`）。两个真后端（Claude Code / Codex）
都遵守同一条纪律：

> **平台侧零模型配置。** 想换模型，就用 CLI 自己的方式换。

`config/app.yaml` 里刻意没有 `model` / `base_url` / `api_key` / `env` 这些字段。

## 为什么这么设计

- 只有一处事实源。平台再包一层模型配置，就会出现「平台说 A 模型、CLI 实际用 B 模型」
  这种查不出来的偏差。
- CLI 已经覆盖了换模型的所有姿势（原生、第三方兼容网关、指定模型名），而且是跟着
  CLI 升级走的，不用平台追着同步。
- 两个 provider 共用同一套子进程骨架（`agent/cli_agent.py`），
  **环境变量按原样继承**（`dict(os.environ)`），所以 CLI 读得到自己的配置。

## provider 一览

| `agent.provider` | 行为 | 模型配置在哪 | 何时用 |
|---|---|---|---|
| `claude_code`（默认） | 起 `claude -p --output-format stream-json` 子进程，真 LLM | `ANTHROPIC_*` 环境变量 / `claude` 自己的 settings | 正常使用 |
| `codex` | 起 `codex exec --json` 子进程，真 LLM | `~/.codex/config.toml`（含自定义 provider）+ `CODEX_HOME` | 想用 Codex 后端 |
| `mock` | **脚本化演示，不调 LLM**：调真实行情接口拼一段固定格式回答 | 无 | 没装任何 CLI、只想验证链路 |

`mock` 的回答会带 `（Mock 回答 | provider=mock，非 LLM 生成）` 前缀。看到这个前缀就说明
**当前不是真 CLI 在回答**（改 `config/app.yaml` 的 `agent.provider` 或
`LQ_AGENT_PROVIDER=...`，也可以直接改 `POST /api/settings`）。

## Claude Code 侧

### 三种常见姿势

```bash
# 1. 原生 Anthropic
export ANTHROPIC_API_KEY=sk-ant-...
# 或者用一次 `claude login`（凭据由 CLI 自己保存）

# 2. 第三方兼容网关
export ANTHROPIC_BASE_URL=https://your-gateway.example.com
export ANTHROPIC_AUTH_TOKEN=<网关签发的 token>

# 3. 指定模型名
export ANTHROPIC_MODEL=claude-sonnet-4-5
```

或者写进 Claude Code 自己的配置（`~/.claude/settings.json` 等，以 `claude` CLI 文档为准）。

角色与数据访问口径通过 `--append-system-prompt` + 工作区 `CLAUDE.md` 下发。

## Codex 侧

### 配置在哪

Codex 全部配置都读 `$CODEX_HOME`（默认 `~/.codex`）下的 `config.toml`。
**平台不写这个文件**，也不覆盖任何模型相关项 —— 只做进程内的命令行覆盖（`-c`），
仅用于注入 MCP server（见下）。

```toml
# ~/.codex/config.toml（示例：第三方兼容网关）
model = "your-model"
model_provider = "your-gateway"

[model_providers.your-gateway]
name = "Your Gateway"
base_url = "https://your-gateway.example.com/v1"
wire_api = "responses"
experimental_bearer_token = "..."
```

> 这个文件含凭据，注意它**不在本仓库的 `.gitignore` 覆盖范围内**（它在用户家目录）。

### 与 Claude Code 的几处**实质差异**

这几条都是实机取证（codex-cli 0.159.3）得来的，会直接影响到行为：

| 维度 | Claude Code | Codex |
|---|---|---|
| 角色/口径下发 | `--append-system-prompt` + `CLAUDE.md` | **只有 `AGENTS.md`**（没有等价旗标） |
| 续聊 | `--resume <session_id>` | `codex exec … resume <thread_id> <prompt>` |
| 审批/沙箱 | `--dangerously-skip-permissions` | `--dangerously-bypass-approvals-and-sandbox` |
| MCP 注入 | 工作区 `.claude/mcp.json`（`--mcp-config`） | `-c mcp_servers.<name>.*` |
| 正文粒度 | 逐块 `assistant` 消息 | **整段** `item.completed/agent_message`（无 token 级增量） |
| 会话落盘 | CLI 自己管理 | 写到 `$CODEX_HOME/sessions/`（`resume` 依赖它） |

因此工作区里 **`CLAUDE.md` 与 `AGENTS.md` 内容一致、各写一份**
（`agent/workspace.py` 的 `_write_role_docs`），MCP server 规格也只有一份
（`mcp_server_spec`），claude 侧渲染成 `mcp.json`、codex 侧渲染成 `-c` 覆盖项。

> ⚠️ **MCP 工具在 Codex 下必须 bypass 审批**：默认（含 `-s read-only`）审批策略是
> `never`，而 MCP 调用被标记为需要审批 → 直接报
> `MCP tool call requires approval, but approval policy is never`，工具一个都调不动。
> 所以 `agent.skip_permissions=true` 时用
> `--dangerously-bypass-approvals-and-sandbox`；关掉则退到 `-s workspace-write`，
> 此时 MCP 工具不可用（只能靠 shell/HTTP 取数）。

## 生效方式

两个 provider 都用 `posix_spawn` 起子进程，环境变量按**原样继承**，
所以上面导出的变量会被 CLI 直接读到。

- **UI 启动**：`./lquant.sh start` 之前先 `export`，或写进项目根的 `.env`
  （`lquant.sh` / CLI 会加载 `.env`；注意 `.env` 已在 `.gitignore` 里）。
- **自检查询**：看 `data/logs/api.log` 里 CLI 子进程的 **stderr** ——
  认证/模型名/网关问题的报错都在那里（日志前缀是 `[claude stderr]` / `[codex stderr]`）。

## 相关开关

```yaml
# config/app.yaml
agent:
  provider: ${LQ_AGENT_PROVIDER:claude_code}   # claude_code | codex | mock
  claude_path: ${LQ_CLAUDE_PATH:claude}        # 非标准安装路径时指定可执行文件
  codex_path: ${LQ_CODEX_PATH:codex}
  workspace_dir: ${LQ_AGENT_WORKSPACE:data/agent_workspace}
  timeout_seconds: ${LQ_AGENT_TIMEOUT:300}     # 单次回答超时（超时 kill 子进程）
  skip_permissions: ${LQ_AGENT_SKIP_PERMISSIONS:true}
```

`skip_permissions` 等于给 CLI 全自主权限（无头模式必须，否则会挂在/被拒掉），
边界与风险见 `docs/SECURITY.md`。
