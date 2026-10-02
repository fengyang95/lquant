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

## 缺口清单

> 来源:Task 3 单测 xfail/口径差异、Task 4 E2E 实跑发现、各任务 deferred minors。
> 本段与"人工补注"同属重跑保留区(`scripts/run_baseline_e2e.py` 的
> `PRESERVED_HEADINGS`),脚本重跑不会覆盖;后续 P1(聚宽兼容面)/P2(回测
> 正确性)计划以本表为唯一输入。

| 编号 | 现象 | 归属 | 复现方式 | 建议归属 | 是否阻塞基准策略验收 |
|---|---|---|---|---|---|
| G1 | ~~`JQRunner._sec_data` 构造 `_SecData` 时 `is_st` 恒 False,不读 security 表;ruleset per-instrument `is_st` 元数据(PriceLimit/涨跌停 5%)未接线,策略源码中 ST 剔除为死代码~~ **已在 P1 修复**:is_st 从 security 表贯通策略剔除与涨跌停规则(ST 5%,顺带修复 gem/star 20%/bse 30% 板块值,原先一律 10% 与 cn_a_share.yaml 矛盾) | 兼容面 | `tests/unit/test_jq_api.py::test_is_st_wired_from_security_table` / `::test_st_price_limit_5pct`;`test_baseline_strategy.py::test_baseline_excludes_st` 已 un-xfail | 已闭环(P1) | 否(曾阻塞,已修) |
| G2 | 日线湖 2021–2023 未回填,基准 E2E 只能实跑 2024 一年(请求 4 年) | 数据层 | `PYTHONPATH=src python scripts/run_baseline_e2e.py`(preflight 打印 WARN "缺 2021-2023 回填") | 数据回填 | 是(4 年基准记录缺失) |
| G3 | 切片预热伪影:策略要求 60 个交易日历史 bar,只喂 2024 单年时 2024-01~04 调仓日 `tradable` 为空 → 前 4 个月 0 持仓 | 回测正确性(脚本参数) | 实跑持仓数表 2024-01-02~2024-04-01 均为 0;`--start 2023-10` 喂长预热即消失 | P2(脚本默认预热区间,或 G2 回填后自然消除) | 否(已定性为伪影非策略缺陷) |
| G4 | ~~keep 带漂移:top 20 外但 top 50 内的持仓只卖不调权,单票权重偏离 1/20,实际持仓 19–21 漂移~~ **已裁定(P2):缓冲带设计,非缺陷**——带内(21-50)持仓保留但不调权是为减换手;权重漂移是设计代价而非引擎缺口,漂移幅度已在人工补注量化(实际持仓 19–21)。若未来改为"带内再平衡",须同步更新锁定测试 | 回测正确性(策略语义确认) | `tests/unit/test_baseline_strategy.py::test_baseline_monthly_topn_exit_rule` + `::test_keep_band_positions_retained_without_rebalance`(锁定"带内零订单"语义)+ 实跑持仓数表(见上) | 已闭环(P2 裁定:保留现状) | 否(已量化并记录) |
| G5 | ~~`attribute_history` 窗口按自然日序数截取、缺 bar 日不补行:停牌次日仅 59 行 → 被 MIN_LISTED_DAYS 误剔~~ **已在 P1 修复**:history/attribute_history skip_paused=True(默认)按交易日窗口前推(只数有 bar 的行),停牌不再造成保守误剔;skip_paused=False 保持自然日窗口口径 | 兼容面 | `tests/unit/test_jq_api.py::test_attribute_history_skip_paused_counts_traded_bars` / `::test_history_skip_paused_counts_traded_bars`;`test_baseline_strategy.py::test_baseline_excludes_halted` 口径已更新 | 已闭环(P1) | 否(保守方向偏差,已修) |
| G6 | `get_fundamentals` 的 date 参数未钳制到当前交易日,存在前视口子(聚宽原生语义同此)。**已在 P0 修复**:显式 date 钳制 `min(date, 当日)` | 回测正确性 | `tests/unit/test_jq_fundamentals_wiring.py::test_future_date_param_clamped_to_trade_day` | 已闭环 | 否 |
| G7 | BaoStock `growth.YOYNI` item 不存在(growth 前缀 0 行),实际同比 item 为 tushare `indicator.netprofit_yoy`;曾致策略取数表错误 | 数据层 | financial_pit `SELECT DISTINCT item ... LIKE '%yoy%'`(preflight 已打印);已回填常量并同步单测 | 已闭环 | 否 |
| G8 | T+1 语义口径裁定:计划文本"信号日下单次日开盘成交"与引擎/聚宽口径(当日开盘撮合 + T+1 可卖 `sellable_after_days`)冲突,按引擎/聚宽口径执行;若裁定错误需重审撮合时点 | 回测正确性 | `tests/unit/test_baseline_strategy.py::test_baseline_t_plus_one_fill` | P2(复核撮合时点口径) | 否 |
| G9 | jq_shim 模块级 `_STATE` + 全局上下文绑定,同进程多 runner 并发会互相污染(当前无此用法) | 回测正确性(工程健壮性) | Task 1 review deferred;同进程构造两个 JQRunner 交替 run 可复现 | P2 | 否 |
| G10 | NaN 因子穿透评分:沙箱纯 Python 版 `or 0.0` 与 polars 版 `fill_null` 均接不住真实 NaN,一个 NaN 污染全截面 z 分数,NaN 行降序排第一先入池。**已在 P0 修复**(纯 Python `isnan` 归零;polars `fill_nan(0.0)` 后合成) | 回测正确性 | `tests/unit/test_baseline_strategy.py::test_composite_score_real_nan_never_ranks_first` / `::test_sandbox_score_real_nan_isolated_and_deterministic` | 已闭环 | 否(曾阻塞,已修) |
| G11 | `attribute_history`/`history` 的字段参数传字符串被 `list(str)` 拆成单字符列(`'close'` → `['c','l','o','s','e']`),触发迷惑的 `KeyError('close')`。聚宽原生 fields 为 list/tuple,字符串报错路径应显式。**复现:`scripts/probe_attr_history.py`** | 兼容面 | `PYTHONPATH=src python scripts/probe_attr_history.py`(LOG 显示 cols=['c','l','o','s','e'] + REPRO KeyError) | P1 | 是(复杂策略 E2E 因此中断) |
| G12 | 复杂策略 E2E(`scripts/run_complex_e2e.py`)所需 2025 全年+2026 实跑:复用 G2 数据回填后已具备(日线湖 2024-01~2026-09,247k bars×600 标的) | 数据层 | `PYTHONPATH=src python scripts/run_complex_e2e.py` | 已闭环 | 否 |
| G13 | 全市场 `get_fundamentals` 单次 ~170s(financial_pit 72M 行,无日期/标的缓存;每日调用 = E2E 20h)。热帧:duckdb 全表扫描 | 回测正确性(性能) | `python - <<EOF` 计时复现:单次 172s/168s/167s ×3 | P1(性能专项) | 是(复杂策略每日盘前选股不可用) |
| G14 | 复杂策略 E2E 实跑完成(2025-01-02~2026-09-11,600 标的×411 交易日,月度调仓 top15 等权+止损止盈):总收益 +9.51%,336 笔成交/6 笔拒单(拒单原因分布合理:涨停不可买/跌停不可卖/资金不足一手),**审计 PASS**(停牌日无成交、无 T+1 双向成交、无涨停买入/跌停卖出)。耗时 3831s(大头是 14 次 monthly get_fundamentals×170s,即 G13) | 回测正确性 | `PYTHONPATH=src python scripts/run_complex_e2e.py` | 已闭环 | 否 |

### 2026-09-14 双路审查新增(聚宽兼容面 + 引擎正确性) —— 2026-10-02 收口

> G15/G16/G17/G18/G20(全部四项) 已修复并锁定测试；G19 为**口径差异**（刻意保留
> 部分偏离）。其余开放项见文末「仍未闭环」表。

| 编号 | 现象 | 状态 | 锁定测试 |
|---|---|---|---|
| G15 | **阻塞**:get_price/history/attribute_history 无 `fq` 参数(传 fq='pre' 直接 TypeError);回测行情全程不复权,跨除权日动量/均线与聚宽系统性发散 | **已闭环**:三个 API 均支持 `fq=None/'pre'/'post'`(默认 'pre'，与聚宽一致)；复权只作用于价格字段(open/high/low/close/pre_close/avg)，量额不缩放。`pre` 以**今日**复权因子为基准归一，跨除权日序列连续 | `test_backtest_defect_fixes.py::test_history_fq_pre_adjusts_across_ex_dividend` / `::test_fq_param_accepted_on_all_three_data_apis` |
| G16 | **阻塞**:order_target_percent 未注入沙箱(NameError);get_trade_days/get_index_stocks 有实现未注入 | **已闭环**:四个 API 全部注入。`get_trade_days` 用回测自身日历(与引擎推进一致)；`get_index_stocks` 走 index_cons 表且成分表缺失时抛**可操作**的 DataError(绝不静默返回空) | `::test_order_target_percent_injected_and_targets_percent_of_portfolio` / `::test_get_trade_days_uses_backtest_calendar` |
| G17 | run_monthly 负数 monthday(月末倒数)静默永不触发;run_daily 具体时刻('14:50')一律归 open 桶 | **已闭环**(且比原描述更严重)：'14:50' 原先被解析成「每月第 4 个交易日」；现调度键加 `d:` 前缀区分，非法时刻显式报错；`monthday<0` 按整月长度取倒数；尾盘时刻(>=14:30)归 close 桶 | `::test_run_daily_with_clock_time_stays_daily` / `::test_run_daily_invalid_time_raises` / `::test_run_monthly_negative_monthday_fires_on_last_trading_day` |
| G18 | JQ 路径无公司行为处理；涨跌停未按 tick 取整；停牌/退市持仓按 avg_cost 估值 | **已闭环**：(a) JQ 路径补 `_apply_corporate_actions`，两路径拆股日净值一致；(b) 涨跌停改为 `InstrumentRules.limit_up/limit_down`，按 tick 取整(股票 0.01/基金 0.001)，前收 3.63 涨停价 = 3.99；(c) 停牌估值早已用最近可见收盘价(原描述已过时)，**退市**改为按残值核销(默认 0)且退市日起不可再交易 | `::test_jq_path_applies_corporate_actions` / `::test_jq_path_engine_path_agree_on_split_nav` / `::test_limit_up_price_is_tick_rounded_and_rejects_fill` / `::test_delisted_position_is_written_off_not_frozen` |
| G19 | 默认值偏离聚宽(history field/skip_paused/attribute_history fields、夏普口径、默认费率滑点) | **部分闭环**：默认滑点改为读规则表(单一真源，见下方 D9)；`skip_paused` 保持 True —— 这是 G5 的正确修复方向(按**交易**窗口前推)，刻意不与聚宽 False 对齐。夏普保持几何口径(selfcheck 锁定)。其余差异属**口径差异非缺陷**，对账时按此表校正 | `::test_yaml_slippage_config_is_live` |
| G20 | get_fundamentals 字符串 date 崩；get_current_data ST high_limit 与撮合不一致；两路径拒单口径不可比；is_st 全期恒定 | **已闭环(前三项)**：(a) date 先 `fromisoformat` 归一，字符串/date/None 全部可用；(b) `_SecData` 的 high_limit/low_limit 与撮合同源(ST 显示 10.5/9.5)；(c) 资金不足口径提为显式配置 `EngineConfig.insufficient_cash`(reject=券商/backtrader,truncate=聚宽)，两路径差异由**配置**表达而非偶然实现分歧。(d) is_st 全期恒定仍开放 —— security 表无 ST 变更日期维度，需数据层新增 st_history 后接线 | `::test_get_fundamentals_string_date_does_not_crash` / `::test_get_current_data_st_limit_matches_matching_rule` / `::test_insufficient_cash_modes_are_explicit_and_documented` |

### 2026-10-02 引擎正确性复审新增

| 编号 | 现象 | 修复 | 锁定测试 |
|---|---|---|---|
| D1 | **除权日新建仓只建到 1/ratio 仓位**：挂单缩放逻辑嵌在「遍历持仓」循环内，除权日还没持仓 → 永不执行(ratio=1.3 时 100% 目标只成交 76.8%)；日频次日自愈，周频/月频一直错 | 缩放改为按「今日因子 vs 昨日因子」对所有标的计算 ratio，再分别作用于持仓与挂单 | `test_backtest_defect_fixes.py::test_ex_dividend_entry_not_undersized` |
| D2 | 清仓单向下取整到整手，**零股永远卖不掉**(10 送 9 后剩 11.11 股) | `Order.allow_odd_lot` + `Broker._max_qty` 放行清仓零股(A 股规则要求零股一次性卖出) | `::test_full_liquidation_sells_odd_lot` |
| D3 | T+N 用**自然日**：周五买入 T+2 周一就「到期」，T+5 类锁定系统性偏松 | `Account.available_at` 支持交易日序号表，引擎按交易日算 | `::test_t_plus_n_uses_trading_days_not_calendar_days` |
| D4 | ST 涨跌幅一律 5%：创业板/科创板 ST 实际仍 20%、北交所 ST 仍 30% | `PriceLimit.st_by_board`，按板块取值 | `::test_st_price_limit_is_per_board_not_always_5pct` |
| D5 | ETF 涨跌停一律 10%：科创/创业板 ETF 实际 20%(`by_track_index` 声明了但无人传参) | 代码段/代码显式登记(yaml `by_code_prefix`/`by_code`)，并支持 meta 注入跟踪指数 | `::test_etf_price_limit_resolved_by_code_not_always_10pct` |
| D6 | 原生 Engine 路径**从不注入** is_st/fund_type：ST 按 10%、QDII/黄金/债券 ETF 按 T+1 | 两条路径共用 `security_meta` 模块；fund_type 按名称关键字推断(yaml 可配) | `::test_native_engine_injects_security_meta` / `::test_etf_t0_inferred_from_name_for_gold_and_qdii` |
| D7 | `exceptions.no_price_limit_on`(IPO 首日/复牌首日/ST 变更日)载入后无人读 | `InstrumentRules.no_price_limit` → `limit_up/down` 返回 None，broker 跳过涨跌停校验 | `::test_no_price_limit_flag_disables_limit_check` |
| D8 | 印花税只配了 2008-09-19 之后，且**恒为单边**：早期回测要么崩、要么成本低估一半 | 补全 2000 年起历史区间 + **买卖方向**维度(2008-09-19 起才单边)；早年区间不再抛 RuleNotFound | `::test_stamp_duty_history_covers_both_sides_and_2023_cut` |
| D9 | `config/rules/cn_a_share.yaml` 的 `slippage` 是死配置(写 2bp，实际用代码里硬编码的 5bp) | 规则表成为滑点唯一真源(Engine 与 JQ 路径同源)；配置值校准为实际生效的 5bp | `::test_yaml_slippage_config_is_live` |
| D10 | `attribute_history(fields='close')` 把字符串拆成 `['c','l','o','s','e']` 再抛迷惑的 `KeyError('close')` | 字符串按单字段处理(与 `history` 一致) | `::test_attribute_history_string_fields_not_split_into_chars` |
| D11 | 基本面查询 DSL 的 `表.字段 == 值`(聚宽惯用)退化成 Python bool，抛 `'bool' object has no attribute 'column'` | `Query.filter` 对非条件对象给出**可操作报错**引导到 `.in_([...])`；不重载 `Column.__eq__`(Column 是 dict/set 键，重载会破坏缓存查找) | `::test_fundamentals_filter_rejects_non_condition_with_clear_error` |
| D15 | **涨跌停阈值有第四份重复实现**：`server/api/market.py::_limit_threshold` 硬编码「300/301/688/689→20%，43/83/87/92→30%，其余→10%」，漏了主板 ST 的 5% 与 ETF 跟踪指数 —— 涨跌停家数（市场宽度）系统性偏差 | 改为复用 `build_rules`（与回测/模拟盘同一份规则表），并按当日 `is_st` 切换；`is_st` 列缺失时补 null（老湖/合成湖不再让接口退化成 None）。实测 2026-09-04（当日 1843 只 ST）：涨停 46→**51**、跌停 17→**14** | `tests/unit/test_api_cov_market.py::test_limit_ratio_map_by_board_and_st` |
| D12 | `volume==0` 与「停牌」混用同一个 `halted` 标记，拒单 reason 一律写「停牌或无行情」—— 归因错误导致「为什么没成交」永远查不清 | `Bar.no_volume` 独立标记，零成交日记「无成交量」、真停牌仍记 `suspended`/「停牌或无行情」；`_synth_bar` 透传该标记 | `::test_zero_volume_day_rejected_with_accurate_reason` / `::test_suspended_day_keeps_suspended_reason` |
| D13 | **无限价单**：`Order.limit_price` 字段存在但撮合从未读取（设了限价仍按市价成交）；JQ 沙箱里也没有 `LimitOrder`/`MarketOrder` style 对象 | 撮合按限价判定：以**不含滑点**的基准价判断是否可成交，成交价封顶/保底到限价（限价单绝不成交在更差价位）；当日有效（A 股默认）不成交即作废。沙箱注入 `MarketOrder`/`LimitOrder`，`order`/`order_value`/`order_target`/`order_target_value`/`order_target_percent` 全部支持 `style=` | `::test_limit_order_not_filled_when_price_worse_than_limit` / `::test_limit_order_fills_at_limit_or_better` / `::test_jq_limit_order_style_is_available_and_honored` |

| D14 | **G20d `is_st` 全期恒定**：不随戴帽/摘帽变化，ST 剔除与 5% 涨跌停判定整段回测用同一个值。而 `security.is_st` **全表 0 行为 True** → 原生路径实际完全没做 ST 处理 | 改为读**日线湖逐日的 `is_st`**（baostock `isST`，已入库）：`Bar.is_st` 逐日携带，`InstrumentRules.limit_up/limit_down/limit_ratio(is_st=...)` 按当日覆盖，`Broker` 与 `_SecData` 均以当日值为准，`None` 才退回 `security` 表静态值。**无需新建 st_history 表——数据早就在每日 bar 里** | `::test_per_day_is_st_drives_limit_price_not_static_flag` / `::test_is_st_flip_mid_backtest_changes_limit` / `::test_get_current_data_is_st_is_per_day` |

### 仍未闭环（需要数据层或外部依赖先动，非回测模块可独立修）

| 编号 | 现象 | 为什么本轮没修 | 建议 |
|---|---|---|---|
| **测试顺序/环境污染**（阻断全量 `pytest tests/`） | 全量跑 `pytest tests/` 时**失败用例每次轮换**，且**未改动的 `main` 上同样失败**。实测对照：`pytest tests/unit` 全绿；`pytest tests/`（integration 先跑）在不同次分别挂在 `test_paper_live` / `test_api_data_tasks` / `test_api_data_admin`；错误形态多为 `DataQualityError` 与「期望空库却读到真实行」（如 `test_version_latest_empty` 读到 `data_version=20261002.1`）。**本地 pre-push 钩子跑的就是这条命令，所以它对所有人都是红的** | 测试隔离缺陷，非本次改动引入（已用 pristine `main` 复现）。疑似机制：部分 fixture 只 monkeypatch `catalog.writer/reader`，而部分代码路径直接按 `get_settings()` 解析库/湖路径（`get_settings` 是 lru_cache，且路径按 CWD 解析）→ 跨文件 chdir/缓存残留后读到真实库；另有测试改动 `os.chdir` 但未完全还原 | 统一「库/湖路径」的注入方式：让所有测试走同一个 fixture（monkeypatch `get_settings` 的路径字段而非仅 `catalog`），或在 session 级 fixture 里强制清 `get_settings` 缓存 + 还原 CWD；给 `pytest tests/` 加 `-p no:randomly` 无用（本项目未装），需从注入层解决 |
| **退市股无行情**（性存者偏差的真正来源） | 引擎侧的退市核销已修（D8），但**日线湖里 339 只退市标的的日线一行都没有**（实测：`security` 有 339 条 `delist_date`，其中在 `data/parquet/daily` 出现过的 = **0**）。所以退市股永远不会进入持仓 → 长回测仍然只含「活下来的股票」，**幸存者偏差依旧存在**，且引擎修好也测不出来 | 数据层回填，不是引擎问题 | 回填退市标的（含退市前 N 年）的日线；回填后 D8 的核销逻辑才会真正被走到（单测已锁定该分支） |
| B5 | 参数扫描未向量化：`/api/backtests/sweep` 端点已在，但仍逐档调用事件引擎 | 属性能工程，不影响正确性 | 见 `docs/BACKTEST_ENGINES.md` 行动项 |
| 对账重跑 | backtrader 对账的 3 个任务（baseline_multifactor / momentum_rotation / grid_trading）**当前无法重跑** —— 依赖的 510300.SH / 159915.SZ 在湖里只有 2026 年起的数据。其中 2 个历史上是 ❌ FAIL，因此既没变好也没变坏 | 数据缺口，不是代码问题 | 先回填这两只 ETF 的 2024 起日线，再跑 `scripts/backtest_validation/validate_accuracy.py`（详见 `docs/BACKTEST_VALIDATION.md`） |


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
7. **get_fundamentals 前缀对齐(P1)**:财务表前缀已对齐真实数据
   (income→income、balance→balancesheet),六表全白名单 FIELD_MAP
   (growth/operation 借 indicator 前缀 yoy 尾段),未知字段显式 ValueError。
   属 G7 历史背景的延伸,未单列新缺口编号。
