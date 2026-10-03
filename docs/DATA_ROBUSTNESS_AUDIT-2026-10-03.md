# 数据模块完备性与鲁棒性审计

日期：2026-10-03
工作树：`.claude/worktrees/datarobust`（分支 `worktree-datarobust`，基线 `33ec0f9`）
方法：源码通读（4 路并行分层审计）+ **对真实数据湖的只读实测**（DuckDB / Parquet footer / 目录清单）
性质：只读审计，未修改任何代码与数据

---

## 0. 结论摘要

**完备性：骨架完备，但"声明的能力"与"真实的数据"之间存在系统性落差。**

- 数据层设计（声明式映射、Capability 路由、Fallback 链、质量门禁、断点续传、写入原子性）**架构完整度高于同类项目**，很多机制（flock + temp/rename 原子写、coverage 窗口断点、demo 覆写护栏、映射启动期校验）是真做了的。
- 但**落地的数据资产有硬缺口**：2025 年日线整年缺失、`security` 主表只有 3 行测试数据、2021–2023 只有约 1000 只标的、minute/adj_factor 湖根本不存在。
- 关键矛盾：**"任务报 ok" ≠ "数据在湖里"**。多条路径会把"没取到/没处理"记成"已完成"，且没有任何机制在事后发现整年缺失。

**鲁棒性：缺的不是"更多校验规则"，而是三类结构性手段。**

1. **完整性可观测**（现在的检查全是"内容是否合理"，几乎没有"该有的东西是否存在"）——没有分区连续性校验、没有湖清单/校验和、没有备份快照。
2. **写入边界收口**（质量门禁只挂在日线调用方，不挂在写接口）——除日线外所有数据集都是"无校验写入"。
3. **失败语义正确**（空响应/取消/超时/QC 失败被混淆）——空返回不切源、取消把未处理标的标 done、QC 失败被当成源站故障。

**最紧急的一件事**：2025 年日线可以从现成的备份目录里恢复（见 §1.1），这是**可立即止损的数据丢失**，优先级高于任何代码改动。

---

## 1. 现场实测：数据资产的真实状态

> 这一节全部来自对 `/Users/lyp/code/lquant/data/**` 的只读查询，是本次审计最硬的证据。

### 1.1 【P0】2025 年日线整年缺失（可恢复）

| 年份 | 行数 | 标的数 | 区间 |
|---|---:|---:|---|
| 2021 | 251,035 | 1,046 | 2021-01-04 ~ 2021-12-31 |
| 2022 | 257,389 | 1,086 | 2022-01-04 ~ 2022-12-30 |
| 2023 | 266,141 | 1,109 | 2023-01-03 ~ 2023-12-29 |
| 2024 | 1,218,187 | 5,073 | 2024-01-01 ~ 2024-12-31 |
| **2025** | **缺失** | — | — |
| 2026 | 1,221,763 | 7,247 | 2026-01-05 ~ 2026-09-30 |

`data/parquet/daily/year=2025/` 目录存在，但里面只有一个 0 字节 `.lock` 和一个 `part-0.parquet.bak-demo`（161,198 行 / 672 只 / 含 10 行 demo）。**真正的 2025 年分区文件不存在。**

**根因（可复现的历史事故）**：2026-09-18 的"演示数据覆盖真实湖"事故。备份树保留了事故时的完整湖：

```
data/parquet/daily.bak-demo-20260918-144538/year=2025/part-0.parquet
  → 1,246,474 行 / 5,173 只 / 2025-01-01~2025-12-31
  → 其中 source='demo' 仅 7,830 行（30 只标的 × 261 天）
  → 真实 baostock 行 1,238,644 / 5,143 只
```

对比：live 湖的 2024/2026 已重新同步（demo 行已清干净），**只有 2025 没有恢复**。

**恢复方案**（低风险，纯补齐）：
1. 取 `daily.bak-demo-20260918-144538/year=2025/part-0.parquet`；
2. 过滤 `source='demo'` 的 7,830 行（这 30 只标的的对应日期已被合成值覆盖，`demo_only` 计数 = 7,830，即真实行已丢）；
3. 写入 live `daily/year=2025/part-0.parquet`（走 `write_daily` 即可，护栏允许真实数据覆盖）；
4. 对那 30 只（`000001.SZ/600519.SH/510300.SH/...` 蓝筹与 ETF）重拉 2025 年日线补洞。

**为什么没人发现**：`read_daily` 用 `daily/**/*.parquet` glob（`store/parquet.py:307`），缺一个年份就是**静默少一年**；`latest_trade_date()` 返回 2026-09-30 看起来"很健康"；`scan_coverage` 默认只看近 5~30 天（`sync/manager.py:296,454`），2026-10-03 跑的扫描**永远看不到 2025**。→ 见 §3.1 分区连续性校验。

### 1.2 【P0】影子湖：一套数据根被解析成了两棵树

`data/parquet/` 下同时存在：

```
data/parquet/daily/          ← 真实湖（2021-2024, 2026）
data/parquet/parquet/daily/  ← 影子湖（2024/2025/2026，各 30 只，全 demo）
data/parquet/duckdb/lquant.duckdb  ← 影子 DuckDB
data/parquet/reports/  data/parquet/ask.db
```

**根因**：`Settings.parquet_dir` 默认 `"./data/parquet"`（`core/config.py:140`），且 `config/app.yaml:5` 是 `parquet: ${LQ_DATA_DIR:./data}/parquet`。二者都是 **CWD 相对路径**，`find_root()` 只锚定 `config/`，从不影响数据路径（`core/config.py:29-37`）。只要进程的 CWD 或 `LQ_DATA_DIR` 被设成 `./data/parquet`，就会在 `data/parquet/parquet` 再长出一棵湖 + 一个库，且**两条路径都各自"成功"**。

**次生风险**：`.claude/worktrees/datarobust` 里**根本没有 `data/` 目录**（`.gitignore` 的 `/data` 规则本意是让 worktree 建软链到主仓）。在 worktree 里跑 ingest 会新建一棵空湖并报成功。

→ 修复见 §3.5。

### 1.3 【P1】`security` 主表只有 3 行，且是测试数据

```
symbol      name      sec_type  board  list_date  delist_date  is_st  source
000001.SZ   平安银行    stock     None   NaT        NaT          False  test
600519.SH   贵州茅台    stock     None   NaT        NaT          False  test
510300.SH   沪深300ETF  etf       None   NaT        NaT          False  test
```

只有 3 行、`source='test'`、`list_date/board` 全空。而 `security` 是下列检查的**唯一事实源**：

- 涨跌停校验的板性/ST 判定（`quality/pipeline.py:152-153` → `validators.py:139,146`）
- 覆盖率 `_expected_symbols` 的上市/退市区间（`quality/coverage.py:39-53`）
- 幸存者偏差检查的 `delist_date`（`quality/universe.py`）
- 选股池 `SELECT symbol FROM security ...`（`store/catalog.py:95,106,114,124,131`）

**影响**：这些检查**不是"跳过"，而是拿着 3 行数据在跑**——结果既非正确也非报错。属于典型的静默降级。

**注意**：`tests/conftest.py` 与 `tests/unit/conftest.py` **没有任何 DuckDB/数据根隔离 fixture**（只有 .env / agent provider / 日志 / fundamentals 缓存）。隔离靠各用例自带的 `q_env`/`tmp_path` 局部 fixture。我实测跑了 3 个 reference/catalog 相关用例（`LQ_DATA_DIR` 指向临时目录），**未产生写入**，说明这些用例是干净的；因此这 3 行的来源尚未确证（可能是某次 bootstrap/演示脚本）。**结论：需要加全局数据根隔离门禁来确证并杜绝，而不是先断言是测试泄漏。**

### 1.4 【P1】"幽灵表"仍在，且湖与库两套真相

DuckDB 里 48 张表，数据类实测行数：

| 表 | 行数 | 判断 |
|---|---:|---|
| `daily_bar` / `minute_bar` / `adj_factor` / `sector` / `market_snapshot` | **0** | 遗留占位表，`ddl.py:268-272` 有注释说明，`grep "FROM daily_bar"` 无任何读者 → 死表 |
| `financial_pit` | 77,446,911 | 丰富 |
| `trade_calendar` | 13,527 | 完整 |
| `industry_classify` | 4,897 | 有 |
| `news_item` | 3,641 | 有 |
| `sector_daily` | 2,102 | 有 |
| `money_flow` / `limit_up_pool` / `dragon_tiger` | 1,299 / 1,185 / 699 | 有 |
| `index_daily` / `limit_down_pool` / `northbound_flow` / `etf_meta` / `index_cons` | 138 / 18 / 16 / 10 / 6 | **极薄** |
| `security` | **3** | 见 §1.3 |
| `daily_basic` | **表不存在** | 只存在于 Parquet（3,581,781 行 / 5,668 只 / 2024-01-02~2026-09-23，健康） |

**minute 与 adj_factor 在湖里连目录都没有**（`data/parquet/minute`、`data/parquet/adj_factor` 均不存在）。即：分钟线与复权因子**没有任何落地数据**，而 `adj_factor` 是日线复权价的基础 → 复权价实际上只能靠 `daily_bar.adj_factor` 列（若该列有值）。

### 1.5 【P2】没有任何备份 / 快照 / 完整性校验自动化

全仓 grep `snapshot|backup|备份|retention|manifest|checksum|integrity`，数据侧只命中日志轮转（`core/logging.py:99` retention=14 days）。

现场证据：`data/parquet/` 下躺着 3 个人工备份目录（`daily.bak-20260912` 62M、`daily.bak-20260913` 61M、`daily.bak-demo-20260918-144538` 219M，共 342MB）——**全部是事故后手工 `mv` 的产物**。也就是说：唯一一次数据丢失的恢复能力，来自运维人员的临时操作，而不是系统能力。

### 1.6 【P2】2021–2023 只有约 1000 只标的

2021/2022/2023 分别 1,046/1,086/1,109 只，2024/2026 是 5,073/7,247 只。早期历史只回填了约 1/5 的标的，**且没有任何检查会报告这件事**（`check_coverage` 只看"每日标的数相对窗口中位数是否骤降"，历史薄是"一直薄"，不触发）。对任何跨截面历史回测，这是直接的样本偏差。

---

## 2. 完备性评估

### 2.1 能力矩阵（声明 vs 落地）

| 数据集 | baostock | tushare | akshare | sina | tencent | mootdx | 湖内实际数据 |
|---|---|---|---|---|---|---|---|
| 日线 | Y | Y | Y | – | – | – | 有（缺 2025） |
| 分钟线 | Y(5/15/30/60) | Y(1–60)* | Y(1–60) | – | – | Y(1min) | **无** |
| 复权因子 | Y | Y | Y | Y** | – | – | **无独立湖** |
| 财务 PIT | Y | Y | – | – | – | – | 有（77M 行） |
| 指数成分 | – | Y(唯一) | – | – | – | – | 6 行 |
| ETF 元数据 | P | – | P | – | – | – | 10 行 |
| ETF 日线 | Y | Y | Y | – | – | – | 有 |
| 标的清单 | Y | Y‡ | Y | – | P¶ | – | **3 行** |
| 交易日历 | Y | Y | – | – | – | – | 完整 |
| 实时 | – | – | – | – | Y | Y | 有 |

\* tushare `stk_mins` 需 2000 积分，权限不足转 `SourceUnavailable`（`tushare.py:194-197`），但 `providers.yaml:29-30` 照旧声明。
\*\* **契约不符**：`SinaProvider.adj_factors` 返回 `hfq_close` 而非 `factor`（`sina.py:118-120`），而消费方 `adj.py:47` 选 `["symbol","trade_date","factor"]` → 若 sina 成为主源，直接 `ColumnNotFound`。
‡ tushare `securities()` 对所有标的返回 `sec_type='stock'`、`board=None`、`is_st=False`（`tushare.py:442-449`）——**这解释了 §1.3 的 board/list_date 全空**，即用了 tushare 也会得到无板性/无上市日的清单。
¶ tencent `securities()` 只返回 7 个硬编码指数（`tencent.py:138-156`）。

### 2.2 声明了但零实现的能力

`Capability` 枚举 22 项（`capability.py:11-35`），其中 `CORPORATE_ACTION`、`INDUSTRY`、`ETF_SPOT`、`ETF_IOPV`、`ETF_SHARE`、`MONEY_FLOW`、`LIMIT_UP`、`DRAGON_TIGER`、`SECTOR` **没有任何 provider 实现**；`INDEX_DAILY` 只有 baostock 声明且从不路由（`baostock.py:437`；`market/collectors/index_daily.py:53-55` 实际走 `daily_bars`）。指数成分**连 Capability 都没有**，只靠 `index_cons.py:49-52` 直接 `TushareProvider()`。

### 2.3 配置声明了但模块不存在

`config/providers.yaml` 列了 9 个源，其中 `efinance`、`hithink`、`tickdb`（`:36-81`）**模块文件不存在**，`_import_all` 的 `ImportError` 被静默吞掉（`providers/__init__.py:26-31`）。tickdb 是有意不加载（有注释），另外两个是纯死配置。**3/9 的配置条目是假的。**

### 2.4 完备性缺口清单（按"一个稳健 A 股数据栈应该有"）

**数据维度**
1. **公司行为事件表**（分红/送转/拆并）——完全没有。只有复权因子的启发式推断（`validators.py:325`、`adjustment.py:22`），无法做"因子变动 ↔ 事件"双向对账。
2. **ST 历史**——只有当前快照，历史 ST 不可知（`validators.py:104-110` 自认此局限）。
3. **停牌历史**——`is_suspended` 靠 `volume==0` 启发式推断，`SUSPENDED` 标志位（`flags.py:23`）**从未被设置过**（`flag_tradability` 是孤儿函数，`pipeline.py:125-126` 明确拒绝接线）。
4. **涨跌停/龙虎榜/资金流**——库里有表有少量数据（1,185/699/1,299 行），但无 Capability、无 provider、无质量校验。
5. **北向资金**——16 行，基本空。
6. **财务 PIT 的申报日校验**——无 `pub_date >= stat_date` 检查、无重复键检查（`financial.py:107-128`）。
7. **复权因子独立落湖**——不存在（见 §1.4）。

**元数据/治理维度**
8. **schema 版本**——没有 `schema_version` 表、没有迁移框架、没有文件级 schema 戳。实测日线分区**列集已经漂移**：2021–2023 是 25 列（`symbol` 打头），2024/2026 是 26 列（`trade_date` 打头，多一个 `year` 列）。没有任何机制记录"哪个版本的 writer 产出了这个文件"。
9. **数据字典**——有 `dictionary.py`（字段级），但无数据集级契约（谁写、何时写、期望频率、期望行数量级）。
10. **湖清单/校验和**——完全没有。无法回答"这个分区是不是被截断了"。

---

## 3. 鲁棒性缺口（按优先级，已去重）

> 标注 `[实测]` 的为我亲自验证；其余来自分层审计，均附 file:line。

### P0 —— 正在造成或掩盖数据丢失

**P0-1 [实测] 2025 日线整年缺失且不可发现** → 见 §1.1。修复 = 恢复 + 加分区连续性校验。

**P0-2 [实测] 取消/提前停止会把"从未尝试"的标的标记为 done，retry 后报 ok**
`_run_task` 用 `newly = [s for s, _ in remaining if s not in failed_set]` 然后 `cp.mark(newly)`（`tasks.py:341-342`）。取消时 `backfill_pool` 在**处理该 chunk 之前**就 `break`（`daily.py:135-138`）；提前停止同理（`daily.py:190-198`）。未处理的尾部既在 `remaining` 里、又不在 `failed_set` 里 → **被标 done**。`_finalize` 报 `interrupted`/`failed`，但后续 `retry` 只 unmark `failed_symbols`（`tasks.py:423-425`），于是 `remaining` 为空、`done=total`、`failed=[]` → **状态 ok + 永久空洞**。现有取消用例把这个 bug 当成期望行为编码了（`test_task_cancel.py:133-160`）。
修复：`backfill_pool` 返回 `attempted`/`unprocessed`；只 `mark(attempted - failed)`；补回归用例（取消于第 k 批 → retry 必须恰好重拉未处理标的，且 `done < total`）。

**P0-3 [实测] 分钟线空响应标记 done，且无窗口账本**
`minute.py:53-55`：`if len(df): write_minute(df)` 之后**无条件** `cp.mark(chunk)`——与其上方两行注释（"失败批不标记完成"）自相矛盾，也与 daily/reference 的正确做法相反（`daily.py:160-175`、`reference.py:232-237`）。源站零行返回 = 该标的**永久排除**。且分钟线用 `cp.remaining(list(symbols))` 的 done 语义（`minute.py:40`），**完全没有 coverage 窗口**——改 `start/end` 不会触发重拉。
修复：只标记实际出现在返回帧里的 symbol；加 `empty_response` 失败分类；接入 `Checkpoint.coverage`。

**P0-4 [实测] 财务覆盖度用"外框"判定，跨洞视为已覆盖**
`financial.py:39-46` 用 `cp.covered_window(sym)`（= `min(a), max(b)`，`checkpoint.py:136-141`）判断 `lo <= start_d and hi >= end_d` 就跳过。于是覆盖 `[2016-01-01,2018-12-31] ∪ [2024-01-01,2026-01-01]` 看起来等于覆盖 `[2016,2026]`，**2019–2023 的洞永不回补**——而这正是"停摆一段时间 + 两次滚动 90 天窗口"产生的形状。同型外框逻辑还在 `sync/manager.py:267,618` 的滞后检查里。
修复：改用 `cp.covers(sym, start_d, end_d)`；增量起点取与窗口重叠的那个 span，而不是 `hi`；`covered_window` 改名 `coverage_bbox` 并注明"仅供展示"。

### P1 —— 结构性正确性问题

**P1-1 [实测] 所有 ingest 主路径绕过 `FallbackProvider`，实际是"单源 + 断点重试"**
设计文档描述了多源 fallback，但 `resolve_ingest_source` 只从 `chain.providers` 里挑**一个**返回（`daily.py:68-79`），`_pull_group` 直接调它（`daily.py:253-287`）。同样绕过的还有：`minute.py:36-37`（`providers[0]`）、`adj.py:37-43`、`etf_meta.py:27-29`、`reference.py:177-179`、`index_daily.py:53-55`、`backfill.py:32-35`、`financial.py:84-94`、`daily_basic.py:35-45`。**真正走链的只有** `sync_calendar`、`sync_securities`、realtime。
后果：主源挂了就是失败，不会切辅源；`providers_order` 改动还会让 `providers[0]` 变成不具备该能力的源（`etf_meta` 直接抛未捕获的 `CapabilityMissing`）。
修复：加一个链级 `fetch(method, cap, **kw)`（能力 + 健康感知），所有 ingest 改走它；保留 fund/stock 类别分流。

**P1-2 [实测] 空响应不算失败，不触发切源**
`fallback.py:67-71` 把空帧当成功返回并 `health.ok()`。daily 路径至少把零行记成 `empty_response` 失败（`daily.py:161-165`，做得对），但**不切源**；minute 直接标 done（P0-3）。
修复：把"零行/部分标的缺失"作为软失败，先尝试下一个具备能力的源，再判定为空。

**P1-3 质量门禁只挂在日线调用方，不挂在写接口**
`gate_daily` 是唯一阻断点（`pipeline.py:50-52`），且只被 `daily._stamp` 调用（`daily.py:370`）。以下全部**无校验写入**：`daily_basic`（`daily_basic.py:197`）、`minute`（`minute.py:54`）、`financial`（`financial.py:122`）、`security`/`calendar`（`reference.py:68,110`）、`index_cons`（`index_cons.py:77`）、`etf_meta`（`etf_meta.py:33,80`）。`write_daily`/`write_daily_basic`/`write_minute` 接受任意帧。
修复：把门禁下移到 `write_*` / repo upsert 层，按数据集注册检查集——**写接口才应该是强制边界**。

**P1-4 标为 fatal 的序列级检查从不阻断**
`run_lake_checks` 只 `save_issues` 后返回，从不抛（`pipeline.py:56-133`）。因此 `CALENDAR_STRAY`/`CALENDAR_YEAR_LEN`（`validators.py:244,257`）与 `COVERAGE`（`validators.py:288`）这些写着 fatal 的规则**什么也拦不住**，只有 CLI 退出码会体现（`cli/commands/data.py:204`）。而 `run_lake_checks` **根本没有被调度**（只存在于手动 CLI 与 `POST /data/check`）。
修复：加同步后湖门禁，对刚同步窗口内的 fatal/error 抛错并让调度判 failed；把 `run_lake_checks` 纳入夜间作业。

**P1-5 QC 失败被误判为源站故障**
`_engine.py:114-116` 抛 `DataQualityError`，在 `fallback.py:72` 被当作任意异常捕获 → 给该源记健康惩罚、切到下一个源，**且不落 issue**（`fallback.py:72-80`）。真实数据 bug 被伪装成"源不可用"。
修复：`_call` 分类异常——只有传输/权限/超时类才降级切源；`DataQualityError`/`MappingError`/`CapabilityMissing` 立即上抛并落 issue。

**P1-6 数据根是 CWD 相对，可分裂成多套湖/库** → 见 §1.2。
修复：`parquet_dir`/`duckdb_path`/`cache_dir` 相对 `find_root()` 解析（或要求 `LQ_DATA_DIR` 绝对）；`_root()`/`lake_glob()` 断言解析结果在 `settings.root` 下；加启动自检，发现多于一棵湖/一个库就**响亮失败**。

**P1-7 增量窗口是自然日偏移，且缺口扫描窗口比增量窗口更窄**
`start = today_cn() - timedelta(days=days)`，`days` 默认 10（`tasks.py:52,83-87`；调度默认见 `sync/manager.py:362-364`），**从不参考湖内 `max(trade_date)`**；而 daily 作业的缺口扫描默认只看 **5 天**（`sync/manager.py:296`）。停摆超过 10 天 → 既不在重拉窗口、也不在扫描窗口 → **不可见且不修复**。
修复：起点从湖/断点推导，用交易日算术；强制 `coverage_days >= 2 × ingest_days` 或从断点最早覆盖日扫起。

**P1-8 没有全历史缺口扫描，多数数据集完全不扫**
`scan_coverage` 只扫 daily（`coverage.py:127-156`，且仅近端窗口）；`daily_basic` 的日缺口降级为 info 且**永不修复**（`:143,150-155`）；minute / financial / industry_classify / index_cons / etf_meta **无任何扫描**。
修复：按数据集加扫描器 + 全历史模式；`daily_basic` 缺口提到 error 且可修复；加周期性深度扫描（与每日 5 天检查分离）。

**P1-9 `financial_pit` 无主键 → 每次重拉追加重复行**
`ddl.py:104-108` 无 PRIMARY KEY；`_upsert` 的无主键分支把 `epoch_cols=("trade_date",)` 过滤成空集后走**裸 INSERT**（`catalog.py:48-56`）。读者侧防御性去重（`fundamental/panel.py:59-63`、`research/dialect/fundamentals.py:235-243`）**掩盖了漂移**，而 `financial.py:130` 与 `server/api/data.py:144` 报的 `count(*)` 是虚高的。同一 `pub_date` 的重述靠物理行序决出（`>=` 保留较后行）→ 不确定。
修复：加 `PRIMARY KEY (symbol, stat_date, pub_date, item)`（或唯一索引 + `INSERT OR REPLACE`）；加重复计数监控与"重跑同窗口表不长"的用例。

**P1-10 无超时保护的源（tushare/akshare）可永久挂住 worker**
只有 baostock 走 `run_with_watchdog`（`baostock.py:473-482` 等）。tushare（`tushare.py:191-192`）与 akshare（`akshare.py:103-109,132-138,179-185`）的 SDK 调用**无任何超时**。
修复：包进 `run_with_watchdog` 或设 per-request timeout；加 `provider_timeout` 配置。

**P1-11 限流只做了一半且不可运行时配置**
只有 tushare（`tushare.py:157,189`）与 akshare（`akshare.py:83`）建了 `TokenBucket`；sina（`sina.py:89-90`）与 tencent（`tencent.py:129-130`）**接收 qps 但忽略**。qps 只在构建期从 yaml 读（`providers/__init__.py:91`），不是 `SettingsStore` 键（`settings_store.py:23,33`）。
修复：sina/tencent/mootdx 也建桶；加 `provider_qps` 设置项。

**P1-12 flock 失败静默降级为进程内锁 → 跨进程写会丢数据**
`parquet.py:48-55`：打开/加锁 `.lock` 失败（NFS/SMB/某些容器 overlay/只读目录）只 warning，然后**退化为仅进程内锁**。两个进程随后对同一整年文件做无同步的读-改-写，后写者的 `os.replace` **静默删掉先写者的行**——正是该锁要防的事，且无 fail-closed、无健康信号。
修复：非 dev 环境 fail-closed；或在 `/health` 暴露"锁已降级"标记。

### P2 —— 一致性、可观测性、运维

**P2-1 `GET /sync/freshness` 不存在，但前端在调**
`web/src/app/sync/page.tsx:62` 与 `web/src/app/data/FreshnessHealthCard.tsx:66` 调它；`sync.py` 没注册该路由，`manager.freshness()`（`sync/manager.py:568`）只能通过 `lq sync status`（`cli/commands/sync.py:52`）到达。→ 新鲜度卡片永远 unknown/404。**这是契约破坏，不是有意缓做。**

**P2-2 陈旧/源挂无告警**
`freshness()` 从不按节奏求值；失败只进进程内 `error_ring`（`sync/manager.py:530-551`），无 email/webhook/Slack（`docs/EXTENSION_POINTS.md:14` 明确不做）。`HealthTracker` 是纯内存、重启即丢、从不外露（`fallback.py:19-38`）。

**P2-3 HTTP 触发的 data_task 失败在监控里不可见**
`/monitor/data-pulls` 只读 `collect_log`（`monitor/queries.py:203-220`），而 data_task 失败既不进 `error_ring` 也不进 `collect_log`（`tasks.py:219-265,364-384`）。

**P2-4 湖内读取无损坏容忍**
`read_daily`（`parquet.py:293-319`）不逐文件容错；`/data/daily`（`data.py:307`）、`/data/indicators`（`:351`）、`coverage/monthly`（`:162`）遇到**一个**损坏年分区就 500。只有 `daily_range`（`parquet.py:386-395`）与 coverage（`data.py:119-127`）会降级。
修复：逐文件读 + 跳过并落 issue；补损坏分区用例。

**P2-5 `write_factor` 既不加锁也不原子**
`parquet.py:421-425` 直接 `write_parquet`，与 `write_daily`/`write_minute` 不一致（`factors/cache.py:92` 同）。→ 读者可能看到截断文件。
修复：复用 `_file_lock` + `_atomic_write_parquet`。

**P2-6 原子写无 fsync**
`parquet.py:66-74` 只做 temp + `os.replace`，**不 fsync** 文件与目录。`os.replace` 保证可见性原子，不保证持久性：断电可能留下**名字正确但截断/零长度**的文件，且无写后完整性校验。
修复：fsync 临时 fd 与目录；记录每分区行数供后续校验。

**P2-7 `read_minute` 是唯一不做 schema 漂移容忍的湖读者**
`parquet.py:461` 未传 `missing_columns`/`extra_columns`，而 `read_daily`/`read_daily_basic` 传了（`:306-312`、`:184-189`）——且**日线漂移是实测存在的**（§2.4-8）。

**P2-8 `data_task.rows_written` 报的是"取到的行"而非"落湖的行"**
`batch_rows += len(df)` 在写之前/独立累加（`daily.py:155,171,181`），质量门禁拦批时也照样加（`:169-173`），最后当 `rows_written` 存（`tasks.py:344-346`）。任务遥测不描述湖。

**P2-9 `data_version` 先注册后写入，且分配非原子**
`lineage.py:20-29` 与 `:32-39` 是两个独立 `writer()` 块；`daily.py:363-370` 先注册版本再门禁/写入。崩在中途会留下**幽灵版本**（而它是唯一的缓存失效锚点）。

**P2-10 `merge_daily_basic` 绕过写入层护栏**
`daily_basic.py:188-197` 私有导入 `_atomic_write_parquet`/`_file_lock` 直接重写日线年文件，**绕过** `_reject_demo_overwrite` 与 `_merge_quality_flags`（`parquet.py:227-288`）。今天它只填空所以不会覆盖，但护栏不是结构性生效的。

**P2-11 检查点 JSON 无锁、无形状校验**
`checkpoint.py` 的 `mark`/`unmark`/`record_coverage`/`set_meta` 是整文件读-改-写，写虽原子但**无互斥**（`:75-78,80-92,110-120,143-145`）。并发进程互相丢标记（丢 done 方向安全，丢 coverage 会放大 P0-4 的盲区）。`__init__` 只抑制 `JSONDecodeError`（`:65-66`），合法但非 dict 的文件（如 `[]`）会在 `:70/:148` 抛 `AttributeError`。
另：检查点名是**全局共享**的（`daily_basic.py:74`、`minute_{freq}`、`security_details`、`financial_pit_<source>`），而 `data_task` 互斥只保护 data_task 路径（`tasks.py:150-156`）——CLI 与 API 可并发跑同一检查点名。

**P2-12 遗留死表与死配置**
`daily_bar`/`minute_bar`/`adj_factor`/`sector`/`market_snapshot` 5 张 0 行死表（`ddl.py:70-94`，注释在 `:268-272`）；`v_daily`/`v_minute` 视图把**绝对 glob 冻结在创建时**且湖为空时整体跳过（`ddl.py:273-297`），数据后来到了也不刷新、根换了就失效。
`config/app.yaml` 死键：`store.daily_partition/minute_partition/compression`、`ingest.batch_size/checkpoint_every`、`quality.*`、`logging.*`（无读者）。

**P2-13 部分失败的多文件操作无事务标记**
`write_daily` 按年循环（`parquet.py:275-289`）、`merge_daily_basic` 按年循环、`delete_daily` 按文件循环（`:348-373`）。崩在中途 → 部分分区已更新、部分没有，且无"本批未完成"标记。单文件仍一致、重跑幂等，所以暴露面是"半应用的批次"而非损坏。

**P2-14 重复/漂移的实现**
- `/data/quote` 直连东财 `em_get`（`server/api/data.py:381-409`），绕过 REALTIME 链与健康跟踪（`market/ticks.py:66-71` 走 tencent/mootdx）→ 3 套实时实现。
- symbol→交易所推断三份：`data.py:373-378`、`data_admin.py:261-276`、`core/types.parse_symbol`。
- news 独立直连 akshare（`news/sources/em_news.py:15` 等），`news/tasks.py:124-126` 直接 `AkShareProvider()`，**无视 `providers.yaml enabled:false`** 与 fallback 链。

**P2-15 调度阻塞与孤儿任务**
作业在调度线程内同步执行（`sync/manager.py:339-466`），长任务阻塞其它到期作业与 30s tick。RQ enqueue 无重试（`server/jobs.py:356-400`），worker 死掉会让 data_task 卡在 `running` 直到 API 重启（`tasks.py:448-462`），并通过单任务互斥（`tasks.py:150-156`）**阻塞所有新任务**。
`deploy/launchd/com.lquant.sync.plist:11,16,141,143` 硬编码 `/Users/lyp/code/lquant`，且无 Linux cron/systemd。

### P3 —— 校验规则缺口（"更多手段"里最容易被想到、但收益低于上面结构性问题的一类）

已有：价格区间/OHLC 冲突/负量/单位失配/停牌填充/僵尸/复权跳变（`asserts.py`，仅日线）、主键重复（`pipeline.py:36-44`）、涨跌停（仅 close，`validators.py:174`）、日历对齐、覆盖度、僵尸日、复权因子单调、跨源 L0–L3 标记、幸存者偏差。

缺失：
1. **`pre_close[t] == close[t-1]` 连续性**——只查了 null（`asserts.py:115`）。非 null 的错误 `pre_close` 会静默污染涨跌停与收益率计算。**单项收益最高**。
2. **`pct_chg` 与 `close/pre_close-1` 一致性**——全仓无校验。
3. **OHLC 对涨跌停带**——只查 close；不可能出现的 `high` 超涨停不会被发现。
4. **量/额/换手交叉一致性**（`turnover_rate` vs `volume/float_share`）、成交量离群。
5. **daily_basic 市值/估值合理性**（`total_mv ≈ close×total_share`、pe/pb 符号与量级）与 ×1e4 单位断言。
6. **NaN/Inf 检测**——cast 是 `strict=False`，静默变 null（`schema.py:167-174`、`mapping.py:253-260`）。
7. **逐标的陈旧度**（与批次覆盖度无关）。
8. **分钟线序列校验**——无每 session bar 数、无重复 `ts`、无缺口、无 freq/边界校验（`minute.py:19-59` 全无）。
9. **security 主表校验**——代码格式、`list_date <= delist_date`、重复 symbol、board/ST 一致性（`reference.py:48-70,80-150` 无）。
10. **财务 PIT 合理性**——`pub_date >= stat_date`、重复键、单位、极值（`financial.py:107-128` 无）。
11. **指数成分**——权重和 ≈100、快照重叠（`index_cons.py:47-78` 无）。
12. **ETF 元数据**——费率区间、`sellable_after_days`、`fund_size>0`（`etf_meta.py:21-82` 无）。
13. **日历自校验**——`sync_calendar` 不校验行数/单调（`reference.py:27-45`）。
14. **未知板性静默关闭涨跌停检查**——`replace_strict(..., default=None)` → 阈值 null → `fill_null(False)`（`validators.py:147-148,174-175`）。应显式报 `BOARD_UNKNOWN`。
15. **孤儿检查**：`check_ret_identity`（qfq/hfq 收益一致性，`validators.py:202-228`）**零调用方**；`flag_tradability`（`tradability.py:81`）零调用方，导致 `SUSPENDED`/`NEW_LISTING`/`ST_RISK` 三个标志位是死位。
16. **无隔离区/拒收留痕**：fatal 时整批丢弃且**不持久化被拒行**（`daily.py:167-173`），`data_quality_issue` 只存计数 + ≤20 个标的（`validators.py:197`）→ **没有行级被拒值审计**。
17. **无仲裁**：`fallback.py:3-4` 自述"仲裁至少需要 3 个可用源"，但全仓无 quorum/多数/中位数实现（grep 仅命中该注释）。跨源只做标记（`ingest/crosscheck.py:213-227`），且 peer 取不到数据时降级为 `{"L0": 1}`（`:172-175,234-236`），与"全部一致"**不可区分**。抽样仅 ≤200 只非 ST（`:119-132`），任务后执行且失败被吞（`tasks.py:383-384`）。

---

## 4. "还有哪些手段"——增强路线图

把上面的缺口归纳成**六类手段**（这比逐条列规则更有用，因为多数缺口属于同一类）：

### 手段 A：完整性可观测（当前最大的空白）
现在的检查 95% 在问"取到的数据合不合理"，几乎不问"**该有的东西在不在**"。

- **A1 分区连续性校验**：每个年/月分区文件的 `min/max(trade_date)` 与交易日历对账，启动时 + 每次同步后跑。**这一条就能在 2025 缺失发生当天报警。**
- **A2 湖清单（manifest）**：每分区记录 `row_count / min / max / column_set / writer_version / checksum`，落 DuckDB。用于检测截断、漂移、意外改写。**同时解决 §2.4-8 的 schema 版本缺失。**
- **A3 数据集契约**：每数据集声明期望频率（日/周/月）、期望标的数区间、期望行数量级；违反即 issue。用于发现 §1.6 的"早期历史只有 1/5 标的"。
- **A4 全历史深度扫描**：与每日近端检查分离，周期性跑全量缺口扫描（当前只有近 5~30 天）。
- **A5 逐标的新鲜度**：每个标的最后观测日 vs 日历，独立于批次覆盖度。

### 手段 B：写入边界收口
- **B1 门禁下移到 `write_*` / repo upsert**，按数据集注册检查集（当前只有日线有门禁）。
- **B2 统一原子写**：`write_factor` 补齐 `_file_lock` + 原子写；所有原子写补 fsync。
- **B3 拒收隔离区**：fatal 行写入 `data/parquet/_rejects/<dataset>/` + issue 明细（含样本），提供修复工作流。**把"丢弃"变成"留痕可复算"。**
- **B4 写入前 schema 校验**：入湖帧对 `SCHEMAS` 强校验，未知列显式丢弃而非静默。

### 手段 C：失败语义正确化
- **C1 空响应 = 软失败**，触发切源；只有所有源都空才判空。
- **C2 QC 异常与源站故障分流**（`DataQualityError`/`MappingError` 立即上抛 + 落 issue，不惩罚源健康）。
- **C3 "attempted" 语义**：只把真正尝试过的键标 done（修 P0-2/P0-3）。
- **C4 覆盖度用区间包含判定，不用外框**（修 P0-4）；检查点加锁 + 形状校验。
- **C5 增量起点从湖/断点推导**，不用自然日偏移。

### 手段 D：多源真冗余
- **D1 所有 ingest 走能力+健康感知的链**（修 P1-1）。
- **D2 全源超时 + 全源限流**（修 P1-10/P1-11）。
- **D3 三源仲裁**：实现中位数/多数派按字段取值，明确 tie-break；peer 缺失时落"无法对拍"issue 而非 L0。
- **D4 契约一致性测试**：每个 provider 的每个声明能力都有契约测试（能立刻抓到 sina 返回 `hfq_close` 而非 `factor` 这类问题）。

### 手段 E：可恢复性（当前为零）
- **E1 自动快照**：每次破坏性操作（purge / 全量重写 / demo）前，对目标分区做硬链接或复制快照 + 保留策略。
- **E2 恢复工具**：`lq data restore --partition year=2025 --from <snapshot>`，而不是手工 `mv`。
- **E3 备份目录治理**：现存的 3 个 342MB 手工备份目录需要纳入管理（哪些可删、哪些是唯一副本——**`daily.bak-demo-20260918-144538` 目前是 2025 数据的唯一副本，绝不能删**）。

### 手段 F：可观测与告警
- **F1 补 `GET /sync/freshness`**（前端已在调，属契约破坏）。
- **F2 陈旧/源挂告警**：`freshness()` 按节奏求值，超阈值落 `data_quality_issue` + `error_ring`；加 webhook 出口。
- **F3 data_task 失败进监控**（补 `error_ring`/`collect_log` 出口）。
- **F4 锁降级 / 影子湖 / 数据根自检**暴露到 `/health`。
- **F5 跨进程锁统一**：`_lake_check_lock`/`_ref_lock` 目前是进程内的（`data.py:43,415`），purge 会与 CLI `lq data check`、调度器写湖竞争。

---

## 5. 建议落地顺序

**第 0 步（立即，不改代码）**
1. **恢复 2025 日线**（§1.1 方案），并确认那 30 只标的的补拉。
2. **保护 `daily.bak-demo-20260918-144538`**：它是 2025 的唯一副本，任何清理脚本都必须排除它。
3. 重建 `security` 主表（`lq data reference` 或修 tushare `securities()` 的 board/list_date/is_st 后重拉）——否则所有依赖它的检查都在拿 3 行数据跑。

**第 1 批（结构性地基，收益最大）**
4. 数据根锚定 + 影子湖自检（P1-6）——否则后续所有写入都可能是写错树。
5. 分区连续性校验 + 湖清单（手段 A1/A2）——让"整年缺失"这类问题当天可见。
6. 修 P0-2/P0-3/P0-4 三个"标 done 但没数据"的路径（都是单函数改动 + 明确回归用例）。

**第 2 批（收口）**
7. 门禁下移到写接口（B1）+ 拒收隔离区（B3）。
8. 全 ingest 走链（D1）+ 空响应软失败（C1）+ QC/源站故障分流（C2）。
9. `financial_pit` 主键（P1-9）+ 全源超时（P1-10）。

**第 3 批（可观测/可恢复）**
10. `GET /sync/freshness`（F1）+ 陈旧告警（F2）。
11. 快照 + 恢复工具（E1/E2）。
12. 三源仲裁（D3）、P3 类校验规则按收益排序补齐（`pre_close` 连续性优先）。

---

## 6. 审计方法与证据边界

- 4 路并行分层审计：providers/fallback/ratelimit、quality/validators、ingest/store/checkpoint、CLI/API/调度/可观测。均要求 file:line 举证。
- 我本人独立复验的关键结论：2025 缺失、影子湖、`security` 3 行、死表、CWD 相对数据根、`minute` 空响应标 done、daily 绕过 fallback、sina/adj 契约不符、无备份自动化、worktree 无 data 目录。
- 未做：未打开 live DuckDB 的写路径、**未执行完整测试套件**（因该仓有"pre-push 钩子污染真实湖"与"demo 覆盖真实湖"两次事故史，且缺全局数据根隔离，贸然全量跑测试可能污染生产湖——这本身是 §1.3 要确证的问题，建议在加隔离 fixture 后再跑）。
- 所有实测均为只读查询（Parquet footer 统计 / DuckDB SELECT count）。
