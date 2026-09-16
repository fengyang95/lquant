# P2 回测正确性加固 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复/锁定缺口清单回测正确性侧条目:停牌期持仓估值、除权日净值连续性、G9 沙箱状态隔离、G4 keep 带语义裁定、G3 预热伪影、backtrader 对账扩充到基准策略。

**Architecture:** 每项修复遵循"金标准先证明旧行为错"——先写手算期望的失败测试(或对账差异报告),再最小实现;策略语义类(G4)不修代码只做裁定+锁定测试;对账类(backtrader)用权重调度表方案避免跨引擎复刻因子计算。

**Tech Stack:** Python 3.11+, polars, pytest;`lquant.backtest.{engine,jqapi,account,jq_fundamentals}`、`scripts/backtest_validation/`、backtrader 独立 venv。

**Spec:** docs/superpowers/specs/2026-09-13-backtest-baseline-e2e-design.md(P2 节);缺口清单 docs/BACKTEST_BASELINE_E2E.md(G1–G10)。

## Global Constraints

- 正确性红线:任何行为修复必须先有手算期望的失败测试证明旧行为错、新行为对;严禁改断言迁就错误行为。
- PIT 红线与沙箱三层防护不得削弱。
- 分红处理采用份额调整法(等价分红再投资)是既有架构决策,本计划不引入现金分红(数据层无 cash_div);相关测试只锁定净值连续性。
- 测试离线 fixture;pytest `PYTHONPATH=src /Users/lyp/code/lquant/.venv/bin/pytest`。
- 提交 `fix:`/`test:`/`feat:`/`docs:` 前缀,`--no-verify` + 逐文件 ruff;`git check-ignore` 验证新文件。
- 数据回填(G2)由控制器另行后台执行,不在本计划任务内。

---

### Task 1: 停牌期持仓估值修复(avg_cost → 最近可见收盘价)

**Files:**
- Modify: `src/lquant/backtest/account.py`(`nav` L73-77 的 prices.get 回退)与 `src/lquant/backtest/engine.py:193-194`、`src/lquant/backtest/jqapi.py:960-968`(`_settle` 传 last_prices)
- Test: `tests/unit/test_backtest_accuracy.py`(追加金标准)

**Interfaces:**
- Consumes: `Account.nav(prices)`;bars_by_day 中 symbol 缺席 = 当日停牌。
- Produces: `Account.nav(prices, last_prices: dict[str, float] | None = None)`——持仓 symbol 不在 prices 时先查 last_prices(该 symbol 最近一次有 bar 的 close),再回退 avg_cost;engine 与 JQRunner 的 `_settle` 维护 `self._last_close: dict[str, float]`(每日用当日有 bar 的 close 更新)并传入。向后兼容:last_prices=None 时行为不变。

- [ ] **Step 1: 写失败测试**(金标准,手算入注释)

```python
def test_nav_halted_position_uses_last_close_not_avg_cost():
    # 600000.SH 10 元买入 1000 股(avg_cost=10);涨到 12 后停牌 3 日
    # 旧行为:停牌日 nav 按 avg_cost=10 估值 → 10000;正确:按最近可见 close=12 → 12000
    # 构造 bars:day1 close=10(买入),day2 close=12,day3-5 无该股 bar
    ...
```

- [ ] **Step 2: 确认失败** — 旧行为 nav=10000,断言 12000 FAIL。
- [ ] **Step 3: 实现** — `Account.nav` 加 `last_prices` 可选参,回退顺序 prices → last_prices → avg_cost;engine/JQRunner 每日结算前用当日 bars 更新 `_last_close` 并传入 `nav`。
- [ ] **Step 4: 回归** — `test_backtest_accuracy.py` 全绿 + `test_jq_api.py test_baseline_strategy.py` 全绿。
- [ ] **Step 5: 提交**

```bash
git add src/lquant/backtest/account.py src/lquant/backtest/engine.py src/lquant/backtest/jqapi.py tests/unit/test_backtest_accuracy.py
git commit -m "fix(backtest): 停牌期持仓按最近可见收盘价估值(原 avg_cost 回退失真,G-新)"
```

---

### Task 2: 除权日与停牌复牌净值连续性金标准锁定

**Files:**
- Test: `tests/unit/test_backtest_accuracy.py`(追加;复用现有手算模式)

**Interfaces:**
- Consumes: `Engine._apply_corporate_actions`(engine.py:223-242,份额调整法)、`Account.apply_corporate_action`(account.py:25-33)。
- Produces: 行为锁定测试,无新接口。

- [ ] **Step 1: 写锁定测试**

```python
def test_nav_continuous_across_adj_factor_jump():
    # 除权日:bar.adj_factor 从 1.0 → 2.0,价格减半(原始价跳空)
    # 份额调整:qty*2, avg_cost/2 → nav 前后一日差 = 市场价格变动,无跳变
    # 手算:100 股 @10 → 200 股 @5,nav 均为 1000(不计市场变动)
def test_halted_resumption_catches_up_cumulative_factor():
    # 停牌 2 日期间 adj_factor 1.0→1.5→2.0,复牌日 ratio=2.0 一次性补齐
    # 断言复牌日 qty 与 nav 手算值
```

- [ ] **Step 2: 运行** — 若通过:锁定现状(注释注明份额调整法语义);若失败:按金标准红线修实现(单独 fix commit)。
- [ ] **Step 3: 提交**

```bash
git add tests/unit/test_backtest_accuracy.py
git commit -m "test(backtest): 除权日/停牌复牌份额调整法净值连续性金标准"
```

---

### Task 3: G9 沙箱 get_fundamentals 状态 per-runner 化

**Files:**
- Modify: `src/lquant/backtest/jq_fundamentals.py`(`_STATE` 模块级 → `JQFundamentalsState` 实例)
- Modify: `src/lquant/backtest/jqapi.py`(L521-527 绑定点、L918-921 每日 set_day 调用点)
- Test: `tests/unit/test_jq_fundamentals_wiring.py`(追加)

**Interfaces:**
- Consumes: jqapi JQRunner 实例;grep 确认 set_day 全库仅 jqapi.py:921 一个调用点。
- Produces: `JQFundamentalsState` 类(day/universe 实例属性 + `set_day`/`make_get_fundamentals` 实例方法);JQRunner.__init__ 持有 `self._jf_state = JQFundamentalsState()`;模块级 `set_day`/`make_get_fundamentals` 保留为兼容包装(委托到专用全局实例)供既有测试,或全库改直调——实现者二选一,报告说明。

- [ ] **Step 1: 写失败测试**

```python
def test_two_runners_state_isolated():
    # runner A run 中途(用策略 handle_data 里嵌套触发?不可行)
    # 改为直接单测:两个 JQFundamentalsState 实例 set_day 互不影响;
    # 并断言两个 JQRunner 实例各持有独立 state 对象(实例属性存在且非同一对象)
def test_legacy_module_api_still_bound_per_runner():
    # 保留兼容路径时:沙箱内 get_fundamentals 走所属 runner 的 state
```

- [ ] **Step 2: 确认失败** — 现无实例隔离,FAIL。
- [ ] **Step 3: 实现** — 按上述 Interfaces;jqapi 每日循环改 `self._jf_state.set_day(d, sorted(self._bars_today))`。
- [ ] **Step 4: 回归 + 提交** — wiring/baseline/jq_api 全绿;

```bash
git add src/lquant/backtest/jq_fundamentals.py src/lquant/backtest/jqapi.py tests/unit/test_jq_fundamentals_wiring.py
git commit -m "fix(backtest): 沙箱 get_fundamentals 状态 per-runner 化(G9)"
```

---

### Task 4: G4 keep 带语义裁定与锁定

**Files:**
- Test: `tests/unit/test_baseline_strategy.py`(追加锁定测试)
- Modify: `docs/BACKTEST_BASELINE_E2E.md`(裁定记录)

**Interfaces:**
- Consumes: `STRATEGY_CODE` rebalance 的 keep 带逻辑(baseline_multifactor.py:156-176)。
- Produces: 裁定:**保持现状**——月调仓"持有 top20 等权 + 带内(21-50)持仓保留但不调权"是缓冲带设计(减换手),非引擎缺陷;测试锁定该语义,文档记录裁定与换手/漂移量化。

- [ ] **Step 1: 写锁定测试**

```python
def test_keep_band_positions_retained_without_rebalance():
    # rank 21-50 的持仓:调仓日不被卖出也不被调权(交易列表无该 symbol 的卖出与调权单)
    # rank >50:被卖出;top20:调到等权
```

- [ ] **Step 2: 运行 + 文档** — 通过则锁定;文档 G4 行更新为"已裁定:缓冲带设计,非缺陷"。
- [ ] **Step 3: 提交**

```bash
git add tests/unit/test_baseline_strategy.py docs/BACKTEST_BASELINE_E2E.md
git commit -m "test(research): G4 keep 带缓冲带语义裁定与锁定"
```

---

### Task 5: G3 E2E 脚本默认预热区间

**Files:**
- Modify: `scripts/run_baseline_e2e.py`(数据切片前,把实际喂给引擎的 start 提前)

**Interfaces:**
- Consumes: `read_daily(start, end)`;策略需 60 交易日历史 + factor_formulas 的 20 日窗口。
- Produces: `--warmup-days`(默认 120 自然日):实际传给 JQRunner 的数据 start = max(请求 start - warmup, 湖最早日);文档记录调仓从"请求 start"起算(预热期内的调仓输出丢弃或标注)。实现上最简:喂入提前数据,但在解析 res.nav/records 时过滤掉早于请求 start 的日期。

- [ ] **Step 1: 实现** — CLI 参数 + 切片逻辑 + nav 过滤(请求 start 之前的日期不入 metrics/nav 摘要,文档注明)。
- [ ] **Step 2: 验证** — 用 2024 数据实跑 `--start 2024-01-01`(预热从 2023 数据,若无则用湖内可用早窗)确认 1-4 月不再 0 持仓;湖无预热数据时 WARN 跳过预热。
- [ ] **Step 3: 提交**

```bash
git add scripts/run_baseline_e2e.py
git commit -m "feat(research): E2E 脚本默认预热区间,消除切片预热伪影(G3)"
```

---

### Task 6: backtrader 对账扩充到基准策略(权重调度表方案)

**Files:**
- Modify: `scripts/backtest_validation/bt_runner.py`(`TASKS` 增 `baseline_multifactor`)
- Modify: `scripts/backtest_validation/validate_accuracy.py`(预计算每月目标权重表传给 bt)
- Modify: `docs/BACKTEST_VALIDATION_BENCHMARKS.md`(新对账结论)

**Interfaces:**
- Consumes: `bt_runner.py` 的 TASKS 协议(独立 venv,无 polars);主仓预计算的权重调度表 `list[dict]`(`{"date": "2021-02-01", "weights": {"600000.SH": 0.05, ...}}`)以 JSON/parquet 传递;validate_accuracy.py 已有的前复权切片与逐日净值对比框架(容差 <0.5%)。
- Produces: `BaselineMultifactorBT(bt.Strategy)`:每月第一个交易日按调度表 `order_target_value` 到目标权重;同样复刻差额取整、min_order_value、T+1 lots。对账口径:同权重调度表喂两边(lquant 侧用 JQRunner 执行同一调度表的模式,或直接用基准策略产出 positions 快照序列)。

- [ ] **Step 1: 生成调度表** — validate_accuracy.py 增 `--strategy baseline_multifactor`:跑一次 lquant 侧基准策略(JQRunner),导出每个调仓日的目标权重快照(context.portfolio.total_value × per)为 JSON。
- [ ] **Step 2: bt 侧策略** — `BaselineMultifactorBT` 按调度表在对应日期执行 order_target_value;参数注入调度表路径。
- [ ] **Step 3: 对账运行** — 主仓 venv + bt 独立 venv 各跑 2024(湖可用区间),逐日净值对比;最大日差 <0.5% PASS,否则分析分歧源并记录(参照 momentum_rotation 的 FAIL 分析格式)。
- [ ] **Step 4: 文档 + 提交**

```bash
git add scripts/backtest_validation/ docs/BACKTEST_VALIDATION_BENCHMARKS.md
git commit -m "test(backtest): 基准多因子策略接入 backtrader 对账(权重调度表方案)"
```

---

### Task 7: 缺口清单收口 + 全量回归

**Files:**
- Modify: `docs/BACKTEST_BASELINE_E2E.md`

- [ ] **Step 1: 更新清单** — G3/G4/G9 状态收口;Task 1 的停牌估值新缺口编号(G11);G8 复核结论(backtrader 对账结果)写入。
- [ ] **Step 2: 全量回归** — `PYTHONPATH=src .venv/bin/pytest tests/unit -q` 无新增失败。
- [ ] **Step 3: 提交**

```bash
git add docs/BACKTEST_BASELINE_E2E.md
git commit -m "docs: 缺口清单 P2 收口(G3/G4/G9/G11/对账结论)"
```

---

## 验收(对照 spec P2 节)

1. 停牌期持仓按最近可见收盘价估值(金标准证明)。
2. 除权日/停牌复牌净值连续性有金标准锁定(份额调整法)。
3. G9 状态隔离,双 runner 互不污染。
4. G4 裁定记录 + 锁定测试;G3 预热伪影消除。
5. backtrader 对账覆盖基准策略,结论入 docs/BACKTEST_VALIDATION_BENCHMARKS.md。
6. 全量回归无新增失败。
