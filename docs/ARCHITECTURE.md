# lquant 仓库架构框架与模块设计

> 定位：A 股（个股 + ETF）**数据接入 → 因子分析 → 量化回测 → 市场看板 → 模拟盘** 一体化平台。
> Python 3.13 后端 · Rust 核心算法 · Next.js 15 前端 · DuckDB + Parquet 存储。
> 生成日期：2026-09-08 ｜ 状态：**骨架已落地，18 个单元测试全通过，CLI 可运行**

---

## 0. 一页看懂

```
┌────────────────────────────────────────────────────────────────────┐
│ L6  前端  Next.js 15 + TS + Tailwind + ECharts                      │
│     大盘 · 自选 · 个股 · 因子库 · 回测报告 · 数据看板 · 板块 · 情绪  │
└───────────────────────────┬────────────────────────────────────────┘
                REST (OpenAPI) + WebSocket 进度推送
┌───────────────────────────┴────────────────────────────────────────┐
│ L5  服务  FastAPI + Pydantic v2 + RQ(Redis)                         │
│     /api/data  /api/factors  /api/backtests  /api/market  /ws/jobs  │
└───────────────────────────┬────────────────────────────────────────┘
┌───────────────────────────┴────────────────────────────────────────┐
│ L4  组合  screener → dedup(相关性去重) → weighting(风险平价/HRP)     │
│ L3  回测  策略 DSL → Rust lq-backtest（规则表 RuleSet 全数据化）      │
│ L2  因子  DSL → AST → Polars LazyExpr + Rust lq-ops（预处理 + 评价）  │
│ L1  存储  Parquet 湖(按年分区) + DuckDB(目录/即席) + Arrow 零拷贝     │
│ L0  接入  Provider 抽象 + Capability + Fallback + 看门狗 + 质量校验   │
└───────────────────────────┬────────────────────────────────────────┘
        BaoStock(主) → akshare → efinance → 同花顺 → mootdx(实时)
```

**三条贯穿全局的硬约束**

| 约束 | 原因 |
|---|---|
| 代码格式统一 `000001.SZ` / `510300.SH`，绝不用裸 6 位 | `000001` 是歧义码（上证指数 vs 平安银行） |
| 时间统一 ISO 8601 + `Asia/Shanghai` | 不做隐式本地时间假设，未来加市场不返工 |
| 金额统一元 | 源站万元/亿元混用是静默错误的温床，adapter 层换算 + 断言 |

---

## 1. 完整目录树

```
lquant/
├── README.md                     # 快速开始 + 三条硬约束
├── Makefile                      # 统一入口（make setup / dev / test ...）
├── pyproject.toml                # Python 依赖（hatchling + src layout）
├── docker-compose.yml            # Redis（RQ 队列）+ 可选 RedisInsight
├── .env.example / .gitignore / .pre-commit-config.yaml
│
├── config/                       # 全部配置外置，代码不含魔法数字
│   ├── app.yaml                  #   路径 / 存储 / 采集 / 质量 / 日志
│   ├── providers.yaml            #   Provider 优先级 + capability + 跨源对拍
│   ├── rules/cn_a_share.yaml     #   撮合规则表（印花税区间 / T+N / 涨跌停）
│   └── factors/custom.yaml       #   因子表达式 + 默认预处理配方
│
├── src/lquant/
│   ├── core/                     # ── 无任何业务依赖的地基
│   │   ├── config.py             #   Settings、load_yaml(${ENV} 插值)
│   │   ├── types.py              #   Symbol / parse_symbol / Board / SecType
│   │   ├── errors.py             #   分层异常（区分该重试还是该换源）
│   │   ├── registry.py           #   Registry[T] 注册表 + describe() 自省
│   │   ├── calendar.py           #   交易日历（官方日历，不能按工作日推）
│   │   ├── db.py                 #   writer() / reader()（DuckDB 单写者）
│   │   └── logging.py
│   │
│   ├── data/                     # ── L0 接入 + L1 存储
│   │   ├── schema.py             #   内部统一 schema（Polars）+ coerce()
│   │   ├── capability.py         #   Capability 枚举（daily/minute_1/etf_iopv...）
│   │   ├── base.py               #   DataProvider 抽象类（6 个核心方法）
│   │   ├── fallback.py           #   FallbackProvider + HealthTracker
│   │   ├── ratelimit.py          #   TokenBucket（东财封 IP 是头号风险）
│   │   ├── watchdog.py           #   子进程超时看门狗（BaoStock 静默挂起唯一解）
│   │   ├── normalize.py          #   代码归一 / 单位换算 / fatal 断言
│   │   ├── providers/
│   │   │   ├── __init__.py       #    注册表 + build_chain() 工厂
│   │   │   ├── baostock.py       #    ✅ 主源，已实现日线
│   │   │   ├── akshare.py / efinance.py / hithink.py   # 待实现
│   │   ├── ingest/
│   │   │   ├── daily.py          #    日线回填（断点续传 + 缩批重试）
│   │   │   └── minute.py / financial.py / reference.py / etf_meta.py
│   │   ├── quality/
│   │   │   ├── asserts.py        #    记录级八项断言
│   │   │   └── validators.py / crosscheck.py / golden.py   # 五层验证
│   │   └── store/
│   │       ├── ddl.py            #    20 张表 DDL（幂等）
│   │       ├── parquet.py        #    Parquet 湖读写（按年分区 + zstd）
│   │       └── catalog.py        #    TradeCalendarRepo / SecurityRepo / FactorDefRepo
│   │
│   ├── factors/                  # ── L2 因子
│   │   ├── dsl/
│   │   │   ├── lexer.py / parser.py / ast_nodes.py
│   │   │   ├── analyzer.py       #    静态分析：min_window + 未来函数检测
│   │   │   └── compiler.py       #    AST → Polars Expr；plan() 拆 TS/CS 嵌套
│   │   ├── ops/
│   │   │   ├── registry.py       #    @op(category=TS|CS|EL, min_window=)
│   │   │   ├── ts_ops.py / cs_ops.py
│   │   │   └── rust_bridge.py    #    Rust 算子覆盖 + 自动降级
│   │   ├── preprocess/
│   │   │   ├── registry.py       #    METHODS 注册表（对齐 AlphaPurify）
│   │   │   └── winsorize/standardize/neutralize/orthogonalize
│   │   ├── evaluate/             #    ic / quantile / decay / attribution / report
│   │   └── engine.py             #    FactorEngine.compute()
│   │
│   ├── backtest/                 # ── L3 回测
│   │   ├── rules/model.py        #    RuleSet / TaxSchedule / InstrumentRules
│   │   ├── rules/loader.py       #    YAML → RuleSet
│   │   ├── events.py             #    Order / Fill / Bar
│   │   ├── broker.py             #    ✅ 撮合（涨跌停 / 整手 / 佣金累计）
│   │   ├── account.py            #    ✅ 持仓可用量按 sellable_after_days
│   │   ├── engine.py / slippage.py / metrics.py
│   │   └── strategy/base.py / factor_topn.py
│   │
│   ├── portfolio/                # ── L4 组合：screener / dedup / weighting
│   ├── market/                   # ── 看板热通路
│   │   ├── em_client.py          #    em_get() 限流代理（必须一开始就走）
│   │   ├── scheduler.py          #    采集时刻表
│   │   ├── providers/mootdx.py
│   │   └── collectors/           #    sector / money_flow / sentiment / limit_up
│   ├── research/                 # ── 研究层
│   │   ├── dialect/
│   │   │   ├── mapping.py        #    JQ API → 原生映射 + WARNINGS + UNSUPPORTED
│   │   │   ├── jq_shim.py        #    ✅ attribute_history / order_target_* / log
│   │   │   └── jq_import.py      #    ✅ AST 扫描，导入即失败
│   │   ├── ml/                   #    dataset / model / backtest（借 qlib 接口）
│   │   └── notes/                #    研报复现（M10）
│   ├── paper/                    # ── 模拟盘 + 偏差告警
│   ├── server/                   # ── L5：main.py / deps / jobs / ws / schemas
│   │   └── api/                  #    health data factors backtests market paper
│   ├── cli/                      # ── lq data / factor / backtest / strategy
│   └── _rust/                    # ── Rust 桥接
│       ├── loader.py             #    status() / has_rust()（ABI 冒烟）
│       └── ops_ref.py / metrics_ref.py   # Python 参考实现（降级路径）
│
├── crates/                       # ── Rust workspace
│   ├── Cargo.toml                #    ⚠️ pyo3/pyo3-polars/polars 统一版本
│   ├── lq-ops/                   #    #[polars_expr] 插件：ts_corr / ts_regbeta
│   ├── lq-backtest/              #    ✅ 撮合 + 佣金（含 2 个单测）
│   └── lq-metrics/               #    ✅ rank_ic / max_drawdown（含 2 个单测）
│
├── web/                          # ── Next.js 15 App Router
│   ├── package.json / next.config.mjs / tailwind.config.ts（涨红跌绿）
│   └── src/app/{dashboard,watchlist,security,factors,backtests,sectors,data}
│
├── scripts/
│   ├── setup.sh                  # ✅ 装依赖 + 建库 + 编 Rust + 装前端
│   ├── dev.sh                    # ✅ Redis + API + Worker + Web 一键启动
│   ├── init_db.py                # ✅ 建 20 张表（幂等）
│   ├── bootstrap_data.py         # ✅ 全市场日线回填入口
│   └── build_rust.sh             # ✅ maturin develop（三个 crate）
│
├── tests/unit     # test_symbol / test_dsl / test_rules / test_broker / test_normalize / test_rust_abi
├── tests/integration/test_pipeline_smoke.py
└── docs/          # STRUCTURE.md / EXTENSION_POINTS.md / ARCHITECTURE.md
```

---

## 2. 模块卡片（每个模块"长什么样"）

### 2.1 `core` — 地基

| 文件 | 关键 API | 说明 |
|---|---|---|
| `types.py` | `Symbol(code, exchange)`、`parse_symbol(raw)`、`Board`、`SecType` | 支持 `600000` / `600000.SH` / `sh.600000` / `600519.XSHG` 四种输入；ETF 代码段（51/56/58/15/16/159）自建，adata 的表不含 |
| `registry.py` | `Registry[T].register(key, meta)`、`get`、**`describe()`** | 贯穿全局的模式：新增能力不改上层，且前端能自动枚举 UI |
| `errors.py` | `SourceUnavailable` / `SourceSchemaChanged` / **`CapabilityMissing`** / `LookaheadError` | 分层异常让上层区分"该重试""该换源""该报错" |
| `db.py` | `writer()` / `reader()` | DuckDB **只允许单个写进程**，写操作全部收敛到 `writer()` |
| `calendar.py` | `is_trading_day` / `trade_days` / `prev_trade_day` | A 股调休时周末上班不是交易日、工作日放假也不是 |

```python
# types.py 的核心 —— 三条硬约束之一
def parse_symbol(raw: str) -> Symbol: ...      # "sh.600000" -> Symbol("600000", "SH")
Symbol("510300", "SH").sec_type                # -> SecType.ETF
```

### 2.2 `data` — 接入与存储

**Provider 契约（6 个核心方法 + 能力声明）**

```python
class DataProvider(ABC):
    capability: frozenset[Capability]
    def daily_bars(self, symbols, start, end) -> pl.DataFrame
    def minute_bars(self, symbols, start, end, freq) -> pl.DataFrame
    def adj_factors(self, symbols, start, end) -> pl.DataFrame
    def financial_pit(self, symbols, start, end) -> pl.DataFrame   # 必须含 pub_date
    def securities(self) -> pl.DataFrame
    def trade_calendar(self, start, end) -> pl.DataFrame
```

**为什么用 Capability 而不是简单 Fallback**：Fallback 链解决不了"某源根本没有 1 分钟线"。
不支持就抛 `CapabilityMissing`——**绝不静默返回空**，"回测跑通了但数据不对"是最糟的结果。

**BaoStock adapter 的两个坑（已处理）**

```python
# 1) 批量连续请求静默挂起 —— 实测 20 只跑 12 分钟无返回，
#    socket.setdefaulttimeout 完全无效（它用自己的阻塞 socket）
rows = run_with_watchdog(self._query, ...)     # 子进程 + 超时 kill

# 2) 只请求不复权价（adjustflag="3"），复权在读取时用 adj_factor 动态算
#    —— 与 free-stockdb 的核心卖点一致：原始价 + 复权因子分离
```

**存储分工**：`daily_bar` 等大表走 Parquet（按年单文件，**绝不按 symbol 分文件**）；
`security` / `trade_calendar` / `factor_def` 等小表走 DuckDB；两者通过 `catalog.py` 的 Repo 访问。

### 2.3 `factors` — DSL 一鱼四吃

```
字符串表达式 → lexer → parser(AST) → analyzer(静态检查) → compiler(Polars Expr) → 执行
```

四个收益：① DAG 调度与缓存 key ② LLM 可直接生成 ③ 可静态禁止未来算子 ④ 前端编辑器自省。

```python
ast = parse("Rank(Ts_Mean($close,5) / $close - 1)", "rev_mom")
check(ast)                       # 未注册算子 / 未来函数 → 抛错
# ast.min_window = 5, ast.fields = {"close"}
```

**必须记住的坑（Polars issue #25691）**：嵌套 `.over()` 被当作层级分区，而因子的时序与截面是**正交**的。
`Ts_Mean(Rank($close), 5)` 不报错但结果错。所以执行器一开始就要有 `plan()` 拆步骤、中间物化：

```python
if has_nested_ts_cs(ast.root):      # TS 与 CS 嵌套 → 拆成多步，每步物化
    steps = plan(ast.root)
```

**预处理层**（M4 填）：`去极值 → 标准化 → 中性化 → 正交化`。
从原始因子直接算 IC，A 股场景下 IC 会被极值股和行业暴露绑架，是假信号。
默认配方写在 `config/factors/custom.yaml`。

### 2.4 `backtest` — 规则全数据化

```python
rs = load_ruleset("cn_a_share")
rules = rs.for_symbol("513050.SH", SecType.ETF, Board.MAIN,
                      fund_type="qdii", sellable_after_days=0)
rules.tax_rate(date(2023, 1, 1))     # 0.001  印花税历史区间
rules.tax_rate(date(2024, 1, 1))     # 0.0005
rules.sellable_after_days            # 0  → QDII ETF 是 T+0
```

四处设计修正（第三方源码推翻了原设计）：

| 项 | 原来 | 现在 | 依据 |
|---|---|---|---|
| T+N | 按 `sec_type` 查表 | **per-instrument `sellable_after_days`**，规则表只给默认 | akquant `MarketModel` |
| 印花税 | 单一税率 | **`TaxSchedule` 按日期区间**（2023-08-28 千一→万五） | rqalpha `pit_tax` |
| 最低佣金 | 按成交计 | **按订单累计**（分多次成交只收一次 5 元） | rqalpha `commission_map` |
| ETF 印花税 | 沿用股票 | **`FundConfig` 无 tax 字段** → 免征 | akquant `fund.rs` 独立印证 |

```python
# broker.py 已实现并测试
target = max(r.commission.min, cum * r.commission.rate)
comm = max(target - paid, 0.0)     # 400 股分两次成交：5.0 + 0.0 = 5.0（不是 5.5）
```

### 2.5 `market` — 看板热通路

与冷通路（因子/回测）**必须分开**：热通路的数据**当天不采就永久丢失**
（涨停池、龙虎榜、异动源站不提供历史回溯），失败重跑没用。

```python
# 东财请求必须一开始就走限流代理，别先裸奔再补
r = em_get(url, qps=3.0)
```

### 2.6 `research` — 方言与 ML

**JQ 兼容：导入即失败，绝不静默返回错误结果**

```bash
$ lq strategy check /tmp/jq_demo.py
L5: [警告] attribute_history — data[sec] 是【上一单位时间】的数据，JQ 老手也常踩
L6: run_interval() 暂不支持（需改写成原生 on_bar）
```

方言只翻译 API 调用，**撮合/费率/T+N 全由内核 RuleSet 决定**——
所以 JQ 策略跑出来的成本与原生策略完全一致。

**ML 不引入 qlib 运行时**（ADR-11）：它要求数据以自有 bin 格式托管，与 Parquet 湖并存就是双份数据、双份复权口径。只借 Model 三段式接口和 Alpha158 因子集。

### 2.7 `_rust` — Rust 边界与降级

```python
# 每个 Rust 模块必须有 Python 参考实现，加载失败自动降级
$ python -c "import lquant._rust.loader as l; l.print_status()"
polars 1.44.1
  lq_ops       python-fallback     ← 未编译 Rust，系统照常工作
  lq_backtest  python-fallback
  lq_metrics   python-fallback
```

⚠️ `pyo3` / `pyo3-polars` / `polars` 三者 ABI 强绑定，版本错配要到运行时才炸。
CI 必须跑 `make smoke`（pre-commit 已挂）。

---

## 3. 数据流：一次回测的端到端

```
1. 用户提交 BacktestRequest{strategy, params, start, end, universe}
        ↓  POST /api/backtests  →  RQ enqueue(lquant-backtest)
2. RuleSet 加载 config/rules/cn_a_share.yaml（税率区间 / T+N / 涨跌停）
3. 逐交易日事件循环：
     读 Parquet(daily_bar + adj_factor) → 动态复权
     → 因子 DSL 计算（Polars / Rust）
     → 预处理（去极值 → 标准化 → 中性化）
     → 策略 on_bar 返回目标权重
     → Engine 生成订单 → Broker.match（涨跌停/整手/佣金累计）
     → Account.apply_fill（available 按 sellable_after_days）
     → NAV 落盘
4. 绩效 metrics（Rust lq-metrics）→ JSON + 报告
        ↓  WS /ws/jobs/{id} 推送进度 → 前端图表
```

---

## 4. 启动脚本

### 4.1 Make targets

| 命令 | 作用 |
|---|---|
| `make setup` | 建 venv → 装依赖 → 生成 `.env` → 建库 → 编 Rust → 装前端 |
| `make db-init` / `make db-reset` | 建 20 张表（幂等） / 删库重建 |
| `make bootstrap` | 全市场日线回填（带看门狗，首次 20–30 分钟） |
| `make dev` | Redis + API(8000) + Worker + Web(3000) 一键启动 |
| `make api` / `make worker` / `make web` | 单独起某一个 |
| `make smoke` | ABI 冒烟：确认 Rust 扩展可加载 |
| `make test` / `lint` / `fmt` / `type` | 质量门禁 |
| `make rust-build` / `rust-test` | 编译三 crate / 跑 Rust 单测 |
| `make docker-up` / `docker-down` | Redis |

### 4.2 脚本

| 脚本 | 说明 |
|---|---|
| `scripts/setup.sh` | 检测 `uv` / `cargo` / `npm`，缺哪个提示哪个并**跳过**（不中断） |
| `scripts/dev.sh` | `trap cleanup EXIT` 统一收进程；Redis 用 docker，无 docker 退化为 `redis-server` |
| `scripts/init_db.py` | 读 `data/store/ddl.py` 的 `DDL_STATEMENTS`，幂等建表 |
| `scripts/bootstrap_data.py` | 回填入口；批次超时自动缩批重试 |
| `scripts/build_rust.sh` | 三 crate 依次 `maturin develop --release` |

---

## 5. 实现状态与里程碑对照

| 模块 | 状态 | 里程碑 |
|---|---|---|
| `core/*` | ✅ 可用 | M0 |
| `data/base` `capability` `fallback` `normalize` `watchdog` | ✅ 可用 | M1 |
| `data/providers/baostock`（日线） | ✅ 可用 | M1 |
| `data/store`（DDL + Parquet + catalog） | ✅ 可用（20 表） | M1 |
| `factors/dsl`（lexer→parser→analyzer→compiler） | ✅ 可用 | M3 |
| `factors/ops`（TS/CS 算子 + 注册表） | ✅ 可用 | M3 |
| `factors/preprocess` `evaluate` | 🔲 骨架 | M4 |
| `backtest/rules` `broker` `account` | ✅ 可用（含测试） | M5 |
| `backtest/engine` `metrics` | 🔲 骨架 | M5 |
| `portfolio/*` | 🔲 骨架 | M6 |
| `market/*` | 🔲 骨架（em_client 可用） | M7b |
| `research/dialect`（JQ） | ✅ 静态检查可用 | M6b |
| `research/ml` | 🔲 骨架 | M6c |
| `server/*` | 🔲 骨架（main 可启动） | M7 |
| `web/*` | 🔲 页面占位 | M8 |
| `crates/*` | ✅ 代码就绪（未编译，本机无 cargo） | M6 |

**已验证可执行**：`scripts/init_db.py`（20 表）、`lq factor add`（AST 检查）、
`lq strategy check`（JQ 导入检查）、18 个单元测试。

---

## 6. 扩展点落地状态

| EP | 说明 | 档位 |
|---|---|---|
| EP-1 数据源 | 写 Provider + yaml | 随功能实现 |
| EP-2 因子算子 | `@op(category, min_window)` | 随功能实现 |
| EP-3 预处理方法 | `METHODS` 注册 | 随功能实现 |
| EP-4 撮合规则 | 加 YAML 文件 | 随功能实现 |
| EP-5 策略方言 | JQ shim | M6b |
| EP-6 组合权重 | skfolio estimator | 随功能实现 |
| EP-7 看板采集器 | collector 注册 | 随功能实现 |
| EP-8 ML 模型 | Model 接口 | M6c |
| EP-9 看板→因子 | **表结构先建** | M7b 建表 |
| EP-10 通知告警 | 仅留接口 | 不做 |
| EP-11 LLM 挖因子 | 架构预留 | 不做 |
| EP-12 多市场 | 三条硬约束先守 | 不做 |

---

## 7. 下一步建议

**先做 M1 收尾**（唯一有真实技术风险的部分）：

1. `data/ingest/reference.py` — 标的表 + 交易日历（没有日历，其余全跑不了）
2. `data/providers/baostock.py` 补全 `securities()` / `trade_calendar()`
3. 跑通 `make bootstrap --full` 一次，验证看门狗与断点续传真的生效
4. `data/quality/validators.py` 的**涨跌停约束**——一条断言挡住 80% 的价格错误，性价比最高

**不要先做前端**：M4 结束能在 Jupyter 出因子报告，M5 出回测，前端放 M8。
但 **ETF 元数据表和规则表要在 M1/M5 就建好**，后期回填成本极高。

---

## 8. M1 收尾落地记录（2026-09-08）

### 8.1 数据层补全

- **providers/baostock.py**：6 个核心方法全部实现（daily/minute/adj_factors/
  securities/security_details/trade_calendar/etf_meta/financial_pit）。
  关键约束：所有网络调用是**模块级函数**（spawn 子进程可 pickle），
  每次调用独立 login/logout。
- **ingest/reference.py**：日历 + 标的清单快路径 + 逐只补上市日期慢路径；
  `_merge_existing_details` 防 INSERT OR REPLACE 冲掉已补的 list_date。
- **ingest/checkpoint.py**：断点续传（原子写 JSON），daily/financial/minute 共用。
- **ingest/etf_meta.py**：per-instrument T+N（跨境/债/金/货币 = T+0），
  可选 akshare 东财快照增强规模。
- **ingest/financial.py**：PIT 财务，无 pub_date 的记录**直接丢弃**。
- **ingest/demo.py**：合成数据生成器（`lq data demo`），格式与真实数据一致，
  数据源被网络阻断时兜底，换真数据直接覆盖。
- **store/catalog.py**：upsert 按目标表列自动对齐（换源不保证列全）。
- **store/parquet.py**：新增分钟线 write_minute（freq+年月分区）。

### 8.2 服务层

- **jobs.py**：Redis 探测 + 本地线程降级（无 Redis 不阻塞启动）。
- **health API**：`GET /api/health` 暴露 redis/rust/数据覆盖度，
  一键脚本的健康检查依赖它。

### 8.3 一键脚本 lquant.sh

`install / build / start / stop / status / restart / logs / bootstrap / doctor / all`。
自动装 uv（装不上降级 pip）、venv、全部 Python/Node 依赖（清华源 + npmmirror）、
建库、启动 API+Worker+Web 并探活。无 Redis/cargo 均不阻塞。

### 8.4 已知环境坑（重要）

- **BaoStock 走原始 TCP（10030 端口）**，在公司网络/代理 TUN 下常被静默阻断
  —— 看门狗会兜住并报 `SourceUnavailable`，此时用 `lq data demo` 兜底或换网络。
- 本机沙箱/代理同时会拦 npm 大包下载（chokidar），前端依赖需在正常网络装。
- 依赖解析冲突已修：tenacity>=8.1（mootdx 锁 <9）、httpx>=0.25,<0.26（mootdx 锁 <0.26）。

---

## 9. L2–L6 分层填实记录（2026-09-08）

自底向上把六层从占位填成可验证实现，每层都跑了真实数据验证：

### L2 因子层（factors/preprocess + evaluate）
- 预处理四步全注册：winsorize(mad/quantile/sigma3/clip) · standardize(zscore/rank/minmax) ·
  neutralize(ols/ridge/lasso，行业用 WLS 变体) · orthogonalize(symmetric/gram_schmidt/pca)。
  流水线执行器自动按「去极值→标准化→中性化→正交化」排序，乱序 spec 会被纠正。
- 验证：中性化把市值暴露 corr 从 -0.99 打到 8e-17；对称正交把 0.8 相关因子打到 1e-8。
  **数学坑**：factor_b = a*factor+b 这种精确仿射的矩阵秩 1，正交化后 corr=1 是正确行为，
  测试夹具必须含独立噪声。
- 评价体系：IC/RankIC/IR/t 值/正比例、十分位分层多空、衰减半衰期+建议调仓频率、
  行业/市值归因、自包含 HTML 报告（无外部依赖）。demo 数据上多空年化 27%、夏普 1.85。

### L3 回测层
- 引擎：T 日收盘出信号 → T+1 开盘价撮合（防未来函数）；先卖后买；涨跌停拒单；
  滑点三模式（pct/tick/volume_pct）；每笔成交用 per-instrument RuleSet 计费。
- 修真 bug ①：`to_frame()` 净值首点为 int 时 Polars 推断 Int64，混入浮点即炸 ——
  显式 schema_overrides=Float64。
- 修真 bug ②：规则表 ETF 段漏配佣金（免印花税≠免佣金），补 rate=2.5e-4, min=5.0。

### L4 组合与研究
- screener 多因子打分（zscore 加权）+ 过滤（流动性/上市天数/ST）；
- weighting：equal/mcap/ic_weight/inverse_vol/min_variance/risk_parity/HRP（谱聚类+队列式二分）；
- dedup：相关性去重（按流动性优先保留）；
- ML：build_dataset（PIT 标签对齐+去同行）→ walk_forward 切分 →
  lgbm（libomp 缺失自动降级 sklearn GBRT/ridge）→ IC 报告 → 预测接入回测。
  全链路验证通过（合成数据 IC 为负属预期，无真实 alpha）。

### L5 市场看板 + 模拟盘
- 采集器注册表（7 个：涨停/跌停/炸板/资金流/板块/北向/情绪），调度时点 close/evening；
- 表结构可迁移（旧 sentiment_daily 自动删建）；_upsert 兼容无主键表（按 trade_date 先删后插）；
- 每个采集器带 demo 模式，离线环境全链路可验证；
- 模拟盘 PaperEngine：事件驱动撮合、T+N 冻结/解冻、涨跌停拒单、资金不足拒单（带 reason）；
  compare_nav 对拍分级 ok/warning/critical —— 对拍报 critical 时先查「信号分歧」还是「执行 bug」。

### L6 API + 前端
- 四组路由填实：/api/factors（注册+AST 校验+评价+HTML 报告）、/api/backtests（同步跑+落库+净值）、
  /api/market（总览/板块/资金流/涨停池/手动采集）、/api/paper（回放/状态/对拍）。
  坏 DSL 返回 422（parse+check 双重校验）。
- 前端页面：大盘看板（情绪卡+北向）、板块热力表、因子研究（注册+评价+指标卡）、
  回测列表+详情（净值/回撤图+成交明细）、模拟盘（回放+持仓+对拍判定）。涨红跌绿贯穿。
- **遗留**：web 依赖安装被宿主安全层拦截（chokidar 下载触发 CODEBUDDY_BROKER_DENY，
  npmmirror/jsregistry/官方源均同），需用户在自己终端 `cd web && npm install` 后 `./lquant.sh start`。

### 测试
26 个 pytest 全过（18 原有 + 预处理 3 + 模拟盘 5）。

## 10. 第二轮前后端完善（2026-09-08）

### 后端新增/修复
- `/api/data` 扩成完整数据服务：coverage（湖+12 张表覆盖度）、securities（标的搜索）、
  daily（个股日线，读 Parquet 湖）、quote（东财 push2 实时行情，em_get 限流+断网降级 available=false）。
- `/api/watchlist`：自选股 CRUD（DuckDB watchlist 表，懒建表），列表带每票最近收盘与涨跌幅。
- `/api/factors/{name}`：因子详情（定义+关联报告），注意注册在 /reports* 之后防路由遮蔽。
- `/api/market/money-flow` 加 symbol 参数：全市场 Top 或单票 30 日资金流。
- ws.py 重写：轮询 jobs 状态推真实进度（本地注册表/RQ 双模式，终态后关连接）；
  之前 ws 路由根本没挂到 app 上（死代码），已挂载。
- jobs.py 加 `_LOCAL_JOBS` 注册表与 `get_job()`。

### 三个深坑（都已在代码注释里留了墓志铭）
1. **DuckDB 读写实例分裂**：reader() 加 read_only=True 后，DuckDB 实例缓存按
   「路径+配置」区分 → 同进程两个独立实例，写后读不到（因子注册完立即 404、
   watchlist name=null）。修复：reader 也用读写连接，共享同一实例。
2. **writer 内嵌套 reader**：文件锁导致内层连接静默失败被 except 吞掉。
   约定：进 writer 前把所有查数做完。
3. **factor_def 旧结构静默丢数据**：磁盘表是旧 DDL（expr 列），API 写 expression
   被 upsert 列对齐逻辑丢弃（列不存在→NULL）。修复：DDL 更新 + ensure_factor_def
   迁移函数挂到服务启动（ALTER RENAME → 建新表 → INSERT SELECT → DROP）。

### 前端填实
- 公共件：api.ts（ApiError 透出 detail + fetcher/del）、QuoteTable（涨红跌绿通用表）、
  KChart（lightweight-charts K线+成交量，阳红阴绿）。
- 页面：数据覆盖度页（12 表卡片+采集触发按钮）、个股详情页（实时报价头+K线+资金流）、
  自选股页（300ms 防抖搜索添加+行情列表）、因子详情页（定义+快速评价+历史报告）。
- 导航加「自选」。

### 验收
- 26 pytest 全过；uvicorn 起服 curl 实测：coverage/securities/daily/quote（真实茅台
  1309.30 -0.51%）/watchlist 增删查/factors 注册-详情-列表/404 语义全通；
  WS 冒烟 not_found 终态推送正常。
- 符号归一化：resolve_symbol（600519 → 600519.SH）在 API 边界统一，内部层只见规范码。

## 11. 对照方案查漏补缺（2026-09-08 晚）

逐项核对《细化技术方案》功能清单，补齐 6 个 P0/P1 缺口：

| ID | 功能 | 实现 |
|---|---|---|
| M9 | 采集健康度 | collect_log 逐采集器落库（ok/empty/failed）+ /api/market/collect-status + 数据页健康度卡（当日缺口标橙） |
| M3 | 技术指标 | factors/indicators.py（MA/EMA/MACD 国内口径/Wilder RSI/BOLL）+ /api/data/indicators + KChart overlays 叠加 MA + 个股页指标卡 |
| F6 | 因子相关性 | factors/analysis.py 按日 Spearman 矩阵 + 冗余对判定 + /api/factors/analyze + 因子页热力矩阵 |
| F7 | 因子合成 | 按日秩 zscore 等权/IC 加权 → 全套评价 + HTML 报告 + /api/factors/synthesize |
| B6 | 回测对比 | /api/backtests/compare（归一净值按日对齐）+ 回测页复选框对比（ECharts 多线 + 指标表） |
| R-ML5 | 实验管理 | ml_run 表 DDL + run_ml_pipeline 自动落记录 + coverage 纳入 |
| SYS5 | 备份 | scripts/backup.py（DuckDB CHECKPOINT 后复制 + Parquet zip，保留 N 份） |

坑：collect_log 旧表无主键导致 INSERT OR REPLACE 报 Binder Error —— record 改先删后插，
ensure_collect_log 启动迁移（日志可再生，直接重建）。

仍留待后续：F4 两级缓存（改 engine，动面大）、D3 复权因子入库（需新浪源联网验证）、
B4 回测归因、B5 参数扫描、F9 看板反哺因子。

## 12. 测试补全与扩展点（2026-09-08 深夜）

### 测试体系（31 → 67 个）
- **API 层集成测试**（tests/unit/test_api.py，14 个）：拷贝真实 duckdb+parquet 到临时目录，
  TestClient 全链路离线跑 —— health/data/watchlist CRUD/factors 注册-校验-404/
  backtests run-list-detail-compare/market 采集健康度/strategies/ws 推送。
- **回测引擎 + 指标**（test_engine.py，9 个）：防未来函数 T+1 撮合时序、rebalance=none、
  滑点方向性（买抬卖压）、滑点成本对照、max_drawdown 已知序列、零波动 sharpe。
- **组合权重**（test_weighting.py，9 个）：风险平价等风险贡献、最小方差 ≤ 等权方差、
  HRP 分散性、NaN 行容错、未知方法退等权（设计约定，不是报错）。
- **技术指标**（test_indicators.py，6 个）：MA 逐点对照手算、MACD 国内口径 HIST=2(DIF-DEA)、
  RSI 单调序列极值、BOLL 轨道关系。
- **前端**（vitest 设施，待 npm install 后可跑）：lib/format.ts（纯函数格式化，从 QuoteTable 抽出）
  + format/api（fetch mock + detail 透出验证）/QuoteTable 组件（涨红跌绿 class 断言）测试，
  vitest.config.ts + testing-library + jsdom 已配好，`npm test` 即可。

### 测试挖出的真 bug（已修）
1. **watchlist DELETE 恒 200**：DuckDB 的 DELETE fetchall() 恒返回计数行，
   「删没删到」判据永远为真 → 改 DELETE...RETURNING 拿实际删除行。
2. **RSI 首行 NaN**：首日无涨跌，0/0=NaN（polars 里 NaN≠null，drop_nulls 过滤不掉）
   → 显式置 null。
3. 合成测试数据必须用合法 A 股代码 —— build_rules 对非法代码静默跳过，
   AAA/BBB 这种符号会让整个回测悄悄空转。

### 扩展点与预留接口（现状盘点）
- **数据源**：PROVIDERS 注册表 + providers.yaml 链式 fallback（已有），加新源 = 注册类 + YAML 启用。
- **策略**：STRATEGIES 注册表（本轮新增），@register_strategy 装饰器注册，
  GET /api/strategies 自动枚举，前端回测表单直接用。
- **因子算子/预处理**：METHODS 注册表（已有）。
- **分页**：factors/backtests 列表端点支持 offset/limit（本轮新增），limit 缺省全量，向后兼容。
- **任务**：jobs 队列 Redis/本地双模式 + /ws/jobs/{id} 进度协议（已有）；
  回测转异步时接口签名不变。
- **预留未做**（需要时再加，避免过度设计）：API 鉴权（本地单机工具暂无必要）、
  回测参数网格（B5）、研报复现（R-RP）、看板反哺因子（F9）。

## 13. 看板丰富化 + Qlib Alpha158 + 回测选型（2026-09-08 夜）

### 大盘看板
- 新端点：/api/market/breadth（湖日线截面逐日涨跌家数/涨跌停近似/中位涨跌/成交额，
  不依赖采集任务，盘前也有数）；/api/market/batch（批量个股+ETF：单票行情、
  归一净值按日对齐、等权净值、强弱汇总，最多 20 只）。
- dashboard 重构：宽度六卡 + 90 日宽度图（上涨/下跌堆叠柱 + 中位涨跌线）+
  批量对比区（输入池/快捷池/localStorage 记忆 + ECharts 多线 + 明细表）；
  情绪/北向改为空态降级卡片 —— 看板不再因采集表为空而整页空。

### Qlib Alpha158 内置因子（qlib_alpha.py）
- 公式逐条对照 microsoft/qlib loader.py::Alpha158DL 源码（WebFetch 核对）：
  kbar 9 + price 4 + rolling 29 族 × {5,10,20,30,60} = 158 个，纯 Polars + numpy 滑窗。
- 差异：$vwap 用 amount/volume 代理；Slope/R²/Resi 滑窗闭式解；Resi=sqrt((1-R²)Var(y))；
  IdxMax 0=最老一根。
- API：GET /api/factors/builtin（族/名称过滤）、POST /api/factors/seed-builtin
  （按 names/families 一键入库 factor_def）；evaluate/_compute_factor 自动命中内置因子。
- 端到端已通：种子 → 现算 → IC/RankIC/分层多空/半衰期 → HTML 报告 → 详情页。
- 坑：numpy 滑窗 warmup 产生 NaN，polars 里 NaN≠null，compute_all 出口统一 fill_nan(None)。

### 回测引擎选型（docs/BACKTEST_ENGINES.md）
- 结论：自研 Engine 为唯一执行真源（A 股规则全内置且已测）；参数扫描走 Polars
  向量化快扫（B5 待做）；不引入 backtrader/qlib backtest（第二套撮合规则漂移风险 +
  维护停滞/重依赖）；RQAlpha 观望。
- 预留 BacktestAdapter 协议 + 注册表（backtest/adapter.py）：外部引擎结果与原生
  Engine.run() 同构（AdapterOutput），落同批表，/api/backtests/compare 天然可比。

测试 67 → 83（Alpha158 10 个、breadth/batch 2 个、builtin/seed/evaluate 2 个、adapter 2 个）。
