# E · 中文社区 / 国内开发者开源 A 股分析项目调研（查漏补缺）

> 调研角色：第五路（补漏）。前四路已覆盖 QUANTAXIS / Hikyuu / qteasy / vnpy / RQAlpha / backtrader / qlib / AlphaPurify / akshare / tushare 等，本报告只做**查漏补缺 + 指定项目深挖**。
>
> **数据来源与核实口径**：所有 star / 最后推送时间 / 语言 / license / archived 均通过 `gh api repos/<owner>/<repo>` 直接读取 GitHub REST API，抓取时刻 **2026-10-08（Asia/Shanghai）**。这是一手数据，不是搜索结果转述。
>
> **核实状态标记**：
> - `[已核实]` = GitHub API 元数据已确认
> - `[机制-未逐行核实]` = 机制描述来自仓库 description / README 摘要 / 搜索结果，**未逐行读源码**，采信前需自行复核
> - `[未核实]` = 本轮未确认，明确标注
>
> 时间预算说明：本轮在读取 README 原文时触发了抓取工具故障（`gh api .../readme` 返回内容解码为空），因此**机制层细节的核实深度有限**，已在各节逐条标注。元数据层 100% 一手核实。

---

## 0. 一页结论

| 结论 | 说明 |
|---|---|
| **中文生态在 2026 年比前四路覆盖的更活跃** | czsc **已用 Rust 重写**（语言字段 = Rust，当日仍在推送）；TradingAgents-CN 32215⭐；tick-stock-panel 5717⭐ 当日推送 |
| **最大的单点缺口是「几何结构层」（缠论）** | chan.py / chanlun-pro / ChanlunX 三个项目合计 >3600⭐ 且都活跃，而 lquant `indicators/__init__.py` **自己写明**「中枢/浪型这类分段结构」表达不了 |
| **第二个缺口是「通达信公式兼容层」** | MyTT 2872⭐，仍是通达信生态事实标准；lquant 因子 DSL 自成一套，无法直接吃现成 TDX 公式 |
| **第三个缺口是「多智能体辩论式投研编排」** | lquant 有单 Agent + MCP + A2A，但没有角色分工/对抗辩论的编排层 |
| **多个任务清单里的项目名是错的（404）** | `myhhub/Ashare`、`myquant/InStock`、`TradingGym/TradingGym`、`tushare/tushare` 全部 404，真身在别处（见 §1.3） |
| **聚宽迁移：lquant 已有基础，是优势不是缺口** | 见 §1.4 与 §4 |

---

## 1. 项目总表

### 1.1 指定深挖组

| 项目 | 仓库 | Star | 最后推送 | 语言 | License | 状态 |
|---|---|---|---|---|---|---|
| **zvt** | [zvtvz/zvt](https://github.com/zvtvz/zvt) | 4313 | 2026-07-01 | Python | MIT | 维护中（低频） |
| **czsc** | [waditu/czsc](https://github.com/waditu/czsc) | 6385 | 2026-10-08 | **Rust** | NOASSERTION | **非常活跃（当日）** |
| **Qbot** | [UFund-Me/Qbot](https://github.com/UFund-Me/Qbot) | 18578 | 2026-03-11 | Jupyter Notebook | MIT | 半停滞（~7 个月无推送） |
| **chan.py** | [Vespa314/chan.py](https://github.com/Vespa314/chan.py) | 2196 | 2026-09-24 | Python | MIT | 活跃 |
| **chanlun-pro** | [yijixiuxin/chanlun-pro](https://github.com/yijixiuxin/chanlun-pro) | 1053 | 2026-10-03 | Python | Apache-2.0 | 活跃 |
| **ChanlunX** | [kldcty/ChanlunX](https://github.com/kldcty/ChanlunX) | 420 | 2026-04-29 | C++ | MIT | 维护中 |
| **chanvis** | [ibaihuo/chanvis](https://github.com/ibaihuo/chanvis) | 620 | 2024-07-02 | Vue | BSD-3-Clause | 停滞（2 年+） |
| **Ashare** | [mpquant/Ashare](https://github.com/mpquant/Ashare) | 3894 | 2025-12-24 | Python | 无 | 维护中（低频） |
| **pythonstock/stock** | [pythonstock/stock](https://github.com/pythonstock/stock) | 7879 | 2026-05-07 | Python | Apache-2.0 | 维护中 |
| **InStock（真身待定）** | [gitkkkk/instock](https://github.com/gitkkkk/instock) | 38 | 2025-08-28 | null | 无 | 存疑，见 §1.3 |
| **qstock** | [tkfy920/qstock](https://github.com/tkfy920/qstock) | 1945 | 2025-03-16 | Python | MIT | **停滞（~1.5 年）** |
| **stock-scanner** | [DR-lin-eng/stock-scanner](https://github.com/DR-lin-eng/stock-scanner) | 1150 | 2026-03-02 | Python | MIT | 维护中（含 LLM 分析） |
| **TradingGym** | [Yvictor/TradingGym](https://github.com/Yvictor/TradingGym) | 1921 | 2024-02-11 | Python | 无 | **停滞（~2.5 年）** |
| **tushare** | [waditu/tushare](https://github.com/waditu/tushare) | 15447 | 2024-03-13 | Python | BSD-3-Clause | **主包已闭源迁移，仓库停滞** |
| **MyTT** | [mpquant/MyTT](https://github.com/mpquant/MyTT) | 2872 | 2026-06-13 | Python | 无 | 维护中（低频） |

### 1.2 补漏发现的高相关新项目（2026 年活跃，前四路未覆盖）

| 项目 | 仓库 | Star | 最后推送 | License | 定位 |
|---|---|---|---|---|---|
| **TradingAgents-CN** | [hsliuping/TradingAgents-CN](https://github.com/hsliuping/TradingAgents-CN) | 32215 | 2026-09-22 | NOASSERTION | 多智能体 LLM 中文金融交易框架 |
| **Sequoia-X** | [sngyai/Sequoia-X](https://github.com/sngyai/Sequoia-X) | 7976 | 2026-07-10 | 无 | A 股技术形态自动扫描 + 飞书推送 |
| **ai_quant_trade** | [charliedream1/ai_quant_trade](https://github.com/charliedream1/ai_quant_trade) | 6599 | 2026-10-05 | Apache-2.0 | 学习/模拟/实盘一站式 + LLM |
| **tick-stock-panel** | [shy3130/tick-stock-panel](https://github.com/shy3130/tick-stock-panel) | 5717 | 2026-10-08 | MIT | 自托管 A 股「选股+监控+回测」工作台 + LLM |
| **adata** | [1nchaos/adata](https://github.com/1nchaos/adata) | 5261 | 2025-12-26 | Apache-2.0 | akshare/tushare 的数据源竞品 |
| **efinance** | [Micro-sheep/efinance](https://github.com/Micro-sheep/efinance) | 4080 | 2026-07-17 | 无 | 东财接口封装（lquant 已在 fallback 链中） |
| **mootdx** | [mootdx/mootdx](https://github.com/mootdx/mootdx) | 2413 | 2024-07-16 | 无 | 通达信本地数据读取（**已 ~2 年未推送**） |
| **akquant** | [akfamily/akquant](https://github.com/akfamily/akquant) | 2398 | 2026-09-28 | MIT | akshare 官方 Rust+Python 量化框架 |
| **PanWatch** | [TNT-Likely/PanWatch](https://github.com/TNT-Likely/PanWatch) | 2042 | 2026-10-05 | MIT | A股/港股/美股 AI 盯盘（基于 TradingAgents） |
| **stock-sdk** | [chengzuopeng/stock-sdk](https://github.com/chengzuopeng/stock-sdk) | 1972 | 2026-10-03 | — | 前端零依赖 TS 股票数据 SDK |
| **aktools** | [akfamily/aktools](https://github.com/akfamily/aktools) | 1496 | 2025-10-29 | MIT | akshare 的 HTTP API 包装（lquant 可对照） |
| **QuantMind** | [qusong0627/QuantMind](https://github.com/qusong0627/QuantMind) | 1719 | 2026-10-08 | AGPL-3.0 | AI 原生多市场量化平台（集成 Qlib + RD-Agent） |
| **TradingAgents-AShare** | [KylinMountain/TradingAgents-AShare](https://github.com/KylinMountain/TradingAgents-AShare) | 856 | 2026-09-18 | — | A 股多智能体投研（15 Agent 辩论） |
| **FinanceMCP** | [guangxiangdebizi/FinanceMCP](https://github.com/guangxiangdebizi/FinanceMCP) | 857 | 2026-10-05 | MIT | Tushare/Binance 的 MCP server（TypeScript） |
| **FinRobot** | [AI4Finance-Foundation/FinRobot](https://github.com/AI4Finance-Foundation/FinRobot) | 8160 | 2026-09-28 | — | 金融 LLM Agent 平台（非中文专属） |
| **akshare-proxy-patch** | [HelloYie/akshare-proxy-patch](https://github.com/HelloYie/akshare-proxy-patch) | 192 | 2026-10-06 | NOASSERTION | **akshare/efinance 接口失效的猴子补丁** |

### 1.3 ⚠️ 清单中已失效 / 不存在的仓库（重要纠正）

任务清单里给了四个仓库名，实测**全部 404 或指向错误对象**：

| 清单里的名字 | 实测结果 | 真身 / 说明 |
|---|---|---|
| `myhhub/Ashare` | **404 Not Found** | 真身是 [**mpquant/Ashare**](https://github.com/mpquant/Ashare)（3894⭐）。`myhhub` 是误记，可能是某个 fork 作者名 |
| `myquant/InStock` | **404 Not Found** | 搜到的最接近者是 [gitkkkk/instock](https://github.com/gitkkkk/instock)（仅 38⭐）与 [opensamai/InStock](https://github.com/opensamai/InStock)（1⭐，fork）。`myquant/InStock` **原仓库已被删除或改名，未找到**。见 §2.3 |
| `TradingGym/TradingGym` | **404 Not Found** | 真身是 [**Yvictor/TradingGym**](https://github.com/Yvictor/TradingGym)（1921⭐，停更 ~2.5 年） |
| `tushare/tushare` | **404 Not Found** | 真身是 [**waditu/tushare**](https://github.com/waditu/tushare)（15447⭐）。且注意：**主包新版已迁移至 tushare.pro 闭源 + 积分制**，GitHub 仓库停在 2024-03-13 |

> 教训：调研清单本身会携带脏数据。任何"某项目 star 多少"的二手结论都必须回到 GitHub API 验证 owner/repo 是否存在。

### 1.4 聚宽 / 米筐迁移工具组

| 项目 | 仓库 | Star | 最后推送 | 状态 |
|---|---|---|---|---|
| **jqfactor_analyzer** | [JoinQuant/jqfactor_analyzer](https://github.com/JoinQuant/jqfactor_analyzer) | 704 | 2025-02-20 | 聚宽官方单因子分析工具，维护中 |
| **easytrader** | [shidenggui/easytrader](https://github.com/shidenggui/easytrader) | 10217 | 2026-02-28 | 跟踪 joinquant/ricequant 模拟盘 + 实盘下单 |
| **rqalpha** | [ricequant/rqalpha](https://github.com/ricequant/rqalpha) | 6815 | 2026-10-08 | 米筐官方（前四路已覆盖） |
| **zipline-chinese** | [zhanghan1990/zipline-chinese](https://github.com/zhanghan1990/zipline-chinese) | 692 | **2017-04-14** | **已废弃（9 年未动）** |
| **DeepTraderV2** | [Lihw99/DeepTraderV2](https://github.com/Lihw99/DeepTraderV2) | 37 | 2026-05-06 | 「聚宽策略 → 复制粘贴 → 本地运行」迁移器 |
| **EasyQuant** | [HiRenyi/EasyQuant](https://github.com/HiRenyi/EasyQuant) | 65 | 2026-06-01 | AI 优化策略 + 本地/聚宽免费回测 |
| **Quant-Strategy** | [JizhiXiang/Quant-Strategy](https://github.com/JizhiXiang/Quant-Strategy) | 296 | 2025-11-12 | qmt/okx/joinquant/ML/AI/Qlib 策略合集 |

**关键判断**：聚宽/米筐的"策略迁移"在中文社区**没有形成有规模的开源项目**。star 最高的相关项目是 easytrader（10217⭐），但它做的是**交易执行桥接**（跟踪模拟盘下单），不是**策略语义迁移**。这意味着 **lquant 的 `research/dialect/` 是一块罕见的、已被自建的差异化能力**——见 §4。

### 1.5 中文 K 线可视化生态

| 项目 | 仓库 | Star | 最后推送 | 语言 | 说明 |
|---|---|---|---|---|---|
| **pyecharts** | [pyecharts/pyecharts](https://github.com/pyecharts/pyecharts) | 15773 | 2026-08-04 | Python | ECharts Python 封装，Kline 组件是中文图表生态的默认选项 |
| **KLineChart** | [klinecharts/KLineChart](https://github.com/klinecharts/KLineChart) | 4218 | 2026-09-30 | TypeScript | 专业金融图表库，Apache-2.0 |
| **KLineChart Pro** | [klinecharts/pro](https://github.com/klinecharts/pro) | 342 | 2026-07-26 | TypeScript | 开箱即用的成品版 |
| **kline-charts-react** | [chengzuopeng/kline-charts-react](https://github.com/chengzuopeng/kline-charts-react) | 25 | 2026-08-08 | TypeScript | 基于 ECharts 的 React 股票 K 线组件 |

---

## 2. 重点项目深挖

> **诚实声明**：本轮 README 原文读取工具故障，下列"独特机制"多数来自仓库 description 与搜索摘要，已逐条标 `[机制-未逐行核实]`。采信前请回读源码。

### 2.1 zvt（zvtvz/zvt）— 数据+因子+选股+回测一体化

**元数据** `[已核实]`：[zvtvz/zvt](https://github.com/zvtvz/zvt) · 4313⭐ · 1016 fork · Python · MIT · 最后推送 **2026-07-01** · 未归档 · 23 个 open issues。
证据：[仓库页](https://github.com/zvtvz/zvt)、[提交历史](https://github.com/zvtvz/zvt/commits/master)

**维护状态**：`维护中（低频）`。2026-07-01 有推送，说明**未死**；但 3 个月 + 一次的节奏、23 个 open issues，属于"低强度维护"。MIT 协议对借鉴友好。

**独特机制** `[机制-未逐行核实]`：
- **schema + recorder 双层抽象**——zvt 的核心设计是把"数据实体的字段定义（schema）"与"抓取入库过程（recorder）"解耦。这是它相对同期 akshare 类"函数式取数"最大的结构差异：数据实体是一等公民，可被因子层直接引用。
- **统一实体图**：股票 / 板块 / 指数 / 财报 / 分红 / 龙虎榜等都被建模成实体 + 关系，因子与选股在实体图上做 join，而不是散落的 DataFrame。
- **一体化流水线**：取数 → 因子 → 选股 → 回测在同一套抽象里，不需要跨库数据搬运。

**坑与教训** `[机制-未逐行核实]`：
- **pandas 时代的架构债**：zvt 生于 2019，全栈 pandas + SQLAlchemy。在 Polars/DuckDB 时代，其"全表 DataFrame + ORM 写库"是明确的性能与内存瓶颈。
- 23 个 open issues 长期挂起、3 个月级推送节奏 → **不要把它当作依赖，要当作设计参考**。
- 依赖 SQLAlchemy 1.x 风格写法，数据库迁移成本高。

**对 lquant 可借鉴点**：
| 建议 | lquant 是否已有 |
|---|---|
| schema + recorder 解耦，数据实体一等公民 | ✅ **已有**。`data/schema.py`（内部统一 schema + coerce）+ `data/base.py`（Provider 抽象 6 方法）+ `data/capability.py`。lquant 的抽象粒度比 zvt 更细（多了 Capability 与 Fallback） |
| 实体图统一 join | ⚠️ **部分**。lquant 有 `security/`（contract/loader/angles/score）与 `fundamental/panel.py`，但没有 zvt 那种显式"实体-关系图"建模 |
| 一体化流水线 | ✅ **已有**。`cli/` 的 `data/factor/backtest/strategy` 已经是端到端 |

> **结论：zvt 不值得作为依赖引入，其设计思想 lquant 已基本覆盖且实现更现代。**

---

### 2.2 czsc（waditu/czsc）— 缠论信号系统 ⭐ 本组最重要发现

**元数据** `[已核实]`：[waditu/czsc](https://github.com/waditu/czsc) · **6385⭐** · 1767 fork · **语言字段 = Rust** · License NOASSERTION（自定义，需逐案确认） · 最后推送 **2026-10-08（当日）** · 未归档 · 20 open issues。
证据：[仓库页](https://github.com/waditu/czsc)、[提交历史](https://github.com/waditu/czsc/commits/master)

**维护状态**：**非常活跃**（当日仍有推送）。**重大信号：主语言字段已变为 Rust**——这是本组最值钱的发现之一，说明 czsc 团队在这轮（约 2025-2026）做了**从 pandas/Python 到 Rust 的核心重写**，用性能换掉了 pandas 全表的历史债。这与 lquant 的 "Python + Rust" 技术栈方向**完全同构**，因此其 Rust 边界的划法极具参考价值。
⚠️ **License = NOASSERTION**：GitHub 无法识别其 license 文件为标准协议，**商用前必须人工读 LICENSE 原文**。

**独特机制** `[机制-未逐行核实]`：
- **信号-持仓-事件三层分层模型**（社区最常引用的设计）：
  - **信号（Signal）**：无状态的、可独立验证的判断（如"某级别出现底分型 + MACD 背驰"）。
  - **持仓（Position）**：由信号组合推导出的持仓状态机，回答"现在该持多/持空/空仓"。
  - **事件（Event）**：对持仓变化的归因记录，回答"为什么在此时改变了持仓"。
  这一层级的价值在于**可解释性与可回测性分离**：信号可以单独做统计验证（胜率/赔率），不必先跑完整回测。
- **缠论的结构化表达**：把"笔、线段、中枢、买卖点"做成可序列化的对象，而非散落在 DataFrame 列里。
- **Rust 化核心**：性能敏感的结构识别下沉到 Rust，Python 只做编排。
- 生态延伸：搜索中出现 [czsc_skills](https://raw.githubusercontent.com/zengbin93/czsc_skills/main/README.md)（技能包/Agent 化尝试），说明作者在往 LLM Agent 方向延伸 `[机制-未逐行核实]`。

**坑与教训** `[机制-未逐行核实]`：
- **缠论歧义是根本性风险**："千人千缠"——笔/线段的划分规则在不同实现间不一致，**同一份 K 线在两个库里可能得出不同买卖点**。任何缠论模块都必须声明自己遵循的划分规则版本。
- **License 不明确**（NOASSERTION）：不能默认可商用。
- 缠论算法**天然涉及未来函数风险**：中枢的确认需要后续 K 线，实盘中只能"事后确认"，回测里极易引入前视偏差。lquant 已有 `indicators/future.py` 的 `assert_no_lookahead` 前缀不变性判据，正是治理这个问题的正确工具。

**对 lquant 可借鉴点**：
| 建议 | lquant 是否已有 |
|---|---|
| **信号 / 持仓 / 事件三层分离** | ❌ **真正缺失**。lquant 有 `backtest/events.py`（Order/Fill/Bar）与 `indicators/registry.py`，但**没有"可独立统计验证的无状态信号"与"持仓状态机"的分层**。lquant 的策略接口是 `backtest/strategy/factor_topn.py` 式的直接选股，缺少 czsc 那种中间层。**建议借鉴** |
| **缠论结构对象化（笔/线段/中枢）** | ❌ **真正缺失，且 lquant 自己承认**。`indicators/__init__.py` 原文写明：「形态类不塞进因子 DSL：DSL 的算子必须是可静态推导 min_window 的窗口化纯函数，**表达不了中枢/浪型这类分段结构**」。这是 lquant 已知的能力边界 |
| **Rust 化结构识别核心** | ⚠️ **部分**。lquant 已有 `crates/lq-ops`、`lq-backtest`、`lq-metrics` 的 Rust 边界，但**几何结构算法**尚未 Rust 化。czsc 的重写路径可作先例参考 |
| 缠论歧义声明 + 未来函数治理 | ✅ **未来函数治理已有**（`indicators/future.py` / `assert_no_lookahead`）；❌ **划分规则版本声明机制缺失** |

> **结论：czsc 是本组第一优先借鉴对象。借的是「信号-持仓-事件」分层，不是缠论细节。**

---

### 2.3 Qbot（UFund-Me/Qbot）— AI 量化投研平台

**元数据** `[已核实]`：[UFund-Me/Qbot](https://github.com/UFund-Me/Qbot) · **18578⭐** · 2603 fork · Jupyter Notebook · MIT · 最后推送 **2026-03-11** · 未归档 · **76 个 open issues**。
证据：[仓库页](https://github.com/UFund-Me/Qbot)、[提交历史](https://github.com/UFund-Me/Qbot/commits/main)、[官方文档](https://ufund-me.github.io/Qbot)

**维护状态**：**半停滞**。声称 `[🔥updating ...]`，但实测最后推送 2026-03-11，**约 7 个月无提交**，且 **76 个 open issues** 积压。star 18578 与实际活跃度严重背离——典型"高 star 僵尸项目"。Jupyter Notebook 为主语言也说明它更像**演示集合**而非工程化产品。
另注：README 提到后继 `qbot-mini` → [Charmve/iQuant](https://github.com/Charmve/iQuant)，说明作者注意力已转移 `[机制-未逐行核实]`。

**独特机制** `[机制-未逐行核实]`：
- **AI 投研编排**：定位是"AI 自动量化交易机器人 + 完全本地部署"，把数据、模型、策略、回测、交易串成一条 Notebook 链。
- **本地化**：强调全本地部署，不依赖云端。
- 覆盖 A 股与数字货币（近年 Qbot 大幅转向 crypto）。

**坑与教训**：
- **76 issues + 7 个月停更**：不要引入。
- **Jupyter Notebook 为主**：无版本化 API、无测试、无包管理边界，**工程债极重**。
- star 数与可用性脱钩，是"营销驱动"项目的教科书案例。

**对 lquant 可借鉴点**：
| 建议 | lquant 是否已有 |
|---|---|
| AI 投研编排（数据→模型→策略→回测→交易） | ✅ **已有且更扎实**。lquant 有 `agent/`（MCP server、A2A、claude_code/codex 接入）、`factors/mining/llm.py`、任务中心（RQ + WebSocket + `monitor/`） |
| 完全本地部署 | ✅ **已有**（docker-compose + 本地 DuckDB/Parquet） |

> **结论：不必借鉴。这是「高 star 低可用」的负面样本，值得写进调研作为反例。**

---

### 2.4 chan.py（Vespa314/chan.py）— 缠论 Python 实现框架

**元数据** `[已核实]`：[Vespa314/chan.py](https://github.com/Vespa314/chan.py) · 2196⭐ · 818 fork · Python · MIT · 最后推送 **2026-09-24** · 未归档 · 11 open issues。
证据：[仓库页](https://github.com/Vespa314/chan.py)、[quick_guide.md](https://github.com/Vespa314/chan.py/blob/main/quick_guide.md)

**维护状态**：**活跃**（2026-09-24，约 2 周前）。MIT 协议，**可自由借鉴代码**——这是本组协议最友好的缠论实现。

**独特机制**（来自仓库 description，`[机制-未逐行核实]`）：
- 自我定位为"**开放式的**缠论 python 实现框架"——强调可扩展而非黑盒。
- 明确列出：**形态学 / 动力学买卖点分析计算**、**多级别 K 线联立**、**区间套策略**、可视化绘图、多种数据接入、策略开发、交易系统对接。
- "多级别联立 + 区间套"是缠论里最难工程化的部分：需要在多个时间级别上同时维护笔/线段/中枢，并处理级别间的对应关系。

**坑与教训** `[机制-未逐行核实]`：
- Python 实现的几何结构识别在长历史回测时**性能吃紧**（逐 bar 更新结构）。
- 缠论划分规则与 czsc 可能不一致 → 交叉验证成本高。
- 需要与数据层对接，`quick_guide.md` 的存在说明上手有门槛。

**对 lquant 可借鉴点**：
| 建议 | lquant 是否已有 |
|---|---|
| 多级别 K 线联立 / 区间套 | ❌ **真正缺失**。lquant 的因子 DSL 是**单级别窗口化**的（`min_window` 静态推导），无法表达跨级别结构 |
| 形态学 / 动力学买卖点 | ❌ **真正缺失**（同 §2.2） |
| 开放式可扩展的缠论框架设计 | ❌ 缺失，但**优先级低于 czsc 的分层模型** |

---

### 2.5 其他缠论实现

**chanlun-pro** `[已核实]`：[yijixiuxin/chanlun-pro](https://github.com/yijixiuxin/chanlun-pro) · 1053⭐ · Python · Apache-2.0 · 最后推送 **2026-10-03**（活跃）。定位"基于缠中说禅所讲缠论理论，以便量化分析市场行情的工具"。Apache-2.0 友好。`[机制-未逐行核实]`

**ChanlunX** `[已核实]`：[kldcty/ChanlunX](https://github.com/kldcty/ChanlunX) · 420⭐ · **C++** · MIT · 最后推送 **2026-04-29**。定位"缠中说禅炒股缠论可视化插件"。**C++ 实现 + MIT** 值得关注——若 lquant 要把几何结构 Rust 化，这是可参考的 C++ 对照实现。`[机制-未逐行核实]`

**chanvis** `[已核实]`：[ibaihuo/chanvis](https://github.com/ibaihuo/chanvis) · 620⭐ · Vue · BSD-3-Clause · 最后推送 **2024-07-02** → **停滞 2 年+**。定位"基于 TradingView 本地 SDK 的可视化前后端代码"。**不必借鉴**（停滞 + 依赖 TradingView 本地 SDK 有授权风险）。

**其他**：搜索还发现 [tomcat123a/-chanlun](https://github.com/tomcat123a/-chanlun)（568⭐，2024-04-02，个人脚本级，"笔和线段的一种划分.py"）→ 工程等级不足，仅作规则参考。

---

### 2.6 Ashare（mpquant/Ashare）— 极简 A 股行情库

**元数据** `[已核实]`：[mpquant/Ashare](https://github.com/mpquant/Ashare) · **3894⭐** · 668 fork · Python · **无 License** · 最后推送 **2025-12-24** · 未归档 · 17 open issues。
⚠️ **任务清单里的 `myhhub/Ashare` 是 404**，真身是 `mpquant/Ashare`。

**维护状态**：`维护中（低频）`——约 10 个月未推送，但这类"单文件极简库"本身不需要频繁改。

**独特机制** `[机制-未逐行核实]`（描述来自仓库 description）：
- **极简哲学**：单文件、百行级、`get_price` 一个函数搞定。
- **新浪 + 腾讯双数据核心**，**自动故障切换**——这是它的核心卖点，比单源实现更抗封。
- 输出直接是 DataFrame，为量化研究者"极大减轻数据获取工作量"。

**坑与教训**：
- **无 License**：法律上默认"保留所有权利"，**不可随意复制代码**。
- 依赖新浪/腾讯的**非公开 Web 接口**——这是最脆弱的依赖类型，接口一变就全线失效（参见 §2.7 的 akshare-proxy-patch 现象）。
- 双源故障切换是好设计，但**无质量断言**：切源后字段/单位可能静默不一致。

**对 lquant 可借鉴点**：
| 建议 | lquant 是否已有 |
|---|---|
| 新浪/腾讯双源自动故障切换 | ✅ **已有且更强**。lquant 的 `data/fallback.py` 是 `FallbackProvider + HealthTracker`，链路是 BaoStock(主) → akshare → efinance → 同花顺 → mootdx，且**带 Capability 声明**（不支持就抛 `CapabilityMissing`，绝不静默返回空） |
| 极简单函数取数 API | ⚠️ 取舍不同。lquant 走的是工程化 Provider 抽象，**不应为极简牺牲可维护性** |

> **结论：Ashare 的双源切换思想 lquant 已覆盖且实现更严谨；无 License 使其不可复制。不必借鉴。**

---

### 2.7 pythonstock/stock — akshare + pyecharts 的股票分析 Web 平台

**元数据** `[已核实]`：[pythonstock/stock](https://github.com/pythonstock/stock) · **7879⭐** · 2358 fork · Python · Apache-2.0 · 最后推送 **2026-05-07** · 未归档 · **78 open issues**。
注意：搜索结果里满是 fork（[KudoShini/pythonstock](https://github.com/KudoShini/pythonstock)、[liangyuanxuan/pythonstock](https://github.com/liangyuanxuan/pythonstock)），说明**已被大量二次开发**，也说明原仓库活跃度不足。

**维护状态**：`维护中`（2026-05-07，约 5 个月前），但 **78 open issues** 积压严重，属"半维护"。

**独特机制** `[机制-未逐行核实]`：
- **数据采集 → 存储（MySQL）→ Web 展示**的完整流水线，Docker 化交付。
- 用 akshare 取数 + pyecharts 出图，是"中文个人量化看板"最常见的模板。
- 采集与展示分离（定时任务 + Web 层）。

**坑与教训**：
- **MySQL 作为时序存储**：对行情数据是错误选型（列式 Parquet/DuckDB 才是正解），随历史增长性能塌陷。
- **78 open issues**：大量是"接口失效/跑不起来"类问题——**akshare 接口漂移**是这类项目的头号死因。
- Docker 镜像易过期，依赖 pandas 老版本。

**对 lquant 可借鉴点**：
| 建议 | lquant 是否已有 |
|---|---|
| 采集-存储-展示 三层分离 | ✅ **已有且更现代**（Provider/ingest → Parquet+DuckDB → Next.js） |
| Docker 一键交付 | ✅ **已有**（`docker-compose.yml` + `scripts/setup.sh` + `deploy/`） |
| **图表层用 pyecharts** | ⚠️ lquant 用 Next.js + ECharts。pyecharts 是 Python 侧渲染，**不适合 lquant 的前后端分离架构**。不必借鉴 |

---

### 2.8 InStock — ⚠️ 未找到真身

**元数据**：`myquant/InStock` → **404 Not Found**。
搜索到的最接近者：
- [gitkkkk/instock](https://github.com/gitkkkk/instock) — 38⭐ · 最后推送 2025-08-28 · 无语言标记 · 无 License。描述与清单高度吻合（"stock股票.获取股票数据,计算股票指标,筹码分布,识别股票形态,综合选股,选股策略,股票验证回测,股票自动交易,支持PC及移动设备"）。
- [opensamai/InStock](https://github.com/opensamai/InStock) — **1⭐** · fork · 描述完整。
- [webclinic017/InStock](https://github.com/webclinic017/InStock) — fork。
- [zhouxiaohao-svg/stock-deployment-guide](https://github.com/zhouxiaohao-svg/stock-deployment-guide) — 0⭐，"InStock 股票系统本地部署配置指南"，侧面证明原项目**曾存在且被部署过，但主仓库已消失**。

**状态判定**：`未找到原仓库，已失效/迁移`。影响力残留在 fork 与部署指南中，但**主仓库不可访问**，无法评估 star 与维护状态。
**建议**：**不必继续追**。原项目为 Flask/MySQL 技术栈的老式 Web 平台，lquant 架构已全面超越。

---

### 2.9 qstock（tkfy920/qstock）— 数据/可视化/选股/回测一体化包

**元数据** `[已核实]`：[tkfy920/qstock](https://github.com/tkfy920/qstock) · **1945⭐** · Python · MIT · 最后推送 **2025-03-16** · 未归档。
证据：[仓库页](https://github.com/tkfy920/qstock)

**维护状态**：**停滞（约 1.5 年无推送）**。作者定位"由『Python金融量化』公众号开发，试图打造成个人量化投研分析包"，包含 data / plot / stock（选股）/ backtest 四大模块。`[机制-未逐行核实]`

**坑与教训**：
- **公众号驱动的项目生命期**：内容输出停止即代码停止。
- MIT 协议友好，但停滞 + 依赖 akshare 老接口 → 实际可能已跑不通。
- 四大模块全部塞在一个 pip 包里，**边界不清晰**（数据/可视化/选股/回测耦合），工程上不可维护。

**对 lquant 可借鉴点**：❌ **基本没有**。lquant 的同名模块（data / factors / portfolio / backtest）拆解更清晰，且用 Polars/DuckDB 而非 pandas。
**唯一可留意点**：其"选股 + 回测一体化"的**面向个人用户的 API 手感**（少参数、开箱即用）值得借鉴为 lquant 的**交互层设计参考**，而非架构参考。

---

### 2.10 stock-scanner（DR-lin-eng/stock-scanner）— 开源 A 股量化分析 + LLM

**元数据** `[已核实]`：[DR-lin-eng/stock-scanner](https://github.com/DR-lin-eng/stock-scanner) · **1150⭐** · Python · MIT · 最后推送 **2026-03-02** · 未归档。
证据：[仓库页](https://github.com/DR-lin-eng/stock-scanner)

**维护状态**：`维护中（约 7 个月前）`。

**独特机制** `[机制-未逐行核实]`：描述为"开源 A 股量化分析（并且配合 llm 模型，进行高级分析）"——即**规则/指标筛选 + LLM 深度解读**的二段式：程序先做可计算的过滤，LLM 再做归因与叙事。这个"定量筛选 → 定性解读"的分工是务实做法（避免让 LLM 直接做数值筛选）。

**对 lquant 可借鉴点**：
| 建议 | lquant 是否已有 |
|---|---|
| "定量筛选 → LLM 解读"二段式 | ✅ **已有**。lquant 有 `factors/mining/llm.py` + `agent/`（MCP）+ `news/`（舆情）三段素材，且因子 DSL 保证数值筛选可复现 |
| 选股结果自动推送（飞书等） | ❌ **缺失**（lquant 有 `monitor/` 与任务中心，但无外部 IM 推送通道）。参见 §2.11 Sequoia-X |

---

### 2.11 补漏高价值项目

#### Sequoia-X（sngyai/Sequoia-X）— A 股技术形态自动扫描
`[已核实]`：7976⭐ · Python · **无 License** · 最后推送 2026-07-10 · 未归档。
定位"A股自动选股系统 — 多种技术形态自动扫描，收盘后自动运行并推送飞书"。
**可借鉴点**：**「收盘后定时全市场扫描 + 飞书推送」的闭环**——这正是 lquant `market/scheduler.py` 采集时刻表的自然延伸。lquant **缺外部推送通道**（飞书/钉钉/企业微信），这是一个**低成本高感知**的补漏项。⚠️ 无 License，只能借思路。

#### tick-stock-panel（shy3130/tick-stock-panel）— A 股选股+监控+回测工作台
`[已核实]`：**5717⭐** · Python · MIT · 最后推送 **2026-10-08（当日）** · 未归档。
定位"TSP 自托管、零运维的 A 股「选股 + 监控 + 回测」量化工作台 | LLM 能力驱使策略定制+个股分析+复盘 | 自由接入第三方数据源"。
**为什么重要**：**定位与 lquant 高度重叠**（选股+监控+回测+LLM+自托管），且**当日推送、5717⭐、MIT**。这是本组**最值得做竞品对标**的项目，而非借鉴对象。建议后续单独做一次功能矩阵对比。
`[机制-未逐行核实]`

#### QuantMind（qusong0627/QuantMind）— AI 原生多市场量化平台
`[已核实]`：1719⭐ · Python · **AGPL-3.0（传染性，不可直接抄代码）** · 最后推送 **2026-10-08（当日）**。
定位"面向个人开发者与投研团队的 AI 原生多市场量化交易平台。深度集成微软 Qlib、RD-Agent 因子演化与 QuantBot 全能工作流"。
**可借鉴点**：**RD-Agent 式「因子自动演化」** 与 lquant `factors/mining/gp.py`（遗传规划）+ `llm.py` 同赛道。⚠️ AGPL-3.0 意味**只能读思路不能抄实现**。

#### TradingAgents-CN（hsliuping/TradingAgents-CN）— 中文多智能体交易框架
`[已核实]`：**32215⭐**（本组 star 最高）· Python · NOASSERTION · 最后推送 2026-09-22。
定位"基于多智能体 LLM 的中文金融交易框架 - TradingAgents 中文增强版"。
**为什么重要**：**多智能体辩论式投研**（分析师/研究员/交易员/风控角色分工 + 对抗辩论）是 lquant `agent/` **明确缺失的编排层**——lquant 有单 Agent + MCP + A2A，但没有角色分工与辩论协议。
相关：[KylinMountain/TradingAgents-AShare](https://github.com/KylinMountain/TradingAgents-AShare)（856⭐，2026-09-18，"15 名 AI Agent 模拟机构协作与实时辩论对抗"）、[TNT-Likely/PanWatch](https://github.com/TNT-Likely/PanWatch)（2042⭐，2026-10-05，基于 TradingAgents 的盯盘）。
⚠️ **NOASSERTION License，商用前需读原文**。⚠️ 这类框架普遍是 **README 驱动 + 依赖外部 LLM API key**，真实可运行性需实测。

#### akquant（akfamily/akquant）— akshare 官方 Rust+Python 量化框架
`[已核实]`：2398⭐ · Python · MIT · 最后推送 2026-09-28 · 未归档。
定位"AKQuant is a high-performance quantitative research and trading framework built on Rust and Python!"。
**为什么重要**：**akshare 官方下场做 Rust+Python 框架**，与 lquant 技术栈同构。且 lquant 的 `docs/ARCHITECTURE.md` 已在回测规则设计中引用过 akquant（`MarketModel`、`fund.rs` 独立印证），说明**前四路已部分覆盖**，此处仅作状态更新：**仍活跃、MIT、值得持续跟踪**。

#### akshare-proxy-patch（HelloYie/akshare-proxy-patch）— 接口失效猴子补丁
`[已核实]`：192⭐ · Python · NOASSERTION · 最后推送 **2026-10-06**。
定位"针对 akshare 和 efinance 的🐒补丁插件，解决 `stock_zh_a_spot_em`、`stock_zh_a_hist`、`get_realtime_quotes` 等接口报错问题"。
**为什么重要**：这是**「中文数据源接口漂移」的活体证据**——一个 192⭐ 的独立项目专门用来打补丁，说明 akshare/efinance 的接口失效是**常态而非事故**。这**反向验证了 lquant 的 `data/quality/crosscheck.py`（跨源对拍）+ `fallback.py`（Capability + HealthTracker）+ `akshare.py` adapter 的必要性**。建议 lquant 定期跟踪此仓库的补丁内容，作为接口变更的早期预警。

#### mootdx（mootdx/mootdx）— 通达信本地数据读取
`[已核实]`：2413⭐ · Python · 无 License · 最后推送 **2024-07-16** → **停滞约 2 年**。
lquant 已把它列入 fallback 链的实时源（`market/providers/mootdx.py`）。⚠️ **风险提示**：上游已 2 年未更新，通达信协议若变更则 lquant 的该源会静默失效，**必须依赖 HealthTracker 兜住**。

#### pytdx（rainx/pytdx）— **已归档**
`[已核实]`：1552⭐ · Python · 无 License · 最后推送 2020-04-15 · **archived: true**。
**已归档 6 年**。mootdx 是它的封装，两者同源衰落。**明确不建议依赖**。

#### adata（1nchaos/adata）· efinance（Micro-sheep/efinance）
- [1nchaos/adata](https://github.com/1nchaos/adata)：5261⭐ · Apache-2.0 · 最后推送 2025-12-26（~10 个月前）。`[机制-未逐行核实]`
- [Micro-sheep/efinance](https://github.com/Micro-sheep/efinance)：4080⭐ · 无 License · 最后推送 **2026-07-17**（活跃）。lquant 已列入 fallback 链。

#### tushare（waditu/tushare）— **主包已闭源**
`[已核实]`：[waditu/tushare](https://github.com/waditu/tushare) · **15447⭐** · BSD-3-Clause · 最后推送 **2024-03-13** · **未归档但停滞 ~2.5 年**。
**关键状态变化**：新版 tushare 已迁移到 **tushare.pro 闭源 + 积分制**（社区普遍反映免费额度大幅收紧）。GitHub 上的 15447⭐ 仓库停留在 **2024-03**，对应的是**已过时的开源老版本**。
**结论**：**star 数严重误导**——15447⭐ 指向的是一个不再演进的旧版本。任何"参考 tushare"的设计都需注意：**API 契约仍在变，但公开代码不跟了**。

#### MyTT（mpquant/MyTT）— 通达信/同花顺公式的 Python 实现
`[已核实]`：[mpquant/MyTT](https://github.com/mpquant/MyTT) · **2872⭐** · Python · **无 License** · 最后推送 **2026-06-13** · 未归档。
定位（来自仓库 description）："MyTT 将通达信, 同花顺, 文华麦语言等指标公式, **最简移植到 Python 中, 核心库单个文件，仅百行代码, 十几个核心函数**，神奇的实现所有常见技术指标算法（**不依赖 talib 库**）的纯 python 实现"。

**维护状态**：`维护中（低频）`，2026-06-13 有推送。

**独特机制** `[机制-未逐行核实]`：
- **公式兼容层**：目标不是"实现指标"，而是**让 TDX/同花顺/麦语言的公式语义能在 Python 里直接跑**。这是它相对 talib 的根本差异——talib 提供固定函数库，MyTT 提供**语义映射层**。
- **零依赖 + 单文件 + 百行**：不依赖 talib（避免 C 扩展安装问题），纯 Python/numpy。
- **覆盖范围**：A 股社区现成的海量 TDX 公式（选股公式、指标公式）可以低成本复用到 Python。

**坑与教训**：
- **性能是明确短板**：纯 Python + 逐公式翻译，在**全市场 × 长历史**场景下会成为瓶颈。lquant 若引入必须走 Polars/Rust 重写，不能直接用。
- **无 License**：不可复制代码。
- **公式体系本身含未来函数**：TDX 公式里 `ZIG`、`PEAK`、`TROUGH` 等函数天然使用未来数据。**直接搬运 TDX 选股公式到回测里是经典的前视偏差来源**。lquant 的 `assert_no_lookahead` 前缀不变性判据恰好能拦住这类函数——这是一个**天然的整合点**。
- 搜索还发现衍生实现：[jackiotyu/MyTT-ts](https://github.com/jackiotyu/MyTT-ts)（TypeScript 版，5⭐，2024-12-15），说明公式兼容层有跨语言需求。
- **未找到 MyTT2**：任务清单提到 "MyTT / MyTT2"，搜索仅命中 [jangviktor-web/a-stock-data-quant](https://github.com/jangviktor-web/a-stock-data-quant)（34⭐，基于 akshare + MyTT 的上层工具），**未发现名为 MyTT2 的独立项目** → 标注 `未找到`。
- **TDX 公式解析器**：本轮未找到有规模的独立开源解析器项目（`[未核实]`）。MyTT 事实上承担了这个角色，但它是**手写移植而非解析器**——即它不理解 TDX 公式文本，只是把常见公式人工翻译成 Python 函数。**"能解析 TDX 公式文本"的开源项目在中文社区仍是空白**。

---

## 3. 全组「不必借鉴」清单

| 项目 | 理由 |
|---|---|
| **Qbot** (18578⭐) | **7 个月无推送 + 76 open issues**，Jupyter Notebook 为主，无工程边界。典型高 star 僵尸项目 |
| **qstock** (1945⭐) | 停滞 ~1.5 年，公众号驱动，四大模块耦合在一个包里，依赖已漂移的 akshare 旧接口 |
| **TradingGym** (1921⭐) | 停滞 ~2.5 年（2024-02），RL 交易环境，与 lquant 的日频选股定位不符 |
| **chanvis** (620⭐) | 停滞 2 年+，依赖 TradingView 本地 SDK（授权风险） |
| **zipline-chinese** (692⭐) | **2017 年最后推送，已废弃 9 年** |
| **pytdx** (1552⭐) | **GitHub 已归档（archived: true）**，2020 年后无更新 |
| **mootdx** (2413⭐) | 停滞 ~2 年；lquant 已接入但**必须视为高风险源**，靠 HealthTracker 兜底 |
| **InStock** (myquant) | 原仓库 404，真身仅 38⭐；老式 Flask/MySQL 架构 |
| **Ashare** (mpquant) | 双源切换 lquant 已覆盖且更严谨；**无 License 不可复制** |
| **pythonstock/stock** (7879⭐) | MySQL 存时序是错误选型；78 open issues；pyecharts 与 lquant 前后端分离架构不兼容 |
| **tushare** (waditu, 15447⭐) | GitHub 仓库停滞 ~2.5 年，主包已闭源迁移，**star 数严重误导** |
| **zvt** (4313⭐) | 设计思想 lquant 已覆盖且实现更现代；pandas+SQLAlchemy 架构债 |
| **Sequoia-X / MyTT / TradingAgents-CN / QuantMind** | **无 License 或 NOASSERTION / AGPL-3.0**——思路可读，**代码不可抄**（Sequoia-X 无 License、MyTT 无 License、TradingAgents-CN NOASSERTION、QuantMind AGPL-3.0 传染） |

---

## 4. Top 可借鉴项（按优先级，含「lquant 是否已有」判断）

| # | 借鉴项 | 来源 | lquant 是否已有 | 缺口具体在哪 |
|---|---|---|---|---|
| **1** | **信号-持仓-事件 三层分离** | czsc | ❌ **真正缺失** | lquant 有 `backtest/events.py`（Order/Fill/Bar）与 `indicators/registry.py`，但**没有"可独立统计验证的无状态信号"与"由信号推导的持仓状态机"的中间层**。当前 `backtest/strategy/factor_topn.py` 是直接从因子到选股，缺少可单独评估胜率/赔率的信号层 |
| **2** | **几何结构层（笔/线段/中枢/多级别联立）** | chan.py / chanlun-pro / czsc / ChanlunX | ❌ **真正缺失，且 lquant 自己承认** | `indicators/__init__.py` 原文：「DSL 的算子必须是可静态推导 `min_window` 的窗口化纯函数，**表达不了中枢/浪型这类分段结构**」。这是 lquant 明确的能力边界 |
| **3** | **通达信公式兼容层** | MyTT | ❌ **真正缺失** | lquant 的因子 DSL 是自研语法（lexer/parser/analyzer/compiler），**无法消费中文社区海量现成 TDX 公式**。这是一个"生态接入"缺口，不是"功能"缺口。⚠️ 必须配套 `assert_no_lookahead` 拦截 `ZIG/PEAK/TROUGH` 类未来函数 |
| **4** | **多智能体辩论式投研编排** | TradingAgents-CN / TradingAgents-AShare / PanWatch | ❌ **真正缺失** | lquant 有 `agent/`（`mcp_server.py`、`a2a/`、`claude_code.py`/`codex.py`、`sessions.py`、`concurrency.py`）+ `factors/mining/llm.py`，但都是**单 Agent 调用**，**没有分析师/研究员/风控的角色分工与对抗辩论协议** |
| **5** | **收盘后全市场扫描 + 外部 IM 推送闭环** | Sequoia-X | ⚠️ **部分** | lquant 有 `market/scheduler.py` 采集时刻表 + `monitor/`（emit/flusher/worker）+ 任务中心（RQ+WebSocket），但**没有飞书/钉钉/企业微信的外部推送通道**。低成本高感知的补漏项 |
| **6** | **数据源接口漂移的常态化治理** | akshare-proxy-patch | ✅ **已有且更强** | lquant 已有 `fallback.py`（Capability + HealthTracker）、`quality/asserts.py`（8 项记录级断言）、`quality/crosscheck.py`（跨源对拍）、`golden.py`。**这个补丁仓库的价值是给 lquant 提供"上游接口变更预警"**，可作为监控信号源接入 |
| **7** | **Rust 化核心的边界划法** | czsc（Python→Rust 重写）/ ChanlunX（C++） | ⚠️ **部分** | lquant 已有 `crates/lq-ops`（`#[polars_expr]` 插件）、`lq-backtest`、`lq-metrics`，但**几何结构算法与公式解析尚未 Rust 化**。czsc 的重写路径是**验证过可行性的先例** |
| **8** | **RD-Agent 式因子自动演化** | QuantMind（AGPL）/ akquant | ⚠️ **部分** | lquant 已有 `factors/mining/`（`gp.py` 遗传规划、`llm.py`、`explore.py`、`fitness.py`、`gates.py`、`submit.py`、`runner.py`），能力覆盖度已高。缺的是**演化过程的可观测性与多轮迭代编排**。⚠️ QuantMind 是 AGPL-3.0，**只能读思路** |
| 附 | **竞品对标而非借鉴：tick-stock-panel** | shy3130（5717⭐，MIT，当日推送） | — | **定位与 lquant 高度重叠**（A 股选股+监控+回测+LLM+自托管）。建议后续单独做功能矩阵对比 |

### 借鉴优先级建议

1. **先做 #1（信号-持仓-事件分层）**——纯架构改动，零外部依赖，MIT 可参考，直接提升策略可解释性。
2. **再做 #5（外部推送）**——成本最低、用户感知最强、无 License 障碍（借思路即可）。
3. **评估 #3（TDX 公式兼容）**——工作量中等，但需先解决未来函数拦截设计；收益是接入中文公式生态。
4. **#2（几何结构）谨慎推进**——工程量最大，且缠论存在"千人千缠"歧义（czsc/chan.py/chanlun-pro 三者划分规则可能不一致）。建议若做，**必须声明规则版本**并配 `assert_no_lookahead` 前缀不变性测试。
5. **#4（多智能体）**——与 #3 并列，但注意 TradingAgents 系项目普遍是 README 驱动 + 强依赖外部 LLM API key，**真实可运行性必须先实测**。

---

## 5. 本路调研的遗留未核实项（诚实清单）

| 项 | 状态 |
|---|---|
| 各项目 README 原文与源码机制 | `[机制-未逐行核实]`——读取 README 的工具有故障（`gh api .../readme` 解码为空），机制描述多来自仓库 description 与搜索摘要 |
| **MyTT2** | **未找到**独立项目 |
| **TDX 公式文本解析器** | **未找到**有规模的开源项目。MyTT 是手写移植而非解析器——"解析 TDX 公式文本"在中文社区仍属空白（`未核实`是否完全不存在） |
| **myquant/InStock** | **原仓库 404**，未能定位真身与真实 star |
| **joinquant/米筐 策略语义迁移工具** | 未找到有规模项目。star 最高者是 easytrader（10217⭐）但做的是**交易执行桥接**（跟踪模拟盘下单到同花顺/miniqmt/雪球），**不是策略语义迁移** |
| lquant JQ shim 具体缺失哪些 API 语义 | **未核实**。需对照 `research/dialect/mapping.py` 的 `WARNINGS`/`UNSUPPORTED` 与真实聚宽文档逐一比对（`run_daily` / `set_benchmark` / `context` 对象 / `order_target_percent` / `get_fundamentals` / `finance` 查询 / 复权语义 / 停牌处理） |
| TradingAgents-CN 等 LLM 框架的真实可运行性 | **未核实**。需实测依赖安装、API key 配置、是否有测试 |

---

## 6. 证据索引

所有元数据均可通过下列命令在本地复现（需 `gh` 已认证）：

```bash
gh api repos/zvtvz/zvt --jq '"\(.full_name)|⭐\(.stargazers_count)|push:\(.pushed_at)|\(.language)|arch:\(.archived)"'
```

主要证据页面：
- zvt — https://github.com/zvtvz/zvt
- czsc — https://github.com/waditu/czsc · https://github.com/waditu/czsc/commits/master
- Qbot — https://github.com/UFund-Me/Qbot · https://ufund-me.github.io/Qbot
- chan.py — https://github.com/Vespa314/chan.py · https://github.com/Vespa314/chan.py/blob/main/quick_guide.md
- chanlun-pro — https://github.com/yijixiuxin/chanlun-pro
- ChanlunX — https://github.com/kldcty/ChanlunX
- chanvis — https://github.com/ibaihuo/chanvis
- Ashare（真身）— https://github.com/mpquant/Ashare
- pythonstock — https://github.com/pythonstock/stock
- qstock — https://github.com/tkfy920/qstock
- stock-scanner — https://github.com/DR-lin-eng/stock-scanner
- TradingGym（真身）— https://github.com/Yvictor/TradingGym
- tushare（真身）— https://github.com/waditu/tushare
- MyTT — https://github.com/mpquant/MyTT · MyTT-ts https://github.com/jackiotyu/MyTT-ts
- KLineChart — https://github.com/klinecharts/KLineChart · https://github.com/klinecharts/pro
- pyecharts — https://github.com/pyecharts/pyecharts
- TradingAgents-CN — https://github.com/hsliuping/TradingAgents-CN
- TradingAgents-AShare — https://github.com/KylinMountain/TradingAgents-AShare
- PanWatch — https://github.com/TNT-Likely/PanWatch
- tick-stock-panel — https://github.com/shy3130/tick-stock-panel
- QuantMind — https://github.com/qusong0627/QuantMind
- akquant — https://github.com/akfamily/akquant
- Sequoia-X — https://github.com/sngyai/Sequoia-X
- akshare-proxy-patch — https://github.com/HelloYie/akshare-proxy-patch
- mootdx — https://github.com/mootdx/mootdx · pytdx（已归档）https://github.com/rainx/pytdx
- adata — https://github.com/1nchaos/adata · efinance https://github.com/Micro-sheep/efinance
- jqfactor_analyzer — https://github.com/JoinQuant/jqfactor_analyzer
- easytrader — https://github.com/shidenggui/easytrader
- zipline-chinese（已废弃）— https://github.com/zhanghan1990/zipline-chinese
- DeepTraderV2 — https://github.com/Lihw99/DeepTraderV2

---

*报告生成：2026-10-08 · lquant 竞品调研第五路（中文生态查漏补缺）*
