# 因子评价任务队列调研 & 与「因子评价页面」前后端复用方案

> 调研分支：`worktree-factorevalq`（基于 `main` @ `7aa9855`）
> 只读调研，未改动任何生产代码。所有结论均带 `文件:行` 证据，关键结论已对运行中的 API 实测复核。

---

## 0. TL;DR

1. **评价任务确实"有队列"，但不在你看得见的地方**：入口是 `POST /api/factors/evaluate`，进 `enqueue("lquant-mining", ...)`。它和**因子挖掘共用同一条队列**，唯一可见处是「任务管理 → 因子挖掘」tab（`/tasks`）。
2. **当前运行环境里根本没有真队列**：Redis 不可用 → 走本地降级分支，每个评价任务在 API 进程内**各起一个 daemon 线程立即执行**（`jobs.py:404-444`）。`lquant-mining` 只是一个字符串标签。
3. **即使 Redis 可用，也没有任何 worker 订阅 `lquant-mining`**（`lquant.sh:524-526`、`scripts/dev.sh:18`、`Makefile:102`、`monitor/worker.py:65-66` 全部只订阅 default/ingest/backtest）。此时评价任务会永远停在 `queued`，并且因为确定性 job id 被 409 永久卡死。
4. **编辑器页是四处分叉里最特殊也最破的一处**：唯一用 REST 轮询、唯一不用 `useJobStream`、唯一读错响应契约（实测：结果永远显示 `—`）。
5. **可复用资产已经齐了**：`useJobStream` + `ProgressBar` + `factors/shared.tsx` 的结果面板 + 任务中心的取消端点。缺的是**一层公共 hook 和结果视图**，把它们从「因子库/详情页」搬到编辑器。

结论一句话：**不需要新建一整套任务管理，只需把编辑器的评价收敛到因子评价页已经在用的那条链路上，并让任务中心能看见"在评哪个因子"。**

---

## 1. "任务队列在哪里"——完整链路

### 1.1 入队

四处前端都打同一个端点（这点已经是复用的）：

```
POST /api/factors/evaluate   # factors.py:910-928，202 返回 {job_id, status}
  └─ _check_formula_supported(formula)        # 入队前同步校验，坏公式 422 而不是跑挂
  └─ jid = _job_result_id(factor)             # 确定性 id: f"factor-eval-{factor}"  factors.py:905-907
  └─ 若同 id 已在 queued/started → 409          # factors.py:923-925
  └─ enqueue("lquant-mining", _run_evaluate_job, req.model_dump(),
             job_id=jid, name="因子评价")        # factors.py:926-927
```

任务体 `_run_evaluate_job`（`factors.py:889-902`）算完后 `save_result("factor-eval-{factor}", "factor_eval", params, m)` 落 `job_results` 表（`eval_results.py:40-47`）。

### 1.2 队列实现（`src/lquant/server/jobs.py`）

`QUEUES`（`jobs.py:19-20`）：`lquant-default / lquant-ingest / lquant-backtest / lquant-mining / lquant-qlib / lquant-ml`。

| 队列 | 谁入队 |
|---|---|
| `lquant-ingest` | data 任务、同步（`data.py:267,444,482,577`；`task_center.py:180`） |
| `lquant-backtest` | 参数扫描（`backtests.py:307`，无 name）、策略回测（`backtests.py:464`，name=策略回测） |
| **`lquant-mining`** | **因子评价**（`factors.py:926`）+ **因子挖掘**（`factors.py:1314`） |
| `lquant-qlib` | Qlib 导出 / 工作流（`qlib.py:85,231`） |
| `lquant-ml` | ML 训练 / 滚动重训（`ml.py:247,256`） |
| `lquant-default` | 应用代码从不入队 |

两种执行模式（`jobs.py:384-444`）：

- **Redis 可用** → RQ `Queue(queue).enqueue(...)`。
- **Redis 不可用（当前就是）** → `LocalJob` + `threading.Thread(...).start()`，**无并发控制、无排队**。同时把入队/终态写 `job_record` 表（`jobs.py:409,437-440`），这是任务中心历史能跨重启的原因。

> 实测：`_redis_available(ttl=0) == False`；`.run/` 只有 `api.pid`/`web.pid`，没有 `worker.pid`；`GET /api/tasks?kind=factor` 返回的评价任务状态是 `finished`。三处互证：**当前评价任务跑在 API 进程内的临时线程里，没有任何"队列"。**

### 1.3 当前环境的真相

```
因子编辑页 ─┐
因子库快速评价 ─┤
因子详情页 ─┼─► POST /api/factors/evaluate ─► enqueue("lquant-mining", ...)
因子挖掘页 ─┘                                     │
                                          Redis 不可用（现状）
                                                  └─► 每任务一个 daemon 线程，立即跑
                                          Redis 可用（未来）
                                                  └─► RQ lquant-mining ─► ❌ 无 worker 订阅 → 永久 queued
```

### 1.4 唯一的"队列视图"

「任务管理」页 `/tasks` → 因子挖掘 tab → `FactorPanel`，数据来自 `GET /api/tasks?kind=factor`（`task_center.py:117-119` 把 `factor` kind 映射到 `lquant-mining`）。

实测返回（本机 API）：

```json
[
 {"id":"factor-eval-BETA10_copy","kind":"factor","name":"因子评价","status":"finished",
  "state":"finished","params":{},"progress":{"done":95,"total":100,"phase":"汇总"}},
 {"id":"factor-eval-pct_change_20","kind":"factor","name":"因子评价", ... "params":{}},
 {"id":"06b3d12dc9b7","kind":"factor","name":"因子挖掘","params":{}}
]
```

**注意 `params` 恒为 `{}`**——这正是"很难管理"的直接原因（见 §3.3）。

---

## 2. 前端现状：四处发起评价，三条不同实现

| 页面 | 入队 | 跟踪方式 | 进度 UI | 结果渲染 | 取消 | 回任务中心 |
|---|---|---|---|---|---|---|
| `factors/page.tsx` 快速评价 | `POST /factors/evaluate`（`:296`） | WS `useJobStream`（`:125`） | `ProgressBar`（`:510-517`） | 指标条 + 评级/稳健/配方 + TopN + 8 图 + `report_url` | ❌ | ❌ |
| `factors/[name]/page.tsx` 详情 | 同上（`:217-219`） | WS `useJobStream`（`:85`） | `ProgressBar`（`:341-349`） | 12 指标 + 全套图表 + `report_url` + 历史报告 | ❌ | ❌ |
| **`factors/editor/page.tsx` 编辑器** | 先 `POST /factors` 保存，再 `POST /factors/evaluate`（`:154-157`） | **REST 轮询** 3s × 100 次（`:29,31,85,93`） | 纯文字（`:342-346`） | 3 个 Stat + 累计 IC 图 | ❌ | 仅文字提及（`:81`，非链接） |
| `factors/mine/page.tsx` 挖掘 | `POST /factors/mine/run`（`:83-84`） | WS，但订阅了错的 id（`:51`） | `ProgressBar` | 幸存因子表 | ❌ | ❌ |
| `tasks/panels/FactorPanel.tsx` 任务中心 | `POST /factors/mine/run`（`:80`） | SWR 2s/15s（`:28-31`） | 行内 WS `LiveProgress` | 「查看结果」→ `result.report_url` | ✅（`:40-52`） | 本身即是 |

结论：**因子评价页已经把"评价 → 进度 → 结果"整条链路跑通了**（WS + 进度条 + 全套结果组件）。编辑器是唯一没接进去的，而且自己实现了一遍还实现错了。

---

## 3. 发现的具体缺陷（按严重度）

### 3.1 🔴 编辑器评价结果永远出不来（契约错配，已实测）

编辑器 `get<EvalMetrics>('/factors/evaluate/{id}')`（`editor/page.tsx:85`）按**平铺结构**读：

```ts
type EvalMetrics = { ic_mean?; rank_ic_mean?; icir?; series?: { ic?: { dates; cum_ic } } };
```

但后端返回的是**信封**（`eval_results.py:50-68`，`factors` 路由是普通 `APIRouter`，无 `EnvRoute`，不封套也不解包）：

实测 `GET /api/factors/evaluate/factor-eval-BETA10_copy`：

```
TOP KEYS:    ['job_id','ts','kind','params','result']
result keys: ['factor','formula','n_samples','ic','rank_ic_mean','long_short',
              'monotonicity','half_life','suggested_rebalance','rating','robustness',
              'steps','covariates','excess','annual_turnover','top_n','style_corr',
              'report_url','outlier','errors','series']
ic_mean flat? None          # ← 编辑器读的就是这个，undefined
result.series? True
ic keys:    ['mean','ir','t_stat','positive_rate','ic_gt_002_rate','t_stat_nw','ic_autocorr']
series.ic:  ['dates','ic','rank_ic','cum_ic']
```

**三重错配**：

1. 编辑器把信封当结果读了（应读 `r.result`）；
2. `ic_mean` / `icir` 字段根本不存在（实际是 `ic.mean` / `ic.ir`）；
3. `series` 在 `result.series` 下。

后果：编辑器的「IC 均值 / RankIC 均值 / ICIR」恒为 `—`，累计 IC 图恒为空。而 `FactorPanel.tsx:59-62` 的 `r.result.report_url` 读法是对的，说明这只是编辑器漏改。

> 对照：WS 帧的 `result` 是 `_run_evaluate_job` 的返回值即扁平 metrics（`ws.py:75-76` → `job.result`）。所以**切到 `useJobStream` 还不够，字段名 `ic_mean/icir` 仍要改成 `ic.mean/ic.ir`**。

### 3.2 🔴 编辑器是唯一 REST 轮询方，刷新/离开即失联

`POLL_MS=3000` × `MAX_POLLS=100` ≈ 5 分钟（`editor/page.tsx:29-31`），超时后才提示「请到任务管理查看」——但那个页面**显示不出是哪个因子**（见 3.3）。`evalJob` 只存在组件 state（`:57`），刷新页面即丢失。

而 `useJobStream` 天然支持刷新重连（WS 侧查 `job_record` 兜底，`ws.py:53-66`），且带 `interrupted` 语义，是更正确的做法。

### 3.3 🔴 任务中心看不见"在评哪个因子"

`_job_items` 对**所有队列类任务**硬编码 `"params": {}`（`task_center.py:97`）。于是：

- `FactorPanel` 的详情列 `String(t.params?.generator ?? '—')`（`:144`）**恒为 `—`**；
- 名称列只显示 `t.id.slice(0, 8)`（`TaskTable.tsx:87`），评价任务的 id 前缀全是 `factor-e`，**一眼无法区分**；
- 无法按因子分组、无法从任务行跳回因子/编辑器。

而因子名其实**已经落库了**：`job_results.params` 里就有 `{factor, formula, ...}`（`eval_results.py:52`）。`list_results(kind, limit)`（`eval_results.py:71-84`）已经写好却**全仓库无人调用**——现成的补数入口。

### 3.4 🟠 确定性 job id 的副作用：覆盖 + 409 陷阱

`_job_result_id = f"factor-eval-{factor}"`（`factors.py:905-907`）同时用作队列 job id、结果主键、报告文件名（`factors.py:627`）：

- `save_result` 是 `INSERT OR REPLACE`（`eval_results.py:44-47`）→ 同因子重跑**静默覆盖**上一次结果与报告，没有历史；
- 同因子并发 → 409（`factors.py:923-925`），这本身合理；但**若 Redis 可用而 `lquant-mining` 无 worker**，第一次入队就永久 `queued`，于是**该因子被永久 409 锁死**，只能手动清 RQ。
- 本地模式下 409 守卫会因进程重启 / `_LOCAL_JOBS` 淘汰（上限 200，`jobs.py:265`）而失效。

### 3.5 🟠 挖掘任务的 job id ≠ 返回的 task_id，WS 永远 not_found

`mine/run` 返回 `task_id = run_id`（`factors.py:1312-1318`），但 `enqueue(...)` **没传 `job_id`**（`factors.py:1314-1315`），`enqueue` 自己生成 `uuid4().hex[:12]`（`jobs.py:371`）。前端 `useJobStream(mineJob)` 订阅的是 `run_id`（`mine/page.tsx:51,85`），`ws.py:67` 只能回 `{"status":"not_found","done":true}` → 页面显示「挖掘任务异常结束」，幸存因子不回流。前端测试 mock 掉了 `useJobStream`（`mine/page.test.tsx`）所以没暴露。

修法一行：`enqueue("lquant-mining", _run_mine_job, ..., run_id=run_id, job_id=run_id)`。

### 3.6 🟠 挖掘任务无进度、不可取消；评价任务排不上 worker

- `_run_mine_job(agent_name, generator, n, run_id)`（`factors.py:1250`）**没有 `progress`/`cancel_check` 形参**，而 `enqueue` 是按签名注入回调的（`jobs.py:376-382`）→ 挖掘既无进度条也无法协作式取消。
- `lquant-mining` 无 worker 订阅（§1.2）。且即便加了，评价与挖掘共用一个 worker 池会互相饿死。
- 任务中心的取消按钮只在 `state === 'running'` 时渲染（`FactorPanel.tsx:156`），**queued 的取消能力（后端支持 `jobs.py:337-341`）在 UI 上被挡掉了**——恰恰是最需要取消的那种情况。

### 3.7 🟡 其他

- `TaskTable` 的 `LiveProgress`（`TaskTable.tsx:10-20`）**未导出**，无法在别的页面复用行内进度。
- `FactorPanel.test.tsx:24-33` mock 了 `params:{agent,generator,n}`，断言了后端**并不提供**的行为——测试与实现对不上。
- retry 端点显式只支持 data（`task_center.py:164-165`），factor/backtest/qlib/ml 都无重试。
- `TaskTable` 有 `actionsOf` 注入点（`TaskTable.tsx:49`），扩展成本很低。

---

## 4. 复用方案

设计原则：**不新增第二套任务体系**。因子评价页已经有一套可用的（WS + 进度 + 结果组件 + 取消端点），把它下沉为公共层，编辑器改接即可。

### 4.1 后端

**B1. 队列可运行（P0）**

把 `lquant-mining` 加进 worker 订阅，或（更好）拆成两条队列，避免评价/挖掘互相饿死：

```
lquant.sh:524-526   rq worker lquant-default lquant-ingest lquant-backtest [lquant-mining]
scripts/dev.sh:18   同上
Makefile:102        同上 / 或补 `make worker-mining`
monitor/worker.py:65-66  可选：GENERAL_QUEUES 增加 lquant-mining，或新增 MINING_WORKER 数配置
```

同时保留本地降级可用（现状）。

**B2. 统一结果读取契约（P0）**

两个选项：

- A：`GET /factors/evaluate/{job_id}` 直接返回扁平 metrics（即现在的 `result`），另留 meta 端点给需要 `params` 的调用方。会改后端契约，`test_factor_eval_task.py:98` 已断言信封 `body["result"]`，属破坏性变更。
- **B（P0 实际采用）**：保留信封不动，前端统一「评价结果走 WS」——编辑器改用与因子库/详情页相同的 `useJobStream`，彻底不再读 REST 信封；`FactorPanel.openResult` 保持读 `r.result.report_url` 的既有正确写法。零后端破坏，且顺带拿到进度、失败态与刷新重连。

无论选哪个，**字段名 `ic_mean/icir` → `ic.mean/ic.ir` 必须改**（§3.1）。

**B3. 让任务中心看得见因子（P1，可管理性的核心）**

在 `task_center._job_items`（`task_center.py:79-99`）里按 kind 回填 params：

```python
# kind == "factor"：评价任务从 job_results.params 取 {factor, formula}；
#                   挖掘任务从 factor_mining_run 按 run_id 取 {agent, generator, n}
```

`list_results(kind, limit)`（`eval_results.py:71`）已经现成。补一个 `list_results_by_kind` / 或用 `get_result(job_id)` 单条回填即可（量小）。顺带：

- `TaskItem` 增加 `subtype?: 'factor_eval' | 'factor_mine'`，把 `FactorPanel.tsx:147` 的 `t.name === '因子评价'` 中文字符串判断替换掉（名称是自由文本，本就不可靠）。
- 评价任务的显示名带上因子名（如 `因子评价 · BETA10_copy`），或让前端从 `params.factor` 渲染。

**B4. 修 job id 语义（P1）**

- 挖掘：`enqueue(..., job_id=run_id)`（§3.5，一行）。
- 评价：把「队列 job id」与「最新结果 key」解耦，消除 409 陷阱：
  - 队列 `job_id = factor-eval-{factor}-{ts}`（唯一，可重跑）；
  - 结果仍写 `factor-eval-{factor}`（保留"每因子最新一次"语义），或改为保留最近 N 次形成历史。
  - 同步放宽/替换 `factors.py:923-925` 的 409 守卫。

**B5. 挖掘补进度与取消（P2）**

给 `_run_mine_job` 加 `progress=None, cancel_check=None` 形参，在漏斗各阶段调 `progress(...)`、批间查 `cancel_check()`——`enqueue` 会自动注入（`jobs.py:387-390`），与评价完全对齐。

### 4.2 前端

**F1. 抽公共 hook `useFactorEval`（P0/P1）**

新文件建议 `web/src/app/factors/useFactorEval.ts`（与页面同域，避免污染 `lib/`）：

```ts
export function useFactorEval() {
  const [jobId, setJobId] = useState<string | null>(null);
  const stream = useJobStream<FactorEvalResult>(jobId);   // 进度+结果+错误+done 全有了

  const start = useCallback(async (params: EvaluateIn) => {
    const r = await post<{ job_id: string }>('/factors/evaluate', params);
    setJobId(r.job_id);
    return r.job_id;
  }, []);

  const cancel = useCallback(async () => {
    if (jobId) await post(`/tasks/factor/${encodeURIComponent(jobId)}/cancel`, {});
  }, [jobId]);

  return { jobId, start, cancel, ...stream };
}
```

编辑器用它**整体替换** `evalJob` state + `:69-99` 的轮询 effect（这段代码可直接删掉约 30 行）。因子库/详情页也可逐步迁移，消除两处重复的 result 回填 effect。

**F2. 抽 `<FactorEvalProgress>`（P1）**

`ProgressBar` + phase + 取消按钮 + `<Link href="/tasks">任务管理</Link>`，统一三处各不相同的进度呈现（编辑器目前甚至连进度条都没有）。

**F3. 抽 `<FactorEvalResultView result>`（P2）**

把 `factors/page.tsx:545-721` 那套（指标条 + Rating/Robustness/Recipe + 超额/TopN + 8 组图表）用 `factors/shared.tsx` 里**已有的** `RatingPanel / RobustnessPanel / RecipeSteps / TopNTable / QuantileCharts / *Option` 工厂抽成一个组件，编辑器直接升级成"和因子评价页一样的完整结果"。这是本方案里复用的最大收益点。

**F4. 编辑器内嵌任务列表（P2，可选）**

把 `TaskTable` + `LiveProgress`（需 export）提到 `components/tasks/`，在编辑器底部放「本因子的评价任务」小表，数据用 `useSWR('/tasks?kind=factor')` 过滤 `params.factor === name`（依赖 B3 的 params 回填），复用 `StateBadge` / `paramsBrief` / `createdText`。

**F5. 编辑器 URL 持久化（P2）**

把 `jobId` 写进 query（`?eval=<jobId>`），配合 WS 的刷新重连能力，切页/刷新后仍能看到进度与终态。

### 4.3 分期落地建议

| 阶段 | 内容 | 价值 | 风险 |
|---|---|---|---|
| **P0** | B1 加 worker 订阅；B2 统一结果契约 + 修字段名；修 3.5 的 `job_id=run_id` | 让评价真的能跑、编辑器结果能显示 | 低，改动集中在 3 个文件 |
| **P1** | B3 params 回填 + subtype；B4 job id 解耦；F1 hook + F2 进度组件；queued 也能取消 | 任务中心真正"可管理"：看得见因子、能取消、能跳回 | 中，涉及 API 契约扩展 |
| **P2** | B5 挖掘进度/取消；F3 结果视图抽取；F4 内嵌任务表；F5 URL 持久化 | 前后端全量复用，消除三处重复 | 中，纯增量 |

### 4.4 测试建议

- 后端：`_job_items` 回填 params 的单测（对照 `FactorPanel.test.tsx` 现在 mock 的错误契约，先修测试）；`enqueue` 的 `job_id` 断言；worker 队列覆盖断言。
- 前端：**新增 `web/src/app/factors/editor/__tests__/page.test.tsx`**（当前只有 `useFactorEditor.test.ts`，评价路径零覆盖）——用真实信封响应体断言 `ic.mean/rank_ic_mean/ic.ir` 渲染正确，正好能卡住 §3.1 这类回归。
- `useJobStream` 已有测试（`lib/__tests__/streaming.test.ts`）可直接复用。

---

## 5. 关键文件索引

| 主题 | 位置 |
|---|---|
| 队列/入队/取消/进度注册 | `src/lquant/server/jobs.py:19,356,329,479`；`progress.py` |
| 评价端点与任务体 | `src/lquant/server/api/factors.py:889-939` |
| 结果落库（信封来源） | `src/lquant/server/eval_results.py:40-84` |
| 任务中心归一（params 恒空处） | `src/lquant/server/api/task_center.py:79-125` |
| WS 协议 | `src/lquant/server/ws.py:28-88` |
| 前端流式 hook / 进度条 | `web/src/lib/streaming.ts:31`；`web/src/components/ProgressBar.tsx:4` |
| 编辑器评价（错配处） | `web/src/app/factors/editor/page.tsx:29-31,57,69-99,148-163,342-357` |
| 已跑通的评价页（复用来源） | `web/src/app/factors/page.tsx:125,235-252,510-521,534-721` |
| 结果面板组件库 | `web/src/app/factors/shared.tsx` |
| 任务中心面板/表 | `web/src/app/tasks/panels/FactorPanel.tsx`；`web/src/app/tasks/TaskTable.tsx` |
| worker 启动 | `lquant.sh:524-526`；`scripts/dev.sh:18`；`Makefile:102`；`src/lquant/monitor/worker.py:65-66` |

---

## 附：复现命令

```bash
# 当前是否走了本地降级（无真队列）
.venv/bin/python -c "import sys;sys.path.insert(0,'src');\
from lquant.server.jobs import _redis_available;print(_redis_available(ttl=0))"

# 任务中心看到的评价任务（注意 params 恒为 {}）
curl -s "http://127.0.0.1:8000/api/tasks?kind=factor&limit=20"

# 编辑器实际拿到的结果形状（信封，顶层没有 ic_mean）
curl -s "http://127.0.0.1:8000/api/factors/evaluate/factor-eval-BETA10_copy"
```

---

## 6. P0 实施记录（本分支已落地）

P0 只做「让评价真的能跑 + 编辑器结果能显示」，未动任务中心的可管理性（B3/B4 仍属 P1）。

| # | 改动 | 文件 | 说明 |
|---|---|---|---|
| 1 | worker 订阅 `lquant-mining` | `scripts/dev.sh:17-21`、`Makefile:101-102`、`lquant.sh:523-527` | 原注释写着 factor 队列却漏订阅；补上后 Redis 模式下评价/挖掘才真正被执行 |
| 2 | 评价结果改走 WS（与因子库/详情页同一机制） | `web/src/app/factors/editor/page.tsx` | 删除 3s×100 的 REST 轮询；`EvalMetrics` 改为后端真实扁平结构；字段名 `ic_mean→ic.mean`、`icir→ic.ir`；补 `ProgressBar`、报告外链、任务管理入口；失败/`not_found` 立即如实报错 |
| 3 | 挖掘 `job_id = run_id` | `src/lquant/server/api/factors.py:1312-1317` | 响应 `task_id` 从此就是队列 job id，前端 `useJobStream(task_id)` 不再收到 `not_found` |
| 4 | 回归测试 | `web/src/app/factors/editor/__tests__/page.test.tsx`（新增，5 例）、`tests/unit/test_api_cov_factors.py::test_mine_run_async_placeholder`（补 job id 断言） | 编辑器评价路径此前**零测试覆盖**；新测试用真实 WS 帧形状锁住字段读取，旧实现下必然失败 |

验证：

- `web`: `npx vitest run` → 89 files / 562 tests 全绿；`npx tsc --noEmit` 通过。
- `backend`: `test_api_cov_factors.py`、`test_factor_eval_task.py`、`test_api_factor_crud.py`、`test_factor_edit_api.py`、`test_monitor_cli.py`、`test_monitor_queries_gap.py` 全绿。

已知局限（留给 P1）：

- `lq worker` supervisor（`monitor/worker.py:65-66`）仍不含 mining 组；`lquant.sh` 靠并行的普通 `rq worker` 覆盖。挖掘 worker 与 ingest 同进程，长挖掘可能阻塞数据任务，建议 P1 拆独立 mining worker 组。
- 编辑器仍不显示历史评价、无取消按钮（P1 的 F1/F2/F4）。
- 任务中心依旧显示不出因子名（P1 的 B3）。

