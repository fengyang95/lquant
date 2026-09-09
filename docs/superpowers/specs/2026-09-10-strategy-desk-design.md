# 策略工作台设计：策略编辑 · 运行 · 分析（含因子库联动）

日期：2026-09-10
状态：已确认（用户口头确认范围 A+B+C + 因子联动）

## 背景与目标

lquant 已有两条回测路径：原生 `Strategy` 子类（`Engine`）和聚宽兼容代码执行器
`JQRunner`（`src/lquant/backtest/jqapi.py`），并有 FastAPI 后端与 Next.js 前端
（运行表单 / 运行列表 / 结果详情 / 多次对比）。

对标聚宽，缺口在三件事：

1. **策略编辑**：代码只能一次性提交，没有策略库（保存/版本/管理）与编辑器体验。
2. **策略运行**：JQRunner 缺 `before_trading_start`/`after_trading_end`、
   `run_weekly`/`run_monthly`；无 `log`/`record` 输出；代码跑的结果未完整接入策略库与对比。
3. **策略分析**：指标固定，用户无法追加自定义分析图表/表格。

另有关键诉求：**策略数据与因子库联动** —— 策略内可直接使用因子库公式
（qlib_alpha 内置因子 + 因子 DSL）计算的因子值。

## 非目标（本期不做）

定时回测、模拟盘对接、`get_fundamentals` 基本面 API、研究 notebook、
多语言编辑器语法、策略市场/分享。

## 1. 策略库 + 编辑器（A）

### 存储

DuckDB 新表 `strategy_def`：

| 列 | 说明 |
|---|---|
| id | UUID 字符串 |
| name | 名称（同 name 多行为版本历史） |
| description | 描述 |
| kind | 固定 `jq`（本期只支持聚宽风格） |
| source | 策略源码 |
| params_json | 默认参数 |
| benchmark | 基准代码 |
| config_json | 回测配置（initial_cash、slippage、order_cost、rebalance 等） |
| version | 整型，同名内自增 |
| is_latest | 布尔 |
| created_at / updated_at | 时间戳 |

写路径：保存同名策略 = 插入新行（version+1，旧行 `is_latest=false`），天然保留版本历史；
`DELETE` 软删（`deleted=true` 列）。

### API（`server/api/strategies.py` 扩展）

- `GET /strategies` —— 现有注册策略 + 用户策略（加 `source: user|builtin` 字段区分）
- `POST /strategies` —— 创建/更新（同名则升版本）
- `GET /strategies/{id}` / `PUT /strategies/{id}` / `DELETE /strategies/{id}`
- `GET /strategies/{id}/versions` —— 版本列表
- `POST /strategies/validate` —— 校验，见下

### 校验（提交前）

`strategy_def` 保存与 `validate` 共用 `backtest/validation.py::validate_source()`：

1. `ast.parse` 语法校验（返回行号/列号的错误信息）
2. import 白名单：`math`、`datetime`、`collections`、`itertools`、`functools`、`statistics`、`numpy`、`pandas`、`polars`、本包相对导入禁止
3. 必须定义 `initialize`；`handle_data`/`before_trading_start`/`after_trading_end` 可选

### 前端

- `/backtests` 页：新增「策略库」面板（列表 + 选中后回填运行表单）
- 新页 `/strategies/editor`：左侧策略列表，右侧 CodeMirror 6（Python 语法、暗色主题，
  跟随项目设计系统 token），顶部「保存 / 校验 / 运行」按钮；
  「运行」调回测 API 后跳转结果页。

## 2. JQRunner 增强 + 因子联动（B）

### 生命周期

`jqapi.py` 调度器从 `run_daily` 单一注册表扩展为通用定时表：

- `before_trading_start` / `after_trading_end`：每日开盘前 / 收盘后钩子（按日循环调用）
- `run_daily(fn, time)`：保持现有行为
- `run_weekly(fn, weekday, time)` / `run_monthly(fn, trading_day_offset, time)`：
  按自然周/月判断触发日

### 输出采集

- `log.info/warn/error(msg)`：收集到 `JQResult.logs`（list[{level, msg, dt}]），
  持久化进 `backtest_run.logs_json`
- `record(key=value, ...)`：每日截面采集成 `{key: [(date, value), ...]}`，
  持久化到新表 `backtest_record`（run_id, date, key, value），
  结果页自动渲染为自定义曲线（复用现有双轴图组件）

### 因子联动（关键）

- 回测请求（`POST /backtests/run-code` 及新的策略运行入口）新增
  `factor_formulas: list[str]`
- 因子计算复用因子库既有管线：内置公式走 `factors/qlib_alpha.compute`，
  DSL 表达式走 `factors/engine.FactorEngine`（与 `GET /api/factors/builtin` 同一套公式体系），
  抽取为共用函数（当前 `server/api/backtests.py::_compute_factor` 与
  `server/api/factors.py::_compute_factor` 有重复，统一到一处）
- 计算出的因子面板按 (date, symbol) 注入 bar `fields`；策略内可用：
  - `get_factor_values(formula, security_list, count)` —— 聚宽同名 API，
    返回当前日往前 count 天各标的因子值 dict
  - `data[stock].factor` / `data[stock].mavg(*)` 等现有字段通道访问注入的因子列
- 校验前移：保存/运行前先解析 formula（内置因子表 + DSL parser），
  非法 formula 直接 422

## 3. 自定义分析（C）

### 分析片段

DuckDB 表 `analysis_def`（id, name, source, is_builtin, created_at, updated_at, deleted）。
用户源码约定：

```python
def analyze(result) -> list[dict]:
    # result 含: dates, nav, returns, trades(DataFrame), positions,
    #            records(dict[key->[(date, value)]]), metrics(dict)
    return [
        {"type": "chart", "title": "月度收益", "data": [{"month": "...", "ret": 0.1}],
         "x": "month", "ys": ["ret"]},
        {"type": "table", "title": "分年度指标", "columns": [...], "rows": [...]},
    ]
```

### 执行

`backtest/analysis.py`：

- `exec` 沙箱（与 jqapi 同一白名单机制；只读访问 result，提供受控的
  numpy/pandas/polars/math/statistics 命名空间）
- 输出 schema 校验（type ∈ chart|table；chart 必含 data/x/ys；table 必含 columns/rows），
  非法输出报错并保留其他合法片段
- 结果 JSON 存 `backtest_run.custom_analysis_json`

### API

- `POST /analyses` / `GET /analyses` / `PUT /analyses/{id}` / `DELETE /analyses/{id}`
  （新 `server/api/analyses.py`，复用 validate_source 机制）
- 回测详情读取时若 `custom_analysis_json` 存在则返回

### 前端

结果页新增「自定义分析」区块：按 chart/table 动态渲染；
`/strategies/editor` 或独立入口提供分析片段的管理与编辑（简单代码框即可）。

## 数据模型汇总（DuckDB 新表）

- `strategy_def`（版本化策略库）
- `analysis_def`（自定义分析片段）
- `backtest_record`（record() 自定义曲线）
- `backtest_run` 增列：`logs_json`、`custom_analysis_json`

## 测试

- 单测：`validate_source`（语法/白名单/缺 initialize）、JQRunner 生命周期
  （weekly/monthly 触发日）、record/log 采集、analysis 沙箱与 schema 校验、
  因子注入与 `get_factor_values`
- API 测试：策略库 CRUD + 版本、analyses CRUD、run-code 带 factor_formulas
- 集成：一个端到端 jq 策略 —— 用内置动量因子选股 + record 记曲线 + 自定义分析输出图表

## 风险

- DuckDB 并发写：沿用现有 `_persist_result` 的单连接写路径
- 沙箱逃逸：本期白名单 + import 禁断已覆盖常规场景，与 jqapi 保持同一套机制，
  后续如需更强隔离再引入子进程执行
- CodeMirror 体积：动态 import，仅编辑器页加载
