# 问 AI（Ask AI）功能设计

日期：2026-09-10
状态：待评审

## 背景与目标

lquant 研报台增加"问 AI"能力：用户用自然语言提问，AI 回答行情查询与市场分析问题。

- **本 worktree 范围**：前后端功能流程——会话管理、流式交互、内置 `a-stock-data` skill、API 补齐，以及一个 **MockAgentService** 让全流程可本地跑通。
- **不在本 worktree 范围**：真正接入 claude code 的实现（其他 worktree 负责）。接入方只需实现本设计定义的 `AgentService` 接口并产生相同的事件流。

## 总体架构

采用 Provider 模式：`AgentService` 抽象接口 + 配置切换实现。

```
src/lquant/agent/
├── __init__.py
├── service.py        # AgentService 抽象 + get_agent_service() 工厂
├── mock.py           # MockAgentService：脚本化回复 + 调真实数据 API
├── sessions.py       # SQLite 会话/消息持久化（aiosqlite）
├── schemas.py        # pydantic: Session, Message, AgentEvent
└── errors.py
src/lquant/server/api/ask.py         # /api/ask 路由
src/lquant/server/ws.py              # 扩展 /ws/ask/{session_id}
config/skills/a-stock-data/SKILL.md  # 内置 skill 文档
```

配置项：`settings.agent.provider`（`mock` | 后续 `claude_code`），工厂按值选择实现。默认 `mock`。

## AgentService 接口契约

```python
class AgentService(ABC):
    async def create_session(self, context: dict | None) -> Session: ...
    async def list_sessions(self) -> list[Session]: ...
    async def delete_session(self, session_id: str) -> None: ...
    async def get_messages(self, session_id: str) -> list[Message]: ...
    async def send_message(self, session_id: str, content: str) -> Message:
        """落库用户消息，启动 agent 后台任务，返回用户消息。"""
    async def cancel(self, session_id: str) -> None: ...
```

实现方（如 claude code）在后台任务中通过 `emit(event: AgentEvent)` 回调推送事件；事件同时经 WS 转发给前端。

## AgentEvent 流协议

前后端共用；所有实现（Mock 与未来 claude code）必须发出同一协议：

| type | 载荷 | 说明 |
|---|---|---|
| `assistant_delta` | `{text}` | 流式文本增量 |
| `tool_call` | `{name, args}` | agent 正在查询数据（前端渲染"正在查询行情…"折叠卡） |
| `tool_result` | `{name, summary}` | 只推摘要，不推原始大表 |
| `done` | `{message_id}` | 回答完成，完整 assistant 消息已落库 |
| `error` | `{message}` | 出错，前端显示重试按钮 |

## API 契约

- `POST /api/ask/sessions` — 创建会话，body 可含 `context: {symbol?, page?}`
- `GET /api/ask/sessions` — 会话列表
- `GET /api/ask/sessions/{id}/messages` — 历史消息
- `POST /api/ask/sessions/{id}/messages` — 发用户消息，返回 202 + 用户消息体，agent 后台运行
- `DELETE /api/ask/sessions/{id}` — 删除会话
- `POST /api/ask/sessions/{id}/cancel` — 中断当前回答
- `WS /ws/ask/{session_id}` — 订阅上述 AgentEvent 流

响应沿用现有 `envelope.py` 包装；错误走统一错误格式。

## 存储

SQLite（`data/ask.db`，aiosqlite，与行情 DuckDB 隔离）：

```sql
ask_sessions(id TEXT PK, title TEXT, context_json TEXT, created_at TEXT)
ask_messages(id TEXT PK, session_id TEXT FK, role TEXT,  -- user | assistant | system
             content TEXT, tool_calls_json TEXT, created_at TEXT)
```

无迁移工具依赖；建表语句启动时 `CREATE TABLE IF NOT EXISTS`。

## MockAgentService 行为

- 收到用户消息后：产生 1–2 个 `tool_call`（真调 `ticks.fetch_quotes` / `/api/market/batch` 等真实数据接口）→ `tool_result`（摘要）→ 将真实行情数据格式化为中文回答，以 `assistant_delta` 分段吐出 → `done`。
- 识别会话 `context.symbol` 时围绕该股票组织回答（"这只股票怎么样"类问题可答）。
- 不依赖任何 LLM；同时充当集成测试夹具。

## a-stock-data skill

**位置**：`config/skills/a-stock-data/SKILL.md`，纯文档随仓库分发。

**内容**：
- frontmatter：`name: a-stock-data`，`description: 查询 A 股行情/资金流/涨停/龙虎榜/ETF 数据并做分析`
- 能力清单 → API 映射表：

| 想知道 | 调用 |
|---|---|
| 个股/指数实时行情 | `GET /api/market/batch?symbols=...` |
| 大盘概览/涨跌家数 | `GET /api/market/overview`、`/api/market/breadth` |
| 板块/行业 | `GET /api/market/sectors` |
| 资金流 | `GET /api/market/money-flow` |
| 涨停池 | `GET /api/market/limit-up` |
| 龙虎榜 | 龙虎榜查询端点（缺失则补齐） |
| ETF | `GET /api/etf/*` |
| 历史K线/因子分析 | `GET /api/factors/*`、`/api/data/*` |

- **约定**：日期格式；复权口径说明；空数据/非交易日的应答方式；回答必须标注数据时点（不把实时快照说成历史）。
- **安全约定**：agent 只读——只允许 GET 数据端点，禁止 `/collect`、`/sync` 等写入/任务端点。API 层不做 agent 鉴权，靠 skill 约束并在此声明。

**API 补齐原则**：实现时逐一核对映射表端点是否真实存在、参数是否够用，缺失才补（预计需补龙虎榜查询、批量日K线查询），不做额外功能。

## 前端设计

新增 `/ask` 页面，风格沿用现有 Panel/PageHeader 与 design token：

```
web/src/app/ask/page.tsx        # 两栏：左会话列表 + 右聊天区
web/src/components/ask/
├── SessionList.tsx             # 会话列表、新建、删除
├── ChatWindow.tsx              # 消息流 + 输入框
├── Message.tsx                 # 单条消息（tool_call 折叠展示）
└── ContextChip.tsx             # 上下文标识（如"600519 贵州茅台"）
web/src/lib/ask-api.ts         # API client + WS 事件封装（可测试纯函数）
```

**交互流**：
1. 进入 `/ask` → `GET sessions` 渲染左栏；空态引导新建。
2. 选中会话 → `GET messages` 渲染历史 → 连 `WS /ws/ask/{id}`。
3. 发消息 → POST 202 → 乐观上屏用户消息 + assistant 打字态 → `tool_call` 显示折叠查询卡 → `assistant_delta` 逐字追加 → `done` 用完整消息替换 → `error` 显示重试按钮。
4. 断线自动重连 WS，重连后拉一次最新消息对账。
5. **上下文入口**：个股页 `/security/[symbol]`、dashboard 加"问 AI"按钮 → 跳 `/ask?symbol=600519`；`/ask` 检测 query 参数自动建会话并写 `context`。

## 错误处理

- Agent 后台任务异常 → 发 `error` 事件 + assistant 错误消息落库，前端可重试。
- WS 断连期间产生的事件不追补：重连后以落库消息为准对账（事件是提示性的，落库是事实源）。
- 会话不存在 / 消息空内容 → 400 明确报错文案。
- SQLite 写失败 → 接口返回 500，WS 侧发 `error` 事件。

## 测试

- **后端 unit**：sessions CRUD、AgentEvent 序列化、Mock agent 事件序列（tool_call → delta → done 顺序）。
- **后端 integration**：httpx ASGI + WS client，覆盖"创建会话 → 发消息 → 收事件流 → 消息落库 → 历史可回放"、"cancel"、"error 路径"。
- **前端**：ask-api.ts 单测（事件折叠、重连对账）、ChatWindow/Message 渲染测试，沿用现有 vitest 模式。

## 里程碑

1. 后端骨架：schemas、SQLite 持久化、AgentService 抽象 + Mock、`/api/ask` 路由（TDD）
2. 流式：WS `/ws/ask` + Mock 事件流打通
3. API 补齐（龙虎榜、批量日K）+ `SKILL.md`
4. 前端 `/ask` 页面 + 组件 + ask-api.ts
5. 上下文入口按钮（个股页、dashboard）
