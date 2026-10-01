# FinancialTool 能力移植说明（指标层 / 退出策略 / 基本面分位）

本文记录从 [ICMoon527/FinancialTool](https://github.com/ICMoon527/FinancialTool)（HEAD `86b80c8`）
移植到 lquant 的三块能力：**做了什么、为什么这么做、修正了原实现的哪些缺陷**。

对照审计见 `FinancialTool-vs-lquant-对比与借鉴.md`（v2 全量代码审计版）。

---

## 0. 移植原则

1. **只取设计意图，不搬代码。** FinancialTool 是 pandas + 大写列名 + 逐票循环、
   无类型标注、无 CI；lquant 是 Polars + 小写列名 + `ruff`(line-length=100)
   + 改动行覆盖率门禁。直接搬运会同时引入风格违规和隐藏 bug。
2. **每个模块都必须过 lquant 既有范式。** 新增能力一律走
   `core/registry.Registry` + 装饰器注册 + yaml/DB 配置，与
   `factors/ops`、`backtest/strategy`、`data/providers` 同构，前端可自动枚举。
3. **原实现的每个缺陷都要变成一条测试。** 见下文的「反向回归」小节 ——
   移植的动力不是「它有这个功能」，而是「我们知道它在这里踩过坑」。

---

## 1. 指标层 `src/lquant/indicators/`

### 1.1 为什么需要

lquant 原来只有 `factors/indicators.py` 一个 85 行的文件（MA/EMA/MACD/RSI/BOLL），
**无注册表、无元数据、不进因子引擎**，唯一消费者是 `/api/data/indicators` 的图表叠加。
FinancialTool 有 174 个指标文件 / 20675 行，其中一批（筹码分布、庄家控盘、
主力吸筹派发、CYW、量能突增）是 A 股实战常用的资金/形态类信号，lquant 完全没有。

### 1.2 分层决策：指标**不进**因子 DSL

`factors/dsl/analyzer.py` 是逐节点表达式树，所有算子必须是**可静态推导
`min_window` 的窗口化纯函数**。缠论的「笔/线段/中枢」是递归分段结构，
波浪是层次化标注 —— AST 里根本没有「分段」这个类型，加再多 `Ts_*` 算子也表达不了。

所以：**新建 `indicators/` 层，指标以「预计算信号列」的形式反哺因子层**，
而不是让指标去实现 DSL 算子。

> 顺带澄清：FinancialTool 的「缠论 / 波浪」**只是两个 YAML 提示词模板**，
> 没有任何算法实现（全仓 grep「缠」仅 3 处命中，都在 prompt 文案里）。
> 本层没有移植它们，因为无物可移。

### 1.3 关键资产：未来函数检测（`future.py`）

这是本次移植**最有价值**的一块，而不是任何一个具体指标。

判据 —— **前缀不变性**：

> 对任意 `t`，只用 `df[:t+1]` 算出的 `t` 时刻指标值，
> 必须与用完整 `df` 算出的 `t` 时刻值**完全一致**。

```python
from lquant.indicators import assert_no_lookahead
assert_no_lookahead(add_my_indicator, df, out_cols=["my_ind"])
```

**为什么需要它**：FinancialTool 里同一个天道 XMA 有两份实现 ——

| 实现 | 位置 | 窗口 | 结果 |
|---|---|---|---|
| `_xma_numba` | `indicators/indicators/tiandao.py:20` | 居中 `[i−h, i+h−ε]` | ❌ 用未来数据（docstring 自己承认「尾部会漂移」） |
| `_compute_xma_truncated` | `api/v1/endpoints/intraday.py:1170` | 右对齐 `[i−h, i]` | ✅ 无泄露 |

于是**日线选股/回测走的是泄漏版本，只有盘中链路是对的**。

lquant 的 `indicators/tiandao.py` 只实现右对齐截断版，并且
`tests/unit/test_indicators_future.py` 里放了**反向对照**：把居中版本原样
实现一遍，断言检测器**必须**抓住它（实测抓出 52 处不一致）。
没有这个反向对照，检测器就只是个摆设。

### 1.4 单一实现源

`factors/indicators.py` 被改写为**纯转发层**（保留原导入路径与函数签名，
`add_ma is lquant.indicators.trend.add_ma` 有测试断言）。
「同一个指标两份实现」的事故在结构上被消除。

### 1.5 已实现指标

| 注册名 | 说明 | 类别 | min_window |
|---|---|---|---|
| `ma` / `ema` | 均线族 | trend | 60 / 26 |
| `macd` | 国内口径 `HIST = 2×(DIF−DEA)` | trend | 60 |
| `boll` | 布林带 | channel | 40 |
| `bbi` | 四均线等权 | trend | 24 |
| `rsi` | Wilder | oscillator | 40 |
| `kdj` | 国内口径，无波动窗口 RSV 置 50 | oscillator | 30 |
| `volume_ratio` | 量比，**分母不含当日** | volume | 20 |
| `volume_surge` | 量能突增 | volume | 25 |
| `turnover_ma` | 换手率均线（缺列原样返回） | volume | 5 |
| `tiandao` | 金牛/金钻通道，**右对齐截断 XMA** | channel | 60 |

`volume_ratio` 的 `shift(1)` 是个易错点：均量窗口若含当日，放量当天会把
分母一起抬高、自己稀释掉信号 —— 已按正确口径实现并加了测试。

---

## 2. 退出策略层 `src/lquant/backtest/exit/`

### 2.1 为什么需要

lquant 的 `Strategy.on_bar()` 只负责「输出目标权重」，**没有任何退出抽象**，
止盈止损散在策略内部。后果是：想做「同一套选股信号 × 不同退出规则」的
正交实验，就得给每个策略复制一份退出逻辑。

### 2.2 与既有引擎的对接（不改引擎一行代码）

`Engine._schedule_rebalance` 的契约是：

- 返回 `[]` → **无操作，保留持仓**；
- 返回非空列表 → 列表外的持仓**全部清掉**；
- `(sym, 0.0)` → 显式清仓该标的。

叠加层 `ExitOverlay` 包住任意 `Strategy`，必须处理一个很容易踩的坑：

> 内层返回 `[]`（无信号）时，若叠加层**只为被退出的标的**返回权重，
> 其余持仓会被引擎当成「不在目标里」**全部清掉**。

正确做法：一旦有退出信号，就把**当前完整组合**物化出来再打折；
没有退出信号时原样透传 `[]`。

另一个坑（开发时实测踩到）：**全量退出不能靠「从权重里删掉」来表达**，
因为 `[]` 会被引擎当成无操作，清仓被静默吞掉。必须显式返回 `(sym, 0.0)`。
`tests/unit/test_exit_overlay.py::test_full_exit_actually_sells` 是这条的回归。

### 2.3 三个内置实现

**`simple`** — 固定止损 / 固定止盈 / 时间止损
- `stop_mode="close"`（默认，收盘破位才卖，过滤盘中假摔）
  或 `"intraday"`（最低价破位；跳空低开按开盘价，否则按破位价）
- 时间止损带 `time_stop_min_return`：持股到期但仍在赚钱不强制离场

**`tiered`** — 分级移动止盈（移植自 `TieredExitStrategy`）

按**峰值曾达到的最大盈利**决定可容忍回撤（阈值来自原实现的实盘调参，保留）：

| 峰值盈利 | 容忍回撤 |
|---|---|
| ≥ 50% | 5% |
| ≥ 30% | 8% |
| 其余 | 10% |

止盈线 = `峰值 × (1 − 容忍回撤)`，**只上移不下移**（棘轮）。
止损默认 12%，时间止损默认「持股 ≥20 交易日且收益 <5%」。

**`pressure`** — 通道压力位分批止盈（移植自 `TiandaoPressureExitStrategy`）

- A 档：首次 `high ≥ 压力位` → 卖 `1/3`
- B 档：A 档后 `close < 压力位` → 再卖剩余 `1/2`
- C 档：移动止盈/止损 → 全清
- 优先级：**C > A/B > 时间止损**
- 压力位取 `REF(金牛, 1)`（上一根的通道上轨）—— 用当日金牛等于同时用结果和原因

### 2.4 修正了原实现的两处缺陷

| 缺陷 | 原实现 | 本实现 |
|---|---|---|
| **open 阶段空转** | `TieredExitStrategy` 在 `open` 阶段不做任何检查，跳空低开当天不执行止损，等收盘才卖，等于把缺口全额吃下 | 每个 bar 都检查，跳空低开同样命中（有测试） |
| **跌停日无条件卖出** | 收盘跌破止损就生成卖单，一字跌停根本卖不出去，回测会以不可能的价格成交 | `is_sealed_limit_down()` 挡掉一字跌停；一字**涨停**不算封死（有涨停买盘，能卖） |

### 2.5 端到端效果

同一段「20 天 +6% 后 5 天 −5.5%」行情，同一套买入信号：

| 退出策略 | 总收益 | 最大回撤 |
|---|---|---|
| 无退出规则 | 115.15% | **−23.77%** |
| `tiered` | 140.89% | **−14.65%** |

（`tests/unit/test_exit_overlay.py::test_tiered_improves_drawdown_over_plain_hold` 断言了这个关系。）

---

## 3. 基本面分位层 `src/lquant/fundamental/`

### 3.1 为什么需要

lquant 的因子体系做的是**横截面排序**（`Rank`/`ZScore`），
**没有「行业内相对位置」这一层**。而 A 股基本面比率跨行业绝对不可比：
毛利率 60% 对白酒是常态、对商贸零售是异常；存货 200 天对地产正常、对生鲜是灾难。

### 3.2 与 FinancialTool 的三点关键差异

| 维度 | FinancialTool | 本实现 |
|---|---|---|
| **PIT** | 同花顺路径不带公告日，只按报告期取最新一期 → **前视偏差**；tushare 回退请求了 `ann_date` 但**从不消费** | 所有解析强制 `pub_date <= asof`，**无公告日的行永不返回**（`panel.resolve_pit`） |
| **分位口径** | 自称「过去 5 年历史分布」，实际是**单一报告日的横截面**且日期硬编码 `date="20251231"` | 观察日由调用方传入，在调仓日循环调用即得**滚动历史分位**（`score.score_history`） |
| **缺失处理** | 分位缺失时走「无分位给 50% 基础分」，**静默给分** | 样本不足 `min_samples` 不出分位；聚合结果同时给出 `coverage`，**必须和总分一起看** |

### 3.3 权重自洽（修正 95≠100）

FinancialTool 声明「盈利与现金流」模块权重 30，但子项合计只有 25，
五个模块上限合计 **95 而非 100** —— 于是「优秀 ≥85」档位几乎不可达。

本实现的 `metrics.validate_modules()` 在**导入时**强制：

- 模块权重合计 == 100
- 每个模块内子项满分之和 == 模块权重
- 指标不得引用未声明的模块

不自洽直接 `ValueError`，把这类漂移变成「导入即失败」。

### 3.4 评分口径

模块（合计 100）：盈利能力 25 / 现金质量 20 / 营运效率 15 / 偿债能力 20 / 估值 20。

分位档位（17 个指标，含 `higher_better` 方向）：

| 条件 | 系数 |
|---|---|
| 正向指标 ≥ P75（反向 ≤ P25） | 1.0 |
| ≥ P50 | 0.8 |
| ≥ P25 | 0.5 |
| 其余 | 0.2 |

聚合同时给出：

- `raw_score` —— 实际挣到的分（缺指标就少拿分）
- `available_max` —— 已评分指标的满分之和
- `normalized_score = raw / available_max × 100` —— **跨覆盖度可比**
- `coverage` —— 已评分 / 全部指标

### 3.5 三表勾稽 `reconcile.py`

用报表之间的**内部一致性**发现数据源错误与财务异常：

| 口径 | 公式 |
|---|---|
| 留存收益勾稽 | `\|NI + OCI − ΔRE\| / \|NI\|` |
| 现金变动勾稽 | `\|CF净变动 − 货币资金变动\| / max(\|·\|)` |
| 利润质量 | `\|NI − 扣非净利润\| / \|NI\|` |

阈值 → 系数：`<5% → 1.0`、`<15% → 0.8`、`<30% → 0.4`、其余 0。

**修正**：原实现在 `NI is None` 时直接做除法会抛 `TypeError`；
本实现所有入口对缺失输入返回 `None`，记为「未检查」——
`checked_items` 为空时 `passed=False`（一项都没查到不能算通过，
否则数据缺失会被当成质量优秀），有测试固定这个语义。

### 3.6 数据接入

直接消费 lquant 既有的两张 PIT 表，**不需要新建 schema**：

- `financial_pit(symbol, stat_date, pub_date, report_type, item, value, unit, source)`
- `industry_classify(symbol, std, code, name, std_date, source)` —— `std_date`（生效日）
  是该列设计的初衷（「防止用今天的分类回测十年前」），本模块是它的强制消费点。

---

## 4. 验证

| 项 | 命令 | 结果 |
|---|---|---|
| Lint | `ruff check src tests` | 全仓 All checks passed |
| 单测（全量） | `pytest tests -m "not slow"` | **2666 passed, 1 skipped, 0 failed**（exit 0） |
| 新增用例 | 6 个文件 | **101 个用例全过** |
| 新增模块覆盖率 | `--cov=lquant.indicators,fundamental,backtest.exit` | **97.53%**（门禁 95%） |
| 未来函数 | 反向对照用例 | 居中 XMA 被抓出 **52 处**；截断版 **0 处** |

> 注意：全量测试**必须串行跑**。并行跑多个 pytest 会因 DuckDB/临时库争用产生
> 大量伪失败（实测出现过 `test_paper_live` / `test_api_data_tasks` 的 ERROR）。
> 单独串行运行时全绿。

新增测试文件：

- `tests/unit/test_indicators_future.py` —— 检测器 + **反向对照**
- `tests/unit/test_indicators_registry.py` —— 注册表、数值正确性、转发层
- `tests/unit/test_exit_strategies.py` —— 三个实现 + 信号模型
- `tests/unit/test_exit_overlay.py` —— 与真实 `Engine` 的契约 + 端到端对比
- `tests/unit/test_fundamental_percentile.py` —— PIT + 分位 + 聚合 + 滚动
- `tests/unit/test_fundamental_reconcile.py` —— 勾稽口径 + 缺失值安全

---

## 5. 明确没做的事（留给后续）

1. **API / 前端接线**：三个模块目前是纯库 + 测试，尚未挂到 `/api/*`。
   注册表已提供 `describe()`，接线时前端可直接枚举。
2. **缠论 / 波浪**：FinancialTool 没有实现，无物可移；本层也没有实现。
3. **筹码分布**：FinancialTool 的版本是**换手率近似**（非真 tick）。
   当前数据湖无 tick 数据，若要移植必须先解决数据源并**显式标注近似口径**。
4. **其余约 150 个指标**：同质实现（4 份 Bollinger）与玄学命名混在一起，
   应按需逐个评估后接入，不做批量搬运。
5. **RL 模块**：本次未做（P0-5）。接入方案见对比报告第 5 节。
