# 从两个开源项目借鉴什么 —— 只谈日线（lquant 视角）

分析对象（已 clone 到 /tmp 只读分析）：

- **TSP** [`shy3130/tick-stock-panel`](https://github.com/shy3130/tick-stock-panel) — Python + Polars/DuckDB，~70 services / 33 api / 292 测试文件
- **easy-stock** [`jundizhou/easy-stock`](https://github.com/jundizhou/easy-stock) — Go + 桌面端，后端 87k 行 / 156 测试文件

**范围约束**：只关注日线；分时/tick/盘口/L2/实时推送一律不纳入。

---

## 〇、先说许可证（决定「能抄代码」还是「只能抄思路」）

| 项目 | 许可 | 对 lquant 的含义 |
|---|---|---|
| TSP | **MIT**（`LICENSE` 首行；README 徽章） | **可以移植代码**，保留版权声明即可 |
| easy-stock | **PolyForm Noncommercial 1.0.0**（`LICENSE` 首行 + `README.md:273-281`） | **不能复制代码**。明确禁止企业生产环境、收费服务、SaaS、商业产品集成、二次销售。只能借鉴**不构成表达的设计思路**并自行实现 |

> 如果 lquant 有任何商业化可能 → easy-stock 那条线**只取思路、不取代码**。下文凡涉及 easy-stock 的建议都已按此处理。

---

## 一、已核验：这些 lquant 已经有，不要重复投入

我把两份报告的建议逐条对 lquant 源码核过，以下**不是缺口**：

| 候选建议 | lquant 现状（证据） |
|---|---|
| 数据源**能力声明 / 能力路由** | 已有：`data/capability.py` 的 `Capability` 枚举 + provider 声明 `capability` + `require()` fail-closed（`providers/__init__.py:90`、`sina.py:87`） |
| **Newey-West HAC t 值** | 已有：`factors/evaluate/ic.py:106 newey_west_tstat`，并已进 IC 报告（`:164 t_stat_nw`） |
| 多重检验校正 | 已有 Bonferroni 风格：`factors/mining/fitness.py:12 sqrt(2*ln(n_trials))`，已接进 submit 的 `LOW_TSTAT` |
| **DSR / PSR / E[maxSR] / CSCV-PBO** | 已有（本轮刚修过公式）：`backtest/confidence.py` |
| **walk-forward 滚动样本外** | 已有：`research/ml/online.py rolling_retrain` + `walk_forward_splits` |
| 回测**涨停不可买 / 一字板**处理 | 已有：`backtest/broker.py:106,130,179`「涨停不可买」，`exit/base.py:171` 一字板判定，`selfcheck.py:145` 有断言 |
| **板块感知涨跌幅 + 无涨跌幅** | 已有：`backtest/rules/model.py:171 no_price_limit`、`:186 price_limit.for_symbol(symbol, board, is_st=…)` |
| 交易日历 / 交易日判断 | 已有：`core/types.py`、各 collector 的 `_is_trading_day`/`today_cn` |
| 跨源**值级对拍**（分层降级） | 已有：`data/quality/crosscheck.py`（L0~L3 分档，明确「只用于标记与降级，绝不用于取值」） |
| Golden 已知答案回归集 | 已有：`data/quality/golden.py`（结构性事实 + 确定性计算 + 冻结因子 IC） |
| 停牌/新股/ST **可交易性打标** | 已有：`data/quality/tradability.py`（`NEW_LISTING`/`ST_RISK`/`SUSPENDED`） |
| PIT 检查 / 复权一致性检查 | 已有：`data/quality/universe.py check_point_in_time`、`validators.py check_ret_identity(qfq vs hfq)`、`check_limit_breach` |
| 组合优化（TE/换手/no-trade band/仓位） | 已有：`portfolio/optimizer.py`、`weighting.py`、`sizing.py` |
| 8 通道通知 + 规则引擎 + 去重/冷却 | 已有：`notify/`（比 TSP 的 4 通道更全） |
| OpenAPI 大体量开放面 | 已有：183 条路径 / 22 分组 |

---

## 二、建议补充（按「价值 ÷ 成本」排序）

### A 档 · 立即做（自包含、低成本、纯日线正确性）

#### A1. 新股「无涨跌幅窗口」精确化 —— 最高性价比
- **来源**：TSP `price_limits.py:47-126`（`is_no_limit_day`），含**分板块 + 政策生效日**的精确窗口：沪深主板 2023-02-17 起上市**前 5 个交易日**、创业板 2020-08-24 起前 5 日、科创板 2019-07-22 起前 5 日、北交所 2021-11-15 起**仅首日**；无历史序列时退化为 `listing_date + N 日历天` 保守边际并声明「宁多标勿漏标」。
- **lquant 现状**：`rules/model.py:171` 的注释是「IPO **首日** / 复牌首日 / ST 变更日」，而 `tradability.py` 的 `NEW_LISTING` 是 **120 天**粗粒度标记。**看起来只覆盖首日**，注册制下的「前 5 个交易日」没覆盖 → 新股上市第 2~5 日的 ±44% 波动会被当成「涨停不可买」而错误拒单，回测新股/次新策略会系统性失真。
- **动作**：核对 `price_limit.for_symbol` 的窗口口径；若确实只到首日，按板块 + 生效日补齐。
- **成本**：低（纯函数 + 一张常量表）。

#### A2. purged / embargoed 交叉验证
- **来源**：TSP `backtest/mining.py:91-152`（outer 504/126/63、inner 252/63/63、`purge_bars=30`、`embargo_bars=5`）。
- **lquant 现状**：`walk_forward_splits` 已有滚动切分，但**没有 purge/embargo**；而 lquant 的标签普遍是「前瞻 h 日收益」，训练/测试边界存在 h−1 阶重叠 → 样本外指标偏乐观。
- **动作**：给 `walk_forward_splits` 加 `purge_bars` / `embargo_bars` 两个参数（默认 0 保持兼容），挖掘与 ML 侧按前瞻窗口传入。
- **成本**：低（切分逻辑改动，不碰引擎）。

#### A3. 交易日**会话级**新鲜度（把「过期」变成可计算量）
- **来源**：easy-stock `foundation/trading_calendar.go:7-39`（`LatestCompletedAStockSession`：15:00 前不把当日算已完成；`AStockSessionLag` 按**交易会话数**计龄，节假日不老化；注释明确「价格新 ≠ 收盘已完成」）。
- **lquant 现状**：有 `today_cn()` 与交易日判断，但**缺少「当日 bar 是否已收盘」的显式判定**；盘后同步前跑因子/回测可能用到未收盘的当日 bar。
- **动作**：加一个 `latest_completed_session()` / `session_lag()` 助手，并在因子与回测入口用它替代裸 `today`。
- **成本**：低（~100 行 + 交易日历已有）。**注意**：只借思路（PolyForm NC）。

#### A4. 交易所**异常波动偏离值**接近度
- **来源**：TSP `services/abnormal_moves.py:41-122`：`偏离值 = 个股 N 日累计涨跌幅 − 对应指数同期涨跌幅`，按板块分档（主板 3 日 ±20%、创业板/科创板 ±30%、北交所 ±40%；10 日 +100%/−50%、30 日 +200%/−70%，**负向阈值显著严于正向**），并输出接近度分级（≥1.0 已触发 / ≥0.7 边缘 / ≥0.5 观察）。
- **lquant 现状**：**无**（grep 仅 1 处无关命中）。这是纯日线、A 股特有、且直接关系风控（避开即将被特别处理/停牌的标的）。
- **动作**：新增一个 quality/信号模块 + 一条监控规则类型。
- **成本**：低（纯函数）。

#### A5. 关键价位指标体系
- **来源**：TSP `indicators/levels.py:62-566`：9 类价位（成交密集区/枢轴点/前高前低/Keltner/ATR 通道/**缺口位含未回补过滤**/斐波那契/整数关口），每个带 `side`/`strength`/`rank`。
- **lquant 现状**：只有一个特定口径 `backtest/exit/pressure.py`（天道通道金牛线做分批止盈），**没有通用价位指标集**。
- **动作**：作为 indicators 注册表的新类别（纯日线、可复用现有 registry/图表链路）。
- **成本**：低。

### B 档 · 随后做（需接现有流水线 / 中等成本）

#### B1. 盘后官方日线的「值级覆盖 + 自动修复」
- **来源**：TSP `jobs/daily_pipeline.py:31-108` + `services/kline_sync.py:477-479` + `services/data_integrity.py:64-171,228-330`。
  三个关键设计：① **`quote_ts` 哨兵**写进当日分区，用来区分「盘中快照」与「盘后权威历史」；② **值级比对**（`enriched.raw_close` 与 `daily.close` 差超半个最小报价单位即删分区重算）—— 因为**行数校验识别不到**（其注释记录了真实事故：3392/5554 只股票实时端点收盘价与官方日线不符）；③ 只报**尾部缺口**，不扩面到历史内部空洞。
- **lquant 现状**：`data/quality/` 有 crosscheck/golden/tradability/validators，但看起来是**跨源对拍**与**结构/事实校验**，没有「盘中快照污染当日分区」的哨兵 + 值级删分区重算。
- **为什么值得**：lquant 有 `market_snapshot` 与盘中采集能力；一旦盘中价被当成收盘价写进日线湖，因子与回测会产生**看似合理的静默错误**。
- **成本**：中（要接进现有同步/修复任务）。

#### B2. 扩展数据表体系（BYO 数据 → 自动因子）
- **来源**：TSP `services/ext_data.py` + `ext_pull.py` + `factors/ext_factors.py:1-24`：HTTP 定时拉取/CSV 上传/JSON 写入 → 自动 schema 发现 + 符号归一 → 并入 DuckDB；数值字段**自动注册为因子** `ext_{表}_{字段}`，字符串字段只进信号条件通道；**PIT 边界**（timeseries 精确对齐、**snapshot 模式只在当日注入**防回看引入未来数据）；按日历史回补时**以响应行 date 校验请求日，不符则拒写**（fail-closed）；每张表天然是 `/rows` + `/values` 查询面。
- **lquant 现状**：**无**（grep 0 命中）。lquant 是「官方数据集」形态，而真实研究里最有 alpha 的常是自有另类数据。
- **成本**：中~高。建议分期：先做「扩展表 + 自动因子注册 + PIT 规则」，回补与分页协议后置。

#### B3. 市场环境 regime + 情绪阶段（分位标定、规则可审）
- **来源**：TSP `services/regime_builder.py:56-152`（5 档状态 = profit/speculation/resilience/trend 四维加权，阈值用 2022–2026 真实 **p15/p85** 标定；显式论证维度独立性，避免 up_pct 与 down_pct 重复计权；inf/nan → 中性 50）+ `services/market_phase.py:45-86,150-255`（6 阶段情绪周期，阈值集中并标注对应分位，EMA α≈1/3 平滑 + **连续 2 日确认** + **大盘弱档否决**）。
- **lquant 现状**：**无**（regime/情绪周期 0 命中）。lquant 有市场日报与板块因子，但缺「因子该不该上线」的环境层判断。
- **可迁移要点**：分位标定而非拍阈值；显式处理子维度相关性；切换要平滑+确认。
- **成本**：低~中（纯日线纯函数）。

#### B4. 「判断 → 留痕 → 次日对账 → 教训沉淀」闭环
- **来源**：easy-stock `review/types.go:223-256,455-481` + `daily_validation.go:189-221,420-470`：把每日结构化研判（市场状态/情景/方向/明日焦点/验证清单）存为**不可变快照**，次日用实际日线逐项判 `correct/partial/wrong/unverified` + 加权 Score/Coverage + `Lessons` + `RealizedRisks`（权重 25/20/25/20）。
- **lquant 现状**：**无**。有因子评价/样本外/ML 版本管理/监控，但**没有任何「研究判断 → 未来事实 → 命中率」的闭环**。
- **为什么值得**：这是把日线数据从「策略参数」变成「认知资产」的唯一路径，且完全日线可实现。**只借思路**（PolyForm NC）。
- **成本**：中（预判 schema + 一个按交易日触发的对账任务 + 一张结果表）。

#### B5. 条件级事后核验（Verify）
- **来源**：easy-stock `stockanalysis/research_verification.go:13-120`：冻结价格锚点与条件，未来**只读已完成日线**判定；交易日基准不可用 → `unavailable` 而非猜测；检测复权修订导致的历史重叠价变化；停牌缺失日不跳过。
- **lquant 现状**：无单标的单条件级的可追溯核验。是 B4 的微观版，成本更低，且能直接串 lquant 的 8 通道通知（条件触发即推）。
- **成本**：低。**只借思路**。

### C 档 · 规划做（需要产品决策）

- **C1. 开放接口 Token 鉴权 + scope 分档 + 契约快照测试**：lquant 183 条路径**目前无鉴权**（`paper/service.py:49` 自己注释「/paper/accounts 是无鉴权 HTTP 端点」）。TSP 的做法是把「路径+方法→所需 scope」做成**唯一规则表**，OpenAPI 按表过滤，并用**契约快照测试**锁死开放面（`api_gateway.py`、`tests/test_openapi_contract.py`）。成本中，但只在「要对外暴露」时才必要。
- **C2. AI 产出的证据约束**（压缩包 + 引用校验 + 阶段检查点 + 预算冻结）：lquant 有 Ask-AI/MCP，但 AI 结论目前缺强制引用与可重放约束。建议先做最小的两件：**论点必须引用来源 ID** + **连接错误/取消不触发 JSON 修复**。成本低~中（思路借鉴）。
- **C3. 扩展点契约化（窄契约 + 版本号 + 加载失败隔离 + 删目录即卸载）**：lquant 已有 EP 文档，可参考 TSP `extensions/contracts.py:9` / `loader.py:47-88` 把「窄契约 + 版本不符跳过 + setup 失败隔离」固化。
- **C4. 把 `portfolio` 包接到 CLI/API**（**这条不是从参考项目抄的，是我核验时发现的自家缺口**）：`portfolio/` 有选池 `screener.py`（ST/次新/停牌/一字板/流动性过滤 + 打分）、`sizing.py`、`weighting.py`、`optimizer.py`、`riskmodel.py`，但**在 CLI/API 里只有 1 处引用**（`strategy.py:50` 导入策略库）。没有 `lq portfolio …` 命令组、也没有对应 API/页面 —— 整个组合工具链**不可达**。TSP 的「选股引擎 + 策略 META 驱动表单 + 因子/策略双向桥」正好说明这类能力应该长成什么样。**成本低、收益高（已有代码，只差接线）**。

---

## 三、工程实践可借鉴（都不涉及代码复制）

1. **把「数据契约」写成可审查的一章**：TSP `CONTRIBUTING.md:82-121` 单列「不可混用的数据契约」（比例/百分比、价格与复权、日期/交易日/时区、历史股本），并要求**新增跨边界映射必须加单位测试**，明令禁止「数值小于 1 就乘 100」式启发式。配「验证矩阵」（按改动类型列最低验证要求）。lquant 口径复杂度同级，这份文档成本极低。
2. **口径披露随响应返回**：TSP 概念成分「当前快照回看历史」的已知偏差用 `membership_note` 字段随 API 返回并在页面展示。lquant 已有 `universe_note` 的先例，可推广成统一约定。
3. **跨进程缓存失效用 generation token**：TSP `enriched_generation.py` 用原子替换的标记文件维护单调代际，读方按代缓存、非 ready 即 fail-closed。lquant 回测/挖掘跑子进程共享数据湖，值得取「generation 令牌」部分（孤儿恢复那套复杂度不必抄）。
4. **AI 写操作 = 带超时的确认卡**：TSP `custom/assistant/actions.py:10-50`：参数对人类完整可见、120s 超时视为拒绝、action 与 tool 分开注册。lquant 的 agent 若要放开写操作，这套是底线。

---

## 四、明确不建议

- **easy-stock 的盘中/实时相关**：连板梯队首末封时间、开板次数、竞价状态、盘中情绪快照、VWAP 距离 —— 强依赖分钟/盘口。
- **easy-stock 的内容运营体系**：大 V 文章采集（雪球/淘股吧/微信）+ OSS 分发 + Electron 桌面 + 浏览器桥接抓取 —— 合规与维护风险高，且不是量化能力。
- **双 AI 运行时**（Hermes + 原生 Codex App Server）：全仓最重的复杂度，收益只是「可选运行时」。
- **36KB 巨型 prompt + 多轮修复的组合优化 AI**：lquant 的确定性求解更可靠。只有两个概念可迁移：**越界错误回灌**（把具体字段错误返回给调用方）与**冻结检查点 + 转录重放测试**。
- **TSP 的 TickFlow 档位体系**：把某数据商商业套餐编码进架构，lquant 自建湖不需要。
- **TSP 的 vectorbt 遗留回测路径**：历史包袱（自承 T+1 用「信号后移一根 K」近似）。
- **TSP 26 个内置策略的具体内容**：与 lquant 策略库定位大概率重叠。只借形式：**META 驱动表单** + **`research_only` 研究模板隔离**（策略在目录里但不进选股列表，仅供挖掘 worker 用）。
- **概念成分「当前快照回看历史」这个做法本身**：TSP 自己承认是已知偏差。要抄的是**披露方式**，若 lquant 做板块/概念应**从第一天起按月累积 PIT 快照**。

---

## 五、建议的下一步（如果要落地）

1. **先做 A1 + A2 + A3**（三处正确性，都是低成本、纯日线、且影响回测可信度）
2. **再做 C4**（接线自家 `portfolio`，已有代码，收益最高）
3. **然后 A4 + A5 + B3**（补齐日线特征与市场状态）
4. **B1 与 B2 需要排期**（要接流水线/面积大）
5. **B4/B5 是产品决策**（把「研究判断」变成可追责记录），但一旦做，会让 lquant 与「一堆脚本」拉开差距

> 一句话：**TSP 值钱的是「把 A 股日线的脏活做对」的工程纪律与精确规则；easy-stock 值钱的是「每次判断都留痕、可回放、次日自动对账」的证据主线。前者 MIT 可以直接移植，后者只能借思路。**
