# lquant 能力基线（竞品借鉴分析的对照面）

- 日期：2026-10-09
- 对象：`/Users/lyp/code/lquant`，HEAD `1d63f00`（`git log -1`）
- 方法：源码盘点 + 注册表枚举 + 对 live 数据湖的只读检查
- 作用：本目录所有「值得借鉴」结论都必须先过这一关 —— **lquant 已有的不重复推荐**。

> 证据约定：结论后附 `路径:行号` 或可复现命令。凡是未逐行读过的，标 **【未逐行验证】**。

---

## 0. 一页数字（实测）

| 维度 | 数量 | 取证方式 |
|---|---:|---|
| Python 源文件 | 348 | `find src/lquant -name '*.py' \| wc -l` |
| Python 源码行数 | 56,540 | `find src/lquant -name '*.py' -exec cat {} + \| wc -l` |
| 测试文件 | 307 | `ls tests/unit/*.py tests/integration/*.py \| wc -l` |
| 覆盖率门禁 | 95% | `pyproject.toml` `fail_under = 95` |
| HTTP 路由 | 198 | `grep -rhoE '@router\.(get\|post\|...)(' src/lquant/server/api/*.py \| wc -l` |
| API 模块 | 25 | `ls src/lquant/server/api/*.py` |
| Web 页面 | 25 | `find web/src/app -name 'page.tsx'` |
| CLI 命令 | 58 | `grep -rhoE '@[a-z_]+\.command\(' src/lquant/cli/commands/*.py \| wc -l` |
| 因子算子 | 45 | `grep -rhoE '@op\(' src/lquant/factors/ops/*.py \| wc -l` |
| 因子评价模块 | 25 | `ls src/lquant/factors/evaluate/*.py` |
| 数据 Provider | 5+ | `@PROVIDERS.register`：baostock / akshare / sina / tencent / tushare |
| 看板采集器 | 11 | `market/collectors/__init__.py:72-92` |
| 组合权重方法 | 6 | `portfolio/weighting.py:249`（equal / inverse_vol / risk_parity / min_variance / hrp / enhanced_indexing） |
| MCP 工具 | 13 | `agent/mcp_server.py:177` |
| 任务队列 | 6 | `server/jobs.py` `QUEUES` |
| 任务中心 kind | 6 | `server/api/task_center.py` `KINDS` |
| Rust crate | 3 | `crates/`：lq-ops / lq-backtest / lq-metrics（每个都有 Python 降级参考实现） |

---

## 1. 已经很强、**不需要**向外借鉴的模块

| 能力 | lquant 现状（证据） | 判断 |
|---|---|---|
| A 股撮合规则 | 涨跌停 tick 取整、分板 ST 涨跌幅、ETF 按代码段定 20%、印花税历史区间 + 买卖方向、T+N 按**交易日**、零股清仓、退市核销、公司行为 | 领先项；`docs/BACKTEST_ENGINES.md` 已论证不引外部引擎 |
| 数据层工程 | Capability 路由、Fallback 链、子进程看门狗、断点续传（区间语义）、原子写 + `flock`、质量门禁、湖完整性自检 | 领先项 |
| 因子评价广度 | IC/RankIC/NW-t、分层、衰减、归因、容量、成本敏感性、事件研究、分组 IC、评级、稳健性、风格相关、overnight 切分、`trace()` | 与 alphalens 系列同级或更宽 |
| 因子挖掘纪律 | GP / 随机 / LLM 三类 generator + 配额门禁 + 提交重验 + `factor_mining_run` 台账 | 比多数开源项目严格 |
| 防自欺机制 | 交叉验证 sentinel（qlib / AlphaPurify 两套）、`upstream_broken` 反证语义 | 罕见 |
| 全栈闭环 | Web 25 页 + 任务中心 + 监控 + 模拟盘对拍 + 通知 | 开源同类里少见 |
| LLM 接入 | 无头 CLI 执行体（Claude Code / Codex）+ MCP 工具 + skill 工作区 + A2A | 设计上「平台零模型配置」，比多数 agent 项目克制 |

---

## 2. 已确认的缺口（本次实测，作为借鉴的靶子）

### 2.1 A 股特有数据面（关键词实测，`grep -ril` 命中数为 0 或仅出现在注释/资讯里）

| 数据面 | 命中 | 说明 |
|---|---|---|
| 融资融券 / 两融余额 | **0** | `margin` 的 5 处命中全是财务报表的毛利率/净利率 |
| 限售解禁 | **0** | `解禁` 唯一命中是 T+N 未解禁的注释（`backtest/exit/simple.py:62`） |
| 筹码分布（CYQ） | **0** | 无 |
| K 线形态识别（W 底/杯柄/头肩） | **0** | `形态` 的命中全是"泄漏形态/字符串形态"等无关词 |
| 宏观数据（CPI/PPI/GDP/LPR/社融） | **0** | `宏观` 只出现在资讯采集器与新闻源里 |
| 大宗交易 | **0** | 仅 `data/quality/crosscheck.py:46` 的量额口径注释提到 |
| 股东户数 / 股东增减持 | **0** | `holder` 命中的是持仓对象 |
| 商誉 / 股权质押 | **0** | 无 |
| 可转债 / 期权 / 期货 | 仅 agent 的外部 skill | `agent/workspace.py:47` 明确写：这些面由第三方 skill `a-stock-data` 内嵌 Python 直连公开源提供，**本地湖没有** |

### 2.2 组合与风控

| 缺口 | 证据 |
|---|---|
| 无统一组合构建管线 | `portfolio/` 只有 screener / dedup / weighting / riskmodel / optimizer 散装函数，无 `Strategy→Optimizer→Executor` 式管线 |
| 无约束优化求解器 | `optimizer.py` 用 `scipy SLSQP`；无行业暴露上限、换手上限、因子暴露约束等通用约束表达（`max_weight`/`max_active` 是特例） |
| **无事前风控层** | 全仓 `grep 风控` = 0；引擎无组合级暴露检查/回撤熔断，退出规则（`backtest/exit/`）只管个股 |
| 无压力测试 / 情景分析 | 无 |

### 2.3 回测与执行

| 缺口 | 证据 |
|---|---|
| 无 nested / intraday 回测 | 引擎是扁平日频事件循环 `backtest/engine.py:230` |
| 无执行算法（TWAP/VWAP/参与率调优） | 只有 `participation` 与四种滑点模型（`backtest/slippage.py`） |
| 无 Almgren-Chriss 类冲击模型 | 容量模块用平方根代理（`factors/evaluate/capacity.py`） |
| 无实盘/券商接口 | 只有模拟盘 `paper/`；`paper/__init__.py:6` 把实盘列为后续阶段 |

### 2.4 数据资产硬缺口（与代码无关，但会决定所有回测结论）

| 缺口 | 证据 |
|---|---|
| **2025 年日线整年缺失** | `ls data/parquet/daily/year=2025/` 只有 `part-0.parquet.bak-demo` 与 `.lock`，无 `part-0.parquet` |
| **退市股无行情 → 幸存者偏差仍在** | `docs/BACKTEST_BASELINE_E2E.md:161`：`security` 有 339 条 `delist_date`，在日线湖里出现过的 = 0 |
| 分钟线湖不存在 | 代码与 schema 齐备但无数据 |
| 无公司行为事件表 | 复权靠 `adj_factor` 启发式 |
| 多源真冗余缺失 | ingest 不走 `FallbackProvider`（`docs/research/qlib/02-lquant-current-state.md` G4，本次 `grep` 复核：`data/ingest/*.py` 无 `build_chain`/`FallbackProvider` 调用） |

### 2.5 模型与实验

| 缺口 | 证据 |
|---|---|
| 模型 zoo 只有 3 个后端 | `research/ml/model.py:219-221`：lightgbm / gbrt / ridge |
| 无 qlib 式实验追踪全量同步 | 只有 `ml_run` + `qlib_run` sqlite |

---

## 3. 借鉴的判定规则（本目录所有结论都按此过滤）

1. **已有更强 → 不推荐**（例：撮合规则、因子评价广度）。
2. **已有但同构 → 只推荐"补口径/补方法"，不推荐换实现**（例：预处理方法扩容）。
3. **真缺且与「A 股日频选股」定位相符 → 推荐，并给出集成点**：
   - 新数据源 → `PROVIDERS` 注册表 + `config/providers.yaml` + `Capability` 枚举
   - 新数据面 → `data/ingest/` + `data/store/ddl.py` + `data/schema.py`
   - 新算子 / 预处理 → `@op` / `METHODS` 注册表
   - 新权重 / 优化器 → `portfolio/weighting.py::METHODS`
   - 新外部引擎 → `backtest/adapter.py::register_adapter`
   - 新看板采集器 → `market/collectors/__init__.py::collector()`
   - 新任务类型 → `server/jobs.py::QUEUES` + `server/api/task_center.py::KINDS`
4. **定位不符 → 明确列为"不做"**（例：期货/期权、日内高频、实盘下单），并给出理由。
