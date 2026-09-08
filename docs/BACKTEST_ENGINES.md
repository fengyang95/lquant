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
- [ ] 如需撮合交叉验证：RQAlphaAdapter（pip 依赖放开后）
