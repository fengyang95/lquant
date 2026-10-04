# qlib × AlphaPurify 借鉴清单与 lquant 移植计划

- 日期：2026-10-03
- 上游调研报告：
  - [`docs/research/qlib/00-qlib-integration-proposal.md`](qlib/00-qlib-integration-proposal.md)（qlib 0.9.7）
  - [`docs/research/alphapurify/00-alphapurify-borrow-and-xval.md`](alphapurify/00-alphapurify-borrow-and-xval.md)（AlphaPurify 1.0.6）
- 本文作用：把两份调研**合并去重**成一张借鉴清单，并落成**可执行的分期移植计划**（含涉及文件与验收标准）。

---

## 0. 结论摘要

### 0.1 两个上游的定位完全不同，借鉴方式也应不同

| | qlib | AlphaPurify |
|---|---|---|
| 本质 | **全流程方法论参照系**（数据→模型→组合→执行→服务化） | **因子清洗/评价的同构实现**（Polars，与 lquant 同一技术栈） |
| 与本仓关系 | 异构（自有 bin 格式 + MLflow + 重依赖） | 同构（Polars，同样做 IC/分层/预处理） |
| 可验证性 | 只能做「结果对拍」（需导出数据） | **可直接逐日位级对拍**（已实测 `max\|Δ\|=0`） |
| 工程成熟度 | 维护模式，测试薄 | **自带测试 2/3 失败**，API 契约与文档不可信 |
| 正确借鉴方式 | 借**架构语义**，隔离 venv 做交叉验证 | 借**算法口径**，隔离 venv 做位级对拍 |

### 0.2 三条总原则

1. **借语义，不借运行时**：qlib 与 AlphaPurify 都不进主 venv（延续 ADR-11 与 `.venv-qlib` 隔离思路）。
2. **借算法，不借契约**：AlphaPurify 的 `get_methods()` 列出的方法名/参数名都不可直接调用（`random_forest`、`neutralizer_cols` 均实测失败），只能借它的**数学口径**。
3. **交叉验证常态化**：不是一次性调研，而是把「第三方对拍」变成可重复的一键脚本（AlphaPurify harness 已落地）。

### 0.3 一页总表

| # | 借鉴项 | 来源 | 移植形态 | 优先级 | 成本 |
|---|---|---|---|---|---|
| 1 | 基准/超额收益打通 | qlib | 原生重实现 | **P0** | 低 |
| 2 | 特征预处理 fit/infer 纪律 | qlib | 原生重实现 | **P0** | 中 |
| 3 | ML 生产化（任务中心 + 模型注册表 + 滚动重训/每日推理） | qlib | 原生重实现 + 工程化 | **P0** | 中高 |
| 4 | MAD 口径文档化 + 零方差语义 | AlphaPurify（交叉验证暴露） | 文档 + 自省 | **P0** | 极低 |
| 5 | 风险模型（收缩/结构化协方差） | qlib | 原生重实现 | P1 | 中 |
| 6 | 基准相对组合优化（TE 约束） | qlib | 原生重实现 | P1 | 中 |
| 7 | 截面快照 `trace()` | AlphaPurify | 原生重实现 | P1 | 中 |
| 8 | 纯暴露 / 组合暴露双视角归因 | AlphaPurify | 原生重实现 | P1 | 中 |
| 9 | Alpha360 因子集 | qlib | 原生重实现 | P1 | 低 |
| 10 | 稳健去极值（huber / rankgauss） | AlphaPurify | 原生重实现 | P1 | 低 |
| 11 | 多 horizon IC 并行 + overnight 切分 | AlphaPurify | 原生重实现 | P2 | 低 |
| 12 | 算子补齐 | qlib | 原生重实现 | P2 | 低 |
| 13 | 预处理方法扩容（boxcox/yeo_johnson/rolling/EWMA） | AlphaPurify | 原生重实现 | P2 | 低 |
| 14 | 多 seed / 集成 | qlib | 原生重实现 | P2 | 低 |

---

## 1. 借鉴清单（按主题合并去重）

### 1.1 度量与口径

| 项 | 来源 | 现状 | 要做什么 |
|---|---|---|---|
| **基准/超额收益** | qlib `benchmark` + `risk_vs_benchmark` | `backtest/attribution.py::risk_vs_benchmark` 有 TE/IR/excess **函数**，但引擎 `metrics.py` 未纳入、无基准序列来源；`config/qlib/workflow_alpha158_lgbm.yaml:16` 仍用 `SH600000` 机械代理 | 指数日线入库 → 回测默认基准可配 → metrics 增加 excess/TE/IR → qlib workflow 用真实指数 |
| **MAD 口径文档化** | AlphaPurify 对拍暴露 | `describe()` 只给 `params={'n': 5.0}`，**不说 n 要乘 1.4826**；与 AlphaPurify（`med±n*MAD`，默认 3）同名不同义，实测默认差 `max\|Δ\|=3.04` | 在 docstring/`describe()`/前端 UI 写明口径 |
| **零方差截面语义** | AlphaPurify 对拍暴露 | lquant `_safe_std` 兜底 1.0 → 静默返回 0；AlphaPurify 返回 null | 显式写明语义（并在 `describe()` 暴露） |
| **t 检验 p 值** | AlphaPurify | lquant 有 `t_stat` + Newey-West `t_stat_nw`，无 p 值 | 可选：补 p 值（lquant 已领先，非必须） |

### 1.2 特征与预处理

| 项 | 来源 | 要做什么 |
|---|---|---|
| **fit/infer 纪律** | qlib `DataHandlerLP` 的 `shared/infer/learn processors` + 只在 `fit_start~fit_end` 上 fit | 引入 `Processor.fit(train)/transform(any)`；`learn` 段 fit、`valid/test` 只 transform；加**泄漏哨兵测试** |
| **稳健去极值** | AlphaPurify `huber` / `rankgauss` | 补两个方法（A 股重尾/一字板场景比 MAD 更稳） |
| **方法扩容** | AlphaPurify `boxcox`/`yeo_johnson`/`rolling_*`/`EWMA`/`volatility_scaling` | P2 按需补（lquant 现有 18 个 vs AP 42 个，缺时序滚动类） |

> 注：lquant 的**截面**预处理是因果的（逐日 `by=trade_date`），这块没问题；缺的是**可 fit 的时序参数**与**非树模型所需的标准化**。

### 1.3 研究与生产衔接（当前最大断点）

**事实**：`run_ml_pipeline`（`research/ml/backtest.py:88`）目前**只被测试和文档调用**——没有 CLI、没有 API、没有队列。`ml_run` 表有指标但**无 artifact 列**。ML 从「研究脚本」到「每天出信号」之间是断的。

| 项 | 来源 | 要做什么 |
|---|---|---|
| **模型注册表** | qlib `Recorder` + `save_objects` | `ml_run` 增 `artifact_path/model_version/stage`（或新建 `ml_model`）；运行记录 ↔ 模型文件可回放 |
| **滚动重训 + 每日推理编排** | qlib `workflow/online/OnlineManager` + `RollingGen`（**注意**：`contrib/online` 是死代码） | 按频率在 `walk_forward_splits` 上重训 → 落 artifact → 记录 → 生成每日信号；失败回滚到上一个可用版本 |
| **ML 任务中心化** | lquant 既有 task center 模式 | 新增队列 `lquant-ml`、`kind=ml`、`server/api/ml.py`、`lq ml …` CLI、前端面板 |

### 1.4 组合与风险

| 项 | 来源 | 现状 | 要做什么 |
|---|---|---|---|
| **风险模型** | qlib `model/riskmodel/`（`ShrinkCovEstimator` LW/OAS、`POETCovEstimator`、`StructuredCovEstimator` PCA/FA） | 无 | 在 `portfolio/` 增协方差估计（收缩/结构化因子模型） |
| **基准相对优化** | qlib `contrib/strategy/optimizer/EnhancedIndexingOptimizer` | 只有 equal/mcap/ic_weight/inverse_vol/min_variance/risk_parity/HRP | 增「约束跟踪误差下最大化预期超额」的优化器 |

### 1.5 诊断与白盒（AlphaPurify 的强项，lquant 明显缺）

| 项 | 来源 | 价值 |
|---|---|---|
| **截面快照 `trace()`** | AlphaPurify `FactorAnalyzer.trace(period, date, bins, position)` | 给定「日期+方向+分箱」回看持仓权重与收益明细——排查「某天净值跳变是哪几只票」的最快路径；lquant 的 HTML 报告做不到这个粒度 |
| **纯暴露 / 组合暴露双视角** | AlphaPurify `PureExposures`（`w_i=f_i/Σ\|f_i\|`）/ `PortfolioExposures` | 「信号本身载荷在什么上」vs「组合表现得像什么」；单看组合暴露会把两种来源混在一起 |
| **overnight 切分** | AlphaPurify `overnight=on/off/only` | 判断「信号是不是靠跳空赚钱」 |
| **多 horizon 并行 IC** | AlphaPurify 对 (1,5,10) 并行 | lquant 是循环；已有 `forward_return_matrix`，并行化直接 |

### 1.6 因子集

| 项 | 来源 | 说明 |
|---|---|---|
| **Alpha360** | qlib `Alpha360DL`（实测 360 特征） | lquant 有 Alpha158 原生实现（158），无 Alpha360；照 `factors/qlib_alpha.py` 的「对照源码重实现 + 抽样对拍」范式补 |

### 1.7 明确不借鉴（合并）

| 项 | 来源 | 理由 |
|---|---|---|
| qlib 回测做执行真源 | qlib | 缺 A 股细则（印花税区间/per-instrument T+N/分板/逐日 ST/退市核销），两套规则漂移 |
| nested/日内执行、RL、DDG-DA | qlib | 与日频选股定位不匹配；RL 还锁 `numpy<2` |
| MLflow 全量实验追踪 | qlib | `ml_run` 自建表已够；MLflow 是 recorder 核心依赖且版本敏感 |
| qlib 模型 zoo 进主 venv | qlib | 与 ADR-11 冲突；只允许 `.venv-qlib` 内按需对照 |
| 引 AlphaPurify 进主 venv | AlphaPurify | 依赖重（plotly/pandas/sklearn/scipy/duckdb）；继续隔离 |
| Plotly 报告 | AlphaPurify | lquant 自包含 HTML 零外部依赖，换 Plotly 是净增依赖 |
| pandas 作内部契约 | AlphaPurify | lquant 全 Polars + Rust，回退是倒退 |
| 注册表与 if/elif 分发分离 | AlphaPurify | 会重演「列得出、调不到」；lquant 单一真源更好 |
| 把上游 tests/examples 当规格 | AlphaPurify | 实测自带测试 2/3 失败、examples keyword 写法 `TypeError` |

---

## 2. 移植计划

> 分期原则：**先修度量与正确性，再做工程化，最后扩能力**——否则会把错误的数字自动化。
> 每期结束都要有可验证的出口标准；详细 TDD 任务书（Task/Step 级）在开工前补到 `docs/superpowers/plans/`。

### Phase 0 — 已完成基线（不需移植，作为前提）

- qlib 隔离链路：`qlib_io/{export,runner,interpreter,store}.py` + `/api/qlib/*` + 前端面板 + `config/qlib/*.yaml`。
- AlphaPurify 交叉验证 harness：`scripts/xval/alphapurify/`（`run_xval.sh` 一键跑，`repo_probe.sh` 复核上游）。
- **已证结论**：IC/RankIC 内核与第三方**位级一致**（`max|Δ|=0`）→ 后续改造不必再怀疑评价内核。

### Phase 1 — 度量与正确性（P0）

**目标**：所有策略都能回答「有没有跑赢指数」；ML 特征预处理不再有泄漏风险。

| 任务 | 涉及文件 | 验收标准 |
|---|---|---|
| **1.1 指数日线入库** | `src/lquant/data/ingest/daily.py::resolve_ingest_source`（当前只路由 fund/stock，**指数没有分支**）、`data/capability.py`（`INDEX_DAILY` 已存在）、`core/types.py`（`SecType.INDEX` 已存在）、`lq data …` CLI | 指数按 `INDEX_DAILY` 能力选源并入库；`lq data status` 显示指数覆盖；`index_daily` 有连续历史 |
| **1.2 回测挂基准** | `src/lquant/backtest/engine.py`、`metrics.py`、复用 `attribution.py::risk_vs_benchmark` | 回测输出含 excess_return / TE / IR；基准可配（默认 `000300.SH`） |
| **1.3 qlib workflow 换真基准** | `config/qlib/workflow_alpha158_lgbm.yaml`（`benchmark`）、`qlib_io/export.py`（导出指数序列）、`docs/qlib.md` | 去掉 `SH600000` 代理；原生与 qlib 两侧超额收益**符号一致、量级接近** |
| **1.4 预处理 fit/infer 纪律** | 新增 `src/lquant/research/ml/processor.py`、改 `research/ml/dataset.py`、`model.py` | **泄漏哨兵测试**：把测试段并入 fit 窗口会让 valid/test 指标变好 → 断言能检测到；线性/NN 可用标准化 |
| **1.5 MAD 口径 + 零方差语义文档化** | `factors/preprocess/winsorize.py`（docstring/params）、`standardize.py`、`registry.py::describe()`、前端预处理 UI | `describe()` 能说明「n × 1.4826 × MAD」；零方差返回 0 的语义有文字 |

**出口标准**：原生与 qlib 两侧都能报超额；反泄漏测试通过。

> **好消息（不用做）**：qlib 侧「PIT 指数成分」是它的弱项（需自己补），而 lquant 已有
> `index_cons`（`sync_index_cons` + `IndexConsRepo.as_of` 防前视）与 `active_symbols(exclude_index=…)`——
> 这块**保持自持即可**，不需要从 qlib 借。

### Phase 2 — ML 生产化（P0）

**目标**：把 ML 从「研究脚本」变成「任务中心可调度、可回放、可回滚」的生产链路。

| 任务 | 涉及文件 | 验收标准 |
|---|---|---|
| **2.1 模型注册表** | `src/lquant/data/store/ddl.py`（`ml_run` 增列或新建 `ml_model`）、`research/ml/backtest.py::run_ml_pipeline`（落 artifact） | 每条 `ml_run` 能定位到具体模型文件与版本 |
| **2.2 ML 接入任务中心** | `server/jobs.py:19`（`QUEUES` 加 `lquant-ml`）、`server/api/task_center.py:19`（`KINDS` 加 `ml`）、新增 `server/api/ml.py`、`cli/commands/ml.py` | 训练任务在任务中心可见/可取消；CLI 与 API 均可发起 |
| **2.3 滚动重训 + 每日推理** | 新增 `research/ml/online.py`（编排）、复用 `walk_forward_splits`、信号落表、对接模拟盘/监控 | 一次完整 walk-forward 重训可调度、可回放；任意历史日期能定位当时在用的模型版本；失败自动回滚 |
| **2.4 前端 ML 面板** | `web/src/app/**`（模型/训练记录页或复用 backtests 区块） | 能看到训练历史、指标、artifact、当前在线模型版本 |

**出口标准**：全链路可调度、可回放、可回滚。

### Phase 3 — 组合、风险与诊断（P1）

**目标**：组合层从「权重技巧」升级为「风险预算」；诊断能力补齐白盒粒度。

| 任务 | 涉及文件 | 验收标准 |
|---|---|---|
| **3.1 风险模型** | 新增 `src/lquant/portfolio/riskmodel.py`（收缩/结构化协方差） | 与样本协方差对比：条件数更低、样本外更稳 |
| **3.2 基准相对优化** | 新增 `src/lquant/portfolio/optimizer.py`（TE 约束）、注册进 `portfolio/weighting.py::METHODS` | 样本外 TE/IR 不劣于 equal/risk_parity 基线 |
| **3.3 截面快照 `trace()`** | 新增 `factors/evaluate/trace.py`、`evaluate/__init__.py` 导出、API + 前端 | 给定日期/方向/分箱可回看权重与收益明细 |
| **3.4 纯暴露/组合暴露归因** | 扩展 `factors/evaluate/attribution.py`（`pure_exposure` / `portfolio_exposure`）、导出 | 能回答「alpha 是不是只是换了皮的 beta/行业暴露」 |
| **3.5 Alpha360** | 新增 `src/lquant/factors/alpha360.py`、测试 | 与 qlib `Alpha360DL` 抽样面板数值一致 |
| **3.6 稳健去极值** | `factors/preprocess/winsorize.py` 补 `huber`/`rankgauss` | 与 AlphaPurify 同名方法对齐口径后数值一致 |

**出口标准**：新基线与风险优化进入常规评估流程。

### Phase 4 — 打磨与常态化（P2）

| 任务 | 说明 |
|---|---|
| 4.1 算子补齐 | `Mask/Not/And/Or/Eq/Ne/Ge/Le/Var/Kurt/Med/Count/ChangeInstrument` |
| 4.2 多 horizon 并行 IC | 复用 `forward_return_matrix`，并行化 |
| 4.3 overnight 切分 | 隔夜/日内收益分开统计 |
| 4.4 方法扩容 | `boxcox`/`yeo_johnson`/`rolling_*`/`EWMA`/`volatility_scaling` |
| 4.5 多 seed / 集成 | 训练多 seed 取均值，降单次训练噪声 |
| 4.6 交叉验证常态化 | `scripts/xval/alphapurify/` 扩因子/扩方法；qlib workflow 纳入定期任务做回归哨兵 |

---

## 3. 依赖关系与顺序理由

```
Phase 0（基线）
   └─► Phase 1（度量+正确性）
          ├─► Phase 2（ML 生产化）        ← 依赖 1.4 的 fit/infer 纪律
          └─► Phase 3（组合/风险/诊断）    ← 依赖 1.2 的基准
                 └─► Phase 4（打磨）
```

- **Phase 1 必须最先**：基准错了，Phase 2/3 会把错误数字自动化；fit/infer 错了，ML 指标虚高。
- **Phase 2 依赖 1.4**：没有 train-only fit，滚动重训只会把泄漏也一起自动化。
- **Phase 3 依赖 1.2**：基准相对优化需要基准序列。
- Phase 4 无阻塞，可随时穿插。

## 4. 风险与边界

| 风险 | 说明 | 缓解 |
|---|---|---|
| 双份数据/双复权口径 | qlib bin 与 Parquet 湖并存 | 导出物是**可重建的派生物**，口径以湖为唯一真源 |
| 两套撮合规则漂移 | qlib 有通用 CN 微观结构但缺 A 股细则 | qlib 回测数字**只作对照**，对外结论一律用原生引擎 |
| 交叉验证被误当背书 | 「qlib/AlphaPurify 也这么说」≠ 正确 | 只用于**发现分歧**；分歧本身才是信号 |
| 上游工程不可靠 | AlphaPurify 自带测试 2/3 失败、API 契约与文档不可信；qlib 处维护模式 | 借算法不借契约；隔离 + 固定版本 + 自持补丁 |
| 依赖漂移 | pyqlib + mlflow + lightgbm 版本组合敏感；RL 锁 `numpy<2` | 固定 `.venv-qlib`，纳入 `lq qlib check` 自检 |
| 概念漂移 | 静态模型随市场失效 | Phase 2.3 滚动重训；DDG-DA 留作观察 |

## 5. 与既有 ADR 的关系（不冲突）

| 既有决策 | 本计划是否冲突 |
|---|---|
| ADR-11：ML 不引 qlib 运行时 | **不冲突**。本计划全部为原生重实现 + 隔离 venv 对拍 |
| `BACKTEST_ENGINES.md`：自研引擎为唯一执行真源 | **不冲突**。qlib 回测仅作对照 |
| 隔离 venv（`.venv-qlib`） | **延续**，新增 `.venv-alphapurify` 同构处理 |
| 单一真源注册表（`METHODS`） | **强化**，并作为 AlphaPurify 的反面教材 |

## 6. 下一步

1. 按 Phase 1 拆出 TDD 任务书，落到 `docs/superpowers/plans/2026-10-03-*.md`（沿用仓库既有 Task/Step 格式）。
2. 先做 **1.5**（文档化，极低成本，立刻消除口径歧义），再做 **1.1→1.2→1.3**（基准链路）。
3. **1.4** 与 Phase 2 一起排期（同属 ML 链路，避免重复改动 `research/ml/`）。
