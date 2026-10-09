# 仓库结构速览

```
lquant/
├── config/          应用配置、撮合规则表、因子定义
├── src/lquant/
│   ├── core/        配置 类型 注册表 日历 单写者DB 报告契约原语
│   ├── data/        Provider抽象 适配 入库 质量 存储
│   ├── security/    个股分析（多角度报告）
│   ├── industry/    行业分析（轮动榜 + 多角度报告）
│   ├── factors/     DSL 算子 预处理 评价
│   ├── backtest/    规则 撮合 账户 策略
│   ├── portfolio/   选池 去重 权重
│   ├── market/      看板热通路
│   ├── research/    JQ方言 ML 研报
│   ├── paper/       模拟盘
│   ├── server/      FastAPI + RQ + WS
│   ├── cli/         lq 命令
│   └── _rust/       Rust 桥接 + Python 参考实现
├── crates/          lq-ops / lq-backtest / lq-metrics
├── web/             Next.js 15
├── scripts/         setup / dev / build_rust / init_db
└── tests/           unit / integration
```

## 模块职责与关键类

| 模块 | 关键类 / 函数 | 说明 |
|---|---|---|
| `core/types.py` | `Symbol` `parse_symbol` `Board` | 代码归一，ETF 段自建 |
| `core/registry.py` | `Registry` | 注册表 + 自省（前端枚举 UI） |
| `core/db.py` | `writer()` `reader()` | DuckDB 单写者约束 |
| `core/report.py` | `composite_scores` `metric` `to_score` `percentile_rank` `json_safe` | 报告契约通用原语（个股 / 行业分析共用，单一实现源） |
| `security/service.py` | `analyze_security` | 个股多角度报告（技术/基本面/估值/资金/相对强度/消息） |
| `industry/service.py` | `analyze_industry` `industry_rotation` | 行业多角度报告 + 全行业轮动榜（趋势与 RRG/景气度/估值/拥挤度/宽度） |
| `industry/angles.py` | `trend_angle` `rrg_state` `_nhnl_latest` | 行业角度纯函数（含 RRG 四象限与 NH-NL 净新高占比） |
| `data/base.py` | `DataProvider` | 6 个核心方法 + Capability |
| `data/fallback.py` | `FallbackProvider` `HealthTracker` | 链式故障转移 |
| `data/watchdog.py` | `run_with_watchdog` | BaoStock 静默挂起唯一解 |
| `data/normalize.py` | `normalize_symbols` `to_yuan` | 三种代码格式 + 单位归一 |
| `factors/dsl/*` | `parse` `check` `compile_expr` `plan` | 字符串 DSL 全链路 |
| `factors/ops/registry.py` | `OPS` `@op` | category/min_window 元数据驱动 |
| `backtest/rules/model.py` | `RuleSet` `TaxSchedule` | 规则全数据化 |
| `backtest/broker.py` | `Broker.match` | 最低佣金按订单累计 |
| `research/dialect/` | `jq_shim` `jq_import.scan` | 导入即失败 |
