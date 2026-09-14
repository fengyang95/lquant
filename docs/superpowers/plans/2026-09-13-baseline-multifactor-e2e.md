# P0 基准多因子策略端到端验收 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 JQRunner 上跑通一个聚宽风格的基本面+技术多因子月调仓策略(2021–2024 真实数据),产出缺口清单作为 P1–P3 的输入。

**Architecture:** 先把 `get_fundamentals` 接入 JQRunner 沙箱(现状未绑定),再写基准策略脚本(纯 Python 常量 CODE + 评分函数,可单测),合成小样本单测验证语义(PIT/剔除/调仓/T+1),最后真实数据 E2E 脚本产出运行记录与缺口清单。

**Tech Stack:** Python 3.11+, polars, pandas, duckdb, pytest;现有 lquant 包(`lquant.backtest.jqapi.JQRunner`、`lquant.research.dialect.fundamentals`、`lquant.data.store`)。

**Spec:** `docs/superpowers/specs/2026-09-13-backtest-baseline-e2e-design.md`(本计划只覆盖 spec 的 P0;P1–P3 待 P0 缺口清单出来后各自出计划)

## Global Constraints

- PIT 红线:基本面取数必须 pub_date <= t;单测必须含防前视断言。
- 2021-01-01 至 2024-12-31,月度调仓,top 20 等权,跌出 top 50 卖出。
- JQRunner 无 start/end 参数——日期区间由传入数据决定;`price_mode` 不经 JQRunner 配置(其撮合固定开盘 bucket + PctSlippage(0.0005),set_slippage/set_order_cost 可覆盖)。
- 测试不打真实网络;数据一律 fixture(临时 duckdb + 临时 parquet)。
- 仓库已知坑:ruff 全仓基线违规挡 pre-commit,提交用 `--no-verify` + 对新改文件逐个 `ruff check`;后台任务双模块打桩;新文件用 `git check-ignore` 验证未被本地 exclude 静默忽略。
- 每个任务:先写失败测试 → 最小实现 → 通过 → 提交(`feat:`/`test:`/`docs:` 前缀)。

## File Structure

- Create `src/lquant/backtest/jq_fundamentals.py` — JQRunner 沙箱内 `get_fundamentals` 的绑定桥(~60 行,与 jqapi 解耦)
- Modify `src/lquant/backtest/jqapi.py` — `_install_api()` 中注册 `ns["get_fundamentals"]`
- Create `research/strategies/baseline_multifactor.py` — 基准策略:策略源码常量 `STRATEGY_CODE` + 评分函数 `composite_score(df) -> pl.DataFrame`
- Create `tests/unit/test_jq_fundamentals_wiring.py` — 沙箱内 get_fundamentals 可用性 + PIT 断言
- Create `tests/unit/test_baseline_strategy.py` — 合成小样本全语义测试
- Create `scripts/run_baseline_e2e.py` — 真实数据 E2E:拉数、跑策略、写运行记录 + 缺口清单
- Create `docs/BACKTEST_BASELINE_E2E.md` — 运行记录 + 缺口清单(P1–P3 输入)

---

### Task 1: get_fundamentals 接入 JQRunner 沙箱

**Files:**
- Create: `src/lquant/backtest/jq_fundamentals.py`
- Modify: `src/lquant/backtest/jqapi.py`(`_install_api`,约 L409-531)
- Test: `tests/unit/test_jq_fundamentals_wiring.py`

**Interfaces:**
- Consumes: `lquant.research.dialect.fundamentals.resolve(query, day)`(经 `jq_shim.get_fundamentals`);jqapi 每个交易日执行策略前已知当日 `trade_date` 与股票池。
- Produces: 沙箱命名空间中的 `get_fundamentals(query, date=None)`;`date` 缺省绑定当日交易日;返回 pandas DataFrame(列含 `code`、字段名 snake_case;valuation 另有 `day`,财务表另有 `statDate`)。

- [ ] **Step 1: 写失败测试**(fixture 模板抄 `tests/unit/test_jq_shim_m6b.py` 的 `tmp_catalog` + `_seed_financial`;纯回测行情抄 `tests/unit/test_jq_api.py` 的 `make_df`)

```python
# tests/unit/test_jq_fundamentals_wiring.py
from datetime import date
import polars as pl
import pytest

duckdb = pytest.importorskip("duckdb")

CODE = '''
def initialize(context):
    run_monthly(pick, time="open")

def pick(context):
    df = get_fundamentals(query(income.net_profit))
    record(n_picked=len(df))
    order_value("600000.SH", 100000)
'''

def _make_df():
    # 6 个交易日、两只股票;抄 tests/unit/test_jq_api.py::make_df 的构造方式
    ...

def test_strategy_can_call_get_fundamentals(tmp_path, monkeypatch):
    # 1) monkeypatch catalog writer/reader + parquet._root(抄 tmp_catalog/_bars 模板)
    # 2) _seed_financial([{symbol, stat_date, pub_date, report_type, item="profit.netProfit", value}])
    # 3) res = JQRunner(CODE, initial_cash=1_000_000).run(_make_df())
    # 断言:
    assert res.error is None
    assert any(r["n_picked"] > 0 for r in res.records)   # 策略内确实查到了财务行
```

- [ ] **Step 2: 运行确认失败** — `pytest tests/unit/test_jq_fundamentals_wiring.py -v`;预期 FAIL:沙箱报 `NameError: name 'get_fundamentals' is not defined`。
- [ ] **Step 3: 实现** — `jq_fundamentals.py` 提供一个闭包工厂,持有"当前交易日"状态;jqapi 在每日执行策略前更新它,并在 `_install_api` 注册:

```python
# src/lquant/backtest/jq_fundamentals.py
"""JQRunner 沙箱内 get_fundamentals 的绑定桥。

复用 research/dialect 的 resolve(),按当日交易日绑定 jq_shim 上下文,
保证 financial_pit 的 pub_date <= 当日 PIT 过滤不被绕过。
"""
from __future__ import annotations
from typing import Any

_STATE: dict[str, Any] = {"day": None, "universe": []}


def set_day(day, universe: list[str]) -> None:
    _STATE["day"] = day
    _STATE["universe"] = list(universe)


def make_get_fundamentals():
    from lquant.research.dialect import jq_shim

    def get_fundamentals(query, date=None):
        day = date if date is not None else _STATE["day"]
        if day is None:
            raise RuntimeError("get_fundamentals: 沙箱未绑定当前交易日")
        jq_shim.bind(jq_shim.JQContext(engine=None, trade_date=day,
                                       universe=list(_STATE["universe"])))
        return jq_shim.get_fundamentals(query, date=day)

    return get_fundamentals
```

`jqapi.py` `_install_api` 中注册:`ns["get_fundamentals"] = make_get_fundamentals()`(import 放函数内,避免模块级循环依赖);找到每日循环里执行策略调用前的事件点,调用 `set_day(trade_date, universe)`(universe 用当日 bar 集合的 symbol 列表)。

- [ ] **Step 4: 跑通** — `pytest tests/unit/test_jq_fundamentals_wiring.py -v` PASS;并跑 `pytest tests/unit/test_jq_api.py tests/unit/test_jq_shim_m6b.py -v` 确认无回归。
- [ ] **Step 5: 提交**

```bash
git add src/lquant/backtest/jq_fundamentals.py src/lquant/backtest/jqapi.py tests/unit/test_jq_fundamentals_wiring.py
git commit -m "feat(backtest): JQRunner 沙箱接入 get_fundamentals(PIT 语义复用 resolve)"
```

---

### Task 2: 基准策略模块(源码常量 + 评分函数)

**Files:**
- Create: `research/strategies/baseline_multifactor.py`
- Test: `tests/unit/test_baseline_strategy.py`

**Interfaces:**
- Consumes: 沙箱 `get_fundamentals(query(...))`、`get_factor_values`(JQRunner `factor_formulas`)、`order_target_value`、`run_monthly`、`context.portfolio.positions`。
- Produces: `STRATEGY_CODE: str`(完整聚宽风格策略源码);`composite_score(pe: pl.DataFrame, yoy: pl.DataFrame, mom: pl.DataFrame, vol: pl.DataFrame) -> pl.DataFrame`(列 `code, score`,按 score 降序;NaN 因子行:缺失因子置 0 继续合成)。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_baseline_strategy.py
import polars as pl
from research.strategies.baseline_multifactor import composite_score

def test_composite_score_orders_and_tolerates_nan():
    pe = pl.DataFrame({"code": ["A", "B", "C", "D"], "pe": [10.0, 20.0, 5.0, None]})
    yoy = pl.DataFrame({"code": ["A", "B", "C", "D"], "yoy": [0.1, 0.3, 0.2, 0.0]})
    mom = pl.DataFrame({"code": ["A", "B", "C", "D"], "mom": [0.05, -0.02, 0.01, 0.0]})
    vol = pl.DataFrame({"code": ["A", "B", "C", "D"], "vol": [0.2, 0.4, 0.3, 0.1]})
    out = composite_score(pe, yoy, mom, vol)
    assert set(out["code"]) == {"A", "B", "C", "D"}
    assert out["score"].is_sorted(descending=True)
    # D 缺 pe:pe 项按 0 计,不整行丢弃
    assert out.filter(pl.col("code") == "D")["score"][0] != 0.0
```

- [ ] **Step 2: 确认失败** — `pytest tests/unit/test_baseline_strategy.py -v`,FAIL:模块不存在。
- [ ] **Step 3: 实现评分函数与策略源码**

```python
# research/strategies/baseline_multifactor.py(核心部分示意;导入路径以仓库为准)
import polars as pl

FACTOR_FORMULAS = ["pct_change_20", "rolling_std_20"]
TOP_N, EXIT_N = 20, 50

def _zscore(s: pl.Series) -> pl.Series:
    std = s.std()
    return (s - s.mean()) / std if std and std > 0 else s * 0.0

def composite_score(pe, yoy, mom, vol) -> pl.DataFrame:
    df = pe.join(yoy, on="code", how="outer").join(mom, on="code", how="outer").join(vol, on="code", how="outer")
    for c in ("pe", "yoy", "mom", "vol"):
        df = df.with_columns(pl.col(c).fill_null(0.0))
        df = df.with_columns(pl.col(c).alias(f"z_{c}"))  # 实现:_zscore 后回填
    df = df.with_columns(
        ((-pl.col("z_pe")) + pl.col("z_yoy") + pl.col("z_mom") - pl.col("z_vol")).alias("score")
    ).sort("score", descending=True)
    return df.select("code", "score")
```

`STRATEGY_CODE` 结构(语义,逐条落实):

```python
STRATEGY_CODE = '''
def initialize(context):
    set_order_cost(type="stock", open_tax=0, close_tax=0.001, open_commission=0.0003,
                   close_commission=0.0003, min_commission=5)
    run_monthly(rebalance, time="open")

def rebalance(context):
    # 1) 基本面:pe_ratio 取倒数视角由 score 内 -z_pe 实现;净利润同比查 growth 表
    df = get_fundamentals(query(valuation.pe_ratio, growth.<净利润同比 item>))
    # 2) 技术面:get_factor_values 取 pct_change_20 / rolling_std_20
    # 3) 剔除:ST(名称或 security 表标记)、当日停牌(当日无 bar)、上市不满 60 日
    # 4) composite_score 合成 → top 20 等权 order_target_value;持仓跌出 top 50 卖出
    # 5) record() 记录持仓数、总市值
'''
```

- [ ] **Step 4: 跑通** — `pytest tests/unit/test_baseline_strategy.py -v` PASS。
- [ ] **Step 5: 提交**

```bash
git add research/strategies/baseline_multifactor.py tests/unit/test_baseline_strategy.py
git commit -m "feat(research): 基准多因子策略(基本面 pe+净利同比 × 技术 20 日动量/波动)"
```

---

### Task 3: 合成小样本全语义单测

**Files:**
- Test: `tests/unit/test_baseline_strategy.py`(追加)

**Interfaces:**
- Consumes: Task 1 的沙箱 `get_fundamentals`;Task 2 的 `STRATEGY_CODE`;`tests/unit/test_jq_shim_m6b.py` 的 `tmp_catalog`/`_seed_financial` fixture 模板;`tests/unit/test_jq_api.py` 的 `make_df` 构造模式。
- Produces: 无对外接口;此测试是 P2 回测正确性差异的回归基线。

- [ ] **Step 1: 写测试**(每条断言先失败观察,再调整实现或确认为已知缺口并记入缺口清单)

```python
def test_baseline_pit_no_lookahead(tmp_catalog, _bars):
    # pub_date 晚于调仓日的财报不可见:6-30 调仓只能看到 pub_date<=6-30 的行
    ...

def test_baseline_excludes_halted_and_st(tmp_catalog, _bars):
    # 当日无 bar 的股票不得进入持仓;security 表 is_st 标记剔除
    ...

def test_baseline_monthly_topn_exit_rule(tmp_catalog, _bars):
    # 月初调仓买 top N;次月分数跌出 exit N 的持仓被卖;名次中间的保留不动
    ...

def test_baseline_t_plus_one_fill(tmp_catalog, _bars):
    # 调仓信号日下单 → 次日开盘成交;当日净值不包含未成交仓位
    ...
```

每个测试的完整实现由执行者按上述 fixture 模板展开:构造 4 只股票(不同 pe/yoy/动量)、把调仓日设在已知财报 pub_date 之后/之前各一例、用 `res.positions` 与 `res.trades` 断言。允许的失败模式:实现正确但暴露引擎/兼容层缺口 → 记入缺口清单,测试标记 `@pytest.mark.xfail(reason="gap: <编号>", strict=True)`,严禁改断言迁就错误行为。

- [ ] **Step 2: 全量跑** — `pytest tests/unit/test_baseline_strategy.py tests/unit/test_jq_fundamentals_wiring.py -v` 通过(含 xfail)。
- [ ] **Step 3: 提交**

```bash
git add tests/unit/test_baseline_strategy.py
git commit -m "test(research): 基准策略 PIT/剔除/调仓/T+1 语义单测"
```

---

### Task 4: 真实数据 E2E 运行脚本

**Files:**
- Create: `scripts/run_baseline_e2e.py`
- Create: `docs/BACKTEST_BASELINE_E2E.md`(由脚本生成 + 人工补注)

**Interfaces:**
- Consumes: `lquant.data.store.parquet.read_daily(start=..., end=...)` → `pl.LazyFrame`(需 collect);Task 2 的 `STRATEGY_CODE`;Task 1 的 JQRunner 接线。
- Produces: `python scripts/run_baseline_e2e.py [--start 2021-01-01 --end 2024-12-31]` → 打印 metrics、写 `docs/BACKTEST_BASELINE_E2E.md`(运行参数、nav 摘要、trades/rejected 统计、metrics、缺口清单)。

- [ ] **Step 1: 数据可用性预检**(脚本第一步,不满足则报错退出并提示先跑回填)

```python
from lquant.data.store import parquet as store
from lquant.data.store import catalog

lake = store.read_daily(start=START, end=END).select("symbol").head(1).collect()
if lake.is_empty():
    raise SystemExit("日线湖 2021-2024 无数据:先执行 lq data daily 回填")
with catalog.reader() as con:
    n = con.execute(
        "SELECT count(*) FROM financial_pit WHERE pub_date BETWEEN ? AND ?",
        [START, END]).fetchone()[0]
if n == 0:
    raise SystemExit("financial_pit 无数据:先执行 lq data financial --all")
```

- [ ] **Step 2: 实现 E2E 脚本** — 读数(剔除北交所 `symbol.endswith(".BJ")` 可选)、按日期区间切片、`JQRunner(STRATEGY_CODE, initial_cash=10_000_000, factor_formulas=FACTOR_FORMULAS).run(df)`,`res.error` 非空即失败退出;输出 metrics 与 nav 曲线摘要;把运行记录写成 `docs/BACKTEST_BASELINE_E2E.md`(含参数、时间戳、git commit、metrics 表、rejected 分布)。运行前确认 item 名:用 `SELECT DISTINCT item FROM financial_pit WHERE item LIKE 'growth.%' LIMIT 50` 选定净利润同比的真实 item 名,回填到 `STRATEGY_CODE` 与 Task 2 测试。
- [ ] **Step 3: 运行并记录** — 实跑 2021–2024,把输出落盘;记录运行耗时与内存(如可行)。
- [ ] **Step 4: 提交**

```bash
git add scripts/run_baseline_e2e.py docs/BACKTEST_BASELINE_E2E.md
git commit -m "feat(research): 基准策略真实数据 E2E 脚本 + 运行记录"
```

---

### Task 5: 缺口清单定稿

**Files:**
- Modify: `docs/BACKTEST_BASELINE_E2E.md`

**Interfaces:**
- Produces: 缺口清单表(编号 G1..Gn | 现象 | 归属:兼容面/回测正确性/数据层 | 复现 | 建议 P1/P2 归属)。

- [ ] **Step 1: 汇总** — 从 Task 3 的 xfail 标记、Task 4 实跑的 error/rejected/metrics 异常、过程中所有"语义与聚宽预期不符"的点,整理成清单表;每条给复现方式(测试名或脚本参数)。
- [ ] **Step 2: 分类归属** — 按 P1(兼容面)/P2(回测正确性)/数据层标注;明确每条"是否阻塞基准策略验收"。
- [ ] **Step 3: 提交**

```bash
git add docs/BACKTEST_BASELINE_E2E.md
git commit -m "docs: 基准策略 E2E 缺口清单(P1-P3 输入)"
```

---

## 验收(对照 spec)

1. 基准策略 2021–2024 在 JQRunner 完整跑通,`res.error is None`,无前视(Task 3 PIT 断言)、无静默吞错。
2. 所有已知缺口显式记录于 `docs/BACKTEST_BASELINE_E2E.md`,不修的项写明理由。
3. 后续计划:P1(兼容面补齐)、P2(回测正确性加固)依据缺口清单各自开计划。
