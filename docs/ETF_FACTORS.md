# ETF 专属因子模块

> 目标：为 ETF/LOF 建立一套独立的因子体系（因子定义 → 因子分析 → 回测），
> 与个股链路**口径隔离**但复用同一套评价与撮合内核。

## 1. 为什么 ETF 必须单独一套

| 问题 | 后果（都是静默的） |
|---|---|
| 日线湖 `read_daily()` **不按 `sec_type` 过滤** | ETF 与个股混在同一横截面，IC 没有任何意义 |
| ETF 没有流通市值（`float_mv` 全 NULL） | 个股默认协变量 `market_cap` 覆盖率恒为 0 |
| ETF 没有申万行业分类 | 默认 `industry_sw1` 覆盖率恒为 0 |
| 上述两条叠加 | 中性化**静默失效**却不报错 —— 因子看起来有效，实际什么都没去掉 |
| ETF 横截面薄且高度相关 | 沿用个股评级线（`|IC|≥0.02`）会把真信号误判成噪声 |

因此 ETF 有独立的：标的池、面板构建、字段白名单、协变量口径、评级阈值。
**共用**的是：因子 DSL/算子、`evaluate()` 评价内核、`Engine` 撮合内核与交易规则。

## 2. 模块与文件

| 文件 | 职责 |
|---|---|
| `src/lquant/factors/etf.py` | ETF 池解析 / 面板构建 / 因子加载 / `evaluate_etf` |
| `config/factors/etf.yaml` | 12 个 ETF 因子 + ETF 预处理配方 |
| `config/factors/rating_etf.yaml` | ETF 评级阈值（IC 两条线比个股低） |
| `src/lquant/backtest/etf.py` | `etf_factor_backtest`：ETF 池 → 因子列 → 原生 Engine |
| `src/lquant/cli/commands/etf.py` | `lq etf` 命令组 |
| `src/lquant/server/api/etf.py` | `/etf/universes` `/etf/factors` `/etf/evaluate` `/etf/backtest` |
| `src/lquant/factors/covariates.py` | 新增 `etf_fund_size` / `etf_share` provider |
| `src/lquant/factors/universe.py` | `all_universe_options()` 聚合 ETF 池、`universe_label` 委派 |

改动仅有：`factors/evaluate/__init__.py`（`evaluate()` 支持 `rating_thresholds`）、
`server/api/factors.py`（`_resolve_universe`/`_check_universe` 分流 ETF 池）、
`server/api/backtests.py`（`BacktestIn.universe`）。**不动** `web/`、
**不动** `config/rules/cn_a_share.yaml`（ETF 规则已完备）。

## 3. 标的池

```
etf_all        全部 ETF/LOF
etf_equity     股票型 ETF      ┐
etf_qdii       跨境 ETF        │ 按 etf_meta.fund_type 筛
etf_bond       债券 ETF        │
etf_commodity  商品 ETF        │
etf_money      货币 ETF        ┘
```

`resolve_etf_universe()` 三级降级，**每一级都显式披露**（绝不静默冒充）：

1. `etf_meta`（首选，支持 fund_type 细分）；
2. `security` 表（`sec_type IN ('etf','lof')`）—— `etf_meta` 未同步；
3. 日线湖代码段 —— 前两者都空。

三级兜底分支一律再过一遍 `is_etf_symbol`（`parse_symbol().sec_type`），
**「ETF 池里不出现个股」这条不变量不能因数据来源不同而失效**。

唯一「有池但取不到子集」的情形（请求 `etf_bond` 但 `fund_type` 全 NULL 或
该类型一只都没有）退化为全 ETF 口径，并在第二个返回值写明原因，调用方
必须把披露并入报告口径标签。三级都空 → `ValueError`，API 转 503 + 同步指引。

`is_etf_symbol` 复用 `core.types.Symbol.sec_type`（全平台唯一口径：
沪 50/51/56/58、深 15/16/159）——**绝不**另写一份代码段表，否则必然漂移。

## 4. 面板与字段

`build_etf_panel(start, end, symbols, universe, covs)`：

```
read_daily(symbols=…, start=…, end=…).collect()
  → _restrict_to_etf()                     # 二次保险：剔除非 ETF 行
  → attach_etf_meta()                      # fund_type/track_index/fund_size/…
  → build_covariates(ETF_DEFAULT_COVS)     # 覆盖率显式回传
```

返回 `(面板, cov_* 列名, info)`；`info` 含池口径、降级披露、标的数与
**每个协变量的覆盖率**（覆盖率是结论的一部分）。

**ETF 元数据如何进因子**（两个通道，职责不同）：

- 数值列 `fund_size` / `share_outstanding` / `management_fee` / `custody_fee`
  作为**真实列**挂上面板，由 `etf_allowed_fields()` = `daily_fields() ∪ ETF_EXTRA_FIELDS`
  放开给 ETF 上下文。**不改** `factors/fields.py` 全局白名单 —— 否则个股画布会
  出现「选了也永远是 null」的列。
- `etf_fund_size`（log1p）/ `etf_share`（log1p）注册为 **covariate provider**，
  用于规模中性化。缺列 → `CovariateUnavailable` → coverage=0 **显式上报**，
  绝不静默去均值。
- `fund_type` 是类别量：只做分组键（`cat_col`/`group_col`），不做 `$字段`。

`ETF_DEFAULT_COVS = (etf_fund_size, turnover_1m, momentum_1m)` ——
**刻意不含** `market_cap`/`industry_sw1`（对 ETF 覆盖率恒为 0）。

## 5. 因子清单（config/factors/etf.yaml）

只用现有数据（日线 OHLCV/成交额/换手 + `etf_meta`）可算的 12 个：

| 类别 | 因子 | 表达式 |
|---|---|---|
| 动量 | `etf_mom_20` | `Ts_Return($close, 20)` |
| 反转 | `etf_rev_5` | `-Ts_Return($close, 5)` |
| 动量 | `etf_ma_dev_20` | `$close / Ts_Mean($close, 20) - 1` |
| 动量 | `etf_tsrank_20` | `Ts_Rank($close, 20)` |
| 波动 | `etf_vol_20` | `Ts_Std(Ts_Return($close, 1), 20)` |
| 流动性 | `etf_amt_liq_20` | `Ts_Mean($amount, 20)` |
| 流动性 | `etf_turn_20` | `Ts_Mean($turnover_rate, 20)` |
| 量价 | `etf_pv_div` | `Ts_Corr($close, $volume, 20)` |
| 截面 | `etf_xs_rs_20` | `Rank(Ts_Return($close, 20))` |
| 规模 | `etf_size` | `-Log($fund_size)` |
| 资金流 | `etf_share_chg_20` | `Ts_Return($share_outstanding, 20)` |
| 成本 | `etf_fee` | `-$management_fee` |

> ETF 无 `float_mv`，流动性类必须用 `amount`/`turnover_rate`；
> 规模只能用 `etf_meta.fund_size`（缺点是**当下快照**、非 PIT 序列）。

每条在加载时过 `check(ast, allowed_fields=etf_allowed_fields())`：
字段拼错在**静态期**报错并给近似候选，而不是算出全 null 静默污染 IC。

## 6. 因子评价与回测

```python
evaluate_etf(expr, universe="etf_all", start=None, end=None,
             n_groups=10, with_report=False, with_robustness=False)
```

- 走 `build_etf_panel` → `prepare_segment`（winsorize → zscore → OLS 中性化）→ `evaluate()`；
- **评级阈值**来自 `rating_etf.yaml`（`evaluate()` 新增 `rating_thresholds` 参数传入）；
- **归因/分组改用 `fund_type`**（ETF 无行业）；
- 出口统一做 JSON 归一化（polars DataFrame / date / NaN → 原生），CLI 与 API 共用。

```python
etf_factor_backtest(formula, universe="etf_all", top_n=10, rebalance="monthly", …)
```

- `build_etf_panel(covs=[])` → `compute_factor_col`（与评价链路**同一实现**，
  杜绝「同一公式评价能算、回测算不出」）→ `FactorTopNStrategy` → `Engine`；
- 把 `etf_meta` 的 `fund_type`/`sellable_after_days` 注入 `Engine(meta=…)`，
  比「按名称关键字猜 fund_type」更准；
- **规则零改动**：`build_rules` 按 `parse_symbol().sec_type` 给 ETF 自动挂
  `etf:` 段（免印花税、免过户费、佣金 rate 0.0001/min 0.1、tick 0.001、
  涨跌停 `by_track_index`、T+N by `fund_type`）。

## 7. 验收

```bash
.venv/bin/python -m pytest tests/unit/test_etf_*.py -q     # 68 passed
.venv/bin/python -m ruff check src tests scripts           # All checks passed
```

端到端（demo 数据）：

```bash
lq data demo --start 2024-01-01
lq etf factors && lq etf eval 'Ts_Return($close, 20)' --universe etf_all
lq etf backtest --factor 'Ts_Return($close, 20)' --universe etf_all --top-n 3
```

> 表达式一律用单引号 —— `$close` 在双引号里会被 shell 当变量展开成空串。

## 8. 已知边界 / 后续

- **无 IOPV / 折溢价 / 份额日序列**：折溢价套利类因子需要先补数据管道。
- **`etf_meta` 是当下快照**，非 PIT 序列 → 规模/份额类因子结论只对近期成立，
  报告口径里已注明。
- 未接 Web 前端（本次范围外）；API 已就绪，前端可直接调 `/api/etf/*`。
