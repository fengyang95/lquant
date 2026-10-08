# D · 开源 LLM/多智能体驱动的股票分析与量化决策平台调研

- 日期：2026-10-08
- 调研人：AI+量化交叉领域调研员
- 本仓现状基线：[`docs/AGENT_MODEL.md`](../../AGENT_MODEL.md)（「问 AI / A2A」= 无头 CLI 执行体 + skill + MCP）
- 本报告定位：为 lquant 的 `agent` 模块提供**外部参照系 + 借鉴清单 + 负面清单**
- 证据口径：GitHub REST API 元数据（star/许可证/最后推送）、各仓 README 原文、论文 arXiv 摘要与 HTML 正文、GitHub Issue 正文。所有关键结论附可点击 URL。**star 数与推送时间取 2026-10-08 快照。**

---

## 0. 结论摘要（先读这一节）

### 0.1 一句话结论

> **这整个赛道在 2024–2026 经历了一轮「架构狂欢 → 打假 → 收敛到可审计的模块化」的完整周期。**
> 到 2026 年，做得最认真的项目（TradingAgents、FinRobot V2、LLM_QUANT_FACTORY、FactorGPT、RD-Agent(Q)）**都在做同一件事：把 LLM 从「决策者」降级为「信息接口 / 假设提出者」，把最终决策交给确定性组件，并为此补上时点完整性、审计日志、隐藏测试集与成本核算。**
> 而最容易传播的项目（各种「AI 巴菲特」「AI 炒股」demo）恰恰**没有任何一项**这些机制。

### 0.2 三条对 lquant 最重要的判断

1. **lquant 的 agent 架构（CLI 执行体 + skill + MCP）在「工具契约」这一维度上不落后，但在「可审计性」上落后一个数量级。**
   实测事实：lquant 的 `tool_call` / `tool_result` 事件**不落库**（只有正文落库，见 [`AGENT_MODEL.md`](../../AGENT_MODEL.md) §过程展示）。也就是说，今天「问 AI」给出一条结论，**事后无法回答它读了哪些数据、在什么时点读的**。而 Alpha Illusion 的 P1（Temporal integrity）明确要求「record prompts, retrieved documents or identifiers, tool outputs, and decision timestamps」——这是**部署级结论的入场券**，不是可选项（[arXiv:2605.16895](https://arxiv.org/abs/2605.16895)）。

2. **「LLM 选股不可回测」不是伪问题，而是已被量化证明的结构性问题。**
   前视污染有两条独立通道（模型权重里的参数化记忆 + 检索/工具引入的外部信息），且**声明的知识截止日期不可信**（[arXiv:2602.14233](https://ar5iv.labs.arxiv.org/html/2602.14233) §2.1）。实证幅度：跨过预训练 cutoff 后，**FinMem 总收益下降 ≈71.85%、QuantAgent 的 Sharpe 下降 ≈51.48%**（Alpha Illusion 引 Li et al. 2025，[arXiv:2605.16895](https://arxiv.org/html/2605.16895v1) §2.2）。这个量级意味着：**任何在 cutoff 窗口内跑出的 LLM 选股回测，其数字的主要成分可能就是记忆而非预测。**

3. **最值得 lquant 抄的不是编排，是「纪律」。**
   RD-Agent(Q) 用 **<$10 成本**做到约 2× ARR 且因子数少 70%+（[README](https://github.com/microsoft/RD-Agent)），TradingAgents 的**真实结算记忆**、ai-hedge-fund 的**哈希链账本**、LLM_QUANT_FACTORY 的**隐藏测试集 + 六角色结构化制品 + 哈希链审计**、FactorGPT 的**AST 未来函数检查 + 兜底必须打标**——这些才是能直接长进 lquant 的东西。

### 0.3 一页总表（详见 §2）

| 项目 | 本质 | 编排 | 落到可回测信号？ | 可信度自评 | 对 lquant 价值 |
|---|---|---|---|---|---|
| TradingAgents | 多智能体交易研究框架 | LangGraph，4 分析+多空辩论+交易+风控+PM | 有（ticker×date 网格 + 相对基准 alpha） | 自认「研究脚手架，回测结果不保证可复现」 | ★★★★★ 编排与记忆 |
| ai-hedge-fund | 「AI 对冲基金」demo | 名人投资者角色 + 风控/PM | 有（回测 + 模拟盘哈希链账本） | 自述「proof of concept / 教育用途 / 不真的下单」 | ★★★★ 审计账本 |
| FinRobot | 金融 AI Agent **平台** | AutoGen(V0)→Agents SDK(V1)→PydanticAI(V2) | **无**（产出研报，不出信号） | 分级坦白：V0=教学，V2=生产 | ★★★★★ 「LLM 只叙事」 |
| FinGPT | 金融 LLM（数据/微调） | 无编排（非 agent 框架） | 无 | 披露成本、承认非投资建议 | ★★★ 小模型+成本口径 |
| FinMem | 分层记忆交易 agent | Profiling+Memory+Decision | 有（模拟回测） | **前视污染重灾（-71.85%）** | ★★ 记忆分层反面教材 |
| FinCon | 经理-分析师层级 + 言语强化 | 多 agent + CVaR 风控 | 有（Sharpe 2.37/3.27 被质疑） | 高分母、短窗口 | ★★ |
| StockAgent | 仿真环境下的 agent 行为研究 | 投资/撮合/BBS 三模块 | 模拟盘（非真实回测） | **主动做去泄漏（匿名标的）** | ★★★★ 反事实检验 |
| InvestorBench | LLM agent 金融决策基准 | 基准，非框架 | 评测环境 | 首个多品类基准 | ★★★★ 评测口径 |
| Alpha-GPT / 2.0 | 人在回路 alpha 挖掘 | 挖掘/建模/分析三层 agent + 工具 + 记忆 | 有（工具就是回测） | 无论文回测证据、无开源仓 | ★★★★★ 工具契约范式 |
| RD-Agent(Q) | 数据中心的因子-模型联合优化 | R(提假设)/D(实现) 双 agent 演化 | **有，且给出成本与基准对比** | 较强（NeurIPS 2025） | ★★★★★ 成本与闭环 |
| LLM_QUANT_FACTORY | 可审计 A 股多因子研究平台 | AutoAlpha/AutoCombine/QuantCombine + 6 角色 | 有（纯多主口径 + 硬门禁） | **全文自我设限，最诚实** | ★★★★★ 审计与治理 |
| FactorGPT | LLM 因子工业化平台 | LangGraph：检索→生成→校验→评估→反思 | 有（IC 回测 + 前向锁定预测） | 工程纪律强、兜底打标 | ★★★★★ 因子 DSL 与门禁 |
| paper2alpha | 研报 PDF → 因子骨架 | 单链 LLM 抽取 | 无（v0.1 因子体是 stub） | 自曝 v0.1 限制 | ★★★ 诚实度样板 |
| stockaskill | A 股投资分析 **Skill** | SKILL.md（兼容 ClaudeCode/Codex） | 有（回测脚本） | 中等，边界写得清 | ★★★★★ 与 lquant 同构 |
| iwencai-cli | 问财自然语言选股 CLI | 无（Playwright 爬页面） | 无 | 自述无官方关联 | ★ 反面教材 |

---

## 1. 调研范围与方法

### 1.1 lquant 现状基线（作为对标原点）

从 [`AGENT_MODEL.md`](../../AGENT_MODEL.md) 提取的**事实清单**：

| 维度 | lquant 现状 |
|---|---|
| 执行体 | 单个无头 CLI 子进程（`claude_code` 默认 / `codex` / `mock`），`posix_spawn`，环境变量原样继承 |
| 模型配置 | **平台侧零模型配置**（`config/app.yaml` 刻意无 `model/base_url/api_key`）——单一事实源在 CLI 侧 |
| 角色 | **无多角色**。角色与口径通过 `--append-system-prompt` + 工作区 `CLAUDE.md`/`AGENTS.md` 下发 |
| 能力集 | `config/skills/*`（`lquant-market` / `a-stock-data` / `factor-mining`）+ MCP 工具（`LQ_MCP_ENABLED_TOOLS`） |
| 隔离 | 每会话工作区 `data/agent_workspace/<sid>/`，每轮幂等重建 |
| 状态 | `ask_sessions` + `ask_messages`；provider 建会话后锁定（会话寻址口径冲突） |
| 过程 | `assistant_delta` 落库；`thinking`/`tool_call`/`tool_result` **只活在当前轮，不落库** |
| 回测衔接 | **无**。agent 输出不进入因子/回测/模拟盘链路 |
| 成本 | `timeout_seconds` 超时 kill；**无 token 计量、无成本核算** |
| 反射/记忆 | 无（对话历史除外） |

**结论：lquant 的 agent 是一个「强的取数与问答前端」，还不是「研究流水线上的可审计参与者」。** 本报告的所有借鉴项都围绕这个差距展开。

### 1.2 调研方法

- `web_search` 定位项目与批评文献 → `web_fetch` 取一手材料（raw README、GitHub API JSON、arXiv HTML 全文）。
- 对「LLM 选股」的学术批评做了**专门查证**，而非转述二手博客：找到并通读了 Alpha Illusion（arXiv 2605.16895）全文与 Five Sins（arXiv 2602.14233）全文。
- GitHub Issue 采用 API 端点取正文（网页版会被反爬截断），因此能引用具体 issue body。
- **诚实声明**：`TradingAgents-CN`、部分中文小项目（`AStock-AI-Agent`、`financial-analyst` 等）只做到 README/搜索层面的核实，未逐行读源码；文中已标注证据等级。

---

## 2. 逐项目详析

### 2.A 多智能体交易 / 分析框架

#### 2.A.1 TradingAgents（TauricResearch/TradingAgents）

| 项 | 值 |
|---|---|
| 仓库 | <https://github.com/TauricResearch/TradingAgents> |
| 元数据 | **110,238 stars** / 21,199 forks / Apache-2.0 / Python / 创建 2024-12-28 / 最后推送 **2026-10-03** / open issues 95（[API](https://api.github.com/repos/TauricResearch/TradingAgents)） |
| 论文 | [arXiv:2412.20138](https://arxiv.org/abs/2412.20138)（v7，2025-06-03；q-fin.TR） |
| 依赖 | Python ≥3.11；LangGraph；**需要至少一个付费 LLM key**；已支持 Ollama 与任意 OpenAI 兼容端点（vLLM / LM Studio / llama.cpp） |

**Agent 编排设计（重点）**

- **角色分层**（README 原文）：
  - **Analyst Team**：Fundamentals / Sentiment / News / Technical（四者**并行执行**，各自带自己的工具；v0.5.2 起并行）
  - **Researcher Team**：Bull vs Bear **结构化辩论**
  - **Trader Agent**：综合双方报告，决定**时机与幅度**
  - **Risk Management + Portfolio Manager**：风控团队评估波动/流动性，PM 批准或否决；批准后送模拟交易所
- **状态传递**：LangGraph 图状态；`TradingAgentsGraph.propagate(ticker, date)` → `(state, decision)`；`--checkpoint` 开启后**每个节点后写 state**，崩溃可续跑（`~/.tradingagents/cache/checkpoints/<TICKER>.db`）。
- **成本分层（值得抄）**：`quick_think_llm` 服务分析师/研究员/辩论者/交易员，`deep_think_llm` 服务 research manager 与 portfolio manager；**两档可挂不同 provider**（例：管理者用 Claude，其余用 OpenAI），也各有 `*_backend_url` 可指向本地/中转。
- **记忆与反思（最值得抄）**：`~/.tradingagents/memory/trading_memory.md` 常开。每次完成的运行把决策**追加**进去；后续运行时，系统会**结算所有持有期已到期的历史决策**——抓取**已实现收益（原始 + 相对该标的区域基准的 alpha）**并生成一段反思。PM 在分析时会读到「同一标的的近期决策 + 其他标的的近期教训」。结算失败不阻塞运行，但**报告里会写明失败**。
- **是否落到可回测信号**：**是**。`run_backtest` 在 `iter_grid(start, end, every_n_days)` 生成的 ticker×date 网格上跑同一条流水线，**按已实现 alpha（相对区域基准）分组打分**，并可 `run_id=result.run_id` 断点续跑；memory log 与回测互不污染（回测写自己的 log）。

**可复现性与可信度（本项目最有信息量的部分）**

- README 有**专门的 Reproducibility 章节**，明确承认：LLM 采样非确定；**「live data moves」——同一天同一标的，今天和上周看到的新闻/社媒内容不同**；建议降低 temperature 或改用非推理模型，但**推理模型基本忽略 temperature**。
- README 原文结论：**「Backtest results are not guaranteed to match any published figure. … Treat the framework as a research scaffold for studying multi-agent analysis, not as a strategy with a fixed, replicable return.」**
- **前视泄漏的修复史本身就是证据**：
  - Issue **#1220**「Historical analyses leak current Reddit and StockTwits data into the sentiment report」（2026-08-09 提，2026-08-31 关闭）——issue body 明确指出：Yahoo news 按历史窗口取，但 `fetch_stocktwits_messages()` **不带日期**、Reddit `_search_qs` **硬编码 `t=week`（当前周）**，而 prompt 却声称覆盖 `2020-01-08..2020-01-15`；并提出「历史数据不可得时应注入显式不可用占位符，而不是用当前帖子冒充」。
    → <https://github.com/TauricResearch/TradingAgents/issues/1220>
  - CHANGELOG 显示这是一条**持续两年的补丁线**：v0.3.1「Alpha Vantage look-ahead filtering」→ v0.4.0「look-ahead / point-in-time fixes across FRED macro, social sentiment, and the decision-log memory」→ v0.5.0「point-in-time integrity across every dated path, SEC EDGAR fundamentals served as filed」→ v0.5.2「backtests that see only data published by each analysis date」。
- **Issue #119「Where is the backtesting ?」**（2025-07-08 提，**2026-09-18 才关闭，挂了 14 个月**）：质疑论文 §5.1 声称使用「comprehensive backtesting simulation」但**全文与仓库都未提用什么回测引擎、什么频率**。
  → <https://github.com/TauricResearch/TradingAgents/issues/119>
- **点时刻（PIT）处理是亮点也是遗憾**：SEC EDGAR 按**申报日**给出「as filed」财报（Apple 2008 总资产按当时申报的 $39.6B，而非 2010 重述后的 $36.2B）；而 Yahoo 财报只能按**报告期**而非发布日标注，**于是历史日期的运行会被显式告知「withheld（扣留）」**；内部人交易同样扣留。这是**「宁可拒绝，也不冒充」**的正确姿势。

**值得 lquant 借鉴**
1. 「记忆 → 到期结算真实收益与相对基准 alpha → 生成反思 → 回灌下一轮」闭环——**且与回测日志物理隔离**。
2. 双档模型 + 分档 provider 的成本/质量分层。
3. PIT 不可得时**显式 withheld** 而非静默替换。
4. 回测网格 + `run_id` 断点续跑 + 按评级分组打分。

**坑**
- 前视泄漏不是「一次性 bug」而是**会反复回归的类别**（社媒、宏观、记忆日志各出现一次）。
- 财报数据源本身有 PIT 能力差异（EDGAR 有、Yahoo 无），**跨源时会静默降级**——必须像它一样显式扣留。
- 论文里的「superiority in cumulative returns, Sharpe ratio, and maximum drawdown」与仓库的自述免责**存在张力**；引用论文数字时必须同时引用 README 免责。

#### 2.A.2 TradingAgents-CN（hsliuping/TradingAgents-CN）

| 项 | 值 |
|---|---|
| 仓库 | <https://github.com/hsliuping/TradingAgents-CN> |
| 定位 | TradingAgents 的**中文增强版**，A 股/港股/美股分析 + 学习中心 |
| 技术栈 | FastAPI + Vue 3 + Element Plus + MongoDB + Redis；数据源 Tushare / AkShare / BaoStock |
| 许可证 | **混合许可：除 `app/`（FastAPI 后端）与 `frontend/`（Vue 前端）外为 Apache-2.0；这两个目录为专有，商业使用需授权** |

**要点与坑**
- README 顶部有**版权侵权警告**：`tradingagents-ai.com` 未经授权使用其专有代码并声称是自家产品；项目组声明**未给任何组织或个人商业授权**。
  → **这是一条对本仓直接有用的教训：Apache-2.0 的「源码可用」不等于「可商用」，fork 出来的项目可能把关键目录改成专有许可。**
- v1.x 自述修过「技术指标计算不准确」「基本面 PE/PB 计算错误」「无限循环」——**说明多智能体 + 多数据源的组合极易在数字层出错**，且错误形态是「能跑出结果，但数字错」。
- README 明确要求「分析股票前必须先把股票数据同步完成，否则分析结果会出现数据错误」——**数据就绪度是前置条件而非运行时检查**，与 lquant 的 `lq data status` / 覆盖度口径可以对照。
- 定位是**学习/研究平台，不提供实盘交易指令**。
- 证据等级：README 级（未读源码）。

#### 2.A.3 ai-hedge-fund（virattt/ai-hedge-fund）

| 项 | 值 |
|---|---|
| 仓库 | <https://github.com/virattt/ai-hedge-fund> |
| 元数据 | **63,903 stars** / 11,227 forks / **MIT** / Python / 创建 2024-11-29 / 最后推送 **2026-10-02**（[API](https://api.github.com/repos/virattt/ai-hedge-fund)） |
| 依赖 | `pipx install aihf`；**必须** Financial Datasets API key（付费，价格/基本面/财报）+ 一个模型 key（Anthropic/OpenAI/DeepSeek/Google/xAI/Kimi/TypeSafe） |

**Agent 编排设计**
- 角色为**名人投资者人格**（巴菲特/芒格/木头姐等）+ 估值/情绪/基本面/技术面分析师 + **risk manager** + **portfolio manager**。
- **状态与审计（最值得抄的部分）**：
  - **Paper trading**：每个基金一个**哈希链账本**（`~/.hedge-fund/paper/<name>/`），README 原文「**NAV is a track record, not a reset**」；运行前有**审批步骤**（显示即将执行的精确决策）。
  - **Backtesting**：按历史回放，**权益曲线与基准对绘**，结果存 `~/.hedge-fund/research/`。
  - **Fund definitions 存在 `~/.hedge-fund/mandates/`，其中不含 tickers**——**选股域是每次回测时选的**，而不是焊死在策略定义里（防「策略即事后选出来的那批票」）。
- **是否落到可回测信号**：是（回测 + 模拟盘两条腿），但……

**可复现性与可信度**
- README 首段自我定性：**「This is a proof of concept for an AI-powered hedge fund … for educational purposes only」**，并且**「Note: the system does not actually make any trades.」**
- 因此：**它的回测/模拟盘是「记账系统」而不是「策略验证系统」**；把它当绩效证据是误读。
- 优点在于**工程诚实**：明确 disclaimer、明确不下单、明确教育用途。

**值得 lquant 借鉴**
1. **哈希链账本**（append-only、可验证未被改写）用于「问 AI」的结论轨迹与模拟盘 NAV——比 lquant 现在的「只落正文」强得多。
2. **mandate 与 universe 分离**：策略定义不含标的，选股域在评估时确定 → 天然防「事后挑票」。
3. 执行前**审批步骤**显式展示「即将执行的精确决策」。

**坑**
- 「名人人格」是营销外壳，与预测能力无关；人格 prompt 无法移除模型共享的参数化先验（见 §4.3 **Parametric Prior Lock-in**）。
- 依赖单一付费数据源（Financial Datasets），数据口径不可自证。

#### 2.A.4 FinRobot（AI4Finance-Foundation/FinRobot）

| 项 | 值 |
|---|---|
| 仓库 | <https://github.com/AI4Finance-Foundation/FinRobot> |
| 元数据 | **8,160 stars** / 1,376 forks / Apache-2.0 / Python / 创建 2024-02-27 / 最后推送 **2026-09-28** / 默认分支 `master`（[API](https://api.github.com/repos/AI4Finance-Foundation/FinRobot)） |
| 论文 | [arXiv:2405.14767](https://arxiv.org/abs/2405.14767)（平台）+ [arXiv:2411.08804](https://ar5iv.labs.arxiv.org/html/2411.08804)（股权研究与估值 agent） |

**三代架构（本身就值得抄的一课）**

| 版本 | 框架 | 目录 | 定位 |
|---|---|---|---|
| V0 | **AutoGen** | `finrobot_autogen/` | 教学/复现论文（`pip install finrobot` 装的就是它） |
| V1 | **OpenAI Agents SDK** | `finrobot_equity/` | 自托管 Web 研报生成器 |
| V2 | **PydanticAI** | `finrobot_desktop/` | **生产**（桌面 + 本地 Web + CLI） |
| V3 | DeepSeek-Harness | — | 开发中 |

> **关键观察**：FinRobot **自己换了三代 agent 框架**，并在 README 明确说「FinRobot is not defined by any single agent framework」。→ 对 lquant 的启示：**不要把编排框架当资产，把领域层（工具/算子/工作流/校验/溯源）当资产。** 这一点与 lquant 现有 ADR（借语义不借运行时）高度一致。

**核心设计原则（本报告认为最值得整段抄的一句话）**

> **「models reason, software computes, agents orchestrate, and systems verify.」**
> 所有金融数字由**纯 Python 算子**产出，**不由 LLM 产出**；LLM 只做推理、综合、解释、写报告；估值输出（DCF / DDM / LBO / WACC / comps / Monte Carlo）走**确定性代码路径并全链路溯源**。
> 原文三行：**「Numbers are code-calculated. Narratives are LLM-assisted. Every output is provenance-tracked.」**

**Agent 编排**
- **9 个 agent**：Lead Agent / Orchestrator + 5 个流水线角色（Data → Analysis → Modeling → Synthesis → Report）+ 3 个辩论角色（Bull ↔ Bear → Judge）。
- **7 条研究流水线**：company research / DCF / comps / LBO / DDM / earnings / IC memo。
- **确定性计算**：32 个纯 Python 算子（26 估值分析 + 6 审计）+ 7 个 coordinator。
- **数据**：7 个 provider 带**故障转移**（FMP / Finnhub / yfinance / SEC EDGAR / Adanos / NewsAggregator / FX）。
- **56 个 analyst playbook（skills）**——与 lquant 的 skill 概念同构。
- **产物层**：`artifact/`（报告存储 + **output contract gate**）+ `audit/` + `coverage/`。

**可复现性与可信度**
- **无交易回测、无信号**：产出的是**研究报告**（13 章 + 12 个月目标价 + 区间 + 置信度 + 数据 caveat）。
- 优点：**分级坦白**（V0 明确写「Not intended for production use」；V2 才叫 production）；DCF 表被标注为 **code-computed**，与旁边 LLM 叙述显式区分。
- 缺点：置信度由 LLM 给，**没有校准证据**（正好落进 Alpha Illusion 的 P4 未满足区）。

**值得 lquant 借鉴**
1. **「数字由代码算，叙述由 LLM 写」作为硬约束**（可直接约束 `lquant-market` skill 的输出）。
2. **output contract gate** + artifact store + audit/coverage 模块的目录学。
3. **provider 故障转移表**（lquant 现在单源）。
4. 「不绑定单一 agent 框架」的演进姿态。

**坑**
- V0/V1/V2 三套并存 → 用户极易跑错代（`pip install finrobot` 装到的**不是**推荐使用的 V2）。
- ~184k 行 + React/Tauri/Rust 壳 → **对 lquant 是净增依赖，不可移植**。

#### 2.A.5 FinGPT（AI4Finance-Foundation/FinGPT）

| 项 | 值 |
|---|---|
| 仓库 | <https://github.com/AI4Finance-Foundation/FinGPT> |
| 元数据 | **21,385 stars** / 3,030 forks / **MIT** / Jupyter Notebook / 创建 2023-02-11 / 最后推送 **2026-09-23**（[API](https://api.github.com/repos/AI4Finance-Foundation/FinGPT)） |
| 本质 | **数据中心的金融 LLM**（LoRA 微调 + 指令数据集 + 情感/预测任务），**不是 agent 编排框架** |

**要点**
- 定位对照（FinRobot README 原文）：「Unlike the **single-model paradigm** of FinGPT, FinRobot supports end-to-end agentic workflows」——**两家自己把 FinGPT 定性为单模型范式**。
- **成本口径值得抄**：FinGPT v3.3（llama2-13b LoRA）在 **1×RTX 3090 / 17.25 小时 / $17.25** 上取得优于 GPT-4 的情感分析 F1；README 直接给 GPU 小时单价推导（A100 ≈ $4.10/GPU·h），并对比 **BloombergGPT ≈ $2.67M / 53 天**。自述「**每轮微调 <$300**」。
- **本地模型支持好**：Llama-2-13B LoRA 全本地推理（需 GPU）、Qwen/ChatGLM2/InternLM 中文基座；也支持云 provider（OpenAI GPT-3.5、MiniMax-M3 512K）。
- **FinGPT-Forecaster**：输入 ticker + 起算日 + 取新闻周数，输出**下周股价方向**的判断。明确说明：**训练于美股 DOW30**，其他市场（含 A 股）需自行收集数据重训；中国市场可考虑 FinGPT v1.x。

**可复现性与可信度**
- 情感分析有**可复现的 benchmark 表**（FPB / FiQA-SA / TFNS / NWGI + 设备/时间/成本）。
- **但 forecaster 没有回测**：它是「新闻+财务 → 下周涨跌」的文本预测，**没有 IC / 分层 / 换手 / 成本口径**，也没有 PIT 声明。把它当选股信号是误用。

**值得 lquant 借鉴**
1. **成本核算模板**（设备 × 小时 × 单价 → 单次成本），可直接用于 lquant 的 LLM 成本面板。
2. **小模型 + LoRA 的落地方案**（若 lquant 要把「研报/公告情绪」做成**可回测因子**，本地微调比调用 API 更可控、更便宜、且**训练 cutoff 明确可声明**——这一点对 P1 至关重要）。
3. 明确的「训练于 DOW30、换市场需重训」边界声明。

**坑**
- FinGPT-Forecaster 的 HF Space 免费、易被误读为「能选股」。
- 库中大量 notebook 停留在 2023 年口径，与仓库 2026 的推送时间不一致（**活跃度看推送，成熟度看内容**）。

---

### 2.B 学术 Agent 框架与基准

#### 2.B.1 FinMem（pipiku915/FinMem-LLM-StockTrading）

| 项 | 值 |
|---|---|
| 仓库 | <https://github.com/pipiku915/FinMem-LLM-StockTrading> |
| 论文 | [arXiv:2311.13743](https://arxiv.org/abs/2311.13743)（AAAI Spring Symposium / ICLR Workshop / IJCAI2024 FinLLM Task3） |
| 许可证 | MIT；Python 3.10 |

**编排设计**
- 三模块：**Profiling**（角色/性格设定）、**Memory（分层记忆）**、**Decision-making**（把记忆里的洞察转成投资决策）。记忆分层「贴近人类交易员的认知结构」，**cognitive span 可调**——保留超出人类感知极限的关键信息。
- **train / test 两阶段模式**：train 时信息填充 agent 记忆；test 时用记忆 + 新信息决策，**test 必须提供已训练 agent 路径**。支持 checkpoint 断点续跑（README 明确说「OpenAI API is not stable」是恢复的主要原因）。
- **依赖**：无论用哪个骨干 LLM，**都必须设 `OPENAI_API_KEY`**——因为 embedding 固定用 `text-embedding-ada-002`。本地模型走 TGI endpoint（`model = "tgi"`）。
- 数据：需自备 `data/06_input/subset_symbols.pkl`；默认 TSLA，训练 2022-06-30 → 2022-10-11（**约 3.5 个月**）。

**可复现性与可信度（反面教材）**
- **前视污染有量化证据**：Alpha Illusion 引用 Li et al. (2025) 的对照实验——**跨过预训练 cutoff 后 FinMem 总收益下降 ≈71.85%**（[arXiv:2605.16895](https://arxiv.org/html/2605.16895v1) §2.2）。这几乎可以断定：**在 cutoff 窗口内测出的收益主要来自语义记忆**。
- **窗口过短**：默认实验只有约 15 周；Alpha Illusion 用 Lo (2002) 的 Sharpe 标准误公式指出，**小 T + 大 Sharpe 正是置信区间最宽的区间**（[arXiv:2605.16895](https://arxiv.org/html/2605.16895v1) §2.4）。
- **embedding 强制走 OpenAI** → 即使换成本地 LLM，也仍有外部 API 依赖。

**值得 lquant 借鉴**
- **分层记忆 + 可调 cognitive span** 的思想（衰减/重要度分层）——但必须配合「到期结算」才有意义（TradingAgents 做得更好）。
- **train/test 两阶段**的显式分离：**决策阶段只能读记忆，不能读原始未来数据**。

**坑**
- 「用记忆提升收益」的实验设计**天然鼓励记忆污染**：记忆里存的若是带后见之明的总结，回测就被污染。
- 3.5 个月样本 → 不构成任何策略结论。

#### 2.B.2 FinCon（NeurIPS 2024）

- 论文：[arXiv:2407.06567](https://ar5iv.labs.arxiv.org/html/2407.06567)；[NeurIPS 2024 论文页](https://proceedings.neurips.cc/paper_files/paper/2024/file/f7ae4fe91d96f50abc2211f09b6a7e49-Paper-Conference.pdf)
- **编排**：**manager–analyst 层级** + **conceptual verbal reinforcement**（用自然语言信念更新替代数值梯度）；**CVaR 风险控制在 episode 内触发**（CVaR 骤降即风险告警）。
- **报告数字**：per-ticker Sharpe **2.37**、最佳组合（TSLA, MSFT, PFE）Sharpe **3.27**（Alpha Illusion 复述并质疑，[arXiv:2605.16895](https://arxiv.org/html/2605.16895v1) §2.1）。
- **可复现性**：仓库未在本次调研中被确认为官方维护（搜索结果指向第三方复刻 `hackingthemarkets/FinMem-LLM-StockTrading` 一类）；**数字来自论文，非可独立复跑的开源实现**。
- **借鉴**：①「言语强化」= 把反思写回结构化信念而非改权重，**适合 lquant 的 ADR 文化**；②**episode 内 CVaR 告警**是「LLM 不做风控、确定性组件做风控」的正确切分。
- **坑**：Sharpe 3.27 属于 Alpha Illusion 点名的「headline Sharpe」类型——**必须先扣摩擦、看窗口长度、看是否跨 cutoff**。

#### 2.B.3 StockAgent（MingyuJ666/Stockagent）

- 论文：[arXiv:2407.18957](https://ar5iv.labs.arxiv.org/html/2407.18957v2)；代码 <https://github.com/MingyuJ666/Stockagent>
- **编排**：三模块 —— **Investment Agent**（随机初始资金/负债 + 四种性格：Conservative / Aggressive / Balanced / Growth-Oriented）→ **Transaction**（订单簿 + **random clock page replacement** 随机排序避免并发死锁）→ **BBS**（每日收盘后 agent 匿名发帖分享 tips，次日对所有 agent 可见）。
- **验证设计**：单年 **264 个交易日**（4 季 × 66 天）、季度财报发布、准备金率下调/加息等外部事件（取 2014–2019 真实事件）、按 NASDAQ 与港交所机制建模交易时段、显式交易成本（每股 0.005 + 最低 1 / 最高 5.95）、贷款利息与破产清算。
- **去泄漏（最值得抄的一条）**：论文摘要明确「**StockAgent avoids the test set leakage issue present in existing trading simulation systems**」——做法是**把标的匿名化为 Stock A / Stock B**，阻止模型利用与测试数据相关的先验知识。
- **借鉴**：①**匿名化实体做反事实检验**——极低成本的「模型到底靠参数记忆还是靠给定上下文」判别法，可直接做成 lquant 的哨兵测试；②BBS 模块（信息在 agent 间扩散）对研究**羊群/共振**有用；③显式建成本/贷款/破产。
- **坑**：这是**仿真行为学研究**，不是策略验证；结果不能读成 alpha。

#### 2.B.4 InvestorBench（LLM agent 金融决策基准）

- 论文：[arXiv:2412.18174](https://arxiv.org/abs/2412.18174)（ACL 2025 Long，[PDF](https://aclanthology.org/2025.acl-long.126.pdf)）
- **内容**：首个面向 LLM agent 多品类金融决策的基准；覆盖**单股票 / 加密货币 / ETF**；用 **13 个不同 LLM** 作骨干，在多市场环境与任务上评测推理与决策；提供开源**多模态**数据集与成套环境。
- **借鉴**：①**「同一 agent 框架 × 多骨干模型 × 多任务」**的评测矩阵（lquant 若要对 agent 做回归，这是可抄的形状）；②「基准 vs 部署」的边界意识正是 Alpha Illusion 与 Five Sins 的共同主张。
- **注意**：基准类工作**本身不做可部署性声明**；引用它时要区分「评测得分」与「交易能力」。

#### 2.B.5 QuantAgent（两个同名前作，别混淆）

- **QuantAgent (2024)**：`QuantAgent: Seeking Holy Grail in Trading by Self-Improving Large Language Model`，[arXiv:2402.03755](https://export.arxiv.org/pdf/2402.03755)。
- **QuantAgent (2025)**：`Price-Driven Multi-Agent LLMs for High-Frequency Trading`，[arXiv:2509.09995](https://huggingface.co/papers/2509.09995)；报告 **50.7%–63.7% 的方向准确率**（HFT bars）。
- **可信度**：Alpha Illusion 的复现给出**净收益为负且恶化**：Sharpe **−0.96 → −1.15**（扣佣金/代币成本/价差/市场冲击后），**低于 buy-and-hold**（[arXiv:2605.16895](https://arxiv.org/html/2605.16895v1) Fig.1）；跨 cutoff 后 Sharpe 下降 ≈51.48%（§2.2）。
- **借鉴**：把「方向准确率」当交付指标是本项目最大的坑——**准确率提升不蕴含净收益提升**（Alpha Illusion 引用 Jang et al. 2025 的 BTC 反例：RL 训练后分类准确率明显提升，但模拟交易累计收益下降）。

#### 2.B.6 Alpha-GPT / Alpha-GPT 2.0（人在回路的 alpha 挖掘）

| 项 | 值 |
|---|---|
| 论文 | Alpha-GPT：[arXiv:2308.00016](http://xxx.itp.ac.cn/abs/2308.00016)；Alpha-GPT 2.0：[arXiv:2402.09746](https://ar5iv.labs.arxiv.org/html/2402.09746)（IDEA Research × HKUST） |
| 开源状态 | **本次调研未找到官方开放仓库**——只有论文与第三方复刻（如 `parthmodi152/alpha-gpt`） |

**编排设计（对 lquant 最有参考价值的一份）**
- **三层，每层一个 LLM agent，各带工具与记忆**：
  1. **Alpha Mining Layer**：把自然语言的**交易想法翻译成表达式型 alpha 因子**。工具集覆盖「alpha 计算 → 回测评估 → 算法化搜索增强（GP）→ alpha 部署 → alpha 库维护」。记忆包含：**带注释的 alpha 库**（风格/市场特征/用户描述）、**处理过的文献**（书、研报、论文）、实验日志、用户对话历史与评论。
  2. **Alpha Modeling Layer**：因子 → ML/DL 建模（训练/测试、结构搜索、超参优化、模型基准对比、特征选择与解释、组合优化与交易清单生成）。记忆是**模型 zoo + 模型参数配置模板**。文中给了具体交互例：「给我用至少 10 个主流 ML 模型快速评估第 6 组因子对本周收益的预测能力」→ agent 自己规划并批量跑。
  3. **Alpha Analysis Layer**：基本面/事件风险的**黑名单与权重调整**；核心是**大规模金融行为知识图谱**（产业/供应链/资金/诉讼链）+ **Think-on-Graph** 推理，目标写得很直白：**可靠、可控、可解释、可追溯**。
- **关键设计选择**：**「workflow 是预定义的，我们不需要 LLM 做复杂任务规划；每层都有标准 SOP」**。→ 这是**反「自主 agent」**的路线：LLM 负责意图翻译与工具调度，**流程由人写死**。
- **人在回路（Human-in-the-Loop）**：每个阶段人都可以注入想法；系统把实验结果反馈给人，指导下一轮。

**可复现性与可信度**
- **无论文级回测证据、无开源实现** → 不可复现。
- 但其**接口设计**是可信的：工具即「计算/回测/搜索/部署/库维护」，记忆即「alpha 库 + 文献 + 日志 + 对话」。

**值得 lquant 借鉴（本报告列为最高优先级之一）**
1. **「自然语言想法 → 表达式因子」的工具契约**：`compute(expr) → backtest(expr) → search(expr) → deploy(expr) → library.put(expr)`，每一步都有确定性返回。这正是 lquant 的 `factor-mining` skill 应当具备的接口形状。
2. **alpha 库要存「注释 + 风格标签 + 来源 + 用户描述」**，而不只是公式。
3. **SOP 优先、LLM 不做全局规划**——降低不可复现性与 token 成本。
4. **知识图谱 + Think-on-Graph 用于风险黑名单**：把 LLM 的推理**锚定在结构化图谱路径上**，天然可追溯。

---

### 2.C Qlib 生态的 Agent 角度（按要求只补 agent 视角）

#### 2.C.1 RD-Agent(Q)（microsoft/RD-Agent）

| 项 | 值 |
|---|---|
| 仓库 | <https://github.com/microsoft/RD-Agent> |
| 定位 | **首个数据中心（data-centric）的量化多智能体框架**，做**因子-模型联合优化** |
| 论文 | 总框架 [arXiv:2505.14738](https://arxiv.org/abs/2505.14738)；量化场景 **RD-Agent(Q)** [arXiv:2505.15155](https://arxiv.org/abs/2505.15155)（**NeurIPS 2025 接受**） |
| 依赖 | **仅 Linux**；大量场景需 Docker；Python 3.10/3.11；**LiteLLM** 作默认后端（可接 DeepSeek / Azure / 任意兼容端点） |

**编排设计**
- 两个元组件：**「R」（Research：提出新想法/改进现有想法）+「D」（Development：实现并执行）**，形成**演化闭环**：提假设 → 设计实验 → 写代码 → 执行 → 拿真实指标反馈 → 下一轮。
- **成本分层（明确写进 README）**：`o3(R) + GPT-4.1(D)` —— **用贵模型提假设、用便宜模型写代码**，同时降低每轮耗时与成本。
- **场景**（对 lquant 直接可比）：
  - `rdagent fin_quant`：因子 + 模型**联合**演化
  - `rdagent fin_factor`：只演化因子
  - `rdagent fin_model`：只演化模型
  - `rdagent fin_factor_report --report-folder=...`：**从金融研报中抽取因子并实现**（官方给了 `all_reports.zip` 示例数据集）
  - `rdagent general_model <paper URL>`：从论文抽取模型结构并实现
- **结果（README 原文）**：真实股票市场实验，**成本 < $10**，比基准因子库**约 2× ARR**，且**因子数少 70%+**；在更小资源预算下**超过 SOTA 深度时序模型**；因子-模型交替优化在预测精度与策略稳健性之间取得好折衷。
- **安全设计（值得抄的工程细节）**：Web UI **强制要求 `UI_SERVER_AUTH_TOKEN`，即使监听 localhost 也拒绝启动**（理由：恶意网站可向浏览器本地服务发请求）；**拒绝上传 `.dill/.pickle/.pkl/.py/.pyc/.pyo`**；默认**关闭 legacy pickle trace 反序列化**；CORS 默认关闭；文档明说「把已认证访问视为受信操作员权限，生成代码要在隔离的最小权限环境跑，不要暴露 host 凭据或 Docker socket」。

**可复现性与可信度**
- 强于同类的两点：**给出成本**、**给出与基准因子库和 SOTA 模型的对比**；且在 MLE-bench 上是公开最强 ML 工程 agent（`o3(R)+GPT-4.1(D)` all 30.22% ± 1.5，对比 AIDE o1-preview 16.9%）。
- 局限：**依赖 Qlib 运行时**（lquant 已在 ADR-11 决定不引入 qlib 到主 venv）；**只支持 Linux**；因子/模型的评估口径以 qlib 为准。

**值得 lquant 借鉴**
1. **R/D 双 agent + 演化闭环**，且**反馈来自真实回测指标**而非 LLM 自评。
2. **「研报 → 因子」场景**（`fin_factor_report`）——与 lquant 的 `factor-mining` skill 目标重合，可对照其接口。
3. **成本分层 + 成本公开**：把「<$10」当一等指标。
4. **把 prompt/结果/过程 trace 落盘**（`UI_TRACE_FOLDER`，且上传文件**刻意放在 trace 目录之外**，防止被当成 trace 反序列化）——这条**目录学**直接可用于 lquant。

**坑**
- Qlib 依赖 + Linux + Docker 三重要求 → 移植成本高；**只借范式不借运行时**。

#### 2.C.2 DDG-DA（概念漂移自适应）

- 论文：[arXiv:2201.04038](http://export.arxiv.org/pdf/2201.04038)
- **思想**：**从历史数据中学习「未来分布会怎么变」**，据此调整模型，使模型适应**未来的**数据分布，而不是假设 i.i.d.
- **lquant 既有判断**：`docs/research/00-borrow-and-port-plan.md` §1.7 已把 DDG-DA 列为「与日频选股定位不匹配，留作观察」。
- **本报告补充的 agent 视角**：DDG-DA 在 agent 语境下的价值是**「让 agent 感知自己的失效」**——即把「概念漂移检测」作为一条**确定性监控信号**回灌给 agent（例如：因子近 N 日 IC 与历史分布的偏离度超阈值 → agent 才需要反思），而**不是**让 LLM 自己判断是否需要重训。这与 TradingAgents 的「到期结算」是同一族思路：**用真实结果驱动反思，而非用 LLM 驱动反思**。

---

### 2.D LLM 辅助因子挖掘（对 lquant 最直接对口）

#### 2.D.1 FactorGPT（ZXTLQQ/FactorGPT）

| 项 | 值 |
|---|---|
| 仓库 | <https://github.com/ZXTLQQ/FactorGPT> |
| 元数据 | MIT / Python 3.11+ / LangGraph + Streamlit（21 页） |
| 依赖 | LLM：DeepSeek / OpenAI / Qwen / **Ollama 本地** / 任意 OpenAI 兼容；数据：AKShare / Tushare / Baostock / EastMoney MX / THS iFinD / NeoData |

**编排设计**
- **核心 Agent 闭环：Retrieve → Generate → Validate → Evaluate → Reflect**
  - Retrieve：ChromaDB + BGE 从 62 因子知识库检索（jieba 兜底）
  - Generate：LLM 按**严格安全协议**生成因子代码，签名固定 `alpha_factor(df) -> DataFrame[date, symbol, factor]`
  - Validate：**隔离子进程 + 超时/内存限制 + import 白名单 + 基于 AST 的未来函数（lookahead bias）检测**
  - Evaluate：IC / RankIC / ICIR / IC 胜率 / 分层收益 / 多空 Sharpe、MDD / 换手 / 覆盖度
  - Reflect：**IC 未达阈值时，把回测指标回灌给 LLM 迭代改进因子**
- **六阶段「因子精炼厂」**：矿石仓（28 原始特征 + 50+ 因子池）→ 挖掘层（Transformer 编码 + MaskablePPO 因子组合搜索 + LLM 探脉）→ 研磨（IC/IR/ICIR 批量评估）→ 三层筛选（LASSO 去冗余 → 人机协同复核 → TOP 10% 截断）→ 合金调配（ICIR 加权 + 正交化合成 + **留一法过拟合检验**）→ 方法论报告（一键 MD + JSON）。
- **表达式 DSL + PIT 面板 + operator grid miner + 四维评估**（Highlight 10）；**频谱清洗 + 因子相关矩阵风险分解**（Highlight 12）。
- **数据口径纪律（含 HF 特例）**：
  - 表格明确 **offline（日频）vs hf（分钟频）** 的差异：**分钟 IF 不做行业中市值中性化**；**OOS 切分按自然日而非按分钟**（理由原文：「minutes within a day are autocorrelated — splitting by minute is 『reading the answer before the exam』」）。
  - **命名由代码强制**：因子只有真的读了 `hf_*` 列（ofi/obi/depth_ratio/rvol/spread…）才允许自称 HF/Intraday/Micro。
  - `resolve_market_panel` 是四种数据源**唯一入口**，防止挖掘脚本与页面口径漂移；请求的股票代码统一归一化（`600519`/`SH600519`/`600519.SH`），**取数直接按需取，不做「先取全池再截断」**。

**可复现性与可信度（本项目最值得学的部分）**

1. **溯源与降级必须「响亮」**：
   - 离线兜底时报告头**打标**「`因子来源：模板兜底（并非由大模型生成）`」，UI 徽章显示 `template` 而非 `llm`；原文：**「A template product cannot pass as model output.」**
   - JEV 判定链**三级**（JEV 在线 → 本地模型 → 本地规则），每次结果都上报**是哪一级回答的**（`engine: jev / local-model / heuristic`），并给出 `check_jev.py` 命令自查——因为「静默降到二级」的唯一症状是「判断感觉不太准」。
   - 未解析的 `${VAR}` **在调用时**报错并指名变量，**不在构造时报错**（否则整个 Streamlit 应用会因缺一个环境变量而崩），也绝不发假 key。
   - **密钥永不进 git 跟踪的 `config.yaml`**：写 `.env`（POSIX 权限 0600），`config.yaml` 只保留 `${VAR}` 占位符；保存是**逐行 patch**，改不动内容时文件字节不变。
2. **前向测试（独立于回测）**：通过第三方 [Headline Arena](https://headlinearena.com) 把因子层研究的宏观观点转成对全球宏观资产的**每日概率预测**；**结算标准在出题时冻结、预测在结果存在前锁定、由第三方按真实行情机械结算**（方向性 `50 ± confidence×50`，平台另发布 per-agent CRPS/Brier 校准 API）。作者定性：「**the hardest-to-dispute form of evidence for factor validity**」。
3. **测试与质量**：426 个测试函数 / 461 用例（无网络）；**warnings-as-errors**，理由写得极准：**「本仓在意的失败模式不是抛异常，而是异常被静默吞成一个看似合理的数字」**（`np.corrcoef` 对退化截面返回 0、`np.nanmean` 对空切片告警后给 NaN）。原生 C++ kernel **逐位**对拍 pandas（含 ties、`-0.0`、全常量行、全 NaN 行、`inf`、低于最小样本数），**NaN 的位置也必须一致**。
4. **消融实验**：`ablation_study.py --seed 42 --n-symbols 20` 量化每个模块的样本外边际贡献（ΔICIR），结果写入 `docs/ablation_report.md`。

**值得 lquant 借鉴**
1. **AST 未来函数检测 + import 白名单 + 隔离子进程**——LLM 生成因子的**必备门禁**。
2. **兜底打标 + tier 上报**（`template` vs `llm`；`jev/local-model/heuristic`）——lquant 的 `mock` provider 已有类似前缀，可推广成通用「来源徽章 + 落库字段」。
3. **表达式 DSL + 因子命名规则由代码强制**。
4. **前向锁定预测 + 第三方结算**——解决「回测永远事后」的根本办法。
5. **warnings-as-errors 与逐位对拍**的文化（与 lquant 的交叉验证传统同源）。
6. 密钥、配置、`${VAR}` 的三级纪律。

**坑**
- 页面/图表/演示众多，**容易被误当成「已生产可用」**；README 自己做免责，但信息密度过高（含 21 页 UI）会稀释读者的怀疑。
- 部分模块「自动降级」到 numpy/heuristic（Transformer/RL/ChromaDB 缺失时）——**降级后的结论与宣传口径可能不一致**，必须看 `engine` 字段。
- 依赖上游数据源（AKShare/Sina/东财页面/THS/NeoData）众多 → **口径漂移风险**，作者用「单一入口 + 归一化」缓解。

#### 2.D.2 LLM_QUANT_FACTORY（khakhasshi/LLM_QUANT_FACTORY）

| 项 | 值 |
|---|---|
| 仓库 | <https://github.com/khakhasshi/LLM_QUANT_FACTORY> |
| 定位 | **可审计的多智能体 A 股因子研究与组合发现平台** |
| 许可证 | **PolyForm Noncommercial 1.0.0（2026-07-29 起）——源码可用，但非 OSI 开源；任何商业用途须事先书面许可** |
| 技术栈 | Python 3.12+ / uv / DuckDB + Parquet 按年分区面板 / Tushare 凭证 / 可选 OpenAI 兼容 API |

**编排设计**
- **三个服务**：`AutoAlpha`（:8788 连续因子研究 + 因子库 + 选股 + 回测 + 模拟组合）、`AutoCombine`（:8888 LLM 辅助组合研究）、`QuantCombine`（:8889 **不调用 LLM** 的确定性组合优化：SFFS / NSGA-II / 自适应采样 / Pareto 排序）。
- **结构化 LLM 研究团队（6 类角色）**：研究员 / 数据官 / 风控官 / 组合经理 / 审计员 / 交易员，**各自输出结构化制品**；独立复核、证伪设计、失败归因、交易可执行性分析进入**同一证据链**；**最终裁决仍属于确定性引擎与人工风险审批**。
- **治理不变量（原文提炼，这是本报告认为最完整的一份）**：
  - 每个研究任务**冻结**：市场、**数据可见范围**、探索区、滚动验证区、**隐藏测试区**。
  - **隐藏测试细节永不进入 LLM 上下文**。
  - **LLM 不能越过确定性门禁、不能直接批准策略、不能读取隐藏测试指标**。
  - 因子语言是**类型化表达式树 + 字段白名单 + 信号时点与未来函数检查**。
  - **A 股纯多资金表现是默认排序口径；RankIC 与多空 alpha 只作诊断，不能抵消成交或风险硬门禁的失败**。
  - **日终信号只能在收盘后获得，最早于下一交易日开盘执行**。
  - **失败的硬门禁不能被综合分平均掉**。
  - **手动回测、截图与公开样例不会进入自动研究记忆**。
  - 因子库保存：公式与 **AST**、机制类型、来源任务、**行为簇/同质簇**、生命周期、统一纯多指标、年度表现、**失效标签**、组合边际贡献——目标是区分「新机制」与「换皮参数」。
- **评价体系**：A 股纯多主指标 + 滚动样本外 + **DSR / PBO / FDR** + 参数邻域 + 成本与容量诊断。
- **审计**：不可变制品 + 四类日志 + **公开快照可重算哈希链**（脱敏）。

**可复现性与可信度（本报告认为**最诚实**的项目）**
- 顶部醒目声明：当前随项目验证的本地 A 股面板是 **non-PIT 研究代理数据**；截图中的历史绩效/排名/选股**仅用于展示研究流程，不代表生产资格、未来收益或投资建议**；真实交易前仍需补齐 **PIT 股票状态、复权与未复权成交口径、涨跌停、停牌、退市、费用、容量与独立盲测**。
- 公开样例**明确排除**：价格、证券级收益、持仓、**私有提示词**、隐藏测试结果、凭证、本机路径、可执行生产决策；且说明「没有另行授权的市场数据时，**不能复现截图中的结果**」。
- ROADMAP 把「PIT 数据与真实 A 股成交」「因子同质化与虚假创新」「多 LLM 线程与模型协作（须禁止绕过确定性治理）」列为**仍未解决**的难题。

**值得 lquant 借鉴（本报告评为最高价值）**
1. **「数据可见范围 + 隐藏测试区」冻结** + **隐藏指标永不入 LLM 上下文**——这是把 lquant 的「交叉验证对拍」传统**升级到 LLM 时代**的正确形态。
2. **硬门禁不可被综合分平均掉**、**纯多为主口径 / RankIC 仅诊断**——与 lquant 的 A 股日频选股定位完全一致，可直接照搬成评价章程条款。
3. **因子 AST 指纹 + 同质簇 + 换皮参数识别**。
4. **哈希链审计 + 公开脱敏快照可重算**。
5. **六角色结构化制品 + 确定性引擎终裁**。
6. **手动回测/截图不进自动研究记忆**（防污染自动化流水线的记忆）。

**坑**
- **许可证陷阱：PolyForm Noncommercial，商用需授权** → 只能读设计，**不能抄代码**。
- 需要自备合法授权的 A 股数据 + Tushare 凭证；无数据则无法复现任何数字。

#### 2.D.3 paper2alpha（VernonOY/paper2alpha）

- 仓库：<https://github.com/VernonOY/paper2alpha>；MIT；定位「**从研报 PDF 抽取可执行 alpha 因子**，中文券商研报优先」。
- **流水线**：PDF → PyMuPDF 解析 → **LLM 抽取（JSON mode）** → **Pydantic `ResearchCard`** 结构化 → Jinja2 模板 ← 因子 stub ← **qtype 静态检查**。
- **诚实度样板（值得 lquant 学其写法）**：README「Known limitations (v0.1)」直言 **生成的因子体是 stub（`raise NotImplementedError`），只有元数据/常量/docstring 被填**；LLM 生成因子体排到 v0.2；扫描版 PDF 的表格/公式抽取是启发式、**可能漏数**；只内置 OpenAI，其他 provider 需自己写 ≈20 行适配。
- **借鉴**：①**先做「研报 → 结构化卡片」再做「卡片 → 代码」**的分段；②**静态检查先于执行**（qtype）；③限制写在 README 而不是藏在 issue 里。
- **坑**：v0.1 无法产出可用因子 → 不要按 README 标题预期直接落地。

#### 2.D.4 AlphaGPT_Tushare（LilianaMajamay）等其他社区项目

- <https://github.com/LilianaMajamay/AlphaGPT_Tushare>：基于 DeepSeek + Tushare 的 A 股因子生成与回测系统（源自聚宽社区帖）。
- <https://github.com/GGS112233/AlphaGPT>：基于深度强化学习的开源自动因子工厂（**与 LLM 无关，命名撞车**）。
- <https://github.com/aqbor888/AlphaGPT>：加密市场流动性工具（**同名不同物，注意甄别**）。
- **提示**：`AlphaGPT` 这个名字在 GitHub 上被至少三个无关项目占用；检索时务必用「论文标题 + arXiv 号」定位。

---

### 2.E 中文 A 股方向（与 lquant 同构度最高）

#### 2.E.1 stockaskill（axjing/stockaskill）——与 lquant agent 架构同构

| 项 | 值 |
|---|---|
| 仓库 | <https://github.com/axjing/stockaskill> |
| 形态 | **一个 `SKILL.md` 形态的 A 股中长期投资分析 Skill**（不是应用） |
| 兼容框架 | **opencode / claudecode / codex / openclaw / Cursor / Windsurf —— 同一份 `SKILL.md` 复制到各自技能目录** |
| 数据 | **AKShare 为主 + 本地 SQLite 积累式缓存**（`quant_cache.db`） |

**这是本报告中最应重点对照的项目**：lquant 的 agent = **CLI 执行体 + skill + MCP**，stockaskill = **SKILL.md + 本地脚本**。两者是同一范式，但它在若干工程细节上更成熟：

1. **数据访问纪律（可直接抄）**：
   - **「SQLite 是唯一数据源」**：所有读取先查本地库，远端 API **只作同步手段，不作查询层**。
   - **增量补全、绝不重拉**：检查缓存最新日期，只拉缺失区间（**带 3 天重叠**以修正节假日/延迟）。
   - **日期范围 API 优先**：明确**禁止**用 `ak.stock_zh_a_daily()` 这类无视日期、全量下载的接口作主路径；README 原文：「违反此策略是致命 bug，会导致 API 限额耗尽和 RemoteDisconnected」。
   - **多源容错 + 熔断**：K 线 `baostock → AKShare/EastMoney → efinance`（A 股）/ `AKShare → yfinance`（HK/US）；基本面 `THS → Sina → yfinance`；**连续失败的源自动退避**。
   - **配额**：默认日上限 500 次调用；失败退避 `2^n` 秒、最多 3 次重试；超限**返回本地缓存结果**。
2. **有界同步 + 数据诊断作为一等命令**：`sync symbol/watchlist/portfolio/scan-universe/etf`、`status data ...`、`cache stats/cleanup`。
3. **研究闭环**：`workflow list/run`（**只把 manifest 解析成步骤，不直接执行 shell**）、`deep-diagnose`、**`thesis capture/list/review/postmortem`（建仓假设 → 跟踪 → 复盘）**、`theme-scan`、`scorecard`（自述**启发式、可解释，不是黑盒打分**）。
4. **多因子权重有券商出处**：估值 20%（华泰 EP+BP）、质量 25%（长江雪球）、成长 17%（中信建投超预期）、动量 17%、低波 11%（国君）、市值 9%；组合约束：**单票 ≤20%、持仓 6–30 只、止损 15%、再平衡 30 天**；三档风险偏好。
5. **能力边界写清**：A 股支持最深；HK/US 只有有界候选池；**FUND 路径当前按 ETF-first 语义，不等同广义公募基金**；元数据质量信号（`metadata_source/status/completeness`）只是**软信号**，对低质量数据「轻量降权，不硬过滤」。
6. **跨框架安装体验**：`npx skills add axjing/stockaskill -g` 自动识别框架并落到正确路径——**这是 lquant 的 skill 分发可以借鉴的分发范式**。

**借鉴优先级**：①「SQLite/本地湖为唯一查询层 + 增量带重叠」；②**禁令式数据纪律**（点名禁止的无日期 API）；③`thesis → postmortem` 的假设闭环；④workflow manifest「只解析不执行」的安全姿态；⑤「软信号 vs 硬过滤」的分级。
**坑**：回测是**逐日模拟 + 止损/再平衡**的脚本级实现，未见成本/容量/容量冲击/PIT 状态（ST/停牌/涨跌停）细节声明。

#### 2.E.2 iwencai-cli（shaw-baobao/iwencai-cli）——问财自动化，反面教材

- 仓库：<https://github.com/shaw-baobao/iwencai-cli>；Apache-2.0。
- **做法**：Playwright 驱动**本机 Chrome**（`channel="chrome"`）→ 打开 `https://www.iwencai.com/unifiedwap/result?w=<查询>` → **去掉 `navigator.webdriver` 等自动化特征**（README 自称「无反爬问题」）→ 从前端 DOM 抽取股票列表（合并问财固定左列 + 滚动右列双栏表格，自动翻页）→ 输出表格/JSON；Cookie 持久化到 `~/.iwencai-profile`。
- **README 自述限制**：结果列随查询语句动态变化（**输出的列名就是页面表头**）；单页通常 ≤50 条；**频繁大量查询可能触发速率限制**；**「本项目与同花顺/问财官方无任何关联，仅是对其公开网页结果的自动化封装」**。
- **为什么是反面教材**：
  1. **无 PIT 保证**：问财返回的是**当前**筛选结果，用它构造历史选股清单 → 直接踩 Alpha Illusion 的 P2（dynamic universe）与 P1（temporal integrity）。
  2. **对反爬对抗的依赖是生产级风险**：页面结构、限速、风控策略任一变化即失效。
  3. **列名由页面决定 → 数据结构不可契约化**，无法进入可审计流水线。
- **唯一可借鉴处**：自然语言 → 结构化筛选条件是**用户真正想要的交互**；正确做法是把这个意图编译成**lquant 自己的因子表达式/筛选 DSL**（像 Alpha-GPT 与 FactorGPT 那样），而不是去爬别人的页面。

#### 2.E.3 其他中文 A 股 LLM 项目（证据等级：README/搜索级，未读源码）

| 项目 | 形态 | 备注 |
|---|---|---|
| <https://github.com/Colin579M/AStock-AI-Agent> | 多智能体协作 A 股分析，Tushare/AkShare | 「实时数据抓取 + 智能对话 + 个股机构级分析」 |
| <https://github.com/liangdabiao/easy_investment_Agent_crewai> | **AKShare + CrewAI**，4 个专业 agent | 实时行情/财务/资金流/情绪；A 股特色优化 |
| <https://github.com/jesson-hh/financial-analyst> | 「觀瀾」A 股研究工作台，**24 个 AI sub-agent**，一条命令出深度报告（~10 分钟） | sub-agent 数量与质量无必然关系（见 §4.3） |
| <https://github.com/shuaiwang888/hithink-astock-selector> | 「智能选股 skill」，自然语言多条件筛选，返回符合条件股票 | 与 stockaskill 同类，skill 形态 |
| <https://github.com/cy-Yin/TradingAgents-CN-lite> | TradingAgents 轻量中文化，支持 A 股/港股/美股 | TradingAgents-CN 的轻量替代 |
| <https://github.com/keli941020-coder/ai-factor-lab> | AI 因子实验 | 证据不足，仅列出 |
| <https://gitee.com/at_He/TradingAgents-CN> | TradingAgents-CN 的 Gitee 镜像 | 说明中文项目常有 Gitee/GitHub 双发布 |

**共性坑**：绝大多数是「**多智能体 + akshare + 一份漂亮报告**」，**一律没有**：隐藏测试集、AST 未来函数检查、哈希链审计、成本核算、PIT 声明。**它们是「能演示」而非「可验证」。**

---

## 3. 横向对标：五个维度的可比性

### 3.1 Agent 编排范式谱系

| 范式 | 代表 | 状态传递 | 规划权 | 适合 lquant？ |
|---|---|---|---|---|
| **流水线（Pipeline）** | FinRobot V2、RD-Agent 的 R→D | 结构化产物逐级传递 | 人写死 SOP | ✅ **最契合**（可回测、可审计） |
| **辩论（Debate）** | TradingAgents 多空、FinRobot Bull/Bear/Judge | 消息追加 | LLM | ⚠️ 作为「观点生成器」可以，**作为「独立性/准确性来源」不行**（见 §4.3） |
| **层级（Manager–Analyst）** | FinCon、TradingAgents 的 PM | 上下级汇报 | LLM + 经理裁决 | ⚠️ 裁决权应交给确定性引擎（LLM_QUANT_FACTORY 的做法） |
| **人在回路（HITL）** | **Alpha-GPT 2.0** | 人注入想法 + 系统回报结果 | 人 + 工具 | ✅ **强烈推荐**（与 lquant 的 ADR 文化同源） |
| **演化闭环（Evolve）** | **RD-Agent(Q)**、FactorGPT 的 Reflect | 真实回测指标反馈 | LLM 提假设 | ✅ 强烈推荐（但反馈必须是确定性指标） |
| **单一 agent + 工具** | **lquant 现状**、stockaskill | 对话 + 工具返回 | LLM | ✅ 保持，补审计与结构化产物即可 |
| **自主多轮规划** | 多数「AI 炒股」demo | 自然语言 | LLM 全权 | ❌ **不要做** |

### 3.2 工具（tool）契约设计对比

| 项目 | 工具形态 | 关键设计 |
|---|---|---|
| TradingAgents | LangGraph 节点内 function tools | 每个数据源函数**必须带日期参数**；取不到历史就返回「不可用」 |
| FinRobot | 32 个纯 Python 算子（不是 LLM 工具） | **数字只能来自算子**，LLM 只能引用 |
| Alpha-GPT 2.0 | `compute / backtest / search / deploy / library` | **工具箱即研究流程的每一步**，天然可审计 |
| FactorGPT | `alpha_factor(df) -> DataFrame[date, symbol, factor]` | **固定签名 + 类型化 DSL + 字段白名单**；AST 检查未来函数 |
| LLM_QUANT_FACTORY | 受约束工具 + 确定性门禁 | **LLM 无权批准、无权读隐藏指标** |
| RD-Agent | 代码生成 + 容器内执行 | **生成代码在隔离容器跑**；上传文件拒绝可执行格式 |
| **lquant 现状** | MCP 工具（`lquant-market` 等） | 工具本身可用，但**调用过程不落库 → 无审计** |

**结论**：lquant 的工具**覆盖面**够，缺的是 (a) **每次工具调用的 as-of 时间戳与来源落库**，(b) **工具返回结构的类型契约**（现在正文流出，无 schema），(c) **写操作（下单/改配置）的显式禁止或人工审批**。

### 3.3 记忆与反思机制对比

| 项目 | 记忆内容 | 反思触发 | 是否与回测隔离 |
|---|---|---|---|
| **TradingAgents** | 决策 + **到期结算的真实收益与相对基准 alpha** + 反思段落 | **持有期到期**（确定性事件） | ✅ 回测写自己的 log |
| FinMem | 分层记忆（可调 cognitive span） | 新信息到达 | ❌（train 期记忆会被 test 使用） |
| LLM_QUANT_FACTORY | 因子知识库 + 证据链 + 四类日志 | 门禁失败（确定性） | ✅ 手动回测/截图**不进记忆** |
| FactorGPT | ChromaDB 因子知识库 + 实验记录（SQLite） | **IC 未达阈值**（确定性） | ✅ 前向测试独立于回测 |
| lquant 现状 | 对话历史 | 无 | N/A |

**结论**：**凡是把「反思」交给确定性事件（持有期到期 / 门禁失败 / IC 不达标）的项目，都更可信；凡是交给「LLM 自己觉得该反思了」的，都不可复现。** 这条边界非常清晰。

### 3.4 许可证与可用性对照（合规必查）

| 项目 | 许可证 | 商用提示 |
|---|---|---|
| TradingAgents | Apache-2.0 | 可用 |
| ai-hedge-fund | MIT | 可用 |
| FinRobot | Apache-2.0 | 可用（注意 TRADEMARK_POLICY.md） |
| FinGPT | MIT | 可用 |
| FinMem | MIT | 可用 |
| **TradingAgents-CN** | **混合：`app/`、`frontend/` 专有** | ⚠️ **关键目录不可商用** |
| **LLM_QUANT_FACTORY** | **PolyForm Noncommercial 1.0.0** | ⚠️ **非 OSI 开源，商用须书面授权** |
| FactorGPT | MIT | 可用 |
| paper2alpha | MIT | 可用（vendored 组件保留上游许可） |
| stockaskill | 未在 README 明确（需查 LICENSE） | ⚠️ 使用前核对 |
| iwencai-cli | Apache-2.0 | 可用，但**爬取行为本身有 ToS 风险** |
| RD-Agent | 见仓库 LICENSE（Microsoft） | 使用前核对 |

---

## 4. 可复现性与可信度：学术批评专章

> 本节回答任务中的关键问题：**「LLM 选股不可回测」是不是根本问题？** 答案是：**是，且已被量化。**

### 4.1 两篇核心批评文献

#### 4.1.1 The Alpha Illusion（arXiv 2605.16895，2026-05-16）

- 标题即结论：**《报告的 alpha 不应当被当作部署证据》**（[arXiv:2605.16895](https://arxiv.org/abs/2605.16895)，[HTML 全文](https://arxiv.org/html/2605.16895v1)，CC BY-NC-SA 4.0，复旦/上财/西财/东北大学/帝国理工/鹏城实验室；复现 harness：<https://github.com/hj1650782738/Trading>）。
- **点名系统**：FinCon、FinMem、TradingAgents、FinAgent、**QuantAgent**、FLAG-Trader；以及基准 **FinBen**。
- **三类混杂（confounders）**：
  1. **时间污染**：不是「代码里喂了未来价格」这么简单——**模型权重、后训练数据、检索语料可能已经吸收了事件后的新闻、复盘、年末总结**。原文：「The model need not explicitly access future prices to leak future information through semantic memory.」
  2. **真实摩擦**：给出了最小分解 `PnL_net = PnL_gross − Σ(c·|Δw| + s_t·|Δw| + κ·|Δw|^β)`，并强调**多 agent 系统还要额外扣除 token 成本与推理延迟**。
  3. **短窗口 + 研究者自由度**：LLM 系统在传统 quant 的自由度（因子定义、回看窗口、换手频率、成本假设、权重）之外**又叠加了一层**（模型版本、temperature、system prompt、few-shot、RAG 语料、记忆长度、人格、辩论轮数、输出解析规则、工具调用策略）。
- **关键量化证据（务必记住）**：

| 证据 | 数字 |
|---|---|
| 跨过预训练 cutoff 后 FinMem 总收益 | **↓ ≈71.85%** |
| 跨过 cutoff 后 QuantAgent Sharpe | **↓ ≈51.48%** |
| FinCon 报告 Sharpe | 单票 **2.37** / 组合 **3.27** |
| QuantAgent 报告方向准确率 | **50.7% – 63.7%** |
| FinBen GPT-4 FinTrade Sharpe | **1.51 ± 1.08**（**标准差超过均值一半**） |
| 5 系统 × 8 摩擦分项 = 40 格中**未建模**的 | **35 / 40** |
| 作者一年五票复现：TradingAgents Sharpe | **0.43 → 0.22**（扣佣金/代币/价差/冲击后），**低于 buy-and-hold** |
| 同上 QuantAgent | **−0.96 → −1.15**，**低于 buy-and-hold** |
| 多 agent 辩论在 4 模型 × 9 基准 = 36 组配置中的胜率 | **< 20%**；**增加轮数或 agent 数不改善、甚至变差** |
| LLM 对营收问题的答对率（Shah et al. 2025） | 大盘股 2017 年 **54.17%** vs **1995 年 6.32%**；且**越大越新的公司幻觉率越高** |
| LLM 可据以批量生产论文的候选信号数（Novy-Marx & Velikov 2025） | **30,000+**，警告「**工业化 HARKing**」 |
| 传统 ML 的月度个股 R² 上限（Gu et al. 2020） | 个股 **≈0.4%**，组合 **≈1–2%**；Harvey et al. 2016 建议 **t ≈ 3.0** 门槛 |

- **三个结构性错配（比评估问题更根本）**：
  1. **语言置信度 ≠ 可交易概率**：LLM 的输出来自语言空间的 next-token 分布，不是收益率空间的分布。系统把「口语化置信度」当仓位输入，就**继承了可测量的校准义务（ECE）**。
  2. **金融叙事能力 ≠ 数值执行能力**：TraderBench 显示**延长思考显著改善检索类任务，却几乎不改善交易表现**；期权任务上模型能认出策略、讲清概念，**却算不准 P&L、Greeks 与风险暴露**；Ma et al. 2025 发现同一数值状态**画成图比写成文字**表现好得多——瓶颈在数值执行，不在金融词汇。
  3. **参数化先验 = 未披露的隐性因子暴露（Parametric Prior Lock-in）**：模型在收到任何实时输入前，权重里已经带着稳定的行业/规模/风格/叙事偏好；这些偏好**不像传统因子暴露那样被度量、中性化或约束**，而是**藏在自然语言推理里，表现为「看起来自主的分析」**。给出了反事实诊断：在反向证据强度 ρ 下，**方向翻转率 / 自报置信度 / 实际仓位**三者至少一个应单调响应；被先验锁定的 agent **三者同时不响应**。
- **P1–P6 最小报告协议（可直接当作 lquant 的清单）**：

| 协议 | 失败模式 | 最低报告要求 |
|---|---|---|
| **P1 时间完整性** | 时间旅行 alpha；预训练/检索泄漏；语义未来泄漏 | 模型版本、知识 cutoff、后训练边界、检索语料时间戳、记忆更新规则；**至少一个 post-cutoff 或 PIT 窗口** |
| **P2 动态股票域** | 幸存者偏差、事后清洗的样本、静态域高估收益 | **时变可交易域**；退市/停牌处理；流动性过滤；指数成分变动；融券与卖空约束 |
| **P3 反事实稳健性** | 参数先验锁定、对反向证据不敏感、隐性行业/风格偏离 | 反向证据下的**方向翻转率、置信度变化、仓位变化**；行业中性/风格中性 prompt 测试 |
| **P4 认知校准** | 把语言置信度当交易概率 | **ECE、可靠性曲线、分 regime 校准**；任何用于 sizing/风控的概率或置信度都要样本外校准 |
| **P5 真实实现** | 毛利幻觉；预测准但净收益为负 | 分层扣减：价差、滑点、佣金、冲击、借券成本、**执行延迟、token 成本、推理延迟** |
| **P6 多智能体解耦** | 多智能体共识幻觉；同源模型误差相关；回声室/群体思维 | **单 agent 基线**、角色相似度、分歧率、辩论轮成本、协调延迟、**多 agent 净收益增量** |

- **分层适用**：主张越强、证据要求越严。作者的最终建议是**保守的模块化替代方案**：**把 LLM 当作「可审计的信息接口」，放在独立的校准、风控、执行模块的上游**。并明确肯定 LLM 在金融中的价值：「最强的同侧证据（Lopez-Lira & Tang 2023）表明 LLM 能从新闻标题中抽取与短期市场反应相关的**语义信号**——这**正是我们支持的模块化用法**。」

#### 4.1.2 Evaluating LLMs in Finance Requires Explicit Bias Consideration（arXiv 2602.14233，ICML）

- 作者含 **Alejandro Lopez-Lira（U Florida）、Dhagash Mehta（BlackRock）、Stefan Zohren（Oxford）** 等；[ar5iv 全文](https://ar5iv.labs.arxiv.org/html/2602.14233)；配套清单仓库 <https://github.com/Eleanorkong/Awesome-Financial-LLM-Bias-Mitigation>。
- **五宗罪（the five sins）**：**look-ahead（前视）/ survivorship（幸存者）/ narrative（叙事）/ objective（目标）/ cost（成本）**；它们**互相叠加**，共同制造「**有效性的幻觉（illusion of validity）**」。
- **文献审计结果（这是最有力的一句）**：综述 2023–2025 年 **164 篇**主要会议论文，**只有 26.8% 提到前视偏差，幸存者偏差仅 1.2%**。金融 LLM 论文数从 36（2023）涨到 250（2025），**+594%（6.9×）**。
- **从业者调研**：112 人参与，50 人完整作答；**74% 认为现成评测工具稀缺或不存在**（48% 稀缺 / 26% 不存在）；**50% 把「缺工具/框架」列为最大瓶颈**。
- **前视泄漏的两条通道（原文明确区分）**：
  1. **参数化知识泄漏**：模型在历史时点 t < cutoff 时「时间旅行」；**「声明了 cutoff 并不保证排除了 cutoff 之后的信息」**——实测有模型能给出其声明 cutoff 之后事件的具体信息。**建议：必须在开源模型上验证，因为闭源模型的系统提示与后训练更新不透明。**
  2. **外部知识泄漏**：RAG 检索。现代排序系统依赖**点击、链接、事后评估的重要性**等**在 t 时并不存在**的信号；文档会在**保留原发布日期**的情况下被持续编辑。例：「**限定检索 2020 年前的文档，仍会浮现受 2021 年美国国会山事件影响的材料**」。
  - 结论句：**「Performance metrics under these conditions do not estimate the returns of non-anticipative strategies. They instead reflect pseudo-PnL from a temporally contaminated environment.」**
- **结构性有效性框架（五项，按 pass/fail 二值诊断）**：
  1. **Temporal sanitation**：必须披露预训练/微调所用的**最新日历日期**；评测窗口必须尊重该披露；检索语料与索引须由每个模拟时点可得文档构成，mutable 源须用**带可验证 as-of 时间戳的归档快照**；**禁止**查询当今搜索引擎或事后装配的知识库；排序与索引**不得**使用点击/链接图/事后元数据；结构化数据须为 PIT 序列，**若已被事后修订清洗，须显式建模修订过程或视为受污染**；**必须做全面 trace 记录（prompts、检索文档或标识、工具输出、决策时间戳）**，使独立审计者能**逐动作**验证时间一致性。
  2. **Dynamic universe**：定义时变可交易域 𝒰_t；把退市/并购/失败实体纳入（只要在 t 时可观测）；基准题不得按事后新闻量/当今知名度抽样而不作校正；须报告**样本内退市实体占比**，并**分别报告幸存与非幸存实体的结果分布**。
  3. **Rationale robustness**：把 rationale 当**可测试对象**；关键事实陈述须能追溯到具体检索段落或结构化字段；**审计事实一致性以发现「引用了不存在的事件、错误数量、或 t 之后的信息」**；报告违规率而非挑选案例；**实体替换测试**（替换目标实体标识、固定其余 prompt，验证实体特定事实是否迁移）；**负对照**（打乱/无关输入，确认系统不会编出详细因果故事或自信交易）。
  4. **Epistemic calibration**：输出应含**预测分布 / 校准置信度 / 结构化不确定性**供下游风控消费；**行动空间必须包含显式弃权（No Trade / Do Not Know）**，且评测应把弃权当**有效结果**而非自动失败。
  5. **Realistic implementation constraints**：报净效用；测**完整 Δ_gen 分布**（按硬件/并发/工具使用条件），而非单一最佳时延；回测按 `P_{t+Δ_gen}` 成交或等价的滑点模型；**明确价差、费用、冲击假设**；报告 **token 用量与工具调用次数**并计入 C_M；**净指标为主结果**；对比须用**预算匹配（budget-matched）基线**。
- 对「门槛是否过高」的四点反驳中，最有价值的是最后一句：**「当研究者缺乏支撑可部署主张所需的数据时，恰当的反应是收窄主张本身。降低有效性门槛不会让科学民主化，它只会让错误民主化。」**

#### 4.1.3 相关文献（同族，建议纳入 lquant 的引用库）

| 文献 | 要点 | 链接 |
|---|---|---|
| **Chronologically consistent large language models** | 构造「只见到某时点之前语料」的模型，用于消除时间旅行；亦发表于 JFE | [arXiv:2502.21206](https://ar5iv.labs.arxiv.org/html/2502.21206) / [ScienceDirect](https://www.sciencedirect.com/science/article/pii/S0304405X26001455) |
| **Chronologically Consistent Generative AI** | 同族方法论 | [arXiv:2510.11677](https://browse-export.arxiv.org/pdf/2510.11677) |
| **The Memorization Problem: Can We Trust LLMs' Economic Forecasts?** | 直接质疑 LLM 经济预测因记忆而不可信 | [arXiv:2504.14765](https://ar5iv.labs.arxiv.org/html/2504.14765) |
| Wisdom of LLM Crowds（ACM TSC） | 预测市场问题上**训练 cutoff 污染**与集成 | [DOI](https://dl.acm.org/doi/10.1145/3839337) |
| AI's predictable memory in financial analysis | LLM 在金融分析中的记忆可预测性 | [ScienceDirect](https://www.sciencedirect.com/science/article/pii/S0165176525004392) |

### 4.2 各项目对「可复现」的态度分级

| 级别 | 项目 | 表现 |
|---|---|---|
| **A. 主动设限并写进 README** | **LLM_QUANT_FACTORY**（non-PIT 数据、截图非绩效、公开样例排除隐藏结果）、**TradingAgents**（专章 Reproducibility + 「不保证与任何已发表数字一致」）、**paper2alpha**（v0.1 因子体是 stub）、**ai-hedge-fund**（proof of concept，不下单） | 可直接引用其数字并同时引用其边界 |
| **B. 承认不确定但仍在宣传** | FinRobot（V0/V2 分级明确，但报告置信度未校准）、FinGPT（成本透明，forecaster 无回测） | 数字需自行复跑 |
| **C. 有回测但口径薄** | FactorGPT（工程纪律强，但主要交付是 UI/演示）、stockaskill（脚本级回测）、FinMem/FinCon/QuantAgent（短窗口 + 高 Sharpe） | 只作方法论参考 |
| **D. 无论文回测证据 / 无开源** | **Alpha-GPT 1.0/2.0** | 借接口设计，不借结论 |
| **E. 明示不可复跑** | **TradingAgents Issue #119**（论文声称回测、仓库无引擎、14 个月未回应） | **这是「评测造假」的温和形态：论文与代码脱节** |

### 4.3 「多智能体辩论能提升准确性」——证据不支持

- **Alpha Illusion 引用 Zhang et al. (2025)**：在多智能体辩论场景下，**4 个模型 × 9 个基准 = 36 组配置中，辩论胜率 < 20%**；**增加辩论轮数或 agent 数量不改善，甚至可能降低性能**（[arXiv:2605.16895](https://arxiv.org/html/2605.16895v1) §2.3）。
- **P6 多智能体解耦**要求的正是这个：必须报**单 agent 基线**、角色相似度、分歧率、辩论轮成本、协调延迟、**多 agent 净收益增量**。
- **根因**：同一家族/同一后训练来源的模型**误差高度相关**，多方辩论不是「独立专家聚合」，而是**同一先验的回声室**（Alpha Illusion §3.3 附录 D 指出：**人格 prompt 与多智能体辩论对移除共享先验均无效**）。
- **对 lquant 的直接含义**：如果要做多角色分析，**必须做「单角色基线 vs 多角色」的对照**，否则无法证明多花的 token 值钱。

### 4.4 「LLM 输出的约束/校验机制」清单（跨项目汇总）

| 机制 | 项目 | 作用 |
|---|---|---|
| **结构化输出（JSON mode + Pydantic / 结构化 agent）** | TradingAgents（v0.2.4 起 Research Manager/Trader/PM 结构化输出）、paper2alpha（Pydantic `ResearchCard`）、FinRobot（output contract gate） | 让下游能校验、能拒绝 |
| **AST / 静态检查** | **FactorGPT（未来函数检测）**、paper2alpha（qtype）、LLM_QUANT_FACTORY（类型化表达式树 + 字段白名单 + 信号时点检查） | 在**执行前**拦住前视与非法字段 |
| **沙箱执行（子进程 + 超时 + 内存上限 + import 白名单）** | FactorGPT、RD-Agent（容器） | 模型生成的代码不可信 |
| **确定性门禁不可被平均** | **LLM_QUANT_FACTORY** | 防止「综合分掩盖硬失败」 |
| **隐藏测试集不可见** | **LLM_QUANT_FACTORY** | 防 LLM 对测试集过拟合/泄露 |
| **溯源打标（是谁生成的）** | **FactorGPT**（`template` vs `llm`；`jev/local-model/heuristic`）、lquant（`mock` 前缀） | 防止兜底冒充模型输出 |
| **实体替换 / 匿名化** | **StockAgent**（匿名 Stock A/B）、Five Sins 的 entity substitution test | 判别「参数记忆 vs 上下文」 |
| **反向证据翻转率** | Alpha Illusion P3 / Lee et al. 2025 | 检测参数先验锁定 |
| **弃权作为合法输出** | Five Sins §3.4 | 防止把无知变成虚假精度 |
| **trace 落盘（prompt/检索/tool 输出/决策时间戳）** | Five Sins §3.1、RD-Agent（trace 目录）、TradingAgents（memory log） | **独立审计的入场券** |

---

## 5. 值得 lquant 借鉴的清单（按优先级）

> 每项给出：**要做什么 / 参考来源 / 落点 / 验收标准**。分期建议见 §7。

### P0（不补就等于「问 AI」结论不可引用）

#### B1. 工具调用全链路落库（trace 一等公民）
- **做什么**：`ask_runs` 增加 `agent_trace`（或新表 `ask_tool_calls`），记录每次工具调用的 **name / 入参 / 出参摘要 + 全文指针 / 开始与结束时间戳 / as-of 数据时点 / 数据来源 / 是否降级**。
- **来源**：Five Sins §3.1「comprehensive trace documentation … prompts, retrieved documents or identifiers, tool outputs, and decision timestamps」；RD-Agent 的 `UI_TRACE_FOLDER`；TradingAgents 的 memory log。
- **落点**：`src/lquant/agent/sessions.py`、`cli_agent.py`（当前 `_consume` 只写正文）、前端 `RunTrace`。
- **验收标准**：任取一条历史会话，能回答「这条结论读了哪些数据、数据在什么时点、有没有降级」。**这是 P1 时间完整性的最低要求。**

#### B2. LLM 输出的结构化契约 + 确定性门禁
- **做什么**：定义 `AgentVerdict`（Pydantic）：`{ticker|universe, as_of, direction, confidence, horizon, evidence: [{source, as_of, quote}], abstain: bool, rationale}`。**任何数字字段必须带 `source` 与 `as_of`**，否则拒绝落库。
- **来源**：TradingAgents 结构化输出 agent；paper2alpha 的 `ResearchCard`；FinRobot 的 output contract gate；LLM_QUANT_FACTORY 的六角色结构化制品。
- **落点**：`src/lquant/agent/schemas.py`（已存在，可扩展）+ skill 的输出约定。
- **验收标准**：结构化校验失败的输出**不落库**并显式报错；`abstain=true` 是合法结果。

#### B3. 「不可得就显式 withheld」原则
- **做什么**：skill/MCP 工具对历史时点取不到的数据，**必须返回 `unavailable` 标记 + 原因**，禁止静默用当前数据替代；`as_of` 参数成为**强制**参数。
- **来源**：**TradingAgents**（Yahoo 历史财报 → 「told they are withheld」；内部人交易同理）；TradingAgents Issue #1220 的修复要求原文。
- **落点**：MCP 工具契约 + `lquant-market` skill 文档 + ADR。
- **验收标准**：构造「trade_date=2020-01-15」的用例，断言社媒/新闻类工具**不会**返回晚于该日的内容。

#### B4. 成本核算进净收益
- **做什么**：按会话/按任务记录 token 用量与费用（CLI 侧可解析 usage），并在报告中给出「毛结论 vs 净（扣 token 成本）」；设置**预算上限**（超预算降级或中止）。
- **来源**：**RD-Agent（<$10、R 用贵模型 D 用便宜模型）**、TradingAgents（quick/deep 双档）、FinGPT（GPU 小时成本表）、Alpha Illusion P5、Five Sins #5。
- **落点**：`AgentConfig` + 成本面板 + `timeout_seconds` 旁边加 `budget_usd`。
- **验收标准**：任意任务能报出 token 数与费用；成本面板能按月聚合。

### P1（让 agent 产出「可回测的东西」）

#### B5. 「假设 → 因子表达式」工具链（Alpha-GPT 式）
- **做什么**：把「自然语言想法」编译成 lquant 的**因子表达式 DSL**，并强制走 `validate(AST/未来函数/字段白名单) → compute → evaluate(IC/RankIC/分层) → registry.put`。
- **来源**：**Alpha-GPT 2.0**（mining 层工具集：alpha 计算/回测评估/算法搜索/部署/库维护；**SOP 优先、LLM 不做全局规划**）；**FactorGPT**（固定签名 `alpha_factor(df)->DataFrame[date,symbol,factor]` + AST 检测 + 沙箱 + import 白名单）；**LLM_QUANT_FACTORY**（类型化表达式树 + 字段白名单 + 信号时点/未来函数检查）。
- **落点**：`src/lquant/factors/`（已有注册表与预处理）+ `factor-mining` skill + MCP 工具。
- **验收标准**：一条自然语言想法 → 一个通过全部门禁的因子 → 落 `factor` 注册表并可在回测页选中；**未过门禁的因子不得入库**。

#### B6. LLM 生成物的溯源徽章（防兜底冒充）
- **做什么**：任何 LLM 产物在 DB 与 UI 上带 `generated_by`（`llm:<model>@<provider>` / `template` / `heuristic` / `local-model`），并在报告头部显示。
- **来源**：**FactorGPT**（`template` 兜底打标、「A template product cannot pass as model output」、JEV tier 上报）。
- **落点**：lquant 已有 `mock` 前缀惯例 → 推广为通用字段。
- **验收标准**：把 provider 切成 `mock` 时，报告与 DB 均标记非 LLM。

#### B7. 真实结算的记忆/反思闭环
- **做什么**：agent 的每次「观点/建议」落库并**冻结快照**；持有期到期后**结算真实收益与相对基准 alpha**，自动生成一段反思；下一轮同类标的分析**携带**该反思。**回测运行不写这条记忆，记忆也不参与回测口径。**
- **来源**：**TradingAgents**（memory log 的 settling + reflection，且「Your own memory log is never written to」在回测时）。
- **落点**：新表 `agent_decisions` + 结算任务（挂到既有 task center）。
- **验收标准**：能列出「过去 N 次建议的已实现 alpha 分布」；能证明反思文本与结算数值一一对应。

#### B8. 隐藏测试集 + 数据可见范围冻结
- **做什么**：agent 任务声明 `data_visibility`（探索区/滚动验证区/**隐藏测试区**）；**隐藏测试指标永不进入 LLM 上下文**；LLM **无权**读取或批准。
- **来源**：**LLM_QUANT_FACTORY**（核心治理不变量）。
- **落点**：`factor-eval-task-queue` + `research/ml` 的 walk-forward 切分 + agent 的工具白名单。
- **验收标准**：注入式测试——把隐藏测试指标放进上下文时，断言被拦截并报警。

#### B9. 因子同质化/换皮识别
- **做什么**：因子入库计算 **AST 指纹**，做语义/行为（信号-收益）聚类，标记「同质簇」；新因子必须证明**独立组合贡献**而非公式换皮。
- **来源**：**LLM_QUANT_FACTORY**（AST 指纹 + 同质簇 + 边际贡献）；FactorGPT（LASSO 去冗余 + 留一法过拟合检验）。
- **落点**：`factors/evaluate/` + 因子报告契约。
- **验收标准**：故意投两个仅参数不同的因子，系统标记为同簇。

### P2（可信度与评测）

#### B10. 反事实与校准测试（做成哨兵脚本）
- **做什么**：①**实体替换测试**（把目标股票代码换掉、其余 prompt 不变，检查实体特定事实是否迁移）；②**反向证据翻转率**（给强反向证据，看方向/置信度/仓位是否至少一维单调响应）；③**负对照**（打乱输入，检查是否仍产出详细因果故事或自信交易）；④**ECE 校准**（若要用置信度做 sizing）。
- **来源**：**StockAgent**（匿名标的防泄漏）、Five Sins §3.3/§3.4、Alpha Illusion P3/P4。
- **落点**：`scripts/xval/` 下新增 LLM 哨兵（与既有 `xval-sentinel` 同构，**不进 CI**，发版前跑）。
- **验收标准**：哨兵能产出「翻转率 / 迁移率 / 校准曲线」并留档；回归时能对比。

#### B11. P1–P6 报告协议内化为 lquant 的「LLM 结论引用规范」
- **做什么**：在 ADR 与文档中固化：**任何引用 agent 结论的对外材料，必须同时声明 P1–P6 的满足情况；未满足的只能表述为「压力测试/概念验证」，不能表述为「可部署」。**
- **来源**：Alpha Illusion 的分层适用表（主张强度 ↔ 必需协议 ↔ **可用措辞**）。
- **落点**：新 ADR + `docs/` 模板。
- **验收标准**：文档模板里有 P1–P6 勾选表。

#### B12. 前向锁定预测（解决「回测永远事后」）
- **做什么**：让 agent 对未来事件产出**带概率的预测**，在结果存在前**锁定并写入 append-only 账本**，到期由**固定规则**机械结算，统计方向准确率 / Brier / 分桶校准，并公布历史 scorecard。
- **来源**：**FactorGPT 的 Headline Arena 通道**（结算标准出题时冻结、预测结果前锁定、第三方机械结算、发布 CRPS/Brier 校准 API）。
- **落点**：`ask_*` 上加「冻结预测」按钮 + 结算任务；可先只做内部 scorecard（不接第三方）。
- **验收标准**：账本不可改写（哈希链）；scorecard 可随时重算且与账本一致。

#### B13. 哈希链审计账本
- **做什么**：agent 的关键动作（结论、因子入库、策略批准、模拟盘成交）进入 **append-only 哈希链**，可校验未被改写。
- **来源**：**ai-hedge-fund**（paper ledger：「NAV is a track record, not a reset」）、**LLM_QUANT_FACTORY**（哈希链审计 + 公开脱敏快照可重算）。
- **落点**：复用 lquant 既有审计设施；`docs/` 记录链式规则。
- **验收标准**：`lq audit verify` 能重算全链并检出人为改行。

### P3（工程体验）

#### B14. 分发与安装体验
- **做什么**：让 skill 能被一条命令装到 ClaudeCode/Codex 等目录（lquant 现在靠工作区重建，已经不错；可参考其 CLI 体验）。
- **来源**：**stockaskill**（`npx skills add axjing/stockaskill -g` 自动识别框架与目标路径；一个 `SKILL.md` 多框架复用）。
- **验收标准**：文档里有「一行安装」。

#### B15. 密钥与配置三级纪律
- **做什么**：①密钥永不进 git 跟踪文件，只写 `.env`（0600）并保留 `${VAR}` 占位；②未解析占位符**在调用时**报错并指名变量（不在构造时崩）；③配置保存是**逐行 patch**，内容不变时文件字节不变。
- **来源**：**FactorGPT**（`config.yaml` 只存 `${VAR}`；`_build()` 拒绝且 `available()` 可见；save 逐行 patch）。
- **验收标准**：`git grep` 不到任何明文 key；缺失变量时报错信息含变量名。
- **备注**：lquant 已有 [`SECRET_HYGIENE.md`](../../SECRET_HYGIENE.md) 与 `.gitleaks.toml`，此项是**接续**而非新建。

#### B16. provider 故障转移
- **做什么**：数据源/模型 provider 列表化 + 熔断退避 + 失败显式上报。
- **来源**：FinRobot（7 provider with failover）、stockaskill（多源容错 + 熔断 + 配额）。
- **落点**：`news/`、`market/` 的数据源层。

### 明确「已具备、无需外借」的能力（避免重复建设）

| lquant 已有 | 外部对应物 | 结论 |
|---|---|---|
| 交叉验证对拍（`xval-sentinel`、三态 expect、位级对拍） | AlphaPurify/qlib 对拍 | **比调研到的所有 LLM 项目都更严格**，扩展到 LLM 维度即可（B10） |
| ADR 决策记录 | 无对应物 | **保持并扩展到 agent**（B11） |
| 单一真源注册表（`METHODS`） | AlphaPurify 的反面教材 | 保持 |
| PIT 指数成分（`index_cons.as_of`） | qlib 的弱项 | **自持，不需外借**（对应 P2 的一部分） |
| 自研回测引擎为唯一执行真源 | qlib 回测仅作对照 | 保持 |

---

## 6. 明确的坑（含理由与证据）

### 6.1 幻觉

- **幻觉不是均匀分布的**：LLM 对财务事实的答对率**随公司规模与时间接近度上升而大幅变化**——大盘股 2017 年 **54.17%**，同一类问题在 **1995 年仅 6.32%**，且**越大越新的公司幻觉率越高**（Shah et al. 2025，经 Alpha Illusion §2.2 引用）。
- **叙事偏差（narrative bias）**：LLM 被训练成「流畅且内部一致」，在碎片化、噪声大的金融数据上会**强行缝合出因果叙事**；实测 LLM 在摘要时存在**框架偏差**，倾向于**把中性或负面文本推向更正面**（Five Sins §2.3 引 Aryan et al.）→ **会抑制「微弱但重要」的风险信号**。
- **CoT 不是决策过程的透明记录**：CoT 可能是**事后合理化**，且对非语义因素敏感，在分布漂移下会退化（Five Sins §2.3 引 Chen et al. / Zhang et al.）→ **不要把 CoT 当审计证据**。
- **对冲建议**：B1（trace）+ B6（溯源徽章）+ B2（结构化证据字段，每条事实必须能追溯到 source+as_of）。

### 6.2 前视泄漏（这是最致命的坑）

- **两条通道**：参数化记忆 + 检索语料（Five Sins §2.1）；**声明 cutoff 不保证排除未来信息**。
- **检索通道的三个隐藏机制**：①排序依赖**点击/链接/事后重要性**（t 时不存在）；②文档**保留原发布日期但内容被持续改写**；③**索引/语料本身是事后装配的**（Five Sins §2.1 Issue 2）。
- **实证幅度**：跨 cutoff 后 FinMem 总收益 **↓71.85%**、QuantAgent Sharpe **↓51.48%**。
- **社媒/新闻类工具的典型失败**：TradingAgents Issue #1220 —— 历史日期的运行**仍然抓取「当前」StockTwits 与「当前上周」Reddit**，并放进一个声称覆盖历史窗口的 prompt。**修复方式就是「不可得则显式扣留」。**
- **A 股特有形态（务必列全）**：停牌/涨跌停（一字板不可成交）、ST 状态逐日、退市核销、复权口径（前复权用到了未来因子）、指数成分的 as-of、财报的**公告日 vs 报告期**（lquant 已有 `index_cons.as_of`，但财报的 PIT 需要检查）、以及**公司行动修订**。
- **对冲建议**：B3 + B5 + B8 + B10；并优先把「财报 PIT」与「逐日 ST/停牌/涨跌停」纳入 `lq data status` 的自检项。

### 6.3 不可复现

- **TradingAgents 自述**：「two runs of the same ticker and date can differ. This is expected … not a defect.」并承认**即使 temperature=0，provider 也不保证逐字节一致**，**推理模型变化最大**（因为其内部推理本身在采样）；并承认**即使在同一天、同一标的，社媒与新闻源也反映「现在」**。
- **它给出的最小可复现要求（可直接抄成 lquant 的字段）**：固定 **模型版本 + provider + temperature + prompt**；并且在**回测时只允许看到每个分析日已发布的数据**（v0.5.2）。额外它已经做对的两件事：**公司身份在任何 agent 运行前就确定性地从 ticker 解析**；**market analyst 的精确价格/指标声称被锚定在经验证的数据快照上**（修复了早期「报告里出现不同公司/编造价格」的问题）。
- **对 lquant 的要求**：会话表必须存 `{provider, model_id, temperature?, prompt_hash, skill 集合, mcp_tools 集合, workspace 快照 hash}`。**没有这些，ADR 无法对拍，事后无法解释差异。**

### 6.4 Token 成本与延迟

- **成本可以被公开量化**：RD-Agent(Q) **<$10** 达到约 2× ARR 且因子数少 70%+；FinGPT 微调 **$17.25 / 17.25h / 1×RTX 3090**；BloombergGPT 对比 **≈$2.67M / 53 天**。
- **成本吃收益是系统性的**：35/40 的系统×摩擦格未建模（Alpha Illusion Appendix B）；其一年五票复现中，**扣掉佣金/token/价差/冲击后 TradingAgents Sharpe 0.43→0.22、QuantAgent −0.96→−1.15**，**双双低于 buy-and-hold**。
- **多智能体的成本是乘性的**：辩论轮数 × agent 数 × 工具调用；而辩论胜率 <20%、加轮数不加分（§4.3）→ **多花的 token 常常是净负收益**。
- **延迟也是成本（P5）**：必须按 `P_{t+Δ_gen}` 成交或等价滑点建模；论文须报**完整 Δ_gen 分布**，单一最佳时延不支持部署主张。
- **对冲建议**：B4 + TradingAgents 式双档模型 + **强制报「单 agent 基线 vs 多 agent」的净收益增量（P6）**。

### 6.5 评测造假 / 有效性幻觉

- **论文与代码脱节**：TradingAgents 论文声称「comprehensive backtesting simulation」，Issue #119 问「回测引擎在哪」——**挂了 14 个月**才有回应（2025-07-08 → 2026-09-18）。
- **headline Sharpe 的统计虚高**：FinBen GPT-4 FinTrade Sharpe **1.51 ± 1.08**（标准差 > 均值一半）；Lo (2002) 的 `SE(ŜR) ≈ sqrt((K + ŜR²/2)/T)` 说明**小 T + 大 Sharpe 正是置信区间最宽的区间**；且论文若**不披露用的是 period Sharpe (K=1) 还是 annualized (K=252)**，**headline 数字之间不可比**。
- **工业化 HARKing**：LLM 可以从 **30,000+ 候选信号**里筛选并**生成整篇论文**（Novy-Marx & Velikov 2025）→ 传统 `t ≈ 3.0` 门槛（Harvey et al. 2016）在 LLM 时代更必要。
- **多 agent 共识幻觉**：辩论胜率 <20%，且**人格 prompt 与多 agent 辩论都无法移除共享参数先验**（Alpha Illusion §3.3 / Appendix D）→ 「五个 agent 都同意」**不构成独立证据**。
- **数字本身的静默腐坏**：FactorGPT 的 warnings-as-errors 理由值得逐字记住——**「本仓在意的失败模式不是抛异常，而是异常被静默吞成一个看似合理的数字」**（`np.corrcoef` 对退化截面返回 0、`np.nanmean` 对空切片告警后给 NaN）。
- **对冲建议**：B11（P1–P6 措辞分级）+ B10（反事实/校准哨兵）+ B9（同质化）+ 要求任何 LLM 项目的数字必须**同时**给出：窗口长度、Sharpe 约定（K=1 还是 252）、是否跨 cutoff、摩擦假设、成本。

### 6.6 合规与供应链坑

- **许可证与「开源」的错觉**：TradingAgents-CN 的 `app/`、`frontend/` 是**专有**；LLM_QUANT_FACTORY 是 **PolyForm Noncommercial（商用须书面授权）**。→ **凡是要抄代码，先读 LICENSE 而不是 README 的 badge。**
- **版权纠纷先例**：TradingAgents-CN README 声明 `tradingagents-ai.com` 未经授权使用其专有代码 → **A 股 LLM 项目已有商业侵权先例**。
- **爬第三方页面的脆弱性**：iwencai-cli 明确「与官方无任何关联」、需**去掉自动化指纹**、有**速率限制**、结果列由页面决定 → **不可作生产链路**，且存在 ToS 风险。
- **生成的代码必须隔离执行**：RD-Agent 的文档要求最完整（拒绝 `.pkl/.py` 上传、强制 UI token、CORS 白名单、生成代码在最小权限环境跑、不暴露 host 凭据与 Docker socket）。
- **同名撞车**：`AlphaGPT` 在 GitHub 至少被三个无关项目占用；检索必须用 arXiv 号。

---

## 7. 「不要做」清单（含理由与证据）

| # | 不要做 | 理由 | 证据 |
|---|---|---|---|
| N1 | **不要让 LLM 直接产出最终买卖/仓位并号称「可回测的 alpha」** | 报告的 alpha 不等于部署证据；前视污染、摩擦、研究者自由度三重混杂 | [Alpha Illusion](https://arxiv.org/abs/2605.16895)；FinMem ↓71.85%、QuantAgent Sharpe ↓51.48%；净 Sharpe 0.43→0.22 / −0.96→−1.15 |
| N2 | **不要用模型 cutoff 之内的窗口跑回测并当作证据** | 参数化记忆 + 检索污染两条通道；**声明的 cutoff 不可验证**，闭源模型连系统提示与后训练都不透明 | [Five Sins](https://ar5iv.labs.arxiv.org/html/2602.14233) §2.1；[Chronologically consistent LLMs](https://ar5iv.labs.arxiv.org/html/2502.21206)；[Memorization Problem](https://ar5iv.labs.arxiv.org/html/2504.14765) |
| N3 | **不要把「多智能体辩论」当独立性或准确性来源** | 36 组配置辩论胜率 <20%；加轮数/加 agent 不改善甚至变差；同源模型误差相关，人格 prompt 与辩论都**无法移除共享先验** | [Alpha Illusion](https://arxiv.org/html/2605.16895v1) §2.3、§3.3、Appendix D |
| N4 | **不要把对话截图 / 演示页面 / 公开样例当绩效证据** | 这类材料脱离数据快照与协议指纹，无法复算 | **LLM_QUANT_FACTORY**：截图「仅用于展示研究流程」，且明令**截图不进入自动研究记忆**；其公开快照刻意排除价格与证券级收益 |
| N5 | **不要让 LLM 自报置信度直接决定仓位或风险预算** | 语言空间置信度 ≠ 收益率空间概率；把口语置信度当输入就继承了 ECE 义务；校准误差会传导为决策次优 | [Alpha Illusion](https://arxiv.org/html/2605.16895v1) §3.1（ECE / Kadavath / Guo / Shen et al.）；[Five Sins](https://ar5iv.labs.arxiv.org/html/2602.14233) §3.4 |
| N6 | **不要用静态或事后筛选的股票池做评估** | 幸存者偏差会系统性高估；A 股尤其需要逐日 ST/停牌/涨跌停/退市/成分变动 | [Five Sins](https://ar5iv.labs.arxiv.org/html/2602.14233) §2.2（164 篇中仅 **1.2%** 提及）；P2（dynamic universe） |
| N7 | **不要把爬第三方页面（问财等）接入生产链路** | 无 PIT 保证（返回当前筛选结果）；列名由页面决定 → 不可契约化；对抗反爬是持续维护负担；官方无关联 + 速率限制 | [iwencai-cli](https://github.com/shaw-baobao/iwencai-cli) README 自述；Alpha Illusion P2；Five Sins §2.1 Issue 2（检索/来源污染） |
| N8 | **不要把 LLM 生成的因子直接入因子库** | 必须过类型/字段白名单/未来函数检查/沙箱，并做同质化去重与多重检验校正 | [FactorGPT](https://github.com/ZXTLQQ/FactorGPT)（AST 未来函数检测 + import 白名单 + 子进程隔离 + LASSO 去冗余 + 留一法）；[LLM_QUANT_FACTORY](https://github.com/khakhasshi/LLM_QUANT_FACTORY)（AST 指纹 + 同质簇 + DSR/PBO/FDR） |
| N9 | **不要为「多智能体」把重编排框架引进主 venv** | FinRobot 自己换了 AutoGen→Agents SDK→PydanticAI 三代；框架不是资产，领域层才是；lquant 的 CLI+skill+MCP 已够 | [FinRobot README](https://github.com/AI4Finance-Foundation/FinRobot)（三代并存 + 「not defined by any single agent framework」）；与既有 ADR-11 一致 |
| N10 | **不要假设 token 成本与推理延迟为零** | cost bias：毛利幻觉、预测准但净收益为负；35/40 摩擦格未建模 | [Five Sins](https://ar5iv.labs.arxiv.org/html/2602.14233) §2.5；[Alpha Illusion](https://arxiv.org/html/2605.16895v1) §2.3 + Appendix B |
| N11 | **不要接受「LLM 输出不可复现」作为免罪符** | 至少要固定并落盘 model/provider/temperature/prompt hash/能力集，否则 ADR 无法对拍、事后无法解释 | [TradingAgents README Reproducibility](https://github.com/TauricResearch/TradingAgents#reproducibility) 自己列的可复现手段；[Alpha Illusion](https://arxiv.org/html/2605.16895v1) §2.4（研究者自由度） |
| N12 | **不要把 CoT / 长篇推理当审计证据** | CoT 可能是事后合理化，对非语义因素敏感，分布漂移下退化 | [Five Sins](https://ar5iv.labs.arxiv.org/html/2602.14233) §2.3（引 Chen et al. / Zhang et al.） |
| N13 | **不要用「方向准确率」当交付指标** | 准确率提升不蕴含净收益提升（BTC RL 反例：分类准确率明显提升，累计收益下降） | [Alpha Illusion](https://arxiv.org/html/2605.16895v1) §2.3（引 Jang et al. 2025）；QuantAgent 50.7–63.7% 方向准确率但净 Sharpe 为负 |
| N14 | **不要把「论文声称有回测」当成有回测** | 论文与代码可以长期脱节 | [TradingAgents Issue #119](https://github.com/TauricResearch/TradingAgents/issues/119)（14 个月无回测引擎说明） |
| N15 | **不要抄代码前不看 LICENSE** | 混合许可/非商业许可普遍存在，已有侵权纠纷先例 | TradingAgents-CN（`app/`、`frontend/` 专有 + 侵权警告）；LLM_QUANT_FACTORY（PolyForm Noncommercial 1.0.0） |

---

## 8. 建议的分期落地路线（与 lquant 既有节奏对齐）

```
Phase A（可引用性）      B1 trace 落库 → B2 结构化契约 → B3 withheld 原则 → B6 溯源徽章
                              └─ 出口：任一条历史结论可被独立审计
Phase B（成本与记忆）     B4 成本核算 → B7 结算-反思闭环 → B16 provider 故障转移
                              └─ 出口：能报出「过去 N 次建议的已实现 alpha」与费用
Phase C（可回测衔接）     B5 假设→因子工具链 → B8 隐藏测试集 → B9 同质化识别
                              └─ 出口：自然语言想法能产出过门禁因子并进入回测页
Phase D（可信度常态化）   B10 反事实/校准哨兵 → B12 前向锁定预测 → B13 哈希链
                              └─ 出口：`make xval-sentinel` 增加 LLM 维度；scorecard 可重算
Phase E（规范与体验）     B11 P1–P6 ADR → B14 一行安装 → B15 密钥三级纪律（接续 SECRET_HYGIENE）
```

**原则**：**先让结论可被审计，再让结论可被回测，最后才让结论被引用。** 顺序颠倒会把错误数字自动化——这正是 lquant 在 qlib 移植计划里已经确立的原则（[`docs/research/00-borrow-and-port-plan.md`](../00-borrow-and-port-plan.md) §2）。

---

## 9. 证据链接总表

### 9.1 仓库与论文

| 对象 | 链接 |
|---|---|
| TradingAgents | <https://github.com/TauricResearch/TradingAgents> · [arXiv:2412.20138](https://arxiv.org/abs/2412.20138) |
| TradingAgents Issue #119（回测在哪） | <https://github.com/TauricResearch/TradingAgents/issues/119> |
| TradingAgents Issue #1220（社媒前视泄漏） | <https://github.com/TauricResearch/TradingAgents/issues/1220> |
| TradingAgents-CN | <https://github.com/hsliuping/TradingAgents-CN> · [DeepWiki](https://deepwiki.com/hsliuping/TradingAgents-CN/1-overview) · [lite 版](https://github.com/cy-Yin/TradingAgents-CN-lite) |
| ai-hedge-fund | <https://github.com/virattt/ai-hedge-fund> |
| FinRobot | <https://github.com/AI4Finance-Foundation/FinRobot> · [arXiv:2405.14767](https://arxiv.org/abs/2405.14767) · [arXiv:2411.08804](https://ar5iv.labs.arxiv.org/html/2411.08804) · [ai4finance 页面](https://ai4finance.org/research/finrobot-open-source-ai-agent.html) |
| FinGPT | <https://github.com/AI4Finance-Foundation/FinGPT> · [arXiv:2306.06031](https://arxiv.org/abs/2306.06031) · [HF 模型](https://huggingface.co/FinGPT) · [Forecaster Space](https://huggingface.co/spaces/FinGPT/FinGPT-Forecaster) |
| FinMem | <https://github.com/pipiku915/FinMem-LLM-StockTrading> · [arXiv:2311.13743](https://arxiv.org/abs/2311.13743) |
| FinCon | [arXiv:2407.06567](https://ar5iv.labs.arxiv.org/html/2407.06567) · [NeurIPS 2024](https://proceedings.neurips.cc/paper_files/paper/2024/file/f7ae4fe91d96f50abc2211f09b6a7e49-Paper-Conference.pdf) |
| StockAgent | <https://github.com/MingyuJ666/Stockagent> · [arXiv:2407.18957](https://ar5iv.labs.arxiv.org/html/2407.18957v2) · [ACM DL](https://dl.acm.org/doi/10.1145/3844605) |
| InvestorBench | [arXiv:2412.18174](https://arxiv.org/abs/2412.18174) · [ACL 2025](https://aclanthology.org/2025.acl-long.126.pdf) · [EconPapers](https://EconPapers.repec.org/paper/arxpapers/2412.18174.htm) |
| QuantAgent (2024) | [arXiv:2402.03755](https://export.arxiv.org/pdf/2402.03755) |
| QuantAgent (2025, HFT) | [arXiv:2509.09995](https://huggingface.co/papers/2509.09995) |
| Alpha-GPT | [arXiv:2308.00016](http://xxx.itp.ac.cn/abs/2308.00016) |
| Alpha-GPT 2.0 | [arXiv:2402.09746](https://ar5iv.labs.arxiv.org/html/2402.09746) |
| RD-Agent / RD-Agent(Q) | <https://github.com/microsoft/RD-Agent> · [arXiv:2505.14738](https://arxiv.org/abs/2505.14738) · [arXiv:2505.15155](https://arxiv.org/abs/2505.15155)（NeurIPS 2025） · [arXiv:2407.18690](https://arxiv.org/abs/2407.18690) |
| Qlib | <https://github.com/microsoft/qlib> |
| DDG-DA | [arXiv:2201.04038](http://export.arxiv.org/pdf/2201.04038) |
| LLM_QUANT_FACTORY | <https://github.com/khakhasshi/LLM_QUANT_FACTORY> |
| FactorGPT | <https://github.com/ZXTLQQ/FactorGPT> · [在线 Demo](https://huggingface.co/spaces/ZXTLQQ/factorgpt-demo) |
| paper2alpha | <https://github.com/VernonOY/paper2alpha> |
| AlphaGPT_Tushare | <https://github.com/LilianaMajamay/AlphaGPT_Tushare> |
| stockaskill | <https://github.com/axjing/stockaskill> |
| iwencai-cli | <https://github.com/shaw-baobao/iwencai-cli> |
| AStock-AI-Agent | <https://github.com/Colin579M/AStock-AI-Agent> |
| easy_investment_Agent_crewai | <https://github.com/liangdabiao/easy_investment_Agent_crewai> |
| financial-analyst（觀瀾） | <https://github.com/jesson-hh/financial-analyst> |
| hithink-astock-selector | <https://github.com/shuaiwang888/hithink-astock-selector> |
| ai-factor-lab | <https://github.com/keli941020-coder/ai-factor-lab> |

### 9.2 学术批评与评测

| 文献 | 链接 |
|---|---|
| **The Alpha Illusion**（P1–P6、净 Sharpe 复现、多智能体辩论 <20%） | [arXiv:2605.16895](https://arxiv.org/abs/2605.16895) · [HTML](https://arxiv.org/html/2605.16895v1) · [复现 harness](https://github.com/hj1650782738/Trading) · [Semantic Scholar](https://www.semanticscholar.org/reader/97a832d771e84aacdea39e20782d16d73336ea90) · [EmergentMind 摘要](https://www.emergentmind.com/papers/2605.16895) |
| **Evaluating LLMs in Finance Requires Explicit Bias Consideration**（五宗罪、164 篇审计） | [ar5iv 全文](https://ar5iv.labs.arxiv.org/html/2602.14233) · [清单仓库](https://github.com/Eleanorkong/Awesome-Financial-LLM-Bias-Mitigation) |
| Chronologically consistent LLMs | [arXiv:2502.21206](https://ar5iv.labs.arxiv.org/html/2502.21206) · [ScienceDirect](https://www.sciencedirect.com/science/article/pii/S0304405X26001455) |
| Chronologically Consistent Generative AI | [arXiv:2510.11677](https://browse-export.arxiv.org/pdf/2510.11677) |
| The Memorization Problem | [arXiv:2504.14765](https://ar5iv.labs.arxiv.org/html/2504.14765) |
| Wisdom of LLM Crowds（污染与集成） | <https://dl.acm.org/doi/10.1145/3839337> |
| AI's predictable memory in financial analysis | <https://www.sciencedirect.com/science/article/pii/S0165176525004392> |
| TraderBench（被 Alpha Illusion 引用） | 见 Alpha Illusion §3.2 参考文献 |
| FinToolBench（工具调用合规） | 见 Alpha Illusion §3.2 参考文献 |
| NBER: Assessing the Benefits of Optimized Agentic AI Systems for Asset Pricing | <https://conference.nber.org/conf_papers/f243428.slides.pdf> |
| 中文券商研报：RD-AGENT 实测（AI 驱动的因子挖掘框架） | <http://stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/strategy/rptid/832591048933/index.phtml> |
| 中文报道：TradingAgents 支持 A 股/港股/美股 | <https://cloud.tencent.com.cn/developer/article/2722205> |

### 9.3 本仓相关（内部链接）

- [`docs/AGENT_MODEL.md`](../../AGENT_MODEL.md) — agent 现状基线
- [`docs/research/00-borrow-and-port-plan.md`](../00-borrow-and-port-plan.md) — qlib × AlphaPurify 借鉴清单（本文与其原则一致，且该文已把 DDG-DA 列为不移植）
- [`docs/SECURITY.md`](../../SECURITY.md) — `skip_permissions` 的边界与风险
- [`docs/SECRET_HYGIENE.md`](../../SECRET_HYGIENE.md) — 与 B15 接续
- [`docs/agent-skill/SKILL.md`](../../agent-skill/SKILL.md) — skill 契约
- [`docs/FACTOR_VALIDATION.md`](../../FACTOR_VALIDATION.md)、[`docs/BACKTEST_VALIDATION.md`](../../BACKTEST_VALIDATION.md) — 与 B5/B9/B11 的落点

---

## 10. 收尾：三句话

1. **这个赛道最有价值的产出不是「多智能体」，而是「一整套把 LLM 关进确定性笼子的工程纪律」**——PIT/withheld、隐藏测试集、AST 未来函数检查、哈希链审计、成本核算、溯源打标。lquant 应该**只抄纪律，不抄热闹**。
2. **前视污染与不可复现是结构性的，不会被更强的模型自动解决**（参数化记忆、检索排序的事后信号、语言置信度与收益率空间的错配，都是结构而非能力问题）。**唯一可执行的应对是把 LLM 降级为「可审计的信息接口」，放在独立的校准/风控/执行模块上游**——这也是 Alpha Illusion 作者的最终建议，且它恰好就是 lquant 已有的架构方向（确定性回测引擎为唯一执行真源）。
3. **lquant 在「交叉验证对拍」上已经领先整个赛道，但这份优势目前只覆盖传统因子/预处理，没有覆盖 agent。** 把 `xval-sentinel` 的三态判定（match / diverge / upstream_broken）扩展到 LLM 维度（实体替换、反向证据翻转率、校准曲线），是投入产出比最高的一步。
