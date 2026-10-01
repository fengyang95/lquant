# 模型接入（问 AI / A2A）

「问 AI」和 A2A 端点背后都是**同一个执行体**：内置的 Claude Code CLI
（`agent.provider = claude_code`，默认值）。所以**模型配置就是 Claude Code 自己的
模型配置** —— 平台不额外提供模型 provider / 端点 / 密钥的配置项，`config/app.yaml`
里也刻意没有 `model` / `base_url` / `api_key` 这些字段。

一句话：**想换模型，就用 Claude Code 的方式换。**

## 为什么这么设计

- 只有一处事实源。平台再包一层模型配置，就会出现「平台说 A 模型、CLI 实际用 B 模型」
  这种查不出来的偏差。
- Claude Code 已经覆盖了换模型的所有姿势（原生 Anthropic、第三方兼容网关、指定模型名），
  而且是跟着 CLI 升级走的，不用平台追着同步。

## 三种常见姿势

### 1. 原生 Anthropic

```bash
export ANTHROPIC_API_KEY=sk-ant-...
# 或者用一次 `claude login`（凭据由 CLI 自己保存）
```

### 2. 第三方兼容网关

```bash
export ANTHROPIC_BASE_URL=https://your-gateway.example.com
export ANTHROPIC_AUTH_TOKEN=<网关签发的 token>
```

### 3. 指定模型名

```bash
export ANTHROPIC_MODEL=claude-sonnet-4-5
```

或者写进 Claude Code 自己的配置（`~/.claude/settings.json` 等，以 `claude` CLI 文档为准）。

## 生效方式

`ClaudeCodeAgentService` 用 `posix_spawn` 起子进程，环境变量按**原样继承**
（`dict(os.environ)`），所以上面导出的 `ANTHROPIC_*` 会被 CLI 直接读到。

- **UI 启动**：`./lquant.sh start` 之前先 `export`，或写进项目根的 `.env`
  （`lquant.sh` / CLI 会加载 `.env`；注意 `.env` 已在 `.gitignore` 里）。
- **自检查询**：`POST /api/ask/sessions/{sid}/messages` 后看 `data/logs/api.log` 里
  claude 子进程的 stderr —— 认证/模型名问题的报错都在那里。

## 和 provider 的关系

| `agent.provider` | 行为 | 何时用 |
|---|---|---|
| `claude_code`（默认） | 起 `claude -p --output-format stream-json` 子进程，真 LLM | 正常使用 |
| `mock` | **脚本化演示，不调 LLM**：调真实行情接口拼一段固定格式回答 | 没装 claude CLI、只想验证链路 |

`mock` 的回答会带 `（Mock 回答 | provider=mock，非 LLM 生成）` 前缀。看到这个前缀就说明
**当前不是 Claude Code 在回答**（改 `config/app.yaml` 的 `agent.provider` 或
`LQ_AGENT_PROVIDER=claude_code`，也可以直接改 `POST /api/settings`）。

## 相关开关

```yaml
# config/app.yaml
agent:
  provider: ${LQ_AGENT_PROVIDER:claude_code}
  claude_path: ${LQ_CLAUDE_PATH:claude}      # 非标准安装路径时指定可执行文件
  workspace_dir: ${LQ_AGENT_WORKSPACE:data/agent_workspace}
  timeout_seconds: ${LQ_AGENT_TIMEOUT:300}   # 单次回答超时（超时 kill 子进程）
  skip_permissions: ${LQ_AGENT_SKIP_PERMISSIONS:true}
```

`skip_permissions` 会传 `--dangerously-skip-permissions`（无头模式必须，否则挂在交互授权上），
等于给 CLI 全自主权限 —— 边界与风险见 `docs/SECURITY.md`。
