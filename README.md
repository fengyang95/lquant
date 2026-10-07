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

## 通知旁路（结果找人）

链路结果目前只能「人找看板」；`lquant/notify` 把关键结果主动推出去 ——
首个场景是**模拟盘对账告警**：`day_close` 对账 verdict 为 warning/critical
（官方收盘价与盯市价背离）时自动推送，ok 静默不刷群。

```bash
export LQ_NOTIFY_CHANNELS=wecom,feishu            # 逗号分隔；不配 = 功能关闭
export LQ_WECOM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..
export LQ_FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/..
# 飞书开了「签名校验」再加（密钥只走 env）：
export LQ_FEISHU_WEBHOOK_SECRET=..
# 钉钉加签同理：LQ_DINGTALK_WEBHOOK_URL + LQ_DINGTALK_WEBHOOK_SECRET
lq notify status          # 自检：看 env 配置出了哪些通道
lq notify test            # 各通道发一条测试消息并逐个回报
```

支持：企业微信 / 飞书（含加签）/ 钉钉（含加签）/ Telegram / ntfy / PushPlus /
Server酱³ / 通用 webhook（自建接收端）。长消息按各通道上限**分片续发**
（续片带 `[续 i/n]` 编号），不截断丢信息。

### 告警规则引擎

`POST /api/notify/rules` 可注册阈值规则（价格/涨跌幅/量比/换手四类），
命中即推 `alert` 通道，冷却期落 SQLite 防重复轰炸；DryRun 评估接口
方便前端预览，评估异常显式报 `evaluation_error` 不静默。

### 消息分类与降噪

`notify(title, text, category=..., severity=...)`：report/alert/error 三类
可路由到不同通道子集；进程内降噪（去重指纹 TTL / 冷却 / 静默时段，
critical 豁免）防止告警风暴刷群。

### 自选股每日报告

```bash
lq notify digest          # 对 watchlist 逐票跑多角度分析 → 汇总推送 report 通道
20 15 * * 1-5  lq notify digest    # 外部 cron 定时：交易日 15:20
```

### 组合绩效日报

```bash
lq notify portfolio --account demo   # 净值/当日盈亏/回撤/TOP1-TOP3 集中度 → report 通道
25 15 * * 1-5  lq notify portfolio --account demo   # 外部 cron：交易日 15:25
```

绩效一律取**官方净值口径**（收盘对账重算值）；账户不存在显式 skipped，
快照失败走 error 通道，绝不静默。

### 回测实验记录器

```bash
lq backtest run --factor "mom_20"    # 默认落库 backtest_run（params 附 git_hash）
lq backtest list                     # 实验清单（CLI 与 Web API 落库同源）
lq backtest diff <run_a> <run_b>     # params/metrics 键级对比，代码版本变化显式可见
```

纪律与仓库一致：**零新依赖**（stdlib urllib）、**永不阻断主链路**（通道炸了
对账照常）、**密钥只走 env**（webhook URL 本质是凭证，不入 config/仓库）。

## 技术指标

`src/lquant/indicators` 注册表驱动（趋势/摆动/量能/**形态**/通道五类），
`GET /api/data/indicators` 自动枚举；形态因子包（十字星/锤头/射击之星/
看涨看跌吞没/早晨之星）以 0/1 信号列反哺因子层，信号记在确认日、
全部过 `assert_no_lookahead` 前缀不变性门禁，一字板不误报。

## 仓库地图

| 目录 | 职责 |
|---|---|
| `src/lquant/core` | 配置 / 类型 / 注册表 / 日历 / 单写者 DB 连接 |
| `src/lquant/data` | Provider 抽象、多源适配、入库、质量校验、存储 |
| `src/lquant/factors` | DSL、算子、预处理、评价 |
| `src/lquant/backtest` | 规则表、撮合引擎、账户、策略、实验记录器（list/show/diff） |
| `src/lquant/portfolio` | 选池 / 去重 / 权重 / 选股策略库（一策略一纯函数）/ 仓位模型（ATR 风险预算 + Kelly） |
| `src/lquant/indicators` | 技术指标注册表（五类）：CYQ 筹码、K线形态信号列等 |
| `src/lquant/market` | 看板热通路（实时采集）/ 自选股与组合绩效日报编排 |
| `src/lquant/notify` | 通知旁路：企微/飞书/钉钉/TG webhook，对账告警接线 |
| `src/lquant/research` | JQ 方言兼容、ML 选股、研报复现、研报→因子提案（LLM 接 G0 门禁） |
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

## 密钥与公开仓库卫生

公开仓库暴露的是**全部历史**，不只是当前文件，所以加个推送钩子是不够的：

```bash
make secrets-install    # 装 gitleaks（免 sudo，装一次所有 worktree 共用）
make secrets-history    # 全历史 + 所有分支审计
make public-ready       # 公开前体检：全历史 + 工作区 + 卫生（严格）
```

`pre-commit` / `pre-push` 钩子会自动扫密钥，与 CI 共用同一份规则
（`.gitleaks.toml`）。钩子能用 `--no-verify` 绕过，CI 绕不过。
真出事了**先轮换凭证、再清理历史**——改写历史不能让已流出的值失效。
完整机制、GitHub 网页操作清单与补救手册见
[`docs/SECRET_HYGIENE.md`](docs/SECRET_HYGIENE.md)。
