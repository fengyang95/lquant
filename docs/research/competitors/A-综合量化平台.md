# A-综合量化平台 竞品调研报告

> 调研对象：开源的一体化 / 综合型量化交易与回测平台（重点 A 股适配）
> 产出日期：本轮会话（GitHub API 返回时间戳以「本次查询」表述）
> 作者：竞品调研员（受 lquant 定位约束）
> 服务对象：lquant —— 面向 A 股（个股 + ETF）的「数据接入 → 因子分析 → 量化回测 → 市场看板 → 模拟盘」一体化平台

---

## 0. 方法、证据等级与阅读约定

**方法**：`web_search` + `web_fetch` 读取 GitHub 仓库页 / README / 源码 raw 文件；活跃度与元数据通过 `api.github.com/repos/{owner}/{repo}` 与 `api.github.com/search/repositories` 直接查询；源码事实通过 `raw.githubusercontent.com` 拉取原文后本地核对（给出可点击的 GitHub blob 链接）。

**证据等级标记**（全文遵守）：

| 标记 | 含义 |
|---|---|
| ✅ 源码事实 | 我实际拉取并阅读了该文件的内容，引用的是原文片段 |
| 📄 文档事实 | 来自 README / 官方文档原文，非我推断 |
| 🔍 路径事实 | 我用 HTTP 状态码确认该文件/路径存在（200），但未逐行阅读其内容 |
| ⚠️ 未核实 | 我没有拿到可验证证据，或与既有认知冲突，**不得当作结论使用** |
| 💭 我的推断 | 基于已核实事实的架构判断，不是原文内容 |

**两个重要前置提醒**：

1. **时间戳口径**：本文所有「最近提交」是本次查询时 GitHub API 返回的 `pushed_at` 字段原值。查询环境的系统时钟落在 2026 年区间，因此出现 2026 年的日期；请以「相对新旧」而非绝对年份理解。
2. **许可证是硬约束**：本组项目中至少 3 个带商业化限制（RQAlpha 非商业许可、vectorbt 与 pybroker 的 Commons Clause、backtesting.py 的 AGPL-3.0）。这一点比技术特性更能决定「能不能抄、能不能引依赖」。

---

## 1. 项目清单总表（活跃度与元数据）

数据来源：GitHub API `/repos/{owner}/{repo}`，单次查询快照。star 为量级参考，会随时间变化。

| 项目 | 仓库 | 语言 | Star（量级） | 最近提交（pushed_at） | 许可 | 一句话定位 |
|---|---|---|---|---|---|---|
| QUANTAXIS | [yutiansut/QUANTAXIS](https://github.com/yutiansut/QUANTAXIS) | Python | 11,260 | 2026-09-18 | MIT | 本地化的股票/期货/期权 数据+回测+模拟+交易+可视化 全栈方案，含分布式与任务调度 |
| Hikyuu | [fasiondog/hikyuu](https://github.com/fasiondog/hikyuu) | C++ (Python 绑定) | 3,555 | 2026-10-08 | Apache-2.0 | C++/Python 超高速策略研究与回测框架，**策略部件化**为核心卖点 |
| qteasy | [shepherdpp/qteasy](https://github.com/shepherdpp/qteasy) | Python | ~158（⚠️ 待复核） | 2026-10-05 | BSD-3-Clause | 本地化全流程量化工具包：数据→策略→回测→优化→模拟实盘，A 股 T+1/MOQ 建模细致 |
| vn.py | [vnpy/vnpy](https://github.com/vnpy/vnpy) | Python | 45,761 | 2026-10-06 | MIT | 事件驱动 + 网关抽象的实盘交易平台开发框架，生态最大 |
| RQAlpha | [ricequant/rqalpha](https://github.com/ricequant/rqalpha) | Python | 6,815 | 2026-10-08 | 非商业（NOASSERTION） | 米筐开源回测框架，**mod 插件机制**与 A 股规则完整度业界标杆 |
| Abupy / abu | [bbfamily/abu](https://github.com/bbfamily/abu) | Python | 18,872 | 2026-01-24 | GPL-3.0 | 阿布量化：面向中文用户的量化教学 + 择时/仓位/度量体系 |
| pybroker | [edtechre/pybroker](https://github.com/edtechre/pybroker) | Python | 3,585 | 2026-10-05 | Apache-2.0 + Commons Clause | 面向 ML 策略的回测框架，**内建 walk-forward** |
| zipline-reloaded | [stefan-jansen/zipline-reloaded](https://github.com/stefan-jansen/zipline-reloaded) | Python | 1,959 | 2026-01-06 | Apache-2.0 | Quantopian 原 zipline 的社区维护续命版 |
| backtrader | [mementum/backtrader](https://github.com/mementum/backtrader) | Python | 23,430 | **2024-08-19** | GPL-3.0 | 老牌事件驱动回测库，事实停更 |
| backtesting.py | [kernc/backtesting.py](https://github.com/kernc/backtesting.py) | Python | 9,020 | 2026-08-05 | AGPL-3.0 | 轻量单文件风格回测库，内置 `optimize()` |
| bt | [pmorissette/bt](https://github.com/pmorissette/bt) | Python | 2,996 | 2026-10-06 | MIT | 组合式（algos）回测与策略树 |
| vectorbt | [polakowo/vectorbt](https://github.com/polakowo/vectorbt) | Python | 9,297 | 2026-09-26 | Apache-2.0 + Commons Clause | 向量化回测，数千组参数一次性跑完（对照组） |
| WonderTrader | [wondertrader/wondertrader](https://github.com/wondertrader/wondertrader) | C++ | 6,380 | 2026-09-01 | MIT | C++ 核心 + 多引擎（CTA/SEL/HFT/UFT）的一站式研交框架 |

**活跃度小结**：⭐ 活跃＝Hikyuu / RQAlpha / qteasy / vn.py / pybroker / bt / vectorbt / QUANTAXIS / WonderTrader；⚠️ 慢＝zipline-reloaded（2026-01）、Abupy（2026-01）；🔴 **停更＝backtrader（2024-08 起无推送）**。

> qteasy 的 star 数：GitHub 搜索 API 返回 `shepherdpp/qteasy` 为 158 star。**与主流认知（数千 star 量级）明显不符，标注为 ⚠️ 待复核**，可能是搜索 API 的计数口径或镜像/迁移影响。其余项目的 star 数来自 `/repos/` 单仓库接口，可信度较高。

> **qteasy 仓库已迁移**：任务清单里给的 `zengbin93/qteasy` 现在返回 404（✅ 已验证）。当前活跃仓库是 **`shepherdpp/qteasy`**（✅ API + README 均确认，README 署名作者 Jackie PENG）。检索时请用新地址。

---

## 2. 逐项目详解

### 2.1 Hikyuu —— 最值得细读的「策略部件化」范式

**一句话定位与技术栈** 📄：基于 C++/Python 的开源超高速量化交易研究框架，聚焦策略分析与回测（[readme.zh.md](https://github.com/fasiondog/hikyuu/blob/master/readme.zh.md)）。三大部分：高性能 C++ 核心库、Python 接口层 `hikyuu`、交互式工具 `hikyuu.interactive`。依赖较轻（numpy/pandas/matplotlib/PySide6/tables），**自研 xmake 构建，不是 cmake**。Python ≥ 3.10。

**架构分层** 📄 + 🔍：

| 层 | 实现 | 证据 |
|---|---|---|
| 数据层 | `StockManager` / `KData` / `Query`；存储支持 HDF5（默认）/ MySQL / ClickHouse / SQLite | readme.zh.md 部件表；[hikyuu/data/](https://github.com/fasiondog/hikyuu/tree/master/hikyuu/data)（`common_h5.py`/`common_mysql.py`/`common_clickhouse.py`/`common_sqlite3.py`）✅ |
| 策略层 | 交易系统 SYS 七部件 + 组合层四部件（见下） | readme.zh.md 部件表 📄 |
| 回测引擎 | `SYS_Simple` / `sys.run()`；C++ 侧 `trade_sys/` 多线程 | [readme 示例](https://github.com/fasiondog/hikyuu/blob/master/readme.zh.md) 📄 |
| 账务 | `TradeManager` / `TradeManagerBase` / `Performance`（绩效） | [trade_manage/](https://github.com/fasiondog/hikyuu/tree/master/hikyuu_cpp/hikyuu/trade_manage) ✅ |
| 风控 | 部件化为 `Stoploss / Stopprofit / ST`，属策略层而非独立风控层 | readme.zh.md 📄 |
| 服务化 | 无内建；主张「Python + Jupyter + 云服务器」自行搭建 | readme.zh.md 📄 |

**A 股规则正确性**（本报告最重要的发现之一）：

| 规则 | 是否内置 | 证据 |
|---|---|---|
| 涨跌停 | ✅ 内置为**指标**：`ISLIMITUP` / `ISLIMITDOWN` | ✅ `indicator/imp/IIsLimitUp.cpp` |
| ST 股 5% 涨跌幅 | ❌ **明确未实现** | ✅ 源码注释：`// 10% for the A-shares, but 5% for the ST stocks (not handled: no ST date)` |
| 印花税历史区间 | ✅ **版本化类**：`TC_FixedA` / `TC_FixedA2015` / `TC_FixedA2017` | ✅ `trade_manage/crt/TC_FixedA2015.h`、`TC_FixedA2017.h` |
| ETF 费率差异 | ✅ 独立类 `TC_FixedETF` | ✅ `trade_manage/crt/TC_FixedETF.h` |
| 最低佣金累计 | ✅ `lowest_commission = 5.0`，逐笔取 max | ✅ `FixedA2017TradeCost.cpp:44-48` |
| 过户费市场差异 | ✅ 仅沪市 `market() == "SH"` 收取 | ✅ `FixedA2017TradeCost.cpp:79-81` |
| 复权 / 除权除息 | ✅ `ADJ_FACTOR` 指标 + `StockWeight` 权息数据 | ✅ `indicator/imp/IAdjFactor.cpp`、`serialization/StockWeight_serialization.h` |
| 整手（100 股） | ⚠️ 未核实（未找到强制校验；`MM_FixedCount(1000)` 只是示例手数） | — |
| T+1 | ⚠️ 未核实（未在 trade_manage 中发现交易日锁定逻辑） | — |
| 停牌 | ⚠️ 未核实 | — |
| 退市/幸存者偏差 | ⚠️ 未核实 | — |

**A 股费用模型原文** ✅（[FixedA2017TradeCost.cpp](https://github.com/fasiondog/hikyuu/blob/master/hikyuu_cpp/hikyuu/trade_manage/imp/FixedA2017TradeCost.cpp)）：

```cpp
FixedA2017TradeCost::FixedA2017TradeCost() : TradeCostBase("TC_FixedA2017") {
    setParam<price_t>("commission", 0.0018);
    setParam<price_t>("lowest_commission", 5.0);
    setParam<price_t>("stamptax", 0.001);
    setParam<price_t>("transferfee", 0.00002);
}
```
买入＝佣金（≥5 元）+ 过户费；卖出＝佣金 + 印花税（**仅 `STOCKTYPE_A` / `STOCKTYPE_GEM`**）+ 过户费（仅沪市）。

**涨跌停判定原文** ✅（[IIsLimitUp.cpp](https://github.com/fasiondog/hikyuu/blob/master/hikyuu_cpp/hikyuu/indicator/imp/IIsLimitUp.cpp)）：主板 `1.1`、北交所 `STOCKTYPE_A_BJ` `1.3`、创业板/科创板 `STOCKTYPE_GEM`/`STOCKTYPE_START` `1.2`；比较 `ks[i].closePrice >= roundEx(ks[i-1].closePrice * limit_up, precision)`；`m_discard = 1` 丢弃首根 K 线。

**值得借鉴的机制**：

1. **策略部件化 + 统一命名体系**（详见 Top 8 #4）。解决了「策略逻辑耦合、无法整篇替换」的问题。
2. **费用模型按监管历史区间版本化**。`TC_FixedA2015` / `TC_FixedA2017` 的差异是过户费口径（`.00002` 按成交额 vs `FixedA` 的 `0.001` 按股数 + 最低 1 元）——✅ 三个文件我都读了参数块。这解决了「长周期回测跨越费率调整点，用单一费率必然失真」的问题。
3. **序列化层独立**（`hikyuu_cpp/hikyuu/serialization/*.h`，基于 Boost serialization）。解决了策略/上下文状态的持久化与跨进程传输，是「部件可独立替换」的技术底座。
4. **C++ 核心 + Python 绑定 + 可选剥离**：性能与生态兼得，核心库可单独发布构建自有工具。

**已知坑 / 失败教训**：
- **商业边界模糊** ⚠️：README 有大量捐赠/订阅权益（赠历史日/分/时/笔数据），社区版功能与捐赠权益耦合。💭 推断：数据获取便利性是付费点，开源部分偏框架。
- **构建门槛**：自研 xmake（非 cmake）📄，C++ 源码构建对非 C++ 背景团队不友好。
- **数据源单一** 📄：`HikyuuTDX` 图形界面「目前仅支持下载国内 A 股历史数据，美股等其他市场暂不支持」。
- **依赖 GUI 组件**：主要依赖含 PySide6 📄，纯服务器/CI 环境需注意。

---

### 2.2 RQAlpha —— A 股规则正确性与「扩展点」设计的最佳教材（但许可受限）

**一句话定位与技术栈** 📄：可扩展、可替换的 Python 回测与交易框架，支持多证券品种（[README.rst](https://github.com/ricequant/rqalpha/blob/master/README.rst)）。纯 Python，依赖轻。

**架构分层**（mod 机制是核心）：

| 层 | 模块 | 证据 |
|---|---|---|
| 数据层 | `rqalpha/data/`：`data_proxy.py`（PIT 访问门面）、`base_data_source/`（含 `adjust.py` 复权）、`bundle/`（行情打包）、`bar_dict_price_board.py`（涨跌停板） | 🔍 路径 + ✅ data_proxy 内容 |
| 策略层 | `rqalpha/core/strategy.py`、`strategy_loader.py`、`strategy_context.py` | 🔍 |
| 回测引擎 | `mod/rqalpha_mod_sys_simulation/`：`matcher/{bar,signal,tick}_matcher.py`、`simulation_broker.py`、`slippage.py`、`simulation_event_source.py` | 🔍 + ✅ bar_matcher |
| 风控 | `mod/rqalpha_mod_sys_risk/validators/`：`price_validator.py`、`is_trading_validator.py`、`cash_validator.py`、`self_trade_validator.py` | ✅ 前两个已读原文 |
| 账务 | `mod/rqalpha_mod_sys_accounts/position_model.py`（持仓/交割/分红/红利税）、`rqalpha/portfolio/`（含 `capital_gains_tax.py`） | ✅ position_model + 🔍 capital_gains_tax |
| 服务化 | `mod/rqalpha_mod_sys_analyser`（报告/Excel 模板）、`sys_progress`、`sys_scheduler` | 🔍 |

**A 股规则正确性** —— 本报告中 A 股覆盖最完整的一个：

| 规则 | 实现 | 证据 |
|---|---|---|
| 涨跌停 | ✅ 事前风控拒单：限价单价格越界直接失败；涨跌停价来自 `price_board` | ✅ [price_validator.py](https://github.com/ricequant/rqalpha/blob/master/rqalpha/mod/rqalpha_mod_sys_risk/validators/price_validator.py) |
| T+1 | ✅ 持仓 `sellable` 定义为「所有持仓 − 今日买入 − 已冻结」，当日买入计入 `_non_closable` | ✅ [position_model.py](https://github.com/ricequant/rqalpha/blob/master/rqalpha/mod/rqalpha_mod_sys_accounts/position_model.py) |
| 停牌 | ✅ `is_suspended` → 拒单 | ✅ [is_trading_validator.py](https://github.com/ricequant/rqalpha/blob/master/rqalpha/mod/rqalpha_mod_sys_risk/validators/is_trading_validator.py) |
| 退市/未上市 | ✅ `get_active_instrument` 失败 → `"is not listing!"` | ✅ 同上 |
| ST 股 | ✅ `is_st_stock` 数据源接口 | ✅ `data_proxy.py:289` |
| 除权除息 | ✅ `get_dividend` / `get_split` + 持仓层 `_handle_dividend_book_closure` / `_handle_dividend_payable` / `_handle_split` | ✅ position_model.py / data_proxy.py |
| 红利税 | ✅ 独立模块 `capital_gains_tax.py`；持仓层 `_pay_dividend_tax` | ✅ + 🔍 |
| 复权 | ✅ `history_bars(adjust_type='pre'/'post')`，默认 `pre` | ✅ `data_proxy.py:211-240` |
| 流动性限制 | ✅ 成交量占比限制 `volume_limit` / 无量 bar 撤单 `inactive_limit` | ✅ [bar_matcher.py](https://github.com/ricequant/rqalpha/blob/master/rqalpha/mod/rqalpha_mod_sys_simulation/matcher/bar_matcher.py) |
| 整手 | ⚠️ 未核实（有 `round_order_quantity` 出现，未确认是否 100 股） | — |
| 印花税 | ⚠️ 未核实具体费率与历史区间（费用在 `sys_transaction_cost/deciders.py`，本次未读取） | — |

**事前风控的原文注释** ✅（值得品味的设计哲学）：
```python
limit_up = round(self._env.price_board.get_limit_up(order.order_book_id), 4)
# 此处不使用 price_limits.reaches_limit，以为事前风控宜松不宜紧，要对挂单做无罪推定。
if order.price > limit_up: ...
```

**值得借鉴的机制**：
1. **`mod` 插件机制**：账户/风控/撮合/费用/分析全部是平级可替换模块（`rqalpha_mod_sys_*`）。解决了「框架绑死单一撮合与费用假设」。移植前提：需要一个统一的 `env`（环境上下文）与 `AbstractFrontendValidator` 之类的抽象基类——lquant 已有 `Registry[T]` 与 `register_adapter`，💭 推断同构度较高。
2. **风控做成「校验器链」而非 if-else**：下单前多道 validator（价格、可交易、资金、自成交），每道独立返回失败原因字符串。解决了「风控规则增删要改撮合核心」的问题。
3. **T+1 建模为持仓字段**（`non_closable`）而非引擎特判。解决了「T+1 逻辑散落在撮合各处」的问题。
4. **bundle 数据打包**（`data/bundle/daybar.py` + `daybar_checker.py`）：数据完整性强校验 + 自动更新。🔍 解决「回测因数据缺失静默出错」。

**已知坑 / 失败教训**：
- 🔴 **许可证是最大障碍** ✅：源码头部中文声明——非商业用途遵守 Apache 2.0；**商业用途须获米筐科技授权**，且「不得向第三方提供、销售、出租、出借、转让本软件、本软件的衍生产品、引用或借鉴了本软件功能或源代码的产品或服务」（[price_validator.py 头部](https://github.com/ricequant/rqalpha/blob/master/rqalpha/mod/rqalpha_mod_sys_risk/validators/price_validator.py)）。原文甚至点名「**引用或借鉴**」。💭 我的理解：即使只「参考实现思路后自己重写」，法律风险也需法务判断；**建议只把 RQAlpha 当认知教材，不复制代码结构，不引为依赖**。
- **数据依赖米筐 bundle**：回测数据需通过 bundle 机制获取/打包，脱离其数据生态成本较高 💭 推断。
- **生态活跃但碎片化**：仓库内 mod 众多，README 编写风格偏「面向自家平台用户」。

---

### 2.3 qteasy —— 「交易规则可配置化」思路最贴合 A 股日频

**一句话定位与技术栈** 📄：本地化、灵活易用的高效量化投资工具包；作者 Jackie PENG；最新版本 2.6.5；Python ≥3.9,<3.13；BSD-3-Clause（[README](https://github.com/shepherdpp/qteasy/blob/master/README.md)）。**注意仓库已从 `zengbin93/qteasy` 迁到 `shepherdpp/qteasy`**。

**架构分层**：

| 层 | 实现 | 证据 |
|---|---|---|
| 数据层 | `DataSource` 对象 + `qt.refill_data_source()`；`DataType` 结构化管理字段；`HistoryPanel` 三维面板（标的 × 时间 × 数据类型）；本地存储 CSV（默认）/ MySQL / HDF / feather；主数据源 Tushare（需 token） | 📄 README；[data_channels.py](https://github.com/shepherdpp/qteasy/blob/master/qteasy/data_channels.py) 🔍 |
| 策略层 | `BaseStrategy` + `realize()`；内置 70+ 策略；多策略「积木式」组合与信号合并（加权平均/取交集等） | 📄 README |
| 回测引擎 | `qteasy/backtest.py`：`backtest_step` / `backtest_batch_steps` / `backtest_flash_steps`，NumPy + Numba；**时间维顺序 + 标的维向量化** | ✅ backtest.py |
| 风控 | `qteasy/risk.py` 🔍；仓位上限 `long_pos_limit` / `short_pos_limit` 作为回测参数 ✅ | ✅ backtest.py |
| 账务 | 交割队列（见下）+ `trade_recording.py` / `trade_io.py` 🔍 | ✅ backtest.py |
| 服务化 | `trader.py`（实盘/模拟后台线程）、`trader_cli.py`、`trader_tui.py`（TUI 界面） | 🔍 路径 + 📄 README |

**A 股规则正确性** —— 机制化程度最高：

| 规则 | 实现 | 证据 |
|---|---|---|
| T+N 交割 | ✅ **通用交割队列**：`stock_delivery_queue` + `stock_delivery_period`；A 股设 1 即 T+1 | ✅ `backtest.py::backtest_step` 参数与 `process_backtest_delivery(...)` 调用 |
| 资金交割 | ✅ `cash_delivery_queue` + `cash_delivery_period`（支持 T+N 资金可用） | ✅ 同上 |
| 整手（MOQ） | ✅ `moq_buy` / `moq_sell`，来自 `config['trade_batch_size']`（README 示例为 100） | ✅ [config_parser.py](https://github.com/shepherdpp/qteasy/blob/master/qteasy/config_parser.py) + [qt_operator.py](https://github.com/shepherdpp/qteasy/blob/master/qteasy/qt_operator.py) |
| 最低佣金累计 | ✅ `cost_min_buy` / `cost_min_sell` | ✅ config_parser.py |
| 买卖分档费率 | ✅ `cost_rate_buy` / `cost_rate_sell`（0≤x<1 校验） | ✅ config_parser.py |
| 滑点 | ✅ `cost_slippage`（比例，0≤x<1） | ✅ config_parser.py |
| 防未来函数 | ✅ 每步只向策略注入「当时可见」数据窗口；`use_latest_data_cycle` 显式控制是否使用交易时最新数据 | 📄 README + ✅ `qt_operator.py` 参数存在 |
| 复权 | ✅ `qt.candle(..., adj='b')` 等复权价显示与 `DataType` 复权字段 | 📄 README |
| 印花税历史区间 | ⚠️ 未核实（看架构应通过 `cost_rate_sell` 配置，但未找到内置历史区间表） | — |
| 涨跌停 | ⚠️ 未核实（未在 backtest.py 中发现涨跌停拒单逻辑） | — |
| 停牌 / 退市 | ⚠️ 未核实 | — |

**交割队列的原文证据** ✅（`qteasy/backtest.py`）：
```python
def backtest_step(
        ...
        cash_delivery_queue: np.ndarray,
        stock_delivery_queue: np.ndarray,
        cash_delivery_period: int,
        stock_delivery_period: int,
        moq_buy, moq_sell, cost_params, long_pos_limit, short_pos_limit,
):
    ...
    delivered_cash, delivered_stocks = process_backtest_delivery(
        cash_delivery_queue=cash_delivery_queue,
        stock_delivery_queue=stock_delivery_queue,
        is_new_day=is_delivery_day, ...)
```
且 docstring 明确 `moq_buy` 语义：「投资产品最小买入交易单位，moq为0时允许交易任意数额，moq不为零时允许交易的产品数量是moq的整数倍」✅。

**值得借鉴的机制**：见 Top 8 #1、#5。核心是「把市场制度抽象成**队列 + 周期参数**」而不是散落的特判。

**已知坑 / 失败教训**：
- **回测引擎未覆盖涨跌停/停牌** 💭 推断（⚠️ 未核实到）：qteasy 的制度建模重点在交割与费用，价格约束类规则需要用户自行处理。对 A 股日频选股而言，涨跌停不可成交是**影响最大的单条规则**（尤其小市值/打板风格），这是明显短板。
- **Tushare 依赖** 📄：默认数据源需自行申请 token 与积分；💭 推断免费额度对全市场长周期日线+财务不够用。
- **Python 版本上限** 📄：`>=3.9,<3.13`，与 lquant（3.12+）兼容但无法用到 3.13。
- **依赖可选包易踩坑** 📄：`ta-lib` 需先装 C 库；`pytables` 需 conda 装。

---

### 2.4 vn.py —— 事件引擎 + 网关抽象（实盘侧范式）

**一句话定位与技术栈** 📄：基于 Python 的开源量化交易平台开发框架（[README](https://github.com/vnpy/vnpy/blob/master/README.md)）。核心极轻（无 Python 第三方强依赖），能力通过 `vnpy_*` 独立包扩展（如 `vnpy_ctastrategy`、各券商 gateway）。MIT，star 量级 4.5 万+。

**架构分层**：

| 层 | 实现 | 证据 |
|---|---|---|
| 数据层 | `vnpy/trader/database.py`（抽象）、`datafeed.py`；实际存储由 `vnpy_mysql`/`vnpy_sqlite`/`vnpy_mongodb` 等独立包提供 | 🔍 |
| 策略层 | 不在主仓库，在 `vnpy_ctastrategy` / `vnpy_portfoliostrategy` 等扩展包 | 📄 README |
| 回测引擎 | 不在主仓库（`vnpy_ctastrategy` 内置 CTA 回测）；主仓库提供 `vnpy/trader/optimize.py` 参数优化工具 | 🔍 |
| 风控 | `vnpy_riskmanager` 独立包 🔍 | — |
| 账务 | `vnpy/trader/object.py`（AccountData/PositionData/TradeData/OrderData）+ `vnpy/trader/engine.py` 的 OMS 类服务 | ✅ gateway.py 回调签名 + 🔍 object.py |
| 服务化 | `vnpy/rpc/`（`client.py`/`server.py`）跨进程/跨机通信；`vnpy_web`/`vnpy_rest` 等独立包提供 API | 🔍 |

**事件引擎** ✅（[vnpy/event/engine.py](https://github.com/vnpy/vnpy/blob/master/vnpy/event/engine.py)）：
```python
EVENT_TIMER: str = "eTimer"

class EventEngine:
    def __init__(self, interval: int = 1) -> None:
        self._queue: Queue = Queue()
        self._active: bool = False
        self._thread: Thread = Thread(target=self._run)
        self._timer: Thread = Thread(target=self._run_timer)
    def _run(self) -> None:      # 主循环：从队列取事件并分发
    def _process(self, event) -> None:   # 按 type 查 handler 列表
    def _run_timer(self) -> None:        # 每秒产生 EVENT_TIMER
    def register(self, type, handler) / unregister(...) / register_general(...)
```

**网关抽象** ✅（[vnpy/trader/gateway.py](https://github.com/vnpy/vnpy/blob/master/vnpy/trader/gateway.py)）：`BaseGateway(ABC)` 定义
- 主动方法：`connect` / `close` / `subscribe` / `send_order` / `cancel_order` / `send_quote` / `cancel_quote` / `query_account` / `query_position` / `query_history` / `get_default_setting`
- 回调方法：`on_tick` / `on_trade` / `on_order` / `on_position` / `on_account` / `on_quote` / `on_log` / `on_contract`
- 每个回调内部即 `self.on_event(EVENT_XXX, data)` 推入事件引擎。

**A 股规则正确性**：❌ **主仓库基本不涉及**。涨跌停/T+1/印花税/整手均无内置证据（⚠️ 未核实到）。💭 推断：vn.py 的定位是「实盘通道与事件中枢」，A 股股票交易需通过券商网关（如 XTP/QMT 类）+ 风控包，制度规则依赖柜台。

**值得借鉴的机制**：见 Top 8 #7。`EventEngine` + `BaseGateway` 的组合解决了「同一套策略代码对接 N 家券商/行情源」的问题：新增通道＝实现一个 gateway 类，不改上层。

**已知坑 / 失败教训**：
- **仓库体积巨大**：`size_kb` 约 334,500 ✅（本次 API 返回），含大量图片/资源；核心代码其实很薄。
- **主仓库不含策略与回测** 💭 推断：新手容易误以为 clone 主仓库即可回测，实际需理解 `vnpy_*` 扩展包生态。
- **A 股支持非一等公民**：生态里 gateway 以期货 CTP 为最强项 💭 推断（⚠️ 未核实逐项清单）。
- **GUI 依赖 PySide6**，服务端部署需无头适配 💭 推断。

---

### 2.5 QUANTAXIS —— 全栈 + 分布式/任务编排

**一句话定位与技术栈** 📄：支持任务调度与分布式部署的 股票/期货/期权「数据/回测/模拟/交易/可视化/多账户」纯本地量化解决方案（[README](https://github.com/yutiansut/QUANTAXIS/blob/master/README.md)）。Python，MIT，存储 MongoDB 4.0+ / ClickHouse 20.0+（可选）📄。

**架构分层** 📄（README 章节标题即分层）：

| 层 | 模块 | 说明 |
|---|---|---|
| 数据层 | `QAFetch`（`QAQuery.py` 🔍）、MongoDB/ClickHouse、`QASetting.py` 🔍 | 数据落库可查 |
| 账务 | **QIFI / QAMarket 统一账户体系**：`qifiaccount` 与 Rust/C++ 版本 100% 一致；`marketpreset` 市场预制基类（tick 大小 / 保证金 / 手续费） | 📄 + ✅ `QUANTAXIS/QAMarket/market_preset.py` 存在 |
| 账户桥 | `QARSBridge.QARSAccount`（高性能 QIFI 账户，`buy` / `buy_open` / `get_positions` / `get_qifi` / `get_account_info`） | 📄 README 代码示例 |
| 回测/模拟 | QAARP（**已移除，不再维护老版本** ✅） | 📄 README |
| 服务化 | `QAWebServer`（Tornado）、`QASchedule`（后台 + 远程任务调度）、`QAEngine`（异步计算 + 局域网分布式 agent） | 📄 |
| 风控 | `OrderGateway` 风控 | 📄（内容未核实） |

**A 股规则正确性**：⚠️ **基本未核实**。`market_preset.py` 是市场预制基类（tick/保证金/手续费）✅ 路径存在，但 T+1、涨跌停、印花税历史区间、停牌、复权、幸存者偏差**均未找到证据**。💭 推断：QUANTAXIS 更偏「数据 + 账户 + 调度」的平台骨架，A 股微观制度靠使用方自行配置。

**值得借鉴的机制**：
1. **QIFI 账户协议与 Rust/C++ 版本 100% 一致** 📄 —— 这是一个**跨语言账户状态契约**。解决的问题：回测/模拟/实盘/多语言组件之间账户口径漂移。对 lquant 的价值：💭 中高（lquant 有 Rust 算子层，若未来账户也进 Rust，需要类似契约；但当前 Python 单侧，收益有限）。
2. **`marketpreset` 把「市场制度」做成可继承的预制类**（tick/保证金/手续费）📄 —— 解决的问题：多市场/多品种参数散落。与 lquant 的 `config/rules/cn_a_share.yaml` 思路同向，但它是**代码类 + 继承**，lquant 是 **YAML 数据化**。💭 我的判断：lquant 的 YAML 路线对「规则可审计、可追溯、可 diff」更好。
3. **任务调度与分布式 agent** 📄（`QAEngine` / `QASchedule`）—— 解决的问题：全市场回填与批量回测的算力扩展。对 lquant 的价值：中（lquant 已有 RQ + 断点续传 + 本地线程降级，分布式是更后面的需求）。

**已知坑 / 失败教训**：
- **open issues 高达 240** ✅（本次 API），相对 Hikyuu 的 4 与 vn.py 的 5 明显偏高。
- **老模块被移除** 📄：README 明写「⚠️ 移除 QAARP（不再维护老版本）」——意味着**历史上存在过大规模重构/破坏性变更**，第三方教程与示例代码可能失效 💭 推断。
- **重型外部依赖** 📄：MongoDB / ClickHouse 作为核心存储，部署与运维成本高于 Parquet 文件湖。对 lquant 是明确的**反例**（lquant 用 DuckDB + Parquet 零服务依赖）。
- **架构野心过大**：数据/回测/模拟/交易/可视化/多账户/调度/微服务全都要 💭 推断，容易出现「哪块都不够深」；从 A 股制度规则几乎无内置可见一斑。

---

### 2.6 pybroker —— walk-forward 与费用抽象最规范

**一句话定位与技术栈** 📄：Algorithmic Trading in Python with Machine Learning（[README](https://github.com/edtechre/pybroker/blob/master/README.md)）。纯 Python，依赖含 pandas/numpy/Numba（`vect.py`）；许可 **Apache 2.0 + Commons Clause** ✅（`src/pybroker/config.py` 文件头原文："This code is licensed under Apache 2.0 with Commons Clause license"）。

**架构分层** 🔍（`src/pybroker/`）：`data.py` + `ext/data.py`（数据源，含 Yahoo/Alpaca 类）、`indicator.py` + `vect.py`（指标/Numba 向量化）、`strategy.py`（`Strategy` / `ExecContext` / `TestResult`）、`portfolio.py`（账户/持仓）、`eval.py`（指标 + bootstrap）、`model.py`（模型训练）、`optimize.py`、`parallel.py`、`slippage.py`、`cache.py`、`scope.py`。

**A 股规则正确性**：❌ 面向美股/加密。**无涨跌停、T+1、印花税、整手的内置证据**（⚠️ 未核实到）。有**通用**的：
- 费用模型 `FeeMode` ✅：`ORDER_PERCENT`（按订单金额百分比）/ `PER_ORDER`（每单固定）/ `PER_SHARE`（每股固定）/ `Callable[[FeeInfo], Decimal]`（自定义）/ `None`。
- `buy_delay` / `sell_delay` 默认 `1` ✅ —— 即下一个 bar 成交，**默认规避未来函数**。
- `enable_fractional_shares`（加密用）、`position_mode`（DEFAULT/LONG_ONLY/SHORT_ONLY）、`leverage`、`interest_rate`、`round_fill_price` ✅。

**值得借鉴的机制**：见 Top 8 #6。
1. **内建 walk-forward 分析** ✅：`WalkforwardWindow(NamedTuple)` 携带 `train_data` / `test_data` 行索引；`walkforward_split(df, windows, lookahead, train_size=0.9, shuffle=False)`；`Strategy.walkforward(windows, lookahead=1, train_size=0.5, calc_bootstrap=False, parallel_indicators, parallel_models, warmup, seed=42)`。文档明确 `lookahead` 的作用：「需要防止训练数据泄漏到测试边界；**任何窗口测试集中的 bar 都不会用于拟合该窗口的模型**」。解决了「只有全样本回测、没有样本外验证」导致过拟合的问题。
2. **费用模型抽象为枚举 + 可调用对象** ✅：`FeeMode` 覆盖「按比例/按单/按股/自定义」四类。解决了「不同券商费率结构异构」的问题。💭 移植前提：需要一个 `FeeInfo` 值对象（订单金额/股数/方向）。
3. **bootstrap 置信区间评价** ✅：`bootstrap_samples` 默认 `10_000`，`calc_bootstrap` 可选开关。解决了「只报点估计、不知策略收益是否显著异于随机」的问题。
4. **滑点模型为可插拔对象** ✅：`SlippageContext` + `apply_slippage(ctx) -> (shares, fill_price)` + `adjust_fill` + `is_fill_noop`，且有「成交价不得降到 0 或变负」的 adverse-price floor 常量。解决了滑点与流动性在撮合中的耦合。

**已知坑 / 失败教训**：
- **Commons Clause 限制** ✅：不能直接提供「基于本软件的托管/付费服务」类商业化。引为依赖需法务评估。
- **数据源偏美股** 💭 推断：A 股需自建 `DataSource` 适配。
- **无 A 股制度** 💭 推断（见上）：拿来做 A 股日频选股，制度层要从零补。

---

### 2.7 Abupy（abu）—— 中文生态最强，但明显老化

**一句话定位与技术栈** 📄：阿布量化交易系统（股票、期权、期货、比特币、机器学习），基于 Python 的开源量化投资架构（[README](https://github.com/bbfamily/abu/blob/master/readme.md)）。GPL-3.0，star 18,872，**最近提交 2026-01-24**（相对偏旧）✅。

**A 股规则正确性** —— 有明确文档证据 ✅：
README 课程目录第 8 讲「A 股市场的回测」包含条目：
> 「1. A股市场的回测示例　2. **涨跌停的特殊处理**　3. 对多组交易结果进行分析」

第 3 讲包含「**滑点策略与交易手续费**」：滑点买入卖出价格确定及策略实现、交易手续费的计算以及自定义手续费 ✅（含一张 `type | date | symbol | commission` 表格）。
第 4 讲「多支股票择时回测与仓位管理」：自定义仓位管理策略的实现 ✅。
第 6 讲「回测结果的度量」：度量基本用法、可视化、扩展自定义度量类 ✅。
第 7 讲「寻找策略最优参数和评分」：参数取值范围、Grid Search、度量评分机制、不同权重评分、自定义评分类 ✅。

💭 我的判断：Abupy 是**少数把「A 股涨跌停特殊处理」写进正式教程**的项目，说明作者认真处理过；但**具体实现类名与文件路径 ⚠️ 未核实**（我按 `abupy/TradeBu/*` 猜测的 4 条路径全部 404，说明源码目录结构与常见记忆不符，未继续深挖）。

**架构分层**：⚠️ **未核实到具体分层证据**。README 目录显示其组织方式按「教程讲次 + 功能主题」（择时、仓位管理、度量、参数评分、多市场），而非按传统工程分层。💭 推断：Abupy 更接近「教科书 + 工具箱」而非「平台」。

**值得借鉴的机制** 💭（基于 README 结构，非源码）：
- **买入因子 / 卖出因子 / 仓位管理三段式**思路（README 多讲次围绕此展开）—— 与 Hikyuu 的 `SG` + `MM` 部件化同源。⚠️ 类名未核实。
- **度量评分体系**（多度量加权打分选参数）✅ README 明确 —— 解决的问题：单指标（如收益率）选参会选出「高收益高风险」的脆弱参数。对 lquant 价值：高（因子研究天然需要多指标评分）。

**已知坑 / 失败教训**：
- **最近提交 2026-01-24**，且 README 通篇是「量化教程/书籍配套」风格，💭 推断工程维护力度弱于 Hikyuu/RQAlpha。
- **GPL-3.0** ✅：传染性许可，引代码进闭源平台有法律风险。
- **历史包袱**：README 推荐 Anaconda + 明确的「环境部署」文档，💭 推断对现代 uv/pyproject 工作流不友好；且作为教学项目常绑定较老依赖（具体版本 ⚠️ 未核实）。
- **多市场并行**（A 股/港股/比特币）导致 A 股特化深度有限 💭 推断。

---

### 2.8 zipline-reloaded —— 原版已死，续命版的坑同样真实

**一句话定位与技术栈** 📄：Pythonic 事件驱动回测系统，原为 Quantopian 的官方引擎（[README](https://github.com/stefan-jansen/zipline-reloaded/blob/main/README.md)）。Apache-2.0，Python ≥3.9。

**关键历史** 📄（README 原文）：
> Zipline 由众包投资基金 Quantopian 开发并作为其回测与实盘引擎使用。**该基金于 2020 年底关停**，托管其文档的域名已过期。该库被 Stefan Jansen 的《Machine Learning for Algorithmic Trading》一书广泛使用，作者正努力保持其可用。

**架构分层**（路径已确认存在 🔍）：
| 层 | 路径 |
|---|---|
| 数据层 | `src/zipline/data/bundles/core.py`（bundle 打包与注册） |
| 因子层 | `src/zipline/pipeline/engine.py`（**Pipeline 横截面因子计算引擎**） |
| 策略层 | `src/zipline/algorithm.py`（`TradingAlgorithm`，回测/实盘同一套） |
| 回测引擎 | `src/zipline/finance/ledger.py`（账本）、`src/zipline/finance/metrics/metric.py` |
| 费用/滑点 | `src/zipline/finance/commission.py` |

**A 股规则正确性**：❌ 面向美股。有**通用**的佣金与滑点模型（`commission.py`）✅ 路径存在，但涨跌停/T+1/印花税/整手**无证据**（⚠️ 未核实到）。💭 注：`TradingAlgorithm` 有 `handle_halts` 之类的停牌处理概念，⚠️ 本次未核实。

**值得借鉴的机制** 💭（部分为路径级证据）：
1. **Pipeline 横截面因子 API**（`src/zipline/pipeline/engine.py`）：把因子计算声明为「对全市场面板的表达式」，引擎负责按交易日切片计算。解决的问题：截面因子（排序/分位/中性化）在事件驱动框架里最难写。**对 lquant 的因子研究定位价值极高**——但 lquant 已用 Polars 表达式自研 DSL，💭 更该借鉴的是「因子计算与交易日历严格对齐」的语义，而非引库。
2. **bundle 数据打包**：解决「回测数据可复现」。
3. **同一 `TradingAlgorithm` 跑回测与实盘**。

**已知坑 / 失败教训**：
- **升级即破坏** ✅ README 明写：3.0 升级到 pandas ≥2.0 / SQLAlchemy >2.0「是重大版本更新，**可能破坏现有代码**」；2.4 升级 `exchange_calendars` ≥4.2「可能破坏现有代码」；3.05 兼容 numpy 2.0 需 pandas ≥2.2.2 且提醒「其他包可能还没跟上」。—— **这是依赖地狱的教科书案例**。
- **生态已死** 💭：原 Quantopian 社区与文档域名失效，只剩作者个人 + ML4T 读者群维护。
- **最近提交 2026-01-06** ✅，属本次调研中第二旧。
- **macOS/ARM 编译问题** ⚠️ 未核实（历史上有 C 扩展编译负担）。

---

### 2.9 backtrader —— 🔴 已停更，新项目不应选

**一句话定位与技术栈** 📄：Python 回测库（[README.rst](https://github.com/mementum/backtrader/blob/master/README.rst)）。GPL-3.0，star 23,430（本组最高之一）。

**活跃度**：✅ **`pushed_at = 2024-08-19`** —— 在本次查询的全部项目中唯一停留在 2024 年的，**事实停更**。

**架构与能力** 📄（README 特性列表原文）：多数据源（csv/在线/pandas/blaze）、Renko/daily→intraday 切片 filter、多数据多策略、多周期、内建重采样与 replay、逐步或一次性回测、内建指标库、TA-Lib 支持、Analyzers（TimeReturn / Sharpe / SQN，**pyfolio 集成标注 deprecated**）、**Flexible definition of commission schemes**、内建 broker 模拟（Market/Close/Limit/Stop/StopLimit/StopTrail/StopTrailLimit/OCO、bracket 单、**slippage**、**volume filling strategies**）、Sizers、**Cheat-on-Close / Cheat-on-Open**、Schedulers、Trading Calendars、绘图。

**路径事实** 🔍：`backtrader/cerebro.py`（引擎/编排中心 `Cerebro`）、`backtrader/brokers/bbroker.py`（撮合与账户）、`backtrader/linebuffer.py`（`LineSeries`/`LineBuffer` 的元类驱动数据管道）均确认存在。⚠️ `backtrader/commissions/comminfo.py` 返回 404，说明佣金类实际路径与常见记忆不同，**未核实**。

**A 股规则正确性**：❌ **全部无内置**。涨跌停/T+1/印花税区间/整手/停牌/幸存者偏差均无证据（⚠️ 未核实到）。有通用 `commission schemes` 与 `volume filling`，💭 推断若要实现 A 股规则，应挂在 broker（`bbroker.py`）的订单执行与 `commissions`（佣金）层。

**值得借鉴的机制** 💭：
- **`Line`/`LineSeries` + 元类驱动的声明式指标**（`linebuffer.py`）：指标只需写 `lines = ('xxx',)` 与 `next()`，框架自动处理最小周期、对齐与数组扩展。解决的问题：手写向量化指标的窗口对齐极易错位。**移植前提**：需要一套「行式」数据管道抽象；lquant 走 Polars 表达式路线，💭 二者是替代关系而非互补，移植价值低。
- **Cheat-on-Close / Cheat-on-Open 显式开关**：把「用当根 bar 收盘价成交」这种**必然引入未来函数**的行为做成显式 API，而不是默许。✅ README 原文有该特性。💭 这个「危险能力必须显式开启」的设计哲学值得抄。
- **Analyzer 插件化**：绩效分析器可增删。💭 与 lquant 的指标输出层同向。

**已知坑 / 失败教训**：
- 🔴 **停更（2024-08）** ✅。
- **GPL-3.0** ✅。
- **`pyfolio` 集成已 deprecated** ✅ README 明示 —— 依赖链腐烂的实例。
- 💭 架构基于 Python 元类魔术，调试与类型提示体验差；社区 fork（如 backtrader2）活跃度需另查 ⚠️ 未核实。

---

### 2.10 backtesting.py —— 轻量对照组

**一句话定位与技术栈** 📄：`🔎 📈 🐍 💰 Backtest trading strategies in Python.`（[README](https://github.com/kernc/backtesting.py/blob/master/README.md)）。**AGPL-3.0** ✅，star 9,020，最近提交 2026-08-05 ✅。

**架构** 🔍：`backtesting/backtesting.py`（主模块：`Backtest` / `Strategy` / `optimize`）、`backtesting/lib.py`（辅助）。单文件风格，无重依赖（numpy/pandas/bokeh）。

**A 股规则正确性**：❌ 无内置（⚠️ 未核实到任何涨跌停/T+1/印花税处理）。💭 推断：非 A 股目标，`optimize()` 用多头进程跑参数网格。

**值得借鉴** 💭：极简 API（`Backtest(data, strategy).run()` + `optimize(...)`）与「回测结果自带交互式图表」的体验。对 lquant 的价值：低（lquant 目标是平台而非单文件库），但**API 简洁度**值得作为看板交互的参考。

**坑**：**AGPL-3.0** ✅ —— 若作为网络服务提供且修改源码，需开源；引为依赖风险高。

---

### 2.11 bt —— 组合式（algos）回测

**一句话定位与技术栈** 📄：从可复用 Python 组件构建、测试、比较投资策略（[README](https://github.com/pmorissette/bt/blob/master/README.md)）。MIT ✅，star 2,996，最近提交 2026-10-06 ✅（很活跃）。

**核心机制** 📄 README 特性原文：
- **Compose strategy logic**: combine algorithms for **scheduling, security selection, weighting, and rebalancing**.
- **Build portfolios of strategies**: nest strategies and securities in a common tree.
- **Model trading costs**: configure commissions and transaction cost models.
- **Compare results**: 通过 [ffn](https://github.com/pmorissette/ffn) 输出收益/权重/交易/回撤统计与图表。

**A 股规则正确性**：❌ 无内置（⚠️ 未核实到）。

**值得借鉴的机制** 💭：**把组合回测拆成 4 类可组合算法**（调度 / 选股 / 权重 / 再平衡），并用策略树嵌套表达「组合的组成合」。解决的问题：多策略组合的配置爆炸。
**对 lquant 的价值：高**——lquant 的 L4 层已有 `screener → dedup(相关性去重) → weighting(风险平价/HRP)` 流水线，💭 与 bt 的 algos 思路同构；差异是 bt 允许把「策略」本身作为树的节点递归嵌套，lquant 目前是固定流水线。若将来要做「母基金/策略组合的元回测」，bt 的树形组合是直接参考。

**坑**：依赖 `ffn` 输出图（matplotlib），与看板技术栈（ECharts）不同构 💭 推断；组合回测偏月频调仓，日内/日频细节弱。

---

### 2.12 vectorbt —— 向量化对照组（速度的极端值）

**一句话定位与技术栈** 📄：README 原文 ——
> VectorBT takes a radically different approach to backtesting: instead of looping through bars one strategy at a time, it **packs thousands of configurations into NumPy arrays, accelerates the hot path with Numba and Rust, and runs them all at once**, turning hours of grid search into seconds.

依赖：pandas + NumPy + Numba，**可选 Rust 引擎**（预编译提速、免 JIT 开销）✅。许可：**fair-code，Apache 2.0 with Commons Clause** ✅。star 9,297，最近提交 2026-09-26 ✅。

**关键约束** ✅（README 原文）：本项目是 **VectorBT PRO 的开源社区版**。PRO 才提供：并行化、更多数据集成、组合优化，以及**限价单 / 杠杆 / 期货合约乘数建模**、随机搜索、**条件参数**、交叉验证教程。README 中多处特性直接标注 `> **VectorBT PRO**` 才可用。

**架构** 🔍：`vectorbt/portfolio/{base,orders,trades,logs,nb}.py`、`vectorbt/indicators/{basic,configs,factory,nb}.py`、`vectorbt/generic/{splitters,stats_builder,ranges,drawdowns}.py`、`vectorbt/base/{accessors,array_wrapper,combine_fns,index_fns,reshape_fns}.py`。

**A 股规则正确性**：❌ 无内置。且 README 明确把**限价单**列为 PRO 功能 ✅ —— 而 A 股涨跌停本质需要限价/不可成交语义。💭 这是选型的决定性缺陷。

**值得借鉴的机制** 💭：
1. **把参数网格编码为数组的新维度**，用一次广播代替 N 次循环。解决的问题：万级参数扫描的事件循环开销。**但**：A 股 T+1/涨跌停/手数无法在纯向量化里正确表达（路径依赖），因此只能用于**筛参数**。
2. **`base/combine_fns.py` + `reshape_fns.py`**：用一套「合并/重塑函数」统一处理 1D/2D/ND 参数组合的广播语义。💭 这是向量化参数扫描真正的难点所在，值得读源码（本次未读 ⚠️）。
3. ⚠️ 我**未核实** `param_product` / `from_signals` 等具体 API 名，README 中未出现；引用时请勿直接使用这些名字。

**坑**：
- **Commons Clause** ✅：商业限制。
- **Numba 重二进制依赖** + 可选 Rust：与 lquant 的 Polars/DuckDB 轻部署取向冲突 💭。
- **PRO 闭源** ✅ README 明确：重要能力（限价单/杠杆/并行/条件参数）在闭源版，开源版是「漏斗」。

---

### 2.13 WonderTrader —— C++ 多引擎与机构级风控设计

**一句话定位与技术栈** 📄：基于 C++ 核心模块、适应全市场全品种交易的量化交易开发框架；应用层为 `wtpy`（[README](https://github.com/wondertrader/wondertrader/blob/master/README.md)）。MIT ✅，star 6,380，最近提交 2026-09-01 ✅。

**架构分层** 📄（README 明确）+ 🔍 路径：

| 层 | 实现 | 证据 |
|---|---|---|
| 数据层 | **本地数据伺服**：内置存储引擎本地落盘，通过 **UDP 端口广播实时行情**，实现 `1+N` 服务结构，可向多个组合盘提供无差别数据服务 | 📄 README |
| 策略层 | **多引擎**：CTA（同步策略引擎，事件+时间驱动，单标的择时/中频套利）、SEL（异步策略引擎，时间驱动，**多因子选股/截面多空**）、HFT（事件驱动，1–2 μs）、UFT（0.9 起，175 ns 内） | 📄 README |
| 回测引擎 | **统一回测引擎**，C++ 实现；C++ 策略与 Python 策略都能回测；CTA/SEL/HFT/UFT 与执行单元均可回测；`WtBtSnooper` 回测查看器 | 📄 README（`src/WtBtCore/WtBtEngine.h` 未核实到） |
| 风控 | **四层**：① 组合盘资金风控（虚拟资金，组合下行触发即停）；② 通道流量风控（合规：总撤单笔数、短时下单/撤单次数）；③ 账户资金风控（回撤）；④ **离合器机制**（信号与执行分离，风险时直接断开信号执行，不影响策略继续计算以便观察） | 📄 README |
| 账务 | `策略组合`：理论部位**独立存储**、组合盘整体绩效独立核算、多账户并发执行 | 📄 README |
| 服务化 | `wtpy` 监控服务（组合盘运行监控、实时事件通知、回测查看器）；**全自动远程部署（标注"在建"）** | 📄 README |

**路径事实** 🔍：`src/WtCore/WtEngine.h` ✅ 存在、`src/WtCore/TraderAdapter.h` ✅ 存在；⚠️ `src/WtCore/WtRiskMonitor.h` 与 `src/WtBtCore/WtBtEngine.h` 在我探测的路径下 404，**具体类名未核实**。

**A 股规则正确性**：⚠️ **未核实到逐条证据**。README 强调全市场全品种与组合风控，但涨跌停/T+1/印花税/整手/复权的具体配置项**我未取得**。💭 推断：作为以中国期货/股票为目标且"数十亿级实盘管理规模"的框架，制度参数应通过配置文件提供；**引用时请勿声称它「内置了」具体某条规则**。

**值得借鉴的机制**：
1. **信号与执行分离 + 目标仓位合并执行** 📄：原文「目标仓位合并以后，避免了自成交的风险，同时降低了保证金占用和佣金开销」。解决的问题：多策略操作同一标的时的自成交与重复委托。**对 lquant 的价值：高**——lquant 的模拟盘/组合层多策略并存时，同样需要「先合并目标仓位，再统一执行」。
2. **离合器机制** 📄：风险触发时**只断开信号执行、不断开策略计算**，从而继续观察策略在特定行情下的表现。解决的问题：风控熔断后「策略变盲」。💭 这是很精巧的产品设计，lquant 的看板/模拟盘可借鉴其「旁路观察」思想。
3. **分引擎而非单引擎** 📄：CTA/SEL/HFT/UFT 分别对应不同标的数与延迟要求，**共用一套数据与撮合**。SEL 引擎明确面向「多因子选股 / 截面多空」——与 lquant 日频选股定位最接近。💭 解决的问题：用低频框架跑多标的截面策略性能不足（README 原文批评解释型语言在 50–100 标的上「无法满足需求」）。
4. **M+1+N 执行架构** 📄：一个组合盘（资金/风险参数/单位交易数量）→ 多账户按资金规模与风险偏好放大手数（README 给了 500w/1000w 的倍数计算示例）→ 多通道执行。解决的问题：同一个策略组合服务多个不同风险偏好的账户。
5. **C++ 核心 + Python 策略** 📄：兼顾性能与策略保密（原文：C++ 级别代码提供最大策略保密性）。

**已知坑 / 失败教训**：
- **C++ 构建门槛** 💭 推断（README 未见 pip 一键安装说明于正文；实际使用依赖 `wtpy` 包）；`src/` 下 C++ 工程编译对 Python 团队是硬门槛。
- **文档与实现边界不清**：README 中 UFT/HFT 延迟数字（175ns/1-2μs）是厂商宣称 💭，需自行验证。
- **「全自动远程部署」标注"在建"** 📄 —— 说明部分能力是路线图而非现状。
- 💭 推断：核心抽象在 C++，Python 层做绑定，**策略调试体验与纯 Python 框架有差距**；对 lquant（Python 为主 + Rust 算子插件）而言，直接移植代码不可行，只能借鉴设计。

---

## 3. Top 8 最值得 lquant 借鉴的机制

> 评级维度：**可移植性**（与 lquant 的 Python/Polars/Rust 技术栈是否同构）、**移植成本**（低/中/高）、**对「A 股日频选股 + 因子研究 + 回测 + 看板」的价值**。

### 🥇 #1 qteasy 的「交割队列」通用 T+N 建模
- **机制是什么** ✅：把市场交割制度抽象成两个队列 + 两个周期参数——`stock_delivery_queue` / `cash_delivery_queue` 与 `stock_delivery_period` / `cash_delivery_period`；每步调用 `process_backtest_delivery(...)` 推进。A 股设 `stock_delivery_period=1` 即 T+1，ETF/港股/期货改参数即可。
- **解决什么问题**：T+1 从「撮合里的特判」变成「状态队列」，既避免制度逻辑散落，又能表达 T+0/T+1/T+2、资金 T+0 可用但股份 T+1 可卖的分离（A 股实际就是这种不对称）。
- **移植前提**：需要一个「按日推进 + 可变状态」的回测循环。⚠️ lquant 的向量化快扫（`BACKTEST_ENGINES.md` B5）**无法直接承载**路径依赖队列——💭 这恰好印证了 lquant 现有「向量化筛参 → 事件引擎复核」的分工是对的。
- **可移植性**：高（纯 Python/NumPy 语义，无重依赖）｜**移植成本**：低-中｜**价值**：**极高**（lquant 已有 `config/rules/cn_a_share.yaml` 与规则表，把 T+N 数据化是自然延伸）
- 证据：[config_parser.py](https://github.com/shepherdpp/qteasy/blob/master/qteasy/config_parser.py)、[backtest.py](https://github.com/shepherdpp/qteasy/blob/master/qteasy/backtest.py)

### 🥈 #2 RQAlpha 的「事前风控校验器链」
- **机制是什么** ✅：下单前依次经过独立 validator（`PriceValidator` 查涨跌停、`IsTradingValidator` 查停牌/退市、`CashValidator` 查资金、`SelfTradeValidator` 查自成交），每个 validator 只返回 `None` 或**人类可读的拒单原因字符串**。
- **解决什么问题**：风控规则增删不必改撮合核心；**拒单原因是可解释的**（A 股回测里「为什么这笔没成交」是最常见的疑问）。
- **移植前提**：需要一个统一的 `env`/context 暴露 data_proxy 与 price_board（涨跌停价）。lquant 已有 `config/rules/cn_a_share.yaml` 与规则表，💭 只需补一个「涨跌停价查询」抽象即可落地。
- **可移植性**：高（纯 Python）｜**移植成本**：低｜**价值**：**极高**
- 沉淀的设计哲学 ✅（原文注释）：「事前风控宜松不宜紧，要对挂单做无罪推定」——事前只挡明显违规，成交与否交给撮合。
- 证据：[price_validator.py](https://github.com/ricequant/rqalpha/blob/master/rqalpha/mod/rqalpha_mod_sys_risk/validators/price_validator.py)、[is_trading_validator.py](https://github.com/ricequant/rqalpha/blob/master/rqalpha/mod/rqalpha_mod_sys_risk/validators/is_trading_validator.py)
- ⚠️ **法律提醒**：许可原文包含「引用或借鉴本软件功能或源代码」的禁止表述，建议**只吸收设计思想、独立实现**，不复制代码。

### 🥉 #3 Hikyuu 的「交易费用类按监管历史区间版本化」
- **机制是什么** ✅：把 A 股费用拆成若干**具名类**：`TC_FixedA`（旧规则）、`TC_FixedA2015`、`TC_FixedA2017`、`TC_FixedETF`，每个类带 `commission` / `lowest_commission` / `stamptax` / `transferfee` 参数；核心逻辑是「买入＝佣金(≥5元)+过户费」「卖出＝佣金+印花税(仅 A/GEM)+过户费(仅沪市)」。
- **解决什么问题**：长周期回测跨越费率/印花税调整点时，单一费率必然产生系统性偏差；版本化类让「哪一年用哪套费率」显式可审计。
- **移植前提**：需要一个「按日期选费率表」的解析器。lquant 已有 `config/rules/cn_a_share.yaml`（据 `BACKTEST_ENGINES.md` 与 `ARCHITECTURE.md` 描述其含「印花税区间」），💭 落地成本很低——把费率表按生效区间数据化即可，无需类继承。
- **可移植性**：高（C++ 逻辑可直译为 YAML/规则表）｜**移植成本**：低｜**价值**：**极高**
- 证据：[FixedA2017TradeCost.cpp](https://github.com/fasiondog/hikyuu/blob/master/hikyuu_cpp/hikyuu/trade_manage/imp/FixedA2017TradeCost.cpp)、[FixedA2015TradeCost.cpp](https://github.com/fasiondog/hikyuu/blob/master/hikyuu_cpp/hikyuu/trade_manage/imp/FixedA2015TradeCost.cpp)

### #4 Hikyuu 的「策略部件化 + 统一命名体系」
- **机制是什么** 📄：把策略拆成可独立替换的部件，并给出短名：组合层 `PF`(Portfolio)/`SE`(Selector)/`AF`(AllocateFunds)/`MF`(MultiFactor)；交易系统层 `EV`(Environment)/`CN`(Condition)/`SG`(Signal)/`ST`(Stoploss-Stopprofit)/`MM`(MoneyManager)/`PG`(ProfitGoal)/`SP`(Slippage)；另有 `TM`(TradeManager)/`OB`(OrderBroker) 与数据层 `StockManager`/`KData`/`Query`。示例 `SYS_Simple(tm=..., sg=SG_Flex(...), mm=MM_FixedCount(1000))`。
- **解决什么问题**：策略研究时能「只替换一个部件、观察单一部件的影响」；也让策略公开/复用变成组装问题。这是**因子研究平台最需要的可实验性**。
- **移植前提**：需要部件接口（策略/风控/仓位/滑点各自抽象）+ 序列化以持久化部件。lquant 已有 `Registry[T]` + `@op` 注册表 + `backtest/adapter.py`，💭 同构度较高，属于「把已有注册机制上升为策略插槽」。
- **可移植性**：高（概念层）｜**移植成本**：中（需要重构策略层接口）｜**价值**：**高**
- 证据：[readme.zh.md 部件表](https://github.com/fasiondog/hikyuu/blob/master/readme.zh.md)、[trade_sys/](https://github.com/fasiondog/hikyuu/tree/master/hikyuu_cpp/hikyuu/trade_sys)、[serialization/](https://github.com/fasiondog/hikyuu/tree/master/hikyuu_cpp/hikyuu/serialization)

### #5 qteasy 的「按步数据窗口注入 + use_latest_data_cycle 显式开关」
- **机制是什么** 📄 + ✅：不做全时间轴一次性算指标，而是「数据提前打包装配 + 按步注入数据窗口」——策略在 `realize()` 内 `get_data()` 只能拿到该步对应的历史窗口；另设 `use_latest_data_cycle` 显式控制「是否使用交易当时的最新数据」。
- **解决什么问题**：**从机制上杜绝无意中的未来函数**（README 原文强调这是「机制保证」而非「需自行保证」）。同时 `use_latest_data_cycle` 直面了一个真实的模糊地带：日频回测时当天 bar 是否已生成——qteasy 把它做成可配置项而非隐式假设。
- **移植前提**：需要一个「按日推进 + 窗口视图」的策略调用协议。💭 lquant 的事件引擎可直接采用；向量化快扫则需退化为「只算到 t-1」的表达式约束（lquant 已有 `analyzer.py` 的 `min_window` + 未来函数检测，属于静态防线）。
- **可移植性**：高｜**移植成本**：中｜**价值**：**极高**（因子研究最怕未来函数，这是**机制层防御**而非文档承诺）
- 证据：[README「普通特性」第 3、4 条](https://github.com/shepherdpp/qteasy/blob/master/README.md)、[backtest.py](https://github.com/shepherdpp/qteasy/blob/master/qteasy/backtest.py)、[qt_operator.py](https://github.com/shepherdpp/qteasy/blob/master/qteasy/qt_operator.py)

### #6 pybroker 的「内建 walk-forward + bootstrap 置信区间 + FeeMode 抽象」
- **机制是什么** ✅：`WalkforwardWindow` 携带 train/test 行索引；`walkforward_split(df, windows, lookahead, train_size)` 切窗，`lookahead` 专门用于**防止训练数据泄漏到测试边界**；`Strategy.walkforward(...)` 直接跑完整 walk-forward 回测；`calc_bootstrap` + `bootstrap_samples=10_000` 给出收益指标的置信区间；`FeeMode` ∈ {ORDER_PERCENT, PER_ORDER, PER_SHARE, Callable, None} 抽象异构费率。
- **解决什么问题**：①「只有全样本回测、没有样本外验证」→ 过拟合；②「只报点估计、不知显著性」→ 无法判断策略是否只是噪声；③「费率结构异构」→ 每家券商一套硬编码。
- **移植前提**：需要 (a) 数据能按 symbol×date 展平成主表并保留行索引；(b) 模型训练接口；(c) `FeeInfo` 值对象。lquant 用 DuckDB/Polars，💭 主表展平是同构的，属于中等改动。
- **可移植性**：高（纯 Python）｜**移植成本**：中｜**价值**：**高**（因子研究→策略回测的必需闭环）
- 证据：[strategy.py](https://github.com/edtechre/pybroker/blob/master/src/pybroker/strategy.py)、[config.py](https://github.com/edtechre/pybroker/blob/master/src/pybroker/config.py)、[slippage.py](https://github.com/edtechre/pybroker/blob/master/src/pybroker/slippage.py)

### #7 vn.py 的「EventEngine + BaseGateway」抽象
- **机制是什么** ✅：`EventEngine` 用一个 `Queue` + 两个线程（主循环 `_run` / 定时器 `_run_timer`，每秒发 `EVENT_TIMER`）做事件分发，`register(type, handler)` 泛化订阅；`BaseGateway(ABC)` 把「行情订阅 + 下单撤单 + 查询 + 8 个 on_* 回调」固化为标准接口，回调内部统一转成事件入队。
- **解决什么问题**：新增一家券商/行情源＝实现一个 gateway 类，**上层策略与风控零改动**；回测/实盘可共用同一套事件与数据结构。
- **移植前提**：需要事件循环 + 数据对象（Tick/Trade/Order/Position/Account）统一定义。
- **可移植性**：高（纯 Python，无重依赖）｜**移植成本**：低-中｜**价值**：**中-高**（lquant 的 L5 已有 FastAPI + RQ + WebSocket；💭 gateway 抽象对「未来接实盘通道/QMT」是必要预留，对当前看板阶段价值中等）
- 证据：[vnpy/event/engine.py](https://github.com/vnpy/vnpy/blob/master/vnpy/event/engine.py)、[vnpy/trader/gateway.py](https://github.com/vnpy/vnpy/blob/master/vnpy/trader/gateway.py)
- 💭 提醒：vn.py 主仓库**不含 A 股制度规则**，只借架构，不借规则。

### #8 WonderTrader 的「目标仓位合并执行 + 离合器 + 四层风控」
- **机制是什么** 📄：① 多策略/多标的先**合并成目标仓位**再统一执行（README 原文：避免自成交、降低保证金与佣金开销）；② **离合器**：信号与执行分离，风险时只断开信号执行、保留策略计算以便继续观察；③ 四层风控（组合盘虚拟资金 / 通道流量合规 / 账户资金 / 离合器）；④ `M+1+N` 架构用「组合盘 + 手数放大倍数」服务多账户。
- **解决什么问题**：① 多策略同标的的自成交与重复委托；② 风控熔断后策略「变盲」，无法与理论对照；③ 一套策略组合服务 N 个不同风险偏好账户。
- **移植前提**：需要一个「信号 → 目标仓位 → 执行」的解耦层 + 风险开关。
- **可移植性**：中（原实现是 C++，仅能借鉴设计）；💭 但①②③都是**纯概念**，Python 侧重写成本不高｜**移植成本**：中-高｜**价值**：**中-高**（lquant 当前聚焦日频回测+看板，多账户/多通道是后置需求；但「目标仓位合并」在模拟盘上马上有用）
- 证据：[README（策略组合统一管理 / 离合器机制 / 风控四层）](https://github.com/wondertrader/wondertrader/blob/master/README.md)、[src/WtCore/WtEngine.h](https://github.com/wondertrader/wondertrader/blob/master/src/WtCore/WtEngine.h)

### 次级借鉴（未进 Top 8 但值得记录）

| 来源 | 机制 | 价值判断 |
|---|---|---|
| vectorbt | 把参数网格编码为数组新维度、一次广播跑完；`combined_fns`/`reshape_fns` 统一广播语义 | 💭 **只借思想不引依赖**：A 股 T+1/涨跌停是路径依赖，纯向量化无法正确表达，仅可用于**筛参**——与 lquant 已有结论一致 |
| backtrader | `Cheat-on-Close` / `Cheat-on-Open` 把「必然引入未来函数的行为」做成**显式开关** | 高（低成本、高收益的 API 设计哲学） |
| zipline-reloaded | `Pipeline` 横截面因子引擎（`pipeline/engine.py`）；bundle 数据打包 | 中（lquant 已有 Polars 因子 DSL，语义层参考） |
| bt | 组合回测的 4 类可组合 algos + 策略树嵌套；依赖 `ffn` 出图 | **高**（lquant L4 组合层的直接参照） |
| Abupy | 度量评分体系（多指标加权打分选参）；「A 股涨跌停的特殊处理」教程 | 中-高（评分体系可借；源码结构 ⚠️ 未核实） |
| QUANTAXIS | QIFI 账户协议跨语言一致；`marketpreset` 市场预制类；任务调度/分布式 agent | 中（YAML 数据化优于类继承；分布式是后置需求） |
| backtesting.py | 极简 API + 结果自带交互图 | 低（AGPL 许可劝退） |
| Hikyuu | C++ 核心 + Python 绑定 + 核心库可独立剥离 | 中（lquant 已有 Rust 算子插件路径，同构） |

---

## 4. A 股规则正确性横向对照表

图例：✅ 已核实内置 ／ ⚠️ 未核实 ／ ❌ 无内置证据

| 项目 | 涨跌停 | T+1 | 印花税(历史区间) | 整手 | 最低佣金 | 复权/除权息 | 停牌 | 退市/幸存者偏差 |
|---|---|---|---|---|---|---|---|---|
| **Hikyuu** | ✅ `ISLIMITUP/DOWN`（ST 5% **明确未实现**） | ⚠️ | ✅ `TC_FixedA2015/2017` | ⚠️ | ✅ 5.0 元 | ✅ `ADJ_FACTOR`+`StockWeight` | ⚠️ | ⚠️ |
| **RQAlpha** | ✅ `PriceValidator`+`price_board` | ✅ `sellable`/`non_closable` | ⚠️（`sys_transaction_cost/deciders.py` 未读） | ⚠️ | ⚠️ | ✅ `adjust_type='pre'`+拆分分红+红利税 | ✅ `is_suspended` | ✅ `get_active_instrument`「is not listing」/`is_st_stock` |
| **qteasy** | ❌/⚠️ | ✅ 交割队列 | ⚠️ | ✅ `moq`=`trade_batch_size` | ✅ `cost_min_*` | ✅ `adj='b'` 等 | ⚠️ | ⚠️ |
| **vn.py** | ❌ | ❌ | ❌ | ❌ | ❌ | ⚠️ | ⚠️ | ❌ |
| **QUANTAXIS** | ⚠️ | ⚠️ | ⚠️ `marketpreset` 含手续费但未核实细则 | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ |
| **Abupy** | ✅ 文档级（教程专章） | ⚠️ | ⚠️（有手续费自定义教程） | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ |
| **pybroker** | ❌ | ❌ | ❌ | ❌ | ⚠️ 可用 `PER_ORDER`/Callable 表达 | ⚠️ 有 `adjust` 参数 | ❌ | ❌ |
| **zipline-reloaded** | ❌ | ❌ | ❌ | ❌ | ⚠️ 有 `commission.py` | ⚠️ 有 `adjust` 概念 | ⚠️（`handle_halts` 未核实） | ❌ |
| **backtrader** | ❌ | ❌ | ❌ | ❌ | ⚠️ 有 commission schemes | ⚠️ | ❌ | ❌ |
| **backtesting.py** | ❌ | ❌ | ❌ | ❌ | ❌ | ⚠️ | ❌ | ❌ |
| **bt** | ❌ | ❌ | ❌ | ❌ | ⚠️ 有 transaction cost models | ⚠️ | ❌ | ❌ |
| **vectorbt** | ❌（限价单属 PRO） | ❌ | ❌ | ❌ | ❌ | ⚠️ | ❌ | ❌ |
| **WonderTrader** | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ |

**结论** 💭：**只有 Hikyuu 与 RQAlpha 在 A 股制度层达到「可直接依赖」的完整度**；qteasy 在交割/费用上很强但价格约束类规则缺失；其余项目均需从零补。这恰好解释并支持 lquant 自研 `config/rules/cn_a_share.yaml` 的决策。

---

## 5. 已知坑 / 失败教训汇总（对 lquant 的直接启示）

| 坑类型 | 具体案例 | 证据 | 对 lquant 的启示 |
|---|---|---|---|
| **维护停滞** | backtrader 最后推送 2024-08-19；Abupy 2026-01；zipline-reloaded 2026-01 | ✅ API | 不引入停更/半停更项目做核心依赖（lquant 已决断，本次调研再次确认） |
| **许可雷区** | RQAlpha 非商业 + 明令禁止「引用或借鉴」；vectorbt / pybroker 的 Commons Clause；backtesting.py AGPL；backtrader / Abupy GPL-3.0 | ✅ 源码头部 / README | **只读思路，不复制代码结构，不引依赖**。Rust/Python 自研是唯一安全路线 |
| **依赖地狱** | zipline 3.0 升级 pandas≥2/SQLAlchemy>2「可能破坏现有代码」；2.4 升级 exchange_calendars 同样；backtrader 的 pyfolio 集成已 deprecated | ✅ README | 保持依赖轻量（lquant 的 Polars/DuckDB/Parquet 组合正是此策略的体现） |
| **重外部服务** | QUANTAXIS 以 MongoDB/ClickHouse 为核心存储 | ✅ README | lquant 的「零服务依赖兜底」（Redis 缺失降级线程、cargo 缺失降级纯 Python）是正确方向 |
| **重构破坏生态** | QUANTAXIS README 明写「移除 QAARP（不再维护老版本）」 | ✅ README | 对外契约要版本化；规则/费用等易变内容数据化（YAML）而非代码化 |
| **数据格式/生态锁定** | RQAlpha 的 bundle 与米筐数据生态绑定；zipline 的 bundle 需自建；qteasy 默认 Tushare 需 token+积分 | ✅/📄 | lquant 的 Provider 抽象 + Capability + Fallback 是更稳的设计 |
| **制度规则被忽略** | 本组 13 个项目中，11 个对 A 股涨跌停/T+1 无内置证据 | 本报告 §4 | **A 股制度正确性就是 lquant 的差异化护城河**，不要在选型时把它当"细节" |
| **闭源漏斗** | vectorbt 开源版缺失限价单/杠杆/并行/条件参数，均在 PRO | ✅ README | 评估开源项目时要看「关键能力是否在付费墙后」 |
| **性能宣称需验证** | WonderTrader 宣称 UFT <175ns、HFT 1-2μs；Hikyuu 宣称 166ms/1913 万 K 线 | 📄 README | 厂商/作者基准需自测复现；lquant 已有 `BACKTEST_VALIDATION_BENCHMARKS.md` 的正确做法 |
| **生态碎片化** | vn.py 主仓库不含策略与回测，全在 `vnpy_*` 独立包 | 💭 推断 + 🔍 目录 | 单仓薄核心 + 扩展包是可选路线，但要保证「开箱即用」的引导 |

---

## 6. 证据链接总表

| # | 项目 | 证据类型 | 链接 |
|---|---|---|---|
| 1 | Hikyuu | 仓库/README（部件化架构表） | https://github.com/fasiondog/hikyuu/blob/master/readme.zh.md |
| 2 | Hikyuu | 源码：A 股费用（2017 版） | https://github.com/fasiondog/hikyuu/blob/master/hikyuu_cpp/hikyuu/trade_manage/imp/FixedA2017TradeCost.cpp |
| 3 | Hikyuu | 源码：A 股费用（2015 版） | https://github.com/fasiondog/hikyuu/blob/master/hikyuu_cpp/hikyuu/trade_manage/imp/FixedA2015TradeCost.cpp |
| 4 | Hikyuu | 源码：涨跌停判定（含 ST 未实现注释） | https://github.com/fasiondog/hikyuu/blob/master/hikyuu_cpp/hikyuu/indicator/imp/IIsLimitUp.cpp |
| 5 | Hikyuu | 目录：trade_sys / trade_manage / serialization | https://github.com/fasiondog/hikyuu/tree/master/hikyuu_cpp/hikyuu |
| 6 | RQAlpha | 源码：涨跌停事前风控 | https://github.com/ricequant/rqalpha/blob/master/rqalpha/mod/rqalpha_mod_sys_risk/validators/price_validator.py |
| 7 | RQAlpha | 源码：停牌/退市校验 | https://github.com/ricequant/rqalpha/blob/master/rqalpha/mod/rqalpha_mod_sys_risk/validators/is_trading_validator.py |
| 8 | RQAlpha | 源码：T+1 持仓（sellable/non_closable/分红/拆分/红利税） | https://github.com/ricequant/rqalpha/blob/master/rqalpha/mod/rqalpha_mod_sys_accounts/position_model.py |
| 9 | RQAlpha | 源码：PIT 数据门面（is_suspended/is_st_stock/get_dividend/adjust_type） | https://github.com/ricequant/rqalpha/blob/master/rqalpha/data/data_proxy.py |
| 10 | RQAlpha | 源码：撮合（成交量限制/无量撤单） | https://github.com/ricequant/rqalpha/blob/master/rqalpha/mod/rqalpha_mod_sys_simulation/matcher/bar_matcher.py |
| 11 | RQAlpha | 路径：红利税模块 | https://github.com/ricequant/rqalpha/blob/master/rqalpha/portfolio/capital_gains_tax.py |
| 12 | qteasy | 仓库（新地址） | https://github.com/shepherdpp/qteasy |
| 13 | qteasy | 源码：交易规则参数解析（费率/MOQ/交割期） | https://github.com/shepherdpp/qteasy/blob/master/qteasy/config_parser.py |
| 14 | qteasy | 源码：回测引擎与交割队列 | https://github.com/shepherdpp/qteasy/blob/master/qteasy/backtest.py |
| 15 | qteasy | 源码：Operator 组装与 use_latest_data_cycle | https://github.com/shepherdpp/qteasy/blob/master/qteasy/qt_operator.py |
| 16 | vn.py | 源码：事件引擎 | https://github.com/vnpy/vnpy/blob/master/vnpy/event/engine.py |
| 17 | vn.py | 源码：网关抽象 BaseGateway | https://github.com/vnpy/vnpy/blob/master/vnpy/trader/gateway.py |
| 18 | pybroker | 源码：walkforward / walkforward_split | https://github.com/edtechre/pybroker/blob/master/src/pybroker/strategy.py |
| 19 | pybroker | 源码：StrategyConfig 与 FeeMode | https://github.com/edtechre/pybroker/blob/master/src/pybroker/config.py |
| 20 | pybroker | 源码：滑点模型 | https://github.com/edtechre/pybroker/blob/master/src/pybroker/slippage.py |
| 21 | WonderTrader | README（多引擎/风控/离合器/M+1+N） | https://github.com/wondertrader/wondertrader/blob/master/README.md |
| 22 | WonderTrader | 路径：WtEngine.h / TraderAdapter.h | https://github.com/wondertrader/wondertrader/tree/master/src/WtCore |
| 23 | QUANTAXIS | README（QIFI/QAMarket/QAEngine/QAWebServer/QASchedule） | https://github.com/yutiansut/QUANTAXIS/blob/master/README.md |
| 24 | QUANTAXIS | 路径：market_preset.py | https://github.com/yutiansut/QUANTAXIS/blob/master/QUANTAXIS/QAMarket/market_preset.py |
| 25 | Abupy | README（A 股涨跌停特殊处理 / 滑点手续费 / 度量评分） | https://github.com/bbfamily/abu/blob/master/readme.md |
| 26 | zipline-reloaded | README（Quantopian 关停 / 破坏性升级） | https://github.com/stefan-jansen/zipline-reloaded/blob/main/README.md |
| 27 | zipline-reloaded | 路径：Pipeline 引擎 / commission / bundles | https://github.com/stefan-jansen/zipline-reloaded/tree/main/src/zipline |
| 28 | backtrader | README（特性列表 / pyfolio deprecated） | https://github.com/mementum/backtrader/blob/master/README.rst |
| 29 | backtrader | 路径：cerebro.py / bbroker.py / linebuffer.py | https://github.com/mementum/backtrader/tree/master/backtrader |
| 30 | backtesting.py | 路径：backtesting.py / lib.py | https://github.com/kernc/backtesting.py/tree/master/backtesting |
| 31 | bt | README（algos 组合 / 策略树 / 成本模型 / ffn） | https://github.com/pmorissette/bt/blob/master/README.md |
| 32 | vectorbt | README（向量化参数扫描 / PRO 边界 / Commons Clause） | https://github.com/polakowo/vectorbt/blob/master/README.md |
| 33 | vectorbt | 路径：portfolio / generic / indicators | https://github.com/polakowo/vectorbt/tree/master/vectorbt |
| 34 | 全部 | GitHub API 元数据（star/pushed_at/license） | `https://api.github.com/repos/{owner}/{repo}` |

---

## 7. 未核实清单（引用红线）

以下内容**我没有取得可验证证据**，任何下游文档不得把它们当作事实陈述：

1. **Hikyuu**：T+1 是否内置、整手是否强制校验、停牌/退市/幸存者偏差处理。
2. **RQAlpha**：印花税具体费率与历史区间表（`rqalpha_mod_sys_transaction_cost/deciders.py` 未读取）；整手/最小 100 股的强制点。
3. **qteasy**：涨跌停与停牌是否有处理（倾向于无，但未核实）；印花税历史区间表；`trade_batch_size` 等默认配置的具体数值（只在 README 示例中见 `100`）；star 数（⚠️ 搜索结果 158，与认知不符）。
4. **QUANTAXIS**：涨跌停/T+1/印花税/整手/复权/停牌的实现位置与是否存在；`OrderGateway` 风控内容。
5. **Abupy**：源码目录结构与类名（`abupy/TradeBu/*`、`abupy/UtilBu/*` 探测均为 404）；涨跌停特殊处理的实现方式；依赖版本与 Python 兼容性。
6. **zipline-reloaded**：`handle_halts` 等停牌机制的确切类/方法名；`src/zipline/finance/execution.py`（探测返回 000，网络问题而非 404，需重试）。
7. **backtrader**：佣金类的实际路径（`backtrader/commissions/comminfo.py` 为 404）；社区 fork（backtrader2）的活跃度。
8. **backtesting.py / bt / vectorbt**：具体类名与方法名（仅确认文件路径存在，未逐行阅读）；vectorbt 的 `param_product` / `from_signals` API 名**未证实**（README 未出现，请勿引用）。
9. **WonderTrader**：A 股制度规则的配置文件与参数项；`WtBtEngine`、`WtRiskMonitor`、`WtMonSvr` 的确切类名与路径（探测 404）。

---

## 8. 调研结论（对 lquant 的三条行动建议）

1. **A 股制度正确性是护城河，且已被证明稀缺** —— 13 个项目中仅 Hikyuu（部分）与 RQAlpha（最全但许可受限）达到可依赖的 A 股规则完整度。lquant 的 `config/rules/cn_a_share.yaml` 数据化规则表路线正确，应继续加固而非转向引入外部引擎。
2. **立即值得吸收的三个低成本高收益机制**：① qteasy 的**交割队列**（T+N 通用化）；② RQAlpha 的**校验器链 + 可解释拒单原因**（独立实现，不复制代码）；③ Hikyuu 的**费用版本化**（按生效区间数据化）。三者都能以 YAML/规则表 + 轻量状态机实现，与 lquant 现有 `registry` / `rules` 架构同构。
3. **因子与验证闭环补两块**：① pybroker 的**内建 walk-forward + bootstrap 置信区间**（解决过拟合与显著性判断）；② bt 的**可组合 algos + 策略树**（强化 L4 组合层的可表达性）。两者均为纯 Python 语义，无重依赖，与 lquant 技术栈同构。

**不建议**：引入 backtrader（停更）、zipline-reloaded（依赖地狱 + 生态已死）、Abupy（老化 + GPL）、backtesting.py（AGPL）、vectorbt/vectorbt PRO（Commons Clause + 限价单在付费墙后 + 向量化无法表达 A 股路径依赖）、RQAlpha（许可明令禁止借鉴）。WonderTrader 可作架构参照（C++ 异构，不宜移植代码）。
