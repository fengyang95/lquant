---
name: factor-mining
description: 在 lquant 平台上做因子挖掘与因子体检：从自然语言或模板产出 DSL 候选，走 lq factor check/eval/audit/robust/corr/mine/submit 门禁，做经济直觉复核后入库。触发词：挖掘因子、因子挖掘、找因子、设计因子、评估因子、跑因子分析、因子体检、因子衰减、mine factors、alpha 挖掘。
tags: [factor-mining, alpha, factor-evaluation, dsl, quant-research]
---

# factor-mining — 因子挖掘（lquant）

> 你负责出**想法和字符串**，平台负责**算数字和当裁判**。
> 这条边界是硬护栏，不是建议：所有 IC / ICIR / 分层 / 鲁棒性数字都从 `lq` 命令的 JSON 里读，
> 你自己一次都不许算 —— 算出来的数字无法被审计，等于没有。

## 0. 六条铁律

1. **永不自算指标**。你没跑 `lq` 就没有数字，不许用手算/估算/"大约"填充。
2. **eval 前先 check**。`lq factor check` 是毫秒级静态校验（G0），永远第一步。
3. **盯校正门槛**。门槛随 `n_trials` 上升（`sqrt(2·ln n)`）。你前 100 次的"显著"，
   在第 500 次之后就不显著了 —— 响应里的 `corrected_threshold` 每次都要看。
4. **提交前自查 corr**。`lq factor corr` 确认与库内因子 |ρ| < 0.7，否则白跑一趟。
5. **rationale 必填**。submit 的 spec 里没有 rationale 会被拒。
6. **读淘汰原因码**。`LOW_IC / REDUNDANT / SIZE_PROXY / OOS_FAIL / LOW_TSTAT` 都带可操作提示，
   它就是下一轮的方向 —— 别对着同一个方向反复撞。

## 1. 操作面（这就是你的全部工具）

| 命令 | 干什么 | 什么时候用 |
|---|---|---|
| `lq data fields` | 字段白名单 + 覆盖率 | **第一步**：先看数据里到底有什么 |
| `lq factor check "<expr>"` | G0 静态校验（未来函数 / 未注册算子 / 字段白名单） | 每条候选的第一关 |
| `lq factor eval "<expr>" [--agent NAME]` | L1 快筛：IC/ICIR + 分层 + 换手 + 中性化对照 + 校正门槛 + 配额 | 候选太多时批量过一遍 |
| `lq factor audit "<expr>" [--agent NAME]` | L2 深度校验：IC/ICIR + 分层 + 衰减 + 归因 + 评级 + 样本外衰减 | 通过快筛的少数候选 |
| `lq factor robust "<expr>"` | L3 鲁棒性：窗口扰动 / 分段稳定 / 起点敏感 / 剔除最佳月份 / OOS 衰减 | 准备入库之前 |
| `lq factor series "<expr>"` | 逐日 IC/RankIC/累计 IC 序列（含 dates，平台算） | 想看"什么时候失效" |
| `lq factor corr "<e1>" "<e2>" ...` | 两两横截面 Spearman + 冗余对 | 入库前查重 |
| `lq factor mine --generator proposals --proposals f.jsonl --n N` | 批量走 G0–G3 门禁 + 记账落库 | 一批候选一次跑完 |
| `lq factor report "<expr>" --out x.html` | 自包含 HTML 研究报告 | 要给研报式交付 |
| `lq factor run --name <因子名>` | 按注册名跑 L2 深度校验（与 `audit` 同一实现） | 想复跑库内已有因子 |
| `lq factor submit spec.yaml` | ★ 唯一入库通道：服务端重跑 G0–G3，A/B 级入库 | 最后一步 |
| `lq agent list` / `lq agent test <name>` | Agent 注册与接入验收 | 首次接入、或不确定用哪个 Agent 名 |
| `lq agent run <name> --n N` | 以该 Agent 身份平台驱动跑一次挖掘会话 | 无人值守批量挖掘 |
| `lq backtest run --factor "<expr>"` | 分层多空回测（复现工作流的验证终点） | 要能落到组合收益上时 |

语法 / 算子 / 字段 / 候选模板 → 见本目录 `reference.md`（**写表达式前先读它**）。

## 2. 三级评估阶梯：先便宜后昂贵

| 层 | 命令 | 回答的问题 | 量级 |
|---|---|---|---|
| L0 | `check` | 表达式合法吗？有没有未来函数？ | 毫秒 |
| L1 | `eval` | 有没有信号？（IC 与中性化对照） | 秒级 |
| L2 | `audit` | 信号长什么样？稳不稳？能不能赚？（评级 / 单调性 / 半衰期 / 归因 / IS→OOS 衰减） | 数十秒 |
| L3 | `robust` | 换个窗口、换段样本、换个起点还在不在？ | 分钟级 |

**纪律：按 L0 → L1 → L2 → L3 逐级过滤，不要对 50 个候选直接上 L3。**
L1 就该淘汰掉大半；L2/L3 只留给真进决赛的少数几个。

## 3. 挖掘流程

### Step 1 看数据
`lq data fields`。覆盖率低（< 0.8）的字段不要拿来做因子 —— 缺一半的字段算出的 IC 没有意义。
数据为空时先 `lq data reference && lq data sync`（真实数据）或 `lq data demo`（演示数据，
**绝不能用于任何结论**）。

### Step 2 出候选（三策略，按需选一）
- **模板式（默认）**：从 `reference.md` 的分类模板出发，按经济学假设挑，别穷举窗口。
- **变异式**：拿一个已知强因子（库里的或公开的），只改一处（窗口 / 算子 / 截面变换）。
- **组合式**：2–4 个子因子各自截面 Rank 后加权。子因子先各自过 L1，别拿 4 个噪声合成一个噪声。

写候选时记住：**每条都要能写出"为什么它可能有效"的一句话**。
写不出来的候选不要提交 —— 那是随机搜索，不是研究。

### Step 3 批量静态校验
逐条 `lq factor check`。`STATIC_FAIL` 的（字段拼错 / 算子不存在 / 未来函数）当场改掉再往下走；
`check` 会给出近似字段候选，照着改。

### Step 4 写提案文件 + 跑门禁
把通过 G0 的候选写成 JSONL（每行一个对象）：

```json
{"expr": "Rank(Ts_Mean($close,5)/$close-1)", "note": "短期动量，假设强势股有延续性"}
{"expr": "-Ts_Corr(Ts_Return($close,1),Ts_Return($volume,1),20)", "note": "量价背离，放量滞涨是出货"}
```

然后一次跑完：

```bash
lq factor mine --generator proposals --proposals cands.jsonl --n 2 --agent claude-code
```

要点：
- `--n` 等于提案条数（少于文件行数 = 后面的不跑；多于也安全，跑完就停）。
- 输出里的 `n_static_fail / n_low_ic / n_redundant / n_size_proxy / n_survivors` 就是各关淘汰数，
  `corrections[].hint` 是每条淘汰的可操作理由。
- **`--agent` 是记账用的**：不传则不记配额，也就不算在平台的试验总账里。
  接入的 Agent 名先用 `lq agent list` 确认（通常是 `claude-code`），
  首次接入跑一次 `lq agent test claude-code`。

### Step 5 对幸存者做 L2/L3
```bash
lq factor audit  "<expr>" --agent claude-code
lq factor robust "<expr>"
```
`audit` 给评级 + 阻断项；`robust` 给 `verdict`（robust / fragile / unknown）与每项检查的 `detail`。
**两个都通过才进 Step 6。**其中任一项 fragile 时，按 `blockers` / `hint` 指的方向改，
而不是在同一方向上继续调参数。

### Step 6 经济直觉复核（**由你做的唯一主观判断**）
平台的数字只能说明"统计上像"，说不说明"为什么"。对每个因子给一档：

| 档位 | 标准 | 例子 |
|---|---|---|
| 强 | 已知异象 + 清晰的行为/结构原因 | 量价背离（放量滞涨=派发）、低波动异象、短期反转（流动性补偿） |
| 中 | 逻辑说得通但依据不够硬 | 多窗口动量加权、行业相对强弱 |
| 弱 | 写不出故事，只是数值凑巧 | 深层嵌套算子、窗口恰好为 17 天 |

**弱直觉的因子即使数字好看，也要显式标注"疑似数据挖掘"，并说明为什么仍值得（或不值得）继续。**

### Step 7 入库
```bash
lq factor corr "<新因子>" "<库内因子1>" "<库内因子2>"     # 查重
lq factor submit spec.yaml
```

spec.yaml：

```yaml
name: pv_diverge_20
expr: "-Ts_Corr(Ts_Return($close,1),Ts_Return($volume,1),20)"
agent: claude-code
rationale: "20 日量价背离：价格涨而量能不配合，视为派发信号，故取负"
claimed:
  ic_mean: 0.03          # 你自己报的数字会被平台重算 —— 报错方向就是 D 级
n_trials: 120            # 本轮（含历史）总试验次数，用于校正门槛
start: "2024-01-01"
```

`submit` 不信任任何自报数字：它重跑 G0–G3（含样本外），按与 `claimed` 的一致性给 A/B/C/D 级，
C/D 落 `factor_replication` 归档。**A/B 才是入库成功。**

## 4. 怎么读结果（判定线）

| 指标 | 有效线 | 说明 |
|---|---|---|
| `ic_mean` | \|IC\| ≥ 0.02 | A 股日频。> 0.05 极罕见 —— **先怀疑未来函数或数据错误** |
| `rank_ic_mean` | 同上 | 极端值多时以 RankIC 为准 |
| `icir`（= IR） | ≥ 0.3 可用，≥ 0.5 优秀 | 只有均值没有 ICIR 等于没看 |
| `t_stat_nw` | 过 `corrected_threshold` | 日度 IC 强自相关，朴素 t 会高估 3–5 倍，只看 NW |
| `ic_autocorr` | 越高越稳 | 近零/为负 = 每个行情片段各自碰运气 |
| 分层 `monotonicity` | \|ρ\| ≥ 0.8 优秀 | IC 高但不单调 = 极端股撑起来的 |
| 多空 `sharpe` | ≥ 0.5 可用，≥ 1.0 优秀 | 注意它是日频再平衡口径，别当实盘夏普 |
| `half_life` | — | 短半衰期 → 高频调仓，成本会吃掉收益 |
| `gross_exposure` | 越接近 0 越好 | 归因里行业暴露不接近 0 = 中性化没做干净，实际赌的是行业 |
| `verdict` | `robust` | L3 全通过才叫稳；`unknown` = 样本不足以判断，不是通过 |
| `rating` | strong / moderate / weak | 评级已含多重检验校正，直接用它，别自己重算 |

`eval`/`audit` 响应里的 `hints` 是平台给的下一步建议，**先读它再决定改哪里**。

## 5. 汇报格式（给用户）

```
⛏️ 因子挖掘结果

候选 <N> 条 → G0 通过 <a> → L1 快筛通过 <b> → L2/L3 通过 <c>

 #  表达式                              类别        RankIC   ICIR   评级      直觉
 1  -Ts_Corr(Ts_Return($close,1),...)   量价        0.031    0.62   strong    强
 2  Rank(Ts_Mean($close,5)/$close-1)    动量        0.028    0.41   moderate  中
 3  ...

入库：<name>（A 级）/ 未入库（原因码 + 提示）
L3：verdict=<robust|fragile>，失败项 <name>: <hint>
```

只说 JSON 里的数字，不要四舍五入到"看起来更好"的精度，也不要补任何没跑过的指标。

## 6. 常见错误（每一条都真实踩过）

1. **拿 0.05 当及格线** → 一无所获。A 股日频 0.02 就是有效线。
2. **只报 IC 不报 t** → 500 次搜索里 0.03 的 IC 遍地都是，没校正门槛等于没筛。
3. **在 test 段上挑因子** → `eval/audit/robust` 只用 train（+val 复核），
   别自己去碰 test 段；碰了就不叫样本外。
4. **参数调到最优再报** → 应报**扰动后的最差**。`robust` 的参数敏感性就是干这个的。
5. **把 `unknown` 当通过** → 样本不足时 L3 会说 `unknown`，那是"没判断"，不是"没问题"。
6. **用演示数据下结论** → `lq data demo` 的数据是编的，结论只能是编的。
7. **合成因子直接加权原始值** → 量纲不同（成交额 vs 收益率）会互相淹没，
   先把每个子因子截面归一（`Rank()` / `ZScore()`）再加权。
