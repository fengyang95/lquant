# 回测引擎选型评估（2026-09-08）

## 结论（TL;DR）

**自研事件引擎保留为唯一「执行真源」；参数扫描走 Polars 向量化快扫（B5 已落地，近似、只筛参数，须回事件引擎复核）；外部引擎通过适配器协议接入，结果与原生引擎同构、可直接 /compare。**

不引入 backtrader / zipline / qlib backtest 作为核心执行器。

## 对比

| 候选 | 优势 | 劣势（对本项目） | 判定 |
|---|---|---|---|
| **自研 Engine**（现状） | A 股规则全内置：T+1、涨跌停拒单（分板 10%/20%）、印花税区间、手数、ETF 佣金差异；与撮合/模拟盘共享同一套 ruleset；测试覆盖 | 参数扫描慢（事件循环逐日） | ✅ 执行真源 |
| **vectorbt** | 向量化极快（比事件驱动快 2~3 个量级），适合万级参数扫描 | Numba 重二进制依赖；A 股 T+1/涨跌停/手数要全部重建，两套规则容易漂移；PRO 版闭源 | ⚠ 只借思想不引依赖：用 Polars 自研向量化快扫（近似成本模型，结果仅供**筛参数**，必须回原生引擎复核） |
| **backtrader** | 事件驱动、A股兼容案例多 | 原作者停更（业界评 T4「新项目不应选」）；引入后是第二套撮合规则，与自研引擎重复 | ❌ |
| **qlib backtest** | 与 Alpha158 研究生态一致 | pyqlib 安装重（沙箱受限）；其 executor 复杂且文档偏少；数据层与我们的湖不同形 | ❌（因子层已借 Alpha158，撮合不借） |
| **RQAlpha（米筐）** | A 股特化最完善（T+1/停牌/分级基金），持续维护 | 商业许可限制； again 引入=第二套撮合 | 👀 观望：若未来要交叉验证撮合精度，经适配器接入做对照 |

## 依据的业界共识

「向量化筛选 → 事件驱动验证 → 实盘」的梯度流水线（vectorbt 筛信号、事件引擎核执行真度）。
我们缺的只是第一环的速度，且第一环的输出**不该直接当作回测结论**——所以用 Polars 自研快扫
（零新依赖、与湖同构）即可，无需引 vectorbt。

## 预留接口：BacktestAdapter

外部引擎若接入（RQAlpha 对照、未来 vectorbt PRO），实现 `backtest/adapter.py` 的协议即可：

```python
from lquant.backtest.adapter import register_adapter

class RQAlphaAdapter:            # 示例
    name = "rqalpha"
    def run(self, df: pl.DataFrame, **params) -> AdapterOutput: ...

register_adapter(RQAlphaAdapter)
```

`AdapterOutput` 与原生 `Engine.run()` 的输出同形（nav 序列 / trades DataFrame / 指标），
落同一批 `backtest_run/backtest_nav/backtest_order` 表，`/api/backtests/compare` 天然可比——
引擎可换，研究资产不换。

## 行动项

- [x] 适配器协议 + 注册表（backtest/adapter.py，本轮落地）
- [x] B5：Polars 向量化参数扫描（`/api/backtests/sweep`，TopN 网格 + 近似成本）
      —— **已落地**：`backtest/sweep.py::run_sweep_vectorized` 一次成型地对整个
      网格出结果（排名 cum_sum 覆盖所有 top_n 档位，不对 values 做逐档事件循环）；
      端点在 `SweepIn.engine` 上提供 `event|vector|auto`（auto 按档数阈值
      `VECTOR_AUTO_MIN_POINTS=12` 自动切）。详见下方「B5：向量化参数扫描」。
- [ ] 如需撮合交叉验证：RQAlphaAdapter（pip 依赖放开后）

## B5：向量化参数扫描（`backtest/sweep.py`）

两条路径输出**同一张网格契约**（`_OUT_COLS`：value / total_return / annual_return /
sharpe / max_drawdown / n_trades / turnover），前端无需区分；差别在**语义权威性**：

| 路径 | 函数 | 定位 |
|---|---|---|
| 事件引擎 | `run_sweep`（逐档 `Engine`） | **唯一执行真源**：T+1 可卖、涨跌停/停牌/退市拒单、手数取整、资金不足拒单、除权复权全真实 |
| 向量化快扫 | `run_sweep_vectorized`（Polars） | **近似，只筛参数**：万级网格先粗筛，候选回事件引擎复核 |
| 选路 | `run_sweep_auto(engine=event\|vector\|auto)` | `auto`：档数 ≥ `VECTOR_AUTO_MIN_POINTS`(12) 走向量化，否则走事件引擎 |

**向量化的策略语义**：每个调仓日按因子降序排名取前 `top_n` 等权；T 日收盘定信号、
T+1 **开盘**建仓（防未来函数），持有到下一个调仓日开盘，区间内买入持有、换仓时恢复等权。
收益用开盘价比值 `open(T+1)/open(建仓日)`，与引擎「T+1 开盘成交、收盘估值」在无隔夜
跳空时严格等价。效率关键：排名 `cum_sum` 一次覆盖**所有** top_n 档位（topN 集合嵌套），
不做逐档事件循环，也不做逐日 Python 循环。

**近似成本模型**（`_approx_cost_rates`，全部按成交额线性化）：
- 佣金 + 过户费 + `pct` 滑点 = 买入单边率；再加印花税（取回测末日税率档）= 卖出单边率；
- 每个换仓日按换手 `turn = (top_n − 名单重叠数) / top_n`，扣
  `turn × 买入率 + turn × 卖出率`；首次建仓只扣买入，不假设期末清仓；
- 未建模：最低佣金（5 元/单，小额单被低估）、`tick`/`volume_pct` 滑点（按 0）、
  等权漂移再平衡产生的日常小额委托、T+1 可卖、涨跌停/停牌/退市、手数取整、
  participation 成交量上限、资金不足拒单、除权复权、并列因子的稳定排序。

**screen → verify 工作流**（务必遵守）：
1. `POST /api/backtests/sweep` 用大网格 + `engine=vector`（或 `auto`）粗筛；
2. 取收益/夏普头部若干档（以及邻域），改成 `engine=event` 重新扫，得到可对外引用的结论；
3. 只有事件引擎的数字可以进报告 / 实盘决策；向量化数字仅用于缩小搜索空间。

**正确性闸**：`tests/unit/test_sweep.py::test_sweep_vectorized_ranking_matches_event_engine`
在无隔夜跳空 + 持续信号的合成面板上同时跑两条路径，断言 total_return 秩相关 ≥ 0.9、
argmax 一致、绝对差 ≤ 2pp（实测秩相关 1.0、绝对差 < 1pp；残差来自上面的近似成本项）。
高换手 / 大幅隔夜跳空的数据上近似会变差（资金不足拒单等路径依赖行为不可向量化），
这正是「必须回事件引擎复核」的原因。

**实测速度**（120 标的 × 500 交易日、60k 行、monthly）：
事件引擎约 **528 ms/档**，向量化约 **7.2 ms/档**（20 档快 ~73×）；
10,000 档向量化 **2.7 s**，按事件引擎单档耗时外推需 **~88 分钟**（约 2000×）。

## 本轮（2026-10-02）成本与撮合口径已修正项

这些是回测结果可信度的直接前提，改动均已单测锁定（见
`tests/unit/test_backtest_defect_fixes.py`）：

| 口径 | 修正 |
|---|---|
| 涨跌停价 | 按最小变动价位取整（股票 0.01 / 基金 0.001）。前收 3.63 的涨停价是 **3.99**（不是 3.993）—— 后者会把「开盘即涨停」放行 |
| ST 涨跌幅 | 只有主板 ST 是 5%；创业板/科创板 ST 仍 **20%**、北交所 ST 仍 **30%** |
| ETF 涨跌幅 | 跟随跟踪指数：科创(588/589 段)/创业板 ETF 为 **20%**，不再一律 10%（`by_code_prefix`/`by_code` 显式登记，支持 meta 注入跟踪指数） |
| ETF T+0 | 按名称关键字推断 fund_type（QDII/黄金/债券/货币 → T+0），关键词表在 rules yaml |
| 印花税 | 按日期区间 **+ 买卖方向** 取：2008-09-19 起才单边征收，此前双边；补全 2000 年起历史区间（早年回测不再崩） |
| 复权 | JQ 路径补上公司行为处理，与 Engine 路径同口径（拆股日净值连续） |
| 退市 | 按残值核销（默认 0=全额损失），退市日起不可再交易 —— 不再按最后收盘价永久冻结 |
| T+N | 按 **交易日** 而非自然日计算可卖数量 |
| 零股 | 清仓单允许卖出零股（A 股规则：零股须一次性卖出） |
| 滑点 | `config/rules/cn_a_share.yaml` 的 `slippage` 成为唯一真源（此前是死配置：写 2bp、实际用 5bp） |
| 资金不足 | 提为显式配置 `EngineConfig.insufficient_cash`：`reject`（券商/backtrader 语义）或 `truncate`（聚宽 order_value 语义） |
| 涨跌停豁免 | `no_price_limit` 标记真正生效（IPO 首日/复牌首日/ST 变更日） |
| 限价单 | `Order.limit_price` 真正被撮合读取（此前是死字段：设了限价仍按市价成交）。判定用不含滑点的基准价，成交价封顶/保底到限价；JQ 沙箱注入 `MarketOrder`/`LimitOrder` |
| 零成交 vs 停牌 | 归因分开：`无成交量`（流动性为零）≠ `停牌`。此前一律记「停牌或无行情」 |
| **逐日 ST** | 涨跌停按**当日**戴帽状态（`Bar.is_st`，来自日线湖的 `is_st`/baostock isST）。此前用 `security.is_st` 静态值，而该列全表 0 行为 True → 原生路径实际完全没做 ST 处理（真实数据有 **377,292 个 ST bar-日 / 2,070 只标的**被按 10% 处理）。`None` 才退回静态值 |
| 退市 | 按残值核销 + 退市日起不可交易；元数据经 `security_meta` 与 DB 同源 |
