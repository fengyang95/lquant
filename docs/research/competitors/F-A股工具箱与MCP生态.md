# F · 新一代 A 股工具箱型开源项目与 A 股 MCP 生态调研

> 调研目标：为 lquant（Python Polars/DuckDB + Rust + Next.js 的 A 股日频选股平台）识别
> **A 股特有数据面缺口**、**值得借鉴的工程机制**、以及**不应投入的自研方向**。
>
> 调研时间：2026-10-08（环境时间）。
> 方法：GitHub Search API（实时 star / pushed_at）+ `raw.githubusercontent.com` 拉 README +
> `codeload.github.com` 下载**完整仓库源码本地审计** + `shields.io` 交叉验证活跃度 + web_search。
> **凡未经本人实测或源码验证的结论，一律标注「未核实」。**

---

## 0. 方法与可复现性说明

本次调研使用了两条非显而易见的取证通道，后续复现可直接用：

```bash
# 1) 完整仓库下载（github.com 网页不可达时仍可用）
curl -sL "https://codeload.github.com/OWNER/REPO/tar.gz/refs/heads/main" -o r.tar.gz

# 2) 实时 star / 最后提交（不限流，可交叉验证）
curl -sL "https://img.shields.io/github/stars/OWNER/REPO.json"
curl -sL "https://img.shields.io/github/last-commit/OWNER/REPO.json"

# 3) GitHub Search API：搜索桶独立限流（10 次/分钟），可拿到真实 stargazers_count / pushed_at
curl -sL "https://api.github.com/search/repositories?q=QUERY&sort=stars&per_page=20"
```

所有 star 数与 `pushed_at` 均来自 GitHub Search API 或 shields.io 的实测返回，非 README 自述。

---

## 1. 项目逐个评估

### 1.1 jangviktor-web/a-stock-data-quant —— 「A 股量化分析工具箱」

| 项目 | 值 |
|---|---|
| 仓库 | https://github.com/jangviktor-web/a-stock-data-quant |
| Star | **34**（shields.io 实测，2026-10-08） |
| 最后提交 | **2026-08**（shields.io `last-commit` 返回 "august"；README CHANGELOG 自述最新 v3.8.1 = 2026-08-28） |
| 规模 | **21,039 行 Python**，57 个 `.py`，97 个 `.md` |
| 许可证 | MIT-0 |
| 测试 | **无**。全仓库 `grep -rl "unittest\|pytest"` 返回空；无 `tests/` 目录 |
| CI | **无**（无 `.github/workflows`） |
| 形态 | 是 **AI Agent Skill 包**（SKILL.md + manifest.json + ClawHub/SkillHub 发布），不是可 import 的库 |

**工程形态判定：个人玩具级 + 营销过度的 Skill 包。**
理由是硬证据而非印象：

- 21k 行 Python，**零测试、零 CI**。README 用大量篇幅做「新手零基础快速上手」「一句话介绍」「Star History」，但没有任何一处给出测试或数据校验证据。
- 核心入口 `bin/quant.py` **2400+ 行单文件 CLI**（README 自述），是典型脚本堆叠而非模块化设计。
- **随包内置 base64 混淆的 `EM_API_KEY` 默认值**（README 明写「开箱即用，用户无需配置或知晓」）。这是把第三方 API 凭据硬编码进开源仓库——既违反 OpenClaw/ClawHub 的凭据规范（所以 v3.8.1 才补 `metadata.openclaw` 声明），也意味着该 key 随时可能被上游吊销/限流。**这是明确的反面教材，lquant 不可模仿。**
- 大量依赖**闭源商业 MCP**（广发证券 MCP `mcp.gf.com.cn`、同花顺 hithink-finance、东财妙想 mx-skills）作为「备用源」，这些源需要申请 Key、有配额、且随时可能变更——把商业闭源服务当作降级链的一环，可用性不可控。
- CHANGELOG 显示 2026-05 至 2026-08 三个月迭代 8 个大版本（v1→v3.8），功能以「新增命令」线性堆叠，v3.7.0 一次性修了 6 个数据源 bug（腾讯成交额单位错位、腾讯日K 302 重定向、同花顺北向结构变更、百度K线 403 废弃）——**这恰恰证明其数据层缺乏契约测试，靠线上报错驱动修复**。

#### 它覆盖的 A 股特有数据面清单（源码级核实）

来自 `lib/`、`bin/cn/` 目录结构（README「项目结构」章节 + 本地解包确认）：

| 数据面 | 命令 | 实现文件 | 数据源 |
|---|---|---|---|
| 筹码分布 | `chip` | `lib/chip_distribution.py`（263 行，**移植自 go-stock**） | 自算（K线+换手率） |
| 形态识别 | `pattern` | `lib/patterns.py`（391 行，**移植自 KlangAlpha/Klang**） | 自算 |
| 融资融券 | `market` / `fund` | `lib/akshare_data.py` + `lib/sources_datacenter.py` | akshare → 东财数据中心 |
| 限售解禁 | `info` / `cn/research.py unlock` | 同上 | akshare → 东财 |
| 大宗交易 | `info` / `cn/research.py` | `lib/sources_datacenter.py` | akshare → 东财 |
| 股东户数 | `info` | `lib/akshare_data.py` | akshare → 东财 |
| 股东增减持 | `cn/research.py` | 同上 | akshare |
| 龙虎榜 | `lhb-gf` / `market` | `lib/sources_gf.py`（广发MCP）/ akshare | 广发 MCP / 东财 |
| 北向资金 | `market` | `lib/sources_hexin.py`（同花顺） | 同花顺 → akshare |
| 涨停/跌停池 | `market` / `hotspot` | `lib/akshare_data.py` | akshare |
| 板块资金流 | `board-flow` | `lib/board_fund_flow.py` | 东财 `data.eastmoney.com/dataapi/bkzj` |
| 估值分位 | `valuation` / `index-val` | `lib/valuation.py` | 东财 datacenter + 百度 |
| F10 财务 | `finance` | `lib/f10_finance.py` | 东财 datacenter |
| 研报/公告/互动易 | `report`/`notice`/`interactive` | `lib/stock_notice.py` | 东财 reportapi / np-anotice / 巨潮 |
| 宏观 | `macro` / `cn/macro.py` | 见 §1.1 下方接口清单 | akshare |
| 期货主连 | `cn/futures.py` | 纯标准库 | 腾讯/新浪 |
| 期权 | `cn/options.py` | akshare | 上交所/CFFEX |
| 可转债 | `cn/research.py cb-list/cb-quote` | akshare | 集思录 |
| 市场温度 | `market-temp` | `lib/market_temp.py` | 5 维加权 |
| 商誉 / 股权质押 | — | **未发现** | — |

**源码中真实出现的 akshare 接口名**（`grep -rhoE "ak\.[a-z_0-9]+" lib/ bin/`，去掉无关项后）：

```
# 两融 / 解禁 / 大宗 / 股东
stock_margin_sse            stock_margin_detail_sse     stock_margin_detail_szse
stock_restricted_release_queue_sina   stock_restricted_release_summary_em
stock_dzjy_mrmx             stock_dzjy_mrtj
stock_zh_a_gdhs_detail_em   stock_ggcg_em               stock_gdfx_top_10_em
stock_gdfx_free_top_10_em   stock_repurchase_em
# 筹码（重要！）
stock_cyq_em
# 宏观
macro_china_cpi  macro_china_ppi  macro_china_gdp  macro_china_lpr
macro_china_money_supply  macro_china_pmi  macro_china_gyzjz
macro_china_trade_balance  macro_china_urban_unemployment
# 其他
stock_lhb_detail_em  stock_lhb_ggtj_sina  stock_lhb_stock_detail_em  stock_lhb_stock_detail_date_em
stock_hsgt_hist_em  stock_zt_pool_em  stock_zt_pool_dtgc_em  stock_sector_fund_flow_rank
stock_individual_fund_flow  stock_hot_rank_em  stock_zh_valuation_baidu
stock_fhps_em  stock_fhps_detail_em  stock_yjyg_em  stock_yjkb_em  stock_yysj_em
stock_xgsglb_em  stock_report_fund_hold  stock_industry_pe_ratio_cninfo
stock_financial_abstract  stock_financial_abstract_ths  stock_financial_analysis_indicator
stock_profit_sheet_by_quarterly_em  stock_balance_sheet_by_quarterly_em  stock_cash_flow_sheet_by_quarterly_em
stock_a_all_pe  stock_a_high_low_statistics  stock_buffett_index_lg  stock_ebs_lg
stock_market_activity_legu  stock_news_em
bond_cb_jsl  fund_etf_spot_em  fund_etf_category_sina
option_finance_board  option_sse_list_sina  option_value_analysis_em  index_option_50etf_qvix
```

> 注：以上接口名是**在该仓库源码里出现的字符串**，因此「该仓库确实调用了它」已核实；
> 但「akshare 中该接口当前是否仍可用、字段口径如何」**未逐条核实**，接入 lquant 前必须实测。

#### 实现质量与可复用性

**结论：薄封装 + 少量自研算法，整体不可直接复用，但有两个文件值得读。**

- **是 akshare 的薄封装吗？部分不是。** 有价值的是两处**非 akshare** 的自研/移植件：
  - `lib/chip_distribution.py`（263 行）——自研 CYQ 算法，**算法在 docstring 里有明确口径说明**（见 §3.1），这是全仓库质量最高的模块之一。
  - `lib/patterns.py`（391 行）——规则式形态识别，移植自 KlangAlpha/Klang。
  - `lib/data_cache.py`（281 行）——CSV+JSON 四档 TTL 缓存。
  - `lib/sources_datacenter.py`（227 行）——东财数据中心直连（绕过 akshare 的降级源）。
  - 其余（行情、宏观、两融、解禁、大宗、股东）**基本是 akshare 的一行调用 + try/except 降级**。
- **`lib/fallback.py` 只有 68 行**——所谓「多源自动降级引擎」是一个轻量装饰器（README 展示了 `@_with_fallback`），不是真正的熔断/重试/健康度体系。
- **无任何口径文档、无字段单位契约、无数据校验。** 反证：v3.7.0 的修复清单里出现「腾讯实时行情成交额字段单位错位（万元被当元使用）」「腾讯日 K http 被 302 重定向」——这类 bug 如果有一张字段单位契约表 + 一个契约测试，根本不可能进主干。
- `references/` 目录（97 个 md）是给 AI Agent 读的文档，不是工程文档。

#### 对 lquant 的价值判定

**(a) 数据面缺口**：见 §2 汇总表。它的最大作用是**当 akshare 接口名的「路标」**——从它源码里一次性拿到两融/解禁/大宗/股东户数/宏观的真实接口名，再去 akshare 核实，可省掉大量摸索。

**(b) 值得借鉴的工程机制**：
1. `chip_distribution.py` 的**算法口径 docstring 写法**（把「衰减 + 高斯核 + 成本中枢优先级」写成可评审的文字），值得作为 lquant 因子的口径文档模板。
2. **多源降级的「透明性」设计**：降级提示输出到 **stderr**，不污染 stdout 的 JSON——lquant 的 CLI/API 若做降级，这条值得抄。
3. **惰性依赖**（`akshare` 缺失时只有相关命令报错，价量路径纯标准库不受影响）——对 lquant 的「数据源可选安装」有参考价值。
4. **`lib/data_cache.py` 的四档 TTL 缓存**思路（不同数据面不同新鲜度要求）——但实现是 CSV，lquant 用 DuckDB 显然应做得更好。

**(c) 是不是一堆脚本不值得借鉴？**
**主体是。** 但**不是零价值**：`chip_distribution.py` 与 `patterns.py` 是两个可独立阅读的算法文件（合计 654 行），降级的「stderr 不污染 stdout」约定值得抄。**其余 20k 行不建议阅读**。

#### 坑（实测/源码可见）

1. **硬编码混淆 API Key**——供应链与合规风险。
2. **零测试零 CI**——已经导致真实线上 bug（单位错位、302、结构变更）。
3. **依赖闭源商业 MCP 做降级**——把可用性押在别人的配额和政策上。
4. **`lib/patterns.py` 的阈值全是魔法数**（`0.05` / `0.15` / `0.6` / `0.3`），无任何标定或回测验证依据，过拟合风险极高。
5. **CYQ 未处理复权**：若输入前复权 K 线，历史价格被整体平移，筹码成本分布会被系统性扭曲（详见 §3.1）。
6. **数据源失效是常态**：自身 CHANGELOG 记录腾讯 302、同花顺北向结构变更、百度 K 线 403 废弃——**它本身就是「A 股数据源极易失效」的活证据**，lquant 应据此设计多源 + 契约测试，而非单源硬依赖。

---

### 1.2 AllenPan-Git/SimpleQuant —— 低代码 A 股回测工具

| 项目 | 值 |
|---|---|
| 仓库 | https://github.com/AllenPan-Git/SimpleQuant |
| Star | **26**（shields.io 实测） |
| 最后提交 | **2026-10-07**（shields.io `last-commit` 返回 "yesterday"） |
| 规模 | **30,893 行 Python**，152 个 `.py` |
| 测试 | **有**。`tests/` 目录含 16+ 测试文件（`test_engine.py`、`test_factors.py`、`test_corporate_actions.py`、`test_walkforward.py`、`test_allocation.py`、`test_credibility.py`、`test_bonds.py`、`conftest.py`…）+ `pytest.ini` + `tools/smoke_test.py` |
| CI | **有**（`.github/workflows/build.yml`） |
| 形态 | 桌面 GUI（PyInstaller `.spec` + `build.bat`/`build.sh`）+ 库 |

**工程形态判定：个人项目里质量最扎实的一个，但数据面窄。**
30k 行、152 个模块化 `.py`（`data/ engine/ stocks/ bonds/ allocation/ export/ llm/ paper/ rules/ plan/`）、有 tests、有 CI、有 CHANGELOG。**不是玩具**，但 star 只有 26 —— 说明「工程质量」和「传播度」在 A 股工具生态里几乎不相关，评估时不能以 star 定质量。

#### 它覆盖的 A 股特有数据面（源码 + README 核实）

**数据面很窄，但每个点都做得比 astock-data-quant 深：**

| 数据面 | 覆盖 | 说明（README 原文核实） |
|---|---|---|
| 历史成分股 | ✅ | 「在沪深300、中证500 的**历史成分股**中按…因子评分」——**显式规避幸存者偏差** |
| 复权 | ✅ **口径明确** | 「个股下载**不复权价与复权因子**（收益使用**后复权价**，整手按**真实价格**计算）」 |
| 停牌/ST/涨跌停 | ✅ | 含 PE/PB/PS、换手率 |
| **可转债** | ✅ **最深** | 全市场含**已退市**可转债；双低、溢价率因子；`bonds/` 模块含**利率与信用利差** |
| 交易规则 | ✅ **细** | 「股票与境内股票 ETF 为 T+1；债券、货币、黄金、商品、跨境 ETF 及可转债为 T+0，**按代码和名称自动判断**」；100 股整手、卖出印花税、最低佣金、滑点、**收盘信号→次根开盘成交** |
| 因子研究 | ✅ | Rank IC、分层回测、因子相关性、行业/市值中性化、IC/ICIR 加权、滚动优化 |
| 组合配置 | ✅ | 按风险等级配置 + **波动率倒数（风险平价）**；**明确拒绝均值-方差**（「它对预期收益的估计误差极为敏感」） |
| 模拟盘 | ✅ | paper 模块 + `paper_daily.sh/bat` |
| LLM | ✅ | `llm/` 模块（一句话描述选股思路） |
| 融资融券/解禁/大宗/股东户数/质押/商誉/筹码/宏观/形态 | ❌ | **README 与模块结构均未发现** |

**已知的诚实度亮点（值得 lquant 学习）** —— README 主动写明了自己的坑：

- 「可转债：新浪日线为**不复权价**、成交量单位为「**张**」，个别转债**缺上市初期的日线**（这些天只用东方财富的收盘价计算因子、不交易）」
- 「东方财富给出的评级是**最新评级**，用于历史回测会有**前视偏差**，因此没有作为选股条件」
- 「未使用均值-方差优化：它对预期收益的估计误差极为敏感」
- 「暂不支持：择时规则中的估值/换手/利率类因子、分钟线、可转债选股」

**这几条是本次调研中最高密度的「口径诚实度」样本**，直接可作为 lquant 数据面文档的写作范式。

#### 对 lquant 的价值判定

**(a) 数据面缺口**：**几乎不补**。它覆盖的（日线、复权、成分股、可转债、规则）lquant 基本已有；**唯一 lquant 弱的是可转债**（lquant 明确没有可转债），而 SimpleQuant 的 `bonds/` 模块（可转债面板 + 双低/溢价率因子 + 利率与信用利差 + 含退市标的）是本清单里**最完整的可转债开源实现**。

**(b) 值得借鉴的工程机制**：
1. **PIT 意识**：「最新评级用于历史回测会有前视偏差，因此不作为选股条件」——这是把 PIT 原则落到字段级。
2. **复权口径分离**：「不复权价 + 复权因子」存储，「收益用后复权、整手用真实价」计算——**与 lquant 的 PIT 财务是同一种哲学**，可直接对照。
3. **历史成分股**规避幸存者偏差。
4. **拒绝均值-方差**的工程判断（对 lquant 的「收缩协方差」是很好的反向论据：SimpleQuant 选择风险平价，lquant 选择收缩协方差，两者都优于裸 MVO——lquant 的设计是可辩护的）。
5. **明确的能力边界文档**（README 主动列「暂不支持」）。

**(c) 是否只是脚本堆不值借鉴？**
**不是。30k 行、有测试有 CI 的模块化工程，是本清单中第二值得精读的仓库**（第一是 a-share-quant 的工程机制，见 §1.3）。建议精读 `data/`、`bonds/`、`allocation/`、`rules/` 四个包。

#### 坑

1. 数据源单一：**BaoStock 为主**（README：「数据：BaoStock 免费数据」），BaoStock 本身更新慢（MCP 项目 README 引用 BaoStock 官方时间表：当日 17:30 日K入库、**次日 11:00 分钟线**）——不适合日内。
2. 全市场首次下载 8~10 分钟 / 约 80MB——数据管道偏重。
3. 可转债的**新浪不复权 + 成交量为「张」+ 缺初期日线**三个坑已自述，lquant 若做可转债需独立处理。
4. star 极少（26），社区验证不足，**issue 区几乎没有外部压力测试**（未核实 issue 数量）。

---

### 1.3 jojo232386/a-share-quant —— 可审计 A 股量化研究/回测系统

| 项目 | 值 |
|---|---|
| 仓库 | https://github.com/jojo232386/a-share-quant |
| Star | **0**（shields.io 实测） |
| 最后提交 | **2026-08** |
| 规模 | **66,002 行 Python**，134 个 `.py`，46 个 md |
| 测试 | **非常多**。`tests/unit/` 40+ 文件：`test_portfolio_equivalence.py`、`test_release_replay.py`、`test_gate_e_audit.py`、`test_corporate_actions.py`、`test_price_streams.py`、`test_a_share_execution.py`、`test_portfolio_availability.py`、`test_rolling_orchestration.py`… |
| CI | **有**（`.github/workflows/ci.yml`），另有 `ruff check`、`uv lock --check`、`uv build` |
| 依赖锁定 | `uv.lock` + `.python-version`（3.11） |

**工程形态判定：这是一份「工程质量示范品」，但几乎没有真实数据。**

**关键证据（README 原文，必须完整引用以定调）**：

> 「使用公开的**确定性合成夹具**验证发布链路；**它们不是真实行情**，也不构成实时数据服务。」
> 「不连接券商、不提交订单、不执行自动交易。」
> 「不证明策略盈利、Alpha 有效、真实成交可行或适用于全部市场环境。」
> 「对未支持的证券、历史时期或公司行为，项目应**拒绝近似运行**，而不是把结果包装为有效结论。」
> 「固定审计标签为 `v0.2-gate-e-public-audit`，其目标提交为 `577c157…`」

仓库里有真实数据相关的基建（`data/akshare_client.py`、`corporate_action_ingestion.py`、`calendar_snapshot.py`、`manifest.py`、`normalize.py`、`price_streams.py`、`corporate_actions.py`），但**发布验证链路走的是合成夹具**。
60k+ 行 / 10 个 parquet 夹具 / 0 star —— 这是典型的「为了展示可审计性而写的工程样本」，**不是可用的数据源**。

**它有 10 个 parquet 文件**，但那大概率是合成夹具而非真实行情（未逐个打开核实，但 README 的表述已足够定性）。

#### 它覆盖的 A 股特有数据面

**几乎没有。** 有价值的是它的**文档与验收体系**：

- `docs/scope.md`（研究范围）
- `docs/support_matrix.md`（支持矩阵）
- `docs/a_share_execution_rules.md`（**A 股执行规则**）
- `docs/data_quality_acceptance.md`（**数据质量验收**）
- `docs/research_loop_v1.md`
- `docs/known_limitations.md`（**已知限制**）
- `docs/engineering/risk_governance.md`（**风险治理与 Blocker/Deferred 登记册**）
- `outputs/A股量化项目_v0.2_Gate_E交付与验收.md`（交付与验收）
- 代码层：`release_manifest.py`、`release_replay.py`、`release_synthetic.py`、`release_network.py`、`corporate_action_ingestion.py`、`price_streams.py`

#### 对 lquant 的价值判定

**(a) 数据面缺口**：**零**。不要指望它提供数据。

**(b) 值得借鉴的工程机制：这是本清单里最高的一个。**
1. **「拒绝近似运行」原则**：对未支持的证券/时期/公司行为**报错而非给近似结果**——这条直接适用于 lquant 的 A 股规则引擎与回测：宁可 skip，不可静默近似。
2. **release manifest + replay**：可复现发布链路（`release_manifest.py` / `release_replay.py`），配 **Gate E 验收门**与**固定审计标签 + 目标 commit hash**。lquant 的「任务中心」「因子评价」「回测」若要有可信度，这套 versioned artifact + replay 验证机制值得完整移植。
3. **`test_portfolio_equivalence.py` / `test_portfolio_verify.py`**：**组合等价性测试**——同一组合用两条不同代码路径/不同数据布局计算，断言结果等价。这对 lquant 的「组合（权重/去重/收缩协方差/基准相对优化）」是极好的回归测试范式（收缩协方差有多种实现，等价性测试能锁住口径）。
4. **`risk_governance.md`**：显式维护 **Blocker/Deferred 登记册**——把「已知做不了的事」当成一等公民管理。
5. **`data_quality_acceptance.md` + `calendar_snapshot`**：数据质量验收 + 交易日历快照化。
6. 工具链卫生：`uv.lock` 锁定 + `ruff` + `uv build` + CI，均可在 CI 中验证。

**(c) 是否只是脚本堆不值借鉴？**
**完全不是，但价值全在「工程机制」而非「数据/策略」。** 它证明了在 A 股量化里，「可审计 / 可复现 / 拒绝近似」是可以工程化的。**建议精读 `docs/` 全部 7 篇 + `release_*.py` + `tests/unit/test_portfolio_equivalence.py`。**

#### 坑

1. **0 star，无社区验证**，单一作者，代码量巨大（66k 行）而 star 为 0——存在过度工程风险，不能整体照搬其复杂度。
2. **验证数据是合成的**，任何「收益/绩效」结论都不可当真；若误以为其 `outputs/` 是真实回测结果会严重误导。
3. 无数据源的真实可用性验证（akshare client 有代码但发布链路不依赖真实网络——`release_network.py` 的存在暗示有网络隔离/禁网策略，未核实具体行为）。

---

### 1.4 24mlight/a-share-mcp-is-just-i-need（及 A 股 MCP 生态）

#### 1.4.1 本仓

| 项目 | 值 |
|---|---|
| 仓库 | https://github.com/24mlight/a-share-mcp-is-just-i-need |
| Star | **646**（shields.io 实测） |
| 最后提交 | **2025-12**（shields.io 返回 "december 2025"）→ **已停更约 10 个月** |
| 规模 | **2,644 行 Python**，28 个 `.py` |
| 测试 | **无**（`find -iname "*test*"` 为空） |
| CI | **无** |
| 框架 | FastMCP，Python 3.10+/3.12+，uv |

**工程形态判定：中小型、无测试、已停更，但架构分层是全清单里最标准的 MCP 参考。**

源码结构（本地解包核实）：

```
mcp_server.py
src/baostock_data_source.py        数据源实现
src/data_source_interface.py       ← 数据源抽象接口（关键设计）
src/formatting/markdown_formatter.py  ← 输出格式化（喂 LLM 用）
src/tools/
    base.py  stock_market.py  financial_reports.py  indices.py
    market_overview.py  macroeconomic.py  analysis.py  date_utils.py  helpers.py
```

**MCP 工具注册方式**：每个模块导出 `register_xxx_tools(app: FastMCP, active_data_source: FinancialDataSource)`，
即 **工具按域分组注册 + 数据源依赖注入**。这个模式对 lquant 的「LLM Agent（MCP 工具 13 个）」有直接参考价值。

**它覆盖的数据面**：**仅 Baostock**。工具域为 股票行情 / 财务报表 / 指数（成分股）/ 市场概览 / 宏观 / 分析 / 日期工具 / 辅助。
→ **A 股特有数据面几乎为零**（无两融、无解禁、无龙虎榜、无北向、无资金流、无筹码、无形态）。
它的价值在于 **MCP 工具设计范式**，不在于数据。

**已知坑**：README 自述「本项目于 **Windows 环境下开发**」——跨平台未保证；Baostock 数据延迟（引用官方时间表：日K 当日 17:30、复权因子 18:00、**分钟线次日 11:00**、财务次日 1:30、周线周六）。

#### 1.4.2 A 股 MCP 生态全景（GitHub Search API 实时 star / pushed_at）

搜索 `mcp a股`，按 star 排序的实测结果（**star 与 pushed_at 为 2026-10-08 实测**）：

| 仓库 | Star | 最后推送 | 定位（描述原文摘要） | 对 lquant 相关性 |
|---|---|---|---|---|
| [hello245m/free-stockdb](https://github.com/hello245m/free-stockdb) | **2813** | 2026-10-04 | A股日K/分钟K/ETF分钟本地量化引擎，增量同步、本地缓存、复权、批量查询、回测、指标 | **中**（数据引擎，非数据面补缺） |
| [simonlin1212/Vibe-Research](https://github.com/simonlin1212/Vibe-Research) | **2635** | 2026-10-07 | 个人投研 Agent：每日复盘、资讯雷达、个股数据、板块中心、持仓、研究记录、回测 | 中（Agent 产品形态） |
| [TNT-Likely/PanWatch](https://github.com/TNT-Likely/PanWatch) | **2042** | 2026-10-05 | A股/港股/美股 AI 监控，基于 TradingAgents | 中 |
| [zhangxiangliang/stock-api](https://github.com/zhangxiangliang/stock-api) | **1960** | 2026-10-08 | A股/美股/港股/场内基金行情，Node.js/浏览器/CLI/MCP 接入 | 低 |
| [TickDB/tickdb-unified-realtime-marketdata-api](https://github.com/TickDB/tickdb-unified-realtime-marketdata-api) | 853 | 2026-06-20 | AI-native 实时行情 API | 低（商业 API） |
| [agentpit-io/hunter-community](https://github.com/agentpit-io/hunter-community) | 570 | 2026-10-05 | 腾讯 WorkBuddy 金融版开源本地替代 | 低 |
| [electkismet/eltdx](https://github.com/electkismet/eltdx) | **564** | 2026-10-05 | **通达信 A 股行情协议 Python 库 + MCP**，快照/分时/逐笔/K线/集合竞价/特殊指标 | **高**（lquant 已有 mootdx，可对照） |
| [elsejj/mcp-cn-a-stock](https://github.com/elsejj/mcp-cn-a-stock) | **460** | 2026-09-30 | 为大模型提供 A 股数据的 MCP 服务 | 中 |
| [rootSunc/CNEquity](https://github.com/rootSunc/CNEquity) | **337** | 2026-10-07 | **中国 A 股数据基础设施。55 个日更数据集：行情/基本面/期货/资金面/公告事件/指数行业/宏观与风险。行级溯源、PIT 语义、复权与历史成分内置，MCP 原生。自托管，零注册零 Token** | **最高**（见下） |
| [shouldnotappearcalm/a-share-skill](https://github.com/shouldnotappearcalm/a-share-skill) | 243 | 2026-09-28 | A股数据分析/量化选股/模拟交易 Skill | 中 |
| [fqgate/FQGate-agent](https://github.com/fqgate/FQGate-agent) | 242 | 2026-10-08 | 同花顺免费开源 AI 插件，Level-2、资讯 | 低 |
| [huweihua123/stock-mcp](https://github.com/huweihua123/stock-mcp) | 178 | 2026-03-25 | 金融数据 MCP（A股/美股/加密货币） | 低 |
| [rancy777/quantdash-ai-stock](https://github.com/rancy777/quantdash-ai-stock) | 165 | 2026-04-21 | A股看盘/复盘/盘前计划 + 情绪周期 + 板块轮动 + MCP + 飞书 | 中 |
| [wolfjkd/tradex-hub](https://github.com/wolfjkd/tradex-hub) | 56 | 2026-10-06 | **129 个 MCP 工具** · eltdx 通达信 + akshare + 同花顺 + 东财 + 本地 · **SmartRouter 多源降级路由** · 免费零鉴权 | **高**（MCP 工具设计参考） |
| [ccq1/cn-financial-mcp](https://github.com/ccq1/cn-financial-mcp) | 43 | 2026-03-15 | A股行情/财报/估值/板块/市场全景/新闻/宏观 | 中 |
| [adambbhe/TDX-finance-mcp-plugin-v3](https://github.com/adambbhe/TDX-finance-mcp-plugin-v3) | 38 | 2026-07-04 | 通达信 MCP 插件，6 工具 + 45 分析技能 | 低 |
| [shouldnotappearcalm/a-share-mcp](https://github.com/shouldnotappearcalm/a-share-mcp) | 27 | 2026-03-18 | MCP A股数据，新浪+腾讯双源 | 低 |
| [Lzh-xbccz/hermes-finance](https://github.com/Lzh-xbccz/hermes-finance) | 27 | 2026-06-22 | 多市场 MCP+Skills，七维证据投票 + 缠论确认 | 低 |

**生态判断**：
1. **A 股 MCP 生态在 2026 年爆发但高度同质化**——绝大多数是「akshare/Baostock 包一层 MCP」，工具粒度是「查行情/查财报/查宏观」这种**取数原语**，与 lquant 已有的 13 个 MCP 工具层级相近。
2. **`rootSunc/CNEquity`（337★）是最需要单独立项调研的项目**——它的描述命中了 lquant 的多个关键词：**55 个日更数据集**、**行级溯源**（lineage）、**PIT 语义**、**复权与历史成分内置**、**MCP 原生**、**自托管零 Token**。如果描述属实，它可能是与 lquant 定位最接近、且数据面最宽的竞品/潜在上游。**⚠️ 未核实**：本次时间预算内未能下载其源码验证「55 数据集」「PIT 语义」是否名副其实，**列为下一轮首要调研对象**。
3. **`wolfjkd/tradex-hub` 的「129 工具 + SmartRouter 多源降级路由」**是 MCP 工具规模化的极端样本，可用作「工具数量是否越多越好」的反面参照（129 个工具对 LLM 的选择准确率是负担）。**⚠️ 129 数字来自仓库描述，未核实。**
4. **`electkismet/eltdx`（564★）**：通达信协议 + MCP，覆盖**快照/分时/逐笔/集合竞价/特殊指标**——lquant 已有 mootdx，但 eltdx 的**逐笔与集合竞价**是 lquant 可能缺的（lquant 是日频为主）。**未核实其协议实现细节。**

#### 对 lquant 的价值判定（MCP 生态）

**(b) 值得借鉴的工程机制**：
1. **`data_source_interface.py` 抽象 + `register_xxx_tools(app, data_source)` 依赖注入**（a-share-mcp）：工具模块不知道数据源是谁，数据源可整体替换。lquant 的 13 个 MCP 工具若还直接调具体数据源，这个解耦值得做。
2. **`formatting/markdown_formatter.py`**：**为 LLM 消费而专门格式化**输出（而非返回原始 dict）。lquant 的 MCP 工具若返回裸 JSON，token 效率与可读性都差。
3. **工具按域分组注册**（stock_market / financial_reports / indices / market_overview / macroeconomic / analysis / date_utils / helpers）——直接对应 lquant 若扩到 30+ 工具时的组织方式。
4. **description 写得像 API 契约**（这是 MCP 工具的核心 UX，比代码实现更重要）。

**(c) 是否只是一堆脚本？**
a-share-mcp 本体是**值得精读的 MCP 参考实现**（2.6k 行，架构清晰），但**已停更 + 无测试 + 仅 Baostock**，**不可作为依赖**，只可作为**设计参考**。

---

### 1.5 MyTT / MyTT2 / Ashare

| 项目 | 值 |
|---|---|
| 仓库 | https://github.com/mpquant/MyTT |
| Star | **未核实**（shields.io 对 `floatingprod/MyTT`、`WangYihang/MyTT` 等错误候选返回 repo not found；`mpquant/MyTT` 通过 Search API 确认存在，但 star 未单独取回） |
| 规模 | **736 行 Python**，5 个 `.py`（`MyTT.py` / `MyTT_plus.py` / `MyTT_python2.py` / `example1.py` / `hb_hq_api.py`） |
| 测试 | **无** |
| CI | **无** |
| 依赖 | 仅 numpy/pandas（**纯计算，无网络**） |

**MyTT2**：**未核实**——本次未在 GitHub Search 结果中确认 `MyTT2` 的独立仓库归属，**不臆造**。

**指标清单（本地源码 `grep "^def"` 实测，共 60+ 个）**：

```
基础: RD RET ABS LN POW SQRT SIN COS TAN MAX MIN IF REF DIFF STD SUM CONST
序列: HHV LLV HHVBARS LLVBARS MA EMA SMA WMA DMA AVEDEV SLOPE FORCAST LAST COUNT EVERY EXISTS
逻辑: FILTER BARSLAST BARSLASTCOUNT BARSSINCEN CROSS LONGCROSS VALUEWHEN BETWEEN TOPRANGE LOWRANGE
指标: MACD KDJ RSI WR BIAS BOLL PSY CCI ATR BBI DMI TAQ KTN TRIX VR CR EMV DPO BRAR DFMA MTM MASS ROC EXPMA OBV MFI ASI XSII
```

**定位**：**通达信/同花顺公式的 Python 逐行移植**，目标是与通达信**公式语义等价**（`SMA(X,N,M)` 加权、`REF`、`BARSLAST` 这类通达信特有语义都实现了）。
`MyTT_plus.py` 是增强版。

**对 lquant 的价值判定**：
- **(a) 数据面缺口：零**（纯计算库，不提供任何数据）。
- **(b) 值得借鉴**：
  1. **它是「通达信公式语义」的最简参考实现**。lquant 有因子 DSL（45 算子），若用户要从通达信/同花顺迁移公式，**`SMA(X,N,M)` 的加权递推、`REF` 的边界处理、`BARSLAST` / `FILTER` 的状态语义**是必须对齐的三处——MyTT 是核对口径的最短路径。
  2. **736 行覆盖 60+ 指标**——极高的代码密度，说明「指标库」本身**不值得大投入自研**，照抄语义即可。
- **(c) 是否只是脚本？** 是「一个算法文件」（`MyTT.py` 单文件核心），但**定位就是算法文件**，这是它的正确形态，不是缺点。
- **坑**：无测试；`MyTT_python2.py` 说明有历史包袱；**未核实**其与通达信实际输出的逐值一致性（这是使用前提，建议 lquant 若借鉴，务必用通达信导出数据做逐值对齐测试）。

**Ashare（mpquant/Ashare）**：**298 行 Python**，4 个 `.py`（`Ashare.py` / `Demo1.py` / `Demo2.py` / `MyTT.py`），含 `.github`（**未核实**是否有可用 workflow），无测试。
定位是**极简行情接口**（腾讯/新浪），是 astock-data-quant 的行情底座之一。
**对 lquant 价值：低** —— lquant 已有多源行情接入，Ashare 的 298 行不构成增量。**坑**：无复权因子处理、无重试、无速率限制，纯 demo 级。

---

## 2. A 股特有数据面缺口清单（本次调研的核心产出）

对每个数据面，给出：**能否用 akshare 低成本补齐 / 是否必须自研 / 建议**。
接口名均来自 §1.1 的源码 `grep` 实证（**「该仓库调用过」已核实；「akshare 当前可用性」未核实，落地前必须实测**）。

| # | 数据面 | lquant 现状 | akshare 接口（源码实证） | 补齐成本 | 建议 |
|---|---|---|---|---|---|
| 1 | **融资融券** | ❌ 缺 | `stock_margin_sse` / `stock_margin_detail_sse` / `stock_margin_detail_szse` | **低** | ✅ **补**。但注意**沪深分两个接口**，需自建统一 schema + 单位对齐（沪深披露字段名不同，**具体字段差异未核实**） |
| 2 | **限售解禁** | ❌ 缺 | `stock_restricted_release_queue_sina`（排队）/ `stock_restricted_release_summary_em`（汇总） | **低** | ✅ **补**。这是 A 股**最有 alpha 潜力且最易实时获取**的事件面之一，日频选股必备 |
| 3 | **大宗交易** | ❌ 缺 | `stock_dzjy_mrmx`（每日明细）/ `stock_dzjy_mrtj`（每日统计） | **低** | ✅ **补**。折溢价率是有效的资金面因子；明细可直接算「机构专用席位买入」 |
| 4 | **股东户数** | ❌ 缺 | `stock_zh_a_gdhs_detail_em` | **低** | ✅ **补**。户数下降=筹码集中，是经典 A 股因子，**但要处理披露频率不规律（季度/不定期）→ PIT 对齐** |
| 5 | **股东增减持** | ❌ 缺 | `stock_ggcg_em` | **低** | ✅ **补**。**需 PIT 处理**（公告日 vs 变动日） |
| 6 | **回购** | ❌ 缺 | `stock_repurchase_em` | **低** | ✅ **补**（低成本高信息量） |
| 7 | **分红送转** | ❌ 缺 | `stock_fhps_em` / `stock_fhps_detail_em` | **低** | ✅ **补**。且**对回测正确性必需**（除权除息处理） |
| 8 | **业绩预告/快报/披露计划** | 部分（PIT 财务） | `stock_yjyg_em` / `stock_yjkb_em` / `stock_yysj_em` | **低** | ✅ **补**。业绩预告是 A 股**最强的短期事件面**，且是天然 PIT 事件 |
| 9 | **筹码分布（CYQ）** | ❌ 缺 | **`stock_cyq_em`（东财筹码分布）** | **极低** | ✅ **先接 `stock_cyq_em` 做基准**，再决定是否自研（见 §3.1） |
| 10 | **商誉** | ❌ 缺 | **未在源码中发现**（⚠️ 未核实 akshare 是否有 `stock_sy_*` / 资产负债表科目可取） | 中 | ⚠️ **可暂缓**。可从财报资产负债表科目自算，但商誉减值**公告日**才重要 → 属事件面 |
| 11 | **股权质押** | ❌ 缺 | **未在源码中发现**（⚠️ 未核实） | **中高** | ⚠️ **优先级最低**。历史数据获取难、口径乱、且质押风险因子近年有效性下降 |
| 12 | **宏观（CPI/PPI/GDP/LPR/社融/M2/PMI）** | ❌ 缺 | `macro_china_cpi` / `_ppi` / `_gdp` / `_lpr` / `_money_supply` / `_pmi` / `_gyzjz`（工业增加值）/ `_trade_balance` / `_urban_unemployment` | **低** | ✅ **补**。但要解决 **PIT 修订问题**（见 §3.3） |
| 13 | **可转债** | ❌ 缺 | `bond_cb_jsl`（集思录） | 中 | 🔶 **可选**。SimpleQuant 的 `bonds/` 模块是更好的参照（含退市标的、双低/溢价率、信用利差） |
| 14 | **期权** | ❌ 缺 | `option_finance_board` / `option_sse_list_sina` / `option_value_analysis_em` / `index_option_50etf_qvix` | 中 | 🔶 **可选/低优先**。日频选股平台对期权的边际价值低；`index_option_50etf_qvix`（QVIX 恐慌指数）可作为**市场情绪因子**单点接入 |
| 15 | **龙虎榜** | ✅ 已有 | `stock_lhb_detail_em` / `stock_lhb_ggtj_sina` / `stock_lhb_stock_detail_em` | — | 已覆盖 |
| 16 | **北向资金** | ✅ 已有 | `stock_hsgt_hist_em` | — | 已覆盖（注意：北向已停止实时披露，**具体政策口径未核实**） |
| 17 | **估值分位** | 部分 | `stock_zh_valuation_baidu` / `stock_a_all_pe` / `stock_industry_pe_ratio_cninfo` / `stock_buffett_index_lg` / `stock_ebs_lg` | 低 | 🔶 补 `stock_buffett_index_lg`（巴菲特指标）做市场级估值 |
| 18 | **市场情绪** | 部分 | `stock_zt_pool_em` / `stock_zt_pool_dtgc_em` / `stock_market_activity_legu` / `stock_a_high_low_statistics` | 低 | 🔶 补 `stock_a_high_low_statistics`（创新高/新低家数） |

### 真正值得补的 Top 6（按性价比排序）

1. **限售解禁**（`stock_restricted_release_*`）— 事件面 alpha，数据干净
2. **业绩预告/快报**（`stock_yjyg_em` 等）— A 股最强事件面，天然 PIT
3. **融资融券**（`stock_margin_*`）— 杠杆资金面，日频
4. **大宗交易**（`stock_dzjy_*`）— 折溢价因子
5. **股东户数 + 增减持**（`stock_zh_a_gdhs_detail_em` / `stock_ggcg_em`）— 筹码集中度 + 内部人行为
6. **宏观最小集**（CPI/PPI/LPR/社融/M2/PMI）— 择时与风格切换的宏观背景

**不建议做**：股权质押（数据难、时效差）、商誉（事件化成本高、可从财报科目自算）、期权（日频选股边际价值低）。

---

## 3. 两个重点专项

### 3.1 筹码分布（CYQ）：**先接现成接口，自研只做「口径对齐」**

#### 已核实的开源实现

| 来源 | Star | 最后推送 | 算法 | 测试 |
|---|---|---|---|---|
| **akshare `stock_cyq_em`** | （akshare 本体，star 未核实） | — | 东财官方筹码分布（**具体算法未公开，未核实**） | akshare 本体有测试体系（**未核实**该接口的测试覆盖） |
| `a-stock-data-quant` `lib/chip_distribution.py` | 34 | 2026-08 | **换手率衰减 + 高斯核 + VWAP/典型价成本中枢**，263 行，**有算法 docstring** | ❌ 无 |
| [kengerlwl/ChipDistribution](https://github.com/kengerlwl/ChipDistribution) | **276** | **2021-03-12** | 筹码分布 python 计算源码（**⚠️ 已停更 5 年，算法细节未核实**） | 未核实 |
| [myhhub/stock](https://github.com/myhhub/stock) | **14758** | 2026-04-02 | 描述含「筹码分布 + 识别股票形态 + 回测 + 自动交易」，**⚠️ 未核实具体实现与测试** | 未核实 |
| [doaspx/flutter_stock](https://github.com/doaspx/flutter_stock) | 134 | 2024-05-17 | Flutter 股票软件的筹码分布（**Dart 实现，非 Python**） | 未核实 |
| [ArvinLovegood/go-stock](https://github.com/ArvinLovegood/go-stock) | 未核实 | 未核实 | **a-stock-data-quant 的筹码算法来源**（Go 实现）；`chip_distribution.go` | 未核实 |

#### `a-stock-data-quant` 的 CYQ 算法口径（源码逐行核实）

`lib/chip_distribution.py` docstring 原文：

> 1. 用换手率对历史筹码做衰减（保留比例 = `1 - turnover`）
> 2. 将当日成交量按以「成本中枢」为中心的高斯核落在 `[low, high]` 与各 bin 的交集上
> 3. 成本中枢优先为日 VWAP（成交额/成交量），否则典型价 `(H+L+C)/3`

关键参数（源码实证）：
- `bins` 默认 80，钳制在 `[10, 300]`
- 高斯核 `sigma = max((high - low) / 4, width / 2)`（**注释自述为「经验值」**）
- `turnover` 钳制在 `[0, 0.98]`
- 输出：`avg_cost`（按筹码量加权价）、`profit_ratio`（`center <= last_close` 的筹码占比）、`top_concentration`（**占比前 5 的 bin 之和**）

**口径问题（这是关键）**：
1. **`top_concentration` 的定义是「前 5 个 bin 的占比之和」，bin 宽度 = `(max_p - min_p) / 80`。** 也就是说，**这个「集中度」随 `bins` 和价格区间宽度而变化**——同一个股票用 `bins=80` 和 `bins=100` 会得到不同的「集中度」。它**不是无量纲的**，跨股票比较是无效的（不同股票价格区间宽度不同 → bin 宽度不同）。
   → **若 lquant 采用，必须改成无量纲定义**（如「覆盖 x% 筹码所需的价格区间宽度 / 现价」，即通达信常见的 90% 成本区间集中度）。
2. **衰减与新增不自洽**：代码先 `dist[i] *= (1 - turnover)`，再把**当日全部 `volume`** 加进去。总筹码量因此单调增长，形状由「近期成交量 vs 历史累积」的比值决定。这与「流通股本守恒」的物理直觉不符——**它输出的 `sum_vol` 没有"股数"含义，只有形状含义**。这本身可接受（最终按 `total_vol` 归一化），但**必须在文档里写明，否则会被误读为"筹码量"**。
3. **未处理复权**：模块只接收 `open/high/low/close/volume/amount/turnover`。**若传入前复权 K 线，历史价格被整体平移，成本中枢全部错位**；若传入不复权 K 线，则**除权除息造成的价格跳空会被当成真实的成本断层**。**两种都是错的**——正确做法是在**后复权价空间**计算筹码分布，或对除权日做成本价调整。**这是 CYQ 最大的坑。**
4. **未处理流通股本变化**：解禁、增发、回购会改变总筹码量，代码完全忽略。

#### 已知口径争议

通达信 / 同花顺 / 东方财富三家的筹码分布结果**互相不一致**（这是社区共识，**本次未找到权威逆向文档，标「未核实」**）。原因大概率就是上面第 1、2、4 条（衰减系数、新增量口径、总股本处理）各家不同，且**官方算法均不公开**。

#### 结论：**值得自研吗？——「半个」值得。**

**建议路线**：
- **第一步（1~2 天）**：直接接 **`ak.stock_cyq_em`**，以**东财口径为基准**。理由：免费、有现成接口、有公认的"事实标准"参照物、避免自研。
- **第二步（3~5 天）**：自研**一个**可控实现（复用 §3.1 的「衰减 + 高斯核」骨架，但修掉上面 4 个口径问题），并且——**这是关键**——把 `stock_cyq_em` 当作**回归测试的黄金标准**：断言自研实现的 `avg_cost` / `profit_ratio` 与东财在**容忍区间**内一致。**没有这个对照，自研 CYQ 就是不可证伪的**。
- **第三步（不推荐）**：不要试图"发明更好的筹码算法"。这个领域**没有 ground truth**，任何自研算法都无法证伪，极易沦为过拟合与自我安慰。

**主要坑（汇总）**：
1. **复权空间错误**（最致命，见上）。
2. **集中度无量纲化缺失** → 跨股票不可比。
3. **衰减系数 `turnover` 依赖输入数据的换手率字段**；不同数据源换手率口径（流通股 vs 总股本）不同 → **必须核实 unit/分母**。
4. **`sigma` 是经验值**，无标定。
5. **无 ground truth，无法验证** → 只能靠东财接口做相对回归。

---

### 3.2 形态识别：**不建议自研（除 ZigZag 原语外）**

#### 已核实的开源生态

**（A）通用技术形态（非缠论）**

| 来源 | Star | 最后推送 | 形态 | 算法 | 测试 |
|---|---|---|---|---|---|
| `a-stock-data-quant` `lib/patterns.py` | 34 | 2026-08 | W底 / V型反转 / 杯柄 / 三重底 / 回踩买入 / Zigzag | **ZigZag(step=3) 转折点 + 硬编码阈值** | ❌ 无 |
| [Theclues/TradeGenuis-box](https://github.com/Theclues/TradeGenuis-box) | 131 | 2026-09-06 | **箱体形态识别**（A股 + 加密货币） | 未核实 | 未核实 |
| [myhhub/stock](https://github.com/myhhub/stock) | 14758 | 2026-04-02 | 描述含「识别股票形态」+ 选股 + 回测 | 未核实 | 未核实 |
| TA-Lib `CDL*` 函数 | — | — | 蜡烛图形态（**数量与实现细节未核实**） | 规则式（**未核实**） | TA-Lib 本体有测试（**未核实**） |
| `patternpy` / `stock-pattern` | — | — | **未核实**（本次未取回，不臆造） | — | — |

**（B）缠论生态（中文里「形态识别」的实际最大分支）**

| 仓库 | Star | 最后推送 | 定位 |
|---|---|---|---|
| [Vespa314/chan.py](https://github.com/Vespa314/chan.py) | **2196** | **2026-09-24** | 开放式缠论 Python 框架；形态学/动力学买卖点、多级别联立、区间套、可视化 |
| [yijixiuxin/chanlun-pro](https://github.com/yijixiuxin/chanlun-pro) | **1053** | 2026-10-03 | 缠论量化分析工具 |
| [ibaihuo/chanvis](https://github.com/ibaihuo/chanvis) | 620 | **2024-07-02**（停更） | 基于 TradingView 本地 SDK 的缠论可视化 |
| [tomcat123a/-chanlun](https://github.com/tomcat123a/-chanlun) | 568 | **2024-04-02**（停更） | 笔/线段/中枢/买卖点划分（单文件 .py） |
| [kldcty/ChanlunX](https://github.com/kldcty/ChanlunX) | 420 | 2026-04-29 | 缠论可视化插件 |
| [chan2zen/rust-chan](https://github.com/chan2zen/rust-chan) | 342 | 2026-02-27 | **Rust 实现缠论**的通达信插件 |
| [YuYuKunKun/chanlun.py](https://github.com/YuYuKunKun/chanlun.py) | 156 | 2026-09-01 | 笔/线段/中枢识别 + TradingView + Backtrader 回测 |
| [TensorCode666/stock-chanlun](https://github.com/TensorCode666/stock-chanlun) | 77 | 2026-08-24 | ChanStock：Vue3 + FastAPI + 缠论结构识别 |
| [noahnan-max/chanlun-trading-system](https://github.com/noahnan-max/chanlun-trading-system) | 73 | 2026-08-03 | 缠论 AI Agent Skill |

#### `lib/patterns.py` 的实现质量（源码逐行核实，391 行）

- 核心原语：`peak_valley_pivots_np(X, step=3)` —— **ZigZag 滑动窗口转折点**，移植自 `KlangAlpha/Klang`。
- 六个检测函数，全部建立在 ZigZag 转折点序列上：
  - `detect_w_bottom(step=3, min_depth_pct=0.05)`：要求 `pivots[a]==1` 且 `|ab-ad|/cd < 0.05`（**双底近似相等容差 5%**），且 `ab/close[b] > 0.05`（**最小深度 5%**）
  - `detect_v_reversal(step=3, min_drop_pct=0.03, max_recovery_ratio=0.8)`：跌幅 >3%，恢复 >60% 跌幅，`up_bars/down_bars <= 0.8`
  - `detect_cup_handle(step=3, max_cup_depth_pct=0.30)`：`|ab-cb|/cb < 0.15`，`cb/3 > cd`（**柄深不超过杯深 1/3**）—— 注意 `max_cup_depth_pct=0.30` 这个参数**在函数体里根本没被使用**（**源码实证的 dead parameter**）
  - `detect_triple_bottom(step=3, min_depth_pct=0.05)`：三个底两两近似（容差 5%）
  - `detect_dip_buy(step=3, min_rise_pct=0.10, max_retrace_ratio=0.50)`：仅在**最后 5 个转折点**内搜索

**质量判定**：
- ✅ **原语选得对**：ZigZag 转折点是经典技术形态识别的标准做法（Perceptually Important Points 的简化版）。
- ❌ **阈值全是魔法数且无标定**：`0.05 / 0.15 / 0.6 / 0.8 / 0.3 / 3` 没有任何来源、没有回测标定、没有敏感性分析。
- ❌ **只用收盘价 `close`**（函数签名只收 `close`）——**忽略了最高/最低价与成交量**，而真实形态定义必然涉及 `high/low`。
- ❌ **没有"突破确认"**：杯柄形态的定义核心是**突破杯沿颈线**，但 `detect_cup_handle` 只检查了几何比例，`handle_end` 之后有没有突破完全不管。**这会产生大量假信号。**
- ❌ **没有成交量确认**：所有形态（尤其杯柄、W底突破）都应有量能配合。
- ❌ **存在 dead parameter**（`max_cup_depth_pct`），说明代码未经过 review。
- ❌ **零测试**：391 行里有 6 个检测器，**没有一个测试用例**。

#### 结论：**值得自研吗？——不值得（除 ZigZag 之外）。**

**理由（按重要性排序）**：

1. **没有 ground truth**。什么算"W 底"没有权威定义。`min_depth_pct=0.05` 还是 `0.08`？两个底"近似相等"是 3% 还是 5%？**任何选择都无法证伪**，只能靠回测收益间接判断——而回测收益在 A 股上极易过拟合（几百个股票 × 几年数据，总能找到一组参数跑赢）。
2. **形态识别的参数空间与"因子挖掘"等价**：`step × min_depth × tolerance × bins`，加上 6 种形态，**组合数轻松上千**。这就是一次隐式的多重检验，**必须有严格的样本外/多重检验校正**，而开源实现全部没有。
3. **开源实现全部是无测试的规则堆**（`lib/patterns.py` 391 行 6 个检测器 0 测试是最典型的样本）。
4. **缠论生态虽然 star 高（chan.py 2196★）且活跃，但缠论本身是"人为构造的完备体系"**——它的问题是**没有可证伪的预测内容**，"笔/线段/中枢"的划分规则由理论内部自定。chan.py 作为**框架工程**（多级别联立、区间套、可视化、数据接入）是优秀的，但**其输出的交易信号同样不可证伪**。

**如果 lquant 一定要做形态，唯一可辩护的做法**：
1. **只做原语，不做形态**：把 **ZigZag / PIP 转折点** 作为因子 DSL 的一个算子暴露给用户（`ZIGZAG(close, step)` → 返回结构位、摆动幅度、摆动周期）。让用户自己在 DSL 里组合形态，**平台不对"这是 W 底"下断言**。
2. 若必须内置形态检测：**必须** (a) 用 `high/low` 而非仅 `close`；(b) 加入**突破确认**（颈线突破 + 量能）；(c) 把**所有阈值做成显式参数并文档化**；(d) **在回测里做参数敏感性分析**（同一形态在不同阈值下的收益分布必须稳健，否则不上线）；(e) 输出**"形成中 / 已确认"状态区分**（astock-data-quant 的 README 提到「"已确认"比"形成中"更可靠」——**这个二分是对的，是它唯一值得抄的形态设计点**）。
3. **绝不做的事**：不要宣称"识别出 W 底 → 看涨"。形态只能作为**筛选条件 + 事件锚点**（用于事件研究），不能作为**信号**。

**Top 3 可借鉴（若做）**：
1. `a-stock-data-quant/lib/patterns.py` 的 **ZigZag 原语 + "已确认/形成中"二分**（算法骨架，不抄阈值）。
2. [Vespa314/chan.py](https://github.com/Vespa314/chan.py)（2196★，活跃）的**多级别 K 线联立 + 框架化数据接入**工程结构。
3. [Theclues/TradeGenuis-box](https://github.com/Theclues/TradeGenuis-box)（131★）——**箱体**比 W 底/杯柄可定义性强得多（"近 N 日高点/低点"是客观的），若只做一个形态，**箱体是性价比最高的**。

---

### 3.3 宏观数据（补充专项）

**已核实的 akshare 宏观接口**（源码实证，见 §1.1）：

```
macro_china_cpi  macro_china_ppi  macro_china_gdp  macro_china_lpr
macro_china_money_supply（M0/M1/M2）  macro_china_pmi  macro_china_gyzjz（工业增加值）
macro_china_trade_balance  macro_china_urban_unemployment
```

**对 lquant 的路线建议**：
- **路线选择：优先 akshare 直取**（接口名已实证、零 token、有社区维护），**不自建 NBS 爬虫**（`data.stats.gov.cn` 反爬维护成本高，**其 API 可用性本次未核实**）。
- **最小指标集**（覆盖"宏观背景 + 择时"）：**CPI、PPI、GDP、LPR、社融/M2、PMI**。
- **必做的 PIT 处理（这是宏观最大的坑）**：
  1. **发布日 ≠ 数据期**。`macro_china_cpi` 返回的是"2026-09 的 CPI"，但它**在 2026-10-15 才公布**。若按数据期对齐到 9 月，就是**严重前视偏差**。**必须建立"指标 → 实际发布时间"映射表**（akshare 部分接口可能已含发布日期字段，**未核实**）。
  2. **修订（revision）**：GDP 等指标有初次公布 / 修订值。**开源项目基本都不处理 PIT 修订**（akshare 只返回最新值）。**未核实** akshare 是否有初值接口（如 `macro_china_gdp` 的 vintages）。
  3. **发布日历**：LPR（每月 20 日）、MLF、统计局数据（每月中旬）的**发布日历是事件研究的前提**。**本次未找到可靠的开源中国宏观经济日历**（`a-stock-data-quant` 的 `sources_wallstreetcn.py` 提到"财经日历"，**未核实其是否含中国宏观发布日**）。**建议 lquant 自建一张静态日历表**（规则明确：统计局月度数据约每月 15 日左右，LPR 每月 20 日）——这是低成本、高确定性的一步。

---

## 4. 跨项目横向对比总表

| 项目 | Star | 最后提交 | Python LOC | 测试 | CI | 数据面宽度 | A股特有面 | 对 lquant 定位 |
|---|---|---|---|---|---|---|---|---|
| [jangviktor-web/a-stock-data-quant](https://github.com/jangviktor-web/a-stock-data-quant) | 34 | 2026-08 | 21,039 | ❌ | ❌ | **最宽** | 两融/解禁/大宗/股东/形态/筹码/宏观/期权/可转债 | ⭐ **接口路标 + 2 个算法文件** |
| [AllenPan-Git/SimpleQuant](https://github.com/AllenPan-Git/SimpleQuant) | 26 | 2026-10-07 | 30,893 | ✅ 16+ | ✅ | 窄但深 | 可转债/复权/历史成分股/规则 | ⭐⭐ **可转债 + 口径诚实度范式** |
| [jojo232386/a-share-quant](https://github.com/jojo232386/a-share-quant) | 0 | 2026-08 | 66,002 | ✅ 40+ | ✅ | 几乎无（合成夹具） | 无 | ⭐⭐⭐ **工程机制（最值得精读）** |
| [24mlight/a-share-mcp-is-just-i-need](https://github.com/24mlight/a-share-mcp-is-just-i-need) | 646 | **2025-12（停更）** | 2,644 | ❌ | ❌ | 仅 Baostock | 无 | ⭐⭐ **MCP 架构参考** |
| [mpquant/MyTT](https://github.com/mpquant/MyTT) | 未核实 | 未核实 | 736 | ❌ | ❌ | 纯计算 | — | ⭐ **通达信公式语义核对** |
| [mpquant/Ashare](https://github.com/mpquant/Ashare) | 未核实 | 未核实 | 298 | ❌ | 有 `.github`（未核实） | 行情 | — | 低 |
| [rootSunc/CNEquity](https://github.com/rootSunc/CNEquity) | **337** | 2026-10-07 | **未核实** | 未核实 | 未核实 | **宣称 55 数据集 + PIT + 溯源** | 宣称覆盖资金面/公告事件/宏观/风险 | ⭐⭐⭐ **最高优先，待源码核实** |
| [wolfjkd/tradex-hub](https://github.com/wolfjkd/tradex-hub) | 56 | 2026-10-06 | 未核实 | 未核实 | 未核实 | 宣称 129 MCP 工具 | 未核实 | ⭐ **SmartRouter 降级路由参考** |
| [electkismet/eltdx](https://github.com/electkismet/eltdx) | 564 | 2026-10-05 | 未核实 | 未核实 | 未核实 | 通达信协议（逐笔/集合竞价） | 逐笔、集合竞价 | ⭐ **若需 tick 级** |
| [Vespa314/chan.py](https://github.com/Vespa314/chan.py) | 2196 | 2026-09-24 | 未核实 | 未核实 | 未核实 | 缠论 | 缠论结构 | 🔶 仅框架参考 |
| [kengerlwl/ChipDistribution](https://github.com/kengerlwl/ChipDistribution) | 276 | **2021-03（停更）** | 未核实 | 未核实 | 未核实 | 筹码 | CYQ | 🔶 已过时 |
| [myhhub/stock](https://github.com/myhhub/stock) | 14758 | 2026-04-02 | 未核实 | 未核实 | 未核实 | 筹码+形态+选股+回测+自动交易 | 未核实 | ⚠️ star 高但**未核实**，需单独调研 |

**一个重要的元观察**：**star 数与工程质量在 A 股工具生态里几乎不相关**——`a-share-quant`（0★，66k 行，40+ 测试，CI 完备）vs `a-stock-data-quant`（34★，0 测试 0 CI）vs `myhhub/stock`（14758★，未核实）。**lquant 的竞品评估不能以 star 为主要指标。**

---

## 5. Top 5 借鉴点（对 lquant 可落地）

1. **【来自 a-share-quant】Release Manifest + Replay + 组合等价性测试**
   `release_manifest.py` / `release_replay.py` 把每次发布固化成**带 commit hash 的可复现 artifact**；`tests/unit/test_portfolio_equivalence.py` 用两条独立代码路径算同一个组合并断言等价。
   → **lquant 落地**：给因子评价/回测/组合各建一个 manifest（数据快照 hash + 参数 + 代码版本），并加**收缩协方差的等价性测试**（如 Ledoit-Wolf 手写实现 vs 库实现）。

2. **【来自 a-share-quant + SimpleQuant】「拒绝近似运行」与「口径诚实度」文档纪律**
   a-share-quant：「对未支持的证券、历史时期或公司行为，项目应**拒绝近似运行**」。
   SimpleQuant：主动写明「可转债新浪为不复权价、成交量为**张**、缺初期日线」「东财评级是**最新评级**，用于历史回测有**前视偏差**，故不作为选股条件」。
   → **lquant 落地**：为每个数据面写一张**口径卡**（字段名 / 单位 / 复权状态 / PIT 语义 / 已知缺陷），并在数据不可信时**报错而非降级出近似值**。

3. **【来自 a-share-mcp + tradex-hub】MCP 工具的「数据源抽象 + 按域分组注册 + LLM 专用格式化」**
   `data_source_interface.py` 抽象 + `register_xxx_tools(app, active_data_source)` 依赖注入 + `formatting/markdown_formatter.py` 为 LLM 专门格式化。
   → **lquant 落地**：把 13 个 MCP 工具重构为「域模块 + 注入数据源」；工具返回**为 LLM 优化过的 Markdown**而非裸 JSON；工具 description 当 API 契约写。**同时反面对照 `tradex-hub` 的 129 工具**——工具数量不是越多越好，lquant 的 13 个是合理区间。

4. **【来自 a-stock-data-quant + SimpleQuant】数据面的「最小增量集合」**
   一次性补齐：**限售解禁 / 业绩预告快报 / 融资融券 / 大宗交易 / 股东户数与增减持 / 分红送转 / 回购 / 宏观最小集**（接口名见 §2）。这批接口**成本低、事件属性强、PIT 友好**，是 lquant 从"价量+财务"扩展到"事件驱动"的最短路径。
   → 特别地，`stock_yjyg_em`（业绩预告）与 `stock_restricted_release_*`（解禁）是**A 股 alpha 密度最高的两个事件面**。

5. **【来自 SimpleQuant + MyTT】复权/公式语义的显式分离**
   SimpleQuant：「不复权价 + 复权因子」分开存，**收益用后复权价、整手用真实价**。
   MyTT：把 `SMA(X,N,M)` 的加权递推、`REF` 边界、`BARSLAST`/`FILTER` 状态语义做成了 736 行的精确参照。
   → **lquant 落地**：(a) 存储层保留不复权价 + 复权因子（lquant 已有 PIT 财务，同哲学）；(b) 因子 DSL 的算子与 MyTT/通达信做**逐值对齐测试**，这比"自己发明算子语义"安全得多。

---

## 6. 「不必借鉴」清单

| 项目 / 做法 | 不借鉴的理由（证据） |
|---|---|
| **`a-stock-data-quant` 的整体工程** | 21k 行、**0 测试 0 CI**、2400 行单文件 CLI、3 个月堆 8 个版本、v3.7.0 一次性修 6 个数据源 bug → 缺乏契约测试。**只读 `chip_distribution.py` + `patterns.py` 两个文件，其余不读。** |
| **硬编码/混淆第三方 API Key** | `a-stock-data-quant` 把 base64 混淆的 `EM_API_KEY` 打包进仓库（README 自述"开箱即用"）。**供应链与合规风险，绝对不可模仿。** |
| **把闭源商业 MCP 作为降级链一环** | 广发 MCP / 同花顺 hithink / 东财妙想均为需 Key 的闭源服务。**降级链的可用性不能押在他人的配额与政策上。** |
| **自研筹码分布（CYQ）算法** | **没有 ground truth**，通达信/同花顺/东财三家口径不一致（社区共识，权威逆向文档**未核实**）。**应直接接 `ak.stock_cyq_em`**；若要自研，只能用东财做回归基准，**不可宣称"更优"。** |
| **自研技术形态识别（W底/杯柄/头肩）** | (1) **无权威定义，无法证伪**；(2) `step × 阈值` 组合上千 → 多重检验过拟合；(3) 开源实现**全是无测试的规则堆**（`patterns.py` 391 行 6 检测器 0 测试；**杯柄检测器的 `max_cup_depth_pct` 是 dead parameter** 源码实证）；(4) 仅用 `close`、无突破确认、无量能确认 → 假信号极多。**最多只做 ZigZag 原语，不做形态断言。** |
| **缠论（chanlun）作为信号源** | 缠论 star 虽高（chan.py 2196★）且活跃，但**笔/线段/中枢的划分规则由理论内部自定，没有可证伪的预测内容**。可作为**框架工程**参考（多级别联立、数据接入），**不可作为 lquant 的信号来源**。 |
| **`jojo232386/a-share-quant` 的代码复杂度** | 66k 行 / **0 star**，单作者，**验证数据全是合成夹具**（README 明示"不是真实行情"）。**只借鉴 `docs/` 与 `release_*.py` 的机制，整体复杂度不照搬。** |
| **`24mlight/a-share-mcp-is-just-i-need` 作为依赖** | **2025-12 停更（约 10 个月）**、无测试、无 CI、**仅 Baostock**、README 自述"在 Windows 环境下开发"。**只作 MCP 架构参考，不作依赖。** |
| **股权质押数据** | 历史数据获取难、口径混乱、时效差；**且 akshare 是否提供对应接口本次未核实**。**优先级最低，暂缓。** |
| **把宏观指标按"数据期"直接对齐到日频** | 前视偏差的经典陷阱（9 月 CPI 在 10 月中才公布）。**必须用发布日对齐 + 自建发布日历。** |
| **以 star 数作为竞品质量指标** | 实证：`a-share-quant` 0★/66k行/40+测试 vs `myhhub/stock` 14758★（质量**未核实**）vs `a-stock-data-quant` 34★/0测试。**无关。** |
| **`mpquant/Ashare` 作为数据层** | 298 行，无复权因子、无重试、无速率限制，demo 级。lquant 已有多源接入，无增量。 |

---

## 7. 后续待办（本次时间预算内未完成，明确移交）

按优先级：

1. **【最高】`rootSunc/CNEquity` 源码级调研**（337★，2026-10-07）。其自述「55 个日更数据集 + 行级溯源 + PIT 语义 + 复权与历史成分内置 + 自托管零 Token」如果属实，是与 lquant **定位最接近**的项目。可执行：
   ```bash
   curl -sL "https://codeload.github.com/rootSunc/CNEquity/tar.gz/refs/heads/main" -o cne.tar.gz
   ```
2. **`myhhub/stock`(14758★) 核实**——它同时宣称筹码分布 + 形态识别 + 回测 + 自动交易，star 极高但**本次完全未核实**，是一个明显的调研盲区（高分可能是"书籍列表"类仓库的 star 误读，需验证）。
3. **逐个实测 §2 的 akshare 接口**（本次只验证了"仓库调用过"，未验证"当前可用"）。建议写一个 `scripts/verify_akshare_endpoints.py` 批量探测。
4. **TA-Lib `CDL*` 形态函数清点**（数量、实现、已知问题）——本次未核实。
5. **`patternpy` / `stock-pattern` / `stock-patterns` 等英文形态库**——本次未核实。
6. **`ArvinLovegood/go-stock` 的 `chip_distribution.go`** 原文比对（a-stock-data-quant 的 CYQ 来源）——未核实其 star 与算法差异。
7. **通达信/同花顺 CYQ 官方算法**是否有公开逆向文档——本次未找到，标「未核实」。
8. **中国宏观经济发布日历**的开源数据源——本次未找到。
9. **北向资金当前披露政策口径**（实时披露是否已停止）——未核实。
10. **`electkismet/eltdx`(564★) 的逐笔/集合竞价协议实现**——若 lquant 需日内或集合竞价数据。

---

## 附录 A：本次调研的原始证据文件

完整仓库源码快照（已解包，供后续 grep）：

```
/tmp/research/repos/astockdq     ← jangviktor-web/a-stock-data-quant (master)
/tmp/research/repos/simplequant  ← AllenPan-Git/SimpleQuant (main)
/tmp/research/repos/asharequant  ← jojo232386/a-share-quant (main)
/tmp/research/repos/asharemcp    ← 24mlight/a-share-mcp-is-just-i-need (main)
/tmp/research/repos/mytt         ← mpquant/MyTT (main)
/tmp/research/repos/ashare       ← mpquant/Ashare (main)
```

> 注：`/tmp` 为临时目录，可能被清理。如需长期保留，建议复制到 `docs/research/competitors/_raw/`。

## 附录 B：star / 活跃度实测原始值

```
jangviktor-web/a-stock-data-quant  stars=34      last-commit=august
AllenPan-Git/SimpleQuant           stars=26      last-commit=yesterday (2026-10-07)
jojo232386/a-share-quant           stars=0       last-commit=august
24mlight/a-share-mcp-is-just-i-need stars=646    last-commit=december 2025
kengerlwl/ChipDistribution          (search) 276★  pushed 2021-03-12
myhhub/stock                        (search) 14758★ pushed 2026-04-02
Vespa314/chan.py                    (search) 2196★  pushed 2026-09-24
yijixiuxin/chanlun-pro              (search) 1053★  pushed 2026-10-03
rootSunc/CNEquity                   (search) 337★   pushed 2026-10-07
electkismet/eltdx                   (search) 564★   pushed 2026-10-05
wolfjkd/tradex-hub                  (search) 56★    pushed 2026-10-06
elsejj/mcp-cn-a-stock               (search) 460★   pushed 2026-09-30
zhangxiangliang/stock-api           (search) 1960★  pushed 2026-10-08
hello245m/free-stockdb              (search) 2813★  pushed 2026-10-04
simonlin1212/Vibe-Research          (search) 2635★  pushed 2026-10-07
TNT-Likely/PanWatch                 (search) 2042★  pushed 2026-10-05
Theclues/TradeGenuis-box            (search) 131★   pushed 2026-09-06
```

`mpquant/MyTT` 与 `mpquant/Ashare` 的 star 数**未单独取回**（通过 Search API 确认仓库存在）。
