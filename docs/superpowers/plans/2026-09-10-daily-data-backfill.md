# 日线数据补全与管理 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan 逐任务执行。步骤用 `- [ ]` 勾选跟踪。

**Goal:** 一次性全历史批量回填（含退市池 + ETF）+ 每日定时增量 + 前端数据管理页 + 跨源对拍与主源/peer 可配置。

**Architecture:** 复用现有 checkpoint/看门狗/质量门禁/jobs 队列；新增 data_task（DuckDB）为唯一进度真源，执行器逐批流式推进；API 立即返回 task_id；前端 /data 三区改造。

**Tech Stack:** Python 3.13 / FastAPI / Polars / DuckDB / pytest；Next.js 15 + TS + Tailwind。

**Spec:** docs/superpowers/specs/2026-09-10-daily-data-backfill-design.md

## Global Constraints

- 代码格式统一 `000001.SZ` / `510300.SH`，绝不用裸 6 位
- 时间 ISO 8601 + Asia/Shanghai；金额统一元
- DuckDB 单写者：写收敛 writer()，进 writer 前把查数做完
- 湖只信 primary 源，对拍 peer 数值绝不写回湖
- 涨红跌绿（前端）
- pytest 全绿 + ruff 干净才能提交（make test / make lint）

---

### Task 1: DAILY_BAR schema 扩展（9 列）

**Files:**
- Modify: `src/lquant/data/schema.py`（DAILY_BAR dict）
- Test: `tests/unit/test_schema_daily.py`（新建）

**Interfaces:**
- Produces: DAILY_BAR 新列：`pct_chg: Float64, is_st: Boolean, is_suspended: Boolean, pe_ttm/pb_mrq/ps_ttm/pcf_ncf_ttm/total_mv/float_mv: Float64`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_schema_daily.py
from lquant.data.schema import DAILY_BAR

def test_daily_bar_has_quant_columns():
    cols = ("pct_chg", "is_st", "is_suspended", "pe_ttm", "pb_mrq",
            "ps_ttm", "pcf_ncf_ttm", "total_mv", "float_mv")
    for col in cols:
        assert col in DAILY_BAR, col

def test_daily_bar_types():
    assert str(DAILY_BAR["pct_chg"]) == "Float64"
    assert str(DAILY_BAR["is_st"]) == "Boolean"
    assert str(DAILY_BAR["is_suspended"]) == "Boolean"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_schema_daily.py -v`
Expected: FAIL（列不存在）

- [ ] **Step 3: 最小实现**

`schema.py` 的 DAILY_BAR 在 `"adj_factor"` 行后插入：

```python
    "pct_chg": pl.Float64,        # 当日涨跌幅%（源站 pctChg）
    "is_st": pl.Boolean,          # ST/*ST（源站 isST）
    "is_suspended": pl.Boolean,   # 停牌标记（行保留不再丢）
    "pe_ttm": pl.Float64,         # 滚动市盈率（peTTM）
    "pb_mrq": pl.Float64,         # 市净率（pbMRQ）
    "ps_ttm": pl.Float64,         # 滚动市销率（psTTM）
    "pcf_ncf_ttm": pl.Float64,    # 滚动市现率（pcfNcfTTM）
    "total_mv": pl.Float64,       # 总市值（元）
    "float_mv": pl.Float64,       # 流通市值（元，close×volume/turn 推导）
```

- [ ] **Step 4: 跑测试通过**

Run: `uv run pytest tests/unit -k schema -v`（含既有 schema 测试，必须全绿）

- [ ] **Step 5: Commit**

```bash
git add src/lquant/data/schema.py tests/unit/test_schema_daily.py
git commit -m "feat: DAILY_BAR schema 扩展 9 列（估值/状态/市值）"
```

### Task 2: baostock adapter 扩列 + 停牌保留

**Files:**
- Modify: `src/lquant/data/providers/baostock.py`（_DAILY_FIELDS + 映射层）
- Modify: `src/lquant/data/quality/asserts.py`
- Modify: `src/lquant/backtest/engine.py`
- Test: `tests/unit/test_baostock_daily_fields.py`（新建）

**Interfaces:**
- Produces: 模块级函数 `_map_daily_raw(rows) -> pl.DataFrame`（子进程 pickle 兼容）；daily_bars 输出含 9 新列；停牌行保留 is_suspended=True、volume=0
- Consumes: Task 1 的 schema 列名

- [ ] **Step 1: 写失败测试（不联网，喂模拟原始行）**

```python
# tests/unit/test_baostock_daily_fields.py
import polars as pl
from lquant.data.providers.baostock import _map_daily_raw

# 行结构与 _DAILY_FIELDS 顺序一致：
# date,code,open,high,low,close,preclose,volume,amount,turn,tradestatus,isST,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM
RAW = [
    ["2024-01-02", "sh.600519", "1700.0", "1720.0", "1690.0", "1710.0", "1705.0",
     "1000000", "1.71e9", "0.8", "1", "0", "0.29", "25.5", "8.2", "6.1", "18.0"],
    ["2024-01-03", "sh.600519", "1710.0", "1725.0", "1700.0", "1715.0",
     "0", "0", "0", "0", "0", "0", "", "", "", ""],  # 停牌行
]

def test_daily_map_full_columns():
    df = _map_daily_raw(RAW)
    r0 = df.filter(pl.col("trade_date") == pl.date(2024, 1, 2)).row(0, named=True)
    assert r0["pe_ttm"] == 25.5
    assert df["is_suspended"][1] is True
    exp = 1710.0 * 1_000_000 / (0.8 / 100)
    assert abs(r0["float_mv"] - exp) < 1.0

def test_suspended_row_kept():
    df2 = _map_daily_raw(RAW)
    assert len(df2) == 2
    assert df2.filter(pl.col("is_suspended"))["close"][0] == 1715.0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_baostock_daily_fields.py -v`
Expected: FAIL（`_map_daily_raw` 不存在）

- [ ] **Step 3: 实现 baostock 映射层**

`src/lquant/data/providers/baostock.py`：
1. `_DAILY_FIELDS` 追加 `,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM`
2. 抽模块级函数 `_map_daily_raw(rows) -> pl.DataFrame`：把 `_fetch_daily` 里
   frames→DataFrame→cast 那段移进来（spawn 子进程可 pickle，watchdog 兼容）
3. 映射规则：
   - schema 列表按新字段顺序补全
   - **删掉停牌过滤行** `filter(tradestatus != "0")`
   - `is_suspended = tradestatus == "0"`；`is_st = isST == "1"`
   - 估值四列 cast Float64（空串 → null）
   - `float_mv = close * volume / (turn/100)`，turn 为 0/空 → null
   - `total_mv` 置 null（baostock 无股本列；tushare 增强时回填）

- [ ] **Step 4: 跑测试通过 + 回归**

Run: `uv run pytest tests/unit -k "baostock or schema" -v`

- [ ] **Step 5: 质量门禁停牌豁免 + 回测停牌拒单**

`src/lquant/data/quality/asserts.py`：断言上下文里排除停牌行（只影响断言，不影响入湖数据）：

```python
if "is_suspended" in df.columns:
    df_check = df.filter(~pl.col("is_suspended"))
else:
    df_check = df
```

`src/lquant/backtest/engine.py`：撮合前 `is_suspended=True` → 拒单 reason="suspended"。
`tests/unit/test_engine.py` 补：停牌日下单不成交。

- [ ] **Step 5b: 回归 + Commit**

Run: `make test && make lint`

```bash
git add -A && git commit -m "feat: baostock 日线扩列估值/状态字段 + 停牌保留与门禁/回测适配"
```

### Task 3: Checkpoint.unmark + 可复用逐批回填函数

**Files:**
- Modify: `src/lquant/data/ingest/checkpoint.py`（加 unmark）
- Modify: `src/lquant/data/ingest/daily.py`（拆出 `backfill_pool`）
- Test: `tests/unit/test_backfill_pool.py`（新建）

**Interfaces:**
- Produces: `Checkpoint.unmark(keys: list[str]) -> None`；
  `backfill_pool(pool: list[tuple[str, date]], start: date, on_progress: Callable | None = None) -> dict`
  返回 {"done": int, "failed": [{"symbol","reason"}], "rows": int}
- Consumes: 现有 write_daily / gate_daily / watchdog / FallbackProvider

- [ ] **Step 1: Checkpoint.unmark 失败测试**

```python
# tests/unit/test_backfill_pool.py
import polars as pl
import pytest
from lquant.data.ingest.checkpoint import Checkpoint

def test_checkpoint_unmark(tmp_path, monkeypatch):
    monkeypatch.setattr("lquant.core.config.get_settings", lambda: FakeSettings(tmp_path))
    cp = Checkpoint("t1")
    cp.mark(["a", "b"])
    cp.unmark(["a"])
    assert cp.is_done("b") and not cp.is_done("a")
```

（FakeSettings 为测试文件内定义的简单 stub：cache_dir 指向 tmp_path。）

- [ ] **Step 2: 实现 unmark**

checkpoint.py 加：

```python
    def unmark(self, retry_keys: list[str] | set[str]) -> None:
        self._data["done"] = sorted(self.done - set(retry_keys))
        self._flush()
```

- [ ] **Step 3: backfill_pool 失败测试（mock provider，不联网）**

```python
def test_backfill_pool_happy_and_early_stop(tmp_path, monkeypatch):
    # FakeProvider.daily_bars: 前 2 批返回 1 行/只，第 3 批起全抛 RuntimeError
    # 断言: done/failed/rows 正确; 连续 10 批全失败 → 提前停，不再调 provider
    ...
```

- [ ] **Step 4: 拆出 backfill_pool**

daily.py 重构（保持 backfill_daily 兼容签名，内部转调 backfill_pool）：

```python
BATCH = 200
SUB_BATCH = 20
EARLY_STOP_BATCHES = 10

def backfill_pool(pool, start, end=None, on_progress=None):
    """pool: [(symbol, end_date), ...] 逐批流式回填。

    每批: 拉数 → 缩批重试 → 质量门禁 → write_daily → on_progress(cb)
    连续 10 批全失败 → 早停 ({"early_stopped": True, ...})
    """
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "refactor: 拆出 backfill_pool 逐批回填函数（进度回调 + 早停）"
```

### Task 4: data_task 表 + 执行器

**Files:**
- Create: `src/lquant/data/ingest/tasks.py`
- Test: `tests/unit/test_data_tasks.py`（新建）

**Interfaces:**
- Produces:
  - `create_task(kind: str, params: dict) -> dict`（含前置校验，有 running 任务时抛 ValueError）
  - `execute_task(task_id: str) -> dict`
  - `retry_task(task_id: str) -> dict`
  - `get_task(task_id) -> dict | None` / `list_tasks(limit=50) -> list[dict]`
  - `mark_interrupted_on_startup() -> int`
- Consumes: Task 3 `backfill_pool(pool, start, on_progress)`；
  `SecurityRepo().all_symbols() / etf_symbols()`；`Checkpoint(f"daily:{task_id}")`

- [ ] **Step 1: 失败测试（状态机 + 池构建 + 前置校验）**

```python
# tests/unit/test_data_tasks.py
import pytest
from lquant.data.ingest import tasks

@pytest.fixture
def empty_db(tmp_path, monkeypatch):
    """内存/临时 DuckDB + security 表 2 只在市 + 1 只退市 + 1 只 ETF。"""
    ...  # monkeypatch reader/writer 指向临时库，塞入 security 行

def test_create_full_backfill_requires_delisted(empty_db, monkeypatch):
    # 删掉退市股后 create_task("full_backfill", {...}) 应抛 ValueError
    ...

def test_pool_building():
    # full: all_symbols(含退市) 按 delist_date 截断 end；daily: active + etf
    ...

def test_state_machine_and_early_stop(empty_db, monkeypatch):
    # create → execute(mock backfill_pool 连续失败) → status=failed
    ...
```

- [ ] **实现骨架（Step 2-4）**

```python
_DDL = """CREATE TABLE IF NOT EXISTS data_task (
    task_id VARCHAR PRIMARY KEY, kind VARCHAR, params JSON,
    status VARCHAR, phase VARCHAR,
    total_symbols INTEGER, done_symbols INTEGER,
    failed_symbols JSON, failed_detail JSON, rows_written INTEGER,
    started_at/finished_at TIMESTAMP, message VARCHAR)"""

def create_task(kind, params): ...
def _pool_with_ends(kind, start, end):
    """full: stocks=all−etf（delist 截断）+ etf；daily: active + etf。
    返回 [(symbol, end_date), ...]，退市股 end=min(end, delist_date)。"""
def execute_task(task_id): ...
def _execute_phases(task_id, task, progress_cb): ...  # stocks → etf 两个 phase
def retry_task(task_id):
    """unmark failed 的 checkpoint 后重新 execute（只补漏）。"""
def mark_interrupted_on_startup(): ...
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: data_task 表 + 数据任务执行器（状态机/前置校验/早停/retry）"
```

### Task 5: API /api/data/tasks + crosscheck 端点 + WS 兜底 + 启动标记

**Files:**
- Modify: `src/lquant/server/api/data.py`
- Modify: `src/lquant/server/ws.py`（job 不存在时查 data_task 兜底）
- Modify: `/api/data/tasks/{id}/retry` 归入 data.py
- Test: `tests/unit/test_api_data_tasks.py`（新建）

**Interfaces:**
- Consumes: Task 4 的 create_task/execute_task/retry_task/get_task/list_tasks；
  现有 `jobs.enqueue("lquant-ingest", execute_task, task_id)`；现有 run_crosscheck

- [ ] **Step 1: 失败测试（TestClient，拷贝临时库，同 test_api.py 模式）**

```python
def test_create_and_get_task(api_env):
    r = client.post("/api/data/tasks", json={"kind": "daily_update", "params": {"days": 5}})
    assert r.status_code == 202
    task_id = r.json()["task_id"]
    r2 = client.get(f"/api/data/tasks/{task_id}")
    assert r2.status_code == 200
    assert r2.json()["kind"] == "daily_update"

def test_409_when_task_running(api_env): ...
def test_422_full_backfill_no_delisted(api_env): ...
def test_retry_endpoint(api_env): ...
def test_crosscheck_endpoints(api_env): ...
```

- [ ] **Step 2: 实现 data.py 路由**

```python
@router.post("/tasks", status_code=202)
def create(req: TaskIn):           # → 409 有 running；422 前置校验失败
@router.get("/tasks")              # → list_tasks()
@router.get("/tasks/{task_id}")    # → get_task() or 404
@router.post("/tasks/{task_id}/retry", status_code=202)
def retry(task_id):                # → 409 有 running
@router.post("/crosscheck")        # {start?, end?, peers?, limit?} → summary
@router.get("/crosscheck/issues")  # → data_quality_issue 检索
```

创建即 `jobs.enqueue("lquant-ingest", execute_task, task_id)`，返回 202 + task_id。

- [ ] **Step 3: ws.py data_task 兜底（WS 断开时前端降级轮询同一端点，状态不丢）**

`job_progress` 中 `job is None` 分支：先查 `get_task(job_id)`（data_task 表），
命中则按 {job_id, status, progress: {done,total,phase}, done} 推送直至终态。

- [ ] **Step 3b: main.py 启动标记 interrupted**

main.py 启动钩子里加 `mark_interrupted_on_startup()`（Task 4 已实现），异常不阻塞启动。

- [ ] **Step 4: 回归 + Commit**

Run: `uv run pytest tests/unit/test_api_data_tasks.py -v && make test`

```bash
git add -A && git commit -m "feat: /api/data/tasks + crosscheck API + WS data_task 兜底 + 启动标记 interrupted"
```

### Task 6: settings 新增 crosscheck_peers + 对拍读运行时配置

**Files:**
- Modify: `src/lquant/core/settings_store.py`（SETTING_DEFS 加 crosscheck_peers）
- Modify: `src/lquant/data/ingest/crosscheck.py`（配置读取改 SettingsStore 优先）
- Test: `tests/unit/test_crosscheck_settings.py`（新建）

**Interfaces:**
- Produces: 设置项 `crosscheck_peers`（list，校验只允许声明 daily/etf_daily 的源）；
  `run_crosscheck` 的 peers/primary 解析顺序：SettingsStore 覆盖 > providers.yaml
- Consumes: 现有 SettingsStore / SETTING_DEFS / run_crosscheck

- [ ] **Step 1: 失败测试**

```python
# tests/unit/test_crosscheck_settings.py
def test_crosscheck_peers_setting_validation():
    # 非法源名 → SettingsStore.put 抛 ValueError
    # 合法：tushare,akshare（capability 校验在 put 的 choices 外另行校验）
    ...

def test_run_crosscheck_reads_runtime_config():
    # put crosscheck_peers=tushare 后 run_crosscheck 使用 tushare
    # 未配置 → 回退 providers.yaml crosscheck 节
    ...
```

- [ ] **Step 2: 实现**

settings_store.py SETTING_DEFS 加：

```python
    "crosscheck_peers": SettingDef(default=(), ty="list", label="对拍 peer 源（跨源印证）"),
```

crosscheck.py 配置解析：

```python
def _cfg() -> dict:
    """SettingsStore 覇盖 > providers.yaml crosscheck 节。主源 = providers_order[0]。"""
```

- [ ] **Step 3: 回归 + Commit**

Run: `make test && make lint`

```bash
git add -A && git commit -m "feat: crosscheck_peers 设置项 + 对拍读运行时配置（主源=providers_order 首位）"
```

### Task 7: sync daily 作业接执行器（全市场增量）

**Files:**
- Modify: `src/lquant/sync/manager.py`
- Test: `tests/unit/test_sync_daily_all.py`（新建）

**Interfaces:**
- Consumes: Task 4 `execute_task` / `create_task`
- Produces: sync `daily` 作业 params 支持 `market: "all"`，走 data_task 执行器

- [ ] **Step 1: 处理逻辑**

`run_job` 的 `kind == "daily"` 分支改为：
1. `params.market == "all"`（默认）→ `t = create_task("daily_update", {"days": ...})`
2. 调 `execute_task(t["task_id"])`，rows 取执行器返回
3. `params.market == "sentinel"` 保留旧 backfill_daily 路径（兼容）

- [ ] **Step 2: 测试**

```python
def test_sync_daily_creates_data_task(monkeypatch):
    # run_job(kind=daily, params={market: all}) 后 data_task 表多一行 daily_update
    ...
```

跑：`uv run pytest tests/unit/test_sync_daily_all.py -v`

- [ ] **Step 3: 回归 + Commit**

```bash
git add -A && git commit -m "feat: sync daily 作业接数据任务执行器（全市场增量）"
```

### Task 8: 前端 /data 页改造（任务区 + 失败下钻 + 覆盖度 + 数据源配置 + 跨源印证）

**Files:**
- Modify: `web/src/app/data/page.tsx`（整页改造）
- Test: `web/src/lib/__tests__/format.test.ts` 补任务状态文案映射

**Interfaces:**
- Consumes: Task 5 API（/api/data/tasks*、/crosscheck*）、Task 6 settings API

- [ ] **Step 1: 数据源配置区**

```tsx
// 主源下拉（= providers_order 拖排序首位，用现有 /settings PUT providers_order）
// peers 多选（GET /settings/providers 过滤 capability 含 daily/etf_daily 的源）
// PUT crosscheck_peers；保存后提示「下一次拉取即生效」
```

- [ ] **Step 1b: 任务状态文案映射单测（web/src/lib/format.ts 加 taskStatusText）**

```ts
export function taskStatusText(s: string): string {
  const m: Record<string, string> = {
    pending: "等待中", running: "拉取中", ok: "完成", partial: "部分完成",
    failed: "失败", interrupted: "已中断",
  }
  return m[s] ?? s;
}
```

- [ ] **Step 2: 任务区**

- 「全量回填」按钮 → 弹窗（起止日期选择，默认 2016-01-01 ~ 今天）→ POST /api/data/tasks
- 「立即增量」按钮 → POST {kind: daily_update, params: {days: 10}}
- 任务列表：状态徽章（taskStatusText + 颜色）、进度条 done/total、rows、耗时
- running 任务 2s 轮询 GET /tasks/{id}（WS /ws/jobs/{task_id} 同步推送，断连降级轮询）

- [ ] **Step 3: 失败下钻 + 跨源印证卡 + 覆盖度（保留现有 coverage 卡片）**

- 点 partial/failed 任务行展开 failed_detail 表（symbol/reason/重试按钮 → POST retry）
- 「跨源印证」卡：POST /crosscheck 触发按钮 + 最近 summary + 分歧明细表
  （symbol/field/primary/peer/偏差%）+ resolve 按钮

- [ ] **Step 4: 验证**

Run: `cd web && npm test`（format 单测）；`npm run build` 通过；`npm run dev` 起服页面手测

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(web): 数据管理页——任务进度/失败下钻/跨源印证/数据源配置"
```

### Task 9: 集成收尾——demo 全链路 + 覆盖度展示 + 验收

**Files:**
- Modify: `src/lquant/server/api/data.py`（coverage 湖统计带新列空值容忍）
- Test: `tests/integration/test_backfill_task_e2e.py`（新建）

- [ ] **Step 1: e2e 测试（demo provider，不联网）**

```python
def test_full_backfill_e2e(demo_env):
    # create_task(full_backfill) → execute_task → data_task=ok
    # 湖里出现 2016 起合成日线（含 9 新列）→ coverage 湖统计反映行数
    # retry 端点对 failed 补拉 → partial→ok
    ...
```

Run: `uv run pytest tests/integration/test_backfill_task_e2e.py -v`

- [ ] **Step 2: auto_crosscheck 钩子**

`tasks.py` execute 收尾（ok/partial 时）：`params.auto_crosscheck`（默认 true）→
窗口抽样对拍，结果记 message 字段（summary JSON 字符串）。

- [ ] **Step 3: 全量回归 + 验收**

Run: `make test && make lint`，uvicorn 起服手测 /data 页全流程

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "test: 数据任务 e2e + auto_crosscheck 钩子"
````

