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
| 时间范围重拉 | 任务 params 必带 start/end；前端弹窗可选起止日期，对已拉区间重跑 =
  write_daily 按 (symbol, trade_date) 同键覆盖，幂等 |
| 跨源印证 | 复用 run_crosscheck（抽样对拍、分歧分档、落 issue，peer 数值绝不写回湖），
  补 API + 前端展示；任务完成后可选自动抽检 |

## 2.2 跨源对拍设计（复用 data/ingest/crosscheck.py）

已有能力：从湖抽哨兵样本 → 直接实例化 peer（不经过 enabled 门，运维动作）→
classify_divergence 分档 → flag_cross_source 写回湖 → 落 data_quality_issue。

新增：
- `POST /api/data/crosscheck {start?, end?, peers?, limit?}` → summary + issues 列表
- `GET /api/data/crosscheck/issues` → data_quality_issue 检索（含 resolve）
- 任务完成后的自动抽检：data_task 参数 `auto_crosscheck: true`（默认 true，daily_update 也做），
  执行器在 ok/partial 收尾时对本次窗口跑一次抽样对拍，结果记 failed_detail 同级字段
- 前端数据页第三区加「跨源印证」卡：最近对拍 summary（比对标的数/分歧分档计数）+
  分歧明细表（symbol/field/primary 值/peer 值/偏差%），可一键 resolve

原则不变：对拍只用于标记与降级，peer 不可用不算失败，绝不做取值来源。

## 2.3 主源 / peer 源可配置（settings 机制，配置后再拉）

现有基础：`SettingsStore`（app_setting 表覆盖层 + 白名单校验）已有 `providers_order`
（fallback 链，链头即主源）；`GET /settings/providers` 已返回各源启用状态与 capability。

新增/明确：
- 设置项 `crosscheck_peers`（list，新增到 SETTING_DEFS）：对拍 peer 源名单，
  只允许填**已声明 daily/etf_daily capability 的源**（写入时校验，非法源 422）
- 主源即 `providers_order` 第一位 —— 不另设 key，一个名单一个语义，
  执行器/对拍都从 SettingsStore 读（每次任务创建时读取，改完配置**下一次拉取即生效**，
  不需要重启服务）
- 前端数据页「数据源配置」区：主源下拉（= providers_order 拖排序首位）+ peers 多选
  （只列出 capability 支持日线的源）+ 「保存并立即生效」
- 执行器与 run_crosscheck 改读 SettingsStore 而非 providers.yaml 的 crosscheck 节
  （yaml 值退化为默认值，仍可写）

## 2.4 DAILY_BAR schema 扩展（全部纳入）

`DAILY_BAR`（`data/schema.py`）新增 9 列，BaoStock 日线接口全部免费直出：

| 列 | 类型 | 说明 |
|---|---|---|
| pct_chg | Float64 | 当日涨跌幅%（源站 pctChg） |
| is_st | Boolean | ST/*ST 标记（源站 isST） |
| is_suspended | Boolean | 停牌标记（tradestatus=0 → 行保留并标记，**不再丢行**） |
| pe_ttm | Float64 | 滚动市盈率（peTTM） |
| pb_mrq | Float64 | 市净率（pbMRQ） |
| ps_ttm | Float64 | 滚动市销率（psTTM） |
| pcf_ncf_ttm | Float64 | 滚动市现率（pcfNcfTTM） |
| total_mv | Float64 | 总市值（元，总股本×close 换算） |
| float_mv | Float64 | 流通市值（元） |

配套改动：
- baostock adapter：_DAILY_FIELDS 扩列 + 映射层加字段（ETF 无估值字段 → null）
- 停牌行从「丢弃」改为「保留 + is_suspended=true，volume=0」；
  质量门禁断言同步（停牌行豁免量价断言）、回测撮合拒停牌（engine 已有涨跌停拒单，
  补停牌判断）
- 中性化/市值暴露改用 float_mv 列（有值优先，回退现路径）
- schema 是「目标态」：旧湖文件缺列由 parquet 读取侧 `missing_columns='null'` 兼容，
  重拉区间自然补齐，无需迁移存量
- 衍生指标（复权价/MA/涨跌停判定等）仍不入湖，读取侧按需计算

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
