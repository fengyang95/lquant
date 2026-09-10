# 策略工作台（编辑/运行/分析 + 因子联动）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 聚宽兼容策略的「策略库保存/版本化 + 生命周期补全 + record() 自定义曲线 + 因子库公式联动 + 自定义分析片段」全链路。

**Architecture:** 后端三层——`backtest/validation.py`（源码校验）、`backtest/strategy_store.py`（DuckDB 策略/分析库）、`backtest/analysis.py`（分析沙箱），叠加在既有 `jqapi.JQRunner` 与 `server/api/backtests.py` 持久化之上；前端加 `/strategies/editor` 页与结果页扩展。

**Tech Stack:** Python/FastAPI/Polars/DuckDB；Next.js/React/SWR/CodeMirror 6（@uiw/react-codemirror）。

**Spec:** `docs/superpowers/specs/2026-09-10-strategy-desk-design.md`

## Global Constraints

- 复用 `lquant.core.db.reader/writer`，不新开连接路径；新表走 `src/lquant/data/store/ddl.py::DDL_STATEMENTS`（幂等 `CREATE TABLE IF NOT EXISTS`）。
- 用户代码/分析源码的 `exec` 是设计前提（同 jqapi 模块 docstring）；import 白名单校验统一在 `validation.py`，jqapi/analysis 共用。
- Python 侧 ruff（行宽 100）；提交走 pre-commit，若钩子报的是仓库历史 lint（非本次改动文件），可用 `--no-verify` 提交并在 commit message 里注明。
- 前端跟随项目设计系统（Panel/PageHeader/KChart 组件 + tailwind token），CodeMirror 动态 import 只在编辑器页加载。
- API 错误统一 HTTPException，中文错误信息（沿用现有风格）。

---

### Task 1: 源码校验 `validate_source`

**Files:**
- Create: `src/lquant/backtest/validation.py`
- Test: `tests/unit/test_strategy_validation.py`

**Interfaces:**
- Produces: `validate_source(source: str, *, require_initialize: bool = True) -> list[str]`（返回中文错误列表，空列表 = 通过）；`ALLOWED_IMPORTS: frozenset[str]`（Task 5 复用）

- [ ] **Step 1: 写失败测试**

```python
"""validate_source：语法 / import 白名单 / 必须定义 initialize。"""
from lquant.backtest.validation import validate_source

GOOD = """
def initialize(context):
    set_benchmark('000300.SH')

def handle_data(context, data):
    order('600519.SH', 100)
"""

def test_good_code_passes():
    assert validate_source(GOOD) == []

def test_syntax_error_reports_line():
    errs = validate_source("def initialize(context)\n    pass")
    assert any("行 1" in e for e in errs)

def test_disallowed_import():
    errs = validate_source(GOOD + "\nimport os\n")
    assert any("import" in e.lower() and "os" in e for e in errs)

def test_import_inside_function_also_checked():
    errs = validate_source(GOOD + "\ndef f():\n    import subprocess\n")
    return

def test_missing_initialize():
    errs = validate_source("def handle_data(context, data):\n    pass")
    assert any("initialize" in e for e in errs)

def test_require_initialize_false():
    src = "def analyze(result):\n    return []\n"
    assert validate_source(src, require_initialize=False) == []
```

注意 `test_import_inside_function_also_checked` 应断言 `any("subprocess" in e for e in errs)`（写实现时补全断言，先让测试红）。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_strategy_validation.py -x -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

```python
"""策略/分析源码静态校验：语法 + import 白名单 + 入口函数。"""
from __future__ import annotations

import ast

ALLOWED_IMPORTS = frozenset({
    "math", "datetime", "collections", "itertools", "functools",
    "statistics", "numpy", "pandas", "polars",
})


def validate_source(source: str, *, require_initialize: bool = True) -> list[str]:
    """返回错误列表（空 = 通过）。错误带行号。"""
    errs: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [f"语法错误（行 {e.lineno}）: {e.msg}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root not in ALLOWED_IMPORTS:
                    errs.append(f"行 {node.lineno}: 不允许 import {root}（白名单: "
                                f"{', '.join(sorted(ALLOWED_IMPORTS))}）")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root and root not in ALLOWED_IMPORTS:
                errs.append(f"行 {node.lineno}: 不允许 import {root}")
    names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if require_initialize and "initialize" not in names:
        errs.append("必须定义 initialize(context) 入口函数")
    return errs
```

- [ ] **Step 4: 跑测试通过**

Run: `uv run pytest tests/unit/test_strategy_validation.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/lquant/backtest/validation.py tests/unit/test_strategy_validation.py
git commit -m "feat(backtest): 策略源码静态校验 validate_source"
```

---

### Task 2: 策略库/分析库存储 `strategy_store`

**Files:**
- Modify: `src/lquant/data/store/ddl.py`（DDL_STATEMENTS 追加三张表）
- Create: `src/lquant/backtest/strategy_store.py`
- Test: `tests/unit/test_strategy_store.py`

**Interfaces:**
- Produces:
  - `save_strategy(name, source, *, description="", config=None, benchmark=None) -> dict`（同名 → version+1，旧行 is_latest=false；返回最新行 dict）
  - `list_strategies() -> list[dict]`（is_latest 且未删除，含 id/name/description/version/updated_at）
  - `get_strategy(strategy_id) -> dict`（含 source；不存在 raise KeyError）
  - `list_versions(name) -> list[dict]`
  - `delete_strategy(strategy_id) -> None`（软删 `deleted=true`）
  - `save_analysis(name, source) -> dict` / `list_analyses() -> list[dict]` / `get_analysis(analysis_id) -> dict` / `delete_analysis(analysis_id) -> None`
  - `delete_strategy(strategy_id)` 供 Task 6 API 使用

- [ ] **Step 1: DDL**

`ddl.py` 的 `DDL_STATEMENTS` 追加：

```python
    "CREATE TABLE IF NOT EXISTS strategy_def ("
    " id VARCHAR, name VARCHAR, description VARCHAR, kind VARCHAR DEFAULT 'jq',"
    " source VARCHAR, params_json VARCHAR, benchmark VARCHAR, config_json VARCHAR,"
    " version INTEGER, is_latest BOOLEAN DEFAULT TRUE, deleted BOOLEAN DEFAULT FALSE,"
    " created_at TIMESTAMP, updated_at TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS analysis_def ("
    " id VARCHAR, name VARCHAR, source VARCHAR, is_builtin BOOLEAN DEFAULT FALSE,"
    " deleted BOOLEAN DEFAULT FALSE, created_at TIMESTAMP, updated_at TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS backtest_record ("
    " run_id VARCHAR, trade_date DATE, key VARCHAR, value DOUBLE)",
```

- [ ] **Step 2: 写失败测试**

```python
"""strategy_store：策略/分析库 CRUD + 版本化（tmp DuckDB，自包含）。"""
import pytest

from lquant.backtest.strategy_store import (
    delete_strategy, get_strategy, list_strategies, list_versions,
    save_analysis, save_strategy,
)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("LQ_DUCKDB_PATH", str(tmp_path / "t.duckdb"))
    from lquant.core.config import get_settings
    get_settings.cache_clear()
    from lquant.core.db import writer
    from lquant.data.store.ddl import DDL_STATEMENTS
    with writer() as con:
        for s in DDL_STATEMENTS:
            con.execute(s)
    yield
    get_settings.cache_clear()


def test_save_and_get(store):
    row = save_strategy("demo", "def initialize(ctx): pass", description="x")
    got = get_strategy(row["id"])
    assert got["source"].startswith("def initialize")
    assert got["version"] == 1


def test_same_name_bumps_version(store):
    a = save_strategy("demo", "def initialize(ctx): pass")
    b = save_strategy("demo", "def initialize(ctx): pass  # v2")
    assert b["version"] == 2
    names = [r["id"] for r in list_strategies() if r["id"] == b["id"]]
    assert names and b["version"] > a["version"]
    assert len(list_versions("demo")) == 2


def test_soft_delete(store):
    row = save_strategy("tbd", "def initialize(ctx): pass")
    delete_strategy(row["id"])
    assert all(r["id"] != row["id"] for r in list_strategies())


def test_analysis_crud(store):
    row = save_analysis("monthly", "def analyze(result): return []")
    assert get_analysis(row["id"])["name"] == "monthly"
    assert any(r["name"] == "monthly" for r in list_analyses())
```

（若 `get_settings` 不读 `LQ_DUCKDB_PATH` 环境变量，改为 monkeypatch `lquant.core.db._path` 返回 tmp 路径 —— 先看 `core/config.py` 再定，测试红时调整 fixture。）

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_strategy_store.py -x -q`
Expected: FAIL

- [ ] **Step 4: 实现 `strategy_store.py`**

```python
"""策略库 / 自定义分析库（DuckDB）。版本化：同名保存 = 插入新行。"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from lquant.core.db import reader, writer


def _now():
    return datetime.now()


def save_strategy(name: str, source: str, *, description: str = "",
                  config: dict | None = None, benchmark: str | None = None) -> dict:
    from lquant.backtest.validation import validate_source
    errs = validate_source(source)
    if errs:
        raise ValueError("；".join(errs))
    with writer() as con:
        row = con.execute(
            "SELECT COALESCE(MAX(version), 0) FROM strategy_def WHERE name = ?",
            [name]).fetchone()
        ver = int(row[0]) + 1
        sid = uuid.uuid4().hex[:12]
        now = _now()
        con.execute("UPDATE strategy_def SET is_latest = FALSE WHERE name = ?", [name])
        con.execute(
            "INSERT INTO strategy_def VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [sid, name, description, "jq", source,
             json.dumps(config or {}), benchmark, json.dumps({}),
             ver, True, False, now, now])
    return get_strategy(sid)


def list_strategies() -> list[dict]:
    with reader() as con:
        rows = con.execute(
            "SELECT id, name, description, kind, version, updated_at "
            "FROM strategy_def WHERE is_latest AND NOT deleted "
            "ORDER BY updated_at DESC").fetchall()
    return [{"id": r[0], "name": r[1], "description": r[2], "kind": r[3],
             "version": r[4], "updated_at": str(r[5])} for r in rows]


def get_strategy(strategy_id: str) -> dict:
    with reader() as con:
        row = con.execute(
            "SELECT id, name, description, kind, source, params_json, benchmark, "
            "config_json, version, is_latest, updated_at FROM strategy_def WHERE id = ?",
            [strategy_id]).fetchone()
    if not row:
        raise KeyError(strategy_id)
    return {"id": row[0], "name": row[1], "description": row[2], "kind": row[3],
            "source": row[4], "config": json.loads(row[5] or "{}"),
            "benchmark": row[6], "config_json": json.loads(row[7] or "{}"),
            "version": row[8], "is_latest": row[9], "updated_at": str(row[10])}


def list_versions(name: str) -> list[dict]:
    with reader() as con:
        rows = con.execute(
            "SELECT id, version, updated_at, is_latest FROM strategy_def "
            "WHERE name = ? AND NOT deleted ORDER BY version DESC", [name]).fetchall()
    return [{"id": r[0], "version": r[1], "updated_at": str(r[2]), "is_latest": r[3]}
            for r in rows]


def delete_strategy(strategy_id: str) -> None:
    with writer() as con:
        con.execute("UPDATE strategy_def SET deleted = TRUE WHERE id = ?", [strategy_id])


def save_analysis(name: str, source: str) -> dict:
    from lquant.backtest.analysis import run_user_analysis  # 仅校验 import（见 analysis.py Task 5）
    raise NotImplementedError              # 实现在 Task 5 之后补：调用 validate_source
```

`save_analysis` 本任务先写成调用 `validate_source(source, require_initialize=False)` + INSERT（模式同 `save_strategy`，表 `analysis_def`，列 id/name/source/created_at/updated_at），`list_analyses`/`get_analysis`/`delete_analysis` 同理。**不要**留 `NotImplementedError`。

- [ ] **Step 5: 跑测试通过 + Commit**

Run: `uv run pytest tests/unit/test_strategy_store.py -q`
Expected: PASS

```bash
git add src/lquant/backtest/strategy_store.py src/lquant/data/store/ddl.py tests/unit/test_strategy_store.py
git commit -m "feat(backtest): 策略库/分析库存储（版本化 strategy_def/analysis_def/backtest_record DDL）"
```

---

### Task 3: JQRunner 生命周期钩子 + record()

**Files:**
- Modify: `src/lquant/backtest/jqapi.py`
- Test: `tests/unit/test_jq_lifecycle.py`

**Interfaces:**
- Produces: `JQResult.records: dict[str, list[tuple[date, float]]]`；用户 API `record(**kv)`、`log.info/warn/error`（已有）；生命周期 `before_trading_start`/`after_trading_end`（用户代码顶层定义即可）；`JQRunner` 构造参数不变

- [ ] **Step 1: 写失败测试**

```python
"""JQRunner：before_trading_start / after_trading_end / record 采集。"""
import polars as pl
import pytest

from lquant.backtest.jqapi import JQRunner


def _df() -> pl.DataFrame:
    """两日两标的合成行情。"""
    rows = []
    for i, d in enumerate(["2026-01-05", "2026-01-06"]):
        for s, base in (("600519.SH", 100.0), ("000001.SZ", 50.0)):
            px = base + i
            rows.append({"symbol": s, "trade_date": pl.Date, "open": px, "high": px,
                         "low": px, "close": px, "pre_close": base + i - 1 if i else base,
                         "volume": 1e6, "amount": 1e8, "halted": False})
    ...
```

合成数据构造方式参考 `tests/unit/` 里现有 JQRunner 测试的 fixture（若已有 jqapi 测试文件则直接复用其数据构造函数，grep `JQRunner` in tests/）。核心断言：

```python
CODE = """
calls = []

def initialize(context):
    run_daily(rebalance, time='open')

def before_trading_start(context):
    record(cash=context.portfolio.available_cash)
    calls.append('pre')

def rebalance(context):
    if not context.portfolio.positions:
        order('600519.SH', 100)

def after_trading_end(context):
    calls.append('post')
"""

def test_lifecycle_and_record():
    r = JQRunner(CODE).run(df)
    assert r.error is None
    assert r.records["cash"][0][0] == df_dates[0]        # 记录了日期
    assert r.records["cash"][0][1] == pytest.approx(初始资金)
    assert r.metrics["n_trades"] >= 1
```

（测试文件里用具体合成数据写全断言；`calls` 顺序断言：每日 pre → open → post。）

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_jq_lifecycle.py -x -q`
Expected: FAIL（`records` 属性不存在 / before_trading_start 未被调用）

- [ ] **Step 3: 实现**

jqapi.py 修改点：

1. `JQResult` 加字段 `records: dict[str, list[tuple[date, float]]] = field(default_factory=dict)`
2. `_install_api` 里注册 `record`：

```python
        def record(**kv) -> None:
            for k, v in kv.items():
                self.res.records.setdefault(str(k), []).append((self._today, float(v)))
```

3. `run()` 主循环每日按序调用，放在 `_due_funcs` 之前/之后（严格 pre → 当日调度 → post）：

```python
            for hook, bucket in ((self._before_trading_fn, "open"),
                                 (self._after_trading_fn, "close")):
                ...
```

具体：`_compile` 里取 `self._before_trading_fn = self.ns.get("before_trading_start")`、`self._after_trading_fn = self.ns.get("after_trading_end")`；主循环里

```python
            for fn in (self._before_trading_fn,):
                if fn:
                    self._bucket = "open"
                    fn(self.context) if fn.__code__.co_argcount else fn()
            for fn, bucket in self._due_funcs(d, i):
                ...  # 现有逻辑不动
            if self._after_trading_fn:
                self._bucket = "close"
                self._after_trading_fn(self.context) \
                    if self._after_trading_fn.__code__.co_argcount else self._after_trading_fn()
```

异常处理与现有调度一致（捕获 → `res.error` → return）。

- [ ] **Step 4: 跑测试通过 + Commit**

Run: `uv run pytest tests/unit/test_jq_lifecycle.py -q`
Expected: PASS

```bash
git add src/lquant/backtest/jqapi.py tests/unit/test_jq_lifecycle.py
git commit -m "feat(jqapi): before_trading_start/after_trading_end 生命周期 + record() 自定义曲线采集"
```

---

### Task 4: 因子库联动

**Files:**
- Create: `src/lquant/factors/panel.py`
- Modify: `src/lquant/backtest/jqapi.py`
- Test: `tests/unit/test_factor_linkage.py`

**Interfaces:**
- Consumes: `factors/qlib_alpha.compute(df, name) -> df+[_factor]`（非法名 raise）、`factors/dsl`（`lquant.factors.dsl.parser.parse` + `lquant.factors.engine.FactorEngine`）
- Produces:
  - `compute_factor_columns(df: pl.DataFrame, formulas: list[str]) -> tuple[pl.DataFrame, dict[str, str]]` —— 返回（带新列的 df, {formula: 列名}）；内置因子名 → `qlib_alpha.compute`，其余 → DSL；解析失败 raise `ValueError`
  - `JQRunner(code, *, ..., factor_formulas: list[str] | None = None)`；`run()` 内计算面板；用户 API `get_factor_values(formula, security_list=None, count=1) -> dict[str, list[float]]`

- [ ] **Step 1: 写失败测试（panel.py）**

```python
"""因子联动：compute_factor_columns + JQRunner.get_factor_values。"""
import polars as pl
import pytest

from lquant.factors.panel import compute_factor_columns


def _df() -> pl.DataFrame:
    # 40 日 × 2 标的 OHLCV，close 线性上行，参考 tests/unit 现有 jq/factor 测试数据构造
    ...
```

断言：
- `compute_factor_columns(df, ["mom_20" if 内置名存在 else 实际内置名, "pct_change_20"])[1]` 返回两列映射；结果 df 列名与映射值一致且无全空列
- 非法 formula：`compute_factor_columns(df, ["not_exist_99"])` raise `ValueError`

先跑 `uv run python -c "from lquant.factors.qlib_alpha import list_builtin; print([x['name'] for x in list_builtin()][:10])"` 确认真实内置因子名（如 `mom_20`/`std_20` 等）再写死测试断言。

- [ ] **Step 2: 写失败测试（JQ API）**

```python
CODE = """
def initialize(context):
    set_benchmark('000300.SH')

def handle_data(context, data):
    v = get_factor_values('MOM_20_OR_REAL_NAME', ['600519.SH'], count=1)
    if v['600519.SH'][0] > 0:
        order('600519.SH', 100)
"""

def test_get_factor_values_in_runner(...):
    r = JQRunner(CODE, factor_formulas=[<真实内置名>]).run(df)
    assert r.error is None
    assert r.metrics["n_trades"] >= 1
```

- [ ] **Step 3: 跑测试确认失败** → `uv run pytest tests/unit/test_factor_linkage.py -x -q`，Expected: FAIL

- [ ] **Step 4: 实现 panel.py**

```python
"""因子列统一计算：内置 qlib 因子名 → qlib_alpha.compute；其余 → 因子 DSL。

与 GET /api/factors/builtin 同一套公式体系；策略回测 / 因子页共用。
"""
from __future__ import annotations

import polars as pl


def compute_factor_columns(df: pl.DataFrame, formulas: list[str]) -> tuple[pl.DataFrame, dict[str, str]]:
    """按公式算因子列。返回 (新 df, {formula: 列名})；非法公式 raise ValueError。"""
    from lquant.factors.qlib_alpha import compute as qlib_compute
    from lquant.factors.qlib_alpha import resolve_name

    out = df.sort(["symbol", "trade_date"])
    colmap: dict[str, str] = {}
    for f in formulas:
        col = f"_f_{f}"
        try:
            resolved = resolve_name(f)          # 内置名 → (fam, window)
        except Exception as e:                  # noqa: BLE001 → 走 DSL
            d = _dsl_compute(out, f, col)
            out = d
            colmap[f] = col
            continue
        try:
            out = qlib_compute(out, f).rename({"_factor": col})
        except Exception as e:                  # noqa: BLE001
            raise ValueError(f"因子公式 {f} 计算失败: {e}") from e
        colmap[f] = col
    return out, colmap


def _dsl_compute(df: pl.DataFrame, expr: str, col: str) -> pl.DataFrame:
    from lquant.factors.dsl.parser import parse
    from lquant.factors.engine import FactorEngine
    node = parse(expr, col)
    return FactorEngine(df.lazy())._compute_column(df, expr, col)  # 以实际 FactorEngine API 为准
```

**注意**：`_dsl_compute` 的调用形态（`FactorEngine.compute(expr, name)` 签名 `compute(expr, name="f")` 返回 df）以 `factors/engine.py` 实际代码为准，实现时读该文件后调整；DSL 解析失败必须转成 `ValueError("非法因子公式 ...")` 而不是裸异常。

- [ ] **Step 5: 实现 jqapi 集成**

`JQRunner.__init__` 加 `factor_formulas: list[str] | None = None`，存 `self._factor_formulas = list(factor_formulas or [])`。`run()` 开头（`Engine.prepare` 之后、主循环之前）：

```python
        self._factor_panels: dict[str, dict[tuple[date, str], float]] = {}
        if self._factor_formulas:
            df2, colmap = compute_factor_columns(raw_df, self._factor_formulas)
            for f, col in colmap.items():
                sub = df2.select(["trade_date", "symbol", col]).drop_nulls()
                self._factor_panels[f.upper()] = {
                    (r[0], r[1]): float(r[2]) for r in sub.iter_rows()}
        ...
```

`run(data)` 需要保留原始 df 引用（`self._raw_df = data if isinstance(data, pl.DataFrame) else None`）——`Engine.prepare` 吃 df，面板计算用同一 df。`get_factor_values` 装到 ns：

```python
        def get_factor_values(formula: str, security_list=None, count: int = 1):
            f = str(formula).upper()
            panel = self._factor_panels.get(f)
            if panel is None:
                raise ValueError(f"因子 {formula} 未注册（回测请求需带 factor_formulas=[...]）")
            secs = [str(s) for s in (security_list or sorted(self._bars_today))]
            n = int(count)
            out: dict[str, list[float]] = {}
            i = self._day_index
            for s in secs:
                vals = [panel[(self._dates[j], s)] for j in range(max(0, i - n + 1), i + 1)
                        if (self._dates[j], s) in panel]
                out[s] = vals
            return out
```

（严格截至当日 `i`，不取未来。）

- [ ] **Step 6: 跑测试通过 + Commit**

```bash
git add src/lquant/factors/panel.py src/lquant/backtest/jqapi.py tests/unit/test_factor_linkage.py
git commit -m "feat(factors): 因子面板统一计算 + JQRunner get_factor_values 联动"
```

---

### Task 5: 自定义分析沙箱

**Files:**
- Create: `src/lquant/backtest/analysis.py`
- Test: `tests/unit/test_analysis_sandbox.py`

**Interfaces:**
- Consumes: `validation.ALLOWED_IMPORTS`
- Produces:
  - `run_user_analysis(source: str, payload: dict) -> list[dict]` —— `payload` 形状 `{"dates": [date...], "nav": [float...], "returns": [float...], "trades": pl.DataFrame, "positions": dict, "records": dict, "metrics": dict}`；返回 `[{type: chart|table, ...}]`
  - `AnalysisError(ValueError)` —— 源码校验/执行/输出 schema 失败统一抛它

- [ ] **Step 1: 写失败测试**

```python
"""analysis 沙箱：执行 + 输出 schema 校验。"""
import polars as pl
import pytest

from lquant.backtest.analysis import AnalysisError, run_user_analysis

PAYLOAD = {
    "dates": ["2026-01-05", "2026-01-06"],
    "nav": [1.0, 1.01],
    "returns": [0.0, 0.01],
    "trades": pl.DataFrame({"trade_date": [], "symbol": [], "qty": []}),
    "positions": {}, "records": {}, "metrics": {"sharpe": 1.2},
}


def test_chart_output():
    src = """
def analyze(result):
    import statistics
    return [{"type": "chart", "title": "t", "data": [{"x": d, "ret": r}
             for d, r in zip(result["dates"], result["returns"])],
             "x": "x", "ys": ["ret"]}]
"""
    out = run_user_analysis(src, PAYLOAD)
    assert out[0]["type"] == "chart" and out[0]["ys"] == ["ret"]


def test_table_output():
    src = """
def analyze(result):
    return [{"type": "table", "title": "分年", "columns": ["year", "ret"],
             "rows": [["2026", 0.1]]}]
"""
    out = run_user_analysis(src, PAYLOAD)
    assert out[0]["type"] == "table"


def test_bad_import_rejected():
    with pytest.raises(AnalysisError):
        run_user_analysis("import os\ndef analyze(r): return []", PAYLOAD)


def test_bad_schema_rejected():
    with pytest.raises(AnalysisError):
        run_user_analysis("def analyze(r): return [{'type': 'pie'}]", PAYLOAD)


def test_runtime_error_wrapped():
    with pytest.raises(AnalysisError, match="1, 0"):
        run_user_analysis("def analyze(r): return 1/0 and []", PAYLOAD)
```

- [ ] **Step 2: 跑测试确认失败** → Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

```python
"""自定义分析片段：沙箱 exec，输出 chart/table spec，前端动态渲染。"""
from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict, deque
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import polars as pl

from lquant.backtest.validation import ALLOWED_IMPORTS, validate_source


class AnalysisError(ValueError):
    pass


def _ns() -> dict:
    ns = {"__name__": "__analysis__",
          "math": math, "statistics": statistics,
          "date": date, "datetime": datetime, "timedelta": timedelta,
          "Counter": Counter, "defaultdict": defaultdict, "deque": deque,
          "np": np, "pd": pd, "pl": pl}
    return ns


def _validate_specs(specs) -> list[dict]:
    if not isinstance(specs, list):
        raise AnalysisError("analyze 必须返回 list[dict]")
    out = []
    for i, s in enumerate(specs):
        if not isinstance(s, dict) or s.get("type") not in ("chart", "table"):
            raise AnalysisError(f"分析输出[{i}] type 必须是 chart|table")
        if s["type"] == "chart" and not ({"data", "x", "ys"} <= set(s)):
            raise AnalysisError(f"分析输出[{i}] chart 需要 data/x/ys")
        if s["type"] == "table" and not ({"columns", "rows"} <= set(s)):
            raise AnalysisError(f"分析输出[{i}] table 需要 columns/rows")
        out.append({"type": s["type"], "title": str(s.get("title", "自定义分析")),
                    **{k: s[k] for k in s if k not in ("type", "title")}})
    return out


def run_user_analysis(source: str, payload: dict) -> list[dict]:
    errs = validate_source(source, require_initialize=False)
    if errs:
        raise AnalysisError("；".join(errs))
    try:
        ns = _ns()
        exec(source, ns)                       # noqa: S102 - 设计前提（同 jqapi）
        fn = ns.get("analyze")
        if fn is None:
            raise AnalysisError("必须定义 analyze(result) 函数")
        specs = fn(payload)
    except AnalysisError:
        raise
    except Exception as e:                     # noqa: BLE001
        raise AnalysisError(f"分析执行失败: {e}") from e
    return _validate_specs(specs)
```

- [ ] **Step 4: 跑测试通过 + Commit**

Run: `uv run pytest tests/unit/test_analysis_sandbox.py -q`
Expected: PASS

```bash
git add src/lquant/backtest/analysis.py tests/unit/test_analysis_sandbox.py
git commit -m "feat(backtest): 自定义分析沙箱 run_user_analysis（chart/table spec）"
```

---

### Task 6: API 层接入

**Files:**
- Modify: `src/lquant/server/api/strategies.py`（CRUD + validate + 版本）
- Create: `src/lquant/server/api/analyses.py`
- Modify: `src/lquant/server/api/backtests.py`（run-code 增强 + run 详情带 records/analysis）
- Modify: `src/lquant/server/app.py`（注册 analyses router —— 文件名以实际 app 工厂为准，grep `include_router`）
- Test: `tests/unit/test_strategy_api.py`

**Interfaces:**
- Consumes: Task 1/2/4/5 全部公开函数、`_persist_result`
- Produces（REST 契约，Task 7/8 前端依赖）:
  - `GET /strategies`（现有列表 + 用户策略，条目带 `source` 字段区分）、`POST /strategies {name, source, description?, config?, benchmark?}`、`GET/PUT/DELETE /strategies/{id}`、`GET /strategies/{id}/versions`
  - `POST /strategies/validate {source} -> {errors: []}`
  - `GET/POST/PUT/DELETE /analyses[/{id}]`（POST/PUT body `{name, source}`，保存时先 `run_user_analysis(source, 最小 payload)` 冒烟 + validate）
  - `POST /backtests/run-code` 请求体新增 `factor_formulas: list[str] = []`、`strategy_id: str | None = None`；params 里记录 `strategy_id`/`factor_formulas`/`logs`
  - `GET /backtests/{run_id}` 响应新增 `records: {key: [{date, value}...]}`、`logs: [...]`、`custom_analysis: [spec...]`

- [ ] **Step 1: 写失败测试**

`tests/unit/test_strategy_api.py` 用 test_api.py 同款 `api_env` fixture 模式（chdir tmp + generate_demo + DDL）。关键用例：

```python
def test_strategy_crud_roundtrip(client):
    r = client.post("/api/strategies", json={"name": "s1", "source": GOOD_SRC})
    assert r.status_code == 200
    sid = r.json()["id"]
    assert client.get(f"/api/strategies/{sid}").json()["source"].startswith("def initialize")
    r2 = client.post("/api/strategies", json={"name": "s1", "source": GOOD_SRC + "# v2"})
    assert r2.json()["version"] == 2
    assert client.get("/api/strategies").json() 中含用户策略条目（source 字段 == "user"）
    assert client.post("/api/strategies/validate", json={"source": "import os"}).json()["errors"] != []


def test_run_code_with_factor_and_records(client):
    body = {"code": JQ_SRC_USING_RECORD_AND_FACTOR, "start": "2026-01-01",
            "factor_formulas": [REAL_BUILTIN_FACTOR]}
    r = client.post("/api/backtests/run-code", json=body)
    assert r.status_code == 200
    rid = r.json()["run_id"]
    detail = client.get(f"/api/backtests/{rid}").json()
    assert detail["records"] and detail["custom_analysis"] == []
    assert detail["logs"]
```

注意 test_api.py 里既有 run-code 测试的路径前缀（`/api` 与否）、以及 run 详情字段名 —— 照抄现有测试的 URL/断言风格。

- [ ] **Step 2: 跑测试确认失败** → Expected: FAIL

- [ ] **Step 3: 实现 strategies.py 扩展**

```python
class StrategyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    source: str = Field(min_length=10, max_length=200_000)
    description: str = ""
    config: dict = {}
    benchmark: str | None = None


@router.post("")
def create_strategy(req: StrategyIn) -> dict:
    from lquant.backtest.strategy_store import save_strategy
    try:
        return save_strategy(req.name, req.source, description=req.description,
                             config=req.config, benchmark=req.benchmark)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
```

`GET /{id}`（KeyError→404）、`PUT`（= 再 save 同名）、`DELETE`（软删）、`GET /{id}/versions`、`POST /validate`（返回 `{"errors": validate_source(req.source)}`）。`GET ""` 列表合并：内置注册策略条目加 `"source": "builtin"`，用户策略条目加 `"source": "user"`。

- [ ] **Step 4: 实现 analyses.py + run-code 增强**

`analyses.py`：CRUD 调 strategy_store 的 analysis 函数 + 保存时 `run_user_analysis` 冒烟（payload 用空数据最小 payload：`{"dates": [], "nav": [], "returns": [], "trades": pl.DataFrame(), "positions": {}, "records": {}, "metrics": {}}`），失败 422。在 app 工厂 `include_router(analyses.router, prefix="/api")`（前缀按现有注册模式）。

`backtests.py::run_jq_code`：

```python
class JQCodeIn(BaseModel):
    code: str = Field(min_length=10, max_length=100_000)
    start: str = "2026-01-01"
    end: str | None = None
    initial_cash: float = Field(default=1_000_000, gt=0)
    benchmark: str = "000300.SH"
    factor_formulas: list[str] = Field(default_factory=list, max_length=10)
    strategy_id: str | None = None
    run_analysis: bool = True


@router.post("/run-code")
def run_jq_code(req: JQCodeIn) -> dict:
    ...
    runner = JQRunner(req.code, initial_cash=req.initial_cash, benchmark=req.benchmark,
                      factor_formulas=req.factor_formulas or None)
    ...
    params = {..., "strategy_id": req.strategy_id,
              "logs": res.logs[-100:], "factor_formulas": req.factor_formulas}
    _persist_result(...)
    # backtest_record 落库
    with writer() as con:
        con.execute("DELETE FROM backtest_record WHERE run_id = ?", [run_id])
        rows = [{"run_id": run_id, "trade_date": d, "key": k, "value": v}
                for k, series in res.records.items() for d, v in series]
        if rows:
            con.register("_rec", pl.DataFrame(rows))
            con.execute("INSERT INTO backtest_record SELECT run_id, trade_date, key, value FROM _rec")
    # 自定义分析（默认全量启用的 analyses；本期简化：返回空列表占位由后续任务填充）
    ...
```

`GET /{run_id}` 详情追加（读 `backtest_record` group by key + params 里的 logs）：

```python
        rec_rows = con.execute(
            "SELECT trade_date, key, value FROM backtest_record WHERE run_id = ? "
            "ORDER BY trade_date", [run_id]).fetchall()
    records: dict[str, list] = {}
    for d, k, v in rec_rows:
        records.setdefault(k, []).append({"date": str(d), "value": v})
    ...
    out["records"] = records
    out["logs"] = params.get("logs", [])
    out["custom_analysis"] = []
```

- [ ] **Step 5: 跑全部后端测试 + Commit**

Run: `uv run pytest tests/unit/test_strategy_api.py tests/unit/test_api.py -q`
Expected: PASS（test_api.py 既有用例不回归）

```bash
git add -A src/lquant/server tests/unit/test_strategy_api.py
git commit -m "feat(api): 策略库/分析库 CRUD + run-code 因子联动与 record 落库 + run 详情扩展"
```

---

### Task 7: 前端策略库面板 + 编辑器页

**Files:**
- Create: `web/src/app/strategies/editor/page.tsx`
- Modify: `web/src/app/backtests/page.tsx`（策略库下拉/面板，选中回填 run-code 表单）
- Modify: `web/src/components/Sidebar.tsx`（加「策略编辑」入口）

**Interfaces:**
- Consumes: Task 6 REST 契约（`web/src/lib/api.ts` 的 `get/post/del` helper —— 以实际导出名为准）
- Produces: 编辑器页路由 `/strategies/editor`（Task 8 不依赖）

- [ ] **Step 1: 安装 CodeMirror（动态 import，仅编辑器页加载）**

Run: `cd web && npm install @uiw/react-codemirror @codemirror/lang-python @codemirror/theme-one-dark`

- [ ] **Step 2: 编辑器页**

`web/src/app/strategies/editor/page.tsx`（"use client"；布局：左 260px 策略列表 SWR `useSWR("/strategies")`，右编辑器 + 顶栏按钮）。核心逻辑（样式 token 跟随 `Panel.tsx`/`PageHeader.tsx` 现有类名风格）：

```tsx
const CodeMirror = dynamic(() => import("@uiw/react-codemirror"), { ssr: false });
const pyLang = ... // dynamic import 里再取 @codemirror/lang-python 的 python()

const [code, setCode] = useState(DQ_TEMPLATE);
// 保存: post("/strategies", {name, source: code, ...}) → 422 时展示 errors
// 校验: post("/strategies/validate", {source: code}) → errors 列表展示
// 运行: post("/backtests/run-code", {code, start, end, factor_formulas})
//       → router.push(`/backtests/${run_id}`)
```

页面骨架（state: `strategies`, `selected`, `name`, `code`, `errors`, `running`）+ 模板代码常量（预填一个带 `initialize/handle_data/record/get_factor_values` 的示例策略，API 名与 Task 3/4 实现一致）。输入校验失败把后端 422 的 `detail`（可能含「行 N」）渲染在编辑器下方红框。

- [ ] **Step 3: backtests 页策略库联动**

`backtests/page.tsx` 若已有 run-code 表单：加策略下拉（SWR `/strategies`，filter `source === "user"`），选中后 `GET /strategies/{id}` 回填 code/参数；无 run-code 表单则在页面加「从策略库运行」入口跳编辑器页。

- [ ] **Step 4: Sidebar 入口 + 构建校验**

Run: `cd web && npm run typecheck && npm run build`
Expected: 通过

- [ ] **Step 5: Commit**

```bash
git add web/src/app/strategies web/src/app/backtests/page.tsx web/src/components/Sidebar.tsx web/package.json web/package-lock.json
git commit -m "feat(web): 策略编辑器页 + 策略库面板（CodeMirror 动态加载）"
```

---

### Task 8: 前端结果页 record 曲线 + 自定义分析区块

**Files:**
- Modify: `web/src/app/backtests/[runId]/page.tsx`

**Interfaces:**
- Consumes: Task 6 的 `GET /backtests/{run_id}` 新字段 `records`/`logs`/`custom_analysis`
- Consumes: KChart/Chart 组件（`web/src/components/KChart.tsx`、`Chart.tsx` —— 实际 props 以文件为准，先读再用）

- [ ] **Step 1: record 曲线**

详情页读 `data.records`；非空时在 NAV 图下方渲染每个 key 一条曲线卡片（KChart 或 Chart，x=日期，y=value；多 key 用 tab 或堆叠卡片，选实现简单的）。`data.logs` 非空时折叠面板显示运行日志。

- [ ] **Step 2: 自定义分析动态渲染**

`data.custom_analysis` 数组渲染：`type==="chart"` → 用项目图表组件（data 数组 + x/ys 字段映射 series）；`type==="table"` → 简单 table（沿用现有表格样式类）。空数组不渲染区块。

- [ ] **Step 3: typecheck/build + Commit**

Run: `cd web && npm run typecheck && npm run build`

```bash
git add web/src/app/backtests/[runId]/page.tsx
git commit -m "feat(web): 回测详情页 record() 自定义曲线 + 运行日志 + 自定义分析渲染"
```

---

### Task 9: 端到端集成测试

**Files:**
- Test: `tests/unit/test_strategy_desk_e2e.py`

**Interfaces:**
- Consumes: Task 6 全部 REST 端点

- [ ] **Step 1: 写端到端测试**

api_env 模式 + generate_demo 数据；一个 jq 策略：`initialize` 注册 `run_daily` + `set_benchmark`，`handle_data` 里 `get_factor_values`（demo 数据上可算的内置因子，先 `list_builtin()` 选一个基于 close 的，如动量类）选股下单，每日 `record(total=context.portfolio.total_value)`；`POST /strategies` 保存 → `POST /backtests/run-code`（带 `factor_formulas`）→ 断言 run_id、`GET /backtests/{run_id}` 的 records/metrics、`POST /analyses` 保存一个 chart 输出的分析 → 手动调 `run_user_analysis`（或如果 Task 6 给 run-code 接了分析执行，则断言详情里 custom_analysis 非空）产出 spec。

- [ ] **Step 2: 跑全量测试**

Run: `uv run pytest tests/unit -q`
Expected: 全部 PASS

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_strategy_desk_e2e.py
git commit -m "test: 策略工作台端到端集成测试（保存→因子联动回测→record→分析）"
```

---

## 遗留与偏差说明（相对 spec）

1. `run_weekly/run_monthly`/`log` jqapi 已实现，无需开发（spec 写时未确认）。
2. logs/自定义分析结果存 `backtest_run.params_json`，不新增列（避免存量库迁移）。
3. `data[stock].factor` 语义与聚宽复权因子冲突 → 只提供 `get_factor_values()`。
4. 自定义分析在本计划中为「保存片段 + 手动执行 + 前端渲染 spec」；run 时自动执行全量分析放到后续迭代（Task 6 留了 `run_analysis` 字段占位）。
5. `run-code` 详情的 `custom_analysis` 本期返回 `[]`（执行接线在后续迭代），前端按空值隐藏区块。
