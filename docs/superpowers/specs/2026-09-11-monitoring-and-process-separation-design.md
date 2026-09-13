# 监控子系统 + 进程分离 设计文档

- 日期：2026-09-11
- 状态：方案 A 已与用户对齐，待 spec 评审
- 影响范围：`src/lquant/`（新 `monitor` 包、`server/main.py`、`cli/main.py`、`core/config.py`）、`web/`、`lquant.sh`、`tests/`

## 1. 背景与目标

当前 lquant 是单 http 进程（uvicorn 起 FastAPI），任务队列双模式：有 Redis 走 RQ，无 Redis 降级为后台线程（`server/jobs.py`）。本次需求：

1. **监控页面**：数据拉取延迟、任务延时、API 接口耗时、CPU/mem 占用，用于平台运维。历史曲线 + 实时快照。
2. **进程分离**：一个 http 进程专注展示/交互；一个通用 worker 进程；CPU 密集的回测任务可额外起最多 4 个（可配置）专用进程。

非目标：分布式部署、告警推送、用户认证。

## 2. 决策记录

| 决策点 | 结论 | 理由 |
|---|---|---|
| 跨进程任务通道 | 复用 RQ + Redis（`server/jobs.py` 现有双模式） | 沿用现有基础设施；Redis 转为运行必需依赖 |
| 指标存储 | 独立 `monitor.duckdb`，只有 http 进程写 | DuckDB 单写进程硬约束（core/db.py）；独立文件避开主库写锁与迁移逻辑 |
| worker 拉起 | `lq worker` CLI 拉起固定 worker 组 | 生命周期透明，不与 uvicorn --reload 打架 |
| 降级策略 | Redis 断连/未启动时监控页标红 offline | 采集失败永不影响业务 |

## 3. 架构

```
浏览器 ── Next.js /monitor 页面
              │ /api/monitor/*
http 进程 (uvicorn, server/main.py)
  ├─ MonitorMiddleware（纯 ASGI）：API 耗时 → 内存环缓冲
  ├─ flusher 线程（每 10s，唯一写者）→ monitor.duckdb
  │    1) 环缓冲 → metrics_api
  │    2) drain Redis 生命周期事件 list + 内存事件队列 → metrics_task
  │    3) Redis proc 样本 → metrics_sys
  │    4) 超 7 天（可配）清理，每天一次
  ├─ http 进程自采样线程（proc_name='http'）→ Redis
  └─ /api/monitor/* 只读聚合查询
worker 进程组（`lq worker` 拉起，multiprocessing spawn）
  ├─ 1 个通用 worker（lquant-default + lquant-ingest 队列）
  ├─ K 个回测 worker（lquant-backtest 队列），K 可配默认 2，硬上限 4
  ├─ 每个 worker 进程内 sampler 线程：每 5s 推样本 → Redis SETEX 15s
  └─ MonitoringWorker：任务 started/finished/failed 事件 → Redis list
```

Redis key 规范（前缀 `lquant:monitor:`）：
- `lquant:monitor:proc:<name>` — JSON 样本 `{pid, cpu_pct, mem_rss_mb, current_job, ts}`，SETEX 15s；name 形如 `http`、`general-0`、`backtest-0..3`
- `lquant:monitor:events` — 任务生命周期事件 list（LPUSH 写入 / RPOP FIFO drain，flusher 拉走后即删）
- `lquant:monitor:recent` — 近期任务事件展示 list（LPUSH + LTRIM 0 49，不 drain）

## 4. 配置

`core/config.py` Settings 新增（`config/app.yaml` 的 `monitor:` 段可覆盖；`LQ_MONITOR_ENABLED`、`LQ_MONITOR_DB`、`LQ_BACKTEST_WORKERS` 环境变量优先级更高）：

```yaml
monitor:
  enabled: true
  flush_interval_sec: 10   # flusher 落盘周期
  sample_interval_sec: 5   # 进程自采样周期
  retention_days: 7
  backtest_workers: 2      # 回测 worker 数，硬上限 4
```

- `monitor_db_path`：默认 = `duckdb_path` 同目录的 `lquant.monitor.duckdb`
- `backtest_workers_max: 4`（硬编码常量，不开放配置）
- CLI/环境变量/config 优先级：CLI `--backtest` > env `LQ_BACKTEST_WORKERS` > config

## 5. monitor 包结构（新包 `src/lquant/monitor/`，每文件 <200 行）

```
src/lquant/monitor/
  __init__.py      # start_monitor()/stop_monitor() 线程生命周期 + 装配
  types.py         # 冻结数据类：ApiMetricPoint / ProcSample / TaskEvent
  ring.py          # ApiRing 环缓冲（线程安全，snapshot()→tuple，落盘成功才 clear）+ local_events 内存事件队列 + 包级单例
  api_mw.py        # 纯 ASGI 计时中间件
  proc_sampler.py  # 自采样线程（psutil；http 进程也自采样 proc_name='http'）
  flusher.py       # flush 周期：环缓冲→metrics_api、事件→metrics_task、样本→metrics_sys + 每天一次保留清理
  queries.py       # 分桶聚合查询 + /summary 快照
  worker.py        # MonitoringWorker(Worker) 子类 + spawn 入口 + run_supervisor()
  emit.py          # emit_task_event()：事件统一出口（Redis 可用→两个 list；不可用→内存队列）
```

## 6. monitor.duckdb 表结构

```sql
-- 只有 http 进程写；flusher 每周期幂等 CREATE TABLE IF NOT EXISTS
CREATE TABLE IF NOT EXISTS metrics_api (
  ts TIMESTAMP, route VARCHAR, method VARCHAR, status INTEGER,
  duration_ms DOUBLE, dur_category VARCHAR,
  -- dur_category: status>=400 → 'error'（优先）；否则 ≤100ms 'fast' / >1s 'slow' / 其余 'normal'
  PRIMARY KEY (ts, route, method, status, duration_ms)
);

CREATE TABLE IF NOT EXISTS metrics_task (
  event_ts TIMESTAMP, job_id VARCHAR, job_name VARCHAR, event VARCHAR,
  -- event: 'started' / 'finished' / 'failed'；started 事件 elapsed_ms 为 NULL
  queue VARCHAR, enqueued_at TIMESTAMP, started_at TIMESTAMP, finished_at TIMESTAMP,
  elapsed_ms DOUBLE,       -- 执行耗时 = finished_at - started_at
  queue_delay_ms DOUBLE,   -- 排队延时 = started_at - enqueued_at（enqueued_at 缺失为 NULL）
  message VARCHAR          -- failed 事件错误消息，截断 200 字符
);

CREATE TABLE IF NOT EXISTS metrics_sys (
  ts TIMESTAMP, proc_name VARCHAR, pid INTEGER, cpu_pct DOUBLE, mem_rss_mb DOUBLE,
  current_job VARCHAR,
  PRIMARY KEY (ts, proc_name)
);
```

- 去重依赖"环缓冲只在落盘成功后清空"；主键只防极端重复
- 写失败 → log warn + 保留数据下周期重试
- 查询连接：同路径同配置 → DuckDB 共享实例，内部锁并发安全（同 core/db.py 模式）

## 7. 组件行为

### MonitorMiddleware（api_mw.py，纯 ASGI）
- 计时区间：请求开始 → `http.response.start`（TTFB 口径），perf_counter
- route 模板：send 时读 `scope["route"].path`（路由已发生），无 route → `'unmatched'`
- 排除：`/api/monitor*`、`/api/health`、websocket scope、静态资源（`/_next` 前缀、`.js/.css/.svg/.png/.ico/.map` 后缀）——避免自反馈；`_should_skip()` 与 `_classify()` 为可单测 helper
- `monitor_enabled=False` → 中间件透传不采集

### proc_sampler（proc_sampler.py）
- psutil `Process.cpu_percent()`（本进程，非进程树）+ mem_rss + current_job
- worker 进程：RQ Worker registry 按 pid 匹配自身 → `get_current_job_id()`（RQ 版本行为以 uv.lock 为准，实现时验证）
- psutil 缺失 → cpu/mem 记 None，样本照发（在线状态照常）
- Redis 不可用 → 样本发送静默失败（log），在线状态自然过期

### MonitoringWorker + 事件上报（worker.py + emit.py）
- `MonitoringWorker(Worker)` 覆写 `perform_job`：前置发 started；结束后按 `job.get_status()` 发 finished/failed；failed 的 error 从 `job.latest_result()` 截断 200 字符
- 事件统一走 `monitor/emit.py` 的 `emit_task_event()`：Redis 可用 → LPUSH events + recent 两个 list；Redis 不可用（本地降级任务）→ 内存事件队列
- `server/jobs.py` 本地降级路径：包装 fn，执行线程内发 started/finished/failed 事件（→ 内存队列）；enqueue/dispatch 逻辑不动

### `lq worker` CLI（cli/main.py 子命令 + monitor/worker.py supervisor）
- preflight：Redis ping 失败 → stderr + exit 1，提示"先启动 Redis（docker compose up -d redis）"
- 拉起 1 通用（default+ingest 队列）+ K 回测 worker；K 来源优先级 CLI `--backtest` > env `LQ_BACKTEST_WORKERS > config `monitor.backtest_workers`，clamp 0..4
- `--general 0` 可关闭通用 worker
- multiprocessing spawn context；父进程 watchdog 每 5s 检查，死子进程 respawn + log
- SIGINT/SIGTERM → 子进程 SIGINT（RQ friendly shutdown）→ 5s grace → terminate

### flusher（flusher.py）
- 每 flush_interval_sec（默认 10s）一个周期；daemon 线程
- 环缓冲只在 DuckDB 写成功后 clear；写失败 log warn 保留重试
- Redis 任何操作 try/except，失败仅 log
- 停止时做最后一次 final flush 再停线程

### 查询（queries.py）
- 队列深度：pending = Queue.count + StartedJobRegistry.count；failed = FailedJobRegistry.count（三队列逐一展示）
- 分桶聚合：`/api-latency`、`/tasks` 共用分桶 helper；range→bucket 映射 {1h:1min, 6h:5min, 24h:15min, 7d:2h}
- 时间网格 generate_series + LEFT JOIN 补零桶；`time_bucket` 可用性以锁定 DuckDB 版本为准，fallback epoch//bucket_sec 整除分桶
- /summary 的 api_live：环缓冲最近 5min 实时 p50/p95/错误率（未落盘数据）
- 数据拉取延迟：主库 collect_log（reader()）recent 200 + by_job 聚合（count/avg/失败率，ORDER BY avg duration DESC）

## 8. API 端点（server/api/monitor.py，统一 envelope）

- `GET /api/monitor/summary` — 实时快照：procs（含 http，online = 样本年龄 <15s）/queues/api_live/task_recent（recent list 尾部 50 条）
- `GET /api/monitor/api-latency?range=1h|6h|24h|7d` — 分桶 series + 慢接口排行（按 route 聚合 p95 DESC top10）
- `GET /api/monitor/tasks?range=...` — p95 queue_delay + p95 elapsed 双口径曲线
- `GET /api/monitor/data-pulls` — collect_log recent 200 + by_job 聚合
- `GET /api/monitor/workers` — /summary 的 procs 部分复用

## 9. 前端 /monitor 页面

`web/src/app/monitor/page.tsx`（+ 局部组件拆分，遵循项目设计系统——项目记忆：globals.css 导入坑、双轴对齐坑）：
- stat 卡区：进程卡（online 点 + cpu/mem 当前值）+ 队列深度（三队列 pending/failed）+ api_live p95
- 进程状态表：http + workers（online 点/cpu/mem/current_job/样本年龄）
- API 耗时曲线：p50/p95 双线 + 错误率右轴，range 切换 1h/6h/24h/7d
- 任务延时曲线：p95 queue_delay + p95 elapsed 双线
- 数据拉取延迟表：recent 200（job/trade_date/耗时/rows/status）+ by_job 聚合
- 30s 自动刷新 /summary；曲线 on-mount + range change 时拉取

## 10. 核心行为细节

- **监控采集永不影响业务**：所有采集路径 try/except + log；写失败保留待重试
- **DuckDB 单写**：只有 http 进程 flusher 写 monitor.duckdb
- **psutil** 加入 pyproject（`psutil>=5.9`）；缺失降级 cpu/mem NULL，在线状态照常
- **RQ 行为**（perform_job 语义、registry API）以 uv.lock 锁定版本为准，实现时验证
- **保留策略**：超 retention_days（默认 7 天）行每天清理一次
- **monitor.duckdb 默认路径**：`data/duckdb/lquant.monitor.duckdb`（data/ 已 gitignore）
- `server/main.py`：create_app 注册中间件；startup `start_monitor()`，shutdown final flush + `stop_monitor()`；现有 startup 逻辑不动
- `lquant.sh` 加 `lq worker &> logs/worker.log &` 启动段
- docker-compose 已有 redis 服务则复用，无则提示

## 11. 测试计划

- **单测**：ring buffer 环绕/溢出/snapshot 不可变、`_should_skip`/`_classify`、flusher 落盘/写失败重试/保留清理、queries 分桶聚合（内存 DuckDB fixture）、CLI K clamp、preflight
- **集成**（pytest.mark.integration，Redis 不可用则 skip，复用 `jobs._redis_available()`）：http + lq worker 子进程全链路：提交任务 → 等 finished 事件落盘 monitor.duckdb → 查 /api/monitor/summary 与 /tasks 断言数据；monitor.duckdb 用 `LQ_MONITOR_DB` 指向 tmp path
- 80% 覆盖率目标按全局规则执行
