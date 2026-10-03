# 安全边界

lquant 是**单机单人研究工具**，不是多租户服务。请先把这句话读完再决定怎么部署。

> 本文讲**运行时**安全边界：谁能访问这个 API，谁就能在这台机器上执行代码。
> 仓库内容不要泄露（密钥扫描门禁、公开仓库前的体检与补救手册）是另一件事，
> 见 [`SECRET_HYGIENE.md`](SECRET_HYGIENE.md)。

## 一句话风险

API 无鉴权，且以下端点会 **exec 用户提供的 Python 代码**：

| 端点 | 入口 | 说明 |
|---|---|---|
| `POST /api/backtests/run-code` | `backtest/jqapi.py` `JQRunner` | 聚宽方言策略代码 |
| `POST /api/analyses`、`PUT /api/analyses/{id}` | `backtest/analysis.py` | 自定义分析片段（保存前冒烟即执行） |
| `POST /api/strategies`、`PUT /api/strategies/{id}` | `backtest/strategy_store.py` | 策略源码保存前校验 |

另外，当 `agent.provider` 是 `claude_code`（**默认值**）或 `codex` 时，问 AI 会拉起
对应的 CLI 子进程，并在 `skip_permissions=true`（默认）下带全自主权限：

| provider | 全自主旗标 | 效果 |
|---|---|---|
| `claude_code` | `--dangerously-skip-permissions` | 不弹授权，可任意读写本机 |
| `codex` | `--dangerously-bypass-approvals-and-sandbox` | 跳过审批**并关掉沙箱**，同上 |

> codex 侧还有一层：审批策略不 bypass 时，**MCP 工具调用会被直接拒绝**
> （`MCP tool call requires approval, but approval policy is never`），
> 即 `skip_permissions=false` 时 MCP 取数不可用。详见 `docs/AGENT_MODEL.md`。

**结论：谁能访问这个 API，谁就能在这台机器上执行代码。**

### 权限与并发的两个可调旋钮（2026-10）

`skip_permissions` 现在是**两层**的：全局默认值（`app_setting.agent.skip_permissions`，
可在「问 AI → ⚙ AI 设置 → 运行参数」改）+ **会话级覆盖**
（`PATCH /api/ask/sessions/{sid}/config` 的 `skip_permissions`，可在会话头
「调整能力」里改）。会话级取值的三态是刻意设计的：

| 值 | 含义 |
|---|---|
| `null`（不传 / 选「跟随默认」） | 跟随全局默认档 |
| `true` | 本会话明确开启全自主权限 |
| `false` | 本会话明确关闭 |

`null` 与 `false` **不是一回事**：前者随时跟着全局值变，后者是一道钉死的闸。
所以「开一个只问答的会话」应该用 `false`，而不是把全局值调低（那会影响别的会话）。

另外新增了全局并发上限 `agent.max_concurrent_runs`（默认 4，1~64）。
每个回答都是一个**带全自主权限的 CLI 子进程**，此前没有任何上限：一次多会话齐发
就能在本机拉起几十个进程。超限的新请求直接拒绝（429，**不排队**），
`GET /api/ask/runs` 能看到当前占用（provider / 已运行时长 / pid / 工作区）
并逐个终止。这条闸门是**可用性**保护，不是安全边界 —— 它不改变「谁能调 API
谁就能执行代码」这条结论。

## 三层收口（都不是容器级隔离）

1. **静态校验** —— `backtest/validation.py` `validate_source`
   import 白名单 + 危险调用黑名单（`__import__`/`eval`/`exec`/`compile`/`open`/`getattr`…）
   + 危险属性黑名单（`__class__`/`__subclasses__`/`__globals__`…）。
   调用点：`/api/backtests/run-code`、`/api/analyses*`、`/api/strategies*`。

2. **执行侧受限内建** —— `backtest/sandbox.py` `safe_builtins()`
   给 `exec` 的命名空间**显式**设 `__builtins__`。
   不设的话 CPython 会自动注入完整内建，第 1 层白名单直接失效
   （`__import__("os")` 不是 `import` 语句，AST 白名单看不见它）。

3. **默认只绑回环** —— `lquant.sh` 的 `LQ_API_HOST`，默认 `127.0.0.1`。
   设成 `0.0.0.0` 会打印告警。

### 三层都挡不住什么

exec 语义下，只要能触达任意对象，对象图遍历逃逸
（`().__class__.__bases__[0].__subclasses__()` 找 `os._wrap_close` 拿 `__globals__`）
在理论上始终可达。第 1、2 层是**抬高门槛**，不是证明安全。

要真正隔离，需要第四层：把用户代码丢进受限子进程
（`resource.setrlimit` 限 CPU/内存/进程数 + 禁网 + 超时强杀）。
本仓库已为 BaoStock 做过同构的 watchdog 子进程强杀（`data/watchdog.py`），可复用。

## A2A 端点（2026-10 新增）

`GET /.well-known/agent-card.json` + `POST /a2a`（JSON-RPC，支持 SSE 流式）。
它把上面那条结论从「本机进程」**扩大到「网络」**：A2A 客户端发一条消息，
等价于在你这台机器上跑一次 CLI 执行体（`agent.provider` 是哪个就是哪个，
`skip_permissions=true` 时带全自主权限）。

收口措施（都在默认配置里生效）：

1. **默认只绑回环** —— `LQ_API_HOST` 默认 `127.0.0.1`（`lquant.sh`）。A2A 不额外开监听。
2. **可选 Bearer 鉴权** —— 设 `LQ_A2A_TOKEN=<随机串>` 后，`POST /a2a` 必须带
   `Authorization: Bearer <token>`；Agent Card 里会随之声明 `securitySchemes`。
   **未设置时启动会打 warning**（"A2A 端点已开放且未配置 LQ_A2A_TOKEN…"），
   把「现在这个口子是敞的」明确写在日志里，而不是靠部署者记得。
3. **回给调用方的内容做了分级**：
   - `Task.history`（非流式的 `message/send` 与 `tasks/get`）**只回文本 part**，不回 tool 调用。
   - 流式 `message/stream` 会透出**过程数据**：回答正文之外，还有 `thinking` 推理、
     工具入参、工具结果全文、`system` 初始化事件 —— 挂在独立的 `{taskId}-trace`
     artifact 上（`metadata.kind` 标明类型），与 `-answer` 分开，只想要答案的消费方
     可以只读 `-answer`。工具进度仍在 `statusUpdate` 里带工具名与截断后的结果摘要。
   - **过程数据一律先过脱敏**（`src/lquant/agent/redact.py`，黑名单口径）：
     键名命中 `token/secret/password/api_key/credential/authorization/...` 的整棵子树打码；
     值里的常见凭据形态（`sk-*`、`ghp_*`、`AKIA*`、JWT、`Bearer ...`、`xox?-`）
     与**本机绝对路径**（`/Users`、`/home`、`/var`、`/tmp`…）替换为占位符。
     任意单帧文本超 64 KiB 会截断并置 `metadata.truncated`。
   - ⚠️ 脱敏是**黑名单**：安全性取决于名单覆盖是否完整。把 A2A 暴露给不受信调用方时，
     应假定「名单外的敏感字段可能原样外泄」；新增敏感键名要同步 `redact.py`。
4. **不对批量 JSON-RPC 请求做支持**（直接回 `-32600`），减少一次请求里混入多种操作的攻击面。

**仍然挡不住的**：A2A 会话与 web 的 `/ask` 共用同一份 `ask.db`，所以通过 A2A 提的问题
会出现在 `/ask` 页面（这是有意为之的可观测性，不是漏洞，但要知道别人问过什么你都看得到）。

需要暴露给外部 Agent 时的建议：

```bash
export LQ_A2A_TOKEN="$(openssl rand -hex 24)"
./lquant.sh start        # 仍绑回环；外部通过 SSH 隧道或反代 + HTTPS 进来
```

## 部署建议

- **不要**把 API 暴露到公网或不可信内网。默认配置已经只绑回环。
- 需要远程访问时，用 SSH 端口转发，**不要**直接 `LQ_API_HOST=0.0.0.0`：
  ```bash
  ssh -L 8000:127.0.0.1:8000 user@host
  ```
- 只用公开数据、不需要落盘/执行命令时，关掉 CLI 全自主权限：
  ```yaml
  # config/app.yaml
  agent:
    provider: claude_code      # 或 codex
    skip_permissions: false    # claude：不传 --dangerously-skip-permissions
                               # codex ：退到 -s workspace-write（代价：MCP 工具不可用）
  ```
- CORS 只允许 `http://localhost:3000`（见 `server/main.py`）。改动它等于放宽浏览器侧的
  跨站访问面，谨慎。

## 报告问题

这是个人研究项目，没有正式的漏洞响应流程。如果你发现可稳定利用的逃逸链，
请直接开 issue 并附最小复现。
