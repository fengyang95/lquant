# 基准多因子策略端到端验收与四方向加固 — 设计文档

日期:2026-09-13
状态:已与用户确认设计;待实施计划。

## 背景与目标

仓库已具备:回测引擎(T+1 开盘撮合、涨跌停/停牌拒单、T+N 可卖、金标准自检 + backtrader 对账)、
基本面 PIT 数据(`financial_pit`,pub_date 防前视)、聚宽兼容层(`backtest/jqapi.py` JQRunner +
`research/dialect/fundamentals.py` get_fundamentals DSL)、ML 三段式 + walk-forward
(`research/ml/`,LGBM→SklearnGBDT→Ridge 降级链)。

本次工作以**一个真实基准策略的端到端验收为主线**,缺口驱动地加固四个方向:
回测正确性、聚宽兼容面、基本面因子、ML 模型(lightgbm/sklearn)。

## 推进方式(已确认)

方案 A:以基准策略为验收主线,四方向作为支撑阶段串行推进;
兼容面以"基准策略 + 常用选股场景"为准,不做全 API 扫描。

## P0 基准策略定义

- 位置:`research/strategies/baseline_multifactor.py`(或等价位置,以仓库惯例为准)。
- 股票池:全 A,剔除 ST、停牌、上市不满 60 日。
- 因子:
  - 基本面:`get_fundamentals` 取 `valuation.pe_ratio`、`income.net_profit` 同比增速;
  - 技术面:因子 DSL 计算 20 日动量、20 日波动率;
  - 各因子截面 ZScore 后等权合成。
- 组合:月度调仓,合成分数 top 20 等权持有;持仓跌出 top 50 卖出。
- 运行:JQRunner,2021–2024,`price_mode=next_open`。
- 产出:一次真实运行记录 + **缺口清单**(每项标注归属:兼容面 / 回测正确性 / 数据层)。
  缺口清单是 P1–P3 的唯一输入。

## P1 聚宽兼容面补齐(按需)

- 范围:基准策略实际用到的方法 + 常用选股场景;预期包括
  `get_fundamentals` 全字段贯通(valuation/income/growth/operation,balance/cashflow 视缺口)、
  `get_price`/`history` 边界语义、`order` 系列在涨跌停/停牌下的返回值语义。
- 每项补齐配金标准单元测试(手算期望值,复用 `golden.py` 模式)。
- 不做全 API 扫描。

## P2 回测正确性加固

- 输入:P0 缺口清单中回测侧条目。预判重点:
  分红送股的持仓复权/现金处理、停牌期持仓估值、调仓日涨跌停部分成交、复权因子切换日净值连续性。
- 每项修复后跑 `scripts/validate_backtest.py` 六层验证;backtrader 对账扩充到基准策略。
- **正确性红线**:任何修复必须先在 `selfcheck.py` 金标准框架下证明旧行为错、新行为对。

## P3 ML 深化

- 基准策略因子直接作为特征库(复用 `research/ml/dataset.py` PIT 拼接)。
- lightgbm 月度滚动训练(walk-forward 已有);补:特征名注册、
  预测分数落 `factor_value` 表以复用 tear sheet。

## P4 终验

- 线性打分 vs ML 打分两版基准策略,同参数对比:IC / 分层 / 净值 / 换手,出对比报告。

## 全局约束

- PIT 红线:基本面数据任何取数路径必须有 pub_date 防前视断言。
- 错误处理与测试遵循现有 golden-test 模式;新增接口走 pytest 单测。
- 遵守仓库已知坑:ruff 基线违规用 `--no-verify` + 逐文件 lint;Rust 相关测试遵循
  `--no-default-features` 链接约定;长写入按短写协议。

## 验收标准

1. 基准策略在 JQRunner 上 2021–2024 完整跑通,无前视、无静默吞错。
2. P0 缺口清单条目全部关闭或在报告中显式声明不修及理由。
3. `scripts/validate_backtest.py` 六层验证通过;基准策略 backtrader 对账在既定容差内。
4. 两版打分方式对比报告产出,ML 链路预测分数可复用因子评价体系。
