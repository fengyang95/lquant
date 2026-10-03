# qlib 能力调研与 lquant 集成建议

- 调研日期：2026-10-03
- 被调研对象：microsoft/qlib，本地实装版本 **pyqlib 0.9.7**（`.venv-qlib`，Python 3.12）
- 证据来源：本地 qlib 源码逐模块核对 + 官方文档/论文 + lquant 仓库源码与既有决策文档
- 配套原始材料：
  - `01-qlib-capability-inventory.md`（qlib 能力逐模块清单，源码级证据）
  - `02-lquant-current-state.md`（lquant 现状与已落地 qlib 集成，含 `file:line` 证据与实测数据资产状态）
  - `03-qlib-docs-ecosystem-limits.md`（官方文档、生态、维护状态与已知局限，含生态快照与来源列表）

---

## 0. 结论摘要（TL;DR）

**总判断：不要把 qlib 引入为运行时或执行真源，把 qlib 当作「方法论参照系 + 隔离的交叉验证器」。**

这个判断与仓库既有的两条决策一致，本次调研没有推翻它们，而是把它们从「因子层」推广到「工作流/组合/服务化」层：

| 既有决策 | 出处 | 本次调研结论 |
|---|---|---|
| ML 不引入 qlib 运行时，只借 Model 三段式接口与 Alpha158 | `docs/ARCHITECTURE.md`（ADR-11） | 维持。已验证 `research/ml/model.py` 确实借了 fit/predict/finetune/save/load 接口 |
| 自研事件引擎为唯一执行真源，不引入 qlib backtest | `docs/BACKTEST_ENGINES.md` | 维持。qlib 有通用 CN 微观结构，但缺 A 股细则，引入即第二套口径漂移源 |
| 真 qlib 链路走隔离 venv（`.venv-qlib`）做交叉验证 | `docs/qlib.md`、`qlib_io/*` | 维持并**扩展**：从「单工作流交叉验证」升级为「多模型交叉验证台」 |

### 建议集成的能力（按形态分三类）

**A. 隔离链路内直接复用/扩展**（`.venv-qlib`，零污染主环境）

| # | 能力 | 现状 | 建议 |
|---|---|---|---|
| A1 | qlib 作为独立交叉验证器（Alpha158 + LGBM + TopkDropout） | 已落地 | 扩展为多模型对照（见 A2） |
| A2 | qlib 模型 zoo（Linear/DNN/GRU/ALSTM/Transformer…） | 未接入 | 在同一 workflow 框架下加 2–3 个异构模型做稳健性对照 |
| A3 | qlib 官方回测（TopkDropout） | 已落地 | 仅作对照数字，不进对外结论（nested executor 未用，也不打算用） |
| A4 | 用 lquant 自己的预测喂 qlib 的 `TopkDropoutStrategy` + `backtest_daily` | 未做 | 只交叉验证**组合/回测层**，不碰 qlib 模型与数据层——比整条 workflow 更便宜的对照 |

**B. 原生重实现（借 qlib 语义，不引依赖）—— 本次调研的最高价值部分**

| 优先级 | 能力 | qlib 对应物 | lquant 现状缺口 |
|---|---|---|---|
| **P0** | 基准/超额收益打通 | `benchmark` + `risk_vs_benchmark` | 指标函数已有，但指数数据未接到回测/qlib 工作流 |
| **P0** | 特征预处理 fit/infer 纪律 | `DataHandlerLP` 的 shared/infer/learn processors，仅在 `fit_start_time~fit_end_time` 上 fit | ML 路径**完全无特征标准化**；预处理是逐日截面（因果），但没有 train-only fit 概念 |
| **P0** | 滚动重训 + 每日推理编排 | `workflow/online/OnlineManager` + `RollingGen` | 只有 `walk_forward_splits` 切分助手，无编排、无模型切换、无每日推理 |
| **P0** | 模型注册表（运行记录 ↔ 模型文件） | `Recorder` + `save_objects` | `Model.save/load` 有，但 `ml_run` 表无 artifact 列，训练记录无法回放到模型 |
| P1 | Alpha360 因子集 | `Alpha360DL` | 仓库内无任何引用 |
| P1 | 风险模型 + 基准相对组合优化 | `model/riskmodel/*`（POET/shrink/structured）、`contrib/strategy/optimizer`（EnhancedIndexing） | 只有 equal/mcap/ic_weight/inverse_vol/min_variance/risk_parity/HRP |
| P2 | 算子补齐 | `Mask/And/Or/Eq/Ne/Ge/Le/Var/Kurt/Med/Count/ChangeInstrument` | 现有 34 个算子，缺的主要是逻辑/比较类 |
| P2 | 多 seed / 集成 | `model/ens/*` | 单模型单 seed |

**C. 明确不做（并给出理由）**

| 能力 | 不做的理由 |
|---|---|
| nested decision execution / 日内高频回测 | lquant 定位日频选股；qlib 的嵌套执行栈复杂度高、且其 A 股规则细节与 lquant 真源不一致 |
| RL 订单执行（`qlib/rl`） | 需 `tianshou`，落地成本远高于日频场景收益 |
| DDG-DA 元学习（`model/meta`） | 研究性强、样本要求高；先用 P0 的滚动重训覆盖概念漂移 |
| MLflow 全量实验追踪 | `ml_run` 自建表已覆盖所需；引 MLflow 是纯增量复杂度 |
| 模型 zoo 全家桶进主 venv | 与 ADR-11 冲突；只允许在 `.venv-qlib` 内按需使用 |
| 用 qlib 回测替换原生引擎 | 与 `BACKTEST_ENGINES.md` 结论冲突 |

---

## 1. qlib 能力全景（本地 0.9.7 源码核对）

qlib 的设计哲学是「AI 导向的量化投资全流程」：数据 → 模型 → 策略 → 执行。
官方 README 的表述是 "alpha seeking, risk modeling, portfolio optimization, order execution"。
（注：常被引用的 "alpha-seeking / risk-seeking / execution 三层" **并非** qlib 官方术语——
源码/论文里的真实分层是 Infrastructure → Learning Framework → Workflow → Interface，
Alpha/Risk 只是 Forecast Model 下的子框。本报告按源码实际结构组织。）落到代码是下面七个域。

### 1.1 数据层（`qlib/data/`）

| 子模块 | 提供什么 | 关键点 |
|---|---|---|
| `data.py` | `Cal`(日历)、`Instrument`(股票池)、`Feature`/`ExpressionD`(表达式取数)、`DatasetD`、`LocalProvider` | 核心是**表达式即数据**：`D.features(instruments, ["Ref($close,1)/$close-1"], start, end)` |
| `ops.py` | 算子/表达式引擎：`ElemOperator`(Abs/Sign/Log/Mask/Not)、`PairOperator`(Add/Sub/Mul/Div/Greater/Less/Gt/Ge/Lt/Le/Eq/Ne/And/Or)、`If`、`Rolling`(Ref/Mean/Sum/Std/Var/Skew/Kurt/Max/Min/Quantile/Med/Mad/Rank/Count/Delta/Slope/Rsquare/Resi/WMA/EMA)、`PairRolling`(Corr/Cov)、`TResample`、`ChangeInstrument` | 表达式字符串 → 算子树 → numpy/pandas；可注册自定义算子 |
| `cache.py` | `ExpressionCache`/`DatasetCache` + `MemCache`/`DiskExpressionCache`/`DiskDatasetCache` | 表达式级缓存是它比「每次重算」快的关键 |
| `storage/` | 文件存储后端抽象（bin 格式等） | 与自有数据格式强耦合 |
| `dataset/` | `Dataset`/`DatasetH`、`DataHandler`/`DataHandlerLP`、`processor.py`(DropnaLabel/CSZScoreNorm/RobustZScoreNorm/CSRankNorm/Fillna/TanhProcess/ProcessInf…)、`loader.py`(**Alpha158DL=158 特征 / Alpha360DL=360 特征，本机实测**)、`handler.py`(Alpha158/Alpha360) | **`DataHandlerLP` 的 shared/infer/learn processor 分组 + 只在 fit 窗口 fit** 是最值得借的方法论。实测语义：`ZScoreNorm`/`MinMaxNorm`/`RobustZScoreNorm` 用 **fit 区间**统计量，`CSZScoreNorm`/`CSRankNorm` 是**截面**处理，`DropnaLabel` 是 **learn-only**（`is_for_infer()==False`，`processor.py:105-109`） |
| `_libs/` | Cython 滚动/扩展算子 | 性能件 |

两个值得单独记的点：

- **PIT 表达式算子**：`P`/`PRef` 让财报类字段在**表达式层**做 point-in-time 对齐——
  这是 lquant「财务必须带 `pub_date`」约束的表达式版等价物。
- **多级缓存**：内存 → 磁盘(HDF5) → Redis 锁；表达式级缓存是它比「每次重算」快的关键。

### 1.2 工作流与实验（`qlib/workflow/`、`qlib/cli/`）

- `qrun` 入口：读一个 YAML 就能跑完「数据 → 训练 → 回测 → 记录」。
- `R`/`Recorder`：基于 **MLflow** 的实验记录（参数、指标、artifact、模型对象持久化）。MLflow 是 recorder 的**核心依赖**，且因 MLflow 3.13 关闭了 qlib 依赖的 filesystem tracking 后端，上游把版本钉在 `mlflow<3.13`；本机 `.venv-qlib` 装的是 3.16.1，所以 `runner.py` 里有两处 file-store hack。
- `workflow/online/`：**当前的**在线框架——`OnlineManager`(`online/manager.py:101`)、`OnlineStrategy`、`RollingStrategy`、`RMDLoader`、`DSBasedUpdater`/`PredUpdater`/`LabelUpdater`、`OnlineTool`。定位是**批式**「滚动重训 + 重出信号」编排，**不是实时服务**（无 broker gateway、无行情流接入、无低延迟模型服务）；源码自带 FIXME（`manager.py:81` delay_prepare 未正确实现）。
- `contrib/rolling/`：**离线**滚动实验（`Rolling`，自述 "only for testing rolling models offline"）。
- `contrib/online/`：**已死代码**——0.9.7 里 `import qlib.contrib.online.operator` 直接 `ModuleNotFoundError`（引用了不存在的 `qlib.contrib.backtest` 与缺失的 `executor.py`）。**不可集成**，要用就用 `workflow/online/`。
- `workflow/task/`：任务生成与调度。

### 1.3 模型（`qlib/model/`、`qlib/contrib/model/`）

- 基座：`model/base.py`(Model/BaseModel)、`model/trainer.py`。
- GBDT 系：`LGBModel`、`XGBModel`、`CatBoostModel`、`DoubleEnsemble`。
- 线性：`LinearModel`。
- 深度（PyTorch）：`pytorch_nn`/`DNNModelPT`、`gru`、`lstm`、`alstm`、`gats`、`sfm`、`tabnet`、`tcn`、`tcts`、`tra`、`transformer`、`localformer`、`krnn`、`sandwich`、`hist`、`igmtf`、`add`、`adarnn`（多数还有 `*_ts` 变体）。
- 集成：`model/ens/`（ensemble、group）。
- 风险模型（**core，非 contrib，sklearn/numpy 自包含**）：`ShrinkCovEstimator`（Ledoit-Wolf/OAS 收缩，`riskmodel/shrink.py:7`）、`POETCovEstimator`（因子模型，`poet.py:6`）、`StructuredCovEstimator`（PCA/FA 结构化，`structured.py:11`）。是 `EnhancedIndexingOptimizer` 的天然输入。
- 元学习：`model/meta/`（DDG-DA：data_selection、meta_model、meta_dataset、task_generator）。
- 可解释：`model/interpret/`。

> 注：本机 0.9.7 的 `contrib/model` 共 35 个文件，其中 27 个 PyTorch 模型（含 7 个 `*_ts` 时序变体），
> **没有** TensorFlow 模型，**也没有** `pytorch_tft.py`。
> 由于 `.venv-qlib` 未装 torch，这些模型在当前实装下**不可用**——只有线性 / GBDT 系可用。

### 1.4 回测与执行（`qlib/backtest/`）

`Exchange` → `Executor`(**支持嵌套决策执行**) → `Position`/`Account`/`Order` → `Signal` → `report`。
亮点是 **nested decision execution**：日内可分多步决策（外层日频、内层分钟）。

`Exchange.__init__` 实测参数（`backtest/exchange.py:38-54`）：
`deal_price`、`limit_threshold`（单浮点或**表达式元组**）、`volume_threshold`、
`open_cost`(默认 0.0015)、`close_cost`(默认 0.0025)、`min_cost`(默认 5.0)、
`impact_cost`、`trade_unit`(A 股 100)。（另注：`limit_threshold` 存在 0.095 vs 0.099 的文档不一致，用前须以代码为准。）

**关键判断（经源码核对后修正）**：qlib 的回测**并非**没有 CN 微观结构——它建模了
`NestedExecutor`（日频外层→分钟内层）、涨跌停检查（`exchange.py::check_stock_limit`）、
停牌（`$close is None`）、100 股整手取整、成交量上限裁剪、二次冲击成本、
**T+1 现金结算**（`Position.settle_start`）、订单级 PA/FFR 与 Brinson 归因。

它缺的是 A 股**细则**：印花税历史区间与买卖方向、per-instrument T+N（T+0 ETF）、
分板 20%/30% 涨跌幅、逐日 ST 戴帽、退市残值核销——这些 lquant 已全部单测锁定。
所以结论不是「qlib 撮合粗糙」，而是「**两套规则的差异面足以造成口径漂移**」，
这与 `docs/BACKTEST_ENGINES.md` 的判定一致，且理由更精确。

### 1.5 策略与组合（`qlib/strategy/`、`qlib/contrib/strategy/`）

- `TopkDropoutStrategy`（最常用，TopK + 随机剔除 n_drop；位于 `contrib/strategy/signal_strategy.py:75`）。
- `contrib/strategy/optimizer/`：`EnhancedIndexingOptimizer`（**基准相对**优化）、`PortfolioOptimizer`。
- `contrib/strategy/cost_control`、`signal_strategy`、`rule_strategy`。
- 风险模型 + 优化器组合起来，是 qlib 在「组合层」相对普通 TopK 的实质增量。

### 1.6 服务化（现状澄清）

- **当前的在线/滚动编排在 `qlib/workflow/online/`**（详见 §1.2）：`OnlineManager` + `RollingStrategy` + `RMDLoader`，是**批式**重训/重出信号，不是实时服务。
- `qlib/contrib/rolling/`：**离线**滚动实验工具（自述 only for testing rolling models offline）。
- `qlib/contrib/online/`：**已死代码，不可用**（见 §1.2）。
- 一句话：**qlib 没有实时交易/行情接入层**，不要指望从它那里借「实盘服务」。

### 1.7 分析与工具

- `contrib/report/`：`analysis_model`（IC/RankIC/score 分析）、`analysis_position`（收益/风险图）、graph。依赖 plotly/statsmodels/seaborn——本机 `.venv-qlib` 未装，故**当前实装下不可用**；但 `calc_ic`/`risk_analysis` 等计算核是纯 pandas，可移植。
- `contrib/tuner/`：超参搜索。
- `contrib/data/utils/`：数据过滤、标签生成。
- `contrib/eva/`：评价。
- `qlib/rl/`：订单执行 RL（依赖 `tianshou`）。
- `contrib/ops/`：高频算子。

### 1.8 扩展点（决定「能不能只借一部分」）

qlib 支持按接口插拔：自定义 Model（实现 `fit/predict`）、自定义 DataHandler/processor、自定义 Strategy、自定义算子（`C.custom_ops`）、自定义数据源（`LocalProvider`/`DatasetProvider`）。
**这意味着「只借某几层」在工程上可行**——但前提是数据要落成 qlib 的 bin 格式（lquant 已用 `qlib_io/export.py` 解决）。

### 1.9 版本与维护状态（决定「值不值得依赖」）

- 本机 0.9.7 **就是当前最新 PyPI 发布**（tag v0.9.7, 2025-08-15）；`main` 的文档版本号是 0.9.8.dev11。
- 发布节奏已降到约 **1 次/年**，open issues ~484；2025-11→2026-09 的提交只有 CI/依赖/安全/日历修复，**没有新模型**。
- 结论：**「在维护，但不在演进」**。真正的创新下游是 `microsoft/RD-Agent`（LLM 驱动因子/模型研发）。
- 已知摩擦：并行数据路径 bug #1927（joblib 1.5.0 兼容）长期未修；online 模式 bug #184 自 2021 年开放至今。
- 生态澄清：`TradingAgents`、`FinGPT` **不是** qlib 集成（README 零引用）。
- **A 股数据面 qlib 不解决**：官方数据集因数据安全政策**已停用**；社区替代是 `chenditc/investment_data`
  （需 Tushare token、单维护者、Wind/Caihui 来源许可不清）。上游 `main` 新增了 AkShare/Baostock 的
  CN 交易日历与 `baostock_5min` 采集器（打包版数据止于 2022-12）。**结论：数据面必须自持**——
  这恰是 lquant 的强项。

---

## 2. lquant 现状基线（事实）

### 2.1 原生链路（主环境，无 qlib 依赖）

- **数据**：Parquet 湖（按年分区）+ DuckDB（48 张表）；Provider 抽象 + Capability + Fallback + 看门狗；BaoStock 主源；`index_daily` / `index_cons`（含 `as_of` 防前视）已在代码与库中存在。
- **因子**：自研 DSL（lexer→parser→analyzer→compiler→Polars），34 个算子；四阶段预处理（winsorize/standardize/neutralize/orthogonalize，**逐日截面**，`by` 默认 `trade_date`）；IC/RankIC/分层/衰减/归因 + 自包含 HTML 报告。
- **回测**：自研事件引擎为唯一执行真源，A 股规则全内置（T+N、涨跌停分板、ST 逐日、印花税区间、手数、退市残值、滑点）；Polars 向量化快扫做参数粗筛（screen→verify）。
- **组合**：screener / dedup / weighting（equal、mcap、ic_weight、inverse_vol、min_variance、risk_parity、HRP）。
- **ML**：`research/ml/`，Model 接口（fit/predict/finetune/**save/load**）+ LGBM/sklearn-GBRT/Ridge；`build_dataset` + `walk_forward_splits`；`ml_run` 表记录运行。
- **服务/前端**：FastAPI + RQ(可降级) + WS；任务中心多队列；Next.js 前端（data/factors/backtests/tasks/monitor/paper 等）。
- **运维**：监控、模拟盘、CLI `lq`、一键脚本。

### 2.2 已落地的 qlib 隔离链路

| 文件 | 作用 |
|---|---|
| `src/lquant/qlib_io/export.py` | 湖 → qlib bin 格式（纯 polars/pyarrow，主 venv 可跑） |
| `src/lquant/qlib_io/runner.py` | qrun 风格工作流（qlib 专用 venv，子进程） |
| `src/lquant/qlib_io/interpreter.py` | `find_qlib_python` 解释器探测 |
| `src/lquant/qlib_io/store.py` | `qlib_run` 运行元数据表 |
| `src/lquant/cli/commands/qlib.py` | `lq qlib export/check/workflow` |
| `src/lquant/server/api/qlib.py` | `/qlib status/export/configs/workflow/runs/compare` |
| `config/qlib/workflow_alpha158_lgbm.yaml` | Alpha158 + LGBM + TopkDropout |
| `src/lquant/factors/qlib_alpha.py` | Alpha158 的纯 Polars 原生重实现（316 行） |
| web `QlibExportCard/QlibWorkflowPanel/QlibRunsSection/QlibTaskPanel` | 前端接入 |

已验证：`.venv-qlib` 可正常 `import qlib(0.9.7) / lightgbm(4.7.0)`。
（注：当前 checkout 的 `data/qlib/` 只有 `runs/` 空日志与 `qlib_runs.db`，没有已导出的 bin 数据，说明最近一次工作流未产出 metrics——集成状态「代码就绪、数据待跑」。）

### 2.3 既有边界（文档自述，需更新）

`docs/qlib.md`「已知边界」写的是「湖内无指数日线 → benchmark 置 null」。
**但代码里 `Capability.INDEX_DAILY`、`data/ingest/index_cons.py`、`index_daily` 表（本库 138 行）都已存在**，
所以这条边界已经过时——这正是下面 P0-1 的切入点。

### 2.4 qlib 能力 vs lquant 能力 对照矩阵

| 能力域 | qlib | lquant 现状 | 判定 |
|---|---|---|---|
| 数据存储 | 自有 bin 格式 + `LocalProvider` | Parquet 湖 + DuckDB | **自研为真源**，qlib bin 仅作可重建的派生物 |
| 表达式取数 | `D.features` 表达式即数据 + 表达式级缓存 | 自研 DSL + Polars，逐次计算 | 自研；表达式缓存可借鉴（低优先） |
| 算子集 | ~40（含 Mask/And/Or/Eq/Ne/Var/Kurt/Med/Count/ChangeInstrument） | 34 | 自研；P2 补齐少数 |
| 标准因子集 | Alpha158 + Alpha360 | Alpha158（原生）+ **无 Alpha360** | P1 补 Alpha360 |
| 预处理 | shared/infer/learn 分组，**仅训练窗口 fit** | 逐日截面四阶段（因果，但无 fit 概念） | **借语义重实现（P0）** |
| 模型接口 | `Model(fit/predict/finetune)` | 已借同构接口 + save/load | 已对齐 |
| 模型 zoo | GBDT 系 + 线性 + 十余个 PyTorch 模型 | LGBM / sklearn-GBRT / Ridge | 主环境不引；隔离链路按需对照 |
| 风险模型 | POET / shrinkage / structured 协方差 | 无 | P1 借语义重实现 |
| 组合策略 | TopkDropout、EnhancedIndexing 优化器 | screener / dedup / 7 种 weighting | P1 补基准相对优化 |
| 回测撮合 | 通用 CN 微观结构，缺 A 股细则（见 §1.4） | A 股规则全内置 | **自研为唯一执行真源** |
| 嵌套/日内执行 | nested decision execution | 无（日频） | 不引 |
| 实验追踪 | MLflow `Recorder` | `ml_run` 自建表 | 自研；P0 补 artifact 关联 |
| 滚动重训/在线 | `workflow/online` + `contrib/rolling` | 仅 `walk_forward_splits` 切分助手 | **借语义重实现（P0）** |
| 报告分析 | `contrib/report` 图形 | HTML 报告 + 归因 | 自研已够 |
| 超参搜索 | `contrib/tuner` | 无（有 Polars 向量化参数扫描） | 暂不引 |
| RL 执行 | `qlib/rl`（tianshou） | 无 | 不引 |
| 元学习（概念漂移） | DDG-DA | 无 | 观察，先用滚动重训覆盖 |

---

## 3. 差距分析

| ID | 差距 | 证据 | 影响 |
|---|---|---|---|
| G1 | 基准/超额收益未打通 | `backtest/attribution.py::risk_vs_benchmark` 有 TE/IR/excess **函数**，但引擎 `metrics.py` 未纳入、也无基准序列来源；`config/qlib/workflow_alpha158_lgbm.yaml:16` 仍用 `SH600000` 机械代理 | 所有策略只能看绝对收益，无法回答「有没有跑赢指数」 |
| G2 | 无 Alpha360 | 全仓 grep `Alpha360` 零命中；qlib 侧实测 360 特征 vs lquant 原生 Alpha158 的 158 | 少一套与 Alpha158 互补的基线特征 |
| G3 | 特征预处理无 train-only fit、ML 路径无标准化 | `research/ml/` grep 无 scaler/normalize；`factors/preprocess/registry.py` 是逐日截面（因果），但无 learn/infer 分组 | 对树模型无碍；一旦上线性/NN 模型，标准化窗口泄漏会直接虚高验证集 |
| G4 | 无滚动重训/在线推理编排 | 只有 `research/ml/dataset.py::walk_forward_splits`（切分助手）；无调度、无模型切换、无每日推理 | 「研究能跑」到「每天出信号」之间是断的 |
| G5 | 运行记录与模型文件断链 | `model.py:47-62` 有 save/load；`ddl.py:147-158` 的 `ml_run` 无 artifact/version 列 | 训练记录无法回放到具体模型，无法回滚/对比 |
| G6 | 组合层无风险模型与基准相对优化 | `portfolio/weighting` 方法集；无协方差风险模型、无 EnhancedIndexing | 组合是「权重技巧」而非「风险预算」 |
| G7 | 无嵌套/日内决策执行 | 原生引擎为日频；`BACKTEST_ENGINES.md` 明确不引 | 非目标，接受 |
| G8 | 实验追踪形态不同 | `ml_run` 自建表 vs qlib MLflow `Recorder` | 已有等价物，不引 MLflow |
| G9 | 报告可视化 | lquant 已有 HTML 报告 + 归因 | 已够，不引 qlib report |
| G10 | 前沿能力（RL/元学习/高频） | `qlib/rl`、`qlib/model/meta`、`contrib/ops` | 与日频选股定位不匹配 |
| G11 | 无查询期表达式引擎与表达式缓存 | qlib `D.features(...)` 动态取数 + 内存/磁盘/Redis 多级缓存；lquant 是预计算 Polars 列 | 取数灵活性与重复计算成本；**判定不引**（自研 DSL + 湖已够，缓存可选） |

**排序后的真实缺口**：G1（基准）> G3（fit/infer 纪律）≈ G4/G5（滚动重训 + 模型注册）> G6（风险组合）> G2（Alpha360）> 其余。

---

## 4. 集成建议（详表）

### P0-1 打通基准与超额收益（原生 + qlib 两侧）

- **借什么**：qlib 的 `benchmark` 语义与 `risk_vs_benchmark` 指标（lquant 已实现后者）。
- **做什么**：
  1. 指数日线入湖并纳入 `data status` 覆盖度（`index_daily` 已有表与 capability，补齐历史）。
  2. 原生回测默认 benchmark 可配（`000300.SH` 等），metrics 增加 excess_return / TE / IR。
  3. `qlib_io/export.py` 导出指数序列（或让 workflow 的 `benchmark` 指向真实指数），去掉 `SH600000` 代理。
  4. 更新 `docs/qlib.md` 的「已知边界」。
- **收益**：一次性提升所有回测与所有 qlib 对照的可解释性。**成本：低**（管道已存在）。
- **验收**：同一策略在原生引擎与 qlib 工作流上，超额收益符号一致、量级接近。

### P0-2 特征/预处理 fit-infer 纪律

- **借什么**：`DataHandlerLP` 的 `shared_processors / infer_processors / learn_processors` 三分组，以及**只在 `fit_start_time~fit_end_time` 上 fit、再 transform 到全区间**。
- **做什么**：
  1. 在 `research/ml/dataset.py` 引入 `Processor` 概念：`fit(train_df)` → `transform(any_df)`，把标准化/去极值从「逐日截面」推广到「可 fit 的时序参数」。
  2. 明确 `learn` 段 fit、`valid/test` 只 transform；`DatasetConfig` 增加 fit 窗口字段。
  3. 为线性/NN 模型提供标准化；树模型保留 raw。
  4. 加一个「泄漏哨兵」测试：故意把测试段统计量写进 fit，断言检测得到。
- **收益**：这是**方法论正确性**问题，不是性能问题。**成本：中**。
- **验收**：`valid/test` 的指标不因把测试段并入 fit 窗口而改善（反泄漏回归测试）。

### P0-3 滚动重训 + 每日推理编排 + 模型注册表

- **借什么**：`RollingGen`（滚动窗口生成）+ `TrainerR/TrainerRM` + `qlib/workflow/online/OnlineManager`（批式滚动重训 + 重出信号 + 模型切换；注意**不要**用已死的 `contrib/online/`）+ `Recorder`（运行 ↔ artifact 绑定）+ `MultiPassPortAnaRecord`（多次回测稳健性检验）。
- **做什么**：
  1. `ml_run` 增加 `artifact_path / model_version / stage`（或新建 `ml_model` 表）。
  2. 新增编排器：按频率（月/季）在 `walk_forward_splits` 上重训 → 落 artifact → 记录 → 生成每日预测信号。
  3. 信号落表并对接模拟盘/监控（复用现有 task center 队列做长任务）。
  4. 失败回滚：保留上一个可用模型版本。
- **收益**：把 ML 从「研究脚本」变成「可运行的生产链路」。**成本：中高**。
- **验收**：一次完整 walk-forward 重训可在任务中心可见、可取消、可回放；任意历史日期能定位到当时在用的模型版本。

### P1-1 Alpha360 原生实现

- **借什么**：`Alpha360DL` 的公式定义（与 `qlib_alpha.py` 同样的「对照源码重实现」路径）。
- **收益**：与 Alpha158 并列的第二套标准基线，成本低（已有重实现范式与测试脚手架）。
- **验收**：与 qlib 原生 Alpha360 在抽样面板上数值一致。

### P1-2 风险模型 + 基准相对组合优化

- **借什么**：`model/riskmodel`（结构化/收缩协方差）与 `contrib/strategy/optimizer/EnhancedIndexingOptimizer` 的思想。
- **做什么**：在 `portfolio/` 增加协方差估计（shrinkage / 结构化因子模型）与基准相对优化（约束跟踪误差下最大化预期超额）。
- **收益**：组合层从启发式权重升级为风险预算。**成本：中**。
- **验收**：优化组合的样本外 TE/IR 不劣于等权/风险平价基线。

### P1-3 隔离链路扩展为「多模型交叉验证台」

- **借什么**：`contrib/model` 的异构模型（Linear / DNN / GRU / ALSTM / Transformer）。
- **做什么**：在 `config/qlib/` 增加 2–3 个 workflow 变体，仅切换 `model` 段；`/qlib/runs/compare` 做横向对比。
- **收益**：验证「Alpha158 上的 alpha 是不是 LGBM 特有」，是低成本稳健性检验。**成本：低**（框架已在）。
- **边界**：所有重依赖只进 `.venv-qlib`，主 venv 不受影响。

### P2 项

- **算子补齐**：`Mask/Not/And/Or/Eq/Ne/Ge/Le/Gt/Lt/Var/Kurt/Med/Count/ChangeInstrument`。成本低、收益小（现有 34 个已覆盖主流）。
- **多 seed / 集成**：借 `model/ens` 语义，训练多 seed 取均值，降低单次训练噪声。
- **qlib 工作流常态化**：把 `lq qlib workflow` 纳入定期任务，作为原生链路的回归哨兵。

---

## 5. 分期路线图

| 期 | 内容 | 依赖 | 出口标准 |
|---|---|---|---|
| 第 1 期 | P0-1 基准打通 + P1-3 多模型对照 | 指数数据补齐 | 原生与 qlib 两侧都能报超额；≥3 个模型可对比 |
| 第 2 期 | P0-2 fit/infer 纪律 + 泄漏哨兵测试 | 第 1 期 | 反泄漏测试通过；线性/NN 可用 |
| 第 3 期 | P0-3 滚动重训 + 每日推理 + 模型注册 | 第 2 期 | 全链路可调度、可回放、可回滚 |
| 第 4 期 | P1-1 Alpha360 + P1-2 风险组合 | 第 2/3 期 | 新基线与风险优化进入常规评估 |
| 持续 | P2 算子/集成/常态化交叉验证 | — | 按需 |

**顺序理由**：先修「度量」（基准）与「正确性」（泄漏），再做「工程化」（滚动重训）；否则会把错误的数字自动化。

---

## 6. 风险与边界

| 风险 | 说明 | 缓解 |
|---|---|---|
| 双份数据 / 双复权口径 | 导出 qlib bin 与 Parquet 湖并存 | 保持「导出物是派生物、可重建」；口径以湖为唯一真源（`docs/qlib.md` 已定） |
| 两套撮合规则漂移 | qlib 有通用 CN 微观结构，但缺 A 股细则 | qlib 回测数字**只作对照**，对外结论一律用原生引擎 |
| qlib 依赖/版本漂移 | pyqlib + mlflow + lightgbm 版本组合敏感 | 固定 `.venv-qlib` 版本并纳入 `lq qlib check` 自检 |
| 交叉验证被误当背书 | 「qlib 也这么说」不等于正确 | 交叉验证只用于发现分歧；分歧本身才是信号 |
| qlib-core 处于维护模式 | 约 1 次发布/年、open issues 较多，创新已转向 `microsoft/RD-Agent`（LLM 驱动因子/模型研发） | 把 qlib 当 **vendored 研究依赖**而非服务；固定版本、自持补丁 |
| A 股数据面 qlib 不解决 | 官方数据集已停用；社区源需 Tushare token、单维护者、许可不清 | 数据面继续自持——这恰是 lquant 的强项，不是缺口 |
| 概念漂移 | 静态训练模型随市场失效 | P0-3 滚动重训；DDG-DA 留作后续观察 |

---

## 7. 附：一句话结论

**qlib 值得借的不是它的模型，而是它的「数据-特征-模型-组合」之间的方法论纪律；
lquant 已经借了接口（Model）和因子集（Alpha158），下一步该借的是
「只在训练窗口 fit」「基准相对度量」「滚动重训与每日推理的编排」这三件事，
并且继续用隔离 venv 把 qlib 当交叉验证器而不是运行时。**
