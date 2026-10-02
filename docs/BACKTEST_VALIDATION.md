# 回测准确性验证方案

> 回测错了不会报错，只会让你亏钱。本文件定义 lquant 回测结果的**六层验证体系**，
> 每一层都有对应的自动化落地物。跑 `python scripts/validate_backtest.py` 可一键体检。

## 为什么需要专门的验证

回测错误的特征是「静默」：净值曲线漂亮、指标齐全，但数字是错的。
常见错误全部不抛异常：

| 错误 | 后果 | 被哪层抓住 |
|---|---|---|
| 用 T 日收盘价撮合 T 日信号（未来函数） | 收益虚高，实盘必亏 | L3 截断不变性 |
| 当日买入当日卖出（违反 T+1） | 换手虚高、成本低估 | L3 T+1 约束 |
| 费用漏算印花税/过户费 | 高换手策略收益虚高 | L2 会计恒等式 |
| 最大回撤用收益率序列直接取 min | 回撤被低估 | L4 指标交叉核对 |
| 年化用算术平均 | 高波动策略年化虚高 | L4 指标交叉核对 |
| 涨停板照买不误 | 根本买不进去的收益算进来了 | L3 涨跌停拒单 |
| 滑点方向写反（负滑点） | 越交易越赚 | L3 滑点单调性 |
| 引擎口径与第三方不一致 | 对外汇报数字打架 | L5 交叉引擎对照 |

## L1 金标准手算（Golden Case）

**方法**：构造 3~4 天、1~2 只标的、价格完全已知的小数据集，关闭滑点与费用
（零费率规则集），**用笔算**推出每一天的持仓、现金、净值、成交明细，
断言引擎输出与手算值误差 < 1e-6。

这是唯一能证明「引擎逻辑本身正确」的方法——合成随机数据只能证伪，不能证真。

**落地**：`tests/unit/test_backtest_accuracy.py::test_golden_buy_hold_hand_computed`
覆盖：信号日 → 次日开盘成交 → 收盘估值 → 净值序列 → 指标全链路。

## L2 会计恒等式（Cash Conservation）

**方法**：无论策略多复杂，资金流必须严格守恒：

```
initial_cash + Σ卖出成交额 − Σ买入成交额 − Σ全部费用 ≡ final_cash
final_cash + Σ持仓数量 × 收盘价 ≡ final_nav
```

两条恒等式在**零费率**下必须精确成立（浮点误差 < 1e-6），
在**真实费率**下把费用项计入后同样成立。
任何一条不成立，说明成交、费用或估值环节有资金凭空产生/消失。

**落地**：`test_cash_conservation_zero_fee` / `test_cash_conservation_with_fees`。

## L3 性质测试（Property-Based）

单点金标准之后，用「不变量」把正确性推广到任意数据：

1. **无未来函数（截断不变性）**：在同一日期截断数据集前后分别跑回测，
   截断日之前的净值序列必须**逐点一致**——未来数据的存在与否不能影响历史。
   这是抓「偷看未来」最有效的一招。
   落地：`test_no_lookahead_truncation_invariance`
2. **T+N 可卖约束**：`sellable_after_days=10` 时，买入后 10 天内的卖出信号必须
   产生零卖出委托；期满后恢复。
   落地：`test_t_plus_n_sell_constraint`
3. **涨跌停拒单**：开盘价 = 涨停价时买单必须被拒（`涨停不可买`），反之跌停拒卖。
   落地：`test_limit_up_buy_rejected`
4. **滑点单调性**：其他条件不变，滑点率 0 < r1 < r2 ⇒ 最终净值单调递减。
   落地：`test_slippage_monotonicity`
5. **成交时点正确**：`next_open` 模式下首笔成交日 > 首个信号日，成交价 = 次日开盘；
   `close` 对照模式成交价 = 当日收盘。
   落地：`test_next_open_fill_price_and_delay`

### L3 补充不变量（2026-10-02 复审新增，共同特征是「静默算错」）

| 不变量 | 落地 |
|---|---|
| **涨跌停价按 tick 取整**：前收 3.63 的 10% 涨停价是 3.99，用 3.993 判定会放行「开盘即涨停」的买单 | `test_backtest_defect_fixes.py::test_limit_up_price_is_tick_rounded_and_rejects_fill` |
| **除权日新建仓不得欠配**：挂单缩放与是否已持仓无关，ratio=1.3 时不能只建到 76.8% | `::test_ex_dividend_entry_not_undersized` |
| **除权日净值连续**：拆股只改份额不改净值；JQ 路径与 Engine 路径必须同口径 | `::test_jq_path_applies_corporate_actions` / `::test_jq_path_engine_path_agree_on_split_nav` |
| **退市核销**：退市日起持仓按残值变现，不得按最后收盘价永久冻结（长回测 NAV 虚高） | `::test_delisted_position_is_written_off_not_frozen` / `::test_delisted_symbol_cannot_be_bought_on_or_after_delist_date` |
| **零股可全卖**：清仓单允许非整手数量，10 送 9 后不得残留零股 | `::test_full_liquidation_sells_odd_lot` |
| **T+N 按交易日**：周五买入 T+2 周一不可卖 | `::test_t_plus_n_uses_trading_days_not_calendar_days` |
| **ST 分板涨跌幅**：主板 5% / 创业板·科创板 20% / 北交所 30% | `::test_st_price_limit_is_per_board_not_always_5pct` |
| **ETF 涨跌幅跟随指数**：科创·创业板 ETF 20%，不是一律 10% | `::test_etf_price_limit_resolved_by_code_not_always_10pct` |
| **印花税区间+方向**：2008-09-19 起才单边；早年区间不得崩 | `::test_stamp_duty_history_covers_both_sides_and_2023_cut` |
| **滑点配置是活配置**：yaml 改值必须真的生效 | `::test_yaml_slippage_config_is_live` |
| **资金不足口径显式**：reject（券商）vs truncate（聚宽）由配置决定 | `::test_insufficient_cash_modes_are_explicit_and_documented` |

## L4 指标交叉核对

指标是「第二次计算」，用独立实现/独立公式交叉验证：

- 年化收益 = `(1+总收益)^(252/n) − 1`（几何口径，与 metrics.py 独立重算对照）
- 最大回撤 = `1 − nav/max(cummax(nav))`（净值口径，非收益率口径）
- 夏普 = `年化收益 / 年化波动`（日收益 std × √252）
- 月度收益 = 月末净值复利环比，净值表为唯一真源

**落地**：`test_metrics_cross_check_hand_formula`。

## L5 交叉引擎对照（Cross-Engine）

**方法**：把同一份策略/数据经 `backtest/adapter.py` 接入 vectorbt 等第三方引擎，
在**无摩擦设定**（零费率、零滑点、无涨跌停）下对比净值：

- 两引擎日收益序列相关系数 > 0.9999
- 最终净值差 < 1bp

有摩擦设定下**不要求一致**（规则口径本来就不同），
但摩擦成本 = 无摩擦净值 − 有摩擦净值 必须为正且随费率单调。

> 说明：自研引擎保持唯一执行真源（见 docs/BACKTEST_ENGINES.md），
> 第三方引擎只做对照，不进生产路径。

**当前真实状态（2026-10-02 第二次复核）**：

- `backtest/adapter.py` 不再是空注册表：内置 **`frictionless_buy_hold`** ——
  一个零依赖、纯向量化的对照实现，**不复用 Engine/Broker/Account 任何代码**，
  因此它与原生引擎是两份独立实现。
- L5 的核心断言已落地（`test_backtest_defect_fixes.py`）：
  - `::test_l5_frictionless_nav_matches_independent_reference` ——
    无摩擦设定（零费率/零滑点/无涨跌停/lot_size=1）下，两引擎**逐日净值与
    日收益序列一致**（净值差 ≤ 1bp）；
  - `::test_l5_friction_cost_is_positive_and_monotonic_in_rate` ——
    有摩擦必然劣于无摩擦，且滑点越大净值越低；
  - `::test_l5_adapter_output_matches_engine_contract` ——
    `AdapterOutput` 与 `Engine.run()` 同形，可直接进 `/compare`。
- 第三方引擎（vectorbt/RQAlpha）仍**未接入**（原计划放开 pip 依赖后补），
  走同一协议即可；上表的内置对照引擎先把 L5 这一层「有测试」补上。
- **backtrader 对账的 2 个 FAIL 仍未能重跑**：见下节「重跑受限」。
- `result`：L5 从「零测试」变为「无摩擦一致性 + 摩擦单调性有测试」。

### backtrader 对账重跑受限（2026-10-02 实测）

`scripts/backtest_validation/validate_accuracy.py` 的 5 个任务里，只有 2 个
能在**当前数据湖**上重跑：

| 任务 | 依赖标的 | 湖里覆盖 | 能否重跑 |
|---|---|---|---|
| sma_cross | 600519.SH | 2024-01-02~2026-09-30 | ✅ |
| turtle_donchian | 600519.SH | 同上 | ✅ |
| baseline_multifactor | 510300.SH（参考标的） | **仅 2026-01-05 起** | ❌ 缺 2024 数据 |
| momentum_rotation | 510300.SH, 159915.SZ | **仅 2026-01-05 起** | ❌ 缺 2024–2026H1 |
| grid_trading | 510300.SH | **仅 2026-01-05 起** | ❌ 缺 2024–2025 |

可重跑的 2 个任务在本次修复后结果**与原记录逐位一致**（sma_cross 最大日差
0.0210%、turtle_donchian 0.0137%，成交笔数 34/17、14/7 不变）——
这本身是「本轮修复没有扰动已验证口径」的回归证据。

**结论**：那 2 个 ❌ FAIL 既没有变好也没有变坏，它们**当前无法验证**。
要收口需先把 510300.SH / 159915.SZ 的 2024 起日线回填进湖，再重跑全文。

## L6 实盘/模拟盘对账（Paper Reconciliation）

前五层证明「引擎算得对」，这一层证明「引擎和现实是一回事」：

- 回测同期段 vs 模拟盘（paper trading）日收益：日均偏差 < 10bp，相关系数 > 0.99
- 模拟盘成交价 vs 当日开盘价：偏离中位数 ≈ 滑点设定值
- 回测成交明细 vs 实际委托：方向一致率 100%，数量偏差 < 1 手

**落地**：`test_backtest_defect_fixes.py::test_paper_vs_backtest_reconciliation`
—— 同一份行情 + 同一笔成交喂给回测与模拟盘，断言**持仓、费用、逐日净值序列
完全一致**（价格有涨有跌，避免退化成常量比较）。

**当前真实状态（2026-10-02 复核）**：

- 该测试**已落地**，另有两条前置锁定用例：
  `::test_paper_t_plus_n_by_trading_days_and_lot_size`、
  `::test_paper_applies_corporate_action_share_adjustment`。
- 为此修掉了 `PaperBroker` 侧的 4 处口径分叉（此前它对不上账）：
  1. **公司行为完全缺失** → 新增 `apply_corporate_action`（份额调整法，与 Engine 同口径）；
  2. **T+N 一律当 T+1** → 改为按交易日递减的冻结台账（`frozen`），并**持久化**
     （`paper_position.frozen_json` + 启动迁移），否则 load_broker 一次就丢；
  3. **不查整手** → 买入强制整手、仅一次性清仓允许零股（与 `Broker` 同口径）；
  4. **涨跌停自己算** → `paper/quotes.limit_prices` 改走
     `InstrumentRules.limit_up/limit_down`（tick 取整 / ST 分板 / ETF 跟踪指数 / 豁免）。
- 另外修掉 `PaperEngine.push` **不盯市**的缺陷：`nav()` 用 `last_price` 估值，
  离线回放从不刷新它 → 净值曲线一直停在建仓当天（`service.tick` 一直在做，
  只有 replay 路径漏了）。
- `paper/reconcile.py` 做的是**模拟盘内部**「盘中快照 vs 收盘官方日线」重算，
  **不是**回测 vs 模拟盘对账，两者不可混淆。

## 一键体检

```bash
.venv/bin/python scripts/validate_backtest.py
```

依次执行 L1~L4 的全部自动化测试并输出清单报告；
L5/L6 为集成项，随对应模块落地自动纳入（脚本内 `PENDING` 列表已显式列出，
不会把未做项当成通过项）。
