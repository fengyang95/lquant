# 基准多因子策略 E2E 运行记录

由 `scripts/run_baseline_e2e.py` 生成,人工补注见文末。

## 运行参数

| 项 | 值 |
|---|---|
| 区间 | 2021-01-01 ~ 2024-12-31 |
| 初始资金 | 10,000,000 |
| 调仓 | 月度第 1 交易日开盘,top 20 等权,跌出 top 50 卖出 |
| 因子公式 | pct_change_20, rolling_std_20 |
| 净利润同比 item | `indicator.netprofit_yoy` |
| 运行时间戳 | 2026-09-14T10:47:12 |
| git commit | `a1c85e6` |
| 耗时 | 1823.9 s |
| 日线实际覆盖 | 2024-01-02 ~ 2024-12-31 |
| 日线条数 | 1,220,606 |
| 标的数(剔除 .BJ) | 5,083 |

## Metrics

| 指标 | 值 |
|---|---|
| n_periods | 241 |
| total_return | 0.332740 |
| annual_return | 0.350327 |
| annual_vol | 0.273907 |
| sharpe | 1.28 |
| sortino | 1.62 |
| max_drawdown | -0.140369 |
| max_dd_peak_idx | 78 |
| max_dd_trough_idx | 121 |
| calmar | 2.50 |
| win_rate | 0.381743 |
| payoff_ratio | 1.01 |
| underwater_periods | 150 |
| longest_dd_periods | 97 |
| skew | 0.265324 |
| kurtosis | 4.94 |
| best_period | 0.084397 |
| worst_period | -0.085204 |
| start | 2024-01-03 |
| end | 2024-12-31 |
| initial_cash | 10000000 |
| final_nav | 13,327,395.51 |
| n_trades | 204 |
| n_rejected | 13 |
| total_fee | 50,831.14 |
| turnover | {'n_trades': 204, 'total_amount': 68803610.55, 'turnover_per_period': 337272.6007352941} |

## NAV 摘要

| 日期 | NAV |
|---|---|
| 2024-01-02 | 10,000,000.00 |
| 2024-03-18 | 10,000,000.00 |
| 2024-05-30 | 10,176,713.16 |
| 2024-08-08 | 10,049,203.66 |
| 2024-10-24 | 12,769,635.72 |
| 2024-12-31 | 13,327,395.51 |

区间收益 33.27%(10,000,000 → 13,327,396)。

> 注:请求区间 2021-01-01 ~ 2024-12-31,但日线湖实际覆盖 2024-01-02 ~ 2024-12-31
> (2021–2023 未回填),本记录为覆盖区间内的实跑结果。

## Trades / Rejected

- 成交 204 笔;拒单 13 笔(`涨停不可买` x10; `资金不足一手` x2; `停牌` x1)。

## 各调仓日实际持仓数(keep 带漂移量化)

| 日期 | 持仓数 | 持仓市值 |
|---|---|---|
| 2024-01-02 | 0 | 0 |
| 2024-02-01 | 0 | 0 |
| 2024-03-01 | 0 | 0 |
| 2024-04-01 | 0 | 0 |
| 2024-05-06 | 19 | 9,460,951 |
| 2024-06-03 | 19 | 9,591,067 |
| 2024-07-01 | 21 | 9,779,154 |
| 2024-08-01 | 21 | 10,351,254 |
| 2024-09-02 | 20 | 9,967,340 |
| 2024-10-08 | 20 | 13,711,209 |
| 2024-11-01 | 20 | 14,642,044 |
| 2024-12-02 | 20 | 14,055,626 |

keep 带(top 50)内但 top 20 外的持仓不会被再平衡 → 持仓数/权重随时间漂移:
区间内持仓数 0–21(均值 13.3,首次 0,末期 20)。

## growth item 确认

financial_pit 中与"同比/yoy"相关的 DISTINCT item:['indicator.cfps_yoy', 'indicator.roe_yoy', 'indicator.eqt_yoy', 'indicator.or_yoy', 'indicator.basic_eps_yoy', 'indicator.q_sales_yoy', 'indicator.equity_yoy', 'indicator.dt_eps_yoy', 'indicator.op_yoy', 'indicator.ebt_yoy', 'indicator.dt_netprofit_yoy', 'indicator.tr_yoy', 'indicator.netprofit_yoy', 'indicator.ocf_yoy', 'indicator.bps_yoy', 'indicator.assets_yoy']。
策略采用 `indicator.netprofit_yoy`(tushare fina_indicator,主源)。

## 人工补注

1. **数据缺口**:日线湖当前仅覆盖 2024-01-02 ~ 2024-12-31(2021–2023 未回填)。
   本次实跑为覆盖区间内的结果,等 2021–2023 回填后重跑本脚本即可补全 4 年记录。
2. **前 4 个月 0 持仓是切片预热伪影**,不是策略缺陷:策略要求上市满 60 个
   交易日(`attribute_history(c, 60)`),而 bars 只喂了 2024 一年,2024-01~04
   的调仓日没有任何标的凑满 60 根历史 bar → `tradable` 为空。喂入更长预热
   区间(如 start=2023-10)即消失。
3. **keep 带漂移**:top 20 外但 top 50 内的持仓只卖不调权,实际持仓数在
   19–21 之间漂移(见上表);每次调仓只对跌出 top 50 的仓位清仓、对 top 20
   补到等权,其余仓位保留旧权重 → 单票权重偏离 1/20。
4. **拒单分布良性**:涨停不可买 x10、资金不足一手 x2、停牌 x1,共 13 笔,
   占成交 204 笔的 6.4%,均为规则内正常拒单。
5. **资源占用**:实跑约 30 分钟(1824 s),峰值 RSS 约 3 GB(全市场 5083 标的、
   122 万根 bar、月度 get_fundamentals 全表扫描 financial_pit 约 3000 万行)。
6. **item 确认**:BaoStock `growth.YOYNI` 在本库不存在(growth 前缀无任何行),
   实际 item 为 tushare fina_indicator 的 `indicator.netprofit_yoy`
   (33.4 万行),已回填常量并同步单测。
