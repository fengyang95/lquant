# 回测引擎选型评估（2026-09-08）

## 结论（TL;DR）

**自研事件引擎保留为唯一「执行真源」；参数扫描走 Polars 向量化快扫（B5 待做）；外部引擎通过适配器协议接入，结果与原生引擎同构、可直接 /compare。**

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
- [ ] B5：Polars 向量化参数扫描（`/api/backtests/sweep`，TopN 网格 + 近似成本）
      —— **当前状态：端点已落地（异步队列 + 逐档返网格），但未向量化**：
      `backtest/sweep.py` 仍是逐档调用事件引擎，没有任何 Polars 向量化。
      档数少时够用，万级网格需按本项补向量化近似成本模型。
- [ ] 如需撮合交叉验证：RQAlphaAdapter（pip 依赖放开后）

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
| 退市 | 按残值核销 + 退市日起不可交易；元数据经 `security_meta` 与 DB 同源 |
