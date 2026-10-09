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
- **部署前 git 守卫**：`./lquant.sh update` 要求当前在 `main` 分支且工作树干净（含未跟踪文件），
  否则拒绝执行，避免静默部署旧代码；`LQ_DEPLOY_BRANCH` 可指定其他分支，`LQ_ALLOW_DIRTY=1` 可跳过干净校验

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
# 钉钉加签同理：LQ_DINGTALK_WEBHOOK_URL + LQ_DINGTALK_SECRET
# 其余通道的 env（变量名见 src/lquant/notify/channels.py 各通道 docstring）：
#   Telegram     LQ_TELEGRAM_BOT_TOKEN + LQ_TELEGRAM_CHAT_ID
#   ntfy         LQ_NTFY_URL（含 topic，如 https://ntfy.sh/my-topic）[+ LQ_NTFY_TOKEN]
#   PushPlus     LQ_PUSHPLUS_TOKEN
#   Server酱³    LQ_SERVERCHAN3_SENDKEY
#   通用 webhook LQ_GENERIC_WEBHOOK_URL（自建接收端）
lq notify status          # 自检：逐个通道报告就绪/未就绪与缺失的 env（不真发）
lq notify test            # 各通道发一条测试消息并逐个回报
```

支持：企业微信 / 飞书（含加签）/ 钉钉（含加签）/ Telegram / ntfy / PushPlus /
Server酱³ / 通用 webhook（自建接收端）。长消息按各通道上限**分片续发**
（续片带 `[续 i/n]` 编号），不截断丢信息。

### 告警规则引擎

`POST /api/notify/rules` 可注册阈值规则（即 `notify/rules.py` 的 `ALERT_TYPES`
全集：价格上下限 `price_above`/`price_below`、涨跌幅双向 `pct_change_up`/
`pct_change_down`、因子 IC 下限 `ic_below`），命中即推 `alert` 通道，冷却期落
SQLite 防重复轰炸；DryRun 评估接口方便前端预览，评估异常显式报
`evaluation_error` 不静默。

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

### 评价口径：as-of 指数成分（防幸存者偏差）

评价 / trace / 冗余分析 / 因子合成 / ML 训练与推理的股票池，在请求带区间时自动取
「区间起点当日已生效」的成分快照（`IndexConsRepo.symbols_as_of`，与 JQ 方言
`get_index_stocks` 防前视同一口径）——用今天的成分回溯十年历史会引入幸存者
偏差（中途调入的大牛股被追溯进池、被调出的衰落股丢失），IC 系统性高估。

降级是显式的，不假装严格 point-in-time：

- 区间起点**早于现存最早一批快照** → 退回现存最早一批（口径已被替换，不是
  「起点当日生效」），并在响应 / 报告 / 任务结果里用 `universe_note` 写明实际用了哪一批；
- 该指数**一条已生效快照都没有** → 503 明说，不假装；
- `universe_note` 目前只在 API 响应与报告 HTML 里披露，**Web 前端尚未渲染该字段** ——
  走带池的默认评价（起点固定早于首批快照）时，需在响应体里确认口径是否被替换。

### 统计置信度：PSR / DSR / CSCV-PBO

```bash
lq backtest confidence --returns day_ret.csv --n-trials auto --strategy mom_20
# → {"sharpe_annual": ..., "psr": ..., "n_trials": ..., "dsr": ..., "expected_max_sharpe_annual": ...}
```

回答「这个 Sharpe 是本事还是运气」：PSR 显著性（偏度/峰度校正）、E[maxSR]
噪声水位、DSR 校正（López de Prado 方法论，零新依赖）。`--n-trials auto`
从实验记录器数台账——**含放弃的**：只数活下来的等于给橡皮图章盖章。网格级
过拟合检验 `backtest/confidence.py::cscv_pbo`（函数级 API；sweep 输出契约
连着前端，集成留给独立 commit，不假装已自动接入）。

### 因子在线监控闭环

```bash
lq factor ic-sync mom_20             # 逐日 IC/RankIC 落 factor_ic_daily（窗口快照替换，幂等）
30 15 * * 1-5  lq factor ic-sync mom_20   # 外部 cron：交易日收盘后
lq factor ic-health                  # ok / stale / degraded / no_data（只读 DryRun）
```

借鉴 qlib Online Serving 的「上线后持续评估」：因子入库只是起点，真正的
风险在上线后衰减。`factors/monitor.py::run_daily_check` 编排
「同步 → 健康评估 → 告警」：规则引擎新增 `ic_below` 类型（threshold=IC
下限），因子近窗口 IC 跌破下限自动走既有 8 渠道通知；单因子同步失败
不阻断但 error 逐条可见。

### 再平衡管理：换手约束 + no-trade band

```python
# 优化器内 L1 换手约束（平滑化求解 + 真 L1 事后核验，与 TE 约束同构）
enhanced_indexing_weight(..., max_turnover=0.05, prev_weights=prev)
# 权重输出层 no-trade band：小偏离是噪声驱动的纯摩擦，不动
weights(M, "enhanced_indexing", syms, scores=..., band=0.03, prev_weights=prev)
```

cost_matrix 已证明高换手是收益杀手，这里给权重层装刹车；band 吸收后
权重和 < 1 即现金缓冲。

### 看板另类数据因子化（EP-9）

资金流 / 龙虎榜 / 涨停池三张表在 `market/collectors` 采集已久、因子层零
消费 —— EP-9 把它们做成 `CovariateProvider`（`factors/sources/board.py`，
6 个特征：主力净占比、3 日主力净占比、上榜标记、5 日净买合计、连板数、
20 日炸板数），G0-G3 门禁从此能评价情绪/资金面因子：

```bash
lq factor eval "cov_lhb_on_board * cov_mf_main_ratio" --cov mf_main_ratio,lhb_on_board
```

防前视是生命线：龙虎榜 T 日盘后晚间定型、资金流/涨停池盘中会漂，所有特征
按 T 日算好再组内 `shift(1)` —— **T+1 行的因子值只能看到 ≤T 日的看板数据，
宁可晚一天，不可用未来**。语义分界照 covariates 三硬约束：未上榜/当日无流
数据 = 0（业务事实），表未同步 = `CovariateUnavailable`（coverage=0 显式
上报，绝不填 0 冒充）。submit spec 可声明 `covs:`，G0 字段白名单随实际
挂载的列走 —— 面板里有什么字段，校验就认什么；covariate 数据不可用时
在 RECOMPUTE 阶段最早暴露，不会入库后因子值全 null 静默失效。

## 个股分析 / 行业分析

两个**对称**的能力域，产出同一种报告（0–100 分 + 分角度可解释指标），
通用契约原语集中在 `src/lquant/core/report.py`，只有一份实现：

| | 个股分析 `src/lquant/security` | 行业分析 `src/lquant/industry` |
|---|---|---|
| 输入 | 一个代码 `600519.SH` | 一个行业（`801780.SI` 或中文名「银行」） |
| 角度 | 技术面 / 基本面 / 估值 / 资金面 / 行业与相对强度 / 消息面 | 趋势与轮动 / 景气度 / 估值 / 资金与拥挤度 / 宽度与情绪 |
| API | `GET /api/security/{symbol}/analysis` | `GET /api/industry/{identifier}/analysis`、`/rotation`、`/list` |
| 前端 | `/security/[symbol]` | `/industry`（轮动榜）+ `/industry/[identifier]`（详情） |

行业分析的口径要点（详见 [`docs/行业分析能力设计.md`](docs/行业分析能力设计.md)）：

- **行业指数是成分股等权合成的**，不是交易所/申万官方指数 —— 报告、API、
  前端三处都显式声明，避免被当成官方指数用；
- **PIT 安全**：行业归属按 `industry_classify.std_date` 做 as-of join
  （分类变更逐日生效），财务按 `pub_date`，行情/估值/资金流按 `trade_date`；
- **五个角度**：趋势与轮动（含 **RRG 相对旋转图**四象限）、景气度（PIT 财务中位数
  + 相对全市场超额 + 环比动能）、估值（自身历史分位 + 全市场横向分位）、
  资金与拥挤度（**拥挤度为反向指标**）、宽度与情绪（含 **NH-NL 净新高占比**）；
- **缺失即标注**：任何角度取不到数 → `available=false` + 补齐方式，
  绝不补一个「中性 50 分」把空缺填平。

```bash
curl 'localhost:8000/api/industry/rotation?window=20'   # 全行业轮动榜
curl 'localhost:8000/api/industry/银行/analysis'          # 单行业多角度报告
```

MCP 侧对应 `get_industry_rotation` / `get_industry_analysis` 两个工具（供「问 AI」调用）。

## 技术指标

`src/lquant/indicators` 注册表驱动（趋势/摆动/量能/**形态**/通道五类），
`GET /api/data/indicators/registry` 自动枚举（`GET /api/data/indicators` 是
按 `symbol` 算单票指标值的端点，`names` 默认 `ma,macd,rsi,boll`）；形态因子包
（十字星/锤头/射击之星/看涨看跌吞没/早晨之星）以 0/1 信号列供图表与单票分析
消费，信号记在确认日、全部过 `assert_no_lookahead` 前缀不变性门禁，一字板不
误报。这些信号列（含 `cyq_*` 筹码类）**尚未接入因子评价面板与 G0 字段白名单**
—— 因子面板字段来自 `read_daily()` + covariates，`lq factor check pattern_doji`
会以 `STATIC_FAIL` 拒绝，接入是独立的一步工作。

## 仓库地图

| 目录 | 职责 |
|---|---|
| `src/lquant/core` | 配置 / 类型 / 注册表 / 日历 / 单写者 DB 连接 / **报告契约通用原语**（`report.py`） |
| `src/lquant/data` | Provider 抽象、多源适配、入库、质量校验、存储 |
| `src/lquant/security` | **个股分析**：多角度报告（技术/基本面/估值/资金/相对强度/消息） |
| `src/lquant/industry` | **行业分析**：行业轮动榜 + 多角度报告（趋势与 RRG/景气度/估值/拥挤度/宽度） |
| `src/lquant/factors` | DSL、算子、预处理、评价、在线监控（IC 日表 + 健康度）、看板另类数据协变量（EP-9） |
| `src/lquant/backtest` | 规则表、撮合引擎、账户、策略、实验记录器（list/show/diff）、统计置信度（PSR/DSR/PBO） |
| `src/lquant/portfolio` | 选池 / 去重 / 权重（no-trade band）/ 优化器（TE + 换手约束）/ 选股策略库（一策略一纯函数）/ 仓位模型（ATR 风险预算 + Kelly） |
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
