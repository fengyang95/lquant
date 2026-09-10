# 行业资讯模块设计(2026-09-10)

## 背景与目标

为 lquant 增加"行业资讯"子系统:采集、存储、关联与展示四类资讯(快讯流 / 新闻·公告 / 社媒讨论 / 券商研报),覆盖**行业**与**个股**两个维度。MVP 不接 AI;AI 摘要 / 情绪标签 / 情绪因子为后续里程碑。

需求结论(与用户确认):
- 用途:行业研究辅助 + 个股新闻流 + 后续可量化为情绪因子(三者都要)
- 来源:快讯流 / 新闻公告 / 社媒 / 研报,四类全要
- 时效:每日批量拉取,复用现有 ingest 任务体系
- AI:MVP 不引入

## 非目标(Non-goals)

- 不做秒级实时推送
- 不做 AI 摘要 / 情绪打分 / 情绪因子(M3)
- 不做正文全抓与解析(标题+摘要+链接足够研究参考)
- 不做多用户/权限

## 方案选型

采用**方案 A:资讯作为数据域新模块**,复用现有体系:
- 任务状态机复用 `data_task` 模式(pending→running→ok/partial/failed/interrupted),独立表 `news_task`
- 采集器插件化,统一 NewsSource 协议,单来源失败不影响其他来源
- 存储进现有 DuckDB store,新增 DDL
- API 仿照 `server/api/data.py`,前端仿照 `web/src/app/data/` 面板风格

## 架构

```
web/src/app/news/                 三 tab:全部流 / 行业视图 / 个股视图
  ↓ REST
src/lquant/server/api/news.py     列表查询 / 任务触发 / 任务状态
  ↓
src/lquant/news/
  sources/        NewsSource 协议 + 采集器插件(每来源一个)
  store.py        news_item 表读写(去重/过滤/分页)
  link.py         资讯↔行业/个股关联
  tasks.py        news_task 状态机
  __init__.py
  ↓
DuckDB(news_item / news_task 两张新表,ddl.py 注册)
```

## 数据模型

### news_item

| 列 | 类型 | 说明 |
|---|---|---|
| news_id | VARCHAR PK | sha1(source_name + external_id),去重键 |
| source | VARCHAR | 类别:telegraph / news / social / report |
| source_name | VARCHAR | 具体站点(财联社电报 / 新浪7x24 / 东财…) |
| external_id | VARCHAR | 源站原始 id |
| title | VARCHAR | 标题(电报类可能为空) |
| content | VARCHAR | 正文或摘要,截断到 2000 字 |
| url | VARCHAR | 原文链接 |
| industry_code | VARCHAR | 关联行业代码(可空;GILS,同 industry_classify 体系) |
| symbols | VARCHAR[] | 关联个股列表(可空) |
| published_at | TIMESTAMP | 源站发布时间(缺失用采集时刻兜底 + flag bit1) |
| collected_at | TIMESTAMP | 采集时间 |
| quality_flags | INTEGER | 位掩码:1=时间缺失推断 2=内容截断 4=关联为推断非精确 |
| source_tag | VARCHAR | 采集器名 |

> 注:DDL 里 `source` 列存类别,`source_tag` 存采集器名,避免与上游 daily_bar 的 `source` 约定冲突。

### news_task

复用 data_task 模式的独立表:task_id / kind(`daily` / `manual`)/ params(JSON: date, sources)/ status / per-source 明细 JSON / rows_written / started_at / finished_at / message。独立表避免与 data_task schema 漂移;执行器独立但状态机一致,启动时同样执行 mark_interrupted_on_startup 逻辑。

## 关联(link.py)

- **个股关联**:在标题+内容中匹配股票名称/简称。预置词表(来自现有股票基础信息,按名称长度降序防前缀吞并),中文无词边界,用预编译正则词表匹配;命中写入 symbols。
- **行业关联**:优先来源自带行业字段;否则用关键词→行业映射(维护在 `config/schema/news.yaml`,用户可编辑);最后用 industry_classify 反查(个股→行业)兜底,使个股新闻计入行业视图。
- 关联不改原文,只写 industry_code / symbols + quality_flags bit4(推断标记)。

## 采集器(NewsSource 协议)

```python
class NewsSource(Protocol):
    name: str
    category: str  # telegraph / news / social / report
    def fetch(self, day: date) -> list[NewsItem]: ...
```

M1 两个采集器(akshare 通道):
- `telegraph`:`ak.stock_telegraph_cls()`(财联社电报);不可用时 fallback 新浪 7x24 `stock_info_global_sina`
- `em_news`:东财个股新闻 `ak.stock_news_em(symbol=...)`,按股查询,遍历目标股票池(默认成交额 top 200,可配置),逐股拉取

M2 再加 social / report 采集器,协议已预留 category。

## 任务执行流程

1. `POST /api/news/tasks` → create 校验(同源任务互斥)→ pending
2. execute:逐 source 顺序执行,单 source 失败记 per-source 明细;全 ok→ok,部分→partial,全挂→failed;partial/failed 可 retry(只重跑失败 source)
3. 去重:`news_id = sha1(source_name + external_id)`,`INSERT ... ON CONFLICT DO NOTHING`,返回实际新增行数
4. 采集入库后对当日新增行批量执行关联

## API(server/api/news.py)

读接口走 reader(),写接口走 writer()(DuckDB 单写者)。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/news/items | 过滤参数:source/industry/symbol/date/keyword,分页 |
| GET | /api/news/industries | 行业聚合:按 industry_code 分组计数+最新标题 |
| GET | /api/news/sources | 来源清单+每来源最近采集时间/行数 |
| GET | /api/news/tasks | 任务列表 |
| POST | /api/news/tasks | 手动触发采集(kind=manual) |
| POST | /api/news/tasks/{id}/retry | 重试失败来源 |
| GET | /api/news/summary?date= | 每日简报:计数 by source/category/industry top10 |

## 前端(web/src/app/news/)

仿 data/ 页面面板风格,三个 tab:
- **全部流**:时间线列表 + 来源徽章 + 来源/行业/关键词过滤 + 分页
- **行业视图**:左侧行业列表(计数),右侧该行业资讯时间线
- **个股视图**:股票搜索 → 该股新闻流;选股器复用 watchlist 的搜索组件,不可复用则简单下拉
- 侧栏加 "资讯" 入口(Sidebar.tsx)

## 错误处理

- 单来源失败不影响其他来源(per-source 明细),TaskConflictError→409
- 内容截断 2000 字打 bit2;时间缺失用采集时刻兜底打 bit1
- 服务重启把残留 pending/running 任务标 interrupted(同 data_task)
- 输入校验:分页 limit≤200,keyword≤100 字符,symbol 格式校验

## 测试

- 单测:link(词表匹配/长度降序/行业反查)、store(去重/ON CONFLICT/分页过滤)、tasks(状态机/retry 只重跑失败源/互斥 409)、sources(akshare mock 契约测试)
- e2e:采集→入库→API→前端过滤链路
- 前端:过滤/分页/来源徽章组件测试(仿 data/__tests__)

## 里程碑

- **M1(本设计落地范围)**:框架 + telegraph/em_news 采集器 + 落库 + 关联 + API + /news 三 tab 页面
- **M2**:social / report 采集器 + 行业关键词映射 YAML 编辑 UI + 正文抓取评估
- **M3**:AI 摘要 / 情绪标签 + 情绪因子链路(对接 ask-ai 设计)

## 开放问题

- akshare 电报接口字段名/时间精度实现前实测;stock_telegraph_cls 不可用则 fallback 新浪 7x24
- "活跃池"定义(默认成交额 top 200)实现时确认
- 雪球/研报接口可用性 M2 选型时评估
