# lquant

面向 A 股（个股 + ETF）的 **数据接入 → 因子分析 → 量化回测 → 市场看板 → 模拟盘** 一体化平台。

- 后端 Python 3.12+（Polars / DuckDB / FastAPI）
- 核心算法 Rust（通过 `pyo3-polars` 表达式插件 + `maturin` 接入，可选）
- 前端 Node.js / Next.js 15
- 数据主源 BaoStock（免费、免注册、支持 ETF），多源可切换

> 设计文档见仓库根目录 `../量化平台设计文档.html`（单文件，双击可看）。

## 快速开始（一键）

```bash
./lquant.sh all          # 安装全部依赖 + 生产构建打包 + 启动全栈
```

或分步：

```bash
./lquant.sh install      # 自动装 uv/依赖/前端包 + 建库（含国内镜像加速）
./lquant.sh bootstrap    # 拉地基数据：交易日历 + 标的清单 + ETF 元数据 + 哨兵池日线
./lquant.sh start        # 启动 API(:8000) + Worker + Web(:3000)，自动健康检查
./lquant.sh status       # 查看运行状态
./lquant.sh stop         # 停止
```

常用运维命令：

| 命令 | 说明 |
|---|---|
| `./lquant.sh build` | 打包：前端 standalone 生产构建 + 后端 wheel + 发行 tar.gz |
| `./lquant.sh bootstrap --full` | 全市场日线回填（首次约 20-30 分钟，断点续传） |
| `./lquant.sh restart` / `logs api\|web\|worker` | 重启 / 追日志 |
| `./lquant.sh doctor` | 环境体检（缺什么一目了然） |
| `make dev` | 开发模式（热重载） |

### 无 Redis / 无 cargo 也能跑

- **Redis 缺失**：任务队列自动降级为 API 进程内本地线程执行，启动不阻塞
- **cargo 缺失**：自动安装 rustup（国内走 rsproxy 镜像，可用 `LQ_RUSTUP_DIST_SERVER` 覆盖）；
  装不上才降级为纯 Python 参考实现（`src/lquant/_rust/*.py`）
- 脚本默认走清华 PyPI 镜像 + npmmirror，可用 `LQ_PYPI_INDEX` / `LQ_NPM_REGISTRY` 覆盖

## 数据层速览

```bash
lq data reference          # 交易日历 + 标的基础表（地基，先跑这个）
lq data sync               # 日线回填（看门狗 + 断点续传）
lq data etf                # ETF 元数据（含 per-instrument T+N 推断）
lq data minute --freq 60min # 分钟线（默认中证 800 池）
lq data financial --symbols 600000.SH,510300.SH   # PIT 财务（stat_date+pub_date）
lq data status             # 数据覆盖度一览
```

关键设计：

- **看门狗**：BaoStock 批量请求会静默挂起（实测 12 分钟无返回），所有网络调用走子进程超时强杀
- **断点续传**：长任务每批写 `data/cache/checkpoints/*.json`，中断重跑自动跳过已完成项
- **防未来函数**：财务必须带 `pub_date`；上市/退市日期入 `security` 表防幸存者偏差
- **T+N per-instrument**：跨境/债券/黄金/货币 ETF = T+0，股票 ETF = T+1，存 `etf_meta.sellable_after_days`

## 仓库地图

| 目录 | 职责 |
|---|---|
| `src/lquant/core` | 配置 / 类型 / 注册表 / 日历 / 单写者 DB 连接 |
| `src/lquant/data` | Provider 抽象、多源适配、入库、质量校验、存储 |
| `src/lquant/factors` | DSL、算子、预处理、评价 |
| `src/lquant/backtest` | 规则表、撮合引擎、账户、策略 |
| `src/lquant/portfolio` | 选池 / 去重 / 权重 |
| `src/lquant/market` | 看板热通路（实时采集） |
| `src/lquant/research` | JQ 方言兼容、ML 选股、研报复现 |
| `src/lquant/server` | FastAPI + RQ(可降级) + WebSocket |
| `src/lquant/cli` | `lq` 命令行 |
| `crates/` | Rust 内核（lq-ops / lq-backtest / lq-metrics） |
| `web/` | Next.js 前端 |
| `config/` | 应用配置、规则表、因子定义 |
| `lquant.sh` | 一键安装 / 启动 / 打包脚本 |

## 三条硬约束

1. **代码格式统一** `000001.SZ` / `510300.SH`，绝不用裸 6 位（`000001` 是歧义码）
2. **时间统一 ISO 8601 + Asia/Shanghai 时区**，不做隐式本地时间假设
3. **金额统一元**，源站万元/亿元在 adapter 层换算并断言

## 每个 Rust 模块必须有 Python 参考实现

`src/lquant/_rust/*.py` 是纯 Polars 实现，Rust 扩展加载失败时自动降级。
这不是洁癖——是一个人扛不住编译失败全线停摆的现实。
