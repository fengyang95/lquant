# lquant 因子模块 · 整合方案

> 基于真实仓库 `/Users/lyp/code/lquant`（git main）实测。
> 四项需求：① 因子接入（分来源）② 自动挖掘（Agent 可插拔）③ 结果正确性 ④ 中性化。
> 另含研报复现与 Agent 接入架构。本文只讲关键问题与决策，细节实现从简。

---

## 一、先看关键问题（实测发现，非推测）

### 1.1 最严重：默认中性化是静默假动作

`preprocess/` 四个 stage（去极值/标准化/中性化/正交化）**实现完整**，但三层全断：

| 层 | 现状 |
|---|---|
| 内核 | ✅ OLS/Ridge/Lasso/行业均值剔除齐全，`engine.compute_many(steps=...)` 有参数 |
| 接线 | ❌ **API 与前端零暴露**（`server/api/factors.py`、`web/` 里 preprocess 相关零命中） |
| 数据 | ❌ **默认协变量 `["market_cap","industry_sw1"]` 在 schema 中不存在** |

数据侧：`DAILY_BAR` 无市值列；行业在独立 `INDUSTRY` 表（列名 `std`/`code`）。
于是 `_regress.split_levels` 对缺失列 `continue` → 设计矩阵只剩截距 →
**OLS 残差退化为 `y - mean(y)`**：所谓"市值行业中性化"实际只做了截面去均值，完全静默。

### 1.2 其余关键缺陷（按危害排序）

| # | 位置 | 问题 | 危害 |
|---|---|---|---|
| 12 | `factors/cache.py::key` | 签名**不含 `steps`** | 换预处理配方针中旧缓存，"改了参数没反应"，破坏可复现 |
| 1 | `api/factors.py::_compute_factor` | `except KeyError: pass` 探测内置因子 | 吞掉真缺列错误，误导成 422 |
| 6 | `dsl/analyzer.py` | **不校验字段合法性** | 拼错 `$float_mv` → 全 null → IC 全 NaN，无声 |
| 4 | `web/.../factors/page.tsx` | `catch { setEvalSeries(null) }` | 图表接口失败被吞，显示"样本不足"实为 500 |
| 2 | `api/factors.py` | `/evaluate` 与 `/evaluate/series` 各全量重算 | 2 倍开销，两次结果可能不一致 |
| 10 | `factors/dsl/` | **只有 parse（字符串→AST），没有 printer（AST→字符串）** | GP 交叉变异、LLM 结构化解、跨来源去重全做不了 |
| 9 | `factors/ops/` | 算子仅 10 个 | 连 Alpha158 都表达不全 |
| 5 | `config/factors/custom.yaml` | 全仓库无代码加载 | 死配置 |
| 7 | `analysis.py::synthesize` | 嵌套 `over()`（Polars #25691 模式） | 需测试确认，该模式不报错但结果错 |

**共性：全是静默错误。** 仓库 `BACKTEST_VALIDATION.md` 那句话对因子更成立——
错了不报错，只会让你亏钱，且 IC 虚高的因子看上去特别漂亮。

### 1.3 已有资产（方案的地基，不必重造）

- `preprocess/` 全套（问题只在接线与协变量）
- `docs/BACKTEST_VALIDATION.md` 六层验证（L1–L6）+ `validate_backtest.py` —— 直接照抄为因子版
- `factors/cache.py` 两级缓存（锚 data_version）、`engine.compute_many` DAG
- `lq` CLI 骨架（click，data/factor/backtest/strategy 四组）
- `factors/dsl/` lexer/parser/analyzer/compiler（缺 printer）
- 门禁三件套原料：`dsl/analyzer`（合法性）、`analysis.correlation`（去重）、`evaluate/*`（多维评估）

---

## 二、需求一：因子接入（分来源筛选）

**根因**：`factor_def` 只有 4 列、无 source 字段，来源靠 `description` 前缀隐式编码；
`_compute_factor` 硬编码快捷公式与 `custom.yaml` 两类来源根本进不了表。

**方案**：`factors/sources/` + `SOURCES` 注册表（沿用仓库 Registry 模式），
`factor_def` 加 `source`/`source_ref`/`factor_id` 一等字段。

**一条硬规则**：

> **Adapter 只做翻译，绝不做执行。** Alpha101 的 `rank(ts_argmax(...))` 必须转成
> lquant DSL 过 analyzer —— 绝不引入第二套执行语义，否则每接一个来源多一套口径，
> 将来 IC 对不上无从排查。

顺带修：字段白名单校验（缺陷 #6）、`custom.yaml` 接活（#5）。

---

## 三、需求二：自动化因子挖掘

### 3.1 第一原则：挖掘器只产字符串，不碰数据

调研（AlphaGen/AlphaForge/CogAlpha/FactorMiner…）剥掉搜索算法后是同一个三件套：
**① 合法性约束 → ② 相关性去重 → ③ 多维评估**——而这三件套 lquant 已全有（见 1.3）。

所以挖掘器只输出表达式字符串，所有计算走统一 `FactorEngine`，
天然享受缓存、未来函数检测、统一 IC 口径。**能吐字符串就能接。**

前置缺口：**必须先补 `dsl/printer.py`（`unparse` + `canonical`）**——
GP 交叉变异、LLM 结构化解、跨来源规范化去重全靠它，`canonical()` 同时是接入侧 `factor_id` 去重的基础。

### 3.2 门禁四阶段（G0→G3），每阶段带淘汰原因码

```
G0 静态（parse/check/量纲，微秒）→ G1 快筛（抽样+中性化 IC，毫秒）
→ G2 去重（|ρ|<0.7，秒）→ G3 全量+样本外复核（仅解锁一次）
```

**淘汰必须返回结构化原因码 + 可操作提示**（`LOW_IC`/`REDUNDANT`/`SIZE_PROXY`…）。
只返回分数 = 把 Human-in-the-loop 降级成随机搜索——外部 Agent 拿到
"与 mom_20 相关 0.82，建议换字段族"才知道下轮往哪走。

样本外划分 70/15/15，搜索全程只用前 70%，OOS 段只在最终复核解锁一次。

### 3.3 两条纪律

1. **`random` 基线必须先建立**，任何生成器的验收 = 同预算下跑赢 random。
   没有对照的"优化"是自欺。
2. **多次试验校正内建**：评估 2000 个表达式后挑最好的本身是巨大的多重检验问题，
   门槛用 `sqrt(2·ln(n_trials))`（2000 次 → 3.90 而非 1.96），UI 上与原始 t 并排显示。

---

## 四、需求三：结果正确性（F1–F6 + N1–N6）

照抄回测六层范式建 `docs/FACTOR_VALIDATION.md` + `test_factor_accuracy.py` + `validate_factor.py`。

**F1–F6**：金标准手算 / 数学恒等式 / 性质测试 / 指标交叉核对 / 交叉实现对照 / 可复现性。

三条杀手锏：

| 断言 | 抓什么 |
|---|---|
| **完美因子 IC == 1.0**（构造与未来收益严格线性相关） | 端到端"证真"，最强的一条 |
| **严格单调变换下 RankIC 完全不变** | Pearson/Spearman 混用、分组按原值而非秩 |
| **截断不变性**：T 日截断后此前因子值与 IC 逐点一致 | 未来函数、全样本统计量泄漏（最隐蔽） |

恒等式里还有两条高频错误探测器：`IC(f - mean_d) == IC(f)`（抓 `over()` 分组列写错）、
`IC(-f) == -IC(f)`（抓多空方向反）。

**中性化专项 N1–N6**（挂 F 体系）：N2 **自中性化归零**
（`neutralize(market_cap, ["market_cap"]) == 0`，一句话验证整条回归链路）、
N4 **残差与设计矩阵正交**（OLS 一阶条件）、N6 **协变量全缺失必须上报 coverage=0 并告警，
不得静默去均值**——把 1.1 的缺陷钉死在 CI 上。

---

## 五、需求四：中性化

### 5.1 分清三件事（都叫中性化，数学不等价）

| 名称 | 做法 | 用途 |
|---|---|---|
| 因子值中性化 | 因子~协变量回归取**残差**再算 IC | **默认**（Barra/alphalens 主流） |
| 收益中性化 | 收益~协变量回归，因子对**残差收益**算 IC | 对照视图 |
| 行业内分组 | 分层回测**行业内分组**组内选股 | 分层回测选项 |

页面上必须标明用的哪种，否则数字没法对话（研报对不上账的最常见原因）。

### 5.2 CovariateProvider：市值/行业做成一等公民

不在 `daily_bar` 里、来源多样 → Registry 模式，预置
`market_cap`(log)/`float_mv`/`industry_sw1`/`beta`/`momentum_1m`/`turnover_1m`…

三条硬约束：

1. **必须 PIT**：行业按 `std_date` as-of 关联——用今天的分类回测十年前 = 前视偏差
2. **覆盖率显式上报**：coverage<80% 图上打阴影；覆盖率本身是结论的一部分
3. **缺失整行剔除，绝不填 0**：缺失市值填 0 = 当成最小市值，引入系统性偏差

另：市值必须取 `log` 再进回归（缺陷 #13），否则被巨无霸绑架。

### 5.3 页面核心：IC 归因阶梯

```
原始 IC        0.052  ████████████
+市值中性      0.031  ███████       ↓ 40%
+行业中性      0.024  █████         ↓ 54%
+换手率中性    0.021  ████          ↓ 60%   ← 六成是风格暴露
```

逐段叠加协变量看 IC 怎么掉，**一眼回答"这因子到底在赚什么钱"**。
列表页加 `IC(中性化)` 列且默认按它排序，否则前列永远是小市值变体。

### 5.4 必须先于挖掘落地

不中性化，GP 前几代就收敛到小市值变体（`-Rank($close)` 原始 IC 极高），
后面全在换皮。因此：**G1 快筛与适应度一律用中性化后 IC**，
新增惩罚 `ic_decay = 1 - |IC_中性|/|IC_原始|`（hinge，阈值 0.5），
衰减>80% 打 `SIZE_PROXY` 淘汰并回传提示——对外部 Agent 是"别再往动量撞"的强信号。

---

## 六、Agent 接入：CLI-first + Skill 化 + 统一注册

### 6.1 控制方向：Agent 开车，平台当工具台和裁判

- **Agent 驱动（主路线）**：平台能力做成 CLI，以 skill 提供给 Agent
  （Claude Code / WorkBuddy / 人类研究员），Agent 自主挖掘、验证、复现研报，
  结果经 `submit` 回传
- **平台驱动（辅，P2）**：内置 GP/枚举等算法无人值守跑批（原 HTTP 会话 API 降级至此）
- **契约不变**：门禁/口径/入库管线是同一套，变的只是谁来按回车

### 6.2 CLI 命令面（现有 `lq` 扩展，全是薄封装）

```bash
lq data fields                    # 字段白名单 + 覆盖率
lq factor check "<expr>"          # G0 静态校验（毫秒，永远第一步）
lq factor eval "<expr>" ...       # IC/ICIR/分层/换手 JSON + 中性化对照
lq factor series / corr           # 序列数据 / 库内查重自查
lq backtest run --factor ...      # 回测（复现的验证终点）
lq factor submit spec.yaml        # ★ 唯一入库通道，服务端重验
lq agent list/show/test/run/freeze# Agent 注册与探针验证
```

`eval` 响应内建：metrics + 中性化对照 + `n_trials` + **校正 t 门槛** + 剩余配额 + hints。
CLI stderr 带结构化淘汰原因码——Agent 读错误即自我修正。

### 6.3 三条硬护栏

1. **算归平台**：Agent 只编排，永远不自算指标；submit 服务端全量重算，
   平台不信任 Agent 的任何数字
2. **预算内建**：eval 配额按 Agent 记账，每次响应返回随 `n_trials` 上升的校正门槛——
   让 Agent 亲眼看着自己的"显著"标准水涨船高
3. **submit 即重验**：重跑 G0–G3（含样本外），A/B 级才入库

### 6.4 统一 Agent 注册：一个注册表，差异只在 driver

`config/agents/*.yaml`，`kind × driver` 正交组合：

| kind × driver | 对应谁 | 怎么跑 |
|---|---|---|
| `builtin + platform` | 内置算法（gp/enumeration/llm） | 平台循环驱动 |
| `skill + agent` | **Claude Code / WorkBuddy / 人类** | 装 SKILL.md 走 CLI，Profile 带权限（配额/可否 submit/可否导原始数据） |
| `external + platform` | 远程专用挖掘器 | HTTP / 子进程 JSON stdio / MCP |

**公共不变量**：同一条门禁（循环与 submit 重验是同一套代码的两个入口）、
同一套口径、同一份记账（`factor_mining_run.agent` 记名）、同一张注册表。

**接入入口三件套**：配置 yaml（fail-fast）· `lq agent` CLI
（**`lq agent test` 即接入验收**：探针表达式验证链路）· `/factors/mine`「Agent 管理」面板
（kind 徽标卡片、权限开关、生成一次性接入指引；运行视角全部统一）。

SKILL.md = Agent 的操作手册 + 纪律（永不自算指标、eval 前先 check、盯校正门槛、
提交前自查 corr、rationale 必填），随仓库版本化，冻结快照含其 hash。

---

## 七、研报复现：挖掘的输入，对话式完成

**定位**：研报是自动挖掘的**输入**，不是平行功能。复现 = 对话式 skill 工作流：

```
读研报(PDF) → 抽 FactorSpec → 对话确认 assumptions → check → eval
→ 比对 claimed → 偏差归因（max 5 轮）→ lq backtest run → submit
```

**谁做什么**：只有①读懂研报、②落地 FactorSpec 必须 LLM；
③计算 ④比对归因绝不能交给 LLM——LLM 算指标会错得很自信。
（这条与挖掘第一原则同源：Agent 只许说话，不许算数。）

**架构上是同一内核的新输入**：提案来自研报文本、目标从"最大化 IC"变为
"最小化与宣称值的距离"，其余（校验/门禁/评估/入库）全复用。

**FactorSpec 关键字段**：`claimed`（宣称值 = ground truth）、`assumptions`
（研报没写的显式记假设，敏感性分析用）、`neutral_spec`（未声明则留 null + 提示，
**严禁 LLM 代猜**）、窗口/股票池/剔除规则/预处理配方。

**价值在说清楚差在哪**：归因五类（`EXPR_MISREAD`/`PARAM_ASSUMED`/`DATA_CALIBER`/
`WINDOW_MISMATCH`/`REPORT_SUSPECT`），分级 A 完全复现 / B 趋势一致 / C 无法复现 /
**D 研报存疑**。**D 级最值钱**——检出研报用了幸存者偏差/未来函数，
手段正好复用 F3 截断不变性。A/B 入库带 `claimed`（将来可评估信息源可信度）；
C/D 落 `factor_replication` 表作为研究资产。

---

## 八、落地路线

| 阶段 | 内容 | 验收要点 |
|---|---|---|
| **M0 止血** | 修缺陷 1/2/4/6 + 算子补到 ~30 | 拼错字段立刻报错；图表失败 UI 可见 |
| **M1 质量地基** | FACTOR_VALIDATION F1–F4 + `validate_factor.py` + **cache.key 纳入 steps（#12）** | 完美因子 IC=1.0；截断不变；换配方不命中旧缓存 |
| **M2 来源接入** | `factors/sources/` + source 字段 + 筛选 UI + custom.yaml 接活 | 按来源筛选；Alpha158/YAML/手动各有正确标记 |
| **M2.5 中性化（前置）** | CovariateProvider（PIT+覆盖率）+ 修 #11/#13 + 配方接入 API/UI + 归因阶梯 + N1–N6 | 自中性化归零；残差正交；**默认配方真的做了市值行业中性** |
| **M3a–e 挖掘内核** | printer/canonical → 门禁+Fitness+runner → random 基线 → gp → llm | canonical 等价同 hash；**gp 同预算跑赢 random（否则不合并）** |
| **M4a 前台** | `/factors/mine` 三视图 + Agent 管理面板 | 漏斗可下钻；三类 Agent 卡片齐全 |
| **M4b Agent 接入** | `lq agent` 命令组 + 统一注册表 + 因子 CLI 命令面 + 配额记账 + SKILL.md | `lq agent test` 三类全过；Claude Code 只靠 SKILL.md+CLI 独立闭环 |
| **M4c 提交重验** | submit 重跑门禁 + `factor_replication` 表 | 谎报 IC 被服务端重算推翻并归档正确数字 |
| **M5（辅）** | llm-explorer 两段式（平台驱动） | LLM 提案 → GP 精炼闭环 |
| **M6 研报复现** | 解析抽取 → skill 复现工作流 → 归因 → 分级入库 | 抽取准确率≥80% 缺失留 null；对话式走完全流程；改错参数能正确归因 |

**排序逻辑**：

1. **M0/M1 最先** —— 没有正确性地基，接入与挖掘只会放大错误
2. **M2.5 必须早于 M3b** —— 不中性化，挖掘器在反复重新发现市值因子，
   "跑赢 random"也只是比谁更像市值
3. **M4 早于 M6** —— 复现依赖 CLI 命令面、submit 重验、F3 截断不变性
   （没有它 `D 级·研报存疑` 检不出来）

**一条贯穿的主线**：平台的不可让渡职责只有两样——**测量口径**（所有指标平台算）
和**入库质量**（门禁重验）。其余编排、推理、对话，全部让位给 Agent。
