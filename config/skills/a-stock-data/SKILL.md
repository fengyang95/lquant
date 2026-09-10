---
name: a-stock-data
description: 查询 A 股实时行情、大盘概览、板块、资金流、涨停池、龙虎榜、ETF 数据并做分析。回答必须标注数据时点。
---

# a-stock-data

通过 lquant 本地 HTTP API（默认 http://localhost:8000）查询 A 股数据。所有端点均为只读 GET。

## 查什么用什么

| 想知道 | 调用 |
|---|---|
| 个股/指数实时行情（价格/涨跌幅/成交量） | `GET /api/market/batch?symbols=600519,000001` |
| 大盘概览（情绪分/涨跌停/北向） | `GET /api/market/overview` |
| 涨跌家数（市场宽度） | `GET /api/market/breadth` |
| 板块/行业表现 | `GET /api/market/sectors` |
| 资金流（Top/单票历史） | `GET /api/market/money-flow`（可选 `?symbol=600519`） |
| 涨停池 | `GET /api/market/limit-up` |
| 龙虎榜（最新交易日上榜） | `GET /api/market/dragon-tiger` |
| 热榜（涨跌/放量/龙虎榜要点） | `GET /api/market/heat` |
| 全市场日度截面（分页） | `GET /api/market/snapshot` |
| 主要指数行情与近 N 日收盘 | `GET /api/market/index` |
| 采集器列表/健康度 | `GET /api/market/collectors`、`GET /api/market/collect-status` |
| 历史K线/因子/标的搜索 | `GET /api/data/*`（coverage / securities / daily…，先 GET /api/data/coverage 探测） |
| ETF 列表/行情 | `GET /api/etf/*`（子端点以 /api/etf/* 实际路由为准，先探测） |

## 约定

1. **只读**：仅允许 GET 数据端点。禁止调用 POST /api/market/collect、/api/sync 等触发写入/任务的端点。
2. **数据时点**：回答中必须标注数据来源与时点（实时快照 / 截至 X 交易日）。
3. **空数据**：非交易日或数据缺失时如实说明，不编造数字。
4. **复权口径**：历史价格默认不复权；如需复权口径请说明。
5. 股票代码为 6 位数字；指数可用常见代码（如 000001 上证）。

## 回答结构建议

先给结论，再给数据依据（来源端点 + 时点），最后可给简短分析。数字必须来自 API 返回，不凭记忆编造。
