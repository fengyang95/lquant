# Qlib 前端接入设计（qlib features in web UI）

日期：2026-09-20
状态：已确认（设计讨论通过）

## 背景与目标

PR #75 落地了 qlib 接入：日线湖 → qlib 二进制导出（`qlib_io/export.py`）、
qrun 风格工作流 runner（`qlib_io/runner.py`）、CLI `lq qlib export/check/workflow`。
目前只能命令行操作。目标：把 qlib 能力融入平台前端，方便鼠标操作：

1. 导出 qlib 数据（异步任务 + 状态展示）
2. 跑 qlib 工作流（因子分析 + 组合回测，长任务接入任务中心）
3. 历史运行查看与对比
4. workflow yaml 配置管理

## 总体决策（已与用户确认）

- **入口**：不建独立 `/qlib` 页面，融入现有页面：
  - `/data`：qlib 数据导出卡片 + 数据状态
  - `/factors`：qlib 工作流运行入口 + 配置管理
  - `/backtests`：qlib 运行结果集中展示 + 对比
- **队列**：新专用队列 `lquant-qlib`（与 backtest/mining 隔离）。
- **任务中心**：新增任务类别 `kind=qlib`，前端任务中心可见/可取消。

## 后端

### 1. 存储：`src/lquant/qlib_io/store.py`（新）

qlib 运行元数据表 `qlib_run`（自建表，模式参照 `paper/store.py` 的自管 DDL）：

```
qlib_run(
  id TEXT PRIMARY KEY,          -- uuid
  config TEXT NOT NULL,         -- workflow yaml 相对路径
  market TEXT,                  -- 股票池覆盖（instruments 名）
  exp_name TEXT NOT NULL,
  status TEXT NOT NULL,         -- queued/running/finished/failed/canceled
  metrics TEXT,                 -- metrics JSON（IC/ICIR/Rank IC/超额收益…）
  config_snapshot TEXT,         -- 运行时 yaml 快照（便于复现与对比）
  log_path TEXT,                -- runner stdout/stderr 落盘路径
  created_at TEXT, finished_at TEXT, error TEXT
)
```

API：`create_run / update_run / get_run / list_runs(limit, status?)`。
初始化挂到现有 `init_db` 流程（幂等 DDL）。

### 2. API：`src/lquant/server/api/qlib.py`（新，prefix=/qlib）

| 端点 | 说明 |
|---|---|
| `GET /qlib/status` | data/qlib 目录状态：是否存在、manifest.json 内容+mtime、日历起止、instruments 清单（all/topN）、check 结果摘要 |
| `POST /qlib/export` | body{start?, end?, symbols?, sec_types?, fields?, top?} → pydantic 校验 → `enqueue("lquant-qlib", …)` 执行 `qlib_io.export.export`（进度上报 progress.py），完成后自动跑 `check()` 并存摘要；返回 202 {job_id} |
| `GET /qlib/configs` | 列出 `config/qlib/*.yaml`（文件名 + mtime + 描述行） |
| `GET /qlib/configs/{name}` | 返回 yaml 原文 |
| `PUT /qlib/configs/{name}` | 保存 yaml：先 yaml.safe_load 语法校验 + 必填键（qlib/experiment/data/…）校验，非法 422；禁止路径穿越（name 白名单正则 `[A-Za-z0-9_-]+`） |
| `POST /qlib/workflow` | body{config, market?, exp_name?} → 校验 config 存在、data/qlib 已导出 → create_run(queued) → `enqueue("lquant-qlib", …)`；返回 202 {run_id} |
| `GET /qlib/runs` | 历史列表（limit、status 过滤） |
| `GET /qlib/runs/{id}` | 详情：metrics JSON 解析、config_snapshot、log 尾部 |
| `POST /qlib/runs/{id}/cancel` | 复用 `request_cancel` 协作式取消 |
| `POST /qlib/runs/compare?ids=a,b` | 两次运行指标并集对比 |

工作流执行体（进程内函数，供 enqueue 调用）：
- update_run(running) → 复用 CLI `_find_qlib_python` 探测逻辑（**抽到
  `lquant/qlib_io/runner.py` 公共函数**，CLI 与 server 共用）→
  子进程跑 `qlib_io/runner.py`（stdout/stderr 落 log_path）→
  成功读 metrics JSON 落库，失败落 error。
- qlib venv 缺失：run 落 failed，error 含安装指引文本；
  `POST /qlib/workflow` 前置也预探测，缺失时直接 422 提示（避免注定失败的入队）。

### 3. 任务中心

- `jobs.py`：`QUEUES` 增加 `"lquant-qlib"`。
- `task_center.py`：`KINDS` 增加 `"qlib"`；`_job_items("lquant-qlib", "qlib", …)`，
  显示名优先 progress 注册名（"Qlib 导出" / "Qlib 工作流"）。
  取消：`cancel_ep` 走通用 `request_cancel` 分支（qlib 队列 job 非孤儿场景）。

### 4. CLI 保持不变

`lq qlib …` 不动；探测逻辑重构为公共函数属内部重构，行为不变。

## 前端（web/）

### `/data` 页 — Qlib 数据导出卡片

- 状态条：数据根目录存在性、manifest（导出时间、标的数、日历范围、字段）、
  上次 check 摘要（problems 数）。
- 表单：start/end（日期选择）、top（数字）、sec_types/fields（多选，缺省全集）、
  symbols（可选文本）。提交 → POST /qlib/export → 跳任务中心。
- 按钮触发 GET /qlib/status 刷新。

### `/factors` 页 — Qlib 工作流区块

- 配置选择（GET /qlib/configs 下拉）+ 内联编辑器（textarea 读原文、
  PUT 保存、保存前前端 JSON/yaml 提示校验结果）。
- 运行表单：market（可选）、exp_name；提交 → POST /qlib/workflow。
- 历史运行列表（GET /qlib/runs）：状态、config、时间；点击跳 /backtests 详情。
- qlib venv 缺失：红色提示条展示安装指引。

### `/backtests` 页 — Qlib 结果展示

- 新增 "Qlib 运行" 区块（与现有回测记录并列）：指标卡片
  IC / ICIR / Rank IC / Rank ICIR / excess_return_without_cost / excess_return_with_cost，
  状态、耗时、config_snapshot 折叠查看。
- 对比：勾选两次 finished 运行 → compare 视图（指标左右对照 + 差值标色）。

### 任务中心 `/tasks`

- kind 枚举加 `qlib`（文案 "Qlib"），列表/汇总/取消沿用现有组件。

## 错误处理

- 所有写操作（export/workflow/PUT config）入参 pydantic/白名单校验，fail fast 422。
- 子进程失败：stderr 全量落 log 文件，run.error 存尾部摘要，前端可展开看日志。
- data/qlib 未导出就跑 workflow：422 明确提示先导出。
- API 统一复用现有 envelope（server/envelope.py）。

## 测试（覆盖率 ≥95% 增量门禁）

- 后端单测（打桩 export/runner 子进程，参照现有 server api 测试模式）：
  - store CRUD 与状态迁移
  - export/workflow 入队参数与校验（非法 config、路径穿越、缺数据目录）
  - workflow 执行体：venv 缺失落 failed、成功落 metrics、取消
  - task_center qlib 归一
  - 配置读写与校验
- 前端 vitest：表单校验、状态展示、runs 列表/对比渲染、任务中心新 kind。

## 非目标（YAGNI）

- 不做 workflow yaml 可视化结构编辑器（只做原文编辑 + 校验）。
- 不做 mlflow 指标全量同步（只落 runner 产出的 metrics JSON）。
- 不做 qlib 数据增量导出（每次全量/按区间覆盖导出）。
