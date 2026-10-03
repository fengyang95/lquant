# lquant 现状能力清单（qlib 差距分析基线）

- 日期：2026-10-03
- 对象：`/Users/lyp/code/lquant`（main，HEAD `076111c`）
- 方法：源码通读 + 文档核对 + **对 live 数据/环境的只读实测**
- 性质：只读盘点，未修改任何源码或数据
- 姊妹文档：`00-qlib-integration-proposal.md`、`01-qlib-capability-inventory.md`（后者盘点的是 qlib 自身能力；本文只盘点 lquant 已有能力，§13 给第一轮差距清单）

> 证据约定：所有结论后附 `路径:行号`。凡我未逐行读过的模块，一律显式标注 **【未逐行验证】**，不猜测内部实现。
> 标注 **【实测】** 的为我亲自对 live 库/湖/环境跑过的只读查询。

---

## 0. 一页结论

lquant 是一个 **A 股（个股 + ETF）全栈量化研究平台**：数据接入 → 因子分析 → 回测 → 组合 → 模拟盘 → 监控 → Web UI，并已存在一条 **"真 qlib" 旁路**（日线湖 → qlib 二进制 → qrun 式工作流）。

- **强项**：数据层工程完备度（Capability 路由、Fallback 链、看门狗、断点续传、原子写 + 双层锁、质量门禁与湖完整性自检）、A 股撮合规则的数据化与正确性（涨跌停 tick 取整、逐日 ST、印花税历史区间 + 方向、T+N 按交易日、退市核销、公司行为）、因子评价体系（IC/ICIR/NW-t/分层/衰减/归因/评级/稳健性）、以及 **Alpha158 的纯 Polars 原生重实现（158 个）**。
- **弱项（相对 qlib）**：数据资产本身有硬缺口（`security` 3 行、2025 日线整年缺失 —— 见 §2.7）；无 PIT 数据库/表达式查询引擎；无实验追踪与模型 zoo；无 nested/intraday 回测；风险模型与组合优化只有轻量实现；无在线服务/滚动重训；qlib 链路目前**没有已落地的导出产物**（§12.10）。
- **架构上的关键 ADR**：**不引入 qlib 运行时**（`docs/ARCHITECTURE.md:284`），只借接口 + Alpha158 因子集；真 qlib 走 **隔离 venv 子进程**（`.venv-qlib`，§12.9）。

---

## 1. 项目形态（Project shape）

### 1.1 定位与三大约束

`README.md:3` 定义平台为「数据接入 → 因子分析 → 量化回测 → 市场看板 → 模拟盘」一体化平台；后端 Python 3.12+（Polars / DuckDB / FastAPI），核心算法 Rust（pyo3-polars + maturin，可选），前端 Next.js 15，数据主源 BaoStock。

三条贯穿全局的硬约束（`README.md:81-85`、`docs/ARCHITECTURE.md:31-37`）：

1. 代码格式统一 `000001.SZ` / `510300.SH`，**绝不用裸 6 位**（`000001` 歧义：上证指数 vs 平安银行）；
2. 时间统一 ISO 8601 + `Asia/Shanghai`；
3. 金额统一**元**，源站万元/亿元在 adapter 层换算 + 断言。

### 1.2 文档清单（已核对）

| 文件 | 行数 | 内容要点 |
|---|---|---|
| `README.md` | 106 | 快速开始、数据层速览、仓库地图、硬约束、Rust 降级原则、密钥卫生 |
| `docs/ARCHITECTURE.md` | 614 | 六层架构（L0 接入→L6 前端）、目录树、模块卡片、数据流、里程碑状态、9 轮填实记录 |
| `docs/STRUCTURE.md` | 39 | 目录树 + 关键类/函数表 |
| `docs/EXTENSION_POINTS.md` | 16 | EP-1..EP-12 扩展点落地状态（含"不做"项：EP-10 通知告警、EP-11 LLM 挖因子架构预留、EP-12 多市场） |
| `docs/DATA_ROBUSTNESS_AUDIT-2026-10-03.md` | 405 | 数据模块完备性/鲁棒性审计（**本文 §2 大量引用**；注意其基线是 `33ec0f9`，HEAD 已修部分项） |
| `docs/BACKTEST_ENGINES.md` | 115 | 引擎选型结论（自研为唯一执行真源）、B5 向量化扫描、成本口径修正表 |
| `docs/BACKTEST_BASELINE_E2E.md` | 187 | 基准多因子策略 E2E 实跑记录 + 缺口清单 G1–G20/D1–D15 |
| `docs/BACKTEST_VALIDATION.md` / `_BENCHMARKS.md` | 184 / 95 | 对账方法学与基准（backtrader 对照） |
| `docs/FACTOR_VALIDATION.md` | 96 | 因子验证方法学 |
| `docs/qlib.md` | 65 | qlib 接入说明（§12 引用） |
| `docs/PORT_FINANCIALTOOL.md` | 351 | 组合/FinancialTool 移植 |
| `docs/因子分析能力完备性审计.md` | 324 | 因子分析能力审计 |
| `docs/SECURITY.md` / `SECRET_HYGIENE.md` | 141 / 179 | 安全与密钥卫生 |

`docs/superpowers/specs/` 与 `docs/superpowers/plans/` 存有设计 spec + 实施 plan（含 `2026-09-20-qlib-frontend-design.md` 与 `plans/2026-09-20-qlib-frontend.md`，见 §12.7）。

### 1.3 pyproject.toml（依赖分层）

`pyproject.toml`：`requires-python = ">=3.12"`（`pyproject.toml:9`）。

**核心依赖**（`pyproject.toml:14-30`）：`polars>=1.44,<1.45`、`pyarrow>=18`、`duckdb>=1.4`、`pydantic>=2.9`、`pydantic-settings`、`pyyaml`、`click`、`rich`、`loguru`、`tenacity`、`baostock>=0.8.8`、`pandas>=2.2`、`aiosqlite`、`psutil`。

**可选依赖分组**（`pyproject.toml:32-70`）：
- `sources`：`akshare`、`efinance`、`mootdx`、`stockstats`、`tushare`；
- `factors`：`scipy`、`statsmodels`、`skfolio`；
- `ml`：`lightgbm>=4.5`、`scikit-learn>=1.5`；
- `server`：`fastapi>=0.115`、`uvicorn[standard]`、`rq>=2.0`、`redis>=5.2`；
- `dev`：pytest / pytest-cov / diff-cover / pytest-asyncio / ruff / mypy / pre-commit / httpx(<0.26) / hypothesis。

**CLI 入口**：`lq = "lquant.cli.main:cli"`（`pyproject.toml:73`）。
**覆盖率门禁**：`fail_under = 95`（`pyproject.toml:126`），源码 `src/lquant`。
**ruff**：line-length 100、py312、select E/F/I/UP/B/SIM/PL（`pyproject.toml:88-114`）。
**pytest**：`testpaths=["tests"]`、`--strict-markers`、markers `slow`/`rust`/`integration`（`pyproject.toml:78-86`）。注释记录了"pytest.ini 会静默覆盖 pyproject 配置"的历史坑（`pyproject.toml:80-82`）。

### 1.4 Makefile / lquant.sh

`Makefile`（146 行）targets：`help setup hooks secrets secrets-history secrets-dir secrets-install public-ready start stop status logs bundle db-init db-reset bootstrap bootstrap-full dev api worker web smoke rust-build rust-test test coverage diff-cov lint fmt type docker-up docker-down clean`（`Makefile:10-144`）。

`lquant.sh`（623 行，未逐行读）：一键 `install / build / start / stop / status / restart / logs / bootstrap / doctor / all`；自动装 uv、venv、依赖（清华源 + npmmirror）、建库、启动 API+Worker+Web 并探活；无 Redis/cargo 均不阻塞（`README.md:12-43`、`docs/ARCHITECTURE.md:434-445`）。

### 1.5 Rust workspace（crates/）

`crates/Cargo.toml`：workspace members `lq-ops` / `lq-backtest` / `lq-metrics`；`edition 2021`；关键版本约束（`crates/Cargo.toml` 注释）：

> `polars-rs 0.55 ↔ polars-py 1.44 ↔ pyo3-polars 0.28 ↔ pyo3 0.29`，三者 ABI 强绑定，错配"要到运行时才炸"。

| crate | 实现 | 说明 |
|---|---|---|
| `lq-backtest` | `crates/lq-backtest/src/lib.rs`（169 行） | `match_order`（撮合 + 佣金累计 + 税 + 过户费 + 整手）+ `match_order_py` pyfunction。规则**全部外部传入**，crate 内无硬编码规则（`lib.rs:4-5`）。含 3 个 Rust 单测（ETF 免印花税、最低佣金按订单只收一次、脏输入拒绝） |
| `lq-metrics` | `crates/lq-metrics/src/lib.rs`（123 行） | `rank_ic`（Spearman，NaN 成对剔除）、`max_drawdown`。4 个 Rust 单测 |
| `lq-ops` | `crates/lq-ops/src/lib.rs` | `ts_corr` / `ts_regbeta`，以 `#[pyfunction] + Vec<Option<f64>>` **数组接口**暴露（**不是** `#[polars_expr]` 表达式插件），与 `src/lquant/_rust/ops_ref.py` 逐位镜像；`.pyi` 只声明两个函数 |

**Rust 必须有 Python 参考实现**（`README.md:87-90`）：`src/lquant/_rust/{loader.py,ops_ref.py,metrics_ref.py,broker_ref.py}`（33/77/58/39 行），加载失败自动降级为纯 Python（`docs/ARCHITECTURE.md:286-298`）。

⚠️ **重要事实**：`factors/ops/rust_bridge.py:1-13` 明确说明 —— **Rust 算子当前不覆盖 `OPS` 表达式注册表**（`register_rust_ops()` 直接 `return 0`，`rust_bridge.py:28-39`），因为 lq-ops 是数组接口而非 Expr 接口，"拿数组函数去覆盖 `OPS['Ts_Corr']` 会让因子引擎拿到 list 而非 Expr，属于静默破坏"。Rust 算子的价值验证走对拍测试，表达式级覆盖因 polars FFI 版本错配**延后**。

---

## 2. 数据层（Data layer）

### 2.1 存储分工

- **Parquet 湖**：大表（日线 / 分钟线 / daily_basic / 因子值）。日线按**年**单文件 `daily/year=YYYY/part-0.parquet`（`store/parquet.py:173-174`）；分钟线按 `(freq, 年月)` 分区（`parquet.py:470`）；因子 `factors/name=<factor>/part-0.parquet`（`parquet.py:448-452`）。**绝不按 symbol 分文件**（`parquet.py:3-4`、`README.md`/`config/app.yaml`）。
- **DuckDB**：小表 / 参考表 / 目录 / 元数据。**单写者**约束：`core/db.py:68-80` 的 `writer()`（进程内 `RLock` + 跨进程 OS 文件锁 + 有界重试），`reader()`（`db.py:83-95`，**刻意不加 `read_only=True`** —— 因为 DuckDB 实例缓存按"路径+配置"区分，读写与只读会变成两个互不可见的实例，见 `db.py:86-91` 的墓志铭）。
- **SQLite**（旁路，独立于 DuckDB）：qlib 运行元数据（`qlib_io/store.py:38`）、模拟盘状态（`paper/store.py:28`）、Ask 会话（`data/ask.db`）。

**原子写与锁**：`store/parquet.py:34-63` 双层锁（进程内 `threading.Lock` + 跨进程 `fcntl.flock` 于 `<file>.lock`）；`parquet.py:66-74` 先写同目录 tmp 再 `os.replace`，读侧不会看到半截文件。
**【实测/审计】不足**：flock 打开失败时**只 warning 并降级为进程内锁**（`parquet.py:48-55`），非 fail-closed（审计 P1-12）；原子写**无 fsync**（审计 P2-6，`parquet.py:66-74`）。

### 2.2 存了哪些数据（schema）

`data/schema.py` 是内部统一 schema 的唯一真相源：

| schema | 关键列 | 位置 |
|---|---|---|
| `DAILY_BAR` | symbol, trade_date, OHLC, pre_close, volume(股), amount(元), turnover_rate, **adj_factor**, pct_chg, **is_st**, **is_suspended**, pe_ttm, pb_mrq, ps_ttm, pcf_ncf_ttm, total_mv, float_mv, sec_type, **quality_flags**(位掩码), source, ingested_at, data_version | `schema.py:11-39` |
| `MINUTE_BAR` | symbol, ts, freq, OHLCV, amount, adj_factor, source, ingested_at | `schema.py:42-55` |
| `FINANCIAL_PIT` | symbol, **stat_date（报告期）+ pub_date（公告日）**, report_type, item, value, unit | `schema.py:58-68` |
| `SECURITY` | symbol, name, sec_type, board, list_date, **delist_date（幸存者偏差防护）**, is_st | `schema.py:71-80` |
| `INDUSTRY` | symbol, std(sw1/cics/em), code, name, **std_date（生效日）** | `schema.py:83-90` |
| `ETF_META` | track_index, fund_type, is_cross_border, **sellable_after_days（per-instrument T+N）**, fees, fund_size, share_outstanding | `schema.py:92-105` |
| `MARKET_SNAPSHOT` | last, pct_chg, volume, amount, iopv, discount, **is_stale（僵尸报价）** | `schema.py:107-118` |
| `DAILY_BASIC` | close, turnover_rate, pe_ttm, pb_mrq, ps_ttm, total_mv(元), float_mv(元), dv_ttm, total_share, float_share | `schema.py:123-137` |

`SCHEMAS` 注册 8 张（`schema.py:139-148`）；`coerce()` 做"缺列补空 + 类型统一"（`schema.py:155-164`），Utf8→Boolean 走 `"1"/"true"` 白名单（`schema.py:167-174`）。

**DuckDB DDL**（`data/store/ddl.py:48-266`，约 30+ 张表）：
`security`、`trade_calendar`、`daily_bar`⚠️、`minute_bar`⚠️、`adj_factor`、`financial_pit`、`industry_classify`、`index_cons`、`etf_meta`、`factor_def`、`ml_run`、`agent_ledger`、`factor_ic`、`factor_mining_run`、`factor_replication`、`factor_value`、`backtest_run`、`backtest_order`、`backtest_position`、`backtest_nav`、`market_snapshot`、`sector`、`collect_log`、`data_quality_issue`、`data_version`、`golden_expected`、`sync_job`、`sync_run`、`strategy_def`、`analysis_def`、`backtest_record`、`app_setting`；`market/schema.py::ensure_market_tables` 另拥有 money_flow / dragon_tiger / sentiment_daily。

⚠️ `daily_bar` / `minute_bar` 是**遗留占位表，恒空、无写入路径**（`ddl.py:268-272` 注释 + 审计 §1.4 实测 0 行）。读取一律走 `store/parquet.py`。`v_daily`/`v_minute` 视图由 `ensure_views()` 建（`ddl.py:273-297`），空湖跳过。

### 2.3 Provider 体系（支持哪些源）

`data/base.py`：`DataProvider` 抽象类，**6 个核心方法**（`daily_bars` / `minute_bars` / `adj_factors` / `financial_pit` / `securities` / `trade_calendar`，`base.py:48-66`）+ 可选 `etf_meta` / `realtime` / `health`；`require(cap)` 不支持就抛 `CapabilityMissing`，**绝不静默返回空**（`base.py:39-45`）。

`data/capability.py:11-35`：22 项能力枚举（DAILY、MINUTE_1/5/15/30/60、ADJ_FACTOR、INDEX_DAILY、FINANCIAL_PIT、CORPORATE_ACTION、CALENDAR、REFERENCE、INDUSTRY、ETF_DAILY、ETF_MINUTE_60、ETF_SPOT、ETF_META、ETF_IOPV、ETF_SHARE、REALTIME、MONEY_FLOW、LIMIT_UP、DRAGON_TIGER、SECTOR）。

`config/providers.yaml` 声明 9 个源，实际状态：

| 源 | enabled | capability | 实测/备注 |
|---|---|---|---|
| **baostock** | ✅ true | daily, minute_5/15/30/60, etf_daily, etf_minute_60, etf_meta, index_daily, financial_pit, calendar, reference, adj_factor | **主源**；批量请求静默挂起 → 必须 watchdog |
| **tushare** | ✅ true | daily, minute_1–60, adj_factor, financial_pit, reference, calendar, etf_daily | 需 `TUSHARE_TOKEN`（缺则 `build_chain` 跳过，`providers/__init__.py:78-83`）；**基本面统一主源**（财务四表 + daily_basic）；日线只做 crosscheck peer |
| **akshare** | ❌ false | daily, minute_1–60, adj_factor, reference, etf_daily | 覆盖最广但易封 IP |
| **efinance** | ❌ false | daily, minute_1, etf_daily, realtime | **模块文件不存在**（审计 §2.3） |
| **hithink** | ❌ false | daily, adj_factor, financial_pit, corporate_action | **模块文件不存在** |
| **mootdx** | ✅ true | realtime, minute_1 | 通达信 TCP；看板热通路首选 |
| **tencent** | ✅ true | realtime, reference | T0 源 |
| **sina** | ✅ true | adj_factor | 复权因子源（hfq.js）；⚠️ **契约不符**：返回 `hfq_close` 而非 `factor`（审计 §2.1、`providers/sina.py:118-120`） |
| **tickdb** | ❌ false | tick | 预留位，**刻意不加载**（`providers/__init__.py:21` 的 `_import_all` 不含它） |

`providers/__init__.py:17-31` `_import_all()` 逐个 import 7 个模块，`ImportError` **静默吞掉**（`providers/__init__.py:28-29`）。`build_chain()`（`providers/__init__.py:57-94`）构建前对 `config/schema/*.yaml` 全量静态校验（`validate_all_mappings`），**映射错误启动即抛**（fail-fast）；按运行时 `providers_order` 重排链（`providers/__init__.py:34-54`）。

Provider 实现规模：`baostock.py` 795、`tushare.py` 465、`akshare.py` 275、`tencent.py` 183、`sina.py` 140、`_engine.py`(MappingProvider) 117。

### 2.4 Fallback / 健康度 / 限流 / 看门狗

- **FallbackProvider**（`data/fallback.py:41-126`）：按 Capability 路由（`_route`，`fallback.py:52-60`），逐源 try、失败记 `HealthTracker` 并切下一源（`_call`，`fallback.py:62-81`）；`last_source` 记录实际服务源（血缘，`fallback.py:48-49`）。
- **HealthTracker**（`fallback.py:19-38`）：连续失败冷却 `min(300*n, 3600)` 秒；**纯内存、重启即丢、从不外露**（审计 P2-2）。
- **限流**：`data/ratelimit.py` `TokenBucket`（8 行起）；**只有 tushare / akshare 建桶**，sina/tencent 接收 qps 但忽略（审计 P1-11）。
- **看门狗**：`data/watchdog.py` `run_with_watchdog`（子进程 + 超时强杀），**只有 baostock 用**；tushare/akshare 的 SDK 调用无超时（审计 P1-10）。

⚠️ **审计 P1-1（实测）**：**所有 ingest 主路径绕过 `FallbackProvider`**，实际是"单源 + 断点重试"。`resolve_ingest_source` 只从 `chain.providers` 挑**一个**返回（`ingest/daily.py:52-79`），`_pull_group` 直接调它（`daily.py:262-296`）。真正走链的只有 `sync_calendar`、`sync_securities`、realtime（`ingest/reference.py:39`、`:59`）。

### 2.5 日历 / 标的 / universe / 退市 / 停牌

- **交易日历**：`core/calendar.py:19-34`（`is_trading_day` / `trade_days` / `prev_trade_day` / `next_trade_day`），底层 `TradeCalendarRepo`（`store/catalog.py:60-82`）查 DuckDB `trade_calendar`。同步入口 `sync_calendar`（`ingest/reference.py:27-45`），区间 1990-12-19 ~ 2035-12-31（`reference.py:23-24`）。**【实测】live `trade_calendar` = 13,527 行**。
- **标的清单**：快路径 `sync_securities`（`reference.py:48-70`，一次请求拿全市场 code/name/status，`_merge_existing_details` 防止把已补的 list_date 冲成 NULL）；慢路径 `sync_security_details`（`reference.py:153-240`，逐只补 ipoDate/outDate，checkpoint 增量，**零行不标完成**）；退市名单 `sync_delisted`（`reference.py:80-112`，走 akshare 沪深退市接口）。`SecurityRepo`（`catalog.py:85-144`）提供 `active_symbols`（未退市、可选剔除指数）、`all_symbols`、`stock_symbols`（**缺省含退市股** —— PIT 回测防幸存者偏差）、`etf_symbols`、`pending_details`。
  ⚠️ **【实测】live `security` 表只有 3 行，且 `source='test'`、`list_date/board` 全 NULL**：`('000001.SZ','平安银行','stock',None,None,'test')`、`('600519.SH','贵州茅台',...)`、`('510300.SH','沪深300ETF','etf',...)`。这与审计 §1.3 完全一致，**仍是当前 live 状态**。影响面：涨跌停校验的板性/ST 判定、覆盖率 `_expected_symbols`、幸存者偏差检查、选股池 —— "不是跳过，而是拿着 3 行数据在跑"（审计 §1.3）。
- **universe 语义**：`factors/universe.py`（`resolve_index_code` / `universe_label` / `all_universe_options`，45 行）；`IndexConsRepo`（`catalog.py:223-277`）以 **`(index_code, eff_date)` 快照**为模型，`as_of(d)` 取"截至 d 的最近一次快照的**整体**"（防僵尸成分/幸存者偏差），`upsert` 用 `epoch_cols=("index_code","eff_date")` 整批替换。**【实测】live `index_cons` = 6 行**（极薄）。
- **行业分类（PIT）**：`IndustryClassifyRepo`（`catalog.py:182-220`），`as_of(d)` 取 `std_date <= d` 中每 symbol 最晚的一次，防"用今天的分类回测十年前"。**【实测】live `industry_classify` = 4,897 行**。
- **ETF 元数据 / per-instrument T+N**：`EtfMetaRepo`（`catalog.py:147-161`），`sellable_days(symbol)` 查不到回落 1。同步 `ingest/etf_meta.py`（`sync_etf_meta` / `enrich_from_akshare` / `sync_etf`），按名称关键字推断跨境/黄金/债券/货币 = T+0。**【实测】live `etf_meta` = 10 行**（极薄）。
- **退市**：`security.delist_date`；回填池对退市股把 `end` 截断到 `min(end, delist_date)`（`ingest/tasks.py:109-114`）。
  ⚠️ **审计 + E2E 缺口**：日线湖里**退市标的日线一行都没有**（审计 E2E 缺口表："security 有 339 条 delist_date，其中在 daily 出现过的 = 0"），所以长回测**幸存者偏差依旧存在**（`docs/BACKTEST_BASELINE_E2E.md:161`）。
- **停牌**：`is_suspended` 标记位存在，但 **`SUSPENDED` flag 从未被设置**（审计 §2.4-3：`flag_tradability` 是孤儿函数，`quality/pipeline.py:153-154` 明确拒绝接线）；停牌靠 `volume==0` 启发式。回测侧已区分 `suspended` / `no_volume`（`backtest/engine.py:185-192`）。

### 2.6 质量自检（五层验证）

`data/quality/` 模块与 `docs/DATA_ROBUSTNESS_AUDIT-2026-10-03.md` 的"已有"清单：

| 检查 | 位置 | 说明 |
|---|---|---|
| **记录级八项断言** | `quality/asserts.py`（168 行） | 价格区间 / OHLC 冲突 / 负量 / 单位失配 / 停牌填充 / 僵尸 / 复权跳变 |
| **入湖门禁** `gate_daily` | `quality/pipeline.py:47-74` | 主键去重（fatal）+ 记录级断言 + `quality_flags` 打标；**fatal 抛 `DataQualityError` 阻断整批入湖** |
| **湖级检查** `run_lake_checks` | `quality/pipeline.py:77-157` | 涨跌停约束（近端 400 天窗口）/ 日历对齐 / 覆盖度 / 僵尸日 / 复权因子单调 / 复权一致性对账 / PIT 幸存者偏差 / golden 已知答案集 |
| **涨跌停约束** `check_limit_breach` | `quality/validators.py:86-199` | 板性从**代码段**推（`board_from_code`，`validators.py:55-69`，因 `security.board` 对股票全 NULL）；容差按 tick 取整修正；新股上市初期豁免（`_FREE_DAYS` 主板/双创 5 日、北交所 1 日）；**实测修前 1191 行命中、修后 0 行**（`validators.py:102-103`） |
| **覆盖度** `check_coverage` | `validators.py:263-292` | 单日标的数 / 窗口中位数 < 90% → fatal（只抓"突降"，历史薄不误报） |
| **僵尸日** `check_zombie` | `validators.py:295-322` | 全市场 |ret|==0 占比 warn≥8% / error≥30% |
| **复权因子单调** `check_adj_factor` | `validators.py:325-349` | 后复权因子应单调不减；跳变 >50% warn |
| **前后复权收益率恒等** `check_ret_identity` | `validators.py:202-228` | 数学上必等，不等必是复权错。⚠️ **零调用方**（孤儿，审计 P3-15） |
| **日历对齐** `check_calendar_alignment` | `validators.py:231-260` | 数据日期须是官方交易日子集；完整年 240–245 天 |
| **湖覆盖度对账** `scan_coverage` | `quality/coverage.py:127-156` | 扫 `[today-days, today]` 的 daily / daily_basic 整日缺失 + 标的级稀疏；`repair=True` 时建 `daily_update` 修复任务 |
| **湖完整性**（**HEAD 新增**） | `store/integrity.py`（285 行） | ① 数据根自检 `check_data_root`（`DATA_ROOT_NESTED` / `SHADOW_LAKE`，fatal）；② **分区连续性** `check_partition_continuity`（`PARTITION_MISSING` fatal / `PARTITION_TRUNCATED` error，按年分区与全量日历对账） |
| **golden 已知答案集** | `quality/golden.py`（108 行）+ `golden_expected` 表 | `run_all()` 比对期望值 |
| **tradability / universe / adjustment / flags / issues** | `tradability.py`(91) / `universe.py`(66) / `adjustment.py`(60) / `flags.py`(43) / `issues.py`(169) | `flag_tradability` 孤儿；`issues` 负责落 `data_quality_issue` |

`pipeline.py:23-41` 另有 `check_lake_structure()` —— **只跑结构性检查（数据根 + 分区连续性），不物化全湖**，供启动自检/同步后门禁用；`run_lake_checks` 会先跑它（`pipeline.py:93-96`）。

**【实测】live `data_quality_issue` = 13,635 行**（有在跑）；`data_version` = 2,398 行。

⚠️ **审计已列但仍存在的结构性问题**（HEAD 未全修）：门禁只挂在日线调用方（`pipeline.py:47-52` 的 `gate_daily` 只被 `daily._stamp` 调，`ingest/daily.py:370`），其余数据集**无校验写入**（P1-3）；标为 fatal 的序列级检查**从不阻断**（`run_lake_checks` 只 `save_issues` 后返回，`pipeline.py:156-157`）且**根本没被调度**（只存在于手动 CLI 与 `POST /data/check`，P1-4）；QC 失败被误判为源站故障（`fallback.py:72`，P1-5）；`financial_pit` **无主键**导致重拉追加重复行（P1-9，`ddl.py:104-108`）；`write_factor` 既不加锁也不原子（P2-5，`parquet.py:448-452`）。

### 2.7 数据资产真实状态（【实测】2026-10-03）

对 live 湖/库的只读实测（与审计 §1 一致，**部分 P0 未修**）：

**日线湖分区**（`data/parquet/daily/`）：

| 年份 | 状态 |
|---|---|
| 2021 / 2022 / 2023 / 2024 / 2026 | 有 `part-0.parquet` |
| **2025** | ❌ **整年缺失** —— 目录里只有 `part-0.parquet.bak-demo`（9.2 MB，Sep 12）与 `part-0.parquet.lock`（0 字节），**没有真正的分区文件** |

审计 §1.1 的 P0 结论**仍然成立**：`read_daily` 用 `daily/**/*.parquet` glob，缺一年就是**静默少一年**；`latest_trade_date()` 返回 2026 仍"看着健康"。唯一副本在 `data/parquet.bak-demoincident-20260918/`（【实测】该备份目录存在于 `data/`）。HEAD 新增的 `check_partition_continuity` 现在**能发现**这个问题（`integrity.py:221-241`），但数据本身尚未恢复。

**DuckDB 表行数【实测】**：

| 表 | 行数 | 判断 |
|---|---:|---|
| `financial_pit` | **77,446,911** | 丰富（PIT 财务是最大资产） |
| `trade_calendar` | 13,527 | 完整 |
| `data_quality_issue` | 13,635 | 有 |
| `industry_classify` | 4,897 | 有 |
| `data_version` | 2,398 | 有 |
| `factor_def` | 160 | 有（Alpha158 已 seed 的痕迹） |
| `collect_log` | 133 | 有 |
| `backtest_run` | 11 | 有 |
| `etf_meta` | 10 | **极薄** |
| `index_cons` | 6 | **极薄** |
| `security` | **3** | **测试数据**（见 §2.5） |
| `ml_run` | 0 | 空 |
| `qlib_run` | 表不存在 | qlib 元数据在独立 sqlite，见 §12.10 |
| `daily_bar`/`minute_bar`/`adj_factor` | （死表，审计实测 0） | 遗留占位 |

其他审计结论（未复测但源码可证）：**minute 与 adj_factor 湖连目录都没有**（`data/parquet/` 下无 `minute`/`adj_factor`）；**无任何自动备份/快照/manifest/checksum**（P2-1，E1/E2 手段为零）；2021–2023 只有约 1/5 标的（P2-6）。

### 2.8 增量同步 / 任务 / 调度

- **断点续传**：`ingest/checkpoint.py`（189 行）。两种记账粒度（`checkpoint.py:8-19`）：`done`（键跑过）与 **`coverage`（键被实际拉取覆盖到哪个日期区间）**。关键 API：`mark` / `unmark` / `remaining(keys, start, end)` / `record_coverage` / **`covers(key,start,end)`**（区间完整包含，`checkpoint.py:132-134`）/ **`covered_until(key,start)`**（从 start 起**连续**覆盖到哪天，`checkpoint.py:150-162`）/ `covered_window`（**外框，仅供展示**，`checkpoint.py:136-148`）。审计 P0-4（外框掩盖跨洞）已由 `covers`/`covered_until` 修掉。文件 `data/cache/checkpoints/<name>.json`，原子写（tmp+replace，`checkpoint.py:176-186`）。⚠️ 无互斥锁、无形状校验（审计 P2-11）。
- **日线回填**：`ingest/daily.py` `backfill_pool`（`daily.py:82-228`）是核心逐批循环：pool 元素 `(symbol, end_date)`（退市股 end 被截断）→ 按 end 分组 → 按标的类别分流（ETF/LOF 走 `etf_daily` 能力源 + `etf_daily_bars`，`resolve_ingest_source`，`daily.py:52-79`）→ `TimeoutError` 缩批重试（SUB_BATCH=20，`daily.py:278-292`）→ **零行显式标 `empty_response`**（`daily.py:164-172`，修了"空响应被当成功标 done"）→ `write_daily(_stamp(...))`，质量门禁 fatal 拦批（`daily.py:173-180`）→ 连续 10 批全失败早停（`EARLY_STOP_BATCHES`，`daily.py:197-205`）。返回 **`unprocessed`**（从未尝试的尾部），调用方**绝不能把它们标 done**（`daily.py:112-115`、`:226-228` —— 审计 P0-2 的修复）。
- **数据任务状态机**：`ingest/tasks.py`（536 行）。`data_task` 表（`tasks.py:27-43`）；kind ∈ {`full_backfill`, `daily_update`}（`tasks.py:48`）；phase = stocks→etf（`tasks.py:49`）；状态 `pending → running → ok/partial/failed/interrupted`（`_finalize`，`tasks.py:219-265`）；**单任务互斥**（`create_task` 里 `status IN ('pending','running')` 则抛 `TaskConflictError`，`tasks.py:150-156`）；原子认领 `_claim_running`（`tasks.py:268-285`，防 TOCTOU）；`claim_retry` 原子认领 + unmark 失败标的（`tasks.py:392-431`）；`mark_interrupted_on_startup`（`tasks.py:453-467`）；进度经 `_progress_update` 逐批 UPDATE + `core/task_events.publish` SSE 推送（`tasks.py:191-216`）；收尾自动跨源对拍（`_auto_crosscheck`，`tasks.py:369-389`）。
  ⚠️ 互斥是**全局单任务**，长任务会阻塞所有新任务（审计 P2-15）。
- **调度**：`sync/manager.py`（721 行）—— `sync_job`/`sync_run` 表 + 后台 daemon 线程 `loop_forever(interval=30)`（`server/main.py` 启动时 spawn，`main.py:190-201`）；`seed_defaults`（`manager.py:156`）、`run_job`（`manager.py:321`）、`_is_due`/`_trading_day_ok`（`manager.py:634,670`）、`freshness`（`manager.py:568`）、`_checkpoint_lag_check`（`manager.py:257`）、`_post_sync_check`（`manager.py:282`）。CLI 侧 `lq sync tick/status/run/seed`（`cli/commands/sync.py`）。
  ⚠️ 作业在调度线程内**同步执行**，长任务阻塞其它到期作业与 30s tick（审计 P2-15）；`deploy/launchd/*.plist` 硬编码 `/Users/lyp/code/lquant`，无 Linux cron/systemd。
- **跨源对拍**：`ingest/crosscheck.py`（243 行）`run_crosscheck`；`config/providers.yaml` 的 `crosscheck` 节：primary=baostock，peers=[tushare]，tolerance 0.1%，字段 open/high/low/close/pre_close/volume；**不比**前复权价/行业/ETF 份额。⚠️ 只做**标记**不做仲裁（`fallback.py:3-4` 自述"仲裁至少需要 3 个可用源"，全仓无 quorum 实现）；peer 取不到数据时降级为 `{"L0":1}`，与"全部一致"不可区分（审计 P3-17）。
- **血缘 / 版本**：`data/lineage.py`（62 行）`new_version()`（`YYYYMMDD.n`）/ `register` / `latest` / `stamp`；`_stamp` 给每批打 `source/ingested_at/data_version`（`ingest/daily.py:356-380`）。⚠️ 先注册后写入、分配非原子 → 崩溃留"幽灵版本"（审计 P2-9）。
- **映射引擎**：`data/mapping.py`（370 行）声明式字段映射（`load_table_mapping` / `apply_mapping` / `validate_all_mappings`），支持 derive 表达式（`_compile_node`/`_compile_binop`/`_compile_call`/`_compile_concat`/`_compile_strptime`）；`data/providers/_engine.py` 的 `MappingProvider` 是通用适配器引擎。`config/schema/{daily_bar,minute_bar,news}.yaml` 是映射配置。
- **归一化**：`data/normalize.py`（112 行）`normalize_symbols` / `to_yuan` / `scale_unit` / `assert_plausible_prices` / `assert_ohlc` / `normalize_60min_bounds`。
- **数据字典**：`data/dictionary.py`（147 行）`dictionary()` 供 `/data/dictionary`。

### 2.9 数据层"能力 vs 落地"落差（审计 §2 摘要）

- 声明但**零实现**的 Capability：`CORPORATE_ACTION`、`INDUSTRY`、`ETF_SPOT`、`ETF_IOPV`、`ETF_SHARE`、`MONEY_FLOW`、`LIMIT_UP`、`DRAGON_TIGER`、`SECTOR`；`INDEX_DAILY` 只有 baostock 声明且**从不路由**；指数成分**连 Capability 都没有**（`ingest/index_cons.py:47-52` 直接 `TushareProvider()`）。
- `config/providers.yaml` 9 个源中 **3 个是死配置**（efinance/hithink 模块不存在、tickdb 预留）。
- 缺失维度：**公司行为事件表**（分红/送转/拆并）完全没有；**ST 历史**只有当前快照（但日线湖的 `is_st` 是逐日的，见 §5.3）；**停牌历史**靠启发式；**schema 版本**无（实测日线分区列集已漂移：2021–2023 25 列 vs 2024/2026 26 列）；**湖清单/校验和**（HEAD 已有分区连续性，但无 manifest/checksum）。

---

## 3. 因子系统（Factor system）

### 3.1 DSL 全链路

`factors/dsl/`：`lexer.py`(32) → `parser.py`(93) → `ast_nodes.py`(59) → `analyzer.py`(71) → `compiler.py`(118)，另有 `printer.py`(79，`unparse`/`canonical`/`canonical_id`) 与 `json_ast.py`(41，供前端画布)。

- **语法**：`$close`、数字、算子名、`+-*/()`、比较、逗号、`#` 行内注释（`lexer.py:1`）。
- **静态分析** `analyze()`（`analyzer.py:16-48`）：返回 `(min_window, fields, op_names)`；**未来函数检测** —— 算子名前缀命中 `{future, lead, next, forward}` 即抛 `LookaheadError`（`analyzer.py:12-13`、`:29-30`）；未注册算子抛 `FactorError`；`min_window` 由算子元数据 + 最后一个 int 参数推断。
- **`check(ast, allowed_fields)`**（`analyzer.py:51-70`）：额外做**字段白名单**校验，拼错字段（`$closs`）静态期报错并给 `difflib` 近似候选（"缺陷 #6"修复）。
- **编译** `compile_expr()`（`compiler.py:94-118`）：AST → Polars `Expr`；**数字实参直接传 Python 数**（不能 `pl.lit`，否则 `rolling_mean` 收到 Expr 会崩），整值窗口 `5.0→5`（`compiler.py:110-116`）。
- **嵌套 TS/CS 拆步** `has_nested_ts_cs` / `plan` / `_split`（`compiler.py:34-88`）：针对 **Polars issue #25691**（嵌套 `.over()` 被当层级分区，TS 与 CS 正交 → "不报错、结果错"）。判据是**任意深度**的异类窗口算子混排（含四则运算包裹）；有嵌套则后序拆步、每步物化、原位替换成 `__step{i}` 引用。
- **引擎** `FactorEngine`（`factors/engine.py:14-93`）：`_compute_column`（单因子，按 plan 分步物化 + 中间列命名防覆盖，`engine.py:18-52`）；`compute`（`engine.py:54-55`）；**`compute_many`**（`engine.py:57-93`）：DAG —— 一次 collect、逐因子累加列、可选共享预处理、**两级缓存**（`factors/cache.py` `TwoTierCache`：内存 LRU + 磁盘 parquet，key 含 defs/window/`data_version`/steps，`cache.py:34-106`）。

### 3.2 算子库

`factors/ops/registry.py`：`OPS` 注册表 + `@op(name, category=EL, min_window=0, label=)`（`registry.py:14-18`）；元数据 `category`(TS/CS/EL) 决定是否分步物化、`min_window` 是未来函数检测与预热期依据。

- **TS（时序，21 个）**：`ts_mean` / `ts_std` / `ts_return` / `ts_delay` / `ts_corr` / `ts_sum` / `ts_max` / `ts_min` / `ts_argmax` / `ts_argmin` / `ts_rank` / `ts_delta` / `ts_cov` / `ts_skew` / `ts_prod` / `ts_ema` / `ts_quantile` / `ts_slope` / `ts_rsquare` / `ts_resi` / `ts_wma`（`ops/ts_ops.py:11-126`）。
- **CS（截面，4 个）**：`rank` / `scale` / `demean` / `zscore`（`ops/cs_ops.py:10-25`）。
- **EL（逐元素，9 个）**：`abs` / `log` / `sign` / `sqrt` / `power` / `greater` / `less` / `signed_power` / `if`（`ops/el_ops.py:10-50`）。
- **目录自省** `ops/catalog.py`：`op_catalog()` / `infix_catalog()` 供前端画布动态生成积木（`catalog.py:46,119`）。
- **Rust 桥接** `ops/rust_bridge.py`：见 §1.5 —— **当前不覆盖 OPS**。

**字段白名单** `factors/fields.py`：`DAILY_FIELDS`（12 个，含主键列）、`NUMERIC_FIELDS`（10 个数值字段）、`FIELD_LABELS`（中文名）、`daily_fields()` / `field_catalog()`。注释明确它是"唯一真相源"，避免"画布能选到、引擎不认"（`fields.py:1-6`）。

### 3.3 预处理

`factors/preprocess/`：`registry.py`(51) + `winsorize.py`(62) + `standardize.py`(82) + `neutralize.py`(153) + `orthogonalize.py`(124) + `_regress.py`(150) + `pipeline.py`(107)。

- **注册表** `METHODS`，键为 `{stage}.{name}`（因为 `none` 四个 stage 都要有，`registry.py:5-6`）；STAGES = `("winsorize","standardize","neutralize","orthogonalize")`（`registry.py:18`）；`default_pipeline()` = MAD 去极值(5) → zscore → OLS 中性化(market_cap + industry_sw1)（`registry.py:41-51`）。
- **方法**：winsorize `mad/quantile/sigma3/clip/none`；standardize `zscore/minmax/rank/none`；neutralize `ols/ridge/lasso/industry_mean/none`；orthogonalize `symmetric/gram_schmidt/pca/none`（`docs/ARCHITECTURE.md:454-456`）。
- **流水线** `pipeline.run()`（`pipeline.py:50-97`）：`normalize_steps` **强制按标准顺序排序**（`STAGE_ORDER`，`pipeline.py:17,20-35`）—— 乱序 spec 会被纠正，否则"先中性化后去极值"静默出错；`keep_original` 写 `{col}_clean` 保留原值；正交化只在多列时执行（`pipeline.py:88-92`）；`drop_nonfinite` 同时剔除 null 与 NaN/Inf（`pipeline.py:105-107`）。
- **数学验证**（`docs/ARCHITECTURE.md:457-459`）：中性化把市值暴露 corr 从 -0.99 打到 8e-17；对称正交把 0.8 相关因子打到 1e-8。

### 3.4 评价体系（IC / 分层 / 衰减 / 归因 / 评级 / 稳健性）

`factors/evaluate/`（19 个模块，约 3,000 行）：

| 模块 | 关键 API | 指标 |
|---|---|---|
| `ic.py`(216) | `ic_series` / `ic_summary` / `ic_by_year` / `newey_west_tstat` / `ic_autocorr` / `_t_stat` | 逐日 Pearson IC + Spearman RankIC；均值/标准差/**IR**/`ir_annual`/t 值/**NW 一致 t 值**（日度 IC 强自相关下朴素 t 会高估 3–5 倍，`ic.py:106-125`）/正比例/阈值胜率/skew/kurtosis/IC 自相关/`n_days`。`STD_EPS=1e-9` 处理 polars 常数序列 std≈7e-18 的伪影（`ic.py:18-20`、`:151-153`） |
| `quantile.py`(199) | `add_quantile` / `group_returns` / `pivot_group_returns` / `quantile_nav` / `long_short_nav` / `quantile_summary` | 十分位分层、多空净值、单调性（Spearman） |
| `decay.py`(120) | `decay_profile` / `half_life` / `decay_summary` / `suggest_rebalance` | 衰减半衰期 + 建议调仓频率 |
| `attribution.py`(109) | `exposure` / `group_exposure` / `return_contribution` / `attribution_summary` | 行业/市值/流动性暴露分解 |
| `rating.py`(198) | `RatingThresholds` / `load_thresholds` / `factor_rating` | 收敛成 Strong/Moderate/Weak + 原因；阈值可配 `config/factors/rating.yaml` |
| `report.py`(478) | `factor_report` / `save_report` | **自包含 HTML 报告**（内嵌 SVG 折线/柱状/多线，无外部依赖，`report.py:52-190`） |
| `robustness.py`(478) | `param_sensitivity` / `time_stability` / `start_date_sensitivity` / `best_month_removal` / `oos_decay` / `robustness_summary` / `perturbed_expressions` | L3 稳健性：窗口扰动/分段稳定/起点敏感/剔除最佳月份/OOS 衰减。**默认关闭**（最贵一步，`evaluate/__init__.py:68-70`） |
| `costs.py`(112) | `factor_turnover` / `cost_matrix` | 换手率与成本敏感性（"高 IC 因子扣完双边万 15 就没了"） |
| `excess.py`(133) | `benchmark_series` / `excess_returns` / `excess_perf` / `group_excess_summary` / `quantile_excess_nav` | **超额收益体系**：年化超额/超额夏普/超额最大回撤/超额净值曲线 |
| `group_ic.py`(62) | `ic_by_group` / `size_group` | 分组 IC（识破"只是小市值暴露"） |
| `neutral_views.py`(111) | `return_neutral_ic` / `industry_group_quantile` / `neutral_views` | 三种"中性化"数学不等价，页面上必须标明（`neutral_views.py:1-2`） |
| `rolling.py`(59) | `rolling_ic` | 滚动 IC/RankIC/IR/胜率 |
| `event_study.py`(183) | `event_study` / `event_study_summary` | 事件式分层累计收益 |
| `outliers.py`(110) | `filter_zscore` / `zscore_filter_stats` / `zscore_filter_with_stats` | 截面异常收益过滤 |
| `style_corr.py`(110) | `style_correlation` | 中性化后残差因子还偷多少风格暴露 |
| `top_n.py`(98) | `top_n_returns` / `top_n_summary` | Top-N 持仓收缩测试 |
| `returns.py`(38) | `forward_return` / `forward_return_matrix` | 前瞻收益（评价体系的共同输入，"对齐错了 IC 会虚高"） |

**统一入口** `evaluate(df, factor, ret_col, n_groups=10, horizons, with_report=True, with_robustness=False, **kw)`（`evaluate/__init__.py:62-100`）：一次跑 IC / 分层 / 衰减 / 归因 / 评级 + 可选 HTML 报告 + 可选稳健性。

### 3.5 因子相关性 / 合成

`factors/analysis.py`(201)：`compute_factor_col`（内置 qlib 因子名 → `qlib_alpha.compute`；其余 → DSL；含 `pct_change_{n}` / `rolling_std_{n}` / `turnover` 捷径，`analysis.py:31`）、`build_matrix`、`correlation`（按日 Spearman + 冗余对判定）、`synthesize`（按日秩 zscore 等权/IC 加权，`analysis.py:163`）。

### 3.6 因子挖掘（mining-async）

`factors/mining/`：

- **门禁流水线 G0–G3**（`gates.py:35-114`）：**G0 静态**（parse + 字段白名单 + 未来函数，微秒级）；**G1 快筛**（train 段 + 中性化 IC，`min_abs_ic=0.02`；含 **SIZE_PROXY 判定** —— 中性化后 IC 衰减 >80% 判为风格代理，`gates.py:80-84`）；**G2 去重**（与幸存者 |rho| ≥ 0.7 淘汰）；**G3 val 解锁一次**（校正门槛 `corrected_threshold(n_evaluated)`，`runner.py:120-124`）。每个拒绝都返回**结构化 reason_code + 可操作 hint**（`gates.py:1-3`、`REASON_CODES`，`gates.py:14-22`）。
- **会话 runner** `run_session`（`runner.py:54-135`）：**70/15/15 时间切分**（搜索只用 train，val 只在 G3 解锁一次，**test 不碰**，`runner.py:45-51`）；逐候选走 G0→G1→G2→G3；GP 生成器可 `feedback(fitness)`；产出 `SessionResult`（含 `n_g1_pass`/`g1_ic_sum` 等搜索效率指标）。
- **生成器**：`random_gen.py`（random 基线，"任何生成器的验收 = 同预算下跑赢 random"，`random_gen.py:1`）、`gp.py`（AST 交叉 + 变异，`GPGenerator`）、`llm.py`（LLM 提案，`load_proposals`/`make_generator`）、`explore.py`（`make_explorer`，LLM 提案 → GP 种群初始化 → GP 精炼）。
- **适应度与多重检验校正** `fitness.py`（`corrected_threshold` / `fitness`，22 行）。
- **submit 即重验**（`submit.py:105-191`）：平台**不信任 Agent 的任何数字** —— 重跑 G0–G3（含 val 段），对照 `spec.claimed` 分级：`|Δ|≤0.005` → A；同向且 `≤0.02` → B；反向 → **D（研报存疑）**；否则 C。**无 `rationale` 直接拒**（`submit.py:115-120`）；`t_val` 为 NaN/非有限也拒（`submit.py:164-173`）；C/D 级落 `factor_replication` 表归档（`submit.py:217-236`）；A/B 级入 `factor_def`。`prepare_segment`（`submit.py:65-89`）是 train/val 切分、submit 重验、`lq factor audit`、`lq factor robust` **共用的唯一实现**（口径不分叉）。
- **协变量一等公民** `factors/covariates.py`(88)：`CovariateProvider` 注册表 + PIT 行业 + 覆盖率上报（`market_cap`/`industry_sw1`/`turnover_1m`/`momentum`），`CovariateUnavailable` 显式上报 coverage=0。
- **Agent 记账** `factors/agents.py`(134)：`AgentProfile` / `load_agents` / `record_eval` / `eval_usage` / `quota_remaining` / `ensure_quota`（配额 + 多重检验校正门槛随试验数上升）。
- **研报复现** `factors/replication.py`(60)：`FactorSpec` / `load_spec` / `attribute`（归因五类）。

### 3.7 因子来源适配器

`factors/sources/`：`__init__.py`（`register_source` / `list_sources`，"Adapter 只做翻译，绝不做执行"，`sources/__init__.py:1`）、`yaml_source.py`（`load_custom`/`factor_id`，把 `config/factors/custom.yaml` 接进统一管线）、`qlib_source.py`（见 §12.5）。

### 3.8 Alpha158 原生重实现（`factors/qlib_alpha.py`，316 行）

**这是 lquant 与 qlib 之间最重要的一座桥**，逐条对照 `microsoft/qlib` 的 `qlib/contrib/data/loader.py::Alpha158DL`：

- **构成**：kbar 9 + price 4 + rolling 29 族 × 窗口 `{5,10,20,30,60}` = **158 个**（`qlib_alpha.py:3-4`、`WINDOWS`，`:24`）。
- **入口**：`list_builtin()`（→ `[{name, family, window, formula}]`，驱动 API 与前端枚举）、`resolve_name()`（`'MA20'→('MA',20)`，`'KMID'→('KBAR',None)`）、`has_factor()`（不抛错探测，避免 `except KeyError` 吞掉数据缺列错误 —— "缺陷 #1"）、`compute(df,name)`（单因子 → `_factor` 列）、`compute_all(df, families, windows)`（批量）。
- **与 qlib 原版的差异**（都写在注释里，`qlib_alpha.py:6-10`）：`$vwap` 用 `amount/volume` 代理（volume≤0 退回 close）；Slope/Rsquare/Resi 用 **numpy 滑窗闭式解**；`Resi = sqrt((1-R²)·Var(y))`；`IdxMax/IdxMin` 窗口内位置 `0=最老一根` 再除以窗口长度。
- **实现细节**：`_prep`（排序 + 派生 `_vwap`/`_ret`/`_vchg`/`_pc`/`_pv`，**shift 必须按 symbol 分组** —— 否则跨股票泄漏，`qlib_alpha.py:140-141`）；`_expr`（match-case 逐族 Polars 表达式，均 `.over("symbol")`）；`_kbar_expr`；`_numpy_block`（BETA/RSQR/RESI/IMAX/IMIN/IMXD/RANK 七族逐 symbol 组滑窗）；**warmup NaN 统一 `fill_nan(None)`**（"polars 里 NaN≠null，drop_nulls 过滤不掉"，`qlib_alpha.py:299-301`）。
- **接线**：`factors/panel.py::compute_factor_columns`（内置名走 `qlib_alpha.compute`，其余走 DSL，`panel.py:15-41`）；`server/api/factors.py` 的 `/builtin`、`/seed-builtin` 端点；`factor_def` 已 seed 160 行【实测】。
- **测试**：`tests/unit/test_qlib_alpha.py`（151 行，10 个用例）—— 158 个注册、resolve_name、MA/ROC 手算对照、kbar 公式、BETA 斜率闭式、Rank 百分位语义、CNTP/SUMD 一致性、多 symbol 分组、warmup 是 null 不是 NaN、全窗口可算。

---

## 4. ML / 研究层（research/）

`src/lquant/research/` 共 1,445 行，分 `ml/`、`strategies/`、`dialect/`、`notes/`。

### 4.1 ML 选股（`research/ml/`）

**ADR-11：借 qlib 的 Model 三段式接口，不引入其运行时**（`research/ml/__init__.py:1-3`、`docs/ARCHITECTURE.md:284`）。

- **模型接口** `model.py`(167)：`Model` 基类（`fit` / `predict` / **`finetune`（默认退化为全量重训）** / `save` / `load`，pickle 序列化，`model.py:21-62`）。三个后端 + 自动降级：
  - `LGBMModel`（`name="lightgbm"`，默认 `n_estimators=300, lr=0.05, num_leaves=31, subsample=0.8, colsample=0.8`，`model.py:65-85`）；
  - `SklearnModel`（`name="gbrt"`，`GradientBoostingRegressor`；注释说明**刻意不用** `HistGradientBoostingRegressor` —— sklearn 1.9 参数名跨版本不稳，`model.py:99-105`）；
  - `RidgeModel`（`name="ridge"`，线性基准）。
  - `available_backends()`（lightgbm 要**真 import 一次**才知道动态库在不在，`model.py:140-154`）；`make_model("auto")` 按 `lightgbm > gbrt > ridge` 挑（`model.py:157-167`）。**降级动机**：macOS 缺 libomp 是常态（`model.py:7-8`）。
- **数据集** `dataset.py`(164)：`DatasetConfig`（features / `label_horizon=5` / `clip_label=0.2` / `label_rank` / `min_samples_per_day=10`，`dataset.py:24-37`）；`Dataset.split(train_end, valid_end, test_end)` **按日期边界**切（"绝不能随机打乱 —— 这是 ML 选股最常见也最致命的错误"，`dataset.py:3-5`）；`xy()` 返回 `(X, y, dates)`，X 用 `fill_null(0)` + `nan_to_num`；`label_rank=True` 时用截面排名作标签（`.rank("average")/count().over(date)`，`dataset.py:77-83`）；`build_dataset`（补前瞻收益 → 截尾 → 丢缺失 → **过滤稀疏日**）；**`walk_forward_splits`**（`train_months=24 / valid=6 / test=6 / step=6` 的滚动前移，"唯一能反映实盘的验证方式"，`dataset.py:134-156`）。
- **训练 + 回测串联** `backtest.py`(140)：`train_and_predict`（切分 → `make_model` → fit → predict → 用预测值直接算 IC，`backtest.py:43-63`）；`signal_backtest`（把信号喂给 `Engine`，默认 `rebalance="weekly"`，`backtest.py:66-85`）；**`run_ml_pipeline`**（一站式：建数据集 → 训练 → 预测 → 回测 → **落 `ml_run` 表**（R-ML5 实验管理），`backtest.py:88-140`）。
- **模型类型支持**：LightGBM / sklearn GBRT / Ridge（回归）。**没有**：分类、深度学习（LSTM/GRU/Transformer）、集成/堆叠、Alpha360、qlib 的 `LGBModel` 超参配方（那是 config/qlib 里的，§12.6）。

### 4.2 策略 / 方言 / 研报

- `research/strategies/baseline_multifactor.py`(183)：基准多因子策略（E2E 用）。**【未逐行验证】** 细节见 `docs/BACKTEST_BASELINE_E2E.md`：月度第 1 交易日开盘、top 20 等权、跌出 top 50 卖出、keep 带漂移（`BACKTEST_BASELINE_E2E.md:11`、`:89-90`）。
- `research/dialect/`：**聚宽（JoinQuant）方言兼容** —— `jq_shim.py`(239，`attribute_history` / `order_target_*` / `log` / `get_price` / `history` / `get_fundamentals` / `get_index_stocks` / `get_trade_days` 等)、`jq_import.py`(39，AST 扫描"导入即失败")、`mapping.py`(35，JQ API → 原生映射 + WARNINGS + UNSUPPORTED)、`fundamentals.py`(432，`get_fundamentals` 查询 DSL)。设计原则：**"导入即失败，绝不静默返回错误结果"**；方言只翻译 API 调用，**撮合/费率/T+N 全由内核 RuleSet 决定**（`docs/ARCHITECTURE.md:271-282`）。
- `research/notes/`：研报复现（M10），仅 `__init__.py`（5 行，**空骨架**）。

### 4.3 原生链路 vs 真 qlib 链路

`docs/qlib.md:55-59` 明确两者关系：`factors/qlib_alpha.py` + `research/ml/` 是**原生链路**；`qlib_io/` 是**真 qlib 链路**；两者"可互为交叉验证（qlib 源为 dump_bin 同构格式，公式口径一致）"。

---

## 5. 回测（Backtesting）

### 5.1 引擎选型结论

`docs/BACKTEST_ENGINES.md:5`：**自研事件引擎保留为唯一"执行真源"**；参数扫描走 Polars 向量化快扫（B5，近似、只筛参数）；外部引擎通过适配器协议接入，结果与原生同构。**不引入 backtrader / zipline / qlib backtest 作为核心执行器**（`BACKTEST_ENGINES.md:13-17`；qlib backtest 的拒绝理由：pyqlib 安装重、executor 复杂、数据层不同形）。RQAlpha 观望（商业许可 + 第二套撮合）。

### 5.2 执行模型（`backtest/engine.py`，595 行）

**防未来函数的核心在成交时点**（`engine.py:3-7`）：T 日收盘后策略看到 T 日 bar（含 close）生成目标权重；订单进 pending，**T+1 日按 `price_mode` 撮合**。默认 `price_mode="next_open"`；可选 `next_vwap`（amount/volume）/ `next_close` / `same_close`（**危险，仅研究对照，显式警告**，`engine.py:38`、`broker.py:46-48`）。

**逐日循环**（`engine.py:255-289`）：
0. **公司行为** `_apply_corporate_actions`（除权日按 `adj_factor` 比值放大持仓份额与**挂单股数**；分红默认再投资的份额调整法；停牌日不调整，复牌一次性补齐。**D1 修复**：挂单缩放必须与是否已持仓无关，`engine.py:293-324`）；
0b. **退市核销** `_apply_delistings`（退市日起按 `delist_recovery` 残值率变现，**不再按最后收盘价永久冻结**；只遍历有退市日的标的以省算力；D8，`engine.py:334-362`）；
1. **撮合上一日挂单** `_fill_pending`（`engine.py:366-408`）；
2. **调仓** `_schedule_rebalance`（`engine.py:410-527`）；
3. **按收盘价估值** `account.nav(prices, self._last_close)`（停牌股用**最近一次有 bar 的 close**，不是 avg_cost，`account.py:88-104`）；NAV ≤ 0 直接抛（`engine.py:280-284`）。

**调仓语义**（`engine.py:410-527`）：权重 = **目标市值 / NAV 的绝对占比**（残差留现金），**不做归一化** —— "归一化会把 0.5 半仓放大成 1.0 满仓，破坏网格/ATR 定仓"（`engine.py:430-433`）；单标的上限 `max_position_weight`；**先卖后买**（A 股卖出资金当日可用，`engine.py:447-491`）；`min_order_value` 过滤小额；`w <= 0` 显式清仓（`engine.py:418-420`）。

**调仓频率** `_should_rebalance`：`daily` / `weekly`（ISO 周） / `monthly` / `none`（`engine.py:563-576`）。

**`EngineConfig`**（`engine.py:34-49`）：`initial_cash=1e6`、`rebalance="daily"`、`price_mode="next_open"`、`slippage="pct"`、`participation=0.1`（单只最多吃当日成交量比例）、`max_position_weight=1.0`、`cash_buffer=0.001`、`min_order_value=1000`、`delist_recovery=0.0`、`insufficient_cash="reject"`（或 `truncate`）。

**`build_rules`**（`engine.py:79-109`）：给每标的生成 `InstrumentRules`，meta 可覆盖 `sellable_after_days`/`fund_type`/`is_st`/`track_index_limit`/`no_price_limit`；缺省从 `security` 表读 is_st、退市日、名称 → **原生 Engine 路径与 JQ 路径共用同一套 per-instrument 元数据**（D6）。

**`prepare`**（`engine.py:151-219`）：长表 → `{date: {symbol: Bar}}`；补 `volume/amount/adj_factor/halted` 默认；`is_st` 逐日（缺失保留 null，由 rules 退回静态值）；`halted = is_suspended | halted | volume==0`，**并单独标记 `suspended` / `no_volume`**（D12 归因分离）；`extra_fields` 塞进 `Bar.fields` 供策略读因子。

### 5.3 撮合（`backtest/broker.py`，229 行）

- **涨跌停不可成交**（`broker.py:90-135`）：取 `InstrumentRules.limit_up/limit_down`（**按 tick 取整**）；`is_st` 用**当日 bar 的真实戴帽状态**，None 才退回静态值；`next_open` 模式在**开盘价**处先筛一道，随后**在真实成交价处再校验一次**（补齐 `next_close`/`next_vwap` "平开收板"的漏洞）。
- **停牌 / 无成交量拒单**（`broker.py:96-99`、`engine.py:387-393`）：`halted` 拒单，reason 区分 `"无成交量"` vs `"停牌或无行情"`。
- **数量约束**：先按 `max_qty`（`participation × volume`）/ 资金 / 整手截断，**再算滑点**（"冲击成本必须按实际成交数量计"，H1 教训，`broker.py:113-119`）；`allow_odd_lot`（清仓零股一次性卖出，A 股规则，D2，`broker.py:224-226`）。
- **限价单** `Order.limit_price`（D13，`broker.py:137-155`）：用**不含滑点的基准价**判定是否可成交，成交价封顶/保底到限价；当日有效，不成交即作废。
- **资金不足**（`broker.py:157-180`）：`reject`（整单作废，券商/backtrader 语义）/ `truncate`（按可用资金截量，聚宽 `order_value` 语义）；**绝不让现金变负（隐性杠杆）**。
- **费用**（`broker.py:182-196`）：`amount × transfer_fee_rate` + `amount × r.tax_rate(d, side)`（**印花税按日期区间 + 买卖方向**，2008-09-19 起才单边，D8）+ 佣金 `max(min, 累计成交额 × rate)` —— **最低佣金按订单累计**，分次成交只收一次（`broker.py:187-195`）。
- **滑点** `backtest/slippage.py`（85 行）：`PctSlippage`（默认，方向性：买上滑/卖下滑）/ `TickSlippage` / `VolumePctSlippage`（平方根冲击模型，Kyle 形式）/ `NoSlippage`；`apply` 统一四参签名 `(price, side, qty, volume)`（"签名统一让 Broker 可以无脑传全量参数，VolumePctSlippage 不再被静默降级"，`slippage.py:9-11`）；**规则表 `cn_a_share.yaml` 的 `slippage` 是唯一真源**（D9，`engine.py:122-134`）。

### 5.4 账户（`backtest/account.py`，104 行）

`Position`（qty / avg_cost / **lots[(buy_date, qty, price)]**）；`available_at(d, rules, date_index)` —— **T+N 按交易日序号算，不是自然日**（D3，`account.py:22-40`）；`apply_corporate_action`（qty×ratio、avg_cost÷ratio、lots 同步）；`Account.apply_fill`（买加仓/卖 FIFO 消 lots）、`nav(prices, last_prices)`。

### 5.5 规则表（`backtest/rules/model.py`，270 行）

设计要点（`rules/model.py:1-12`）：T+N 是 **per-instrument** 属性；印花税有**生效区间 + 买卖方向**；最低佣金按订单累计；涨跌停价**按最小变动价位取整**；**ST 涨跌幅不是一律 5%**（双创 ST 仍 20%、北交所 ST 仍 30%）。

- `round_tick(px, tick)`（`model.py:55-64`）：股票 0.01 / 基金 0.001；浮点噪声用 tick 小数位再 round 一次清掉（"否则 3.99 变成 3.9900000000000002，与 bar.open 相等比较失守"）。
- `TaxSchedule`（`model.py:67-95`）：按区间 + 方向取税率，兼容 3 元组；无匹配抛 `RuleNotFound`。
- `Commission`（`rate` / `min` / `per_order=True`）。
- `PriceLimit`（`model.py:105-138`）：`mode=by_board`（个股）或 `by_track_index`（ETF）；`st_by_board`；`by_code_prefix`（长前缀优先）/`by_code` 兜底。
- `infer_fund_type(name, keywords)`（`model.py:141-154`）：按基金名称关键字推断 qdii/commodity/bond/money → T+0（数据层没有 fund_type 字段，关键词表在 yaml）。
- `InstrumentRules`（`model.py:157-202`）：`tax_rate` / `limit_ratio(is_st)` / `limit_up` / `limit_down`（后三者按当日 is_st 覆盖；`no_price_limit` 时返回 None 跳过校验）。
- `RuleSet.for_symbol(...)`（`model.py:205-270`）：组装 per-instrument 规则，`sellable_after_days` 显式传入时**优先于类型默认**。
- 规则数据在 `config/rules/cn_a_share.yaml`，加载器 `backtest/rules/loader.py`（`load_ruleset` / `default_slippage`）。

### 5.6 绩效指标（`backtest/metrics.py`，241 行）

`perf_from_returns(returns, dates, periods_per_year=252, risk_free=0.0)` 返回：

`n_periods`、`total_return`、`annual_return`（**几何**，`(1+total)^(252/n)-1`）、`annual_vol`、**`sharpe`**、**`sortino`**（标准下行偏差，所有周期计入分母）、**`max_drawdown`**（+ `max_dd_peak_idx` / `max_dd_trough_idx`，基于**含初始峰值 1.0** 的净值序列）、`calmar`、`win_rate`、`payoff_ratio`（**日度口径，不是交易级盈亏比**，`metrics.py:99-100`）、`underwater_periods`、`longest_dd_periods`、`skew`、`kurtosis`、`best_period`、`worst_period`、`start`、`end`、`nav`。
另有 `perf_from_nav`、`monthly_returns`、`turnover_from_trades`（`total_amount` / `turnover_per_period` / `unit`）、`summary_line`。

`Engine._finalize`（`engine.py:580-595`）把 perf + `initial_cash`/`final_nav`/`n_trades`/`n_rejected`/`total_fee`/`turnover` 合成 `res.metrics`。

⚠️ **没有**：超额收益 / 基准对比指标（原生引擎层面；`factors/evaluate/excess.py` 与 `/api/backtests` 的 benchmark 对齐是另一条路，`server/api/backtests.py:154` `_benchmark_nav_aligned`）、信息比率、跟踪误差、换手率以外的交易成本分解、行业归因（`backtest/attribution.py` 233 行存在，`GET /backtests/{id}/attribution`，`backtests.py:506`）。

### 5.7 验证方法学

- `docs/BACKTEST_VALIDATION.md`（184 行）+ `_BENCHMARKS.md`（95 行）：与 **backtrader 对账**（`scripts/backtest_validation/validate_accuracy.py`），3 个任务（baseline_multifactor / momentum_rotation / grid_trading）；⚠️ 当前**无法重跑**（依赖 510300.SH/159915.SZ 的 2024 起日线，湖里只有 2026 起；其中 2 个历史上是 FAIL，`BACKTEST_BASELINE_E2E.md:163`）。
- `backtest/selfcheck.py`（315 行）：引擎自检（锁定夏普几何口径等）。
- `backtest/validation.py`（58 行）：`GET /backtests/validation`（`backtests.py:696`）。
- `backtest/sweep.py`（392 行）：`run_sweep`（事件，逐档 `Engine`）/ **`run_sweep_vectorized`**（Polars 近似，只筛参数）/ `run_sweep_auto`（档数 ≥ `VECTOR_AUTO_MIN_POINTS=12` 走向量化）。向量化语义：调仓日按因子降序取前 top_n 等权、T+1 **开盘**建仓、排名 `cum_sum` 一次覆盖所有 top_n 档位。近似成本模型（`_approx_cost_rates`）**未建模**：最低佣金、tick/volume_pct 滑点、T+1 可卖、涨跌停/停牌/退市、手数取整、participation、资金不足拒单、除权复权（`BACKTEST_ENGINES.md:70-76`）。**正确性闸**：`tests/unit/test_sweep.py::test_sweep_vectorized_ranking_matches_event_engine`（秩相关 ≥0.9、argmax 一致、绝对差 ≤2pp；实测秩相关 1.0、差 <1pp）。**实测速度**：120 标的 × 500 日、monthly，事件 ~528 ms/档 vs 向量化 ~7.2 ms/档（20 档快 ~73×）（`BACKTEST_ENGINES.md:89-91`）。
- `backtest/adapter.py`（123 行）：`BacktestAdapter` 协议 + `register_adapter`，外部引擎结果与原生 `Engine.run()` 同构（`AdapterOutput`），落同批表，`/compare` 天然可比（`BACKTEST_ENGINES.md:25-41`）。
- `backtest/jqapi.py`（1,351 行）+ `jq_fundamentals.py`（58）+ `sandbox.py`（144）：聚宽策略 API 的完整实现与沙箱（`JQRunner`），是 `/backtests/run-code` 的执行体（`backtests.py:395-450`）。

### 5.8 退出策略（`backtest/exit/`）

`base.py`(179) + `tiered.py`(140) + `overlay.py`(135) + `pressure.py`(115) + `simple.py`(114) + `registry.py`(32) + `__init__.py`(60)；`GET /backtests/exit-strategies`（`backtests.py:204`）。**【未逐行验证】** 细节。

---

## 6. 组合构建（Portfolio construction）

`src/lquant/portfolio/` 仅 544 行 —— **这是相对 qlib 最薄的一层**。

### 6.1 权重（`portfolio/weighting.py`，237 行）

`METHODS` 注册 5 种（`weighting.py:206-212`）+ 2 个按分数/市值的辅助：

| 方法 | 实现 | 备注 |
|---|---|---|
| `equal_weight` | 1/N | "样本外最稳健的基准，所有优化结果都该先和它比"（DeMiguel，`weighting.py:3-4`） |
| `score_weight` | 分数归一化（可选仅正） | `weighting.py:48-59` |
| `market_cap_weight` | 市值加权，可选 `sqrt` | `weighting.py:62-71` |
| `inverse_vol_weight` | 逆波动率 | 风险平价一阶近似，几乎不需协方差估计 |
| `risk_parity_weight` | **等风险贡献（ERC）**，`scipy.optimize.minimize` SLSQP | 失败**降级为逆波动率**；建议 ≤100 只 |
| `min_variance_weight` | 最小方差，SLSQP + `max_weight` 约束 | 失败降级为逆波动率；"对协方差估计误差最敏感，慎用" |
| `hrp_weight` | **层次风险平价**（de Prado）：相关距离 → 层次聚类（`scipy.cluster.hierarchy`）→ 递归二分按簇内方差反比分配 | 不做矩阵求逆，对估计误差最稳健；`link` 可选 |

`weights(returns, method, symbols, **kw)`：**未知方法退回等权而不是报错**（`weighting.py:215-220`，"权重算不出来不该让整个流程崩"）。`weight_report`（`weighting.py:223-237`）：各方案组合波动 / 有效持仓数（1/HHI）/ max_weight / n_holdings。

**没有**：cvxpy、均值-方差（Markowitz 显式收益项）、Black-Litterman、因子风险模型、风险预算、跟踪误差约束、换手/交易成本约束、行业/风格约束、整数手约束。

### 6.2 选池与去重

- `portfolio/screener.py`（147 行）：多因子打分（zscore 加权）+ 过滤（流动性/上市天数/ST）。证据：`docs/ARCHITECTURE.md:471`（"screener 多因子打分（zscore 加权）+ 过滤（流动性/上市天数/ST）"）。**【未逐行验证】**
- `portfolio/dedup.py`（113 行）：相关性去重（按流动性优先保留）。证据：`docs/ARCHITECTURE.md:473`。**【未逐行验证】**
- `portfolio/__init__.py`（47 行）：导出面。**【未逐行验证】**

⚠️ **组合逻辑也散落在别处**：`backtest/strategy/factor_topn.py`（23 行，TopN 等权策略，是 `run_ml_pipeline` 的默认策略）；`factors/evaluate/top_n.py`（Top-N 收缩测试）；`factors/evaluate/quantile.py`（分层多空）。**没有一个统一的"组合构建 → 约束 → 优化 → 交易清单"管线**。

`config/factors/custom.yaml` 存默认预处理配方（`docs/ARCHITECTURE.md:55`）。`docs/PORT_FINANCIALTOOL.md`（351 行）记录了 FinancialTool 移植；`pyproject.toml:46` 有 `skfolio>=0.6`（EP-6 组合权重扩展点），但**代码里未见 skfolio 使用**（grep 未命中；**【不确定】** 可能在 scripts/ 或未接线）。

---

## 7. 模拟盘 / 实盘（Paper trading）

`src/lquant/paper/` 共 1,234 行。定位："从回测走向实盘的中间态"（`paper/__init__.py:1`）。

| 模块 | 行数 | 职责 |
|---|---|---|
| `engine.py` | 392 | `PaperConfig` / `PaperPosition` / `PaperOrder` / **`PaperBroker`**（`submit` / `_rules` / `apply_corporate_action` / `_buy_need` / `on_quote` / `_fill` / `on_day_close` / `nav` / `positions_frame`）/ **`PaperEngine`**（`push` / `replay` / `summary` / `orders_frame`）。事件驱动撮合、T+N 冻结/解冻、涨跌停拒单、资金不足拒单（带 reason）（`docs/ARCHITECTURE.md:482`） |
| `store.py` | 306 | **SQLite** 状态持久化（独立于 DuckDB）；`db_path` / `_migrate` / `_conn` / `AccountNotFound` / `account_lock` / `create_account` / `get_account` / `list_accounts` / `load_broker` / `save_broker` / `set_universe` / `record_nav` / `nav_frame` |
| `service.py` | 200 | 运行编排：`create_account` / `submit_order` / `cancel_order` / **`tick`** / `day_close` / `status` / `nav_history`；`resolve_strategy` + `_Manual` 兜底；`_is_trading_day` |
| `quotes.py` | 139 | **实时行情快照源：东财 push2，走 `em_get` 限流**（`quotes.py:1`）；`in_trading_hours` / `in_close_auction` / `now_cn_sh` / `limit_prices` / `_secid` / `_parse_item` / `fetch_snapshot` |
| `reconcile.py` | 92 | **收盘对账：用官方日线重算当日 NAV，覆盖盘中近似值**（`reconcile.py:1`）；`reconcile` / `_official_closes` / `_intraday_nav` |
| `alert.py` | 89 | 偏差告警：`DeviationReport` / **`compare_nav`**（分级 ok/warning/critical）/ `compare_trades` |

**调度**：无独立 daemon；由 API 驱动 —— `POST /api/paper/tick`（`server/api/paper.py:209`）、`POST /api/paper/close`（`:218`）；CLI `lq paper create/order/tick/cancel/close/status/nav`（`cli/commands/paper.py:22-92`）。
⚠️ **没有**：定时轮询调度器（需外部 cron/人工触发）、真实券商接口、盘中止损/风控引擎、多账户并行（有 accounts 但串行）。

---

## 8. 监控 / 运维（Monitoring / ops）

`src/lquant/monitor/` 共 1,304 行。设计：采集（中间件/sampler/worker 事件）→ flusher 落盘 → 查询（`monitor/__init__.py:1`）。

| 模块 | 行数 | 职责 |
|---|---|---|
| `api_mw.py` | 114 | **纯 ASGI 计时中间件**：请求开始 → `http.response.start`（**TTFB 口径**）；`_classify`（状态码分类）/ `_should_skip` / `_route_template` / `_monitor_enabled` / `_record_error` |
| `flusher.py` | 343 | **http 进程内唯一写 `monitor.duckdb` 的落盘线程**（`flusher.py:1`）；`ensure_tables` / `_write_api` / `_write_task` / `_pop_redis_events` / `_requeue_redis_events` / `_drain_sys_samples` / `_write_sys` / `_write_errors` / `_cleanup`（按天清理）/ `flush_once` / `start_flusher` / `stop_flusher` |
| `proc_sampler.py` | 119 | 进程自采样 cpu/mem（psutil，缺失降级 None）→ **Redis SETEX 15s**（`proc_sampler.py:1`）；`sample_once` / `build_payload` / `_loop` |
| `queries.py` | 270 | 查询层：`monitor.duckdb` 聚合 + Redis 快照 + 主库 `collect_log`；`range_bucket`/`range_sec` / `api_latency_series` / `slowest_routes` / `task_latency_series` / `queue_depths` / `proc_statuses` / `api_live` / `recent_task_events` / `data_pulls` / `summary` / `error_logs` |
| `worker.py` | 150 | `MonitoringWorker`（任务事件发射）+ `current_job_fn`（worker registry 匹配）；`spawn_worker` / `_spawn_process` / `run_supervisor` |
| `emit.py` | 83 | 任务生命周期事件统一出口；`_build_event` / `emit_task_event`（经 Redis） |
| `logs.py` | 86 | 监控页运行日志：tail `logs/lquant.log` + 级别过滤 + 关键字搜索（`logs.py:1`）；`tail_app_logs` |
| `ring.py` | 60 | 环形缓冲（**`class ApiRing[T]:` 用了 PEP 695 泛型语法，需 Python 3.12+** —— 我用 3.9 的 ast 解析它时报 SyntaxError，这本身是一条证据） |
| `types.py` | 50 | 不可变 frozen dataclass：`ApiMetricPoint` / `ApiErrorPoint` / `ProcSample` / `TaskEvent` |

**API**（`server/api/monitor.py`，prefix `/monitor`）：`GET /summary` / `/workers` / `/api-latency?range=1h` / `/tasks?range=1h` / `/data-pulls` / `/error-logs?range=24h` / `/app-logs?level=&...`。

**任务中心**：`server/api/task_center.py`（prefix `/tasks`）：`GET ""`（kind 过滤）/ `GET /summary` / `POST /{kind}/{task_id}/retry` / `POST /{kind}/{task_id}/cancel`；`KINDS = ("data","sync","backtest","factor","qlib")`（plan Task 6）；`_job_items(queue, kind, limit)` 归一。

**队列 / 取消**（`server/jobs.py`，531 行）：`QUEUES = ("lquant-default","lquant-ingest","lquant-backtest","lquant-mining", "lquant-qlib")`（`jobs.py:19`）；**Redis/RQ 优先，无 Redis 自动降级为 API 进程内本地线程**（`LocalJob`，`jobs.py:234-263`；`_probe_redis`/`_redis_available`，`:189-223`）；`_LOCAL_JOBS` 容量 200（`:265`）；**协作式取消** `request_cancel(job_id)` → 置 `_canceled` 标记，`enqueue` 通过**签名内省**自动注入 `cancel_check`/`progress` 形参（`jobs.py:356-412`）；`get_job` / `list_recent_jobs` / `_recent_job_records` / `mark_interrupted_jobs`（启动时把遗留 running 标 interrupted，`jobs.py:152`）/ `_record_flusher` 线程落 job 记录。`core/task_events.py` 另有 SSE publish/subscribe。

**运维脚本**：`scripts/backup.py`（DuckDB CHECKPOINT 后复制 + Parquet zip，保留 N 份，`docs/ARCHITECTURE.md:544`）；`lquant.sh doctor`；`/api/health` 暴露 redis/rust/数据覆盖度（`server/api/health.py:17-43`）。

⚠️ 已知缺口（审计）：`GET /sync/freshness` **不存在但前端在调**（`web/src/app/sync/page.tsx:62`、`FreshnessHealthCard.tsx:66`）→ 新鲜度卡片永远 unknown/404（P2-1，**契约破坏**）；陈旧/源挂**无告警出口**（P2-2，`EXTENSION_POINTS.md:14` 明确"不做"通知告警）；HTTP 触发的 data_task 失败在监控里不可见（P2-3）。

---

## 9. 服务端 / API（Server/API）

### 9.1 框架与装配

**FastAPI**（`pyproject.toml:54`）+ Pydantic v2。`server/main.py::create_app()`（`main.py:34-136`）：

- `setup_logging()` 统一接线（server 与 sync worker 共用，`main.py:35-36`）；
- **`duckdb.IOException` 异常处理器**：含 "lock" 的映射为 **503**（"数据湖忙，请稍后重试"），其余原样 500（`main.py:40-55`）；
- CORS 只允许 `http://localhost:3000`（`main.py:56-61`）；
- 20 个 router 以 `prefix="/api"` 挂载：`health data data_admin factors backtests market paper watchlist strategies analyses sync etf news settings ask agent qlib task_center monitor fundamental`（`main.py:62-68`）；
- `ws.router` **无 `/api` 前缀**（`/ws/jobs/{id}`，与前端代理一致，`main.py:69`）；
- **A2A**：`/.well-known/agent-card.json` + `POST /a2a` 单独 include（RFC 8615，不能吃 `/api` 前缀，`main.py:70-73`）；
- startup 钩子：A2A 未鉴权警告、`start_monitor()`、`seed_defaults()` 同步作业播种、`print_status()`（Rust ABI）、`mark_interrupted_jobs()`、**DDL 全量执行 + 4 个幂等迁移**（`ensure_factor_def` / `ensure_collect_log` / `ensure_classify_snapshots` / `ensure_factor_def_columns`）、data_task 中断标记、news 任务恢复、**sync worker daemon 线程**（`LQ_SYNC_WORKER=0` 可关）、**启动兜底补齐大盘数据**（`ensure_market_coverage(days=90)`）（`main.py:74-204`）；
- shutdown：`stop_monitor()` + `shutdown_agent_service()`（aiosqlite 非 daemon 线程必须显式关，`main.py:118-128`）；
- `app = create_app()` 被 `MonitorMiddleware` 包裹（`main.py:130-131`）。

### 9.2 响应封套（`server/envelope.py`，107 行）

`{code, data, message, trace_id}`；`ok()` / `err()` / `new_trace_id()`；`EnvRoute` 复写 `get_route_handler`：成功 return 收进信封；`HTTPException` → 同构信封**保留 status_code**（`code=1`）；`RequestValidationError` → 422 信封 + 人话消息（`_first_validation_error`）；已带 `code` 的 payload **不二次包**；非 JSON / StreamingResponse 原样放行；**必须丢掉旧 Content-Length** 让 Starlette 重算（"否则浏览器读到截断/错帧 body"，`envelope.py:104-107`）。

⚠️ **封套是"显式挂载"的**：只有新路由（etf / settings）用 `make_router()`；旧路由（market/data/factors/backtests…）**返回裸 dict/list**，被大量测试断言（`envelope.py:5-10`）。所以 API 响应形状**不统一**。

### 9.3 API 表面（按模块分组）

> 全部来自 `server/api/*.py` 的装饰器 grep。除 qlib 外均未逐行读语义，标注 **【未逐行验证】**。

| 模块 | prefix | 端点 |
|---|---|---|
| `health.py` | `/health` | `GET /ping`、`GET ""`（redis/rust/覆盖度） |
| `data.py` | `/data` | `GET /ping`、`/coverage`、`/coverage/monthly`、`/gaps`、`POST /gaps/repair`、`GET /securities`、`/daily`、`/indicators/registry`、`/indicators`、`/quote`、`POST /reference/sync`、`/index-cons/sync`、`/tasks`、`GET /tasks`、`GET /tasks/{id}/events`(SSE)、`GET /tasks/{id}`、`POST /tasks/{id}/retry`、`POST /check`、`GET /checkpoints`、`DELETE /checkpoints/{name}`、`POST /crosscheck`、`GET /crosscheck/issues`、`POST /crosscheck/issues/resolve` |
| `data_admin.py` | `/data` | `GET /issues`、`POST /issues/resolve`、`GET /version/latest`、`/versions`、`POST /purge`、`GET /export`、`/dictionary` |
| `factors.py` | `/factors` | `GET ""`(列表)、`POST ""`(注册)、`POST /validate`、`GET /ops`、`/fields`、`POST /ast`、`PUT /{name}`、`DELETE /{name}`、`POST /evaluate`(202 异步)、`GET /evaluate/{job_id}`、`POST /evaluate/series`、`POST /seed-yaml`、`GET /universes`、`/sources`、`/preprocess/methods`、`/builtin`、`POST /seed-builtin`、`GET /agents`、`/agents/{name}/guide`、`/mine/runs`、`/mine/runs/{run_id}`、`POST /mine/run`、`GET /reports`、`/reports/{name}`、`GET /{name}`、`POST /analyze`、`POST /synthesize` |
| `backtests.py` | `/backtests` | `GET /exit-strategies`、`POST /sweep`、`GET /sweep/{id}`、`POST /run`、`POST /run-code`、`GET /run-code/{job_id}`、`GET /{run_id}/code`、`/{run_id}/attribution`、`/{run_id}/holdings`、`GET ""`(列表)、`GET /compare`、`GET /validation`、`POST /run-benchmark`、`GET /{run_id}` |
| `qlib.py` | `/qlib` | 见 §12.3（9 个端点） |
| `market.py` | `/market` | `GET /overview`、`/sectors`、`/money-flow`、`/limit-up`、`/dragon-tiger`、`/collectors`、`/index`、`POST /collect`、`POST /backfill`、`GET /collect-status`、`/breadth`、`/batch`、`/snapshot`、`/heat`、`/schedules` |
| `paper.py` | `/paper` | `POST /replay`、`GET /state`、`POST /compare`、`GET /accounts`、`POST /accounts`、`POST /order`、`/cancel`、`/tick`、`/close`、`GET /nav/{name}` |
| `watchlist.py` | `/watchlist` | `GET ""`、`POST ""`、`DELETE /{symbol}` |
| `strategies.py` | `/strategies` | `GET ""`、`POST ""`、`POST /validate`、`GET /{sid}`、`PUT /{sid}`、`GET /{sid}/versions`、`DELETE /{sid}` |
| `analyses.py` | `/analyses` | `GET ""`、`POST ""`、`GET /{aid}`、`PUT /{aid}`、`DELETE /{aid}` |
| `sync.py` | `/sync` | `GET /jobs`、`POST /jobs`、`POST /jobs/{sync_id}/toggle`、`DELETE /jobs/{sync_id}`、`POST /run`、`GET /history`、`/coverage`（⚠️ **无 `/freshness`**） |
| `etf.py` | `/etf` | `GET /meta`、`/by-symbol/{symbol}`、`/correlation`（**封套路由**） |
| `news.py` | `/news` | `GET /items`、`/industries`、`/sources`、`/tasks`、`POST /tasks`、`POST /tasks/{id}/retry`、`GET /summary` |
| `settings.py` | `/settings` | `GET ""`、`PUT /{key}`、`DELETE /{key}`、`GET /providers`（**封套路由**） |
| `fundamental.py` | `/fundamental` | `GET /metrics`、`/score`、`POST /scores`、`GET /percentiles`、`POST /reconcile` |
| `task_center.py` | `/tasks` | `GET ""`、`/summary`、`POST /{kind}/{task_id}/retry`、`/{kind}/{task_id}/cancel` |
| `monitor.py` | `/monitor` | 见 §8 |
| `ask.py` | （无 prefix，挂 `/api`） | `POST /sessions`、`GET /sessions`、`PATCH /sessions/{sid}/config`、`DELETE /sessions/{sid}`、`POST /sessions/{sid}/cancel`、`GET/POST /sessions/{sid}/messages`、`POST /sessions/{sid}/regenerate`、`/fork`、`GET /runs`、`POST /runs/{sid}/kill` |
| `agent.py` | （无 prefix） | `GET /capabilities`、`GET /skills/{name}`、`PUT /skills/{name}`、`DELETE /skills/{name}` |
| `a2a.py` | （无 prefix） | `GET /.well-known/agent-card.json`、`POST /a2a`（JSON-RPC + SSE 流） |
| `ws.py` | `/ws` | `WS /ws/jobs/{id}`（轮询 jobs 状态推真实进度，本地/RQ 双模式，终态关连接） |

**鉴权**：**没有**。`docs/ARCHITECTURE.md:584` 明确"API 鉴权（本地单机工具暂无必要）"；仅 A2A 有 `a2a_token()`（`a2a.py:48`）与未鉴权启动警告（`main.py:75-77`）。

---

## 10. Web UI（`web/src/app/**`）

**技术栈**（`web/package.json`）：Next.js **15.1.6**（App Router）、React 19、TypeScript 5.7、Tailwind 3.4、**SWR 2.3**（数据获取）、**ECharts 5.5**（+ echarts-for-react）、**lightweight-charts 4.2**（K 线）、**@xyflow/react 12**（因子编辑画布）、**CodeMirror 6**（策略编辑器）、react-markdown；测试 vitest 3 + @testing-library/react + jsdom + **Playwright**（`npm run e2e`）。

**页面**（每个 `page.tsx` 为一个路由；**内容均为组件级判读，未逐页读完**）：

| 路由 | 文件 | 内容 |
|---|---|---|
| `/` | `page.tsx` | 首页 |
| `/dashboard` | `dashboard/page.tsx` | 大盘看板（宽度六卡 + 90 日宽度图 + 批量对比区；`docs/ARCHITECTURE.md:589-595`） |
| `/data` | `data/page.tsx` + 16 个组件 | 数据覆盖度、**QlibExportCard**、质量面板、ChecksPanel、GapsPanel、IssuesPanel、CrosscheckPanel、DataVersionCard、FreshnessHealthCard、SyncJobsPanel、TasksPanel、CheckpointPanel、PurgeModal、BackfillModal、SourceConfigPanel、DataDictionaryModal |
| `/factors` | `factors/page.tsx` + `FactorLibrary.tsx` / `RegisterForm.tsx` / `SaveAsFactor.tsx` / **`QlibWorkflowPanel.tsx`** / `[name]/page.tsx` / `mine/page.tsx` / `reports/page.tsx` / `editor/**`（BlockPalette / Canvas / Inspector / OpenFactorDialog / useFactorEditor） | 因子库、注册、详情、挖掘、报告、**块式画布编辑器**、**qlib 工作流面板** |
| `/backtests` | `backtests/page.tsx` + **`QlibRunsSection.tsx`** + `[runId]/page.tsx` + `workspace/**`（EditorPane / HistoryPanel / QuickRunPanel / ResultPane / RunBar / StrategyPane / ValidationPanel / state.ts） | 回测列表/详情/对比、策略工作台、**qlib 运行结果区** |
| `/tasks` | `tasks/page.tsx` + `TaskTable.tsx` + `types.ts` + `panels/{DataPanel,SyncPanel,BacktestPanel,FactorPanel,`**`QlibTaskPanel`**`}.tsx` | 任务中心（按 kind 分面板） |
| `/monitor` | `monitor/page.tsx` | 监控（延迟/队列/进程/错误日志） |
| `/paper` | `paper/page.tsx` | 模拟盘（回放/持仓/对拍） |
| `/sync` | `sync/page.tsx` + `RetryBadge.tsx` + `retry.ts` | 定时同步作业（⚠️ 调不存在的 `/sync/freshness`） |
| `/news` | `news/page.tsx` + CollectBar / IndustryPanel / NewsFeedList / SourceBadge / SymbolSearch / lib.ts / types.ts / useNewsFeed.ts | 行业资讯 |
| `/sectors` | `sectors/page.tsx` | 板块热力 |
| `/security/[symbol]` | `security/[symbol]/page.tsx` | 个股详情（实时报价头 + K 线 + 资金流 + 指标卡） |
| `/etf` | `etf/page.tsx` | ETF 元数据/相关性 |
| `/fundamental` | `fundamental/page.tsx` | 基本面评分/百分位/对账 |
| `/watchlist` | `watchlist/page.tsx` | 自选股（防抖搜索 + 行情列表） |
| `/settings` | `settings/page.tsx` | 设置（providers 顺序等） |
| `/ask` | `ask/page.tsx` | 「问 AI」（内置 Claude Code / codex 无头 CLI 会话，SSE 流式） |
| `/strategies/editor` | `strategies/editor/page.tsx` | 策略编辑器（CodeMirror + JQ 方言校验） |

**客户端 lib**（`web/src/lib/`）：`api.ts`（`get`/`post`/`postData`/`putData`/`fetcher`/`ApiError` 透出 detail）、`qlib.ts`（§12.8）、`backtestDetail.ts`、`coverage.ts`、`format.ts`、`chart.ts`、`streaming.ts`、`settings-api.ts`、`agent-api.ts`、`ask-api.ts`、`ask-stream.ts`、`factor-canvas/{catalog,compile,decompile,snippet,state,types}.ts`。

**测试**：大量 `__tests__/` 与 `*.test.ts(x)`（vitest），`web/src/lib/__tests__/qlib.test.ts` 存在。

---

## 11. CLI（`src/lquant/cli/commands/**`）

入口 `lquant.cli.main:cli`（`cli/main.py:14-30`），注册 9 个 group：`data factor agent backtest strategy paper worker sync qlib`。

| group | 子命令 | 关键 flag |
|---|---|---|
| `data` | `reference` / `sync` / `etf` / `minute` / `financial` / `basic` / `index-cons` / `status` / `demo` / `check` / `crosscheck` / `fields` | `--skip-details --detail-limit`；`--full --start --end`；`--symbols --enrich`；`--symbols --start --end --freq{5,15,30,60min}`；`--symbols --all --start --end --provider`；`--start --end --no-merge`；`--indexes`；`--start --end`；`--peers --start --end --limit`；`--start` |
| `factor` | `add` / `check` / `eval` / `submit` / `run` / `mine` / `series` / `corr` / `audit` / `robust` / `report`（+ `list` 等） | `eval`: `--start --neutral/--raw --n-groups --agent`；`mine`: `--agent --generator{gp,random,proposals} --n --proposals --start`；`robust`: `--top-months --deltas`；`report`: `--out --start --n-groups` |
| `qlib` | `export` / `check` / `workflow` | 见 §12.2 |
| `agent` | `list` / `show <name>` / `test <name>` / `run <name>` / `freeze <name>` | `run`: `--generator{gp,random,proposals} --n --proposals --start`；`freeze`: `--enable/--disable` |
| `backtest` | `run` | `--factor --spec --start --end --n-groups` |
| `strategy` | `check <path>` | `--dialect`（默认 joinquant） |
| `paper` | `create` / `order` / `tick` / `cancel` / `close` / `status` / `nav` | `create`: `--cash --strategy --universe`；`order`: `--symbol --side --qty --price`；`nav`: `--source{intraday,official}` |
| `worker` | （单命令） | `--general N --backtest N` |
| `sync` | `tick` / `status` / `run <sync_id>` / `seed` | — |

---

## 12. 已存在的 qlib 集成（**重点基线**）

> 这一节是差距分析的直接基线，逐文件精确描述。

### 12.1 `src/lquant/qlib_io/`（570 行）

**`__init__.py`（16 行）** —— 定位与隔离理由（`qlib_io/__init__.py:1-16`）：

- `export.py`：湖 → qlib 二进制，**不依赖 pyqlib**，纯 polars/pyarrow，**在主 venv 运行**；
- `runner.py`：独立脚本，在 **qlib 专用 venv** 内跑 qrun 式工作流，**不 import lquant**；
- 理由："qlib 运行时依赖树较重，本仓库刻意不把它装进主 venv（与 btval venv 同一隔离思路）"；安装方式 `python -m venv .venv-qlib && .venv-qlib/bin/pip install pyqlib lightgbm`。

**`export.py`（254 行）** —— 核心导出器。

- **产出结构**（`export.py:3-8`）：`<out>/calendars/day.txt`（每行一个 `YYYY-MM-DD`）、`<out>/instruments/all.txt`（`SYM\tstart\tend`）、`<out>/features/<SYM>/<field>.day.bin`、`<out>/qlib_export_meta.json`（manifest）。
- **bin 格式**（`export.py:10-12`）：float32 小端一维数组；**首元素是该标的首个有效日在日历中的下标**；其余按日历顺序对齐，**中间缺失日为 NaN**（不是压缩序列）。与 qlib `scripts/dump_bin.py` 一致。
- **价格口径**（`export.py:14-19`，`docs/qlib.md:44-53`）：湖内 OHLC 是**不复权价**；导出时按 `adj_factor` 归一化到"最新一天 = 不复权"（即**前复权到最新**），`open/high/low/close/vwap` 均为复权后序列；**`$factor = adj_factor / adj_factor_latest`**（不复权价 = 复权价 / factor）；`$vwap = amount/volume × f`，`volume<=0`（停牌）置 NaN；volume/amount 及可选字段为原始值。
- **字段**（`export.py:33-40`）：`DEFAULT_FIELDS = (open, high, low, close, volume, amount, vwap, factor)`；`OPTIONAL_FIELDS = (turnover_rate, total_mv, float_mv, pe_ttm, pb_mrq, ps_ttm, pct_chg)`；`ALL_FIELDS = 两者之和`。
- **symbol 映射**（`export.py:45-59`）：`to_qlib_symbol("600000.SH")→"SH600000"`（qlib 惯例，也用作 features 目录名）；`from_qlib_symbol` 反向；只接受 `.SH/.SZ/.BJ`。
- **`_adjusted()`**（`export.py:62-89`）：按标的排序 → `adj_factor` 为 null 的行沿用该标的**最近一个有效因子**（倒排 forward_fill 再 reverse，即 bfill 语义）→ 全程无因子的标的 factor=1 → 每标的 `_f_last` → `f = adj_factor/_f_last` → OHLC × f → vwap。
- **`export(out_dir, start, end, symbols, sec_types, fields, top)`**（`export.py:92-208`）：
  - `sec_types` **默认仅 `["stock"]`**（"湖里混入 ETF 会污染股票池"，`export.py:107`）；
  - `top=N` 时按**导出窗口末日 float_mv**（缺失用 total_mv 兜底）排序，额外产出 `instruments/top{N}.txt`（流动性代理池，`export.py:109-110`、`:185-192`）；
  - 逐 symbol `partition_by` 生成 bin，`start_i = gidx.min()`，`span` 覆盖到 `gidx.max()`（`export.py:155-179`）；
  - manifest 含 `out_dir/start/end/calendar_days/instruments/fields/sec_types/bins/price_basis`（`export.py:194-207`）；
  - 空湖抛 `ValueError("湖内无可导出的日线数据...")`（`export.py:133-134`）。
- **`check(out_dir)`**（`export.py:211-248`）：结构自检 —— 日历有序无重复、`instruments/all.txt` 非空、instruments 与 features 目录**双向一致**、**抽样回读一个 bin** 校验起始下标；返回 `{days, instruments, features_dirs, sample, meta, problems}`。
- **`_read_bin(path)`**（`export.py:251-254`）：返回 `(日历起始下标, 值数组)`，测试与自检用。
- **测试**：`tests/unit/test_qlib_export.py`（384 行，19 个用例）覆盖 symbol 往返/拒绝、导出基本、**复权 close + factor 数值断言**、内部空洞是 NaN、vwap 停牌 NaN、可选字段 + topN、未知字段拒绝、空湖抛错、sec_type 过滤、check 检出损坏、CLI export/check、runner mock 主链路、runner 缺 provider、CLI 子进程/进程内/错误路径、`LQ_QLIB_PYTHON`。

**`runner.py`（113 行）** —— qlib 专用 venv 内的独立脚本，**不 import lquant**。

- 用法：`<qlib-venv-python> runner.py --provider <dir> --config <yaml> --exp-name <n> --out <metrics.json>`（`runner.py:4-10`）。
- config 采用 **qlib 官方 qrun 的 workflow 配置格式**（`qlib_init` / `market` / `data_handler_config` / `dataset`(DatasetH) / `model`(任意 qlib Model) / 可选 `port_analysis`）（`runner.py:12-26`）。
- **执行内容 = qrun 主链路**（`runner.py:24-26`）：`model.fit(dataset)` → `SignalRecord`（预测）→ `SigAnaRecord`（IC/ICIR/RankIC）→ `PortAnaRecord`（组合回测，**可选**，config 含 `port_analysis` 才跑）；结束后把 recorder metrics 写 JSON。
- **两个环境 hack**（都有墓志铭）：
  1. `os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")`（`runner.py:35-37`）—— 新版 mlflow 把 filesystem tracking 后端改为显式 opt-in，否则 qlib 的 `R.start()`（默认 `file:./mlruns`）直接抛 `MlflowException`；
  2. 显式把 `C.exp_manager.kwargs.uri` 规范为 `file://<abs>/mlruns`（`runner.py:75-81`）—— qlib 默认 `"file:" + 绝对路径`（单斜杠），新版 mlflow 会当相对路径解析出 `./Users/...` 游离目录。
- `--market` 覆盖：同时改 `cfg["market"]` 与 `cfg["dataset"]["kwargs"]["handler"]["kwargs"]["instruments"]`（`runner.py:70-72`）。
- 记录：`R.log_params(model=..., handler=..., market=...)`、`R.save_objects(params.pkl=model)`（`runner.py:86-93`）。
- `provider_uri` 缺失 → `SystemExit("provider_uri 未指定...")`（`runner.py:67-68`）。

**`interpreter.py`（25 行）** —— 解释器探测（CLI 与 server 共用）。`find_qlib_python(python=None)` 返回值语义（`interpreter.py:8-25`）：

1. 显式 `python` 参数 → 直接返回；
2. 环境变量 `LQ_QLIB_PYTHON`（且路径存在）→ 返回；
3. **当前解释器能 `import qlib`** → 返回 `None`（=进程内跑）；
4. `./.venv-qlib/bin/python` 或 `./.venv-qlib/Scripts/python.exe` 存在 → 返回该路径（子进程跑）；
5. 都不可用 → 返回 `""`（=找不到，调用方报错并给安装指引）。

**`store.py`（115 行）** —— qlib 运行元数据存储，**sqlite，独立于 DuckDB 主库**，模式参照 `paper/store.py` 的自管 DDL（`store.py:1`）。

- 表 `qlib_run`（`store.py:14-28`）：`id, config, market, exp_name, status, metrics, config_snapshot, log_path, created_at, finished_at, error`；status ∈ `queued/running/finished/failed/canceled`。
- `db_path()`：`LQ_QLIB_DB` 环境变量优先，否则 `<parquet_dir 的父>/qlib/qlib_runs.db`（`store.py:31-40`）。
- API：`create_run`（uuid4 hex[:12]，初始 `queued`）/ `update_run`（**终态自动写 `finished_at`**，`store.py:87-89`；不存在则抛 `ValueError`）/ `get_run` / `list_runs(limit=50, status=None)`（按 `created_at DESC`）。`_LOCK = threading.Lock()` 保护。
- **测试**：`tests/unit/test_qlib_store.py`（50 行，6 个用例）：create/get、生命周期、failed 带 error、update 不存在抛错、list 过滤与排序、get 缺失返回 None。

**`smoke_model.py`（47 行）** —— qlib 冒烟模型 `SmokeRidge`（sklearn Ridge，**不依赖 lightgbm/libomp**）。

- 动机（`smoke_model.py:3-6`）：`qlib.contrib.model` 包的 `__init__` 会连带 import lightgbm（macOS 缺 libomp 直接炸），导致 contrib 里**所有**模型都不可用；`init_instance_by_config` 的 `module_path` 支持**文件路径**，所以用本文件绕开 contrib 包。
- 实现最小 qlib `Model`（`qlib.model.base.Model`）：`fit(dataset)` 取 `dataset.prepare("train", col_set=["feature","label"], data_key="learn")` → `dropna` → Ridge；`predict(dataset, segment="test")` 取 feature → **Ridge 不接受 NaN**（新股缺历史），这些行预测置 NaN 交给下游 IC/回测跳过（`smoke_model.py:38-47`）。

### 12.2 `src/lquant/cli/commands/qlib.py`（118 行）

`qlib` click group，3 个子命令：

1. **`lq qlib export`**（`cli/commands/qlib.py:25-45`）：`--out`（默认 `data/qlib`）、`--start`、`--end`、`--symbol`（multiple）、`--sec-type`（multiple，默认 `stock`）、`--field`（multiple）、`--top`（int）。调用 `qlib_io.export.export(...)`，echo manifest JSON。
2. **`lq qlib check`**（`:48-55`）：`--dir`（默认 `data/qlib`）；echo 自检 JSON，**`problems` 非空则 `sys.exit(1)`**。
3. **`lq qlib workflow`**（`:65-118`）：`--config`（默认 `config/qlib/workflow_alpha158_lgbm.yaml`）、`--provider`（默认 `data/qlib`）、`--python`（qlib venv python）、`--market`（覆盖股票池，如 `all`/`top300`）、`--exp-name`（默认 `lquant_qlib`）、`--out`（默认 `data/qlib/last_metrics.json`）。
   - 前置校验：config 存在、provider 目录存在（否则 `SystemExit` 提示"先跑 lq qlib export"）；
   - **探测顺序**（模块 docstring `:3-6` + `:84-108`）：① 当前解释器能 import qlib → **进程内**调 `runner.main(argv)`；② `LQ_QLIB_PYTHON` / `--python` 指向的 venv → **子进程** `subprocess.run([py, runner.py, ...])`；③ 都不可用 → `SystemExit` 带安装指引（`python -m venv .venv-qlib` + `pip install pyqlib lightgbm`）；
   - 结束后读 metrics JSON，**高亮打印** `IC / ICIR / Rank IC / Rank ICIR / excess_return_without_cost / excess_return_with_cost`（`:114-117`）。
   - `_find_qlib_python` 是 `qlib_io.interpreter.find_qlib_python` 的转发（`:58-62`，spec Task 1 的重构）。

### 12.3 `src/lquant/server/api/qlib.py`（291 行）—— 9 个端点

`router = APIRouter(prefix="/qlib", tags=["qlib"])`（`server/api/qlib.py:17`）；`_QLIB_DATA_DIR = "data/qlib"`（`:19`）、`_CONFIG_DIR = Path("config/qlib")`（`:20`）、`_NAME_RE = ^[A-Za-z0-9_-]+$`（`:21`，防路径穿越）、`QLIB_RUNNER` 绝对路径（`:23`）、`LOG_DIR = data/qlib/runs`（`:152`）。

| 端点 | 语义 |
|---|---|
| `GET /qlib/status` | `{dir, exists}`；存在时附 `manifest`（读 `qlib_export_meta.json`）与 `calendar{start,end,days}`（读 `calendars/day.txt`）（`:66-79`） |
| `POST /qlib/export` **(202)** | body `{start?, end?, symbols?, sec_types?, fields?, top?}`；pydantic 校验 `fields ∈ ALL_FIELDS`、`1<=top<=5000`；`enqueue("lquant-qlib", _run_export_job, params, job_id=uuid4[:12], name="Qlib 导出")`；返回 `{job_id}`（`:82-87`）。任务体 `_run_export_job` 做 **导出 → 自检** 两步并上报进度（`:51-63`） |
| `GET /qlib/check` | 同步自检；目录不存在 → 404（`:90-96`） |
| `GET /qlib/configs` | 列出 `config/qlib/*.yaml`：`{name, path, mtime}`（`:103-109`） |
| `GET /qlib/configs/{name}` | 返回 yaml 原文；名称白名单校验 422；不存在 404（`:112-119`） |
| `PUT /qlib/configs/{name}` | 保存 yaml：**先 `yaml.safe_load` 语法校验 + 必须为 dict 且含 `qlib_init` 键**，否则 422；名称白名单（`:122-137`） |
| `POST /qlib/workflow` **(202)** | body `{config, market?, exp_name="lquant_qlib"}`；校验 config 名、config 文件存在（404）、`data/qlib` 存在（422）、**`find_qlib_python()==""` 预探测 → 422 + 安装指引**（避免注定失败的入队）；`store.create_run(...)` 快照 config 原文 → `enqueue("lquant-qlib", _run_workflow_job, ..., job_id=f"qlibwf-{run_id}", name="Qlib 工作流")`；返回 `{run_id}`（`:216-234`） |
| `GET /qlib/runs` | `limit`(1..500, 默认 50) + `status` 过滤（`:237-242`） |
| `GET /qlib/runs/{run_id}` | 运行详情 + **`log_tail`**（读 log 文件末 5000 字符）；不存在 404（`:245-256`） |
| `POST /qlib/runs/{run_id}/cancel` | 运行不存在 404；状态不在 `queued/running` → 409；`request_cancel(f"qlibwf-{run_id}")` 失败 → 409；成功则 `store.update_run(status="canceled")`（`:259-271`） |
| `POST /qlib/runs/compare` | body `{ids: [a,b]}`（**恰好 2 个**，`Field(min_length=2, max_length=2)`）；取两次运行的 metrics **键并集**，输出 `rows: [{metric, <id1>: v, <id2>: v}]`（`:274-291`） |

**工作流任务体** `_run_workflow_job(run_id, config, market, exp_name, progress, cancel_check)`（`:173-213`）：
`store.update_run(running)` → `find_qlib_python(None)`，`==""` 则 `store.update_run(failed, error=_VENV_MISSING_MSG)` 并返回（**venv 缺失落 failed 而非崩**）→ 拼 argv `[runner.py, --provider, --config, --exp-name, --out, (--market)]` → **子进程** `subprocess.run(cmd, stdout=log_file, stderr=STDOUT)`（**全量 stderr 落 log 文件**）→ `OSError` 落 failed → **`cancel_check()` 为真则落 `canceled`**（协作式取消，`:202-204`）→ `rc != 0` 时把 log **末 2000 字符**存进 `error` 并落 failed → 成功读 metrics JSON 落 `finished`。
`_VENV_MISSING_MSG`（`:165-170`）含 `python -m venv .venv-qlib` + `pip install pyqlib lightgbm` + `LQ_QLIB_PYTHON` 指引。

**测试**：`tests/unit/test_server_qlib_api.py`（175 行，13 个用例）：status 缺目录/有 manifest、export 入队（断言 queue == `lquant-qlib`）、非法字段 422、configs 列表、config get/put、非法 yaml 422、**路径穿越 422**、workflow 无数据 422、workflow 入队 + run 生命周期（venv 缺失 422 → venv 可用 202 → `queued`）、`_run_workflow_job` 成功落 metrics、venv 缺失落 failed 且 error 含 "pyqlib"、runs 列表与 compare 键并集、cancel。

### 12.4 任务中心接入（kind=qlib）

- `server/jobs.py:19`：`QUEUES` 含 **`"lquant-qlib"`**（与 backtest/mining 隔离的专用队列）。
- `server/api/task_center.py`：`KINDS = ("data","sync","backtest","factor","qlib")`，`_QUEUE_OF_KIND["qlib"]="lquant-qlib"`，`_items` 分支调 `_job_items("lquant-qlib","qlib",limit)`（spec Task 6）。
- `server/main.py`：`qlib` 在 include 列表内（`main.py:22,65`）。
- **测试**：`tests/unit/test_task_center_qlib.py`（25 行，3 个用例）：`"qlib" in KINDS`、`_job_items` 归一（queue=`lquant-qlib`、state=finished）、未知 kind 抛 HTTPException。

### 12.5 因子层与 qlib 的两个连接点

**（a）`factors/qlib_alpha.py`（316 行）** —— Alpha158 的**纯 Polars/NumPy 原生重实现**，§3.8 已详述。要点复述：158 个（kbar 9 + price 4 + rolling 29 族 × 5 窗口）；入口 `list_builtin/resolve_name/has_factor/compute/compute_all`；与 qlib 的差异（`$vwap` 代理、Slope/Rsquare/Resi 闭式解、IdxMax 0=最老一根）都写在注释；warmup NaN 统一转 null；接线到 `panel.compute_factor_columns`、`/api/factors/builtin`、`/api/factors/seed-builtin`。

**（b）`factors/sources/qlib_source.py`（68 行）** —— **qlib 公式 → lquant DSL 的翻译器**（adapter 只翻译不执行）。

- `_OP_MAP`（`qlib_source.py:11-16`）：qlib 算子 → lquant DSL 算子：`Mean→Ts_Mean`、`Sum→Ts_Sum`、`Std→Ts_Std`、`Corr→Ts_Corr`、**`Ref→Ts_Delay`**、`Max→Ts_Max`、`Min→Ts_Min`、**`Rank→Ts_Rank`**、`Quantile→Ts_Quantile`、`IdxMax→Ts_ArgMax`、`IdxMin→Ts_ArgMin`、`Slope→Ts_Slope`、`Rsquare→Ts_Rsquare`、`Resi→Ts_Resi`、`WMA→Ts_WMA`。
- `_PASSTHROUGH`（`:18`）：`Greater/Less/Abs/Log/Sign/Power/SignedPower/If` 同名透传。
- 特例（`_translate_call`，`:24-36`）：`Max(x,y)`（二元）→ `Greater`；`Min(x,y)`（二元）→ `Less`；`Quantile(x,d,q)`（三元）→ `Ts_Quantile`；不可翻译抛 `ValueError("不可翻译的 qlib 算子: {name}")`。
- `_preprocess`（`:51-55`）：剥离 `#` 注释；**`$vwap` → `($amount/$volume)` 代理替换**（与 qlib 的 `$vwap = amount/volume` 口径一致）。
- `translate(formula)`（`:58-62`）：parse → `_rename_ops` → `unparse` → **再 parse 一次做 verify**。
- `factor_id(formula)`（`:65-68`）：canonical 形式的 sha1 前 16 位。
- **测试**：`tests/unit/test_qlib_alpha.py` 覆盖 alpha；qlib_source 的翻译测试未在本次 `test_qlib_*` 列表中出现（**【不确定】** 可能在 `test_factor_sources.py` 或类似文件）。

### 12.6 `config/qlib/*.yaml`（2 个工作流）

**`workflow_alpha158_lgbm.yaml`（85 行）** —— 主工作流：

- `qlib_init: {provider_uri: "data/qlib", region: cn}`（`:9-11`）；
- `market: all`；**`benchmark: SH600000`**（`:13-16`）—— 注释说明：湖内暂无指数日线，qlib 0.9.7 里 `benchmark: null` 会被默认成 SH000300 然后报不存在，所以用 SH600000 机械代理，**"超额收益数字仅作参考"**；
- `data_handler_config`（`:18-33`）：`start_time 2024-01-01`、`end_time 2026-09-18`、`fit_start 2024-01-01`、`fit_end 2025-12-31`；`infer_processors: RobustZScoreNorm(clip_outlier)`；`learn_processors: DropnaLabel + CSRankNorm`；
- `label: ["Ref($close,-2)/Ref($close,-1)-1"]`（`:35`）；
- `dataset: DatasetH` + `handler: Alpha158`（`module_path: qlib.contrib.data.handler`）；segments `train 2024-01-01~2025-06-30` / `valid 2025-07-01~2025-12-31` / `test 2026-01-01~2026-09-18`（`:37-50`）；
- `model: LGBModel`（`module_path: qlib.contrib.model.gbdt`），超参：`loss mse, colsample 0.8879, lr 0.0421, subsample 0.8789, lambda_l1 205.7, lambda_l2 580.98, max_depth 8, num_leaves 210, num_threads 8`（`:52-64`）；
- `port_analysis`（`:66-85`）：`TopkDropoutStrategy(topk=30, n_drop=3)`；backtest `2026-01-01 ~ 2026-09-17`（注释：end 收到日历倒数第二个交易日，qlib 末步需要下一交易日定价）、`account 1e8`、`benchmark SH600000`、`exchange_kwargs: deal_price close, open_cost 0.0005, close_cost 0.0015, min_cost 5, trade_unit 100`。

**`workflow_alpha158_smoke.yaml`（78 行）** —— 除 `model: SmokeRidge`（`module_path: src/lquant/qlib_io/smoke_model.py`，`alpha: 1.0`，`:53-57`）外与上面**逐字段相同**；用途：轻量冒烟，不依赖 libomp/OpenMP（`:1`）。

**关键口径**（两文件注释 `:5-8`）：湖内无指数数据 → benchmark 只能代理 → **回测只看绝对收益**；价格为"前复权到最新"序列，`deal_price: close` 直接可用。

### 12.7 文档：scope / 决策 / 非目标

**`docs/qlib.md`（65 行）**：

- **架构**（`:6-12`）：导出器（主 venv，纯 polars/pyarrow，不依赖 pyqlib）+ runner（qlib 专用 venv，独立脚本，不 import lquant）+ CLI `lq qlib export/check/workflow`。
- **隔离理由**（`:14-21`）："pyqlib 依赖树重，刻意不进主 venv（与 btval 同一隔离思路）"；安装 `python -m venv .venv-qlib; .venv-qlib/bin/pip install pyqlib lightgbm`；**macOS 上 lightgbm 需要 OpenMP 运行时：`brew install libomp`**。
- **解释器探测顺序**（`:23-24`）：当前解释器可 import qlib → 进程内；否则 `LQ_QLIB_PYTHON` / `--python` → 子进程。
- **用法**（`:26-42`）：export（默认 stock、默认字段 OHLCV+amount+vwap+factor；`--top 300` 额外产流动性池）→ check（日历有序、清单与 features 一致、bin 抽样回读）→ workflow（Alpha158 + LGBM 训练 + IC 分析 + TopkDropout 回测）；`--market` 覆盖 `all`/`top300`；metrics JSON 含 IC/ICIR/Rank IC/Rank ICIR 及回测年化、最大回撤。
- **价格口径**（`:44-53`）：见 §12.1。
- **与仓库内原生实现的关系**（`:55-59`）：`factors/qlib_alpha.py` + `research/ml/` 是**原生链路**；本模块是**真 qlib 链路**；两者互为交叉验证（qlib 源为 dump_bin 同构格式，公式口径一致）。
- **已知边界**（`:61-65`）：① 湖内无指数日线 → `benchmark: null` → **回测只看绝对收益，不算超额收益**（后续接入指数数据后可打开）；② **导出不覆盖 DuckDB 参考表，纯读 parquet 湖**。

**`docs/superpowers/specs/2026-09-20-qlib-frontend-design.md`（133 行）** —— 前端接入设计（状态"已确认"）：

- **背景**（`:6-15`）：PR #75 落地了 qlib 接入（导出器 + runner + CLI），但"目前只能命令行操作"；目标：① 导出（异步任务 + 状态展示）② 跑工作流（接入任务中心）③ 历史运行查看与对比 ④ workflow yaml 配置管理。
- **总体决策**（`:17-24`，**已与用户确认**）：**不建独立 `/qlib` 页面**，融入现有页面（`/data` 导出卡片、`/factors` 工作流入口 + 配置管理、`/backtests` 结果展示 + 对比）；**新专用队列 `lquant-qlib`**（与 backtest/mining 隔离）；任务中心新增 **`kind=qlib`**。
- **后端**（`:26-81`）：`store.py`（`qlib_run` 表 + `create_run/update_run/get_run/list_runs`）、`api/qlib.py`（9 端点表，`:51-62`）、任务中心接入（`QUEUES`/`KINDS`/`_job_items`/取消走通用 `request_cancel`）、**CLI 保持不变**（探测逻辑抽公共函数属内部重构，行为不变）。
- **前端**（`:83-110`）：三页区块 + 任务中心 kind。
- **错误处理**（`:112-117`）：所有写操作 pydantic/白名单校验 fail fast 422；子进程失败 stderr 全量落 log、run.error 存尾部摘要；未导出就跑 workflow → 422；**API 统一复用现有 envelope**（注：实际 `/qlib` 用的是**裸路由**，未挂 `EnvRoute` —— 见 §9.2；spec 与实现的这一处不一致）。
- **测试**（`:119-127`）：覆盖率 ≥95% 增量门禁；后端单测（打桩 export/runner 子进程）+ 前端 vitest。
- **非目标（YAGNI）**（`:129-133`，**明确三条**）：
  1. **不做 workflow yaml 可视化结构编辑器**（只做原文编辑 + 校验）；
  2. **不做 mlflow 指标全量同步**（只落 runner 产出的 metrics JSON）；
  3. **不做 qlib 数据增量导出**（每次全量/按区间覆盖导出）。

**`docs/superpowers/plans/2026-09-20-qlib-frontend.md`（1,755 行）** —— 实施计划，11 个 Task（TDD 式，含代码骨架）：T1 解释器探测抽公共函数 → T2 `qlib_run` 存储 → T3 status/export 端点 → T4 配置管理端点 → T5 workflow + runs/cancel/compare → T6 任务中心 kind=qlib + 路由注册 → T7 前端 API 封装 + 类型 → T8 `/data` 导出卡片 → T9 `/factors` 工作流面板 → T10 `/backtests` 结果展示 + 对比 → T11 任务中心前端 kind。Global Constraints（`:14-21`）：覆盖率增量 ≥95%、**pyqlib 不进主 venv**（子进程，探测顺序 = 显式 python → `LQ_QLIB_PYTHON` → 进程内 import → `.venv-qlib`）、API 走既有 envelope 约定、写操作 422 fail-fast、**不破坏现有 CLI 行为**、新文件过 ruff、前端 `npx vitest run`、conventional commit 不加 attribution。

### 12.8 Web 端 qlib 组件

**`web/src/lib/qlib.ts`（89 行）** —— 类型 + 10 个函数：
`EXPORT_FIELDS`（15 个字段，与后端 `ALL_FIELDS` 一致，`qlib.ts:4-7`）；类型 `QlibStatus` / `QlibConfigMeta` / `QlibRunStatus` / `QlibRun` / `CompareRow` / `ExportIn`；函数 `getQlibStatus` / `postQlibExport` / `listQlibConfigs` / `getQlibConfig` / `putQlibConfig` / `postQlibWorkflow` / `listQlibRuns(limit=50)` / `getQlibRun` / `cancelQlibRun` / `compareQlibRuns`。**测试** `web/src/lib/__tests__/qlib.test.ts`。

**`web/src/app/data/QlibExportCard.tsx`（114 行）**：Panel「Qlib 数据导出 / 日线湖 → qlib 二进制」；状态条（未导出提示 / 日历 start~end+天数 / 标的数）；表单 start/end/top + 15 个字段多选（中文标签 `FIELD_LABELS`）；提交 `postQlibExport` → 提示"进度见任务中心"；刷新按钮。**测试** `data/__tests__/QlibExportCard.test.tsx`。

**`web/src/app/factors/QlibWorkflowPanel.tsx`（148 行）**：Panel「Qlib 工作流 / 训练 → IC 分析 → 组合回测」；配置下拉（`listQlibConfigs`）+ **内联 textarea 编辑**（`getQlibConfig`/`putQlibConfig`，只做原文编辑 —— 对应 spec 非目标①）+ market/exp_name 输入 + 保存/运行按钮；**历史运行表**（ID/配置/状态/IC/时间）；**显式 loadError 状态**（注释：静默 catch 会让"取不到配置"看起来像"没有配置"，`:28-29`）+ 重试按钮。**测试** `factors/__tests__/QlibWorkflowPanel.test.tsx`。

**`web/src/app/backtests/QlibRunsSection.tsx`（120 行）**：Panel「Qlib 运行 / IC · Rank IC · 超额收益 · 勾选两项对比」；运行表（对比勾选框 —— 仅 `finished` 可勾、ID、配置、状态、IC、ICIR、超额(含成本)、取消按钮、error ⚠）；**勾选两项 → compare 视图**（指标并集左右对照）；`METRIC_TEXT` 映射 6 个指标中文名。**测试** `backtests/__tests__/QlibRunsSection.test.tsx`。

**`web/src/app/tasks/panels/QlibTaskPanel.tsx`（51 行）**：SWR `GET /tasks?kind=qlib`（有 running/queued 时 2s 轮询，否则 15s）；TaskTable + 取消（`POST /tasks/qlib/{id}/cancel`）；Panel「Qlib 任务 / 导出 + 工作流 · lquant-qlib 队列」。

**其他 qlib 引用**（grep）：`web/src/app/tasks/types.ts`（kind 联合类型含 qlib）、`tasks/page.tsx`（挂 QlibTaskPanel）、`tasks/__tests__/types.test.ts`、`factors/page.tsx`（挂 QlibWorkflowPanel）、`factors/FactorLibrary.tsx`、`factors/editor/OpenFactorDialog.tsx`、`factors/editor/page.tsx`、`data/page.tsx`（挂 QlibExportCard）、`backtests/page.tsx`（挂 QlibRunsSection），以及对应测试文件。

### 12.9 隔离 `.venv-qlib` 与理由

- **理由**（`docs/qlib.md:14-15`、`qlib_io/__init__.py:8-9`）：pyqlib 依赖树重（会拖进 mlflow 等），**刻意不进主 venv**，与 btval venv 同一隔离思路。
- **安装**：`python -m venv .venv-qlib` + `.venv-qlib/bin/pip install pyqlib lightgbm`；macOS 需 `brew install libomp`（lightgbm 的 OpenMP 运行时）。
- **实测环境**【实测】：`.venv-qlib` 存在；**pyqlib 0.9.7**、**lightgbm 4.7.0**、**mlflow 3.16.1**（+ mlflow-skinny / mlflow-tracing）；Python **3.12.13**；`import qlib` **成功**。
- **主 venv**【实测】：**没有 qlib**（`ModuleNotFoundError`），但**有 lightgbm 4.7.0**（所以原生 ML 链路可用 lightgbm）。
- **隔离带来的代价**：runner 内**不能 import lquant**（否则主 venv 的依赖在 qlib venv 里缺失），所以 runner 是自包含脚本、config 必须走文件；且 `.venv-qlib` 是**仓库外状态**（不进 git，靠文档指引手工创建）。

### 12.10 端到端落地证据（重要，含不确定）

**【实测】当前 checkout 的 qlib 产物状态**：

- `data/qlib/` **只有** `qlib_runs.db`（12 KB）与 `runs/`（5 个 **0 字节** `.log`，时间戳 2026-09-20 00:41–00:42）；
- **没有** `calendars/`、`instruments/`、`features/`、`qlib_export_meta.json`、`last_metrics.json` → **当前没有已落地的导出数据**；
- `data/qlib/qlib_runs.db` 的 `qlib_run` 表 **查询返回 0 行**（表存在但无记录）；
- `.claude/worktrees/qlibaug/data/qlib/` 与主仓**完全相同**（同一份 sqlite + 同样的 5 个空 log）。
- 主 DuckDB 里 **没有 `qlib_run` 表**（`Catalog Error`）—— 印证 qlib 元数据在独立 sqlite。

**判断**：qlib 链路的**代码、配置、CLI、API、前端、单测都已落地**（git log 有 `#75` 真接入、`#78` 前端接入两组提交），但**当前 checkout 里没有一次成功端到端运行的持久化证据**（导出产物不存在、run 表空、日志 0 字节）。空日志 + 空 run 表的组合，最可能是"运行在 `_run_workflow_job` 早期（子进程无输出）失败或被清理"，也可能是"开发期手工清空/迁移过"。**【不确定】**：无法从现有证据判定 qlib 工作流是否曾在**真实数据**上跑通；`test_qlib_export.py::test_runner_main_mocked` 是**打桩** qlib 的单测，不构成端到端证据。

**唯一可确证的能力**：`lq qlib export` 的**导出语义**有数值级单测锁定（复权价、factor、内部空洞 NaN、vwap 停牌 NaN、topN、sec_type 过滤），且 `check()` 能检出损坏。

### 12.11 qlib 相关测试清单

| 文件 | 行数 | 覆盖 |
|---|---|---|
| `tests/unit/test_qlib_export.py` | 384 | symbol 映射、导出语义（复权/空洞/vwap/可选字段/topN/sec_type）、check、CLI export/check、runner mock 主链路、runner 缺 provider、CLI workflow 子进程/进程内/错误、`LQ_QLIB_PYTHON` |
| `tests/unit/test_qlib_alpha.py` | 151 | Alpha158 原生实现（10 个用例） |
| `tests/unit/test_qlib_store.py` | 50 | `qlib_run` CRUD + 状态迁移 |
| `tests/unit/test_qlib_interpreter.py` | 39 | 显式 python / env / 进程内 import / 找不到 |
| `tests/unit/test_server_qlib_api.py` | 175 | `/qlib` 9 端点 + 任务体 + 取消 |
| `tests/unit/test_task_center_qlib.py` | 25 | kind=qlib 归一 |

（`__pycache__` 里另有 `test_qlib_store` / `test_server_qlib_api` / `test_task_center_qlib` 的 `.pyc`，与源码一致。）

---

## 13. lquant 相对 qlib 的第一轮差距清单（我的判断）

> 这是**第一轮、基于本文 §1–§12 的粗清单**，供后续 gap analysis 精化。每条给出"lquant 现状 → qlib 有什么"。
> 姊妹文档 `01-qlib-capability-inventory.md` 有 qlib 侧的完整能力盘点，本节不重复论证 qlib 细节。

### 13.1 数据层

| # | 差距 | lquant 现状（证据） | qlib 有 |
|---|---|---|---|
| G1 | **无 PIT 数据库 / 无统一表达式查询引擎** | 数据是 Parquet 湖 + DuckDB 小表；因子计算在 Polars 表达式里手工拼（`factors/dsl/compiler.py`）；`financial_pit` 是长表 item/value，`get_fundamentals` 走 JQ 方言 shim（`research/dialect/fundamentals.py`） | `qlib.data` 表达式引擎（`$close/Ref($close,1)` 在 query time 求值）+ 统一的 PIT 数据服务层 |
| G2 | **数据资产有硬缺口** | **`security` 仅 3 行（source='test'）**；**2025 日线整年缺失**；minute/adj_factor 湖不存在；etf_meta 10 行、index_cons 6 行（§2.7【实测】） | qlib 自带可下载的 cn 数据集（含日历/instruments/features 的完整 bin） |
| G3 | **无数据版本管理 / manifest / checksum** | HEAD 已有分区连续性检查（`store/integrity.py`），但**无 manifest、无 checksum、无 schema 版本、无自动快照/恢复**（审计 §2.4-8/9/10、§1.5） | qlib 的 bin 数据 + `dump_bin` 有确定的目录契约；`qlib` 有数据版本/rolling 机制（`1d`/`1min` 等） |
| G4 | **多源真冗余缺失** | 所有 ingest **绕过 FallbackProvider**，实为单源 + 断点重试（审计 P1-1）；空响应不切源（P1-2）；无仲裁（P3-17） | qlib 侧不强调多源；**这条是 lquant 自身欠债**，不是 qlib 优势 |
| G5 | **无公司行为事件表** | 完全没有分红/送转/拆并事件表；复权靠 `adj_factor` 启发式（审计 §2.4-1） | qlib 有 `$factor` 与除权处理；**事件表本身 qlib 也不以"表"形式提供** |
| G6 | **无 intraday / tick 级数据落地** | 分钟线湖**不存在**；`MINUTE_BAR` schema 与 ingest 代码存在但无数据（§2.7） | qlib 支持 `1min` 频率数据集 |

### 13.2 因子 / 特征

| # | 差距 | lquant 现状 | qlib 有 |
|---|---|---|---|
| G7 | **无 query-time 表达式特征引擎** | DSL 是**预计算**（`FactorEngine.compute` 物化成列，`engine.py`），并有两级缓存（`factors/cache.py`）；没有"查询时按表达式动态取特征"的服务化路径 | `qlib.data.D` 表达式引擎，`Alpha158/Alpha360` 是 handler 在 dataset 构建时按表达式算 |
| G8 | **无 Alpha360** | 只有 Alpha158 原生重实现（`factors/qlib_alpha.py`） | `Alpha360`（360 个） |
| G9 | **无 handler/processor 抽象** | 预处理是自研 `METHODS` 注册表 + `pipeline.run`（`factors/preprocess/`），**不是** qlib 的 `DataHandler` + `Processor` 链；config/qlib 里用 qlib 的 `Alpha158` handler 是**真 qlib 链路**才有的 | `DataHandler` / `Processor`（RobustZScoreNorm / CSRankNorm / DropnaLabel…）、`DatasetH`、`TSDatasetH` |
| G10 | **因子库规模小** | `factor_def` 160 行【实测】（含 seed 的 Alpha158）；自定义因子靠 YAML + DSL | qlib 生态有大量预置 handler/模型配方 |

### 13.3 ML / 研究

| # | 差距 | lquant 现状 | qlib 有 |
|---|---|---|---|
| G11 | **无实验追踪 / 无 MLflow 集成** | `ml_run` 表（DuckDB，11 字段）+ `qlib_run` sqlite；**metrics 只落 JSON**；`spec 非目标②` 明确"不做 mlflow 指标全量同步"（`specs/...qlib-frontend-design.md:132`） | qlib `R`（Recorder）+ **MLflow**（`mlruns/`），实验/参数/指标/artifact 完整追踪；`runner.py` 已用 `R.start()`，但 lquant 侧只取 metrics JSON |
| G12 | **无模型 zoo** | 只有 3 个后端：LightGBM / sklearn GBRT / Ridge（`research/ml/model.py:133-137`） | `qlib.contrib.model` 大量模型（LGBModel、XGBoost、CatBoost、DNN、GRU、LSTM、Transformer、TFT、TabNet、DoubleEnsemble、Linear…） |
| G13 | **无 meta-learning / 无 RL** | 无 | qlib 有 `qlib.contrib.meta`（MetaModel / DDG-DA）、`qlib.rl`（RL 框架） |
| G14 | **无滚动重训 / 无在线服务** | `walk_forward_splits` 存在（`dataset.py:134-156`）但**无调度化的滚动重训**；无 online serving / 模型热更新 | qlib 有 `OnlineManager` / `online serving`（`qlib.workflow.online`）、rolling 训练 |
| G15 | **无 finetune 的真实实现** | `Model.finetune` 默认退化为全量重训（`model.py:43-45`） | qlib Model 有真正的 `finetune` 语义 |
| G16 | **数据集构建无"表达式 + handler"声明式配置** | `DatasetConfig(features=[列名])`，特征是**已有列**；构建靠 Python 调用 | qlib 用 YAML 声明 handler + dataset + model，`init_instance_by_config` 实例化 |

### 13.4 回测 / 组合

| # | 差距 | lquant 现状 | qlib 有 |
|---|---|---|---|
| G17 | **无 nested / multi-level 回测决策执行框架** | 自研 `Engine` 是**扁平日频事件循环**（`backtest/engine.py:255-289`）；调仓频率只有 daily/weekly/monthly/none | qlib `NestedExecutor` / `MultiLevelExecutor`（日内/日间嵌套决策） |
| G18 | **无 intraday 回测** | 引擎只吃日线 bar；分钟线湖无数据 | qlib 支持 `1min` 频率回测 |
| G19 | **无风险模型 / 无组合优化器** | `portfolio/weighting.py` 只有 equal/score/mcap/inverse_vol/risk_parity/min_variance/HRP（**544 行的整个 portfolio 包**）；无因子风险模型、无均值方差（显式收益）、无 Black-Litterman、无跟踪误差/换手/行业约束 | qlib 有 `qlib.model.riskmodel`（StructuredCovariance / POET / ShrinkCovEstimator…）+ **`qlib.contrib.strategy.optimizer`**（EnhancedIndexing / MeanVariance / RiskParity / InvariantRiskParity…，基于 cvxpy） |
| G20 | **组合构建无统一管线** | screener/dedup/weighting 是散装函数；`skfolio` 在依赖里但**未见使用** | qlib 的 `PortfolioStrategy` → `Optimizer` → `Executor` 是完整管线 |
| G21 | **基准/超额体系弱** | 原生 `Engine.metrics` **无超额收益/IR/跟踪误差**；`factors/evaluate/excess.py` 与 `/backtests` 的 benchmark 对齐是旁路；qlib 链路因**无指数数据**只能 `benchmark: SH600000` 代理（§12.6） | qlib 回测内置 benchmark + `excess_return_with/without_cost`（`runner.py` 已在读这两个 key） |
| G22 | **无 `TopkDropoutStrategy` 之外的内置策略族** | 自研策略只有 `FactorTopNStrategy`（23 行）+ JQ 方言策略 + 基准多因子；qlib 链路用的是 qlib 的 TopkDropout（在 config 里） | qlib 内置 TopkDropout / EnhancedIndexing / WeightStrategy 等 |
| G23 | **执行模型差异（lquant 更强）** | lquant 撮合**更贴近 A 股**：涨跌停 tick 取整、逐日 ST、印花税历史区间 + 方向、T+N 按交易日、退市核销、公司行为、限价单、participation（`backtest/broker.py` + `rules/model.py`） | qlib `Exchange` 有 deal_price / open_cost / close_cost / min_cost / trade_unit / limit_threshold，但**无印花税历史区间、无逐日 ST、无 T+N per-instrument** → **这是 lquant 的领先项，不是差距** |

### 13.5 工程 / 交付

| # | 差距 | lquant 现状 | qlib 有 |
|---|---|---|---|
| G24 | **无报告/绘图库** | 自研**自包含 HTML**（`factors/evaluate/report.py`，478 行内嵌 SVG）；qlib 链路的图**没有**（只有 metrics JSON） | qlib 有 `plot_graph` / `qlib.contrib.report`（回测报告、IC 图、风险分析图、`report_graph`） |
| G25 | **qlib 链路无生产化落地** | 导出产物不存在、run 表空、日志 0 字节（§12.10【实测】）；无定时重跑 | qlib 有完整 workflow 示例与 `qrun` |
| G26 | **无 qlib 数据增量导出** | spec 非目标③ 明确"每次全量/按区间覆盖导出"（`specs/...:133`）；大湖全量导出成本高 | qlib `dump_bin` 支持增量（`--include_fields` / 已有 bin 复用） |
| G27 | **两套 venv 的运维成本** | `.venv-qlib` 是仓库外手工状态；runner 不能 import lquant；config 必须走文件（§12.9） | qlib 单体安装（但依赖树重，正是 lquant 隔离的原因） |

### 13.6 我认为**不应**列为差距的项（lquant 已有或更强）

- A 股撮合规则完备性（G23）；
- 数据层工程质量：Capability 路由、看门狗、断点续传（coverage 区间语义）、原子写 + 双层锁、质量门禁、湖完整性自检（§2.4–2.6）；
- 因子评价体系的**广度**：NW-t、IC 自相关、稳健性五件套、超额体系、评级、成本敏感性、事件研究、风格相关（§3.4）；
- 因子挖掘的**纪律**：70/15/15 + G0–G3 门禁 + 多重检验校正 + submit 即重验 + rationale 强制（§3.6）—— 这套"防自欺"机制比 qlib 的"配好 config 就能跑"更严格；
- 聚宽方言兼容 + 沙箱（`research/dialect/` + `backtest/jqapi.py`）；
- 全栈闭环：Web UI 20+ 页 + 任务中心 + 监控 + 模拟盘（§7–§10）。

---

## 附录 A：本次实测命令与结果（可复现）

```text
# 数据湖分区
$ ls data/parquet/daily/
year=2021 year=2022 year=2023 year=2024 year=2025 year=2026
$ ls -la data/parquet/daily/year=2025/
part-0.parquet.bak-demo   (9,241,880 bytes, Sep 12)
part-0.parquet.lock       (0 bytes, Sep 16)
# → 无 part-0.parquet：2025 整年缺失

# DuckDB 行数（read_only）
security 3 | trade_calendar 13527 | financial_pit 77446911
industry_classify 4897 | etf_meta 10 | index_cons 6
data_quality_issue 13635 | data_version 2398 | factor_def 160
backtest_run 11 | ml_run 0 | collect_log 133
qlib_run → Catalog Error: Table with name qlib_run does not exist

$ python -c "select symbol,name,sec_type,list_date,delist_date,source from security limit 5"
('000001.SZ','平安银行','stock',None,None,'test')
('600519.SH','贵州茅台','stock',None,None,'test')
('510300.SH','沪深300ETF','etf',None,None,'test')

# qlib 环境
$ .venv-qlib/bin/python -c "import qlib; print(qlib.__version__)"
0.9.7  (Python 3.12.13)
$ ls .venv-qlib/lib/python*/site-packages | grep -i "qlib\|lightgbm\|mlflow"
pyqlib-0.9.7.dist-info | qlib | lightgbm-4.7.0.dist-info | mlflow-3.16.1.dist-info
$ .venv/bin/python -c "import qlib"     → ModuleNotFoundError
$ .venv/bin/python -c "import lightgbm" → 4.7.0

# qlib 产物
$ ls data/qlib/
qlib_runs.db  runs/          # 无 calendars/ instruments/ features/ *_meta.json
$ ls -la data/qlib/runs/     # 5 个 0 字节 .log（2026-09-20 00:41–00:42）
$ sqlite3 data/qlib/qlib_runs.db "select count(*) from qlib_run"  → 0
```

## 附录 B：本文未逐行验证的模块（后续精化时优先补读）

- `backtest/jqapi.py`（1,351 行，聚宽 API 完整实现）、`jq_fundamentals.py`、`sandbox.py`；
- `backtest/exit/*`（退出策略 6 文件）、`attribution.py`、`benchmarks.py`、`selfcheck.py`、`sweep.py`；
- `portfolio/screener.py`、`dedup.py`、`__init__.py`（§6.2 的证据来自 ARCHITECTURE，非源码）；
- `research/strategies/baseline_multifactor.py`、`research/dialect/jq_shim.py` / `fundamentals.py`；
- `paper/engine.py` / `service.py` / `store.py` 内部（本文只到 API/职责层）；
- `monitor/*` 内部实现（本文到职责 + 表/存储层）；
- `server/api/*` 除 qlib 外的**语义**（端点清单完整，行为未逐行）；
- `market/**`、`news/**`、`fundamental/**`、`indicators/**`、`agent/**`（a2a/mcp/ask）；
- `lquant.sh`（623 行）、`scripts/**`；
- Web UI 各页面**具体渲染逻辑**（本文到文件级 + qlib 组件级）；
- `backtest/adapter.py` / `validation.py` / `strategy_store.py`。

## 附录 C：与审计文档的差异说明

`docs/DATA_ROBUSTNESS_AUDIT-2026-10-03.md` 的基线是 `33ec0f9`（worktree `datarobust`）；本仓 HEAD `076111c` 的提交信息为"fix(data): 修复取消/零行/覆盖外框导致的静默数据空洞 + 影子湖与整年缺失自检"。**已在 HEAD 修掉的审计项**（源码可证）：

- P0-2（取消/早停把未尝试标的标 done）→ `backfill_pool` 返回 `unprocessed`（`ingest/daily.py:112-115,226-228`）+ `tasks.py:341-347` 排除；
- P0-4（覆盖度外框判定）→ `Checkpoint.covers` / `covered_until`（`checkpoint.py:132-162`）；
- 手段 A1/A2 的一部分 → `store/integrity.py`（数据根自检 + 分区连续性），并接进 `run_lake_checks`（`quality/pipeline.py:93-96`）；
- 零行标 done（`minute.py` / `reference.py`）→ `reference.py:189-196` 已改，`minute.py` **【不确定】** 是否已改（未读 HEAD 版）。

**仍存在的审计项**：`security` 3 行、2025 日线缺失、ingest 绕过 fallback、门禁只在日线、fatal 不阻断、QC 误判源站、financial_pit 无主键、flock 静默降级、无 fsync、无备份/快照、`write_factor` 非原子、`/sync/freshness` 契约破坏、单任务互斥阻塞等（§2.6、§2.7、§8 已逐条标注）。
