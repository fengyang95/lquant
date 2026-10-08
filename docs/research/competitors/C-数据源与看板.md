# C · 开源 A 股数据源 / 清洗存储 / 看板情绪 · 竞品调研

- 日期：2026-10-08
- 调研对象：30+ 个开源项目（数据源库 / 存储平台 / 看板 / 情绪指标 / 反爬工程）
- 对标平台：**lquant**（Python 3.12 + Polars + DuckDB + Parquet 湖按年分区；主源 BaoStock；可选 akshare/efinance/mootdx/tushare；Provider 抽象 + Capability + Fallback + 子进程看门狗 + 令牌桶限流 + 断点续传 + 质量断言；看板热通路 `src/lquant/market/collectors/`）
- 证据口径：**GitHub REST API（`repos/<o>/<r>` 的 `pushed_at`/`archived`/`open_issues_count`）+ PyPI JSON API（版本 `upload_time`）+ 实际抓取的 README/issue/官方文档**。所有星数、时间戳均为本日实测值；凡未能证实的条目显式标注「**未核实**」。

---

## 0. 结论摘要（TL;DR）

### 0.1 维护状态总表（实测时间戳，本日 = 2026-10-08）

| 项目 | 类型 | ★ | 最后代码推送 | 最后发布 | 结论 |
|---|---|---|---|---|---|
| [akfamily/akshare](https://github.com/akfamily/akshare) | 聚合源 | 22864 | 2026-10-07 | PyPI 1.19.1 / 2026-09-30 | **极活跃，但接口极脆** |
| [1nchaos/adata](https://github.com/1nchaos/adata) | 聚合源 | 5261 | 2025-12-26 | PyPI 2.9.5 / 2025-12-26 | **半停滞（~9 个月）** |
| [Micro-sheep/efinance](https://github.com/Micro-sheep/efinance) | 东财抓取 | 4080 | 2026-07-17 | PyPI 0.5.9 / 2026-07-17 | 低活跃，issue 积压 153 |
| [mootdx/mootdx](https://github.com/mootdx/mootdx) | 通达信 | 2413 | **2024-07-16** | PyPI mootdx 0.11.7 / **2024-05-04** | **停滞 ~2 年**；社区续作 [mootdxPlus](https://github.com/BiomancerGame/mootdxPlus) |
| [waditu/tushare](https://github.com/waditu/tushare) | 积分源 | 15447 | **2024-03-13** | PyPI 1.4.29 / 2026-03-25 | **GitHub 停更，SDK 靠 PyPI 续命** |
| **baostock**（官网，非 GitHub） | 主源 | — | 未核实（无公开仓库） | PyPI 0.9.4 / **2026-09-21** | **仍在维护（近 1 个月内有发版）** |
| [rainx/pytdx](https://github.com/rainx/pytdx) | 通达信协议 | 1552 | 2020-04-15 | PyPI pytdx 1.72 / **2019-08-26** | **已归档（archived=True），事实死亡** |
| [mpquant/Ashare](https://github.com/mpquant/Ashare) | 极简双源 | 3894 | 2025-12-24 | 无 PyPI | 半停滞；单文件设计仍可借鉴 |
| [shidenggui/easyquotation](https://github.com/shidenggui/easyquotation) | 实时行情 | 5455 | 2026-02-28 | PyPI 0.7.7 / 2025-03-25 | 低活跃但未死 |
| qstock | 分析封装 | 未核实 | 未核实 | PyPI 1.3.8 / **2025-03-16** | **停滞 ~1.5 年**（GitHub 真实仓库未证实） |
| [microsoft/qlib](https://github.com/microsoft/qlib) | 数据/ML 平台 | 49220 | 2026-10-08 | — | 活跃（维护模式） |
| [man-group/ArcticDB](https://github.com/man-group/ArcticDB) | 版本化列存 | 2539 | 2026-10-08 | PyPI 6.27.1 / 2026-10-06 | 活跃 |
| [duckdb/duckdb](https://github.com/duckdb/duckdb) | 查询引擎 | 41982 | 2026-10-08 | PyPI 1.5.6 / 2026-09-28 | 极活跃 |
| [ClickHouse/ClickHouse](https://github.com/ClickHouse/ClickHouse) | OLAP | 50295 | 2026-10-08 | clickhouse-connect 1.10.0 / 2026-10-07 | 极活跃 |
| [timescale/timescaledb](https://github.com/timescale/timescaledb) | 时序 PG | 23658 | 2026-10-08 | — | 活跃 |
| [openbq-org/OpenBB](https://github.com/openbq-org/OpenBB) | 数据标准化层 | 73975 | 2026-10-02 | PyPI openbb 5.0.0 / 2026-09-29 | 活跃（已换 org，见 §2.6） |
| [hello245m/free-stockdb](https://github.com/hello245m/free-stockdb) | 本地库 | 2813 | 2026-10-04 | Release 0.3.5 | **极活跃，最值得对标** |
| [rootSunc/CNEquity](https://github.com/rootSunc/CNEquity) | **Parquet 数据湖** | 未核实 | 未核实 | PyPI cnequity 0.16.0 / **2026-10-07** | **极活跃，架构与 lquant 高度同构** |
| [pythonstock/stock](https://github.com/pythonstock/stock) | 全栈看板 | 7879 | 2026-05-07 | — | 低活跃（README 标 V3.0 / 2025-02-28） |
| [gitkkkk/instock](https://github.com/gitkkkk/instock) | InStock 看板 | 38 | 2025-08-28 | Docker 镜像 | 低活跃；`myquant/InStock` 未解析成功（**未核实**） |
| [DR-lin-eng/stock-scanner](https://github.com/DR-lin-eng/stock-scanner) | AI 选股 | 1150 | 2026-03-02 | — | 低活跃，作者自述「有点摆」 |
| [simonlin1212/vibe-astock](https://github.com/simonlin1212/vibe-astock) | 短线复盘看板 | 668 | **2026-10-07** | v1.1.3 | **极活跃，看板口径最贴近 lquant** |
| [tradingview/lightweight-charts](https://github.com/tradingview/lightweight-charts) | 图表库 | 17515 | 2026-10-08 | 文档 5.2 | 极活跃 |
| [xbfighting/tdx2db](https://github.com/xbfighting/tdx2db) | 本地 TDX 库 | 165 | 2026-09-25 | PyPI tdx2db | 活跃（存储落地参考） |
| [zjp-CN/rustdx](https://github.com/zjp-CN/rustdx) | Rust TDX | 279 | 2026-04-08 | — | 活跃 |
| [quantskills/skill-b6-limitup-pool](https://github.com/quantskills/skill-b6-limitup-pool) | 涨停池口径 | 8 | 2026-09-07 | — | 活跃（**口径范本**） |
| [quantskills/skill-b7-lhb-monitor](https://github.com/quantskills/skill-b7-lhb-monitor) | 龙虎榜口径 | 12 | 未核实 | — | 活跃（**席位标签范本**） |
| [hyan1985/MarketMonitoring](https://github.com/hyan1985/MarketMonitoring) | 论坛情绪看板 | 13 | 未核实 | GitHub Actions 每日 | 活跃（**股吧情绪范本**） |
| [HelloYie/akshare-proxy-patch](https://github.com/HelloYie/akshare-proxy-patch) | monkey patch | 未核实 | 未核实 | — | **存在本身即 akshare/efinance 易碎的硬证据** |
| [trading4/dfcf_eastmoney](https://github.com/trading4/dfcf_eastmoney) | 涨停情绪 | 6 | **2024-06-21** | — | 停滞，仅口径参考 |

> `/repos/rainydew/pytdx`、`/repos/myhhub/Ashare` 均返回 Not Found：**通行引用里的 owner 名是错的**——pytdx 真身是 `rainx/pytdx`（已归档），Ashare 真身是 `mpquant/Ashare`。

### 0.2 Top 10 可借鉴项（按优先级）

| # | 借鉴项 | 来源 | 为什么对 lquant 关键 | 成本 |
|---|---|---|---|---|
| 1 | **水位即缓存键 / 有 failed batch 不推水位** | CNEquity `meta/state/{dataset}.json` + `manifest.py` | lquant 有断点续传 checkpoint，但缺「**水位前移门禁**」：批次失败仍可能推进水位 → 静默缺口。这是最便宜的健壮性提升 | 低 |
| 2 | **稀疏 tip 检测 + 覆盖率下限门禁** | CNEquity `valuation_bars_low_coverage`（覆盖 ≥70% 才认当日完整） | 直接防「末日只有几十只票却显示同步成功」——lquant 主源恰好是同一家 baostock | 低 |
| 3 | **契约清单 + 直播探针做字段漂移检测** | CNEquity `datacenter_contracts.py` + `tests/unit/test_datacenter_live_contracts.py`（`pageSize=1`，断言返回键 ⊇ 契约列），配合东财 `code=9501 列不存在` 的 fail-loud | lquant 有 `schema.py`/`quality/`，但缺**每报表一份声明式列契约 + 线上轻量探针**；东财列名漂移是实测发生过的 | 中低 |
| 4 | **存 hfq、查询期按窗口 anchor 派生 qfq** | CNEquity [ADR-0004](https://github.com/rootSunc/CNEquity/blob/34005e9ec81552ad1689fea6519bd4b37b52f998/docs/adr/0004-store-hfq-derive-qfq-at-query.md) | 前复权序列会随新除权事件**整体重算**，持久化不可复现且逼迫全历史回写。`factor_qfq(t) = hfq_factor(t)/hfq_factor(T)`，T = 窗口内 `≤end` 的最新交易日 | 中 |
| 5 | **`strict_adj` fail-loud 复权** | 同上：缺因子**报错**，绝不静默 `factor=1.0` | 静默 `factor=1.0` 是复权类 bug 的头号来源（lquant 的 `_safe_std` 兜底 1.0 是同类反模式） | 极低 |
| 6 | **涨停池状态机 + 显式 provenance 字段** | [skill-b6-limitup-pool](https://github.com/quantskills/skill-b6-limitup-pool)：状态机 6 态、特殊形态 8 类带优先级、`seal_metric_source=minute\|daily_proxy` | 分钟线拉不到时**自动降级并在数据里留痕**（而非静默变精度），正是 lquant 热通路需要的降级纪律 | 低 |
| 7 | **龙虎榜席位标签优先级链** | [skill-b7-lhb-monitor](https://github.com/quantskills/skill-b7-lhb-monitor)：沪深股通专用→北向；机构专用→机构；override；精确映射；子串种子库；营业部/分公司；普通。`hotmoney_net = 游资 + 营业部`（量化不计入） | lquant 有 `dragon_tiger` capability 但无席位语义层；这套优先级链可直接照搬，且自带「别指认个人」的合规边界声明 | 中 |
| 8 | **completely 声明式 schema 演进纪律：curated 列只增不改 + bump `dataset_schema_version`** | CNEquity 消费契约层 | 比「加断言」更根本：**破坏性变更必须显式 bump 版本**，下游可按版本拒绝 | 极低 |
| 9 | **单飞锁（single-flight）与限流正交** | CNEquity `RunLock("baostock")`：*rate-limit alone 不能阻止 N 个会话同时 `login()`* | lquant 有令牌桶但没有跨进程/跨任务的**源级互斥**；baostock 并发 login 正是封号触发条件 | 低 |
| 10 | **交易日主轴 + universe 过滤（ST/停牌/退市）** | CNEquity：窗口按 `trading_calendar` 计、不用自然日；`all_a` = 上市/退市过滤 + `trading_status` 逐日 ST/停牌过滤；幸存者偏差 audit 报 error | lquant 已有 `security` 表防幸存者偏差，但「**风控字段逐日快照**」这条通常被忽略 | 中 |

**额外一条（工程性价比最高）**：把「**低复权质量/低覆盖率**」做成**机器可读的 error finding** 而不是日志文字——CNEquity 的 `audit.universe_survivorship_absent` / `adj_factor_reconciliation` 就是这个形态。

### 0.3 「不要借鉴 / 已失效」清单

| 项 | 判定 | 证据 |
|---|---|---|
| **pytdx** | **已死**：仓库 `archived=True`，最后推送 2020-04-15，PyPI 停在 2019-08-26 | [rainx/pytdx](https://github.com/rainx/pytdx) |
| **mootdx（bopo/mootdx）** | **停滞 ~2 年**（代码 2024-07-16 / PyPI 2024-05-04）。`bopo/mootdx` 已不可访问（GitHub API 返回 `Repository access blocked`）；现属 `mootdx/mootdx`。**不要作为新增依赖的长期赌注** | 实测 API + PyPI |
| **tushare 旧 GitHub 线** | **GitHub 停更 2024-03-13**，且 [pythonstock/stock](https://github.com/pythonstock/stock) README 明写「2.0 最大的更新在于**替换 tushare 库（因部分库不能使用）**，使用 akshare」 | 项目 README（自证） |
| **北向资金「实时净流入 / 每日净买入」** | **数据源已政策性消失**：2024-05-13 起取消盘中实时披露；**2024-08-19 起**沪深股通只公布「成交总额及笔数、ETF 成交总额、**前十大成交活跃证券名单**及成交额」，持股数据改为**每季度第五个交易日**公布上季末单只合计持有量。**任何仍以「北向实时净流入」为情绪因子的模块都是失效或伪造的** | [新华财经 2024-08-19](https://www.cnfin.com/yw-lb/detail/20240819/4090757_1.html)、[东方财富 2024-08-26](https://finance.eastmoney.com/a/202408263165166650.html) |
| **ashare/efinance/akshare 的 `stock_zh_a_hist` 等「裸抓」路径** | **易碎，需补丁层**：存在专门的 monkey-patch 项目修 `stock_zh_a_spot_em`/`stock_zh_a_hist`/`get_realtime_quotes` | [HelloYie/akshare-proxy-patch](https://github.com/HelloYie/akshare-proxy-patch)、[akshare #6987](https://github.com/akfamily/akshare/issues/6987)、[#6574](https://github.com/akfamily/akshare/issues/6574)、[#7180](https://github.com/akfamily/akshare/issues/7180)、[#6061](https://github.com/akfamily/akshare/issues/6061) |
| **qstock** | **停滞 ~1.5 年**（PyPI 1.3.8 / 2025-03-16，仅 7 个版本） | PyPI |
| **adalate**（adata） | 半停滞（~9 个月无推送）；**不建议作为新增主依赖**，但其「统一多源 + 动态代理」文档可读 | 实测 API |
| **把 lquant 内部契约定成 pandas** | 所有 A 股开源库都以 pandas DataFrame 为事实契约；lquant 全 Polars + Rust，回退是倒退 | 与 ADR-11 一致 |
| **InStock / pythonstock 的前端与存储选型** | MySQL/MariaDB + tornado + bokeh（pythonstock）、Docker 单体镜像 —— 与 lquant 的 DuckDB/Next.js 定位冲突；**只借指标口径，不借技术栈** | 项目 README |
| **ArcticDB 作为 lquant 主存储** | 活跃且优秀，但引入 C++ 重依赖与版本化空间放大，与「Parquet 文件即产品、零运维」的现有取舍冲突；**列为可选后续，不进主路径** | ADR-0002 同类取舍 |

---

## 1. 数据源库

### 1.1 akshare（akfamily/akshare）

**1) 定位 + 活跃度 + 维护状态**
- 22,864★ / 3,530 fork / `archived=false` / `open_issues=2` / MIT / 最后推送 **2026-10-07T04:17:29Z**。
- PyPI：最新 **1.19.1（2026-09-30）**，近期 release 数 **223** —— 发版频率以「周」甚至「天」计。
- 判定：**极活跃**，但 `open_issues=2` 与「issue 被大量即时关闭」的维护风格一致——**活跃 ≠ 稳定**。

**2) 可借鉴的具体设计**
- 函数命名即数据契约：`stock_zh_a_hist` / `stock_zh_a_spot_em` / `stock_zh_a_daily`（新浪）等，**一个函数 = 一个源 + 一个数据集**，天然对应 lquant 的 `Capability` + `Provider` 二元组。lquant 的 `providers/akshare.py` 应保持「薄适配器」而非复制其逻辑。
- 参数口径需显式固定：`adjust`（`""` 不复权 / `"qfq"` / `"hfq"`）——**必须由 lquant 侧强制传入并落库记录**，不可依赖默认值。

**3) 失效与坑**
- **接口易碎是结构性特征**，不是偶发：仓库里存在大量「AKShare 接口问题报告」模板 issue，实测样本：
  - [#6061](https://github.com/akfamily/akshare/issues/6061)「**疑似使用了异步高并发导致东财大量封 IP**」（2025-04-15 开）——**与 lquant `ratelimit.py` 注释里「东财封 IP 是头号风险」是同一件事**；
  - [#6574](https://github.com/akfamily/akshare/issues/6574) `stock_zh_a_spot_em` 方法问题；
  - [#6987](https://github.com/akfamily/akshare/issues/6987) `stock_zh_a_hist` 报 `RemoteDisconnected('Remote end closed connection without response')`（典型风控掐连接）。
- 生态已出现**专门打补丁的项目**：[akshare-proxy-patch](https://github.com/HelloYie/akshare-proxy-patch) —— 「针对 akshare 和 efinance 的猴补丁插件，解决 `stock_zh_a_spot_em`、`stock_zh_a_hist`、`get_realtime_quotes` 等接口报错问题」。**一个库需要第三方常驻补丁层，等价于它没有稳定 API 契约。**
- 静默错误风险：东财返回的列名/列序会改版，`KeyError` 是 fail-loud（可接受），危险的是**列存在但语义/单位变了**（如成交额 元↔万元、成交量 手↔股）——这类不会报错。
- 2025-04 的 #6061 表明 akshare 内部曾有**异步高并发**；lquant 若直接复用其函数，会把上游的并发策略一并继承，绕过自己的令牌桶。

**4) 对 lquant 的建议**
- **保留 akshare 为「补缺源」，绝不升为主源**：在 `capability.py` 里对 akshare 声明的能力**逐条标注「易碎」**，并让 Fallback 链把它排在 baostock 之后。
- 对 akshare 路径**强制注入限流与超时**（走 lquant 自己的 `ratelimit` + `watchdog`），并**在子进程内调用**（隔离其内部可能存在的线程/异步）。
- 新增「**列契约断言**」：对每个用到的 akshare 接口登记必需列与单位，缺失/新增即 fail-loud（见 §5.3）。
- 把 `adjust` 参数纳入 `ingest` 的幂等键与 `lineage` 记录，避免同一 symbol 混入不同复权口径。

---

### 1.2 adata（1nchaos/adata）

**1) 定位 + 活跃度 + 维护状态**
- 5,261★ / 707 fork / `archived=false` / `open_issues=36` / Apache-2.0 / 最后推送 **2025-12-26T11:09:57Z**。
- PyPI：最新 **2.9.5（2025-12-26）**，共 81 个版本。仓库与 PyPI 同日 → **~9 个月无更新**。
- 自我定位（仓库描述原文）：「免费开源 A 股量化交易数据库；专注 A 股，专注量化……**多数据源融合，动态设置代理，保障数据高可用性**」。

**2) 可借鉴的具体设计**
- **「多源融合 + 动态代理 + 高可用」作为一等目标**，与 lquant 的 Fallback 链是同一问题域；其「动态设置代理」值得关注——lquant 目前只有限流，**没有出口轮换**（见 §5.1）。
- 提供 A 股特色数据集（涨停板、概念等），可作为 `Capability` 枚举的**覆盖度检查清单**：把它的数据集列表与 lquant 的 Capability 清单做差集，即得缺口。
- 依赖栈以 pandas 为主：**不要照搬其内存模型**。

**3) 失效与坑**
- 半停滞意味着**上游接口改版后无人修**；2025-12 之后的东财/同花顺改版它大概率未跟进（**具体失效接口未逐一验证**）。
- 动态代理的可用性依赖第三方免费代理池，**稳定性不可证**。

**4) 对 lquant 的建议**
- 只借「**用数据集清单反查 Capability 缺口**」这一用法；**不引入依赖**。
- 把「代理池/出口轮换」列入反爬待办，但优先级低于水位门禁与契约断言。

---

### 1.3 efinance（Micro-sheep/efinance）

**1) 定位 + 活跃度 + 维护状态**
- 4,080★ / 753 fork / `archived=false` / **`open_issues=153`** / MIT / 最后推送 **2026-07-17T07:47:42Z**。
- PyPI：最新 **0.5.9（2026-07-17）**，39 个版本。**近 3 个月无推送**，且 issue 积压 153 → 维护带宽不足。

**2) 可借鉴的具体设计**
- PyPI summary 明写「base on **eastmoney**」——**单一数据源、单一协议**的极简设计：所有函数走同一套东财 `push2/push2his` 请求封装。
- 「一个 `_get_json` 出口 → 所有数据集」的结构，天然便于**在一处集中做限流、重试、UA/Referer 注入**。lquant 的 `market/em_client.py` 已有同类思路，可对照其请求头与分页处理。

**3) 失效与坑**
- 与 akshare 同源同风险：**akshare-proxy-patch 同时修 akshare 和 efinance**，说明两者会**同时**因东财改版而挂。
- issue 积压 153 且近 3 个月无提交 → **修不过来的信号**。
- 静默错误：东财 `push2his` 分页在停牌/退市标的上会**返回空数组而非错误**，容易被当成「当日无数据」写入缺口。

**4) 对 lquant 的建议**
- 作为 `market/` 热通路的**二级备源**（主用自研 `em_client.py`），但**必须在其外层套统一限流 + 空结果语义判定**（空数组 ≠ 无数据）。
- 抽出其请求头/分页常量做对拍，验证 lquant `em_client.py` 的字段映射是否与当前东财一致。

---

### 1.4 mootdx（mootdx/mootdx）

**1) 定位 + 活跃度 + 维护状态**
- **注意仓库名**：`bopo/mootdx` 经 GitHub API 返回 `Repository access blocked`（不可访问），现真身为 [mootdx/mootdx](https://github.com/mootdx/mootdx)（2,413★，`archived=false`）。
- 最后代码推送 **2024-07-16**；PyPI `mootdx` 最新 **0.11.7 / 2024-05-04**（100 个版本）。
- 判定：**停滞约 2 年**。但出现社区续作 [BiomancerGame/mootdxPlus](https://github.com/BiomancerGame/mootdxPlus)（最后推送 2026-07-25）——**上游不维护，下游在分叉续命**。
- lquant 现状：`pyproject.toml` 已注明「mootdx 锁 `tenacity<9`」「mootdx 锁 `httpx<0.26`」，dev 依赖被迫跟随上限。

**2) 可借鉴的具体设计**
- 通达信协议的价值在于**不依赖网站反爬**：走 TCP 直连行情服务器，无 IP 封禁问题，是「离线/批量历史」的理想补充。
- 其「服务器列表 + 自动选可用节点」是多源思想在**连接层**的体现，可映射为 lquant 的 Fallback 粒度下沉。

**3) 失效与坑**
- 通达信服务器地址列表**会过期**，且社区常靠人工维护；上游停滞 2 年 → 地址表大概率过期（**具体过期条目未核实**）。
- 依赖上限（`tenacity<9`、`httpx<0.26`）**是硬约束**：会把 lquant 的 HTTP 栈钉在旧版（lquant 已实测踩到）。
- 数据质量：通达信分钟线**不含复权因子**，复权需自行拼接；且不同券商节点返回的**历史深度不一致**。

**4) 对 lquant 的建议**
- **保留为「分钟线/历史补充」的可选源，但不作为长期战略依赖**；在 `capability.py` 中标注 mootdx 为 `EXPERIMENTAL`。
- 若继续使用：**把节点列表做成可配置资产并纳入定期对拍**（用日线口径与 baostock 对拍，偏离即告警）。
- 评估 `rustdx`（[zjp-CN/rustdx](https://github.com/zjp-CN/rustdx)，279★，2026-04-08 活跃）作为**协议层的长期替代**——与 lquant 的 Rust 内核方向一致，且不引入 Python 依赖钉死。

---

### 1.5 tushare（waditu/tushare / tushare pro）

**1) 定位 + 活跃度 + 维护状态**
- 15,447★ / 4,430 fork / `archived=false` / **`open_issues=771`** / BSD-3 / 最后推送 **2024-03-13**（**GitHub 停更 2.5 年**）。
- PyPI：最新 **1.4.29（2026-03-25）**，229 个版本 → **只有 PyPI 在动，GitHub 代码线已死**。
- tushare pro 采用积分制，**免费额度有限**（具体每分钟限频数值随积分与接口不同，**未逐条核实**）。

**2) 可借鉴的具体设计**
- `pro_api(token)` 单例 + `pro.daily(ts_code=..., start_date=..., end_date=...)` 的**「接口名 + 参数 dict」统一调用形态**，是「把多数据集塞进一个客户端」的经典抽象；lquant 的 Provider 抽象更类型安全，**无需回收**。
- 其 `ts_code` 形如 `600000.SH` —— **lquant 已采用同一代码体系**，是 A 股事实标准，无需改动。
- PIT 财务数据的 `ann_date`/`f_ann_date` 分离（公告日 vs 实际公告日）值得对照 lquant 的 `pub_date` 口径。

**3) 失效与坑**
- **积分墙是产品性风险**：接口可用性与积分绑定，**策略依赖它 = 依赖商业政策**。
- 第三方项目已公开弃用：[pythonstock/stock](https://github.com/pythonstock/stock) README 原文「**2.0 最大的更新在于替换 tushare 库（因部分库不能使用）**」——**这是一手自证证据**。
- 静默错误：财务数据的**单位**（元/万元）与**单季/累计**口径在 tushare 各接口间不统一，是经典踩坑点。

**4) 对 lquant 的建议**
- 维持「**可选源**」定位，**不写入任何关键路径**；`capability.py` 中 tushare 的 `FINANCIAL_PIT` 需附加「依赖积分」标记。
- 引入前先落实 §5.3 的**单位契约断言**（金额类字段必须带单位标签）。

---

### 1.6 baostock（lquant 主源）

**1) 定位 + 活跃度 + 维护状态**
- **非 GitHub 项目**（`baostock/baostock` 在 GitHub 上不存在）；官网 [baostock.com](http://www.baostock.com/)（HTTP 301 → HTTPS）。
- PyPI `baostock` 最新 **0.9.4（2026-09-21）**，共 14 个版本（发版克制）。
- 判定：**仍在维护**（距本日 < 1 个月），是当前免费源里**唯一「免注册 + 官方 SDK + 明确复权因子文档」**的组合。
- **未核实**：官网知识库/文档页现已 JS 化，本次未能抓到 `adjustflag` 的官方原文（多次抓取仅得「www.baostock.com ×」外壳）。下述语义来自 lquant 现有实现与二手资料，**建议以 lquant 现有对拍脚本为准**。

**2) 可借鉴的具体设计（重点在「坑的边界」）**
- **官方提供独立的「复权因子」数据集与说明 PDF**（[BaoStock 复权因子简介](http://www.baostock.com/baostock/images/2/20/BaoStock%E5%A4%8D%E6%9D%83%E5%9B%A0%E5%AD%90%E7%AE%80%E4%BB%8B.pdf)）——**把复权当独立数据集而非日线参数**，这个设计值得肯定：因子可单独校验、单独对拍。
- `adjustflag` 三态（不复权 / 前复权 / 后复权）由**查询参数**控制，故**同一 symbol 在不同 `adjustflag` 下会产生三套价格序列**——lquant 必须把 `adjustflag` 纳入存储幂等键，否则会静默串味。

**3) 失效与坑**
- **静默挂起（lquant 已实测）**：BaoStock 批量请求会长时间无返回（lquant README 记录「实测 12 分钟」）。这是**最危险的一类失败**：不是报错，是卡住。
- **IP 黑名单（一手运维证据）**：CNEquity 的故障排查文档明确写：
  > `baostock login failed: 黑名单用户`（错误码 **`10001011`**）—— IP 被免费 API 封禁：**日请求 > 5 万、或并发连接、或扫太快**。处理：**停扫**；换出口或去 QQ 群求助解封。解封后用默认限速 resume，**勿并发**。
  > 「rate-limit alone **不能阻止 N 个会话同时 `login()`**」→ 因此他们加了 **`RunLock("baostock")` 单飞锁**。
  来源：[CNEquity 故障排查](https://github.com/rootSunc/CNEquity/blob/34005e9ec81552ad1689fea6519bd4b37b52f998/docs/operations/troubleshooting.md)
- **复权认知断层（二手，注意甄别）**：有文章称「`adjustflag=3` 所有价格全是前复权」——**该说法与主流文档（3 = 不复权）冲突，本报告不予采信**，仅作为「社区文档混乱本身即风险」的例证：[CSDN 五大陷阱](https://blog.csdn.net/weixin_28235513/article/details/166963637)。**结论：不要相信任何二手 adjustflag 说明，以 lquant 自己的数值对拍为准。**
- **稀疏 tip**：同一 CNEquity 文档记录了真实事故——用 `end=today` 拉，只写完部分标的，形成「稀疏 tip」，而旧逻辑把水位推到最大分区 → **水位显示新鲜、覆盖率断崖**。**这条几乎必然也发生在 lquant 的批量回填上。**

**4) 对 lquant 的建议（这是本报告最关键的一节）**
1. **保持 BaoStock 为主源**——它是当前性价比最高的免费源（免注册 + ETF 支持 + 官方复权因子）。
2. **补三条它没有的纪律**（全部来自对 CNEquity 事故的对照）：
   - **源级单飞锁**：`baostock` 的 `login()`/批量扫描加互斥锁，与令牌桶正交（限流管「多快」，单飞管「并发几个会话」）。
   - **稀疏 tip 检测**：每批写入后校验「当日 symbol 覆盖率 ≥ 阈值（如 70%）」，未达标**拒绝推进水位**并把缺口登记为 finding。
   - **水位前移门禁**：只有 `compact` + `audit` 全绿才推进 checkpoint（lquant 的断点续传目前缺少这层门禁）。
3. **复权走「存因子、查询期派生」**（见 §2.4 与 Top10 #4/#5），不要把三套价格序列都物化。
4. 用 `data/ingest/crosscheck.py` **把 baostock 日线与其他源逐日对拍**，把「对拍失败」也纳入水位门禁。

---

### 1.7 pytdx（rainx/pytdx）

**1) 定位 + 活跃度 + 维护状态**
- **正确仓库是 `rainx/pytdx`**（1,552★），GitHub API 返回 **`archived=true`**，最后推送 **2020-04-15**；PyPI `pytdx` 最新 **1.72 / 2019-08-26**。
- 判定：**已归档、事实死亡**。任何仍引用「pytdx 作为可选源」的文档都应更新。
- 存在备份分叉 `wps1112/pytdx_backup`（125★，2021-03-12）——同样是死线。

**2) 可借鉴的具体设计**
- 它证明了**「协议直连」路线的工程价值**（无网站反爬），也证明了**单一维护者停更后线路速死**。
- 其后续生态（`mootdx`、`rustdx`、`tdx2db`）说明：**协议本身活着，封装层在换人。**

**3) 失效与坑**
- 归档仓库的**服务器地址表、协议版本必然过期**；不要试图修它。

**4) 对 lquant 的建议**
- **从任何支持列表中移除 pytdx**；若需要通达信协议，走 `mootdx`（带 EXPERIMENTAL 标记）或评估 `rustdx`。

---

### 1.8 Ashare（mpquant/Ashare）

**1) 定位 + 活跃度 + 维护状态**
- **正确仓库是 `mpquant/Ashare`**（3,894★）；`myhhub/Ashare` 不存在（**用户给出的 owner 名有误**）。最后推送 **2025-12-24**，无 PyPI 包。半停滞。

**2) 可借鉴的具体设计**
- **单文件、零依赖、可裁剪**：`from Ashare import *` 即用，`get_price(code, frequency, count, end_date)` 一个函数覆盖 `1d/1w/1M/1m/5m/15m/30m/60m`。
- **双内核热备**：新浪 + 腾讯一主一备，自动故障切换——**这是 lquant 可以立刻借鉴的「同层双源」模式**（在 Provider 内部做热备，而不只在 Provider 之间做 Fallback）。
- **代码格式兼容层**：同时接受 `sh600519`（通达信）、`600519.XSHG`（聚宽）、`sh000001`（同花顺）——lquant 的 `normalize.py` 可对照补齐别名表。
- 返回统一 DataFrame（`open/close/high/low/volume`），**列名小写且固定**——lquant 的 `normalize.py` 应对每种源声明等价契约。

**3) 失效与坑**
- **单位陷阱（实证）**：Ashare README 里**指数日线**的 `volume` 是 `303718677.0`（股/手量级），而**个股 60 分钟线**的 `volume` 是 `4541.53`、`12030.00` —— **粒度和量纲明显不同**。这正是「静默单位混用」的教科书例子：都是 `volume` 列，含义不同。
- **无复权**：`get_price` 不含复权处理，历史价直接来自新浪/腾讯接口。
- 从 2025-12 后无更新 → 新浪/腾讯接口改版后**无人修**。

**4) 对 lquant 的建议**
- **借设计不借代码**：把「**Provider 内同层双源热备**」加进 `data/fallback.py`（当前是跨 Provider 的串行 Fallback，缺「主备同时注册、故障零延迟切换」）。
- **立刻做一次单位审计**：检查 lquant 各源写入的 `volume`/`amount` 是否**每个源都带单位标签**，并把「单位」纳入列契约（见 §5.3）。
- 补齐 `normalize.py` 的代码别名表（通达信/聚宽/同花顺三种写法）。

---

### 1.9 easyquotation（shidenggui/easyquotation）

**1) 定位 + 活跃度 + 维护状态**
- 5,455★ / 1,520 fork / `archived=false` / `open_issues=6` / MIT / 最后推送 **2026-02-28**；PyPI 最新 **0.7.7 / 2025-03-25**。**低活跃但未死**。
- PyPI summary：「实时获取免费股票行情，支持**新浪 / 腾讯(港股) / 集思录**」。

**2) 可借鉴的具体设计**
- **「源适配器 + 统一 quote 结构」**：`use("sina")` / `use("qq")` / `use("timekline")` 选择源，返回统一 `{code: {...}}` 字典 —— 是**实时快照层的多源抽象**，粒度比 lquant 的 Provider 更细。
- 对**实时行情**这一窄场景做了**批量拉取**（一次请求多个 code），是关键性能设计；lquant 的 `market/ticks.py` 可对照其批量上限。

**3) 失效与坑**
- **新浪接口的 Referer 要求**：`hq.sinajs.cn` 在 2022 年后要求 `Referer: https://finance.sina.com.cn`，缺失会返回 403。**具体行为未在本次实测**（**未核实**），但这是社区共识级已知变更。
- 源头若失效，`open_issues=6` 的体量意味着**修复缓慢**。
- 集思录（可转债）路径依赖第三方站点结构，易碎。

**4) 对 lquant 的建议**
- 作为 `market/` 实时快照的**参考实现**而非依赖：把「批量 code 上限」「Referer/UA 要求」写进 lquant 的 `sina.py` 注释与契约测试。
- 用它对拍 lquant `providers/sina.py` 的字段映射。

---

### 1.10 qstock

**1) 定位 + 活跃度 + 维护状态**
- PyPI `qstock` 最新 **1.3.8 / 2025-03-16**，**仅 7 个版本**；GitHub 真实仓库身份**未核实**（多次检索未定位到主流同名仓库）。
- 判定：**停滞约 1.5 年**。

**2)–4) 建议**
- **不引入**。仅将其「面向散户的数据+可视化封装」定位作为 lquant 看板的**用户视角参考**（它主打 notebook 一站式，lquant 走平台化，路线不同）。

---

## 2. 数据平台 / 存储

### 2.1 Qlib 的 bin 格式（只谈存储设计）

**证据**：[microsoft/qlib v0.9.7 `qlib/data/storage/file_storage.py`](https://github.com/microsoft/qlib/blob/v0.9.7/qlib/data/storage/file_storage.py)、[`scripts/dump_bin.py`](https://github.com/microsoft/qlib/blob/v0.9.7/scripts/dump_bin.py)。仓库 49,220★，`pushed_at=2026-10-08`，活跃。

**存储布局（实测源码）**
```
<provider_uri>/<freq>/
├── calendars/<freq>.txt          # 一行一个交易日，np.savetxt fmt="%s"
├── instruments/<file>.txt        # symbol \t start_date \t end_date（sep 可配）
└── features/<symbol>/<field>.<freq>.bin   # 纯 float32 裸数组
```
- `.bin` 是**单字段一文件**的 `float32`（小端 `"<f"`）**裸数组**，通过 `np.ndarray.tofile` 写出、`np.fromfile`/内存映射读回。
- **索引语义**：数组下标 = `calendars` 中的行号，而非存储日期。`FileFeatureStorage.write(data_array, index)` 在 `index` 处写入；**`index` 之前的空洞用 `np.nan` 填充**（源码：`np.hstack([[np.nan] * (index - self.end_index - 1), data_array])`）。
- **更新即追加**：`dump_bin.py` 的 `_data_to_bin` 在 `UPDATE_MODE` 下直接 `open("ab")` 追加 `np.array(_df[field]).astype("<f").tofile(fp)`；**不重写历史**。
- **instruments 带 `start_date`/`end_date`**，是**防幸存者偏差的存储级设计**——`instruments` 文件本身可以包含退市标的。

**与 Parquet 湖的取舍（对 lquant 直接相关）**

| 维度 | Qlib bin | Parquet 湖（lquant） |
|---|---|---|
| 布局 | **按 symbol 分目录、按 field 分文件** | 按时间分区、列式 |
| 读放大 | 读「全市场某字段某日」需打开 N 个文件 → **横截面查询差** | 分区裁剪 + 列裁剪 → **横截面查询好** |
| 写追加 | 天然 append（`ab`），**无 compaction 问题** | 需自行 compaction / 小文件治理 |
| 类型 | 只有 `float32`，**无 null、无字符串、无 schema** | 全类型 + 自带 schema/统计信息 |
| 演进 | 加字段 = 加文件（**列只增**，兼容性好） | 加列需重写或落新文件 |
| 一致性 | **无事务、无 manifest**，`index` 错位即静默污染 | 可加 manifest/水位 |
| 依赖 | numpy + mmap，极轻 | DuckDB/PyArrow |

**对 lquant 的建议**
- **不要改存储格式**：lquant 的「按年分区 Parquet + DuckDB」在「横截面选股 + 因子计算」这一主负载上**明显优于 bin**（bin 的按 symbol 布局正是横截面最差形态）。
- **但要借它两点**：
  1. **`instruments` 带起止日期**：lquant 的 `security` 表已有；需确认**查询层默认按 `start_date/end_date` 过滤**，而非事后过滤。
  2. **`index` 语义显式化**：lquant 用「按年分区 + 交易日」时，要保证**分区内也有交易日主轴**，缺日显式写 null（而不是跳行）——Qlib 的 `np.nan` 填充就是这条纪律。

---

### 2.2 ArcticDB（man-group/ArcticDB）

**证据**：[ArcticDB Library API 文档](https://docs.arcticdb.io/latest/api/library/)、仓库 2,539★ / `pushed_at=2026-10-08` / `open_issues=361` / license `NOASSERTION`（**注意：不是标准 OSI 许可，商用前必须读原文**）；PyPI **6.27.1（2026-10-06）**。

**可借鉴的具体设计（从 API 列表实证）**
- 一套**版本化列存** API：`write` / `append` / `update` / `update_batch` / `append_batch` / `delete` / `delete_batch` / **`delete_data_in_range`** / **`snapshot`** / `list_snapshots` / `delete_snapshot` / `delete_staged_data`。
- **增量语义分离得很干净**：
  - `append` = 追加新时间点（幂等、快）；
  - `update` = **就地修正历史点**（如行情修正）；
  - `delete_data_in_range` = **区间删除**（如错误批次回滚）；
  - `snapshot` = **不可变版本快照**（可复现研究）。
- **动态 schema**：DataFrame schema 可演进，配合版本化读取。
- `staged_data` 概念：写入可先「暂存」再提交——**与 CNEquity 的 `staging → curated` 两层遥相呼应**。

**失效与坑**
- **重依赖**：C++ 实现，仓库体积 **263 MB**（`size=263879` KB），构建/部署成本远高于 Parquet。
- **版本化 = 空间放大**：每次 `write` 保留旧版本，长期运行需显式 `snapshot`/`delete_snapshot` 管理，否则磁盘不可控（**具体放大倍率未核实**）。
- 许可 `NOASSERTION` → **不是干净的 MIT/Apache**，必须核对。
- 单机嵌入式定位清晰，但**不是数据湖格式**（无开放文件标准 → 锁死引擎）。

**对 lquant 的建议**
- **不引入为主存储**（理由见 ADR-0002 同类取舍：锁定引擎、体积、许可）。
- **借三个概念到现有实现**：
  1. **「修正历史点」是一等操作**：lquant 现在偏「重跑整批」，应显式支持 `update` 语义（`PK keep=last` 的定点覆盖，CNEquity 已在用）。
  2. **`delete_data_in_range` 式回滚**：数据质量断言失败时，要能**按区间撤销**一个批次。
  3. **snapshot = 可复现研究的锚**：把「用于某次回测的数据版本」记成快照 id，而不是靠文件时间戳。

---

### 2.3 DuckDB + Parquet 数据湖的业界实践

**A. CNEquity（最直接的对标，活跃度：PyPI 0.16.0 / 2026-10-07）**

证据：[CNEquity README](https://github.com/rootSunc/CNEquity)、[架构总览](https://github.com/rootSunc/CNEquity/blob/34005e9ec81552ad1689fea6519bd4b37b52f998/docs/architecture/overview.md)、[ADR-0002](https://github.com/rootSunc/CNEquity/blob/34005e9ec81552ad1689fea6519bd4b37b52f998/docs/adr/0002-parquet-lake-over-database.md)、[ADR-0004](https://github.com/rootSunc/CNEquity/blob/34005e9ec81552ad1689fea6519bd4b37b52f998/docs/adr/0004-store-hfq-derive-qfq-at-query.md)。规模：**42 个数据集（39 curated + 3 derived）**，技术栈 **Python / DuckDB / Polars / MCP** —— **与 lquant 技术栈几乎完全重合**。

**可借鉴的具体设计（ADR-0002 原文要点）**
- **分层（medallion）**：`staging`（每次 run 的原始落地）→ `curated`（**每个主键一行 canonical**）→ `derived`（计算数据集）→ `meta`（manifest / quality / snapshots）。
- **DuckDB 是便利层，不是真源**：原文「Expose an **optional** DuckDB view layer for SQL; **DuckDB is a query convenience, not the source of truth**」；zstd 压缩；**分区裁剪**是性能来源。
- **明确拒绝 DuckDB-only 存储**：「single-file DB couples storage to the engine and complicates **parallel writes** and **portability**」；也拒绝 Postgres/ClickHouse（运维负担）。
- **代价自认**：*「We must implement **compaction** and **PK de-duplication** ourselves」* —— lquant 同样必须自建这两件事。
- **`compact_gate.py`**：**compaction 有门禁**（manifest 驱动），不是无脑合并。
- **水位语义**：`meta/state/{dataset}.json`，**compact 成功后前移**；**有 failed batch 的数据集不推水位**。
- **schema 演进纪律**：**curated 列只增不改**；破坏性变更 **bump `dataset_schema_version`**。
- **交易日主轴**：窗口按 `trading_calendar` 计，**不用自然日**。
- **契约即缓存键**：*「下游缓存键宜含 `meta/state/{dataset}.json`；水位前移即失效」* —— 一句话解决了缓存失效问题。
- **幸存者偏差的量化实证**：同一等权买入持有策略、同一时间段 2016–2021，**保留退市股 5.9% vs 只用当前名单 12.0%** —— **收益被系统性高估一倍**。这是本次调研中最有力的「为什么必须防幸存者偏差」证据。

**B. Arc：Parquet compaction 的工程形态**
证据：[How Parquet Compaction Works in Arc (and Why It Runs in a Subprocess)](https://basekick.net/blog/arc-compaction-deep-dive)（2026-04-16）。
- 核心张力原文：*「Fast ingestion wants small, frequent writes. Fast queries want large, well-organized files. Compaction is the bridge.」*
- **关键工程决策：compaction 跑在子进程**。理由与 lquant 的**子进程看门狗**同源——长任务需可观测、可超时、可强杀。
- 启发：lquant 的 compaction 不应跑在 API/查询进程内，而应作为**独立任务 + 超时强杀**。

**C. 小文件与分区（通用最佳实践）**
- 分区键选择：**按年分区对日频全市场合适**（lquant 现状），但看板热表（涨停池/资金流）**体量小、写入频繁** → 按年分区会产生大量微小文件，**应单独用 DuckDB 表或按月/不分区的单文件**。
- **分区列类型必须一致**（`year=2026` 当字符串 vs 当整数会让谓词下推静默失效）。
- Row group 大小与压缩：zstd 适合「存多读少」，snappy 适合「读多」；**Parquet 统计信息（min/max）是谓词下推的前提**，写入时不要禁用统计。

**对 lquant 的建议**
1. **引入水位门禁 + 覆盖率门禁**（Top10 #1/#2），这是当前最大的静默失败面。
2. **明确「DuckDB 是便利层」**：把 `market/schema.py` 的五张热表与 Parquet 湖的关系写清——**热表可留在 DuckDB，湖是历史真源**，避免「湖和库两套真相」。
3. **实现 compaction（子进程 + 门禁）**：按年分区 + 高频小批写入必然产生小文件；参照 Arc 放子进程，参照 CNEquity 加 manifest 门禁。
4. **PK 去重语义显式化**：声明 `keep=last` 还是 `keep=first`，并在 audit 中校验（CNEquity 用 `keep=last` 覆盖稀疏快照）。
5. **借「交割契约层」概念**：lquant 已有 `capability` + `schema`，但缺一份**给下游的书面契约**（hfq/qfq 语义、PIT 语义、universe 语义、水位语义、schema 版本）。CNEquity 把它写成文档并声明「影响这些条款的改动 = breaking change」——**成本极低、收益极高**。

---

### 2.4 复权口径：本次调研最重要的技术结论

**证据**：CNEquity [ADR-0004: Store hfq factors only; derive qfq at query time](https://github.com/rootSunc/CNEquity/blob/34005e9ec81552ad1689fea6519bd4b37b52f998/docs/adr/0004-store-hfq-derive-qfq-at-query.md)（2026-07-06，**supersedes** 一个「持久化 qfq 并强制全历史重写」的旧方案）。

**论证链（原文）**
1. **qfq 锚定最新收盘价**：每次新公司行为，上游会把**整条** qfq 序列重算 → 「today 算出的 2018-06-01 的 `adj_close`，在下一个除权日之后就对不上了」→ **持久化 qfq 快照不可复现，且每次刷新都要全历史重写**。
2. **hfq 锚定上市基准**：新事件只**追加**事件日及之后的因子行，**历史 hfq 不变** → **天然 append-only**，与「按除权日定向回补（`symbols_to_rebackfill`）」吻合。
3. **决策**：`derived/adj_factors` **只存 `adjust_type='hfq'`**；qfq 在**读路径**派生：
   ```
   factor_qfq(t) = hfq_factor(t) / hfq_factor(T)
   ```
   `T` = 查询窗口内该 symbol 的**anchor trade date**（`≤ end` 的最新交易日；`end` 开放时取湖内最大 bar 日）。`t = T` 时 `factor_qfq(T) = 1.0`，保证**最新价 = 原始价**。
4. **配套纪律**：
   - `strict_adj=True` 时缺因子**报错**，**不静默 `factor=1.0`**；研究路径偏好 `adjust="hfq"`。
   - **append-only derive**：只刷除权日/新上市标的，不重写旧分区。
   - DuckDB 视图 `daily_bars_adj` 用**湖内最新 bar 作开放窗口 anchor**；有界窗口必须用表宏 `daily_bars_qfq(start_date, end_date)` 施加**同一 anchor 规则**——**否则 SQL 与 `load()` 口径不一致**。
   - 配置里若写 `qfq` → **记 warning 并忽略**（qfq 永不落盘）。
5. **自认的代价**：直接读 `derived/adj_factors` parquet 的外部消费者**必须自己套用同一比例**。

**对 lquant 的建议（高优先级）**
- **审查 lquant 的复权实现**：`data/ingest/adj.py` 是否物化了 qfq？若是 → **迁移到「存 hfq + 查询期按窗口 anchor 派生 qfq」**。
- **anchor 必须显式**：`load(..., adjust="qfq")` 与 SQL 视图必须使用**同一 T 定义**，并把这个 T 记进 `lineage`。**两套口径是本类 bug 的最常见来源。**
- **`strict_adj` fail-loud**：禁止 `factor=1.0` 静默兜底（与 lquant 已知的 `_safe_std` 兜底 1.0 是同一类反模式，应一并清理）。
- **把复权口径写进文档契约**（§2.3 建议 5）。

---

### 2.5 ClickHouse / TimescaleDB 在量化场景的取舍

**硬数据**：ClickHouse 50,295★ / `pushed_at=2026-10-08` / `open_issues=8164` / Apache-2.0；`clickhouse-connect` PyPI 1.10.0 / 2026-10-07。TimescaleDB 23,658★ / `pushed_at=2026-10-08` / license `NOASSERTION`（**Timescale License，非纯 OSS**）。

**取舍表**

| 维度 | ClickHouse | TimescaleDB | DuckDB + Parquet（lquant 现状） |
|---|---|---|---|
| 形态 | 列式 OLAP **服务** | **Postgres 扩展** | **嵌入式**进程内 |
| 写入吞吐 | 极高（批量） | 高 | 中（单写者） |
| 去重 | `ReplacingMergeTree`（**最终一致，非实时**） | `ON CONFLICT` / 唯一索引（**强一致**） | 需自建 PK 去重 |
| 时序专用 | 一般（靠 `ORDER BY` 键） | **hypertable / chunk interval / 压缩策略** | 无，靠分区 |
| SQL 生态 | 自成一派（Python 客户端另装） | **完整 Postgres** | DuckDB SQL（接近 PG） |
| 运维 | **重**（集群/合并/磁盘） | 中（PG 运维 + 扩展） | **零** |
| 许可 | Apache-2.0 | **Timescale License** | MIT |
| 何时该上 | 多用户并发 OLAP、TB 级、需低延迟聚合 | 已有 PG 生态、需事务与强一致去重 | **单机研究、文件即产品** |

**关键坑**
- **ClickHouse 的 `ReplacingMergeTree` 去重不是实时的**：查询时可能读到重复行，**必须 `FINAL` 或 `argMax`**——「以为去重了其实没有」是经典静默错误。
- **`ORDER BY` 键设计决定一切**：键选错 → 查询全表扫。量化场景推荐 `(symbol, ts)` 或 `(ts, symbol)` **取决于主负载是时序还是横截面**。
- **TimescaleDB 许可变更**：`NOASSERTION` 意味着**不是 Apache/MIT**；商用需读原文（lquant 若考虑，先过许可）。
- **DuckDB 单写者**：并发写入受限，多进程写同一 `.duckdb` 文件会失败——**lquant 的多 worker 架构必须保证「单写者」或「分文件」**（lquant 已有单写者连接设计的意识）。

**对 lquant 的建议**
- **现阶段继续「DuckDB + Parquet」，不要引入服务型 DB**。理由与 CNEquity ADR-0002 完全一致：研究平台的瓶颈是**数据正确性**而非查询并发，运维负担是不必要的税。
- **划分边界**：确需低延迟**看板聚合**（多用户同时刷）时，可考虑 **ClickHouse 单机**；在那之前，DuckDB 物化视图足够。
- **绝不用 `ReplacingMergeTree` 的「最终一致去重」承载主键语义**——若上 CH，用 `argMax`/`FINAL` 显式表达。
- 若未来上 CH，注意其**客户端与 lquant 依赖栈（Polars/PyArrow）的转换开销**，并保持 Parquet 湖为真源（CH 只是加速层）。

---

### 2.6 free-stockdb（hello245m/free-stockdb）

**1) 定位 + 活跃度 + 维护状态**
- **2,813★**，最后推送 **2026-10-04**（极活跃），Release 通道 `测试版本 0.3.5`，**Windows/macOS/Alpine/manylinux 多平台已发布**。
- 定位（README 原文）：「面向 A 股日 K、分钟 K 与 ETF 分钟、tick 级数据的**本地量化引擎**……将数据同步、清洗、复权、组织为可直接用于批量查询、批量计算研究的数据底座」。
- **形态是「双击更新 → 双击启动 → 直接调用」的本地二进制服务（核心仅 2.2 MB）**，提供 **Python SDK / HTTP API / Excel-WPS / HTML / MCP 五种调用方式**。

**2) 可借鉴的具体设计（README 中最有价值的一段）**
它用一张表把「在线 API 方案 vs 本地引擎」的真实工作量列了出来（原文数据）：
| 环节 | 在线 API 真实耗时 | 本地引擎 |
|---|---|---|
| 数据下载 | 全市场分钟线逐股请求，受限频/积分/IP 风控，**实测 3–5 天**；爬虫批量拉取**封号/限频** | Zstd 压缩传输 + 增量同步 + 断点续传 |
| 数据清洗 | 停牌/退市/代码变更/缺失补拉/时间排序 **1–2 天** | 同步流程自动完成 |
| 复权处理 | 分红送转、推算因子、**历史回刷** **1–2 天** | 内置完整复权因子，**查询时写时计算** |
| 存储设计 | 选型/建表/索引/批量入库 **1–3 天** | 定制 C++ 时序引擎 |
| 指标计算 | pandas 全市场遍历**单次数小时** | **Rust 计算核心比 pandas 快 3 倍，数秒** |
| 板块映射 | 爬概念/行业 + 双向索引 + 持续维护 **1–2 天** | 内置申万一二三级 + **1200 概念板块**，`bk.get()` 毫秒 |
| 接口对接 | 多端各自维护 **1–2 天** | 五种调用方式统一内置 |
| **合计** | **最少 10–15 个工作日** | **30 分钟内可运行** |

- **其他具体设计**：
  - **存储**：历史数据 **Zstd 压缩**，宣称「比 csv/mysql 小 3 倍以上」；**按代码 + 时间范围批量读取**。
  - **复权**：「**查询时写时计算**」——与 CNEquity ADR-0004 的「存 hfq、查询期派生」**独立趋同**，两条证据互为佐证。
  - **增量**：`sync_url.txt` 配置数据源；**只处理变化数据**；**文件校验和**；离线仍可查询。
  - **接口设计**：`rd.get_data(code=7000_codes, start=..., end=..., frequency=..., fq=..., fields=..., as_df=...)` —— **全市场批量 + 任意周期 + 任意复权 + 任意字段筛选**集中在**一个函数签名**里。
  - `zb.get(name="ma,kdj,macd,...", codes=7000_codes, ...)` —— **指标名做成字符串列表**，服务端批量算（39 指标 / 5 指数）。
  - **多形态交付**：同一份本地数据用 Python/HTTP/Excel/HTML/MCP 五种协议暴露——**这是 lquant 可以借鉴的产品化思路**（lquant 已是 HTTP + CLI，**MCP 是缺口**）。

**3) 失效与坑**
- **闭源二进制**：无法审计其复权/清洗实现，**「查询时写时计算」的具体 anchor 未公开**（是否与 lquant 口径一致**未核实**）。
- **Release 标为「测试版本」**：0.3.x，接口可能破坏性变更。
- **下载地址包含非 GitHub 的直链**（README 中 `http://1783355894` 之类），**分发渠道可信度存疑**。
- 声称的「3–5 天」「快 3 倍」等为**自述数据，无第三方验证**（**未核实**）。
- 依赖其自带同步源（`sync_url.txt`），**数据源不可替换则受制于人**。

**4) 对 lquant 的建议**
- **不要引入其二进制**（闭源 + 测试版 + 分发渠道）。
- **强烈借鉴三点**：
  1. **「本地优先 + 离线可查」作为产品承诺**——lquant 已是本地湖，应把「**同步成功即可离线研究**」写成显式契约。
  2. **「一个函数 + 全市场批量 + 多周期 + 多复权 + 字段筛选」**的查询签名——检查 lquant 的 `load()` 是否也做到**一次调用取全市场**，避免研究脚本里出现 N 次单股查询（那正是它表里「3–5 天」的成因）。
  3. **多协议暴露（尤其 MCP）**——lquant 已有 CLI/HTTP；**把因子/指标查询暴露成 MCP 工具**是低成本高价值的补齐。
- 它的「复权查询期计算」是**对 CNEquity ADR-0004 的独立第二证据**，进一步支持 §2.4 的迁移建议。

---

### 2.7 OpenBB Platform 的数据标准化层设计

**1) 定位 + 活跃度 + 维护状态**
- **仓库已迁 org**：`OpenBB-finance/OpenBB` 与 `OpenBB-finance/OpenBBTerminal` 均返回 **301 Moved Permanently**；现真身为 [openbq-org/OpenBB](https://github.com/openbq-org/OpenBB)（**73,975★**，最后推送 **2026-10-02**，描述「Open Data Platform for analysts, quants and AI agents」）。
- PyPI `openbb` 最新 **5.0.0（2026-09-29）**。
- **已归档的周边**（重要信号，说明商业重心迁移）：
  - `OpenBB-finance/openbb-forecast` → **archived**（最后推送 2024-07-19）；
  - `OpenBB-finance/experimental-openbb-platform-agent` → **archived**（2024-07-22）；
  - `OpenBB-finance/openbb-platform-pro-backend` → **archived**（最后推送 2026-08-24）。
- 判定：核心 Platform **活跃**；**开源 Terminal 形态已让位于商业 Workspace**。

**2) 可借鉴的具体设计（证据：[Standardization | OpenBB Docs](https://docs.openbb.co/odp/python/developer/standardization)）**
- **两个 Pydantic 模型定义一个命令**：原文「Every provider-backed command is defined by **two Pydantic models**: a **`QueryParams`** subclass for its inputs and a **`Data`** subclass for one row of output.」
  → 打印周期：**输入契约 + 输出行契约**，二者都是类型。
- **标准模型集中在 `openbb_core.provider.standard_models`**：原文「one module per dataset, for example `EquityHistoricalQueryParams` and `EquityHistoricalData` in `openbb_core.provider.standard_models.equity_historical`.」
  → **一个数据集一个模块**，命名 `XxxQueryParams` / `XxxData`。
- **字段的「标准 vs provider-specific」由声明位置自动判定**：原文「The engine classifies each field by **where it is declared**. Fields declared on a class inside the standard models package are **standard**; fields declared on the provider's own subclass are **provider-specific**.」
  → **不需要手工维护映射表**：继承即标准化。生成的方法「takes standard parameters as named arguments and accepts provider-specific parameters as **keyword arguments**」。
- **具体样例（原文）**：`CboeEquityHistoricalQueryParams` 从 `EquityHistoricalQueryParams` 继承 `symbol`/`start_date`/`end_date`，自己加 `interval`/`use_cache` → 生成的 `obb.cboe.equity.historical` 的签名里有 `symbol/start_date/end_date/provider`，而 `interval`/`use_cache` 走 `**kwargs`。
  → **公共字段进签名、扩展字段走 kwargs** 是「标准化而不失扩展性」的优雅解法。
- 另有 [Validators 文档](https://docs.openbb.co/odp/python/developer/how-to/validators) —— 把校验挂到标准模型层，**一次校验、所有 provider 受益**。
- 还有 [Migration from V4](https://docs.openbb.co/odp/python/migration-from-v4) 文档形态——**大版本破坏性变更有专门迁移指南**，这是成熟项目的基础设施。

**3) 与 lquant 的逐项对照（这是本节的核心价值）**

| 能力 | OpenBB | lquant 现状（`src/lquant/data/`） | 差距 |
|---|---|---|---|
| 能力声明 | 靠**方法是否实现**隐式表达 | **显式 `Capability` StrEnum** | **lquant 更强**（可路由、可校验） |
| 输入契约 | `QueryParams`（Pydantic） | 各 Provider 函数签名 | **lquant 缺类型化 QueryParams** |
| 输出行契约 | `Data`（Pydantic，按数据集分模块） | `schema.py` + `normalize.py` 列映射 | lquant 是「列」级，OpenBB 是「模型」级（含类型与校验） |
| 字段映射 | **继承自动判定 standard / provider-specific** | `mapping.py` **手工映射表** | **关键差距**：手工表会漂移，继承判定不会 |
| 扩展字段 | 走 `**kwargs` | 无统一约定 | 中 |
| 错误体系 | 统一异常（`OpenBBError` 等，**未逐一核实具体类名**） | 有 `watchdog`/`fallback`，异常层级**未统一核实** | 中 |
| 校验位置 | **标准模型层（一次校验，全 provider 受益）** | `quality/` 断言（数据入湖后） | lquant 是「事后断言」，OpenBB 是「入口校验」——**两者应并存** |
| 自动命名空间 | 生成 `obb.<provider>.<model>` | 手工注册 Provider | 中（lquant 规模小，收益有限） |

**4) 对 lquant 的建议**
- **最高价值的一条**：把 `mapping.py` 的**手工列映射**升级为**「声明式模型继承」**——
  - 为每个 `Capability` 定义一个**标准输出模型**（必需字段 + 类型 + 单位标签）；
  - Provider 通过**继承 + 只声明差异字段**来适配；
  - **标准字段/扩展字段的区分由声明位置决定**，不再靠人工维护映射表。
  - 这直接消灭「新增源时忘记更新映射表」这一类 bug，且让 `capability.py` 与模型**互为校验**。
- **输入侧补 `QueryParams`**：把「一次请什么」也类型化（symbols / 时间窗 / freq / adjust / 是否 PIT），**顺带解决 lquant 的 `adjust` 口径漂移问题**（参数类型化后，未显式传 adjust 无法通过校验）。
- **校验前置**：在 Provider 返回处做「**入口校验**」（列名/类型/单位齐全），在入湖后做「**湖级断言**」（分布/覆盖率/跨源对拍）。**两层都要**，OpenBB 只有前者、lquant 目前偏后者。
- **不要抄它的整体架构**：lquant 的 `Capability` 显式枚举在自动化路由上**优于** OpenBB 的隐式能力表达；**保留并强化**。
- **注意其商业重心**：Terminal/Pro 已归档，**OpenBB 不宜作为产品形态对标**，只作**数据标准化层的教科书**。

---

## 3. 看板 / 情绪 / 选股 Web 应用

### 3.1 InStock（InStock 股票系统）

**1) 定位 + 活跃度 + 维护状态**
- **重要更正**：用户给出的 `myquant/InStock` 在本次实测中**未能解析成功**（GitHub 连接失败/Not Found，**未核实**）。当前可见的 InStock 代码库副本为 [gitkkkk/instock](https://github.com/gitkkkk/instock)（38★，最后推送 **2025-08-28**），Docker 镜像 `mayanghua/instock`（自述镜像仅 170 MB）。
- 另有 [opensamai/InStock](https://github.com/opensamai/InStock)（描述：「InStock股票系统，**基于 akshare**抓取股票每日关键数据……」）。
- 判定：**低活跃**，且**代码库归属分散** → 作为可依赖的上游不成立，**只作口径参考**。

**2) 可借鉴的具体设计（价值很高，即使项目本身低活跃）**
- **选股条件分类体系（README 实测）**：把 200+ 栏目分六类，**这是 A 股选股 UI 的信息架构范本**：
  1. **股票范围**：市场/行业/地区/概念/风格/指数成份/上市时间；
  2. **基本面**：估值/每股/盈利能力/成长/资本结构与偿债/股本股东；
  3. **技术面**（大量具名形态）：MACD 金叉、KDJ 金叉、放量突破、低位资金净流入、高位资金净流出、突破均线、均线多头/空头排列、连涨放量、下跌无量、一根/两根大阳线、旭日东升、拨云见日、七连阴（七仙女下凡）、八连阳（八仙过海）、九连阳（九阳神功）、四串阳、天量法则、放量上攻、穿头破脚、倒转锤头、射击之星、黄昏之星、曙光初现、身怀六甲、乌云盖顶、早晨之星、窄幅整理；
  4. **消息面**：公告大事、机构关注、机构持股家数/比例；
  5. **人气指标**：**股吧人气排名 / 排名变化 / 连涨 / 连跌 / 创新高 / 创新低 / 新晋粉丝占比 / 铁杆粉丝占比 / 7 日关注排名 / 今日浏览排名**；
  6. **行情数据**：股价表现、成交情况、资金流向、行情统计、**沪深股通**。
- **「人气指标」这一类是 lquant 目前缺的**：它把**股吧人气**做成了**结构化因子**（排名 + 变化 + 连涨连跌 + 粉丝结构），比单纯「发帖情绪」更可量化。
- 技术指标 + 筹码分布（CYQ / 成本分布）是其另一特色，**lquant 的 Rust 内核适合承接这类批量计算**。

**3) 失效与坑**
- **「沪深股通」栏目已受 2024-08-19 政策冲击**（见 §4.3）：实时北向已不可得，**该栏目若仍展示「北向净流入」即口径过期**。
- 依赖 akshare 抓取 → **继承 akshare 的全部易碎性**。
- **项目低活跃 + 代码库分散**：Docker 镜像是**唯一发布日期**，安全更新不可期。

**4) 对 lquant 的建议**
- **直接借信息架构，不借代码**：把上述**六类选股条件**作为 lquant 看板/因子目录的组织框架；尤其补 **「人气指标」类因子**（股吧人气排名及其变化）。
- **「七仙女下凡 / 八仙过海 / 九阳神功」这类中文形态名**：lquant 若做中文 UI，这是**用户熟悉的命名体系**，值得保留为 alias（底层用规范名 + 中文别名，呼应 lquant 「注册表 + 别名」的既有模式）。
- **明确把「沪深股通」栏目改造**为新披露口径（成交总额 + 前十大活跃成交股 + 季度持股），而不是继续显示已消失的净流入。

---

### 3.2 pythonstock/stock

**1) 定位 + 活跃度 + 维护状态**
- 7,879★ / 2,358 fork / `archived=false` / `open_issues=78` / Apache-2.0 / 最后推送 **2026-05-07**。
- README 标注 **「pythonstock V3.0 项目简介，2025.02.28 更新」**，并称「项目创建于 2017 年 7 月 17 日，**每月不定期更新**」。
- 判定：**低活跃**（README 自述版本停留在 2025-02），**曾是标杆，现已老化**。

**2) 可借鉴的具体设计（技术栈与 lquant 冲突，但工程经验有料）**
- **技术栈**（V3.0 原文）：pandas + numpy 清洗；**MySQL/MariaDB** 存储；**tornado** web；**bokeh** 绘图；**akshare 1.15.59** 抓取；**cron** 每日 18:00 起算，用 **300 天数据**，约 **15 分钟**算完；Docker 部署（镜像压后 200 MB / 本地 500 MB）。
- **反封禁缓存策略（原文第 4 点，值得直接借鉴）**：
  > 「股票数据接口防止被封，**按天进行数据缓存，储存最近 3 天数据，每天定时清除**，同时使用 `read_pickle` / `to_pickle` 的 **gzip 压缩模式**存储。」
  → **「短 TTL 本地缓存 + 每日清理」**是防封的朴素有效手段。
- **通用数据展示系统（原文第 6 点）**：
  > 「配置字典模板之后，**页面自动加载数据并完成展示**，后续自己开发的指标数据可以加入进去。」
  → **「配置驱动的看板」**：新增指标无需写前端；lquant 的 Next.js 前端可考虑「**指标配置 → 自动渲染**」的元数据驱动模式。
- **数据源切换的自证**（原文第 8 点）：
  > 「2.0 最大的更新在于**替换 tushare 库（因部分库不能使用）**，使用 akshare 进行数据抓取。」
  → **tushare 失效的一手证据**（见 §0.3、§1.5）。
- **业务视角**：龙虎榜（新浪）、大宗交易、东财数据——其**页面清单就是 A 股看板的「标准需求集」**。

**3) 失效与坑**
- **MySQL/MariaDB 存储 + pandas 全量计算**在 2026 年是**性能和运维双重负债**；lquant 的「Polars + DuckDB + Parquet」正是对它的换代。
- 依赖 akshare 1.15.59（旧版）→ **接口漂移后大概率已挂**。
- 「每月不定期更新」的承诺与实际推送频率不符。

**4) 对 lquant 的建议**
- **不引入其技术栈**（明确列入「不要借鉴」）。
- **借鉴两点**：
  1. **短 TTL 缓存 + 定时清理**用于**热通路**（涨停池/资金流当天重试时不必重复打源）；
  2. **配置驱动的通用看板**（指标 → 元数据 → 自动渲染），降低新增情绪指标的前端成本。
- 把它的**页面清单**与 lquant 看板做差集，补齐「大宗交易」等缺失页面。

---

### 3.3 stock-scanner（DR-lin-eng/stock-scanner）

**1) 定位 + 活跃度 + 维护状态**
- 1,150★，最后推送 **2026-03-02**。定位：「AI 增强股票分析系统」——**25 项财务指标 + 新闻情绪 + 技术指标 + AI 解读**，PyQt6 桌面 + Flask Web（SSE 流式）。
- README **自述**：「**近期刚刚开学。事情比较多有点摆。可能有些问题修复受限**」→ **维护者自认降速**。
- 判定：**低活跃**，AI 分析型项目的典型生命周期（热情驱动、易停滞）。

**2) 可借鉴的具体设计**
- **25 项财务指标的分类框架**（盈利能力 / 偿债能力 / 营运能力 / 发展能力 / 市场表现）——**可直接作为 lquant 基本面因子的覆盖度检查表**：
  - 盈利：净利润率、ROE、ROA；
  - 偿债：流动比率、资产负债率、利息保障倍数；
  - 营运：总资产周转率、存货周转率；
  - 发展：营收增长率、净利润增长率；
  - 市场：PE、PB、**PEG**。
- **多 AI 提供商 + 主备自动切换 + 降级**：「主备 API 自动切换，确保服务可用性」；「**AI 不可用时自动降级到高级规则分析**」。
  → 与 lquant 的 Fallback 思想同构，但作用在 **LLM 层**；lquant 的 agent 模块可借鉴「**LLM 不可用 → 规则兜底**」这条降级链。
- **SSE 流式推送 + 事件类型**（`connected` / `log` / `progress` / `scores_update`）——**分析类长任务的进度协议**，比轮询体验好；lquant 的 `server/` 有 WebSocket，可对照补齐**分阶段进度事件**。
- **智能缓存**「减少 API 调用」+ 线程池批量分析。

**3) 失效与坑**
- **「AI 分析」在金融场景的价值不可验证**：README 自述「demo 站点因家里云 openwrt 不堪重负而挂」「3.1 出现大量 bug，回退 2.6」——**质量不稳定**。
- 依赖多个第三方 AI key → **成本与政策风险**。
- **不适合作为 lquant 的功能对标**（lquant 是数据/因子平台，不是 AI 荐股工具）。

**4) 对 lquant 的建议**
- **只借「25 项财务指标清单」做覆盖度审计**，以及 **「LLM 失败 → 规则兜底」** 的降级模式。
- **明确不做**「AI 给买卖建议」这一产品形态（合规与可信度双风险）。

---

### 3.4 gushitong 类看板

**1) 事实核查**
- 百度**股市通（gushitong.baidu.com）本身是闭源商业产品**，无对应开源仓库。
- GitHub 搜索「gushitong」仅命中一个 **0★ 的 `gushitong/gushitong.github.io`（2022-09-18）**，**非同类项目**。
- **结论：不存在有影响力的「股市通类」开源看板。** 真正活跃的同类是 **A 股短线复盘 / 情绪看板**方向（见 §3.6 与 §4）。

**2) 对 lquant 的建议**
- 不要把「gushitong 类看板」列为对标对象；**对标应转向 §3.6 的短线复盘看板**（那才是 A 股看板的活跃生态）。

---

### 3.5 OpenBB Terminal 作为看板形态

- **OpenBBTerminal 仓库已 301 重定向**至 `openbq-org/OpenBB`；**开源 Terminal 形态事实上已终止**，商业重心转向 **Workspace / Pro**。
- 相关 Pro 组件仓库 `openbb-platform-pro-backend` 已于 **2026-08-24 最后推送后归档**。
- **可借鉴的只有「插件化工作区组织方式」**：把数据集/图表/表格组织成可配置的 widget 布局，用户按需组合（**配置驱动看板**，与 §3.2 的「配置字典模板」是同一思想）。
- **对 lquant 的建议**：看板采用**插件/widget 化布局 + 元数据注册**，让新增情绪指标只是「注册一个 widget」，而不是改前端路由。

---

### 3.6 短线复盘看板：vibe-astock（本次看板方向的最佳对标）

**1) 定位 + 活跃度 + 维护状态**
- [simonlin1212/vibe-astock](https://github.com/simonlin1212/vibe-astock)：**668★**，最后推送 **2026-10-07**（本调研中**最新的看板项目**），版本 **v1.1.3**，Apache-2.0，Python 3.12 + React 19。
- 一句话定位（README 原文）：「A 股短线复盘看板：**涨停池·连板梯队·龙虎榜·板块资金一屏看完**，赚钱效应/晋级率/梯队断层/情绪周期等**派生指标纯计算直出（不经过 AI）**，AI 只把数据串成能读的盘面研判。」
- **关键设计哲学（原文）**：*「数据、程序计算、人工记录和 AI 解读**分别呈现**。行情与统计**不依赖 AI 生成**；AI 用于解释材料。」* 以及 *「情绪分档是**分析框架，不是客观事实**」*。
  → **这正是 lquant 需要的立场**：计算归计算，LLM 归解释。

**2) 可借鉴的具体设计（README 实测，价值极高）**
- **七个工作模块**（看板信息架构）：首页 / 盯盘 / 复盘 / 资讯雷达 / 个股研究 / 回测 / 我的股票。
- **盯盘**：
  - **盘面数据**：指数、**市场广度**、资金与板块读数、自选池行情；并明确「**不同来源的市场范围、日期和单位要分别阅读**」——**显式的单位/口径提示**。
  - **实时动态**：**从行情快照归集「急拉急跌、封板开板」事件**；并诚实声明 *「快照轮询并非逐笔成交数据，不能保证捕获所有盘中变化」*。
  - **昨日梯队**：固定前一交易日涨停样本，可选**全部 / 二板及以上 / 三板及以上**；**今天回落或行情缺失的成员仍保留**（便于观察延续/断板/覆盖缺口）。
- **复盘**：围绕**情绪、资金、题材、龙虎榜、龙头跟踪**五个分项，再汇总依据与待验证条件。
- **工程纪律（可借鉴性最强的部分）**：
  - *「各路取数显示当前类别，**单路等待上限为 90 秒**，并受整场剩余时间约束；**超时或取消会清理对应进程**」* → **与 lquant 的子进程看门狗同理，且给了具体数值（90 s）与「清理进程」的收尾要求**。
  - *「生成失败**保留旧稿**，同日重新生成保存版本，补做较早日期**不会把最新报告倒退**」* → **幂等 + 版本不倒退**。
  - *「刷新后继续查看任务状态」* → 任务状态持久化。
  - *「旧稿保留原样，不因升级自动获得新的校验结论」* → **历史不可篡改**。
  - *「数据源最多返回 500 条时会标明**部分覆盖**」* → **截断必须显式标注**（`partial coverage` 语义）。
  - *「接口失败会**明确提示，不等同于没有解禁**」* → **失败 ≠ 空值**，这是最关键的一条静默错误防线。
- **「盘中核验」与「实时动态」的职责分离**：前者答「此前条件是否出现」，后者答「刚才发生了什么」；并明确 *「错过的历史盘中时点**不能靠当前行情补造**」*——**与 lquant `market/schema.py` 注释「当天不采就永久丢失」完全同频**。
- **免责与证据纪律**：截图标注拍摄日期与「不代表实时行情或收益案例」；README 首屏直接写明作者在找工作（**治理风险信号**，见下）。

**3) 失效与坑**
- **治理风险（最重要的判断）**：作者在第一屏公开「**Open to Opportunities｜深圳·香港·远程**」并留邮箱——**这是一个求职中的个人项目**，长期维护性存疑。
- **AI 依赖**：虽然强调「计算不经过 AI」，但复盘报告依赖 Codex/Claude/DeepSeek 等外部订阅与 API。
- **快照轮询的固有上限**：自认无法保证捕获所有盘中变化 → **不适合作为高频事件的唯一来源**。
- **未核实**：数据来源具体站点（是否东财/同花顺）、是否有历史回补能力、许可细节。

**4) 对 lquant 的建议（看板方向最高优先级）**
1. **照搬它的四条工程纪律**（成本极低、收益极高）：
   - **单路取数超时上限（90 s 量级）+ 超时清理进程**；
   - **失败 ≠ 空值**：接口失败必须显式标记，不能写成 null/0；
   - **部分覆盖要标注**（数据源截断 500 条等）；
   - **历史稿不可篡改 + 幂等重生成 + 版本不倒退**。
2. **照搬它的看板信息架构**：盯盘（盘面/实时动态/昨日梯队）/ 复盘（情绪·资金·题材·龙虎榜·龙头）/ 资讯雷达 / 个股研究。
3. **「昨日梯队保留已回落成员」**这一条尤其重要：lquant 的 `limit_up_pool` 若只存当天涨停股，就**无法观察断板与覆盖缺口**；应在派生层保留**昨日梯队成员及其今日状态**。
4. **采用它的立场边界**：**所有情绪指标由程序计算并标注口径，AI 只做解释**；情绪分档明确声明为「分析框架而非客观事实」。

---

### 3.7 TradingView lightweight-charts 生态

**1) 定位 + 活跃度 + 维护状态**
- [tradingview/lightweight-charts](https://github.com/tradingview/lightweight-charts)：**17,515★** / 2,646 fork / `open_issues=128` / Apache-2.0 / 最后推送 **2026-10-08**；文档当前版本 **5.2**。**极活跃**。
- 定位：HTML5 canvas 高性能金融图表（K 线/面积/柱状/直方/基线）。

**2) 可借鉴的具体设计 / 必须知道的破坏性变更**
- **v4 → v5 的统一 series 创建 API**（证据：[From v4 to v5](https://tradingview.github.io/lightweight-charts/docs/migrations/from-v4-to-v5)）：
  | v4 | v5 |
  |---|---|
  | `chart.addLineSeries(options)` | `chart.addSeries(LineSeries, options)` |
  | `chart.addAreaSeries(options)` | `chart.addSeries(AreaSeries, options)` |
  | `chart.addBarSeries(options)` | `chart.addSeries(BarSeries, options)` |
  | `chart.addBaselineSeries(options)` | `chart.addSeries(BaselineSeries, options)` |
  | `chart.addCandlestickSeries(options)` | `chart.addSeries(CandlestickSeries, options)` |
  | `chart.addHistogramSeries(options)` | `chart.addSeries(HistogramSeries, options)` |
  → 迁移时必须从包里**显式 import `LineSeries`/`CandlestickSeries` 等**；ESM 下 `LightweightCharts.LineSeries` 亦可。
- v5 的破坏性变更讨论见 [issue #1791](https://github.com/tradingview/lightweight-charts/issues/1791)（「⚠️ Upgrading to v5.0.0? Read this first! Important Breaking Changes」）。
- 性能特征：canvas 渲染、支持大量数据点，但**大数据量需要业务侧做窗口化/分段加载**。

**3) 失效与坑（对 A 股尤其重要）**
- **无内置复权**：图表只画你给的数据 → **复权口径必须在后端统一**（呼应 §2.4）。
- **无内置 A 股规则**：**涨停/跌停颜色、停牌跳空、除权缺口、集合竞价**都需自行处理；A 股「红涨绿跌」与欧美相反，需配置。
- **时间轴类型限制**：日线用 business day / 时间戳需口径一致；**停牌日缺失会造成 K 线视觉跳空**——需业务侧决定「补 null 还是断线」。
- **v5 迁移成本真实存在**：所有 `addXxxSeries` 调用必须改；React 封装库普遍滞后于 v5。
- **未核实**：`charting-library`（功能更全但需授权/水印）的具体条款。

**4) 对 lquant 的建议**
- **采用 lightweight-charts v5**（活跃、Apache-2.0、体积小），但**一开始就按 v5 API 写**，避免踩迁移。
- **复权与涨跌停规则全部在后端处理**：前端只接收「已复权 + 已标注涨跌停/停牌」的序列；`market/schema.py` 之外补一个**「图表专用序列」契约**（含 null 语义与停牌标记）。
- **配色遵循 A 股习惯**（红涨绿跌），并把「涨停/跌停/一字板」做成可配置标记，供看板复用（与 §4.1 的形态标签对齐）。
- **不要自己做 Python 服务端渲染**（无官方方案，社区方案不成熟）；Next.js 客户端渲染即可。

---

## 4. A 股情绪指标的开源实现

### 4.1 涨停 / 炸板 / 连板高度

**最佳口径范本**：[quantskills/skill-b6-limitup-pool](https://github.com/quantskills/skill-b6-limitup-pool)（8★，最后推送 2026-09-07，master 分支，GPL-3.0-only，社区项目）。
> 声明：这是 **Community Project**（未经 QuantSkills 官方审核/背书，仅供研究与教育）。其数据源为 **PandaData**（非免费源）。**只借口径，不借实现与数据源。**

**可借鉴的具体口径（README 实测）**
- **动态状态机（每只票 vs 昨日涨停池）**——6 态，直接可作为 lquant 的枚举设计：
  ```
  未涨停(none) → 新晋首板(first) | 摸板未遂(miss)
  first → 晋级(up, 板数+1) | 炸板出局(out)
  up    → 继续连板(up) | 维持(hold, 仍涨停但板数未升) | 断板(out)
  ```
- **特殊形态标签，带优先级（高→低）**：
  `地天板 > 天地板 > 一字板 > 秒板 > 炸板未封 > 烂板(炸≥3 或 尾盘回封) > 反复板 > 实封`
  → lquant 的 `limit_up_pool` 目前有 `open_count` / `limit_up_type`，**缺「优先级排序」与「回封时间」**。
- **封板指标**：首封时间（`first_seal_time`）、末封时间（`final_seal_time`）、炸板次数（`blow_up_count`）。
- **情绪面（每日 1 行）字段**：
  - `n_limit_up`（涨停家数）
  - `market_blow_rate`（**炸板率**）
  - `max_height`（**最高连板高度**）
  - `promote_rate_by_tier`（**分层晋级率**，如首板→2 板）
  - `prev_limitup_premium`（**赚钱效应**：昨日涨停股今日表现）
- **落盘结构**：主键 `(trade_date, build_id, target_id, result_type)`，两类行 `limit_up_pool` / `limitup_sentiment`；**追加合并，不覆盖历史**。
- **降级策略与 provenance（最亮眼的一条）**：
  > 「分钟线拉不到（流量超限/服务异常）会**自动降级**到日线代理（炸板次数用日内回撤估、回封时间留空），主流程不中断」
  且**在数据里留痕**：`seal_metric_source = minute | daily_proxy`。
  → **降级必须显式标注精度来源**，这正是 lquant 热通路需要但不具备的纪律。
- **题材（概念）用 PIT 过滤**：`get_concept_*` 做 **PIT 过滤**——避免用**今天的**概念成分回看历史（**前瞻偏差**）。
- **输入范围**：全 A · **回看 45 个交易日**（足够覆盖最长连板 + 昨日梯队）。

**其他同类**：
- [trading4/dfcf_eastmoney](https://github.com/trading4/dfcf_eastmoney)（6★，**最后推送 2024-06-21，停滞**）：涨停统计/东财短线情绪/打板连板数据龙头——仅作**字段命名参考**。
- [guoyaohua/limit-up-sniper](https://github.com/guoyaohua/limit-up-sniper)（12★，2026-07-16）：**首板涨停研究**，支持实时 Tick、QMT/XTQuant、**影子验证**与盘后复盘。
- [stock-programmer/limit-up-review](https://github.com/stock-programmer/limit-up-review)（79★，2025-08-10）：涨停复盘 + PDF/财报/公告解析。
- [simonlin1212/vibe-astock](https://github.com/simonlin1212/vibe-astock)：**昨日梯队 + 连板梯队**看板（见 §3.6）。

**坑与静默错误（必须防）**
1. **未处理板块差异的涨跌幅阈值**：主板 10%、**创业板/科创板 20%**、**ST 5%**、**北交所 30%**、新股上市首几日无涨跌幅限制。**用 `pct_chg >= 9.8%` 判定涨停会漏掉 20cm、并把北交所 30cm 误判**。
2. **连板数递推未处理停牌**：停牌日不计入，但**复牌后是否延续**需定义；一字板/秒板需与普通涨停区分。
3. **炸板定义的歧义**：盘中触及涨停价即算「摸板」，**收盘是否封住**决定「炸板」；`blow_up_count` 的计数口径（次数 vs 是否）各家不同。
4. **一字板无法从日线识别**：`open == high == low == close == 涨停价` 才是一字板；**日线缺失时用代理估会失真**（这正是 `seal_metric_source` 存在的意义）。
5. **概念成分的时间点**：用当前概念成分回看历史 = **前瞻偏差**。

**对 lquant 的建议**
- **`market/schema.py` 的 `limit_up_pool` 补齐字段**：`limit_up_streak`（板数）、`first_seal_time` / `final_seal_time`、`special_pattern`（带优先级的枚举）、`lead_concept`、`pool_status`（状态机 6 态）、**`seal_metric_source`**（minute / daily_proxy）。
- **新增 `limitup_sentiment` 日表**：`n_limit_up` / `market_blow_rate` / `max_height` / `promote_rate_by_tier` / `prev_limitup_premium`。
- **涨停判定必须板块感知**：把「涨跌幅上限」做成按 `board`（主板/创业板/科创板/北交所/ST）的规则表——**lquant 已有 `config/规则表` 与 `backtest/rules/`，应让看板复用同一张表**，避免回测与看板两套阈值。
- **降级必须留痕**：引入 `seal_metric_source` 字段，禁止静默降精度。
- **概念成分 PIT 化**：`index_cons.py` 应对概念/行业成分做**按日期生效**，而不是覆盖式更新。

---

### 4.2 龙虎榜

**最佳口径范本**：[quantskills/skill-b7-lhb-monitor](https://github.com/quantskills/skill-b7-lhb-monitor)（12★，GPL-3.0-only，社区项目）。
> 声明原文：*「**游资别名为民间观测映射、非券商官方身份、会迁移，不构成对任何个人/机构的指认**」* —— **这套合规边界声明值得 lquant 照抄**。

**可借鉴的具体口径（README 实测）**
- **席位标签匹配优先级链（核心逻辑）**：
  ```
  营业部名
   ├─ 含「沪股通专用 / 深股通专用」 → 北向
   ├─ 含「机构专用」                → 机构
   ├─ 外部覆盖 B7_SEAT_OVERRIDE 命中 → 按覆盖标注
   ├─ 爬取精确映射（营业部全称命中）  → 游资/量化 + 本尊 alias + 帮派 + 置信度 A/B/C
   ├─ 子串种子库命中                 → 游资/量化 + 帮派
   ├─ 含「营业部 / 分公司」           → 营业部（具名席位，游资盘计入）
   └─ 其他                          → 普通（极少）
  ```
  - 实测覆盖率 **≈99%**；某日「245 个席位里仅 2 个落『普通』」。
- **关键定义**：**游资盘净买 `hotmoney_net` = 游资 + 营业部**（**非机构 / 非北向 / 非量化**的活跃资金）；**「量化」标签（三板组/量化打板/量化基金、外资通道高盛/摩根/中金）不计入游资盘**。
- **多上榜原因拆分**：一只票一天可能有**多条上榜原因**（如同时「涨幅偏离 7%」+「换手 20%」），**每条原因对应各自一组买入/卖出营业部** → 个股详情页**按原因分别列出**。
- **数据落地**：`database.parquet`，含**个股行 + 汇总行**。
- **评分权重（默认）**：机构 0.35 / 游资 0.35 / 净买 0.20 / 席位数 0.10。
- **时序约束（重要运维知识）**：**龙虎榜傍晚才公布，约 19:30 后**才能跑——**lquant 的调度必须按此排期**，不能沿用收盘时刻。
- **席位库是「两层 + 可维护」资产**：爬取 CSV → `build_seat_map.py` → `seat_yyb_map.json`（317 营业部 → 本尊 + 置信度）；并有 `B7_SEAT_OVERRIDE` 临时覆盖机制。
- **输出形态**：机构合集 / 营业部合集 / 个股详情页 / 区间统计 / 交互式 HTML 看板（tab·搜索·筛选·排序·展开）。

**其他实现**：
- [aifinlab/FinClaw `akshare-lhb-detail` skill](https://github.com/aifinlab/FinClaw) —— akshare 龙虎榜明细的封装。
- akshare 提供龙虎榜接口（数据来自东财/交易所公开数据）。
- 交易所口径：**沪/深股通专用席位**是识别北向的传统方法——**注意**：北向实时数据停披露后，**龙虎榜里的「沪股通专用/深股通专用」席位仍然存在**，这是**目前少有的仍可获得北向行为线索的渠道**。

**坑与静默错误**
1. **营业部更名 / 迁移**：游资别名会随时间变化，**标签库必须版本化并可回溯**（否则历史标签被静默改写 → 回测不可复现）。
2. **「机构专用」不等于全是机构**：存在通道资金，**不能作为纯机构代理**。
3. **多原因导致金额重复计数**：若把同一票的多条原因直接求和，会**重复计金额**；必须按「原因 × 席位」建模。
4. **单位与符号**：净买额 = 买入 − 卖出，**部分源返回的是「买卖金额」需自行相减**；缺失值填 0 会把「未上榜」与「净额恰好为 0」混淆。

**对 lquant 的建议**
- **新建席位语义层**（而非只存原始席位字符串）：`seat_tag`（北向/机构/游资/量化/营业部/普通）、`seat_alias`、`confidence`、`tag_version`。
- **引入 `hotmoney_net`** 作为一等派生字段，并**显式排除量化**。
- **龙虎榜表按「上榜原因」拆行**，避免金额重复计数；`dragon_tiger.py` 需支持一票多原因。
- **席位映射表版本化入库**（`tag_version` + 生效日期），保证历史可复现。
- **调度时间改为 19:30 之后**（与涨停池的收盘时刻区分开），并把「当日未公布」与「无数据」区分开。
- **把「沪股通/深股通专用席位」作为北向的替代观测**写进文档（这是 §4.3 政策冲击后的可行退路）。
- 抄它的**合规声明**（别名非官方身份、不指认个人）。

---

### 4.3 北向资金 ★ 本节是本次调研中最重要的「已失效」结论

**政策事实（权威来源：[新华财经 2024-08-19](https://www.cnfin.com/yw-lb/detail/20240819/4090757_1.html)，转引每日经济新闻；旁证 [东方财富 2024-08-26](https://finance.eastmoney.com/a/202408263165166650.html)「『风向标』彻底看不到了」）**

时间线：
- **2024-04-12**：沪深港交易所宣布同步调整沪深港通交易信息披露机制，**分两阶段**。
- **2024-05-13**：**第一阶段**完成——**取消北向资金盘中实时交易信息**（当天是「北向资金交易信息取消实时披露的第一个交易日」）。
- **2024-07-26**：上交所、深交所分别发布公告，明确 **2024-08-19** 起执行。
- **2024-08-19**：**第二阶段生效**。

调整后**仍然公布**的内容：
- **沪股通**：每个交易日**收市后**公布①当日**成交总额及总成交笔数**、②**ETF 成交总额**、③**前十大成交活跃证券名单及其成交总额**；并按**月度、年度**公布汇总；**每季度第五个交易日**公布上季度末**单只证券的沪股通投资者合计持有数量**。
- **深股通**：同上（成交总额及总笔数、ETF 交易总额、**交易金额前十证券名单及其交易总额**、月度/年度汇总、季度末单只持股）。
- **港股通**：盘中当额度余额 ≥30% 时只显示「额度充足」，<30% 才实时公布余额；收市后公布买入/卖出/成交金额及笔数、ETF 总额、**成交金额前十证券名单及其买卖金额**；收市后公布单只证券的港股通投资者合计持有数量。

**已经永久消失的数据**：
- ❌ **北向资金盘中实时净流入/净买入**（自 2024-05-13）；
- ❌ **每日北向资金净买入总额**；
- ❌ 由上述派生的一切「北向实时情绪指标」。

**对开源项目的影响（判定）**
- 任何仍以「北向资金实时净流入」为因子的模块——**要么已失效，要么在伪造数据**。lquant 的看板与 `market/` 必须**立即审计是否存在此类模块**。
- **可行的替代观测**（本次调研的结论）：
  1. **每日收市后的「前十大成交活跃证券名单 + 成交总额」**（可构造「北向活跃度」而非「净流入」）；
  2. **季度末单只证券持股数量**（可构造**低频持仓变动**，适合中低频因子）；
  3. **龙虎榜中的「沪股通专用/深股通专用」席位**（见 §4.2——仍可观测北向在**异动个股**上的行为）。

**政策解读（供因子设计参考）**：文章受访私募观点认为，不再披露实时数据的意图是**降低跟风资金的趋同交易**（「北向卖，跟风者卖」放大波动）；并指出「北向资金作为市场风向标其实并不完全合适」，因为存在**借道沪深港通的「假北向」短线资金**，与真正的海外长线资金（多托管于外资银行）行为不同。

**对 lquant 的建议**
1. **审计现有北向模块**：若 `market/collectors/` 或因子库中有「北向净流入」，**立即标记失效或删除**。
2. **重建为新口径**：把 `Capability.NORTHBOUND`（或等价物）重定义为
   - `northbound_top10_turnover`（每日前十大活跃成交股 + 成交额）
   - `northbound_holdings_quarterly`（季度末单只持股）
   - `northbound_seat_lhb`（龙虎榜北向席位）
3. **在文档与 UI 中明确标注口径变更日期（2024-05-13 / 2024-08-19）**，禁止用新口径数据冒充历史「净流入」序列。
4. **把「数据源政策性消失」列入风险清单**：这类失效**不是 bug，无法修复**，只能换口径——因此 lquant 的 `capability.py` 应该有「**该能力在日期 X 后不可用**」的表达能力，而不仅是「该源是否支持」。

---

### 4.4 资金流（主力/超大单/大单/中单/小单）

**来源与口径**：主流来自**东方财富资金流**（`push2.eastmoney.com` / `push2his.eastmoney.com`）。lquant 的 `market/schema.py::money_flow` 已建模为：
`main_net_inflow` / `main_net_ratio` / `super_large_net` / `large_net` / `medium_net` / `small_net`。

**东财口径（通行理解，具体公式以实测对拍为准，部分**未核实**）**：
- 按**单笔成交金额**分档：**超大单 / 大单 / 中单 / 小单**；
- **主力净流入 = 超大单净额 + 大单净额**（**主力 = 超大单 + 大单**，各档均按**主动性买 − 主动性卖**计算）；
- `main_net_ratio` = 主力净流入 / 成交额（百分比）。

**可借鉴**：README 与实现较好的项目（如 [adata](https://github.com/1nchaos/adata)、[efinance](https://github.com/Micro-sheep/efinance)）都**同时保留分档净额与净占比**，便于跨市值横向比较（**绝对额不可比，占比可比**）——lquant 已具备 `main_net_ratio`。

**坑与静默错误（重点）**
1. **单位混用**：东财在不同接口返回**元 / 万元**甚至**亿元**；lquant 必须在写入时**统一到「元」并记录单位标签**。
2. **正负号**：净流入为负时**部分源用 0 表示**或直接省略字段 → 「0」与「缺失」混淆。
3. **分档之和 ≠ 成交额**：因为分档基于**主动买卖单**，中性单不计入，**不能用总和校验为成交额**（把它当校验会导致无谓告警）。
4. **停牌/无成交日**：净额应为 **null 而非 0**。
5. **当日数据不回溯**：lquant `market/schema.py` 注释已明确「当天不采就永久丢失」——资金流是**必须当天采**的热数据，**失败必须告警**。
6. **主力口径的行业分歧**：部分平台把「主力」定义为「大单+超大单」，也有用「≥某金额的全部成交」；**跨源对比前必须先对齐口径**。

**对 lquant 的建议**
- **单位统一 + 单位标签**：`money_flow` 所有金额字段统一为「元」，并在 `schema.py` 或契约中显式声明；禁止混用万元。
- **null 语义严格**：停牌/无数据写 null，**不写 0**；用 `quality/` 断言「0 值比例异常偏高」为 finding。
- **保留 `main_net_ratio`**（占比）作为跨标的可比量；不要把绝对净额直接喂给横截面模型。
- **热通路优先级**：资金流与涨停池同级（**当天不采即永久丢失**），调度与告警等级提到最高。
- **对拍**：与 akshare/efinance 的同口径接口定期对拍（lquant `crosscheck.py` 已是正确基础设施）。

---

### 4.5 股吧 / 论坛情绪爬虫

**最佳范本**：[hyan1985/MarketMonitoring](https://github.com/hyan1985/MarketMonitoring)（13★，MIT，Python 3.11+，**GitHub Actions 每工作日自动跑**，在线看板 `hyan1985.github.io/MarketMonitoring`）。
> 定位（README 原文）：「多源爬取 A 股论坛帖子，基于**词频词典**做情绪量化，并结合 **TuShare 市场数据**估算大盘综合风险值，自动生成可视化看板。」

**可借鉴的具体设计（README 实测）**
- **多源爬虫**：**东方财富股吧、雪球、同花顺**（上证指数相关讨论）。
- **情绪打分**：**基于多头/空头/否定词词典的分词加权打分，范围 `[-1, 1]`** —— **极简、完全可解释、可复现**，且**不依赖模型**（对比：LLM 打分不可复现）。
- **风险模型（双轨 × 五维）**：
  - 综合风险分 `0–100` = **结构拥挤（0–50）** + **破位风险（0–50）**；
  - **结构拥挤**维度：**资金集中度**（前 5% 个股成交额 / 全市场）、**融资余额占比**、**行业集中度**（通信+电子成交占比）、**论坛情绪亢奋**；
  - **破位风险**维度：**恐慌扩散**（跌幅 ≥5% 个股占比）、**宽度骤降**、**隐性破位**、**炸板跌停潮**；
  - **主升趋势熔断**：均线多头且守住 MA10、未大跌时，**破位分 <20 则总分上限 55**；
  - **历史分位**：成交集中度给**近一年历史极值 + 历史分位**（README 用「PE 式分位条」呈现）；
  - **硬触发与领先信号**：量价背离（高位 + 量能/参与面走弱，**持续 2 日确认**）、**6 类顶部派发形态**（缩量背离、巨量滞涨、衰竭缺口、融资流出等）、**隐性破位**（指数收红/收平但宽度崩塌）。
  - 依赖 `limit_list_d` 做**涨停/炸板/跌停统计**（赚钱效应转弱时加分）。
- **看板布局**（可作 lquant 情绪页的模板）：情绪仪表盘 + 帖子分类占比 / 情绪日线趋势 / 活跃多空词频（左右并排）/ 横版风险卡（总分·等级·双轨分·炸板率·集中度·五维进度条）/ 变动信号 · 硬触发 · 成交集中度历史分位 / 情绪模拟器 / 帖子明细表。
- **工程**：**GitHub Actions 每个工作日自动跑** —— 零服务器运维的调度范式。

**其他实现**
- [aifinlab/FinClaw `a-share-nlp-sentiment` skill](https://github.com/aifinlab/FinClaw/blob/main/skills/a-share-nlp-sentiment/SKILL.md) —— A 股 NLP 情绪 skill。
- InStock 的**「人气指标」**（见 §3.1）走的是**结构化路线**（股吧人气排名/变化/连涨连跌/粉丝结构），**比帖子情绪更稳定**。

**坑与静默错误**
1. **词典法的边界**：反讽、黑话、拼音缩写（如「yyds」「nb」）、同名歧义 → **误判率高**；需**人工审阅 + 词表版本化**。
2. **重复/机器人帖**：股吧充斥灌水与营销帖，**不去重会放大噪声**。
3. **情绪与收益的关系不稳定**：「情绪亢奋」既可能是顶部也可能是主升，**不能单独作为买卖信号**（MarketMonitoring 自己用「主升趋势熔断」来缓解）。
4. **爬虫易失效**：股吧/雪球/同花顺的页面结构与风控经常变；**雪球的风控尤其强**。
5. **时间对齐**：帖子时间需对齐到**交易日与盘前/盘中/盘后**，否则「当日情绪」含义混淆。
6. **未核实**：该项目当前是否仍能成功爬到三个源（GitHub Actions 徽章显示每日运行，但**抓取结果质量未验证**）。

**对 lquant 的建议**
- **优先走 InStock 的「结构化人气指标」路线**（人气排名、排名变化、连涨连跌、粉丝结构）——**比 NLP 情绪更抗噪、更可复现**。
- **若做论坛情绪，采用 MarketMonitoring 的词典法 + 版本化词表**（`[-1, 1]` 加权和），**不要一开始就上模型**；把「词表版本」入库以保证可复现。
- **必须做去重与机器人过滤**（按用户/内容指纹），并**把过滤前后计数都保留**以便追溯。
- **情绪指标只作解释与分档，不作交易信号**（与 §3.6 的立场一致）；MarketMonitoring 的「历史分位 + 分档」表达方式值得照搬（**分位数比绝对阈值稳健**）。
- **调度用 GitHub Actions 式的外部调度或 lquant 自己的 scheduler**，但要处理「非交易日跳过」与失败告警。

---

## 5. 反爬与工程实践

### 5.1 限流与封 IP：公开经验与具体阈值

**一手证据：CNEquity 故障排查文档**（[原文](https://github.com/rootSunc/CNEquity/blob/34005e9ec81552ad1689fea6519bd4b37b52f998/docs/operations/troubleshooting.md)）

| 现象 | 原因（原文） | 处理（原文） |
|---|---|---|
| `baostock login failed: 黑名单用户`（**错误码 `10001011`**） | **IP 被免费 API 封禁：日请求 > 5 万、或并发连接、或扫太快** | **停扫**；换出口或去 QQ 群求助解封。解封后用默认限速 resume，**勿并发** |
| 东财 **429 / Empty reply / 连接被掐** | 请求过密**或海外出口** | 保持 **`min_interval_seconds ≥ 1.0`**；**大陆出口**或 `proxy` |
| cninfo / pboc 间歇失败 | 同源风控 / 站点抖动 | 按页/按调用 `rate_limit`；**社融主写入按年取全量，任一年失败即阻止本次宏观写入**，待下次完整重试 |

**两条可直接落地的原则（原文）**
1. **「时间可以等，封禁成本远高于多等一天」** —— 勿为加速关掉 `min_interval` 或开多进程打同一免费源。
2. **「rate-limit alone 不能阻止 N 个会话同时 `login()`」** → 加 **`RunLock("baostock")` 单飞锁**；并发任务（`valuation_2001` / `float_mv` 扫盘）**直接跳过并留 warning**（`baostock_single_flight`）。

**旁证：akshare issue #6061**（[链接](https://github.com/akfamily/akshare/issues/6061)，2025-04-15）
> 标题即结论：「**疑似使用了异步高并发导致东财大量封 IP**」。与 lquant `ratelimit.py` 注释「东财封 IP 是头号风险」完全一致，且说明**高并发是比高频更危险的触发因素**。

**旁证：东财数据频繁访问答疑**（[腾讯云社区文章](https://cloud.tencent.cn/developer/article/2659302)）——社区层面的统一答疑，说明封禁是普遍现象（**具体内容未逐段核实**）。

**旁证：pythonstock/stock 的防封缓存策略**（[README](https://github.com/pythonstock/stock)）
> 「股票数据接口防止被封，**按天进行数据缓存，储存最近 3 天数据，每天定时清除**，同时使用 read_pickle/to_pickle 的 **gzip 压缩模式**存储。」

**旁证：接口级补丁生态**
- [HelloYie/akshare-proxy-patch](https://github.com/HelloYie/akshare-proxy-patch)：「针对 akshare 和 efinance 的猴补丁插件，解决 `stock_zh_a_spot_em`、`stock_zh_a_hist`、`get_realtime_quotes` 等接口报错问题」→ **上游接口失效是常态，社区只能靠 monkey patch 续命**。

**具体接口现状（部分未核实）**
| 接口/站点 | 现状 | 备注 |
|---|---|---|
| `push2.eastmoney.com` / `push2his.eastmoney.com` | 可用但有风控；**429 / 掐连接**；海外出口更易被拦 | 保持 ≥1 s 间隔、大陆出口 |
| `EastMoney datacenter`（`RPT_*` 报表） | 可用但**列名会改**，改后返回 **`code=9501` 列不存在**（**fail-loud**） | 见 §5.3 |
| `hq.sinajs.cn`（新浪实时行情） | **2022 年后要求 `Referer: https://finance.sina.com.cn`**，缺失 403；**批量查询有上限**（**具体上限未核实**） | 需自带 Referer |
| `d.10jqka.com.cn`（同花顺） | 风控较强，需 Cookie/UA，**当前有效性未核实** | 谨慎使用 |
| 百度股市通 | **闭源商业产品**，无公开接口（见 §3.4） | 不可依赖 |

**对 lquant 的建议（可直接落地）**
1. **限流之外补「源级单飞锁」**：`baostock`（以及任何免费源）同一时刻只允许一个会话；这是 lquant 当前**明确缺失**的一环。
2. **并发度显式化并默认保守**：不要进程池打同一免费源；`min_interval ≥ 1.0 s` 作为默认值写进配置（**lquant 的令牌桶应默认按此设定，而非按「尽量快」**）。
3. **日请求预算**：为每个源设置**每日请求上限计数器**（baostock 阈值 5 万是已知红线，应留足余量，例如自设 2 万告警/3 万熔断）。
4. **出口策略**：默认**大陆出口**；配置化 `proxy`；把「海外出口」作为已知风险标注。
5. **短 TTL 缓存**：热通路当天重试走本地缓存（借鉴 pythonstock 的「3 天缓存 + 定时清除」）。
6. **封禁可观测**：把 `10001011`（黑名单）与 429/掐连接做成**独立告警类别**（区别于普通网络错误），因为处置方式完全不同（**停扫 vs 重试**）。
7. **补丁层不进主仓**：像 akshare-proxy-patch 那样 monkey patch 上游是**技术债**；lquant 应通过**自己的适配器隔离**上游行为，而不是打补丁。

---

### 5.2 字段漂移（schema change）检测：可落地的工程模式

**最有价值的一手实践：CNEquity 的「契约清单 + 直播探针」**（[故障排查原文](https://github.com/rootSunc/CNEquity/blob/34005e9ec81552ad1689fea6519bd4b37b52f998/docs/operations/troubleshooting.md)）

**症状与根因**
> `EastMoney datacenter RPT_… rejected schema: XXX列不存在 (code=9501)`
> 原因：**东财改了报表列名；旧列整报拒绝**。
> 处理：在对应 adapter 的 `_COLUMNS` **换成新名；契约清单自动跟随**。

**两层测试（可直接照搬的形态）**
```bash
# 离线：契约清单完整 + 9501 文案
uv run pytest tests/unit/test_datacenter_contracts.py -q
# 外网：每个 required 报表 pageSize=1，键 ⊇ 契约列
uv run pytest -m network tests/unit/test_datacenter_live_contracts.py -q
```
- **契约清单入口**：`src/cnequity/adapters/eastmoney/datacenter_contracts.py`
- **直播探针**：对每个 `required` 报表请求 **`pageSize=1`**，断言**返回键集合 ⊇ 契约列** → **极低成本、每天可跑**。
- **报表退役管理**：已退役报表（如 `RPT_ECONOMICCALENDAR`）标 **`required=False`**，**不进直播探针** → 避免无谓告警。
- **fail-loud 而非静默空表**：原文「日更整组失败、错误带 report 名（**fail-loud，不静默空表**）」→ **关键设计**：宁可整组失败，也不要写下空表。

**Schema 演进纪律（同一项目）**
- **curated 列只增不改**；
- **破坏性变更 bump `dataset_schema_version`**。

**通用工具与实践（业界）**
- [Microsoft Learn: Detect and Manage Schema Drift](https://learn.microsoft.com/en-us/training/modules/implement-manage-data-quality-constraints-unity-catalog/4-detect-manage-schema-drift) —— 官方教程，讲数据质量约束与 schema 漂移检测（含约束与监控思路）。
- [Data Validation in Production ML: Preventing Silent Failures with Pandera, GE, DBT and Deepchecks](https://sqlbits.com/sessions/event2026/Data_Validation_in_Production_ML_Preventing_Silent_Failures_with_Pandera_GE_DBT_and_Deepchecks)（SQLBits 2026 议题）——**「silent failures」正是本报告的核心关切**。
- 工具谱系：**pandera**（DataFrame schema 声明式校验，pandas/polars 均可）、**Great Expectations / Soda**（期望套件 + 数据文档）、**dbt tests**（SQL 层断言）、**Deepchecks**（分布漂移）。
- **lquant 的取舍**：已有 `quality/` 断言与 `schema.py`，**不必引入重框架**；应实现**「契约清单 + 轻量探针」**这一最小形态（成本最低、覆盖面最大）。

**可落地的四类检测（建议 lquant 全做）**
| 类型 | 做法 | 能抓住的失效 |
|---|---|---|
| **结构契约** | 声明必需列集合；返回键 ⊇ 契约列 | 列改名/列缺失（东财 `9501`） |
| **顺序/数量** | 断言列数或显式按列名取值（**绝不按位置取值**） | 列序变化导致的**错位**（静默错误） |
| **类型/单位** | 类型断言 + **单位标签断言**（元/万元、手/股） | 单位混用（静默错误） |
| **值域/分布** | 值域断言 + 分布漂移（与近 N 日比较）+ **零值/null 比例** | 语义变化、缺失填充为 0 |

**对 lquant 的建议**
1. **为每个用到的外部接口建一份声明式契约文件**（列名 + 类型 + 单位 + 是否必需 + 退役标记），放在 `src/lquant/data/` 下，**与 Provider 同目录**。
2. **加两个测试**：离线契约测试（不需要网络）+ **线上轻量探针**（`pageSize=1` / 单标的，标 `network` marker 定期跑）。lquant 已有 `--strict-markers` 与 `slow/network` 语义的基础设施。
3. **绝不按位置取列**：所有解析按**列名**，并列名做白名单校验。
4. **fail-loud**：契约不满足 → **整批失败 + 带报表名告警**，**不写空表**（CNEquity 的明确立场）。
5. **schema 版本**：为 lquant 的湖内数据集引入 `dataset_schema_version`，**curated 列只增不改**，破坏性变更 bump 版本。
6. **单位是 schema 的一部分**：金额/量纲字段必须带单位声明（这是 lquant 目前最容易漏的一环）。

---

### 5.3 静默错误案例库（本报告汇总，供 lquant 自检）

| # | 静默错误 | 证据/来源 | lquant 自检点 |
|---|---|---|---|
| 1 | **复权口径混用**：同一 symbol 三套价格序列串味 | [BaoStock 复权因子简介 PDF](http://www.baostock.com/baostock/images/2/20/BaoStock%E5%A4%8D%E6%9D%83%E5%9B%A0%E5%AD%90%E7%AE%80%E4%BB%8B.pdf)、[CSDN 五大陷阱](https://blog.csdn.net/weixin_28235513/article/details/166963637) | `adjustflag`/`adjust` 是否进幂等键与 lineage |
| 2 | **前复权不可复现**：持久化 qfq 后随新除权整体漂移 | [CNEquity ADR-0004](https://github.com/rootSunc/CNEquity/blob/34005e9ec81552ad1689fea6519bd4b37b52f998/docs/adr/0004-store-hfq-derive-qfq-at-query.md) | 是否物化 qfq；anchor T 是否显式 |
| 3 | **缺因子静默填 1.0** | 同上（`strict_adj` 的对立面） | 是否存在 `factor=1.0` 兜底 |
| 4 | **volume 量纲/粒度不一致**：指数 vs 个股 vs 分钟 | [Ashare README](https://github.com/mpquant/Ashare) 实测样例（`303718677.0` vs `4541.53`） | 每源 `volume`/`amount` 是否带单位标签 |
| 5 | **涨停阈值未分板块**：漏 20cm/30cm、误判 ST | §4.1（板块规则） | 看板与回测是否共用同一张涨跌幅规则表 |
| 6 | **稀疏 tip**：`end=today` 只写完部分标的，水位却前移 | [CNEquity 故障排查](https://github.com/rootSunc/CNEquity/blob/34005e9ec81552ad1689fea6519bd4b37b52f998/docs/operations/troubleshooting.md) | 是否有覆盖率门禁与水位前移门禁 |
| 7 | **列名漂移致错位/整表失败** | 东财 `9501 列不存在`（同上）+ [akshare #6987](https://github.com/akfamily/akshare/issues/6987) | 是否按列名取值 + 契约探针 |
| 8 | **资金流单位（元/万元/亿元）与符号** | §4.4 | 是否统一到元 + 单位契约 |
| 9 | **缺失填 0 与真实 0 混淆** | §4.4 / §4.5 | null 语义是否严格；零值比例是否被断言 |
| 10 | **失败被当成空数据**（接口挂了 → 写 null/空表） | [vibe-astock](https://github.com/simonlin1212/vibe-astock) 明确「接口失败会明确提示，不等同于没有解禁」 | 失败 vs 空值是否可区分 |
| 11 | **数据截断未标注** | 同上「数据源最多返回 500 条时会标明部分覆盖」 | 分页截断是否显式标注 |
| 12 | **幸存者偏差**：用今天的名单回看历史 | [CNEquity README](https://github.com/rootSunc/CNEquity)：2016–2021 等权买入持有 **5.9% → 12.0%**（保留退市股 vs 只用当前名单） | `security` 表 + universe 过滤 + audit finding |
| 13 | **概念/行业成分非 PIT**：用当前成分回看历史 | [skill-b6](https://github.com/quantskills/skill-b6-limitup-pool) 用 PIT 过滤 | `index_cons.py` 是否按日期生效 |
| 14 | **龙虎榜多上榜原因金额重复计数** | §4.2 | 是否按「原因 × 席位」建模 |
| 15 | **北向数据已消失却仍在展示** | [新华财经 2024-08-19](https://www.cnfin.com/yw-lb/detail/20240819/4090757_1.html) | 北向模块是否已按新口径重建 |
| 16 | **ClickHouse `ReplacingMergeTree` 去重非实时** | §2.5（若上 CH） | 是否用 `FINAL`/`argMax` 显式去重 |
| 17 | **Parquet 分区列类型不一致 → 谓词下推失效** | §2.3（性能静默退化） | 分区列类型是否统一 |
| 18 | **高并发触发封禁（比高频更危险）** | [akshare #6061](https://github.com/akfamily/akshare/issues/6061) + CNEquity 单飞锁 | 是否有源级互斥 |

---

## 6. 对 lquant 的分期建议（汇总）

> 以下按「成本 × 风险」排序。所有建议都对应上文的具体证据。

### P0 · 立即做（低成本、防静默失败）

1. **水位前移门禁 + 覆盖率门禁**（CNEquity `meta/state` + `valuation_bars_low_coverage`）
   - 批次失败 / 当日 symbol 覆盖率 < 阈值（如 70%）→ **拒绝推进 checkpoint**，登记 finding。
   - lquant 已有断点续传 checkpoint 与 `quality/`，**只缺这道门禁**。
2. **源级单飞锁**（CNEquity `RunLock("baostock")`）
   - 与令牌桶正交：限流管速率，单飞管并发会话数。
3. **`strict_adj` fail-loud**：清理一切 `factor=1.0` 兜底；缺复权因子 → 报错。
4. **契约清单 + 离线契约测试 + 线上 `pageSize=1` 探针**（CNEquity `datacenter_contracts.py`）
   - 覆盖：必需列集合、列数、类型、**单位标签**、退役标记。
   - **绝不按位置取列**；契约不满足 → **整批失败，不写空表**。
5. **北向模块审计**：删除/重标一切「北向实时净流入」；按 2024-05-13 / 2024-08-19 新口径重建（前十大活跃成交股 + 季度持股 + 龙虎榜北向席位）。
6. **失败 ≠ 空值**：区分「接口失败」「无数据」「数据为 0」三态；`money_flow` 等表停牌日写 null。
7. **`dataset_schema_version` + 「curated 列只增不改」** 写进 `schema.py` 的纪律。

### P1 · 本季度（口径统一与看板补齐）

8. **复权迁移到「存 hfq + 查询期按窗口 anchor 派生 qfq」**（CNEquity ADR-0004；free-stockdb 独立趋同）
   - 统一 `load()` 与 DuckDB 视图的 anchor 定义，并把 T 记入 lineage。
9. **涨停池/情绪表补字段**（skill-b6 口径）
   - `limit_up_pool`：`limit_up_streak`、`first_seal_time`、`final_seal_time`、`special_pattern`（8 类带优先级）、`lead_concept`、`pool_status`（6 态状态机）、**`seal_metric_source`**。
   - 新增 `limitup_sentiment`：`n_limit_up`、`market_blow_rate`、`max_height`、`promote_rate_by_tier`、`prev_limitup_premium`。
   - **昨日梯队保留已回落成员**（vibe-astock）。
10. **龙虎榜席位语义层**（skill-b7 口径）
    - `seat_tag` / `seat_alias` / `confidence` / `tag_version`；`hotmoney_net` = 游资 + 营业部（**排除量化**）；按「原因 × 席位」建模。
    - **调度改到 19:30 之后**。
11. **涨跌停规则表统一**：看板与回测共用同一张「按板块的涨跌幅上限」表（主板 10% / 创业板·科创板 20% / ST 5% / 北交所 30% / 新股例外）。
12. **in‑Provider 同层双源热备**（Ashare 模式）：`fallback.py` 增加「主备同时注册、故障零延迟切换」，而不仅是跨 Provider 串行 Fallback。
13. **看板补「人气指标」类因子**（InStock 的股吧人气排名/变化/连涨连跌/粉丝结构）——**优于帖子 NLP 情绪**。
14. **compaction 落地**（Arc + CNEquity）：子进程 + manifest 门禁 + PK `keep=last`；解决按年分区 + 高频小批写入的小文件问题。
15. **看板工程纪律**（vibe-astock）：单路取数超时（~90 s）+ 超时清理进程；部分覆盖标注；历史稿不可篡改；幂等重生成。

### P2 · 后续（架构演进）

16. **Provider 输出模型化**（OpenBB）：为每个 Capability 定义标准输出模型（含类型/单位），Provider 继承 + 只声明差异 → **取代 `mapping.py` 的手工映射表**；输入侧补 `QueryParams`（含 `adjust`，从类型层面消灭口径漂移）。
17. **「配置驱动看板」**（pythonstock + OpenBB widget 化）：新增指标 = 注册元数据 + widget，不改前端路由。
18. **数据版本快照（snapshot）**：把「某次回测用的数据版本」记为快照 id（ArcticDB 概念），替代文件时间戳。
19. **MCP 暴露**（free-stockdb 的五种调用方式）：把因子/指标查询暴露为 MCP 工具。
20. **协议直连的长期替代**：评估 `rustdx`，避免 `mootdx` 的 Python 依赖钉死（`tenacity<9`、`httpx<0.26`）。
21. **书面数据契约文档**（CNEquity 消费契约层）：hfq/qfq 语义、PIT 语义、universe 语义、水位语义、schema 版本；声明「影响这些条款的改动 = breaking change」。

### 明确不做

- ❌ 引入 pandas 作为内部契约（所有 A 股开源库的事实契约，lquant 回退即倒退）。
- ❌ 引入 pytdx（已归档）、mootdx 作为长期战略依赖、qstock、adata（半停滞）。
- ❌ 引入服务型 DB（ClickHouse/TimescaleDB）作为当前存储（除确有多用户低延迟聚合需求）。
- ❌ 引入 ArcticDB 作为主存储（重依赖 + `NOASSERTION` 许可 + 空间放大）。
- ❌ 引入 InStock / pythonstock 的技术栈（MySQL + tornado + bokeh + pandas）。
- ❌ 做「AI 荐股 / 买卖建议」产品形态（合规与可信度双风险）。
- ❌ 用 monkey patch 修上游（akshare-proxy-patch 式技术债）。
- ❌ 继续以「北向实时净流入」为情绪因子（**数据源已政策性消失**）。
- ❌ 持久化物化 qfq 序列（不可复现 + 逼迫全历史回写）。

---

## 附录 A · 硬证据原始数据（实测）

### A.1 GitHub REST API 实测（2026-10-08 抓取）

```
akfamily/akshare     stars=22864 forks=3530 archived=False pushed_at=2026-10-07T04:17:29Z open_issues=2 license=MIT
1nchaos/adata        stars=5261  forks=707  archived=False pushed_at=2025-12-26T11:09:57Z open_issues=36 license=Apache-2.0
Micro-sheep/efinance stars=4080  forks=753  archived=False pushed_at=2026-07-17T07:47:42Z open_issues=153 license=MIT
waditu/tushare       stars=15447 forks=4430 archived=False pushed_at=2024-03-13T14:31:20Z open_issues=771 license=BSD-3-Clause
mootdx/mootdx        stars=2413  archived=False pushed_at=2024-07-16
rainx/pytdx          stars=1552  ARCHIVED=True pushed_at=2020-04-15
mpquant/Ashare       stars=3894  archived=False pushed_at=2025-12-24
shidenggui/easyquotation stars=5455 forks=1520 archived=False pushed_at=2026-02-28T07:19:23Z open_issues=6 license=MIT
microsoft/qlib       stars=49220 forks=7767 archived=False pushed_at=2026-10-08T10:58:43Z open_issues=489 license=MIT
man-group/ArcticDB   stars=2539  forks=225  archived=False pushed_at=2026-10-08T11:44:10Z open_issues=361 license=NOASSERTION size=263879KB
duckdb/duckdb        stars=41982 forks=3879 archived=False pushed_at=2026-10-08T14:02:18Z open_issues=1103 license=MIT
ClickHouse/ClickHouse stars=50295 forks=9072 archived=False pushed_at=2026-10-08T14:13:39Z open_issues=8164 license=Apache-2.0
timescale/timescaledb stars=23658 forks=1167 archived=False pushed_at=2026-10-08T13:54:34Z open_issues=431 license=NOASSERTION
OpenBB-finance/OpenBB        -> 301 Moved Permanently (真身 openbq-org/OpenBB stars=73975 pushed_at=2026-10-02)
OpenBB-finance/OpenBBTerminal-> 301 Moved Permanently
OpenBB-finance/openbb-forecast          ARCHIVED  (pushed 2024-07-19)
OpenBB-finance/experimental-openbb-platform-agent ARCHIVED (pushed 2024-07-22)
OpenBB-finance/openbb-platform-pro-backend ARCHIVED (pushed 2026-08-24)
pythonstock/stock    stars=7879  forks=2358 archived=False pushed_at=2026-05-07T07:42:21Z open_issues=78 license=Apache-2.0
tradingview/lightweight-charts stars=17515 forks=2646 archived=False pushed_at=2026-10-08T11:37:57Z open_issues=128 license=Apache-2.0
hello245m/free-stockdb  stars=2813 pushed_at=2026-10-04
DR-lin-eng/stock-scanner stars=1150 pushed_at=2026-03-02
simonlin1212/vibe-astock stars=668  pushed_at=2026-10-07  (v1.1.3, Apache-2.0, py3.12+react19)
gitkkkk/instock      stars=38   pushed_at=2025-08-28  (Docker: mayanghua/instock)
xbfighting/tdx2db    stars=165  pushed_at=2026-09-25
zjp-CN/rustdx        stars=279  pushed_at=2026-04-08
BiomancerGame/mootdxPlus stars=15 pushed_at=2026-07-25  (社区续作)
quantskills/skill-b6-limitup-pool stars=8 pushed_at=2026-09-07 (GPL-3.0-only)
quantskills/skill-b7-lhb-monitor  stars=12 (GPL-3.0-only)
hyan1985/MarketMonitoring stars=13 (MIT, GitHub Actions 每日)
trading4/dfcf_eastmoney stars=6 pushed_at=2024-06-21
guoyaohua/limit-up-sniper stars=12 pushed_at=2026-07-16
stock-programmer/limit-up-review stars=79 pushed_at=2025-08-10
myquant/InStock      -> 本次未能解析（未核实）；GitHub API: Not Found
baostock/baostock    -> GitHub API: Not Found（非 GitHub 项目）
rainydew/pytdx       -> GitHub API: Not Found（真身是 rainx/pytdx）
myhhub/Ashare        -> GitHub API: Not Found（真身是 mpquant/Ashare）
bopo/mootdx          -> GitHub API: "Repository access blocked"（现真身 mootdx/mootdx）
```

### A.2 PyPI JSON API 实测（2026-10-08 抓取）

```
akshare        1.19.1    uploaded=2026-09-30  n_releases(recent)=223
adata          2.9.5     uploaded=2025-12-26  n_releases=81
efinance       0.5.9     uploaded=2026-07-17  n_releases=39
mootdx         0.11.7    uploaded=2024-05-04  n_releases=100   ← 停滞
tushare        1.4.29    uploaded=2026-03-25  n_releases=229
baostock       0.9.4     uploaded=2026-09-21  n_releases=14    ← 主源仍维护
pytdx          1.72      uploaded=2019-08-26  n_releases=70    ← 死
Ashare         -> 404（无 PyPI 包）
easyquotation  0.7.7     uploaded=2025-03-25  n_releases=40
qstock         1.3.8     uploaded=2025-03-16  n_releases=7     ← 停滞
arcticdb       6.27.1    uploaded=2026-10-06  n_releases=110
duckdb         1.5.6     uploaded=2026-09-28  n_releases=146
clickhouse-connect 1.10.0 uploaded=2026-10-07 n_releases=148
openbb         5.0.0     uploaded=2026-09-29  n_releases=63
cnequity       0.16.0    uploaded=2026-10-07  n_releases=14    ← CNEquity，极活跃
pywencai       0.13.1    uploaded=2025-05-06  n_releases=40
```

### A.3 关键源码/文档引用

- qlib bin 写入/读取：[`qlib/data/storage/file_storage.py` @ v0.9.7](https://github.com/microsoft/qlib/blob/v0.9.7/qlib/data/storage/file_storage.py)、[`scripts/dump_bin.py` @ v0.9.7](https://github.com/microsoft/qlib/blob/v0.9.7/scripts/dump_bin.py)
- CNEquity：[仓库](https://github.com/rootSunc/CNEquity) ｜ [架构总览](https://github.com/rootSunc/CNEquity/blob/34005e9ec81552ad1689fea6519bd4b37b52f998/docs/architecture/overview.md) ｜ [故障排查](https://github.com/rootSunc/CNEquity/blob/34005e9ec81552ad1689fea6519bd4b37b52f998/docs/operations/troubleshooting.md) ｜ [ADR-0002](https://github.com/rootSunc/CNEquity/blob/34005e9ec81552ad1689fea6519bd4b37b52f998/docs/adr/0002-parquet-lake-over-database.md) ｜ [ADR-0004](https://github.com/rootSunc/CNEquity/blob/34005e9ec81552ad1689fea6519bd4b37b52f998/docs/adr/0004-store-hfq-derive-qfq-at-query.md) ｜ [文档站](https://rootsunc.github.io/CNEquity/)
- OpenBB 标准化：[Standardization](https://docs.openbb.co/odp/python/developer/standardization) ｜ [Validators](https://docs.openbb.co/odp/python/developer/how-to/validators) ｜ [Migration from V4](https://docs.openbb.co/odp/python/migration-from-v4)
- lightweight-charts：[v4 → v5 迁移](https://tradingview.github.io/lightweight-charts/docs/migrations/from-v4-to-v5) ｜ [issue #1791](https://github.com/tradingview/lightweight-charts/issues/1791)
- ArcticDB：[Library API](https://docs.arcticdb.io/latest/api/library/)
- Parquet compaction：[Arc — How Parquet Compaction Works](https://basekick.net/blog/arc-compaction-deep-dive)
- Schema drift：[Microsoft Learn — Detect and Manage Schema Drift](https://learn.microsoft.com/en-us/training/modules/implement-manage-data-quality-constraints-unity-catalog/4-detect-manage-schema-drift) ｜ [SQLBits 2026 — Preventing Silent Failures](https://sqlbits.com/sessions/event2026/Data_Validation_in_Production_ML_Preventing_Silent_Failures_with_Pandera_GE_DBT_and_Deepchecks)
- akshare 接口失效：[#6061 东财封 IP](https://github.com/akfamily/akshare/issues/6061) ｜ [#6574](https://github.com/akfamily/akshare/issues/6574) ｜ [#6987 RemoteDisconnected](https://github.com/akfamily/akshare/issues/6987) ｜ [#7180](https://github.com/akfamily/akshare/issues/7180) ｜ [akshare-proxy-patch](https://github.com/HelloYie/akshare-proxy-patch)
- 北向资金政策：[新华财经 2024-08-19](https://www.cnfin.com/yw-lb/detail/20240819/4090757_1.html) ｜ [东方财富 2024-08-26](https://finance.eastmoney.com/a/202408263165166650.html)
- BaoStock：[复权因子简介 PDF](http://www.baostock.com/baostock/images/2/20/BaoStock%E5%A4%8D%E6%9D%83%E5%9B%A0%E5%AD%90%E7%AE%80%E4%BB%8B.pdf) ｜ [CSDN 五大陷阱（二手，部分说法不予采信）](https://blog.csdn.net/weixin_28235513/article/details/166963637)

### A.4 未核实项（明确列出，避免误用）

1. `myquant/InStock` 的真实去向与维护状态（本次未解析成功；可见副本为 `gitkkkk/instock`）。
2. baostock 官网 `adjustflag` 的**官方原文**（站点 JS 化，多次抓取未获正文）；本报告的复权结论**依据 CNEquity ADR-0004 与 lquant 自身对拍**，而非 baostock 官网。
3. baostock GitHub 无仓库 → **无法给出 `pushed_at`/`archived`**；仅以 PyPI 发版日期（2026-09-21）判定「仍维护」。
4. ClickHouse / TimescaleDB 在**量化时序**场景的第三方 benchmark 数值（本次未取到可比数据）。
5. ArcticDB 版本化的**空间放大倍率**。
6. `hq.sinajs.cn` 的 Referer 要求与**批量查询上限**的具体数值。
7. `d.10jqka.com.cn`（同花顺）接口的当前有效性。
8. free-stockdb 自述的性能数字（3–5 天、快 3 倍、小 3 倍）——**厂商自述，无第三方验证**；且其为**闭源二进制**。
9. mootdx 通达信服务器地址列表中**具体哪些条目已过期**。
10. OpenBB 的错误体系具体类名（`OpenBBError` 等）——本次未逐一核对源码。
11. `hyan1985/MarketMonitoring` 与 `quantskills/skill-b7-lhb-monitor` 的 `pushed_at`（GitHub API 配额耗尽）。
12. stock-scanner 的真实维护意愿（README 自述降速）。
