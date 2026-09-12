# factor-mining 参考手册

> SKILL.md 的操作细则与语法参考。写表达式之前看这一份。

## 1. DSL 语法

因子是**字符串表达式**，不是 Python。可以静态遍历 AST，所以能禁止未来函数。

```
表达式   := 比较项 (('+' | '-') 比较项)*
比较项   := 乘积项 (('<' | '>') 乘积项)*        # 比较产生 0/1，供 If 用
乘积项   := 一元项 (('*' | '/') 一元项)*
一元项   := '-' 一元项 | 原子
原子     := $字段 | 数字 | 算子(参数, ...) | ( 表达式 )
```

示例：

```
Ts_Return($close, 20)
-Ts_Corr(Ts_Return($close,1), Ts_Return($volume,1), 20)
Rank(Ts_Mean($close,5) / $close - 1)
0.4*Rank(Ts_Return($close,20)) - 0.3*Rank(Ts_Std(Ts_Return($close,1),20))
If(Ts_Return($close,1) < 0, Ts_Return($close,1), 0)
```

**没有的语法**：赋值、lambda、条件表达式（`a if b else c`）、字符串、索引切片、任何 Python 内建。
需要条件就用 `If(cond, a, b)`。

**注释**：`#` 到行尾是注释，词法阶段直接丢弃（不进 AST、不影响 `canonical_id`）。
所以本手册里的候选模板带行尾说明也能**原样复制提交**，不用手删注释。

**方向约定**：统一写成「值越大越看好」。方向不影响 G0/G1 淘汰（快筛用 |IC|），
但它决定分层图的解读方向和合成时的权重符号 —— 写反了后面每一步都在解释一个负号。

## 2. 算子全表

### 时序算子（TS，22 个）— 在每只股票内沿时间计算

| 算子 | 签名 | 说明 |
|---|---|---|
| `Ts_Mean` | (x, n) | n 期均值 |
| `Ts_Std` | (x, n) | n 期标准差 |
| `Ts_Return` | (x, n) | n 期收益率 `x/x[-n]-1` |
| `Ts_Delay` | (x, n) | n 期前值 |
| `Ts_Delta` | (x, n) | n 期差分 `x-x[-n]` |
| `Ts_Sum` | (x, n) | n 期求和 |
| `Ts_Max` / `Ts_Min` | (x, n) | n 期最大 / 最小 |
| `Ts_ArgMax` / `Ts_ArgMin` | (x, n) | 最大 / 最小值的位置（0 = 窗内最老一根） |
| `Ts_Rank` | (x, n) | 末值在窗口内的分位（0~1） |
| `Ts_Quantile` | (x, n, q) | n 期分位数，q 默认 0.8 |
| `Ts_Corr` | (x, y, n) | n 期滚动相关 |
| `Ts_Cov` | (x, y, n) | n 期滚动协方差 |
| `Ts_Skew` | (x, n) | n 期偏度 |
| `Ts_EMA` | (x, n) | 指数移动平均（span=n） |
| `Ts_WMA` | (x, n) | 加权移动平均（权重 1..n） |
| `Ts_Slope` | (x, n) | 一元回归斜率 |
| `Ts_Rsquare` | (x, n) | 回归 R² |
| `Ts_Resi` | (x, n) | 回归残差标准差 |
| `Ts_Prod` | (x, n) | 连乘（走 log1p 域 —— **输入不能为负**） |

### 截面算子（CS，4 个）— 在同一交易日内横截面计算

| 算子 | 签名 | 说明 |
|---|---|---|
| `Rank` | (x) | 截面排名（等权分位，抗极值，最常用） |
| `ZScore` | (x) | 截面标准化 |
| `Demean` | (x) | 截面去均值 |
| `Scale` | (x) | 截面缩放（绝对值和 = 1） |

### 逐元素算子（EL，9 个）— 无窗口、无截面语义

| 算子 | 签名 | 说明 |
|---|---|---|
| `Abs` | (x) | 绝对值 |
| `Log` | (x) | log1p（与 Alpha158 的 `Log($v+1)` 同口径，可吃 0 和接近 0 的正值） |
| `Sqrt` | (x) | 平方根（负值 → null） |
| `Sign` | (x) | 符号 |
| `Power` | (x, p) | 幂 |
| `SignedPower` | (x, p) | sign(x)·\|x\|^p |
| `Greater` / `Less` | (a, b) | 逐元素取大 / 取小 |
| `If` | (cond, a, b) | `cond > 0` 时取 a，否则取 b |

未注册的算子名 → G0 直接 `STATIC_FAIL`。别猜算子名，用上面这张表。

## 3. 字段

`lq data fields` 打印实际字段与覆盖率。G0 的白名单口径：

**日线字段（`lq factor check` 用这一套）**

| 字段 | 含义 |
|---|---|
| `$open` `$high` `$low` `$close` `$pre_close` | 价格（不复权；复权用 `$adj_factor` 自行处理） |
| `$volume` | 成交量（股） |
| `$amount` | 成交额（元） |
| `$turnover_rate` | 换手率 |
| `$adj_factor` | 复权因子 |
| `$float_mv` | 流通市值 |

**`lq factor mine` 的面板里还会出现 `$cov_market_cap` / `$cov_industry_sw1` / `$cov_turnover_1m`** ——
那是中性化用的协变量，**不要拿来当因子**：
它们会在 G1 被中性化掉 → 直接判 `SIZE_PROXY`。

字段名拼错会在 G0 报错并给出近似候选（`$closs` → 提示 `$close`）。
覆盖率低的字段不要用 —— 缺一半的字段算出来的 IC 是覆盖率曲线的形状，不是信号。

## 4. 候选模板

`{w}` 是窗口占位，`{w1}`/`{w2}` 是长短两个窗口。
**不要穷举窗口**（5/10/20/30/60 全试一遍 = 数据窥探）；
按假设选 1–2 个，让 `lq factor robust` 去检验它对窗口敏不敏感。

### 动量（趋势延续）

```
Ts_Return($close,{w})
Ts_Mean($close,{w}) / $close - 1                          # 均线偏离（平滑动量）
Ts_Return(Ts_Delay($close,5),{w})                         # 跳过最近 5 日，避开短期反转
Ts_Return($close,{w1}) - Ts_Return($close,{w2})           # 相对强弱
Ts_Rank($close,{w})                                       # 收盘价在窗口内的分位（贴近高点 = 强）
Ts_Return($close,{w}) / Ts_Std(Ts_Return($close,1),{w})   # 风险调整动量
Ts_Sum(Ts_Return($close,1)*$volume,{w}) / Ts_Sum($volume,{w})   # 成交量加权日均收益
Ts_Slope(Log($close),{w})                                 # 趋势斜率
```

### 反转（均值回归）

```
-Ts_Return($close,{w})
-($close / Ts_Mean($close,{w}) - 1)                       # 偏离均线取反
-Ts_Rank($close,{w})
-($close / $open - 1)                                     # 日内反转
-($open / Ts_Delay($close,1) - 1)                          # 隔夜反转
-Ts_Delta($close,{w}) / Ts_Std(Ts_Return($close,1),{w})   # 波动调整后的反转
```

### 波动 / 风险

```
-Ts_Std(Ts_Return($close,1),{w})                                    # 低波动（经典异象）
-Ts_Mean($high / $low - 1,{w})                                      # 低振幅
-Ts_Std(Ts_Return($close,1),{w1}) / Ts_Std(Ts_Return($close,1),{w2})  # 波动变化
-Ts_Std(If(Ts_Return($close,1) < 0, Ts_Return($close,1), 0),{w})     # 下行波动
-Ts_Skew(Ts_Return($close,1),{w})                                    # 收益偏度
-Ts_Mean($high - $low,{w}) / $close                                  # ATR 比
```

### 量价 / 流动性

```
-Ts_Corr(Ts_Return($close,1), Ts_Return($volume,1),{w})              # 量价背离
-Ts_Corr(Log($close), Log($volume),{w})                              # 价格-成交量的趋势背离
-(Ts_Mean($volume,{w1}) / Ts_Mean($volume,{w2}) - 1)                 # 异常放量
-Ts_Mean($turnover_rate,{w})                                          # 低换手
-Ts_Mean($amount / $float_mv,{w})                                     # 成交额/流通市值
Ts_Mean(Sign(Ts_Return($close,1)) * $amount,{w}) / Ts_Mean($amount,{w})  # 资金流强度
Ts_Cov(Ts_Rank($close,{w}), Ts_Rank($volume,{w}),{w})                 # 量价同向程度
```

### 复合（先各自截面归一，再加权）

```
Rank(Ts_Return($close,{w1})) - Rank(Ts_Std(Ts_Return($close,1),{w1}))
Rank(-Ts_Return($close,{w1})) - Rank(Ts_Corr(Ts_Return($close,1),Ts_Return($volume,1),{w2}))
0.4*Rank(Ts_Return($close,{w1})) - 0.3*Rank(Ts_Std(Ts_Return($close,1),{w1})) - 0.3*Rank(Ts_Corr(Ts_Return($close,1),Ts_Return($volume,1),{w2}))
```

复合的坑：**先归一再加权**。直接相加会让量纲大的因子（成交额）淹没量纲小的（收益率），
你得到的是一个改名叫"复合"的单因子。

## 5. 原因码 → 下一步怎么改

`lq factor check` / `mine` / `submit` 的淘汰都带 `reason_code` + `hint`。

| 原因码 | 含义 | 下一轮方向 |
|---|---|---|
| `STATIC_FAIL` | 解析失败 / 字段不在白名单 / 未注册算子 / 引用未来数据 | 照着 `hint` 改字段名或算子名；未来函数无法绕过，换设计 |
| `COMPUTE_FAIL` | 计算过程报错或 IC 序列为空 | 常见：全 null 字段、开方负数、连乘吃负数、窗口超过样本长度 |
| `LOW_IC` | 中性化后 \|IC\| 不足 | **换字段族**（价格 → 量能 → 波动），或加截面变换（`Rank`/`ZScore`）；不要只调窗口 |
| `REDUNDANT` | 与幸存者 / 库内因子 \|ρ\| ≥ 0.7 | 换持有期、换字段族，或做成正交化后的残差因子 |
| `SIZE_PROXY` | 中性化后 IC 衰减过大 | 这个信号本质是市值/风格暴露，不是选股 alpha —— 直接放弃，别硬救 |
| `LOW_TSTAT` | 未过校正后 t 门槛 | 要么真信号弱，要么你搜得太多了 —— 看 `n_trials` 是多少 |
| `OOS_FAIL` | val 段复核失败 | 训练段过拟合。缩短搜索空间、提高单条候选的假设强度 |

## 6. 命令输出字段速查

```
lq factor check   → passed, stage, reason_code, hint
lq factor eval    → ic_mean, rank_ic_mean, t_stat, n_days, ic_raw_mean,
                    neutralized, n_trials, corrected_threshold, quota_remaining, hints
lq factor audit   → rating{rating,score,ic_mean,icir,t_stat_nw,t_threshold,significant,
                          monotonicity,ls_sharpe,reasons,blockers}
                    ic{mean,std,ir,t_stat,t_stat_nw,positive_rate,ic_gt_002_rate,ic_autocorr,n_days}
                    rank_ic{...同上...}
                    quantile{monotonicity,top_bottom_spread,long_short,groups}
                    decay{half_life,suggested_rebalance,profile}
                    attribution{by,gross_exposure,industry_exposure}   # 无行业列时为 null
                    oos_decay{icir_is,icir_oos,decay,threshold,passed}
                    n_days{train_days,val_days,test_days}, covariates, hints
lq factor robust  → verdict(robust|fragile|unknown), n_passed, n_judged,
                    checks[{name,status(passed|failed|skipped),value,threshold,hint,detail}]
                    # 五类检查：param_sensitivity / time_stability /
                    #          start_date_sensitivity / best_month_removal / oos_decay
lq factor mine    → run_id, agent, generator, n_evaluated, n_static_fail, n_low_ic,
                    n_redundant, n_size_proxy, n_survivors, survivors[]
lq factor submit  → ok, grade(A|B|C|D), name, recomputed{ic_train,ic_val,t_val,threshold},
                    reason_code, hint, attribution
```

`checks[].status = skipped` 的含义是**样本不足、没判**（不是通过）：
比如样本只有 4 个月却要剔除最佳 5 个月、或没传 `expr` 做参数敏感性。

## 7. 数据切分（谁看哪一段）

| 段 | 占比 | 用途 |
|---|---|---|
| train | 70% | 搜索、快筛、参数敏感性 —— 你唯一能反复看的一段 |
| val | 15% | 只在 L2/L3 复核一次（`oos_decay`、`submit` 重验） |
| test | 15% | **不碰**。它是入库后的最终裁判，碰了就不再是样本外 |

`eval / audit / robust / submit` 都自动按这个切分跑，你不需要（也不应该）自己切。
