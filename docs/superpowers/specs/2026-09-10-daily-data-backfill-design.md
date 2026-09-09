# 日线数据补全与管理设计

> 日期：2026-09-10 ｜ 状态：已确认（用户批准）
> 范围：一次性全历史批量回填（含退市股 + ETF）+ 每日定时增量 + 前端数据管理页

## 1. 背景与目标

日线数据需要：
1. 一次性批量拉取全历史（2016-01-01 起）日线，之后每天定时增量更新
2. 前端数据管理界面：任务进度、失败明细、覆盖度详情

现有基础：`data/ingest/daily.py`（断点续传/看门狗/质量门禁）、`sync/manager.py`（调度 + 历史）、
`server/jobs.py`（Redis/RQ + 本地线程）、`/ws/jobs/{id}`、`/api/data/coverage`。

缺口：全量回填无「可观察任务」形态；每日增量只跑哨兵池 200 只；前端无统一数据管理视图。

## 2. 核心决策

| 决策点 | 结论 |
|---|---|
| 历史范围 | 2016-01-01 起 |
| 覆盖标的 | 股票（**含退市**）+ ETF/LOF，两个 phase 顺序执行，统计独立 |
| 回填池 | full_backfill 用 `all_symbols()`（含退市，防幸存者偏差）；daily_update 用 `active_symbols()` + ETF |
| 前置校验 | security 表退市股数为 0 → 422 拒绝 full_backfill，提示先跑 `lq data reference` |
| 任务形态 | 后台流式：API 立即返回 task_id，进度逐批 UPDATE 落 DuckDB `data_task` 表 |
| 进度可见性 | WS `/ws/jobs/{id}` + 前端轮询降级（读同一行，不丢状态） |
| 容错粒度 | 单批失败不中断；failed_symbols 累计；retry 只补漏（清 failed 的 checkpoint） |
| 激进早停 | 连续 10 批全失败 → 置 failed 早停（防源挂时空转） |
| 每日增量 | sync `daily` 作业改全市场（股票+ETF），回看 10 天，重启自动补跑 |
| ETF 风险 | BaoStock ETF 日线非官方支持，失败不阻塞股票 phase，fallback 切 tushare |
| 退市股拉取 | end 取 min(end, outDate)；outDate 未知则照常拉（下次 reference 同步修正） |

## 3. data_task 表（DuckDB）

    CREATE TABLE IF NOT EXISTS data_task (
        task_id        VARCHAR PRIMARY KEY,
        kind           VARCHAR,          -- full_backfill | daily_update
        params         JSON,             -- {start, end, market, days}
        status         VARCHAR,          -- pending|running|ok|partial|failed|interrupted
        phase          VARCHAR,          -- stocks | etf | null
        total_symbols  INTEGER,
        done_symbols   INTEGER,
        failed_symbols JSON,             -- ["600000.SH", ...]
        failed_detail  JSON,             -- [{symbol, reason}...]
        rows_written   INTEGER,
        started_at     TIMESTAMP,
        finished_at    TIMESTAMP,
        message        VARCHAR
    )

进度行每批 UPDATE 一次（全市场 10 年约 30 行 UPDATE，DuckDB 单写者下开销可忽略）。
不采用内存态 + 定期 flush —— 重启会丢进度。

## 4. 执行器 `data/ingest/tasks.py`

    create_task(kind, params) -> task_id
        - full_backfill：校验退市股存在，池 = all_symbols + etf_symbols
        - daily_update：池 = active_symbols + etf_symbols
    execute_task(task_id)                       # 被 jobs 队列调用的入口
    run_backfill_task(task_id, pool, start, end) # 流式逐批推进
    retry_task(task_id)                          # 清 failed 的 checkpoint 后续传

执行节奏（每批）：
1. target.daily_bars(batch, start, min(end, outDate))
2. 缩批重试：整批超时 → 切 20 只小批，失败者记 failed_symbols
3. 质量门禁 fatal → 该批不入湖，记 failed
4. write_daily 落湖 → UPDATE data_task 进度 → WS 推送
5. 连续 10 批全失败 → status=failed 早停
6. 服务重启后 pending/running 残留 → 启动时标 interrupted，可 retry 续传

## 5. API（server/api/data.py 新增 /tasks 路由）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/data/tasks | 创建任务，202 + {task_id}；有 running 任务 → 409 |
| GET | /api/data/tasks | 任务历史列表 |
| GET | /api/data/tasks/{id} | 进度 + failed_detail（轮询端点） |
| POST | /api/data/tasks/{id}/retry | 只重跑失败标的 |

创建后由 jobs 队列（Redis/RQ 或本地线程）执行 execute_task。
WS 复用 /ws/jobs/{id}：执行器每批推送 {done, total, phase, status}。

## 6. 每日增量（sync/manager.py）

- `daily` 作业 params 加 `market: "all"`：池 = active_symbols + etf_symbols，
  执行走同一个执行器（run_backfill_task），也建 data_task 记录 → 历史可查
- 调度语义不变：18:30 周一~五，重启后 last_run 早于今天 → 自动补跑

## 7. 前端 /data 页（三区改造）

1. 任务区：任务列表（状态徽章 + 进度条 + 行数/耗时），「全量回填」按钮（选起止日期）、
   「立即增量」按钮；running 任务 WS 实时刷新，WS 断开降级 2s 轮询
2. 失败下钻：partial/failed 任务展开 failed_detail 表 + retry 按钮
3. 覆盖度详情：现有 coverage 卡片 + 按月聚合的每日标的数 vs 应有标的数，缺口标橙

涨红跌绿设计系统沿用 [[lquant-research-desk-design-system]]。

## 8. 不做的（YAGNI）

- 分钟线/财务的批量回填 UI（执行器 kind 可扩展，暂不加）
- 任务并行/优先级（单写者约束下串行 + 409 冲突即可）
- 失败自动重试（手动 retry 按钮足够，自动重试仅保留批内缩批重试）
- 多市场扩展（执行器参数已留 market 字段）

## 9. 测试

- 单测：任务 CRUD/状态机（pending→running→ok/partial/failed）、
  早停逻辑、缩批重试、退市股 end 截断、retry 只补漏、前置校验 422
- 集成：demo provider 跑通 full_backfill 全链路（含 data_task 落库 + API 列表/详情/retry）
- API：422/409 语义、进度端点、WS 推送格式

## 10. 关键文件

- `src/lquant/data/ingest/tasks.py`（新）：data_task 表 + 执行器
- `src/lquant/data/ingest/daily.py`：拆出可复用的逐批回填函数（带进度回调）
- `src/lquant/server/api/data.py`：/tasks 路由
- `src/lquant/server/main.py`：启动时标 interrupted 残留任务
- `src/lquant/sync/manager.py`：daily 作业接执行器
- `web/src/app/data/page.tsx`：三区改造
