# 声明式多源数据接入机制 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 新数据源接入 = 一份字段映射 YAML 节 + 一个薄 fetch 类；baostock 迁移到引擎，akshare/tushare 完整接入，tickdb 预留。

**Architecture:** `schema.py` 保持唯一事实源；新增 `mapping.py`（YAML 映射加载/校验/用）与 `providers/_engine.py`（MappingProvider 基类：raw fetch → apply_mapping → coerce → assert → _post_normalize）。各源只实现取数，复杂源特有逻辑走 hook。

**Tech Stack:** Python 3.11+ / Polars / PyYAML / pytest；akshare、tushare SDK 在测试中 mock。

**Spec:** `docs/superpowers/specs/2026-09-09-multi-source-data-interface-design.md`

## Global Constraints

- schema.py 是唯一事实源；映射目标列必须 ∈ SCHEMAS[table]，否则 `MappingError`
- 网络调用沿用 watchdog/ratelimit；baostock 走模块级函数 + run_with_watchdog
- 单位统一：价格元、volume 股、amount 元；分钟线 ts = bar 结束时刻；60min 边界 11:00/14:00/15:00
- tushare token 走 env `TUSHARE_TOKEN`（providers.yaml env_key），缺失不报错、不入链
- Fallback/HealthTracker/quality 管线不动；测试用 pytest，SDK 一律 mock

---

### Task 1: mapping.py — 映射加载/校验/应用

**Files:**
- Create: `src/lquant/data/mapping.py`
- Modify: `src/lquant/core/errors.py`（加 `MappingError`）
- Test: `tests/unit/test_mapping.py`

**Interfaces (Produces):**
- `MappingError(LQuantError)` in core/errors.py
- `@dataclass(frozen=True) DeriveRule(expr: str, from_cols: tuple[str, ...])`
- `@dataclass(frozen=True) TableMapping(table, rename, derive, fill, required)`
- `load_table_mapping(table: str, source: str, config_dir: Path | None = None) -> TableMapping` — 读 `config/schema/<table>.yaml` 的 `sources.<source>` 节；缺节抛 MappingError
- `apply_mapping(df, tm, params: dict | None = None) -> pl.DataFrame` — rename→derive→fill（fill 可被 params 覆盖）；最终按 SCHEMAS[table] 列序 select，**schema 外列丢弃**
- `validate_table_config(table: str, path: Path) -> list[str]` — 错误列表，空 = 合法

**derive 求值器约束：** ast 白名单解析，允许 Name(列引用)、Constant、BinOp(+−×÷)、Call 仅 `concat`（→concat_str）与 `strptime(col, fmt)`（fmt 含 %H → to_datetime，否则 to_date）。其它节点抛 MappingError。不实现 str.slice 等（YAGNI，baostock 时间解析留在 fetch 层）。

- [ ] Step 1: 失败测试 `tests/unit/test_mapping.py`：rename/derive/fill 行为、fill 被 params 覆盖、schema 列序 select 且丢弃 schema 外列、缺 source 节抛 MappingError、validate 报未知目标列 / 非法表达式节点（`__class__` 属性访问）、validate 干净文件返回空
- [ ] Step 2: `pytest tests/unit/test_mapping.py -v` 确认 FAIL
- [ ] Step 3: 实现 errors.py MappingError + mapping.py（TableMapping/加载/ast 求值器/apply/validate）
- [ ] Step 4: 测试 PASS
- [ ] Step 5: Commit `feat: 数据源字段映射加载/校验/应用机制 mapping.py`

### Task 2: _engine.py — MappingProvider 基类

**Files:**
- Create: `src/lquant/data/providers/_engine.py`
- Test: `tests/unit/test_engine.py`

**Interfaces (Consumes/Produces):**
- Consumes Task 1: `load_table_mapping(table, source)` / `apply_mapping(df, tm, params)`
- Produces `class MappingProvider(DataProvider)`:
  - 子类设置 `name`、`source`（yaml sources key，默认 = name）
  - 抽象 `_fetch_raw(self, table: str, **params) -> pl.DataFrame`（返回源列名 df）
  - `_post_normalize(self, df, table) -> df`（默认原样返回）
  - `request(self, table: str, **params) -> pl.DataFrame`：
    `_fetch_raw`（params 含 `_raw` 时直接使用）→ `load_table_mapping` → `apply_mapping(raw, tm, params)` → `schema.coerce(df, table)` → daily/minute 表执行 `assert_plausible_prices` + `assert_ohlc` → `_post_normalize`
    params 中 `freq` / `sec_type` 自动注入 fill 覆盖

- [ ] Step 1: 失败测试 `tests/unit/test_engine.py`：FakeProvider（DAILY capability，_fetch_raw 返回源列名脏数据）→ `request("daily_bar")` 输出 amount=30000、sec_type=stock、close dtype Float64；负价数据触发断言异常；`_post_normalize` hook 生效（FakeProvider 覆写加一列验证）
- [ ] Step 2: 确认 FAIL
- [ ] Step 3: 实现 `_engine.py`
- [ ] Step 4: PASS
- [ ] Step 5: Commit `feat: MappingProvider 通用适配器引擎`

### Task 3: 映射 YAML + baostock 迁移

**Files:**
- Create: `config/schema/daily_bar.yaml`, `config/schema/minute_bar.yaml`
- Modify: `src/lquant/data/providers/baostock.py`（daily_bars/minute_bars 改走 engine；其余方法不动）
- Test: 现有测试回归

**Interfaces:**
- Consumes: Task 1/2 的 `request(table, _raw=...)` 路径
- Produces: baostock daily/minute 输出 schema 与迁移前一致；`config/schema/*.yaml` 的 baostock 节

**daily_bar.yaml `sources.baostock` 节：**
```yaml
table: daily_bar
sources:
  baostock:
    rename:
      date: trade_date
      code: symbol
      preclose: pre_close
      turn: turnover_rate
      tradestatus: trade_status
      isST: is_st
    derive:
      amount: {expr: "amount * 100", from: [amount]}   # 千元 → 元
    fill: {sec_type: stock, source: baostock, quality_flags: 0, adj_factor: 1.0}
    required: [symbol, trade_date, open, high, low, close, volume]
```
（注意：required 里 high/low/close 逐项列出，不缩写）

**minute_bar.yaml `sources.baostock` 节：**
```yaml
table: minute_bar
sources:
  baostock:
    rename: {code: symbol}
    fill: {freq: "5min", source: baostock, adj_factor: 1.0}   # freq 由 params 覆盖
    required: [symbol, ts, open, high, low, close, volume]
```
baostock 分钟线 time 列解析（YYYYmmddHHMMSSmmm → ts）、停牌过滤、年份切片留在 `_fetch_raw`/`_post_normalize`（60min 边界归一 11:30/14:00→11:00/14:00/15:00 在 `_post_normalize`）。

- [ ] Step 1: 写两份 yaml（daily 如上；minute 的 freq/source/adj_factor fill，freq 由 params 覆盖）
- [ ] Step 2: 迁移 BaoStockProvider 继承 MappingProvider，daily_bars/minute_bars 保留 watchdog fetch（模块级函数不动），列转换/单位换算/符号归一交给 engine；freq 经 params 注入
- [ ] Step 3: 回归 `pytest tests/unit -k "baostock or quality or golden or engine" -v`
- [ ] Step 4: Commit `refactor: baostock 日线/分钟线迁移到 MappingProvider 引擎`

### Task 4: akshare 完整接入

**Files:**
- Create: `src/lquant/data/providers/akshare.py`
- Modify: `config/schema/daily_bar.yaml`、`minute_bar.yaml`（加 akshare 节）
- Modify: `config/providers.yaml`（akshare note 更新，保持 enabled: false 供用户开启）
- Test: `tests/unit/test_providers.py` 追加

**Interfaces:**
- Consumes: Task 2 engine `request(table, _raw=..., freq=..., sec_type=...)`
- Produces: `AkShareProvider(MappingProvider)`，source="akshare"
- 能力集：`{DAILY, MINUTE_1, MINUTE_5, MINUTE_15, MINUTE_30, MINUTE_60, ADJ_FACTOR, REFERENCE, ETF_DAILY}`。
  **不声明** FINANCIAL_PIT（新浪财务指标接口无 pub_date，未来函数风险）；**不声明** CALENDAR（无干净接口）
- 接口对照：
  - DAILY → `stock_zh_a_hist`（列：日期/股票代码/开盘/收盘/最高/最低/成交量(手)/成交额(元)）
  - MINUTE_* → `stock_zh_a_hist_min_em(period=1/5/15/30/60)`（时间列字符串 → _fetch_raw 内 to_datetime）
  - ADJ_FACTOR → 前复权/不复权两次拉取相除（同 baostock 做法）
  - REFERENCE → `stock_info_a_code_name`
  - ETF_DAILY → `fund_etf_hist_em`，走 daily_bar 表 + params 注入 sec_type=etf

- [ ] Step 1: yaml 追加 akshare 节：daily rename {股票代码: symbol, 日期: trade_date, 开盘: open, 收盘: close, 最高: high, 最低: low}，derive volume*100（手→股），amount 已是元直接 rename；minute rename {股票代码: symbol}，时间列在 _fetch_raw 解析为 ts，volume*100，fill freq 由 params 覆盖
- [ ] Step 2: 失败测试（mock akshare SDK 模块）：daily 归一化正确（单位换算、列名）、minute freq 注入、securities 映射、ETF 走 daily_bar+sec_type=etf、akshare import 失败时 _import_all 吞掉不注册
- [ ] Step 3: 确认 FAIL
- [ ] Step 4: 实现 AkShareProvider（akshare 延迟导入；_import_all 已吞 ImportError）
- [ ] Step 5: PASS + Commit `feat: akshare 完整接入声明式映射机制`

### Task 5: tushare 完整接入

**Files:**
- Create: `src/lquant/data/providers/tushare.py`
- Modify: `config/schema/daily_bar.yaml`、`minute_bar.yaml`（加 tushare 节）
- Modify: `config/providers.yaml`（tushare 条目：`env_key: TUSHARE_TOKEN`，qps 按积分档位，enabled: true）
- Modify: `src/lquant/data/providers/__init__.py`（build_chain 尊重 env_key：env 缺失 → skip 该源不报错）
- Test: `tests/unit/test_providers.py` 追加

**Interfaces:**
- Produces: `TushareProvider(MappingProvider)`，source="tushare"
- 能力集：`{DAILY, MINUTE_1, MINUTE_5, MINUTE_15, MINUTE_30, MINUTE_60, ADJ_FACTOR, FINANCIAL_PIT, REFERENCE, CALENDAR, ETF_DAILY}`
- 接口对照：
  - DAILY → `pro.daily`（ts_code/trade_date/open/high/low/close/vol(手)/amount(千元)，ts_code 与内部格式一致）
  - MINUTE_* → `pro.stk_mins`（ts_code/freq 参数 1min/5min/15min/30min/60min；2000 积分门槛，不足时 SDK 报错 → 按现有 fallback 机制切源）
  - ADJ_FACTOR → `pro.adj_factor`（直接返回因子，无需相除）
  - FINANCIAL_PIT → `income`/`balancesheet`/`cashflow`（end_date + ann_date 双日期天然 PIT；宽表 → 长表拆分在 `_post_normalize`）
  - REFERENCE → `stock_basic`（list_date/list_status 天然含退市，幸存者偏差防护）
  - CALENDAR → `pro.trade_cal`（exchange=SSE, is_open）
  - ETF_DAILY → `fund_daily`
- env 门控：build_chain 中 `item.get("env_key")` 存在且 `os.environ` 缺失该变量 → skip 该源（logging.warning），不抛错

- [ ] Step 1: yaml 追加 tushare 节（daily: ts_code→symbol 直接 rename、vol*100 手→股、amount*1000 千元→元；minute: freq 参数翻译，vol/amount 换算）
- [ ] Step 2: 失败测试：token 缺失时 build_chain 跳过 tushare（monkeypatch os.environ）；mock ts.pro_api：daily 归一化（单位换算）、adj_factor 直出、financial_pit 宽表→长表（item=tushare.revenue 等前缀）、trade_cal 映射 is_open、能力集声明
- [ ] Step 3: 确认 FAIL
- [ ] Step 4: 实现 TushareProvider（tushare 延迟导入；_import_all 列表加 "tushare"）
- [ ] Step 5: PASS + Commit `feat: tushare 完整接入声明式映射机制`

### Task 6: 启动校验接线 + 三源对拍 + tickdb 预留

**Files:**
- Modify: `src/lquant/data/providers/__init__.py`（build_chain 内对每张已配置表调 validate_table_config，错误抛 MappingError）
- Create: `tests/unit/test_crosscheck_providers.py`
- Modify: `config/providers.yaml`（tickdb 预留条目 enabled: false + note）
- Modify: `src/lquant/data/providers/__init__.py`（_import_all 列表加 akshare/tushare）

**Interfaces:**
- Consumes: Task 1 `validate_table_config`、Task 3/4/5 的三份 yaml 节
- Produces: 启动时映射错误 fail-fast；三源对拍测试基建

- [ ] Step 1: 失败测试 test_crosscheck_providers.py：构造同一标的同一交易日的三份 mock 源数据（baostock 千元/akshare 手/tushare 千元 vol 手），各自按对应 yaml 节 apply_mapping 后 close/volume/amount 数值一致（tolerance 0），列集 ⊆ schema
- [ ] Step 2: 确认 FAIL
- [ ] Step 3: build_chain 接入 validate_table_config + _import_all 更新 + tickdb 预留 yaml 条目
- [ ] Step 4: PASS
- [ ] Step 5: 全量 `make test` + `ruff check` 收尾；Commit `feat: 启动校验接线与三源对拍测试`
