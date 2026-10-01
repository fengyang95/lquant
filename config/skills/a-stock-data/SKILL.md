---
name: a-stock-data
description: 查询 A 股实时行情、大盘概览、板块、资金流、涨停池、龙虎榜、ETF 数据并做分析。回答必须标注数据时点。
tags: [a-stock, market-data, quotes, etf, money-flow, market-breadth]
---

# a-stock-data

用 lquant 本地 HTTP API 查 A 股数据，基址 **`${LQ_API_BASE}`**（脚手架同步进工作区时
会替换成实际地址）。所有端点均为只读 GET。

## 先看 MCP，再走 HTTP

下列 MCP 工具已覆盖了大部分查询，**优先用它们**（免写请求、免记参数）：

| MCP 工具 | 对应 HTTP |
|---|---|
| `get_quotes` | 实时行情（等价 `/api/market/batch` 的最新价部分） |
| `get_daily` | 日线（`/api/data/daily`） |
| `get_market_overview` | `/api/market/overview` |
| `get_market_breadth` | `/api/market/breadth` |
| `get_sectors` | `/api/market/sectors` |
| `get_money_flow` | `/api/market/money-flow` |
| `get_limit_up` | `/api/market/limit-up` |
| `get_dragon_tiger` | `/api/market/dragon-tiger` |
| `get_heat` | `/api/market/heat` |
| `get_index_quotes` | `/api/market/index` |
| `get_etf_list` | `/api/etf/meta` |

**只有 MCP 没覆盖的才走 HTTP**（下表的「走 HTTP」列标 ✔ 的那些）。

## 查什么用什么（HTTP 侧）

| 想知道 | 调用 | 走 HTTP |
|---|---|---|
| 大盘概览（情绪分/涨跌停/北向） | `GET /api/market/overview` | |
| 涨跌家数（市场宽度） | `GET /api/market/breadth` | |
| 板块/行业/概念/地域表现 | `GET /api/market/sectors?kind=industry\|concept\|area` | |
| 资金流（全市场 Top / 单票历史） | `GET /api/market/money-flow?top=20&symbol=600519` | |
| 涨停池 | `GET /api/market/limit-up` | |
| 龙虎榜（最新交易日） | `GET /api/market/dragon-tiger` | |
| 热榜（涨跌/放量/龙虎榜要点） | `GET /api/market/heat` | |
| 全市场日度截面（分页 + 排序） | `GET /api/market/snapshot?page=1&size=50&sort=change_pct` | ✔ |
| 批量对比（含等权净值曲线） | `GET /api/market/batch?symbols=600519,510300.SH&days=60` | ✔ |
| 采集器列表 / 健康度 | `GET /api/market/collectors`、`GET /api/market/collect-status` | ✔ |
| 数据覆盖度 / 标的搜索 / 历史K线 | `GET /api/data/coverage`、`/api/data/securities`、`/api/data/daily` | ✔ |
| ETF 元数据（跟踪指数/规模/T+N） | `GET /api/etf/meta?q=沪深300`、`GET /api/etf/by-symbol/510300.SH` | ✔ |
| ETF 相关性（两两日收益相关） | `GET /api/etf/correlation?...`（先看 `/api/etf/meta` 里的 symbol 口径） | ✔ |

## 约定

1. **只读**：仅允许 GET 数据端点。禁止 `POST /api/market/collect`、`/api/sync/*`、
   `/api/data/*` 里任何触发写入或任务的端点。
2. **数据时点**：回答中必须标注数据来源与时点（实时快照 / 截至 X 交易日）。
3. **空数据**：非交易日或数据缺失时如实说明「暂无数据」，**不编造数字**。
   注意端点读不到 DuckDB 表时会返回空结构而不是报错 —— 空不等于「市场真的没有」。
4. **复权口径**：历史价格默认不复权；如需复权口径请说明。
5. 股票代码 6 位数字（内部统一成 `600519.SH` 这类带后缀格式）；
   指数可用常见代码（如 000001 上证）。

## 回答结构建议

先给结论，再给数据依据（来源端点/工具 + 时点），最后可给简短分析。
数字必须来自工具/API 返回，不凭记忆编造。
