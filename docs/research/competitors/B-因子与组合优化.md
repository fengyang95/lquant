# B — 因子研究与组合优化：开源项目调研报告

> 调研对象：开源**因子研究 / 因子挖掘 / 组合优化 / 风险模型**项目。
> 目标平台：**lquant**（Python Polars/DuckDB + Rust + Next.js 的 A 股日频选股平台）。
> 编写日期：2026-10-08。
> 说明：本报告**不重复** `docs/research/qlib/`、`docs/research/alphapurify/` 已覆盖的内容（qlib 本体、Alpha158/360、AlphaPurify 的 XVal），只补充它们没覆盖的部分。
> 方法与纪律：所有函数签名/默认值均来自**实际抓取的源码或官方文档**；未能抓取到源码的接口一律标注「**未核实**」，不做臆造。每条结论附可点击证据链接。

---

## 0. 先立基线：lquant 已经有什么

差异分析必须建立在准确基线上。经代码核对，lquant **已有**以下能力（不是任务书里列的那一小部分）：

| 层 | 模块 | 已实现 |
|---|---|---|
| 预处理 | `src/lquant/factors/preprocess/` | 去极值（`winsorize.py`）、标准化（`standardize.py`）、中性化（`neutralize.py`）、**三种正交化**（`orthogonalize.py`：`symmetric` 施密特对称正交 / `gram_schmidt` 逐步回归 / `pca`）、幂变换（`power.py`）、滚动（`rolling.py`）、回归底座（`_regress.py`）、pipeline |
| 评价 | `src/lquant/factors/evaluate/` | IC、分组 IC、分层、衰减、归因、事件研究、滚动、成本、稳健性、风格相关、容量、样本、报表 |
| 挖掘 | `src/lquant/factors/mining/` | `gp.py`（粗糙 GP）、`random_gen.py`（随机基线）、`fitness.py`（**含多重检验校正**）、`gates.py`、`llm.py`、`runner.py`、`submit.py` |
| 因子库 | `src/lquant/factors/qlib_alpha.py` | Alpha158 原生实现 |
| 组合 | `src/lquant/portfolio/weighting.py` | `equal` / `score` / `market_cap`（含 `sqrt`）/ `inverse_vol` / `risk_parity`(SLSQP ERC) / `min_variance`(SLSQP) / `hrp`(single linkage + 递归二分) / `enhanced_indexing` |
| 组合 | `src/lquant/portfolio/optimizer.py` | TE 约束下最大化预期超额的增强指数优化（SLSQP） |
| 组合 | `src/lquant/portfolio/riskmodel.py` | 样本 / Ledoit-Wolf 收缩 / OAS 收缩 / 结构化 PCA / POET 协方差；条件数、PSD 修复 |
| 组合 | `src/lquant/portfolio/dedup.py` | 相关性贪心去重（`threshold=0.9`）+ 簇标签 + 簇摘要 |

**因此本报告的"差异"部分一律按上述基线计算**：凡 qlib/AlphaPurify 调研已覆盖的（离线数据层、Alpha158/360、XVal），本报告不再列。真正有价值的借鉴集中在三块：

1. **因子评价的"口径纯度"**（alphalens 的 group-relative / zero_aware / max_loss 诊断）；
2. **候选表达式的前置校验与适应度设计**（AlphaGen 的 `validate_parameters` + alpha pool 互相关奖励；gplearn 的 parsimony + OOB bagging）；
3. **组合层的风险度量广度与"防过拟合的模型选择"**（Riskfolio 的 26 种凸风险度量、skfolio 的 Purged CV + WalkForward + 特征因子风险模型）。

---

## A. 因子评价：alphalens / alphalens-reloaded

### A.1 定位 + 活跃度 + 技术栈

- **alphalens**（`quantopian/alphalens`）：因子评价的**行业事实标准**，被 pyfolio/zipline 时代的研究流程广泛引用。**已归档、实质停更**（Quantopian 2018 年关停，2020 年仓库归档）。纯 pandas/numpy/scipy/statsmodels/matplotlib，依赖重量中等（`statsmodels` 用于 `factor_alpha_beta` 的 OLS）。
  - 源码：<https://github.com/quantopian/alphalens>
  - API 参考（第三方整理）：<https://deepwiki.com/quantopian/alphalens/5-api-reference>
- **alphalens-reloaded**（`stefan-jansen/alphalens-reloaded`）：社区维护 fork，修 alphalens 在新 pandas 上的 API 破坏。
  - 仓库与发布：<https://github.com/stefan-jansen/alphalens-reloaded> · <https://github.com/stefan-jansen/alphalens-reloaded/releases>
  - 中文安装/分支选择说明：<https://easyclaw.ijinshan.com/skills/alphalens/install.html>
  - 社区建议更新到 reloaded：<https://mf.bigquant.com/wiki/doc/NjWvZDOlK3>

### A.2 可借鉴的算法口径与接口设计（重点，均为源码核对）

#### (1) 入口函数与默认值 —— 一整套"数据契约"

```python
# alphalens/utils.py::get_clean_factor_and_forward_returns
def get_clean_factor_and_forward_returns(
        factor, prices, groupby=None, binning_by_group=False,
        quantiles=5, bins=None, periods=(1, 5, 10),
        filter_zscore=20, groupby_labels=None, max_loss=0.35,
        zero_aware=False, cumulative_returns=True)
```

设计要点（**lquant 最该借的是这里的"契约化"**）：

- **长表 MultiIndex `(date, asset)`**：factor 是 `pd.Series`，prices 是宽表 `DataFrame(index=日期, columns=资产)`。输出是带 `factor / group / factor_quantile` 三列的长表。这套契约把"评价"变成纯函数，可复现性极强。
- **`max_loss=0.35`**：允许最多丢 35% 的样本；超过就抛 `MaxLossExceededError`，并在 `get_clean_factor` 里把丢样拆成三段打印：
  ```python
  tot_loss  = (initial_amount - binning_amount) / initial_amount
  fwdret_loss = (initial_amount - fwdret_amount) / initial_amount
  bin_loss  = tot_loss - fwdret_loss
  # "Dropped X% entries from factor data: Y% in forward returns computation and Z% in binning phase"
  ```
  这是**极好的工程口径**：lquant 的因子评价如果不报告"因前视对齐/分箱失败丢了多少样本"，很容易在部分覆盖的因子上得出虚假高分。**建议原样移植这个三分账**。
- **`no_raise = False if max_loss == 0 else True`**：`max_loss=0` 表示"任何丢样都直接报错"，用于对拍/CI。

#### (2) 分箱：`quantize_factor(factor_data, quantiles=5, bins=None, by_group=False, no_raise=False, zero_aware=False)`

- `quantiles` 用 `pd.qcut(labels=False) + 1`（**等频**，1..N）；`bins` 用 `pd.cut(labels=False) + 1`（**等宽/自定义边界**）。二者互斥。
- **`zero_aware`**：`quantiles//2` 分别对 `x>=0` / `x<0` 分箱，再 concat 排序。**这是给"中心化、零点是多空分界"的因子设计的**——A 股很多反转/情绪因子正是这种形态，等频分箱会把零点随机切进某一层。lquant 的 `evaluate/quantile.py` 是否支持零感知分箱需核对；**这是低成本的实质增益**。
- **`by_group`（`binning_by_group`）**：按行业分组后**组内**分箱。文档明确写了动机：
  > "This is useful when the factor values range vary considerably across groups … You should probably enable this if the factor is intended to be analyzed for a group neutral portfolio."
- **非唯一分箱边界错误**有专门的装饰器 `non_unique_bin_edges_error`，把 pandas 的 `'Bin edges must be unique'` 翻译成 4 条可操作建议（减少分位数 / 自定义边界 / 改用 bins / 离散值用 bins）。**lquant 可借这条错误信息模板**——离散/稀疏因子（如"是否 ST"）在等频分箱上必然踩这个坑。

#### (3) 前视泄漏：源码里**明写**的坑

```python
# compute_forward_returns(... filter_zscore=None ...)
if filter_zscore is not None:
    mask = abs(forward_returns - forward_returns.mean()) > (filter_zscore * forward_returns.std())
    forward_returns[mask] = np.nan
```
docstring 原话：

> `filter_zscore` : int or float, optional — Sets forward returns greater than X standard deviations from the the mean to nan. **Caution: this outlier filtering incorporates lookahead bias.**

而 `get_clean_factor_and_forward_returns` 的**默认**是 `filter_zscore=20`——**默认开启一个官方承认带前视偏差的滤波**。这是 alphalens 最著名的方法论争议点。**lquant 必须显式默认关闭**（若尚未如此），并在报告里注明。

对齐本身是干净的：`prices.pct_change(period).shift(-period).reindex(factor_dateindex)`，即"因子日 → 未来 period 日收益"。但价格从哪一天开始算（T 日收盘还是 T+1 日开盘）由**调用方传入的 `prices` 决定**，文档明确要求调用方保证 `prices` 是"因子算完之后下一个可得的价格"。**A 股 T+1 场景下这条必须写进 lquant 的接口文档**。

#### (4) IC：`factor_information_coefficient(factor_data, group_adjust=False, by_group=False)`

- 核心是一行：`group[forward_ret_cols].apply(lambda x: stats.spearmanr(x, f)[0])`，**按 `date` 分组做截面 Spearman**，多空窗口一起算。
- **`group_adjust=True`**：先 `demean_forward_returns`（减去同组均值）再算 IC → 得到"行业相对 IC"。文档解释：
  > "group-wise normalization incorporates the assumption of a group neutral portfolio constraint and thus allows the factor to be evaluated across groups."
- **`by_group=True`**：每个行业各算一条 IC 时序。
- `mean_information_coefficient(..., by_time=None)`：`:any`/`M`/`W` 等 pandas 频率做 IC 的时间聚合；`by_group` 时按组聚合。

> **对 lquant 的价值**：lquant 有 `evaluate/group_ic.py` 和 `evaluate/neutral_views.py`，但 alphalens 的 **`group_adjust` 语义（收益去组均值 vs 因子去组均值）** 是明确且被广泛引用的口径。建议把"行业相对 IC（收益减行业均值）"作为独立口径固化并写进文档，而不是隐含在某一个中性化开关里。

#### (5) 因子组合收益与多空权重：`factor_weights(factor_data, demeaned=True, group_adjust=False, equal_weight=False)`

- **gross leverage 恒为 1**：`return group / group.abs().sum()`。
- `demeaned=True`（默认）→ `group - group.mean()` → **美元中性多空**。
- `group_adjust=True` → 先组内 demean 得到组权重，再对组权重整体 `to_weights(..., demeaned=False, equal_weight=False)` → **组中性**。
- `equal_weight=True` → 用中位数切正负，正负各归一到 1 / 各侧个数。

#### (6) 分层收益与"多空价差 t 统计"：`mean_return_by_quantile` + `compute_mean_returns_spread`

```python
def mean_return_by_quantile(factor_data, by_date=False, by_group=False,
                            demeaned=True, group_adjust=False):
    ...
    group_stats = factor_data.groupby(grouper)[fwd_cols].agg(['mean','std','count'])
    std_error_ret = group_stats.T.xs('std', level=1).T / np.sqrt(group_stats.T.xs('count', level=1).T)
    return mean_ret, std_error_ret

def compute_mean_returns_spread(mean_returns, upper_quant, lower_quant, std_err=None):
    mean_return_difference = mean_returns.xs(upper_quant, level='factor_quantile') \
                           - mean_returns.xs(lower_quant, level='factor_quantile')
    # 独立近似：joint_std_err = sqrt(std1**2 + std2**2)
    joint_std_err = np.sqrt(std1**2 + std2**2)
```

**注意口径**：价差的标准误用 `sqrt(std_top² + std_bottom²)`，即**假设两分层独立**。它们其实是同一批收益的不同分位，近似共线，这是个**已知的保守/次优口径**。lquant 若有分层价差检验，建议改用配对差序列的标准误（更严谨），并把这个差异写进对拍说明。

#### (7) 换手与秩自相关（因子稳定性）

```python
def quantile_turnover(quantile_factor, quantile, period=1):   # 该层"新进名字"占比
def factor_rank_autocorrelation(factor_data, period=1):       # 截面 rank 的时序自相关
```
`factor_rank_autocorrelation` 的做法是先 `groupby(date)['factor'].rank()`，pivot 成 `date × asset`，再 `corrwith(asset_factor_rank.shift(period), axis=1)`。docstring 解释动机：

> "We must compare period to period factor ranks rather than factor values to account for systematic shifts in the factor values of all names or names within a group."

**这是因子换手的纯信号视角指标**，与回测换手互补。lquant 有 `evaluate/decay.py`，但**秩自相关**这个具体口径值得单独加。

#### (8) 事件研究底座：`common_start_returns` / `average_cumulative_return_by_quantile`

```python
def average_cumulative_return_by_quantile(factor_data, returns,
        periods_before=10, periods_after=15, demeaned=True,
        group_adjust=False, by_group=False)
```
把每期每个分层的收益序列**对齐到共同相对索引 `-periods_before … +periods_after`** 再求均值/标准差。`create_pyfolio_input` 则把 `(returns, positions, benchmark)` 按 pyfolio 契约产出，`positions(weights, period, freq=None)` 自己处理"权重在持有期内不变、每日重算"的持仓展开。

#### (9) 回归式 alpha/beta：`factor_alpha_beta`

```python
x = add_constant(universe_ret[period].values)      # 基准 = 全市场等权收益
reg_fit = OLS(y, x).fit()                          # y = 因子组合收益
alpha_beta.loc['Ann. alpha', period] = (1 + alpha) ** freq_adjust - 1
# freq_adjust = pd.Timedelta('252Days') / pd.Timedelta(period)
```
**年化 alpha 的复利化处理**（`(1+α)^(252/period) - 1`）比简单乘更容易在长窗口上爆表，A 股 250 交易日口径下建议显式改用 `252 → 242` 并注明。lquant 有 `evaluate/attribution.py`，口径对齐需逐项核。

### A.3 与 lquant 已实现部分的差异

| 维度 | alphalens | lquant 现状 | 结论 |
|---|---|---|---|
| 数据契约 | `(date, asset)` 长表 + 宽表价格，纯函数 | Polars 宽表为主 | alphalens 契约更利于"因子即序列"的复用；lquant 不必改架构，但**可提供 `to_alphalens` 导出**做交叉验证 |
| 丢样诊断 | **三分账打印 + `max_loss` 硬阈值** | 未见等价机制（未核实全量代码） | **lquant 缺**，低成本高价值 |
| 零感知分箱 | `zero_aware=True` | 未核实 | **大概率缺**，低成本 |
| 组内分箱 | `binning_by_group` | 未核实 | 中价值 |
| 组相对 IC | `group_adjust` / `by_group` | 有 `group_ic.py`，口径待核 | 口径对齐 |
| 前视滤波 | **默认 `filter_zscore=20`（自带前视）** | 应默认关闭 | **alphalens 更差**，lquant 不要照抄默认 |
| 分层价差标准误 | `sqrt(std1²+std2²)`（独立性近似） | 待核 | **lquant 可做得更好**（配对差） |
| 秩自相关换手 | `factor_rank_autocorrelation` | 有 decay，无秩自相关 | 可补 |
| 多空组合构造 | gross leverage=1 + 组中性/等权三开关 | 有 weighting，但非因子评价语义 | 可借语义 |
| 性能 | pandas groupby.apply，慢 | Polars/Rust | **lquant 明显更好** |

### A.4 已知坑与失败教训（有证据）

1. **仓库归档**：alphalens 随 Quantopian 关停，**不再维护**；应直接用 `alphalens-reloaded`（[releases](https://github.com/stefan-jansen/alphalens-reloaded/releases)）。
2. **pandas API 破坏**：源码中仍留 `ix.labels`、`pd.Series.as_matrix()` 时代的写法（见 `backshift_returns_series` 用 `ix.labels`，`utils.py`；`MultiIndex(levels=..., labels=..., sortorder=...)`），这些在新 pandas 上会直接 `AttributeError`。这是 fork 存在的原因。
3. **默认前视偏差**：`filter_zscore=20` 的 docstring 自认 "incorporates lookahead bias"。**这是本项目最需要引以为戒的一条**。
4. **分层价差的独立性近似**：`sqrt(std1²+std2²)` 高估标准误、低估显著性（保守），但被大量论文照抄。
5. **`groupby(level=...)` / `.append` 等 deprecation**：reloaded 的主要修复方向。
6. **纯 pandas 长表性能**：`groupby(date).apply(...)` 在 A 股全市场 × 长历史下会成为瓶颈（与 lquant 的 Polars 相比是数量级差距）。

### A.5 移植成本 / 价值 / 是否值得做

| 借鉴项 | 移植成本 | 对 A 股日频选股价值 | 值得做？ |
|---|---|---|---|
| 丢样三分账 + `max_loss` 硬阈值 | **低** | 高（防止局部覆盖因子虚高） | ✅ 强烈建议 |
| `zero_aware` 零感知分箱 | **低** | 高（反转/情绪因子正确分层） | ✅ 建议 |
| `binning_by_group` 组内分箱 | 低 | 中 | ✅ 建议 |
| `group_adjust` 组相对 IC 口径显式化 | 低 | 高（A 股行业效应极强） | ✅ 建议 |
| `factor_rank_autocorrelation` 秩自相关 | 低 | 中高（换手预估） | ✅ 建议 |
| 非唯一分箱边界错误模板 | 低 | 中（稀疏因子可用性） | ✅ 可选 |
| `create_pyfolio_input` / pyfolio 契约 | 中 | 低（lquant 自有报表） | ⚠️ 不必 |
| alphalens 整体依赖引入 | 高 | 负（引入 pandas 重依赖、前视默认） | ❌ 只借口径不引库 |

---

## B. Alpha101 / Alpha191 的开源 Python 实现

### B.1 项目清单、活跃度、技术栈

| 项目 | 说明 | 技术栈 | 证据 |
|---|---|---|---|
| `yli188/WorldQuant_alpha101_code` | 流传最广的 Alpha101 pandas 实现，分 `101Alpha_code_1.py` / `_2.py` | pandas + scipy.rankdata，**逐因子一个方法**，无工程抽象 | <https://github.com/yli188/WorldQuant_alpha101_code> |
| GitHub `alpha101` topic | 同类实现的集合（含多份 fork） | 多数为纯 pandas | <https://github.com/topics/alpha101> |
| GTJA191 公式表（社区整理） | 国泰君安 191 的**完整统一算子 + 191 条公式文本** | 文档 | <https://github.com/laozdao/dao-quant-research/blob/main/articles/M06-factor-validation/M06-06-gtja191-formula-reference.md> |
| GTJA191 因子分析文 | 191 的 IC/IR 实证与核心因子结论 | 文档 | <https://github.com/laozdao/dao-quant-research/blob/e1e0.../M06-05-gtja191-factor-analysis.md> |
| `STHSF/AlphaFactorLibrary` | Alpha101+191+技术因子打包 | 未核实 | 搜索指向 <https://skills.rest/skill/alpha-factor-library> |
| 一篇把 191/101 用于 A 股的论文 | 基础因子来自 GTJA191/Alpha101 | 论文 | <http://arxiv.org/pdf/2309.11979> |

> **活跃度说明**：`yli188/WorldQuant_alpha101_code` 属于"一次性发布的参考实现"，长期无实质维护；这类仓库的价值在于**因子公式转录**，不在于工程质量。精确 star 数本轮**未核实**。

### B.2 可借鉴的算法口径与接口设计（源码逐行核对）

#### (1) 算子辅助函数的**默认值**（yli188 参考实现）

```python
ts_sum(df, window=10)          # df.rolling(window).sum()
sma(df, window=10)             # df.rolling(window).mean()
stddev(df, window=10)          # df.rolling(window).std()
correlation(x, y, window=10)   # x.rolling(window).corr(y)
covariance(x, y, window=10)    # x.rolling(window).cov(y)
ts_rank(df, window=10)         # df.rolling(window).apply(rolling_rank)
                               #   rolling_rank(na) = rankdata(na)[-1]   ← 最后一个值的秩
product(df, window=10)         # df.rolling(window).apply(np.prod)
ts_min / ts_max(df, window=10) # rolling.min() / rolling.max()
delta(df, period=1)            # df.diff(period)
delay(df, period=1)            # df.shift(period)
rank(df)                       # df.rank(pct=True)          ← 默认 axis=0，见下方"坑"
scale(df, k=1)                 # df.mul(k).div(np.abs(df).sum())
ts_argmax(df, window=10)       # df.rolling(window).apply(np.argmax) + 1     ← +1：从 1 起算
ts_argmin(df, window=10)       # df.rolling(window).apply(np.argmin) + 1
decay_linear(df, period=10)    # 见下
```

**`decay_linear` 的确切权重**（这是最常被写错的一个）：

```python
divisor = period * (period + 1) / 2
y = (np.arange(period) + 1) * 1.0 / divisor        # 权重 1,2,...,period 归一到和=1
for row in range(period - 1, df.shape[0]):
    x = na_series[row - period + 1: row + 1, :]
    na_lwma[row, :] = np.dot(x.T, y)               # 越近的样本权重越大
```
即 `decay_linear(x, d) = Σ_{i=1..d} i·x_{t-d+i} / (d(d+1)/2)`。参考实现还先做 `ffill → bfill → fillna(0)`，**这本身就是一处前视**（`bfill` 用未来值补过去），虽然作者注释说"backtest engine should assure to be snooping bias free"。

**AlphaGen（更现代的向量化实现）给出的等价口径**（已抓源码，见 C 节）：
- `WMA`: `weights = arange(n); weights /= weights.sum()` —— 与 `decay_linear` 同构；
- `EMA`: `alpha = 1 - 2/(1+n); weights = alpha ** arange(n,0,-1)`；
- **`Rank`（时间序列秩）**：`(right + left + (right > left)) / (2n)`，其中 `left = count(last < x)`、`right = count(last <= x)` —— 这是**处理并列值的标准秩公式**，比 `rankdata(na)[-1]` 更严谨。**lquant 建议采用 AlphaGen 的并列处理口径**。

#### (2) Alpha191 独有的算子（lquant 的 DSL 需对照）

GTJA191 的统一算子定义（社区整理，用于原研报口径）：

| 算子 | 语义 | 备注 |
|---|---|---|
| `DELTA(A,n)` | A(t) − A(t−n) | 通用 DSL 多有 |
| `DELAY(A,n)` | A(t−n) | 通用 |
| `SUM/STD/CORR/MAX/MIN(A,n)` | 滚动聚合 | 通用 |
| `RANK(A)` | **当日截面升序排名 0~1** | lquant 有 |
| `TSRANK(A,n)` | 当前值在过去 n 日的时间序列排名 | 通用 |
| **`SMA(A,n,m)`** | n 日移动平均，**m 为权重**（中国券商口径的"平滑因子"） | **lquant 需确认是否支持带 m 的 SMA** |
| **`COUNT(cond,n)`** | 过去 n 日满足条件的天数 | Alpha150–169 大量使用；**lquant 需确认** |
| **`REGBETA(A,B,n)`** | A 对 B 的 n 日滚动回归系数 | Alpha142/144/146/148；**表达式内嵌滚动回归，是 DSL 的结构性挑战** |
| **`REGRESI(A,B,n)`** | A 对 B 回归的残差 | Alpha143/145/147/149；同上 |

**191 的结构分布**（可直接指导实现优先级）：

- Alpha1–23：CORR/TSRANK/条件判断，形态各异（`RANK(MAX(DELTA(VOLUME,1),0))*-1` 这类从 Alpha33 开始高度模板化）。
- Alpha24–71：**大量同构变体**（`RANK(DELTA((CLOSE-OPEN)/(HIGH-LOW), k))*-1` 的 k=1..5；`RANK(DELTA(CLOSE,k))*RANK(VOLUME)*-1` 的 k=1..5；`RANK(CLOSE-DELAY(CLOSE,i))*RANK(CLOSE-DELAY(CLOSE,j))*-1` 的组合），**共线性极高**。
- Alpha104–191：几乎全部是 `CORR(RANK(MAX/MIN(...)), RANK(VOLUME), 6)` 与 `TSRANK(...)` 的模板复制。
- **只有 142–149 用了 `REGBETA`/`REGRESI`**，151–169 用了 `COUNT`。

社区给出的实证结论：**核心因子是 Alpha1/2/5/10/72/171/191（IC>0.08, IR>1.5）**；回测参数为中证 500 成分股、日频调仓、千 1 手续费、持仓 50–100 只（[证据](https://github.com/laozdao/dao-quant-research/blob/main/articles/M06-factor-validation/M06-06-gtja191-formula-reference.md)）。

#### (3) 可借鉴的**接口设计**

- **"公式即字符串"**：这类库把每条 alpha 写成带公式注释的独立方法，公式原文放在注释里。**lquant 的 DSL 若能把"研报公式原文"作为一等公民注释/元数据保留**，可极大降低口径争议。
- **`Alphas(df_data)` 的列名契约**：参考实现直接吃 `S_DQ_OPEN/S_DQ_HIGH/S_DQ_LOW/S_DQ_CLOSE/S_DQ_VOLUME/S_DQ_PCTCHANGE/S_DQ_AMOUNT`（Wind 风格），并在构造时算 `vwap = amount*1000/(volume*100+1)`。
  - **注意那两处隐式单位换算**（`volume*100`、`amount*1000`）和 **`+1` 防除零**——这正是"口径不明"的典型来源，**lquant 应显式命名单位并在数据字典里固化，绝不照抄隐式换算**。

### B.3 与 lquant 已实现部分的差异

lquant 已有 DSL（TS/CS 算子）+ 原生 Alpha158。差异集中在：

| 需要的能力 | Alpha101 | Alpha191 | lquant 现状 | 差距判定 |
|---|---|---|---|---|
| `decay_linear(d)` 精确权重 | ✅ | 用 SMA/MA | 未核实 DSL 是否有 | **需确认**；若缺，低中成本 |
| `ts_argmax/argmin` 位置算子 | ✅（+1 从 1 起） | ✅ | 未核实 | 需确认 |
| `product`（滚动乘积） | ✅ | — | 未核实 | 需确认 |
| `scale(x, a=1)` = `x·a/Σ|x|` | ✅ | — | 未核实 | 需确认 |
| `signedpower(x, a)` | ✅（`**`） | — | 有 `power.py` | 大概率有 |
| `SMA(A,n,m)` 带权重 m | — | ✅ | **很可能缺** | 低成本 |
| `COUNT(cond, n)` | — | ✅（20 条） | **很可能缺** | 低成本，**优先** |
| `REGBETA/REGRESI` 表达式内滚动回归 | — | ✅（8 条） | 有 `_regress.py` 底座，但**是否能在 DSL 表达式内调用待核** | **结构性**；8 条因子，价值中等 |
| `IndNeutralize(x, IndClass.Sector/Industry/Subindustry)` | ✅（约 20 条用到） | — | 有中性化，但**是预处理阶段而非表达式算子** | 见下 |
| `adv5/10/15/20/30/40/50/60/81/120/150/180` | ✅ | — | 未核实是否有 adv 一族 | 低成本，**优先** |

**关于 `IndNeutralize` 的结构性差异（重要）**：

- Alpha101 里 `IndNeutralize(x, IndClass.sector)` 出现在**表达式内部**（Alpha48/58/59/63/67/69/70/76/79/80/82/87/89/90/91/93/97/100），即"先行业中性化，再参与后续 TS/CS 运算"。
- lquant 的中性化在 `preprocess/neutralize.py`，属于**预处理阶段**，是流水线的一步。
- **这不是谁对谁错**：表达式内中性化表达能力更强，但代价是每次求值都要行业数据、且掩盖了"因子是否本身就去过行业"。lquant 的预处理式设计**在工程上更清晰**；如需覆盖那 ~20 条 alpha，建议提供显式的 `ind_neutralize(expr, industry)` 算子并**强制标注**。

**A 股适配性问题（Alpha101 有相当数量不可直接落地）**：

| 依赖 | 涉及范围 | A 股可得性 |
|---|---|---|
| `cap`（市值） | Alpha56（参考实现直接注释掉） | 日频可得，但**市值因子与规模风险高度耦合** |
| `vwap` | 约 30+ 条 | 日频可用"成交额/成交量"近似；**不等于真实分钟 VWAP** |
| `IndClass.sector/industry/subindustry` | 约 20 条 | 申万一级/二级可得 |
| `adv{d}`（d=5..180） | 大量 | 由成交量可得，但**180 日 adv 在 A 股次新股上覆盖不足** |
| 分钟级/日内 | 部分隐含 | 日频平台不可用 |
| 做空 | 全部多空语义 | A 股个股做空受限，**只能多空合成或纯多头 TopN** |

**A 股特有约束（这些在 Alpha101/191 原研报里都不存在）**：T+1、涨跌停（因子在涨跌停日的可交易性）、停牌、ST、次新股。参考实现**完全没有**这些过滤——lquant 的 `factors/universe.py` 若已覆盖，则优于这些库。

### B.4 已知坑与失败教训（有具体证据）

1. **`rank()` 的 axis 默认值 bug（最严重）**
   参考实现写：
   ```python
   def rank(df):
       #return df.rank(axis=1, pct=True)
       return df.rank(pct=True)
   ```
   被注释掉的才是**截面（axis=1）**排名，启用的是 pandas 默认 `axis=0`——**沿时间轴排名**。对 `DataFrame(index=日期, columns=股票)`，这意味着 `rank()` 实际在算"个股在历史上的分位"，而不是"当日横截面分位"。**Alpha101 中绝大多数因子都用到 `rank`，这一处默认值污染面极广**。已被社区指出并重写（[StackOverflow: Improve the implementation of worldquant 101 alpha factors using numpy](https://stackoverflow.com/questions/73694527/improve-the-implementation-of-worldquant-101-alpha-factors-using-numpy/74607611)）。
2. **`scale()` 同样有 axis 问题**：`df.mul(k).div(np.abs(df).sum())`——`DataFrame.sum()` 默认 `axis=0`，得到的是**逐列（逐资产）的时间序列和**，而不是当日截面 `Σ|x|`。WQ 原文的 `scale` 是**截面**缩放。
3. **公式与研报不一致（转录错误）**
   - Alpha57 的实现与论文定义不符，社区专门开帖讨论（[Quantra Community: Alpha 57 as implemented does not match the definition in the paper](https://quantra.quantinsti.com/community/t/alpha-57-as-implemented-does-not-match-the-definition-in-the-paper/26690/6)）。
   - 源码中**作者自己标注**：Alpha27 的注释是 `## Some Error, still fixing!!`。
   - Alpha38 的注释写 `rank(Ts_Rank(close, 10))`，实现却是 `rank(ts_rank(self.open, 10))` —— **`close` 写成了 `open`**。
4. **整批因子被跳过**：Alpha48/58/59/63/67/69/70/76/79/80/82/87/89/90/91/93/97/100 因需要 `IndNeutralize`，Alpha56 因需要 `cap`，**均未实现**（源码中只剩注释）。
5. **`df.as_matrix()`**：`decay_linear` 里用的是 pandas 0.25 就废弃、1.0 移除的 API，**现代 pandas 直接报错**。
6. **`bfill` 引入前视**：`decay_linear` 的 `fillna(method='bfill')` 用未来值填过去。
7. **逐因子 `.rolling().apply(np.argmax)`**：`ts_rank`/`product`/`ts_argmax` 都走 Python 回调，**在 A 股全市场 × 数十年数据上慢到不可用**（数量级问题）。
8. **`+1` 的歧义**：`ts_argmax` 返回 `argmax+1`（1 起算，最旧=1），而 WQ 原文的 `Ts_ArgMax` 语义在不同文档里有 0 起/1 起之争，**必须写明**。
9. **191 的共线性陷阱**：如 B.2(3) 所示，191 条里绝大多数是模板复制的同构变体。**直接全量入库会制造严重的多重共线性**，必须配合 lquant 的 `orthogonalize.py` / `dedup.py` 做筛选——**这一点是 lquant 的既有优势，不是劣势**。

### B.5 移植成本 / 价值 / 是否值得做

| 借鉴项 | 移植成本 | 价值（A 股日频选股） | 值得做？ |
|---|---|---|---|
| `COUNT(cond, n)` 算子 | 低 | 中高（20 条 191 因子依赖） | ✅ 建议 |
| `SMA(A,n,m)` 带权重平滑 | 低 | 中（中国券商口径惯例） | ✅ 建议 |
| `adv{d}` 一族 | 低 | 高（Alpha101 大量依赖 + 流动性风控） | ✅ 建议 |
| `decay_linear` 权重口径固化 + 单测 | 低 | 中高 | ✅ 建议 |
| `ts_argmax/argmin` 位置算子（明确定义起点） | 低 | 中 | ✅ 建议 |
| `scale` / `product` | 低 | 中 | ✅ 可选 |
| **把 Alpha101/191 公式作为"研报原文元数据"入库** | 低 | 高（口径可追溯） | ✅ 建议 |
| **建立 "Alpha101/191 转录错误清单" 并在实现中标注** | 低 | 高（避免照抄 bug） | ✅ 建议 |
| `REGBETA/REGRESI` 表达式内滚动回归 | 中高 | 中（仅 8 条） | ⚠️ 视 DSL 架构 |
| `ind_neutralize` 作为表达式算子 | 中 | 中 | ⚠️ 可选（~20 条） |
| **整库照搬 Alpha101/191** | 高 | **低（大部分不可用 + 强共线 + 带 bug）** | ❌ 不推荐 |

> **结论**：Alpha101/191 的正确用法是**"算子清单 + 公式元数据 + bug 黑名单"**，不是"把因子批量接进来"。lquant 的既有预处理/正交化链路已经比这些库完整得多。

---

## C. 符号回归 / 遗传规划 / LLM 挖因子

### C.1 项目清单、活跃度、技术栈

| 项目 | 定位 | 活跃度（证据） | 技术栈 |
|---|---|---|---|
| **gplearn** | GP 符号回归的 sklearn 风格实现，事实标准 | 官方文档 **0.4.3** 在维护（[API 参考](https://gplearn.readthedocs.io/en/stable/reference.html)）；有已知性能 issue（[#71](https://github.com/trevorstephens/gplearn/issues/71)） | numpy/scipy/scikit-learn/joblib；**无 pandas 强制依赖** |
| **DEAP** | 通用进化算法框架（GA/GP/NSGA-II…） | 长期维护，文档含符号回归教程（[symbreg.rst](https://raw.githubusercontent.com/p-chambers/deap/refs/tags/0.7.1/doc/examples/symbreg.rst)） | 纯 Python，轻依赖；学习曲线陡 |
| **AlphaGen**（KDD 2023） | RL 生成"协同"公式化 alpha 集合 | 官方仓库 `ICT-FinD-Lab/alphagen`，并已扩展出 HARLA（[README](https://github.com/RL-MLDM/alphagen/blob/master/README.md)，论文 Frontiers of Computer Science 2026） | PyTorch + stable-baselines3 + qlib（数据适配层）；**重** |
| **AutoAlpha** | 分层进化（结构 GA + 参数局部搜索） | 社区仓库（[szy1900/autoAlpha](https://relatedrepos.com/gh/szy1900/autoAlpha)、[AutoAlpha2022](https://relatedrepos.com/gh/AutoAlpha2022/AutoAlpha)） | 未核实 |
| **OpenEvolve** | AlphaEvolve 的开源复现（进化 + LLM 变异） | PyPI 有包（[openevolve](https://pypi.org/project/openevolve/0.0.20/)） | Python + LLM API |
| **RD-Agent**（微软） | LLM 驱动的自动化研发（含因子挖掘） | 活跃（[部署教程](https://easyclaw.ijinshan.com/skills/rd-agent/)） | Python + LLM；重 |

### C.2 可借鉴的算法口径与接口设计

#### (1) gplearn：**bloat 控制与 OOB 验证**（lquant GP 最缺的两块）

**已抓取的确切默认值**（[官方 API 参考](https://gplearn.readthedocs.io/en/stable/reference.html)）：

```python
SymbolicRegressor(
    population_size=1000, generations=20, tournament_size=20,
    stopping_criteria=0.0,
    const_range=(-1.0, 1.0), init_depth=(2, 6), init_method='half and half',
    function_set=('add','sub','mul','div'),
    metric='mean absolute error',
    parsimony_coefficient=0.001,
    p_crossover=0.9, p_subtree_mutation=0.01, p_hoist_mutation=0.01,
    p_point_mutation=0.01, p_point_replace=0.05,
    max_samples=1.0, warm_start=False, low_memory=False,
    n_jobs=1, verbose=0, random_state=None)

SymbolicTransformer(
    population_size=1000, hall_of_fame=100, n_components=10,
    generations=20, tournament_size=20, stopping_criteria=1.0,
    ... , metric='pearson', ...)   # 默认 metric 是 pearson，不是 IC
```

**可借鉴点 A — parsimony pressure（抗 bloat）**：

> `parsimony_coefficient` float or "auto", default=0.001 — This constant penalizes large programs by adjusting their fitness to be less favorable for selection… If "auto" the parsimony coefficient is recalculated for each generation using **c = Cov(l,f)/Var(l)**, where Cov(l,f) is the covariance between program size l and program fitness f in the population, and Var(l) is the variance of program sizes.

- 适应度惩罚形式为 `fitness = raw_fitness + parsimony·length`（`raw_fitness` 会被取负号以便"越大越好"；具体符号以源码为准，**逐位换算未核实**）。
- **lquant 的 `fitness.py` 只有 `|IC_neutral| - ic_decay_hinge`，完全没有复杂度惩罚**。这是最直接可搬的一块。

**可借鉴点 B — `max_samples` 袋外（OOB）适应度**：

> `max_samples` float, default=1.0 — The fraction of samples to draw from X to evaluate each program on.

属性里有：
> `'best_oob_fitness'` : The out of bag fitness of the best program in the generation (**requires max_samples < 1.0**).

- 每个个体在**随机子样本**上评估 fitness，剩余样本给出 OOB fitness 用于监控泛化。**这是 GP 里对抗过拟合最便宜有效的手段**，lquant 的 GP 目前没有。

**可借鉴点 C — 五种遗传算子 + `hall_of_fame` 去相关选择**：

- 交叉 `p_crossover=0.9`、子树变异 `p_subtree_mutation=0.01`、**提升变异 `p_hoist_mutation=0.01`（专门用于控制 bloat）**、点变异 `p_point_mutation=0.01`、`p_point_replace=0.05`；"概率之和 ≤1，余额归 reproduction（原样复制）"。
- `SymbolicTransformer` 从 `hall_of_fame=100` 个最优个体里挑出 `n_components=10` 个**彼此相关性最低**的——**这正是 lquant 的 `dedup.py` 想做的事，但 gplearn 是在搜索循环内做，能主动产出低相关因子集合**。

**可借鉴点 D — 受保护的算子语义（避免除零/NaN 爆炸）**：

> `'div'` : protected division where a denominator near-zero returns 1.；`'sqrt'` : protected square root where the absolute value of the argument is used；`'log'` : protected log where the absolute value of the argument is used and a near-zero argument returns 0.；`'inv'` : protected inverse where a near-zero argument returns 0.

**lquant 的 GP 变异会随机改窗口为 `[3,5,10,20,60]`，但没有系统性的"受保护算子"设计**——建议在 DSL 求值层统一为除法/开方/对数加保护，而不是靠变异避开。

#### (2) DEAP：**多目标 + 通用进化骨架**

DEAP 的 `Toolbox`/`creator` 机制与 `algorithms.eaSimple`、`tools.selTournament`、`selNSGA2`、`cxTwoPoint`、`cxUniform`、`mutUniform`、`HallOfFame` 是其核心（官方符号回归示例见 [symbreg.rst](https://raw.githubusercontent.com/p-chambers/deap/refs/tags/0.7.1/doc/examples/symbreg.rst)）。

- **关键借鉴：把因子挖掘建模成多目标问题** —— `(IC 绝对值, 复杂度)` 或 `(IC, 1-换手)` 用 **NSGA-II（`selNSGA2` + `eaMuPlusLambda`）** 求 Pareto 前沿，而不是把复杂度塞进单一标量适应度。
- 相比 gplearn，DEAP 更灵活（可自定义任意个体编码与算子），但**没有 `max_samples` 的 OOB 机制**，需要自己搭。
- 具体 API 名以官方文档为准：<https://deap.readthedocs.io/>（**未逐行抓取核对，签名请以文档为准**）。

#### (3) AlphaGen：**给 lquant 挖矿模块最大启发的项目**（源码已逐段核对）

**(a) 表达式与算子实现**（`alphagen/data/expression.py`）

```python
class CSRank(UnaryOperator):          # 截面 rank
    def _apply(self, operand):
        nan_mask = operand.isnan()
        n = (~nan_mask).sum(dim=1, keepdim=True)
        rank = operand.argsort().argsort() / n
        rank[nan_mask] = torch.nan
        return rank

class Rank(RollingOperator):          # 时间序列 rank（处理并列）
    def _apply(self, operand):
        n = operand.shape[-1]
        last = operand[:, :, -1, None]
        left  = (last <  operand).count_nonzero(dim=-1)
        right = (last <= operand).count_nonzero(dim=-1)
        return (right + left + (right > left)) / (2 * n)

class WMA(RollingOperator):           # = decay_linear
    def _apply(self, operand):
        n = operand.shape[-1]
        weights = torch.arange(n, dtype=..., device=...); weights /= weights.sum()
        return (weights * operand).sum(dim=-1)

class EMA(RollingOperator):
    def _apply(self, operand):
        n = operand.shape[-1]
        alpha = 1 - 2 / (1 + n)
        power = torch.arange(n, 0, -1, ...)
        weights = alpha ** power; weights /= weights.sum()
        return (weights * operand).sum(dim=-1)

class Corr(PairRollingOperator):
    def _apply(self, lhs, rhs):
        clhs = lhs - lhs.mean(-1, keepdim=True); crhs = rhs - rhs.mean(-1, keepdim=True)
        ncov = (clhs * crhs).sum(-1)
        nlvar = (clhs**2).sum(-1); nrvar = (crhs**2).sum(-1)
        stdmul = (nlvar * nrvar).sqrt()
        stdmul[(nlvar < 1e-6) | (nrvar < 1e-6)] = 1     # ← 常数序列保护
        return ncov / stdmul
```

**算子全集**：`Abs, Sign, Log, CSRank, Add, Sub, Mul, Div, Pow, Greater(=max), Less(=min), Ref, Mean, Sum, Std, Var, Skew, Kurt, Max, Min, Med, Mad, Rank, Delta, WMA, EMA, Cov, Corr`。

**可直接借鉴的三处口径**：
1. **`CSRank` 的 NaN 处理**：先记 `nan_mask`，排完序后把 NaN 位置还原为 NaN。**很多实现忽略这一步**，导致 NaN 被当作最小值参与排名。
2. **`Rank` 的并列值公式** `(right+left+(right>left))/(2n)`。
3. **`Corr` 的近零方差保护**：`stdmul < 1e-6` 时把分母置 1（返回 0 而非 NaN/inf）。**stop/一字板横盘股在 A 股极常见，这个保护是必需的**。

**(b) 表达式前置校验 —— 一个非常优雅的设计**（`expression.py`）

```python
class Operator(Expression):
    @classmethod @abstractmethod
    def n_args(cls) -> int: ...
    @classmethod @abstractmethod
    def category_type(cls) -> Type["Operator"]: ...
    @classmethod @abstractmethod
    def validate_parameters(cls, *args) -> Maybe[str]: ...   # Maybe = some(msg)|none

    @classmethod
    def _check_arity(cls, *args) -> Maybe[str]: ...          # 参数个数
    @classmethod
    def _check_exprs_featured(cls, args) -> Maybe[str]: ...  # 至少一个 operand 是特征（不是纯常数）
    @classmethod
    def _check_delta_time(cls, arg) -> Maybe[str]: ...       # 窗口必须是 DeltaTime/int
```

- 用 `Maybe[str]` 做**可组合的失败累积**（`.or_else(...)`），能在**求值之前**拒绝非法表达式树（"`Mean(2.0, 5d)` 没有特征操作数"、"窗口位置传了个表达式"）。
- 还有 `class OutOfDataRangeError(IndexError)`：`Feature.evaluate` 显式检查 `period.start < -data.max_backtrack_days` —— **越界直接抛错，而不是静默返回错位数据**。
- **lquant 的 GP 目前只在 `unparse` 后做 `_seen` 去重，没有树级合法性校验**。这个 `validate_parameters` 模式**移植成本低、收益高**（能在生成阶段挡掉大量无效候选，节省评估预算）。

**(c) 适应度/评价接口**（`alphagen/data/calculator.py`）

```python
class AlphaCalculator(metaclass=ABCMeta):
    def calc_single_IC_ret(self, expr) -> float
    def calc_single_rIC_ret(self, expr) -> float
    def calc_single_all_ret(self, expr) -> Tuple[float, float]
    def calc_mutual_IC(self, expr1, expr2) -> float          # ← 因子间互相关
    def calc_pool_IC_ret(self, exprs, weights) -> float      # ← 池子线性组合后的 IC
    def calc_pool_rIC_ret(self, exprs, weights) -> float
    def calc_pool_all_ret(self, exprs, weights) -> Tuple[float, float]
```

`TensorAlphaCalculator` 还提供：
```python
def make_ensemble_alpha(self, exprs, weights):   # 加权线性合成
def _calc_ICIR(self, v1, v2):                     # mean/std of batch IC
def calc_pool_all_ret_with_ir(...) -> (IC, ICIR, RankIC, RankICIR)
```

**这套接口的核心洞见**：**奖励不是"单个因子的 IC"，而是"把新因子加进池子后，池子组合的 IC"**。这就是论文标题里的 "Synergistic"（协同）。**`calc_mutual_IC` 让算法显式知道"这个因子和池子里已有的有多像"**。

> **对 lquant 的直接冲击**：`fitness.py` 目前是 `fitness = |IC_neutral| - decay_hinge`，**完全是单因子视角**。AlphaGen 的 pool-based reward 是解决"挖出 100 个高 IC 但彼此 0.95 相关的因子"这个经典失败模式的**算法级**方案。**这是本报告 Top 1 级别的借鉴项。**

**(d) LLM 挖因子（AlphaGen 的 `alphagen_llm`）**

- 模块划分：`client/`（OpenAI / llama.cpp 客户端）、`prompts/`（`system_prompt.py`、`interaction.py`、`common.py`）、脚本 `scripts/llm_only.py`、`scripts/llm_test_validity.py`（专门测试 system prompt 对"有效 alpha 率"的影响）。
- **接口设计**：LLM 客户端是**可替换 `base.py` 抽象**，因此本地模型和 API 模型同构。`llm_test_validity.py` 的存在说明他们把**"LLM 产出的公式有多少能通过编译+求值"当成一个可测量的指标** —— 这正是 lquant `mining/llm.py`（40 行）最该补的度量。

#### (4) AutoAlpha / OpenEvolve / RD-Agent（借鉴点，细节未核实）

- **AutoAlpha**：分层进化——**外层 GA 搜结构，内层局部搜索调参数（窗口长度、常数）**。相比 gplearn 只搜结构，这对"窗口该是 5 还是 20 天"这类连续/离散参数更有效。**未核实源码细节**。
- **OpenEvolve / AlphaEvolve 类**：**岛模型（island model）+ LLM 变异**，用 LLM 做"有语义的"变异（而非随机改算子）。对 A 股场景的价值取决于是否有领域 prompt。
- **RD-Agent**：假设生成 → 代码实现 → 回测反馈的闭环。设计层面值得参考"让 LLM 输出可执行代码并被自动验证"的工程约束。

### C.3 与 lquant 已实现部分的差异

lquant 的 `mining/gp.py` 是 102 行的**极简 GP**：

```python
GPGenerator(seed=None, pop_size=40, elite=6, p_mutation=0.35)
# 选择：pop.sort by -fitness，从 top-elite 随机取两个父代
# 交叉：单点替换 a 的随机位置（replace(a, depth=randint(0,2))）—— 实际深度极浅
# 变异：_mutate(node, force) 随机改 Field/窗口 Num
# 去重：_seen 集合，重复则重新随机生成
```

对照结论：

| 能力 | gplearn / AlphaGen / DEAP | lquant `mining/` | 差距 |
|---|---|---|---|
| Bloat / 复杂度惩罚 | gplearn `parsimony_coefficient=0.001` 或 `auto`（Cov/Var） | **无** | **缺，优先级高** |
| OOB / 样本外适应度 | gplearn `max_samples<1` + `best_oob_fitness` | **无** | **缺，优先级高** |
| 因子池协同奖励 | AlphaGen `calc_pool_IC_ret` + `calc_mutual_IC` | **无（只有单因子 fitness）** | **缺，优先级最高** |
| 多样性/去相关选择 | gplearn `hall_of_fame=100 → n_components=10` 挑最低相关 | 有 `dedup.py` 但在挖掘之外 | 可整合 |
| 多目标 Pareto | DEAP `selNSGA2` + `eaMuPlusLambda` | **无** | 缺（中优先） |
| 遗传算子多样性 | gplearn 5 种（含 hoist 抗 bloat） | 只有 crossover + mutation | 中 |
| 表达式前置校验 | AlphaGen `validate_parameters`（Maybe 累积） | **无** | **缺，低成本** |
| 受保护算子 | gplearn `div/sqrt/log/inv` + AlphaGen `Corr` 保护 | DSL 层待核 | 需确认 |
| 锦标赛选择 | `tournament_size=20` | 仅 elite 随机 | 低-中 |
| 多重检验校正 | **三者都没有** | **有 `corrected_threshold = sqrt(2·ln(n_trials))`** | **lquant 领先** |
| 衰减惩罚 | 三者都没有 | **有 `decay_hinge`** | **lquant 领先** |

> **关键判断**：lquant 的 GP **在统计纪律上（多重检验、衰减惩罚）已经领先开源**，但**在搜索算法本身（bloat 控制、OOB、协同奖励、多目标）落后**。方向应该是**把 gplearn/AlphaGen 的搜索机制搬进来，保留自己的统计纪律**。这个组合没有现成开源项目做过。

### C.4 已知坑与失败教训

1. **gplearn 并行反而更慢**：官方 issue [#71 "Run in parallel took much more time than single job"](https://github.com/trevorstephens/gplearn/issues/71)；相关性能讨论见 [DeepWiki: Parallel Processing and Performance](https://deepwiki.com/trevorstephens/gplearn/5.3-parallel-processing-and-performance)。**lquant 若给 GP 加多进程，必须先做小样本基准**。
2. **gplearn `parsimony_coefficient` 需反复调**（文档原话："This parameter may need to be tuned over successive runs"），`auto` 也不是万能。
3. **`metric='pearson'/'spearman'` 不直接预测目标**（文档明说），`SymbolicTransformer` 默认 `metric='pearson'` —— **默认设置不是 IC/ICIR**，用在因子场景需要显式改。
4. **以全样本 IC 作为奖励 = 数据窥探**：AlphaGen 用 train/valid/test 切分（README 的 `start_time/end_time`），但**多数复现没有严格 OOS**。lquant 的 `sqrt(2·ln n)` 校正正是为此，**必须保证 n_trials 统计的是"全部尝试过的表达式数"，而不是"入库数"**，否则门槛会被严重低估。
5. **AlphaGen 复现门槛高**：依赖 PyTorch + stable-baselines3 + qlib 数据适配（`alphagen_qlib/stock_data.py` 需要 qlib 的 metadata），README 明确要求先走 qlib 数据准备流程。**直接复现成本高**。
6. **DEAP 学习曲线**：`creator`/`Toolbox` 的全局可变状态风格与现代 Python 工程习惯冲突，**适合当算法参考而非依赖**。
7. **LLM 挖矿的有效率问题**：AlphaGen 专门有 `llm_test_validity.py` 测 system prompt 对有效 alpha 率的影响——说明**LLM 直接生成的公式大量不可编译/不可求值**，需要自动校验层。

### C.5 移植成本 / 价值 / 是否值得做

| 借鉴项 | 移植成本 | 价值 | 值得做？ |
|---|---|---|---|
| **因子池协同奖励（`calc_pool_IC_ret` + `calc_mutual_IC`）** | **中** | **极高**（直击"高 IC 但高相关"失败模式） | ✅✅ 最高优先 |
| **AlphaGen 的 `validate_parameters` 前置校验** | **低** | **高**（省评估预算 + 防非法树） | ✅✅ 强烈建议 |
| **gplearn `max_samples` + OOB fitness** | **低** | **高**（抗过拟合） | ✅✅ 强烈建议 |
| **gplearn parsimony（`c=Cov(l,f)/Var(l)`）** | **低** | 高（抗 bloat，结果可读） | ✅ 建议 |
| `hall_of_fame → n_components` 低相关挑选 | 低 | 中高（与 `dedup.py` 合并） | ✅ 建议 |
| AlphaGen 的 `Rank`/`CSRank`/`Corr` NaN 与并列口径 | 低 | 中（口径纯度） | ✅ 建议 |
| 受保护算子（div/sqrt/log/inv） | 低 | 中高 | ✅ 建议 |
| 锦标赛选择 | 低 | 低中 | ⚠️ 可选 |
| 多目标 NSGA-II（DEAP） | 中高 | 中（IC vs 复杂度 vs 换手） | ⚠️ 中期 |
| AutoAlpha 分层参数搜索 | 中高 | 中 | ⚠️ 中期 |
| OpenEvolve 岛模型 + LLM 变异 | 高 | 中（不确定） | ⚠️ 观望 |
| RD-Agent 闭环 | 高 | 低中（与 lquant 现有 LLM 管线重叠） | ❌ 不必 |
| 引入 AlphaGen 运行时（Torch+SB3+qlib） | 高 | 低（lquant 是 Polars/Rust） | ❌ 只借设计 |

---

## D. 组合优化：PyPortfolioOpt / Riskfolio-Lib / skfolio / cvxportfolio

### D.1 项目清单、活跃度、技术栈

| 项目 | 定位 | 活跃度 / 技术栈 | 证据 |
|---|---|---|---|
| **PyPortfolioOpt** | 经典均值方差 + CLA + HRP，最易上手 | 长期维护；pandas + **cvxpy** | [EfficientFrontier 源码](https://raw.githubusercontent.com/robertmartin8/PyPortfolioOpt/master/pypfopt/efficient_frontier/efficient_frontier.py) · [HRP 源码](https://github.com/PyPortfolio/PyPortfolioOpt/blob/main/pypfopt/hierarchical_portfolio.py) · [文档](https://pyportfolioopt.readthedocs.io/en/stable/OtherOptimizers.html) |
| **Riskfolio-Lib** | **风险度量最全**的组合优化库 | 活跃，README 标注 **7.4 (2026)**；cvxpy + pandas + sklearn + statsmodels + **arch** + anastropy + networkx + xlsxwriter（**依赖很重**） | [README](https://github.com/dcajasn/Riskfolio-Lib) · [文档](https://riskfolio-lib.readthedocs.io/en/latest/) |
| **skfolio** | **sklearn 风格**的组合优化 + 因子模型 + 模型选择 | 活跃，README 标注 **1.0.0 (2026)**，论文 arXiv:2507.04176；sklearn + cvxpy + scipy | [README](https://raw.githubusercontent.com/skfolio/skfolio/main/README.rst) · [论文](https://arxiv.org/abs/2507.04176) · [站点](https://skfolio.org) |
| **cvxportfolio** | 多期优化 + 显式交易成本/滑点 | 活跃，文档完善 | [Trading policies](https://www.cvxportfolio.com/en/1.0.1/policies.html) · [多期示例](https://www.cvxportfolio.com/en/1.5.0/examples/paper_examples/multi_period_opt.rst.txt) |

**技术栈总结**：**全部是 cvxpy + pandas 生态，没有一个是 Polars**。lquant 的 `weighting.py` 用 SciPy `SLSQP` + numpy，**在依赖重量上明显更轻**；但在凸优化问题的健壮性（不可行判定、solver 回退、风险度量覆盖）上弱于 cvxpy 系。

### D.2 可借鉴的算法口径与接口设计

#### (1) PyPortfolioOpt `EfficientFrontier`（源码逐行核对）

```python
EfficientFrontier(expected_returns, cov_matrix, weight_bounds=(0, 1),
                  solver=None, verbose=False, solver_options=None)
```
- `weight_bounds=(0,1)` 默认 **long-only、单票上限 100%**；做空需 `(-1,1)`。
- 方法：`min_volatility()`、`max_sharpe(risk_free_rate=0.0)`、`max_quadratic_utility(risk_aversion=1, market_neutral=False)`、`efficient_risk(target_volatility, market_neutral=False)`、`efficient_return(target_return, market_neutral=False)`。

**`max_sharpe` 的变量替换技巧（很值得借）**：

```python
self._objective = cp.quad_form(self._w, self.cov_matrix, assume_PSD=True)
k = cp.Variable()
# 把所有不等式/等式约束整体乘 k 重建
self._constraints = [
    (self.expected_returns - risk_free_rate).T @ self._w == 1,
    cp.sum(self._w) == k,
    k >= 0,
] + new_constraints
# 求解后反变换
self.weights = (self._w.value / k.value).round(16) + 0.0
```
这是把**分式规划（Sharpe 最大化）转成凸 QP** 的标准 Charnes-Cooper 变换。**lquant 目前没有 max_sharpe**，若要做，这是正确的实现路径。

**`efficient_risk` 的可行性预检（一个很好的工程细节）**：
```python
global_min_volatility = np.sqrt(1 / np.sum(np.linalg.pinv(self.cov_matrix)))
if target_volatility < global_min_volatility:
    raise ValueError("The minimum volatility is {:.3f}. Please use a higher target_volatility")
```
用**伪逆**算全局最小方差（对奇异协方差稳健），并把"你给的目标波动率不可行"变成**可读的报错**。**lquant 的 `optimizer.py` 已有"不可行时退回基准权重"的处理，但 PyPortfolioOpt 的"先算下界再报错"更友好**。

**注意 `max_sharpe` 的一个真实坑（源码注释自己承认）**：
```python
warnings.warn("max_sharpe transforms the optimization problem so additional objectives may not work as expected.")
```
→ **`max_sharpe` + L2 正则/额外目标函数的组合是不安全的**。

#### (2) PyPortfolioOpt 的风险模型接口（L2 收缩）

lquant 已有 `objective_functions` 概念的对应物（额外目标）。PyPortfolioOpt 的约定是 `add_objective(obj_func, **kwargs)`，其中 `objective_functions.L2_reg(w, gamma=1)` 是**用 L2 惩罚把权重往 0 拉**，用于缓解权重集中。
> **对 lquant 的启示**：`weighting.py` 的 `min_variance` 目前只有 `max_weight` 单边约束。**"L2 正则 / 权重集中惩罚"是比硬上限更平滑的手段**（[文档](https://pyportfolioopt.readthedocs.io/en/stable/OtherOptimizers.html)）。具体 `gamma` 默认值**未逐字核实**。

#### (3) Riskfolio-Lib：**风险度量矩阵**（README 完整核对）

**26 种凸风险度量**（分为 Dispersion / Downside / Drawdown 三类）：

- 离散类：标准差、平方根峰度、2p 阶偶次矩的 p 次根、MAD、**Gini 均差（GMD）**、CVaR 极差、Tail Gini 极差、EVaR 极差、RLVaR 极差、极差。
- 下行类：半标准差、平方根半峰度、p 次根偶次半矩、**一阶下偏矩（Omega 比）**、**二阶下偏矩（Sortino）**、**CVaR**、Tail Gini、**EVaR**、**RLVaR**、最坏情形（Minimax）。
- 回撤类：平均回撤、**Ulcer 指数**、**CDaR**、**EDaR**、RLDaR、**最大回撤（Calmar）**。

其它能力（均为 README 明文）：
- **风险平价支持 22 种风险度量**；**HRP/HERC 支持 37 种风险度量**（naive risk parity）。
- NCO（Nested Clustered Optimization）四种目标函数。
- 最坏情形均值方差、松弛风险平价、**OWA（有序加权平均）**、**MVSK（均值-方差-偏度-峰度，半定松弛）**。
- Black-Litterman（含贝叶斯/增广）、Entropy Pooling、风险因子模型。
- 约束：跟踪误差、换手、**最大持仓数、有效资产数**、图约束、**方差风险贡献约束、因子风险贡献约束**、基数约束、互斥/联合投资。
- 工具：24 种风险度量的有效前沿、风险贡献（按资产/按因子）、不确定性集（均值/协方差的稳健估计）、**载荷矩阵估计（逐步回归 + 主成分回归）**、资产聚类。

**求解器–风险度量对应表（README 原表，工程上极有用）**：

| 风险度量 | LP | QP | SOCP | SDP | EXP | POW |
|---|---|---|---|---|---|---|
| 方差 MV | | | X | X* | | |
| MAD | X | | | | | |
| GMD | | | | | | X** |
| CVaR | X | | | | | |
| EVaR / EDaR | | | | | X** | |
| CDaR / MDD / 平均回撤 | X | | | | | |
| Ulcer | | | X | | | |
| RLVaR / RLDaR / Tail Gini | | | | | | X** |
| 峰度 / 半峰度 | | | | X | | |

> `**` ：这些模型**强烈建议用 MOSEK**，"due to in some cases CLARABEL cannot find a solution and SCS takes too much time"。**对 lquant 的警示：CVaR/CDaR 是 LP（快、开源即可），但 EVaR/RLVaR/峰度需要商用 solver 才实用。选风险度量时这条表决定工程可行性。**

> **精确方法签名（`Portfolio(...)`、`optimization(model=, rm=, obj=, ...)`、`hrp_optimization(linkage=, ...)` 等）本轮未逐字抓取核对，标注「未核实」**，请以 <https://riskfolio-lib.readthedocs.io/en/latest/> 为准。

#### (4) skfolio：**"防过拟合的模型选择"这一层是 lquant 最大的空白**（README 完整核对）

**(a) 统一模型清单（README 原文）**

- 组合优化：Naive（Equal-Weighted / Inverse-Volatility / Random-Dirichlet）、Convex（**Mean-Risk** / **RiskBudgeting** / **MaximumDiversification** / **DistributionallyRobustCVaR** / **BenchmarkTracker**）、Clustering（**HRP** / **HERC** / **SchurComplementaryAllocation** / **NestedClustersOptimization**）、Ensemble（Stacking）。
- 先验估计：Empirical、**CharacteristicsFactorModel**、TimeSeriesFactorModel、**BlackLitterman**、SyntheticData、**EntropyPooling**、**OpinionPooling**。
- 期望收益估计：Empirical / **ExponentiallyWeighted** / Equilibrium / **Shrinkage**。
- 协方差估计：Empirical / **Gerber** / **Denoise** / **Detone** / EWM / **RegimeAdjustedEWM** / **LedoitWolf** / **OAS** / Shrunk / **GeodesicShrinkage** / **GraphicalLassoCV** / Implied。
- 距离估计：Pearson / **Kendall** / **Spearman** / Covariance / **DistanceCorrelation** / **VariationOfInformation**。
- 排序估计：**HierarchicalSeriation** / SpectralSeriation。
- 分布估计：Gaussian / Student-t / Johnson-Su / NIG / Copula（Gaussian, t, Clayton, Gumbel, Joe, Independent）/ **Vine Copula**。
- 不确定性集：均值与协方差上的 Empirical / **CircularBootstrap**。
- 预选变换器：NonDominated / SelectKExtremes / **DropHighlyCorrelatedAssets** / SelectNonExpiring / SelectComplete / DropZeroVariance。
- 截面变换器：StandardScaler / **PercentileRankScaler** / **GaussianRankScaler** / **Winsorizer** / **TanhShrinker**。
- **CV 与模型选择**：sklearn 全套 + **WalkForward** + **CombinatorialPurgedCV** + MultipleRandomizedCV + 协方差预测评价 + **OnlinePredict/OnlineScore**。
- 风险度量：Variance / SemiVariance / MAD / FLPM / **CVaR** / **EVaR** / WorstRealization / **CDaR** / MaxDrawdown / AverageDrawdown / **EDaR** / Ulcer / **GiniMeanDifference** / VaR / DaR / EntropicRisk / 四阶中心矩 / Skew / Kurtosis。
- 优化特性：MinimizeRisk / MaximizeReturns / MaximizeUtility / **MaximizeRatio** / **TransactionCosts** / **ManagementFees** / **L1/L2 正则** / Weight/Group/Budget/**TrackingError**/**Turnover**/**Cardinality**/**Threshold** 约束。

**(b) 关键接口（README 源码级摘录，可直接对照 lquant）**

```python
model = MeanRisk()                                                  # 默认 = 最小方差
model = MeanRisk(objective_function=ObjectiveFunction.MAXIMIZE_RATIO,
                 risk_measure=RiskMeasure.SEMI_VARIANCE)            # 最大 Sortino
model = MeanRisk(prior_estimator=EmpiricalPrior(mu_estimator=ShrunkMu(),
                                                covariance_estimator=DenoiseCovariance()))
model = MeanRisk(mu_uncertainty_set_estimator=BootstrapMuUncertaintySet())
model = RiskBudgeting(risk_measure=RiskMeasure.CVAR)                # CVaR 风险平价
model = NestedClustersOptimization(inner_estimator=MeanRisk(risk_measure=RiskMeasure.CVAR),
                                   outer_estimator=RiskBudgeting(risk_measure=RiskMeasure.VARIANCE),
                                   cv=KFold(), n_jobs=-1)

# 约束与成本（注意是 dict / 字符串线性约束的声明式写法）
model = MeanRisk(min_weights={"AAPL": 0.10, "JPM": 0.05}, max_weights=0.8,
                 transaction_costs={"AAPL": 0.0001, "RRC": 0.0002},
                 groups=[["Equity"]*3 + ["Fund"]*5 + ["Bond"]*12,
                         ["US"]*2 + ["Europe"]*8 + ["Japan"]*10],
                 linear_constraints=["Equity <= 0.5 * Bond", "US >= 0.1",
                                     "Europe >= 0.5 * Fund", "Japan <= 1"])

# 模型选择
cv = CombinatorialPurgedCV(n_folds=10, n_test_folds=2)
cv = WalkForward(train_size=252, test_size=60)
RandomizedSearchCV(estimator=MeanRisk(), cv=WalkForward(train_size=252, test_size=60),
                   param_distributions={"l2_coef": loguniform(1e-3, 1e-1)})
GridSearchCV(estimator=model, cv=KFold(n_splits=5, shuffle=False),
             param_grid={"risk_measure": [RiskMeasure.VARIANCE, RiskMeasure.CVAR,
                                          RiskMeasure.CDAR],
                         "prior_estimator__mu_estimator__half_life": [10,20,30,40]})

# 特征因子风险模型（Barra 式）
model = CharacteristicsFactorModel(
    factors=[("market", GlobalFactor(family="market")),
             ("industry", OneHotCategoricalFactors(category="industry", family="industry")),
             ("beta", FixedWeightedFactor(descriptors=[("market_beta", EWMarketBeta(half_life=year))],
                                          transform_by_group="industry")),
             ("momentum", FixedWeightedFactor(descriptors=[("momentum", EWMomentum(half_life=half_year, skip=month))],
                                              transform_by_group="industry")),
             ("size", FixedWeightedFactor(descriptors=[("log_market_cap", LogMarketCap())],
                                          transform_by_group="industry")),
             ("value", FixedWeightedFactor(descriptors=[("book_to_price", BookToPrice()),
                                                        ("sales_to_price", SalesToPrice()),
                                                        ("cash_flow_to_price", CashFlowToPrice())],
                                           weights=[0.8, 0.1, 0.1],
                                           transform_by_group="industry"))],
    neutralize_against={"volatility": ["beta"], "non_linear_size": ["size"]},
    constrained_families=[("industry", None)],
    exposure_lag=1,
    inv_idio_variance_weight_shrinkage=0.5,
    n_jobs=-1)
model.fit(characteristics=characteristics)
```

**这里有三个对 lquant 极重要的设计细节**：

1. **`Prewired` 因子之间的正交关系被显式声明**：`neutralize_against={"non_linear_size": ["size"]}` —— 即"非线性规模先对规模正交化"。**lquant 的 `orthogonalize.py` 是数据驱动的（按输入列一起正交），而 skfolio 是"模型驱动、显式声明依赖图"**。后者更可解释、更可复现，避免"加了一个因子导致所有因子都变"。
2. **`constrained_families=[("industry", None)]`**：约束行业因子暴露之和为零（行业中性约束）。**这是 Barra 式风险模型的标准约束**。
3. **`exposure_lag=1`**：因子暴露相对收益**滞后 1 期**，是**防前视的显式旋钮**（因子暴露在 t 期可用，解释 t 期收益）。lquant 的时间对齐若无此显式参数，建议补。
4. **`inv_idio_variance_weight_shrinkage=0.5`**：特质方差的收缩系数，直接对应 Barra 的 `σ_spec` 处理。

**(c) CV 设计：`CombinatorialPurgedCV` 与 `WalkForward`**

- `WalkForward(train_size=252, test_size=60)`：**滚动的样本外评估**，A 股调仓周期常用口径。
- `CombinatorialPurgedCV(n_folds, n_test_folds)`：来自 López de Prado 的**组合purged交叉验证**，用于"多次尝试后的最优结果的显著性评估"。**这正好补足 lquant `fitness.py` 的 `sqrt(2·ln n)` 校正**——两者是互补的：前者给"路径分布"，后者给"多重检验门槛"。

#### (5) cvxportfolio：多期与显式成本（**细节未核实**）

文档明示有 policy 抽象与多期优化（[policies](https://www.cvxportfolio.com/en/1.0.1/policies.html)、[multi_period_opt 示例源码](https://www.cvxportfolio.com/en/1.5.0/examples/paper_examples/multi_period_opt.rst.txt)）。核心价值是**把交易成本、滑点、持仓成本、杠杆成本作为优化目标的一部分，而不是事后扣减**。
> **精确类名（`MultiPeriodOptimization`）与签名未核实**，请以文档为准。对 lquant 的意义：**若将来需要"多期/带成本的最优调仓"，这是唯一把成本内生的开源参考**。

### D.3 与 lquant 已实现部分的差异

| 能力 | 开源现状 | lquant | 判定 |
|---|---|---|---|
| 权重方案 | PPO 5 种；Riskfolio 26 风险度量；skfolio 4 大类 | equal/mcap/inverse_vol/score/risk_parity/min_variance/hrp | **lquant 缺风险度量维度** |
| 目标函数 | min vol / max sharpe / max QU / eff-risk / eff-return / max ratio | 仅 min_variance + 增强指数(TE) | **lquant 缺 max_sharpe（需 Charnes-Cooper）**、**缺 max ratio(风险调整)** |
| 风险度量 | CVaR/EVaR/CDaR/EDaR/Ulcer/MDD/GMD/VaR… | **只用方差** | **最大差距** |
| 协方差 | LW/OAS/Gerber/Denoise/Detone/Regime/GraphicalLasso | **LW/OAS/structured PCA/POET** | **lquant 已不错**，缺 Denoise/Detone |
| 收缩期望收益 | skfolio `ShrunkMu`、EWMu(half_life) | 无 | 缺 |
| 不确定性集/稳健 | Bootstrap uncertainty set、DR-CVaR、Worst-case MV | 无 | 缺（对 A 股噪声大有用） |
| 交易成本 | skfolio `transaction_costs` dict；cvxportfolio 内生 | 无（应在 backtest 层） | 缺（内生 vs 事后） |
| 换手约束 | skfolio Turnover；Riskfolio turnover | 无显式 | 缺 |
| 基数/最大持仓数 | Riskfolio cardinality、effective-N、skfolio；**lquant `screener.py` 为选股前过滤** | 部分 | 中 |
| 组/行业约束 | skfolio `groups`/`linear_constraints`；Riskfolio 类约束 | 无 | 缺 |
| L1/L2 正则 | skfolio `l1_coef`/`l2_coef`；PPO `L2_reg(gamma)` | 无 | 缺 |
| 模型选择/CV | skfolio PurgedCV/WalkForward + sklearn GridSearch | 无（lquant 有 backtest 但非组合层 CV） | **重大缺口** |
| 因子风险模型 | skfolio `CharacteristicsFactorModel`（声明式） | `riskmodel.py` 是**统计协方差**，非**因子结构模型** | **重大缺口** |
| 求解器 | cvxpy（clarabel/scs/MOSEK/GUROBI） | SciPy SLSQP | SLSQP 对高维/多约束**不健壮** |
| 依赖重量 | 全部 pandas + cvxpy（Riskfolio 还要 astropy/arch/networkx/xlsxwriter） | numpy/scipy/polars | **lquant 明显更轻** |
| Polars 原生 | **无一个** | 是 | lquant 领先 |

### D.4 已知坑与失败教训

1. **均值方差对输入极度敏感**：**skfolio README 自己开篇就承认**："It is well-known that naive allocation (1/N, inverse-vol, etc.) tends to outperform MVO out-of-sample (DeMiguel, 2007)." 这与 lquant `weighting.py` 文档里"等权是最难被打败的基准"完全一致 —— **两边都同意，不是新发现，但值得固化进验收门槛**。
2. **`max_sharpe` 与附加目标函数不兼容**：PPO 源码主动 `warnings.warn("max_sharpe transforms the optimization problem so additional objectives may not work as expected.")`。
3. **协方差必须 PSD**：PPO `_validate_cov_matrix` 的 docstring 明写 "This **must** be positive semidefinite, otherwise optimization will fail"。**lquant 的 `riskmodel.py` 已有 `nearest_psd`，这是正确的前置处理**。
4. **`max_sharpe` 要求至少一个资产收益 > 无风险利率**，否则直接 `ValueError`（源码显式检查 `if max(self.expected_returns) <= risk_free_rate`）。
5. **`max_quadratic_utility` 的 `risk_aversion` 必须 > 0**（源码检查）。
6. **EVaR/RLVaR/峰度类度量对 solver 要求高**：Riskfolio README 明说这些"highly recommended to use MOSEK… CLARABEL cannot find a solution and SCS takes too much time"。**开源默认 solver 下这些度量的实际可用性很差**。
7. **`efficient_return` 的 `target_return` 必须是 `float`（不接受 int）** —— PPO 源码 `if not isinstance(target_return, float): raise ValueError`。这是接口上的小陷阱。
8. **依赖重量**：Riskfolio 需要 `astropy`（为了稳健估计）、`arch`（GARCH）、`xlsxwriter`、`networkx`，**与 lquant 的轻依赖哲学冲突**；且**全部基于 pandas**。
9. **`skfolio` 的 `CombinatorialPurgedCV` 计算成本随 fold 组合爆炸**（`C(n_folds, n_test_folds)`），且 `cross_val_predict` 返回的是 MultiPeriodPortfolio 对象，**不是简单的权重矩阵**，集成成本不低。

### D.5 移植成本 / 价值 / 是否值得做

| 借鉴项 | 移植成本 | 价值（A 股日频） | 值得做？ |
|---|---|---|---|
| **CVaR / CDaR 作为可选风险度量（LP 形式，开源 solver 可用）** | **中** | **高**（A 股尾部风险重） | ✅✅ 优先 |
| **`max_sharpe` 的 Charnes-Cooper 变换** | 中 | 高（风险调整目标） | ✅ 建议 |
| **`efficient_risk(target_volatility)` + 伪逆可行性预检** | **低** | 高（比 min_variance 更可控） | ✅ 建议 |
| **L1/L2 正则 / 权重集中惩罚** | 低 | 高（缓解极端权重） | ✅ 建议 |
| **换手 + 交易成本显式约束/目标** | 中 | 高（A 股成本敏感） | ✅ 建议 |
| **`WalkForward(train_size, test_size)` 组合层 CV** | 中 | 高（参数选择防过拟合） | ✅ 建议 |
| **`CombinatorialPurgedCV`** | 中高 | 中高（与多重检验校正互补） | ✅ 建议 |
| **`DropHighlyCorrelatedAssets` / `SelectKExtremes` / `Winsorizer` / `GaussianRankScaler`** | **低** | 中高（与 lquant 预处理互补，粒度更细） | ✅ 建议 |
| **`DenoiseCovariance` / `DetoneCovariance`**（RMT 去噪、去市场模态） | 中 | 中高（lquant 现在只有朴素截断） | ✅ 建议 |
| **不确定性集 / Bootstrap / DR-CVaR** | 中高 | 中高（A 股估计噪声大） | ⚠️ 中期 |
| **声明式组/线性约束（`groups` + `linear_constraints` 字符串）** | 中 | 中高（行业/风格约束） | ✅ 建议 |
| **`EfficientFrontier` 式"按需 add_objective/add_constraint"接口** | 中 | 中 | ⚠️ 可选 |
| 引入 cvxpy | 中 | 中高（求解健壮性） | ⚠️ 权衡：+1 个重依赖换 solver 生态 |
| 引入 Riskfolio-Lib 整体 | 高 | 低（pandas + 超重依赖 + 与 Polars 冲突） | ❌ 只借度量清单 |
| 引入 skfolio 整体 | 高 | 低（同上） | ❌ 只借设计 |
| EVaR/RLVaR/峰度类度量 | 高 | 低（需商用 solver） | ❌ 不推荐 |

---

## E. 绩效与报告：empyrical / empyrical-reloaded / quantstats / ffn

### E.1 定位、活跃度、技术栈

| 项目 | 定位 | 状态 | 技术栈 |
|---|---|---|---|
| **empyrical**（`quantopian/empyrical`） | 风险/绩效指标函数库，zipline/pyfolio 依赖 | Quantopian 停更 | numpy/pandas/scipy |
| **empyrical-reloaded** | 社区维护 fork | 活跃（[仓库](https://github.com/stefan-jansen/empyrical-reloaded)） | 同上 |
| **quantstats** | 绩效报告 + 可视化 + HTML tearsheet | **活跃**，README 显示新增 **Monte Carlo 模拟**；要求 Python ≥3.10 | pandas/numpy/scipy/matplotlib/seaborn/tabulate/yfinance（+plotly 可选）（[README](https://raw.githubusercontent.com/ranaroussi/quantstats/main/README.md)） |
| **ffn** | 金融函数库（收益/回撤/统计） | 本轮**未核实** | 未核实 |

### E.2 可借鉴的算法口径与接口设计

#### (1) empyrical / empyrical-reloaded

提供了组合绩效的**标准函数集合**（函数名以官方为准，**签名未逐字核实**）：`sharpe_ratio`、`sortino_ratio`、`max_drawdown`、`calmar_ratio`、`omega_ratio`、`alpha_beta`、`stability_of_timeseries`、`tail_ratio`、`value_at_risk`、`downside_risk`、`annual_volatility` 等。**关键可借的是"年化约定"**（交易日/年、无风险利率的复利处理）——这类小口径差异是不同库报告不可比的根源。中文整理见 [empyrical 模块风险指标计算](https://raw.githubusercontent.com/rainx/inside-zipline/refs/heads/master/risks/empyrical.md)。

#### (2) quantstats：**3 模块划分 + 完整函数清单**（README 核对）

```
quantstats.stats   —— 指标计算
quantstats.plots   —— 可视化
quantstats.reports —— HTML tearsheet: metrics/plots/basic/full/html
```

**`stats` 的完整函数清单（README 原文，可直接当 lquant 报表指标 checklist）**：
```
avg_loss, avg_return, avg_win, best, cagr, calmar, common_sense_ratio, comp,
compare, compsum, conditional_value_at_risk, consecutive_losses, consecutive_wins,
cpc_index, cvar, drawdown_details, expected_return, expected_shortfall, exposure,
gain_to_pain_ratio, geometric_mean, ghpr, greeks, implied_volatility,
information_ratio, kelly_criterion, kurtosis, max_drawdown, monthly_returns,
montecarlo, montecarlo_cagr, montecarlo_drawdown, montecarlo_sharpe,
outlier_loss_ratio, outlier_win_ratio, outliers, payoff_ratio, profit_factor,
profit_ratio, r2, r_squared, rar, recovery_factor, remove_outliers, risk_of_ruin,
risk_return_ratio, rolling_greeks, ror, sharpe, skew, sortino, adjusted_sortino,
tail_ratio, to_drawdown_series, ulcer_index, ulcer_performance_index, upi,
value_at_risk, var, volatility, win_loss_ratio, win_rate, worst
```

**一个精确的签名（README 的 `help()` 输出，可直接引用）**：
```python
conditional_value_at_risk(returns, sigma=1, confidence=0.95,
                          prepare_returns=True, method='parametric')
```
→ **`method='parametric'` 是默认值**，即默认用正态假设的解析 CVaR，而不是历史模拟。**对 A 股厚尾分布这是有偏的**——建议 lquant 若实现 CVaR，**默认用历史/经验分布**并显式暴露 `method`。

**README 里一段很有价值的"口径澄清"**（原文）：
> **Win Rate** = percentage of periods with positive returns；**Consecutive Wins/Losses** = consecutive positive/negative return periods；**Payoff Ratio** = average winning period return / average losing period return；**Profit Factor** = sum of positive returns / sum of negative returns.
> These metrics … For **discretionary traders** with multi-day trades, these period-based metrics may differ from trade-level statistics.

**这是把"period-based vs trade-based"歧义显式写进文档的范例**，lquant 的 `evaluate/` 若有胜率类指标，建议照此澄清。

**已知问题（README 自述）**：
> "For some reason, I couldn't find a way to tell seaborn not to return the monthly returns heatmap when instructed to save - so even if you save the plot … it will still show the plot."

即**绘图副作用无法关闭**——`matplotlib`/`seaborn` 全局状态在集成场景下的经典问题。lquant 用 Next.js 前端 + 服务端报表，**不应把绘图库当成计算库依赖**。

#### (3) ffn

本轮**未核实**（未抓取源码/文档）。按任务要求标注。

### E.3 与 lquant 已实现部分的差异

| 能力 | 开源 | lquant | 判定 |
|---|---|---|---|
| 指标数量 | quantstats ~60 个 | `evaluate/` 有 IC/分层/衰减/归因等**因子侧**指标；组合侧绩效指标覆盖待核 | 组合侧指标可能不全 |
| 报告形态 | HTML tearsheet（quantstats）、notebook | Next.js + `evaluate/report.py` | **lquant 形态更现代** |
| 指标口径文档 | quantstats 显式澄清 period vs trade | 待核 | 建议对标 |
| 计算/绘图分离 | quantstats **不分离**（seaborn 副作用） | 天然分离 | lquant 领先 |
| 依赖重量 | quantstats 需 seaborn/matplotlib/yfinance | 重（yfinance 是**不需要**的） | lquant 更好 |

### E.4 已知坑与失败教训

1. **quantstats 强制性依赖 `yfinance`**（README 的 Requirements 明列）——**一个纯报表库拖进网络数据依赖**，在离线 A 股环境里是负担。
2. **绘图副作用不可关闭**（README 自述，见上）。
3. **empyrical 原版停更**，NaN/极端值处理在 original → reloaded 之间有过修复；`empyrical-reloaded` 的安全/维护状态可通过 [Snyk](https://security.snyk.io/package/pip/empyrical-reloaded) 查看。
4. **年化因子不统一**：不同库对"一年多少交易日""无风险利率是否复利"处理不一，**跨库对比报表会失真**。
5. **CVaR 默认正态参数法**（`method='parametric'`），对厚尾资产有偏。

### E.5 移植成本 / 价值 / 是否值得做

| 借鉴项 | 移植成本 | 价值 | 值得做？ |
|---|---|---|---|
| **quantstats 指标清单作为 lquant 报表 checklist** | **低** | 中高（补全组合侧指标） | ✅ 建议 |
| **period-based vs trade-based 口径澄清写入文档** | **低** | 中（避免误读） | ✅ 建议 |
| CVaR 默认方法显式化（历史 vs 参数） | 低 | 中高 | ✅ 建议 |
| 年化/无风险利率约定统一并写死 | 低 | 中 | ✅ 建议 |
| HTML tearsheet | 中 | 低（lquant 有 Next.js 报表） | ❌ 不必 |
| 引入 quantstats/empyrical 依赖 | 中 | 低（拉入 seaborn/yfinance） | ❌ 只借指标定义 |

---

## F. 因子去相关/正交化 与 A 股风险模型

### F.1 正交化/去相关的开源实现现状

**重要发现**：**主流组合优化库在"因子正交化"上几乎是空白**，它们把共线性问题交给协方差收缩/聚类处理：

| 项目 | 去相关手段 | 证据 |
|---|---|---|
| **Riskfolio-Lib** | **载荷矩阵估计**（逐步回归 + 主成分回归）用于因子模型；**资产聚类**（codependence 度量）；HRP/HERC 的分层聚类 | [README](https://github.com/dcajasn/Riskfolio-Lib) |
| **skfolio** | **`DropHighlyCorrelatedAssets`**（预选阶段丢弃高相关资产）；**`DenoiseCovariance`/`DetoneCovariance`**（RMT 去噪 / 去市场模态）；`neutralize_against` 显式声明因子正交依赖 | [README](https://raw.githubusercontent.com/skfolio/skfolio/main/README.rst) |
| **PyPortfolioOpt** | 无正交化；靠 `L2_reg` 与协方差 | [文档](https://pyportfolioopt.readthedocs.io/en/stable/OtherOptimizers.html) |
| **gplearn** | `SymbolicTransformer` 从 hall-of-fame 里挑**最低相关**的组合 | [API](https://gplearn.readthedocs.io/en/stable/reference.html) |
| **AlphaGen** | `calc_mutual_IC(expr1, expr2)` 在奖励里显式惩罚与其他因子的相关 | [calculator.py](https://github.com/RL-MLDM/alphagen/blob/master/alphagen/data/calculator.py) |

> **对 lquant 的判断**：lquant 的 `orthogonalize.py`（Schweinler-Wigner 对称正交 / Gram-Schmidt / PCA）+ `dedup.py`（贪心相关聚类）**在"因子/资产正交化"这件事上比上述所有开源库都完整**。这块不是空缺，是**领先项**。真正可借的是**"把正交关系声明成模型而不是数据驱动"**（skfolio 的 `neutralize_against`）和**"RMT 去噪/去模态"**（`Denoise`/`Detone`）这两个补充维度。

### F.2 Barra 类开源风险模型

| 项目 | 状态 | 证据 |
|---|---|---|
| **`Peimou/barra-risk-model`** | **极早期**。README 全文仅 3 行："A python module and user interface of a user-defined Barra risk model. Being developed continuously..."；作者为北大/哥大在读学生 | [README](https://raw.githubusercontent.com/Peimou/barra-risk-model/master/README.md) |
| **`hansihuang2016/Barra-Multiple-factor-risk-model`** | 同类仓库 | [relatedrepos](https://relatedrepos.com/gh/hansihuang2016/Barra-Multiple-factor-risk-model) |
| **`rosie068/BARRA_risk`** | 同类仓库 | [relatedrepos](https://relatedrepos.com/gh/rosie068/BARRA_risk) |

**结论：开源 Barra 实现整体不成熟**（GitHub 上多为个人作业/半成品，README 空缺、无测试、无数据管线）。**可借的是"口径"而非"代码"**。

#### 可借的 A 股 Barra 口径（来自一份生产级的 A 股多因子工作流文档，已抓全文）

来源：[aifinlab/finclaw — a-share-multifactor-model SKILL](https://raw.githubusercontent.com/aifinlab/finclaw/refs/heads/main/skills/a-share-multifactor-model/SKILL.md)

**因子体系（Barra CNE5 风格）**：`Size / Beta / Momentum / ResidVol / NLSize / BP / Liquidity / EarningsYield / Growth / Leverage` —— **10 个风格因子**。

**标准处理流程（原文）**：
1. **去极值**：MAD 法 **±3 倍**；
2. **标准化**：Z-score；
3. **缺失值**：**行业均值填充**；
4. **因子暴露需经行业和市值中性化处理**；
5. **截面回归**：每期对"股票收益 vs 因子暴露"做 **OLS**，回归系数即**因子收益率**，并算 **t 统计量**；
6. **风险模型**：因子协方差矩阵（**指数加权，半衰期 90 天**）+ 特质风险（回归残差波动率）+ 股票层面风险分解；
7. **A 股过滤**：**剔除 ST 股和次新股（上市 < 60 日）**；
8. **行业分类**：默认**申万一级（31 个行业）**。

> **这些正是 lquant `riskmodel.py` / `neutralize.py` 最需要对标的"生产口径"**：
> - **半衰期 90 天的指数加权因子协方差** —— lquant 现在有收缩/结构化/POET，但**未见 EWM 因子协方差**（skfolio 有 `ExponentiallyWeighted` / `RegimeAdjustedEWM`）。
> - **上市 < 60 日 / ST 剔除** —— 需核对 `factors/universe.py` 是否默认启用。
> - **特质风险 = 回归残差波动率** —— lquant 的 `structured_cov`/`poet_cov` 有对角残差，但**是否把残差暴露为特质风险输出**待核。

### F.3 与 lquant 的差异汇总

| 能力 | 开源最佳实践 | lquant | 判定 |
|---|---|---|---|
| 对称正交 | **无开源等价物** | ✅ Schweinler-Wigner | **lquant 领先** |
| 逐步回归正交 | Riskfolio 载荷估计（用于因子模型，非正交化） | ✅ gram_schmidt | lquant 领先 |
| 相关去重 | skfolio `DropHighlyCorrelatedAssets`；gplearn hall-of-fame | ✅ `dedup.py` 贪心聚类 + 簇摘要 | lquant 领先（gplearn 的"搜索内去相关"可补） |
| **正交关系声明化** | skfolio `neutralize_against={"x":["y"]}` | ❌ 数据驱动 | **可借** |
| **RMT 去噪 / 去模态** | skfolio `DenoiseCovariance` / `DetoneCovariance` | ❌ 只有朴素 PCA 截断（lquant 自己已注明局限） | **可借（中）** |
| **EWM 因子协方差（半衰期 90 天）** | 社区 A 股工作流 + skfolio `EWMu`/`EWM` | ❌ | **可借（低中成本、高价值）** |
| 因子风险模型（结构式） | skfolio `CharacteristicsFactorModel` | ✅ 统计式（structured/POET） | **可借"声明式因子图"** |
| 特质风险输出 | 社区工作流 | 部分（残差对角） | 待核 |
| 因子收益率时序 + t 统计 | 社区工作流（截面 OLS） | `evaluate/attribution.py` 待核 | 待核 |
| **能力集排序/时序** | skfolio `HierarchicalSeriation`/`SpectralSeriation` | 无 | 低价值 |

### F.4 已知坑与失败教训

1. **开源 Barra 基本不可用**：`Peimou/barra-risk-model` README 只有 3 行且承认"being developed continuously"；同类仓库普遍无测试、无数据、无文档。**不要把开源 Barra 当依赖。**
2. **因子暴露必须行业+市值中性化**，否则 Size 因子会吸收所有行业信息（社区工作流原文明确列出）。
3. **特质风险需检验正态性假设**（社区工作流原文第 3 条），否则风险模型在厚尾下低估 VaR。
4. **朴素 PCA 截断不改善 Frobenius 误差**：lquant `riskmodel.py` 的模块文档**自己已经写明**这一点（`T ≈ N` 时 Marchenko-Pastur 体把谱撑开，截断高估因子部分），并明确"不假装已经做了"。**这是本项目最诚实、最值得保持的工程态度**。RMT 去噪（skfolio `DenoiseCovariance`）正是解决这一点的方向。
5. **OLS 截面回归的稳健性**：社区工作流用 OLS，但**因子暴露共线时 OLS 系数不稳**——这正是 WLS/稳健回归/正交化的用武之地，而开源 Barra 实现普遍无此处理。

### F.5 移植成本 / 价值 / 是否值得做

| 借鉴项 | 移植成本 | 价值 | 值得做？ |
|---|---|---|---|
| **EWM 因子协方差（半衰期 90 天）** | **低** | **高**（A 股生产惯例） | ✅✅ 优先 |
| **声明式因子正交依赖（`neutralize_against`）** | **低中** | 高（可解释 + 可复现） | ✅✅ 建议 |
| **RMT 去噪 / 去模态协方差** | 中 | 中高（补 lquant 已自认的局限） | ✅ 建议 |
| 因子收益率时序 + t 统计的截面回归固化 | 低中 | 高（归因基础） | ✅ 建议 |
| 行业均值填充缺失暴露 | 低 | 中 | ✅ 建议 |
| ST / 次新股（<60 日）默认过滤 | 低 | 高（A 股必需） | ✅ 需核对是否已有 |
| 特质风险正态性检验 | 低 | 中 | ✅ 可选 |
| 引入开源 Barra 实现 | — | **负** | ❌ 不可行（无成熟项目） |

---

## G. Top 10 可借鉴项（横向排序）

排序依据：**(价值 × 可行性) / 成本**，并优先补齐 lquant 的能力空白而非重复已有。

| # | 借鉴项 | 来源 | 一句话 | 成本 | 价值 | 证据 |
|---|---|---|---|---|---|---|
| **1** | **因子池协同奖励** | AlphaGen | 奖励从"单因子 IC"改为"加入池子后组合的 IC"，并用 `calc_mutual_IC` 显式惩罚与已有因子的相关——直击"挖出 100 个高 IC 但彼此 0.95 相关"的失败模式 | 中 | 极高 | [calculator.py](https://github.com/RL-MLDM/alphagen/blob/master/alphagen/data/calculator.py) |
| **2** | **表达式树前置校验 `validate_parameters`** | AlphaGen | 用 `Maybe[str]` + `or_else` 累积失败，在**求值前**拒绝"无特征操作数/窗口位置传表达式"等非法树，省评估预算 | 低 | 高 | [expression.py](https://github.com/RL-MLDM/alphagen/blob/master/alphagen/data/expression.py) |
| **3** | **OOB 样本外适应度 + parsimony 复杂度惩罚** | gplearn | `max_samples<1` 在子样本上评估、其余给 `best_oob_fitness`；`parsimony_coefficient='auto'` 用 `c=Cov(l,f)/Var(l)` 抗 bloat | 低 | 高 | [API 参考](https://gplearn.readthedocs.io/en/stable/reference.html) |
| **4** | **alphalens 丢样三分账 + `max_loss` 硬阈值** | alphalens | 把丢弃样本拆成"前视对齐丢 / 分箱丢"两笔并打印，超阈值抛 `MaxLossExceededError`，防止局部覆盖因子虚高 | 低 | 高 | [utils.py](https://github.com/quantopian/alphalens/blob/master/alphalens/utils.py) |
| **5** | **CVaR / CDaR 风险度量（LP 形式）** | Riskfolio-Lib | 26 种凸风险度量里 **CVaR/CDaR/MDD 是 LP，开源 solver 可用**；EVaR/RLVaR/峰度需 MOSEK，**别选** | 中 | 高 | [README 求解器表](https://github.com/dcajasn/Riskfolio-Lib) |
| **6** | **EWM 因子协方差（半衰期 90 天）+ ST/次新股过滤** | A 股生产工作流 | 指数加权因子协方差、残差特质风险、申万一级行业、剔除 ST 与上市<60 日——A 股 Barra 的落地惯例 | 低 | 高 | [SKILL.md](https://raw.githubusercontent.com/aifinlab/finclaw/refs/heads/main/skills/a-share-multifactor-model/SKILL.md) |
| **7** | **组合层 `WalkForward` + `CombinatorialPurgedCV`** | skfolio | 组合参数选择用滚动样本外 + purged CV 评估，与 lquant 已有的 `sqrt(2·ln n)` 多重检验门槛互补 | 中 | 高 | [README](https://raw.githubusercontent.com/skfolio/skfolio/main/README.rst) |
| **8** | **声明式因子正交依赖 `neutralize_against` + `exposure_lag=1`** | skfolio | 把"非线性规模对规模正交"这类关系显式声明成图，而非数据驱动；`exposure_lag` 是防前视的显式旋钮 | 低中 | 高 | 同上 |
| **9** | **L1/L2 正则 + 换手/交易成本内生约束** | skfolio / cvxportfolio / PyPortfolioOpt | 用平滑的 L2 惩罚替代硬性 `max_weight`；把换手与成本放进目标而非事后扣减 | 中 | 中高 | [PPO 文档](https://pyportfolioopt.readthedocs.io/en/stable/OtherOptimizers.html) · [cvxportfolio](https://www.cvxportfolio.com/en/1.0.1/policies.html) |
| **10** | **零感知分箱 `zero_aware` + 组内分箱 `binning_by_group`** | alphalens | 中心化因子（反转/情绪）在零点切分多空，组内分箱让分层在行业内可比 | 低 | 中高 | [utils.py](https://github.com/quantopian/alphalens/blob/master/alphalens/utils.py) |

### 补充：三条"应当避免"的清单

| 做法 | 为什么不要 | 证据 |
|---|---|---|
| ❌ **引入 alphalens 作为依赖** | 已归档；默认 `filter_zscore=20` **自带前视偏差**（docstring 自认）；纯 pandas 长表性能差 | [utils.py docstring](https://github.com/quantopian/alphalens/blob/master/alphalens/utils.py) |
| ❌ **照抄 Alpha101/191 Python 参考实现** | `rank()`/`scale()` 用了 pandas 默认 `axis=0`，**实际在时间轴上排名而非截面**；Alpha57/38/27 有转录错误；~20 条依赖未实现的 `IndNeutralize`；`as_matrix()` 已移除 API | [参考实现源码](https://raw.githubusercontent.com/yli188/WorldQuant_alpha101_code/master/101Alpha_code_1.py) · [Quantra 讨论](https://quantra.quantinsti.com/community/t/alpha-57-as-implemented-does-not-match-the-definition-in-the-paper/26690/6) |
| ❌ **依赖开源 Barra 实现** | `Peimou/barra-risk-model` README 仅 3 行、自认"being developed continuously"；同类仓库普遍无测试无文档 | [README](https://raw.githubusercontent.com/Peimou/barra-risk-model/master/README.md) |

---

## H. 全量移植成本/价值汇总

| 项目 | 引入整体？ | 只借口径/设计？ | 主要理由 |
|---|---|---|---|
| alphalens / alphalens-reloaded | ❌ | ✅ | 归档 + 默认前视 + pandas 长表慢；口径仍是最权威参考 |
| Alpha101 Python 实现 | ❌ | ✅（算子清单 + 公式元数据 + bug 黑名单） | 有系统性 bug；大量因子依赖不可得数据 |
| Alpha191 Python 实现 | ❌ | ✅（算子清单 + 公式表） | 共线性极高；核心只有 ~7 条 |
| gplearn | ⚠️（可选） | ✅✅ | 依赖轻、可直接用；`max_samples`/`parsimony` 值得自研复刻 |
| DEAP | ❌ | ✅ | 算法参考（多目标）；全局状态风格不适合当依赖 |
| AlphaGen | ❌ | ✅✅ | Torch+SB3+qlib 太重；但 **pool reward 与前置校验设计价值最高** |
| AutoAlpha / OpenEvolve / RD-Agent | ❌ | ⚠️ 观望 | 细节未核实；LLM 管线与 lquant 现有重叠 |
| PyPortfolioOpt | ⚠️（可选） | ✅✅ | 引入 cvxpy 换来健壮 solver；`max_sharpe` 变换值得自研 |
| Riskfolio-Lib | ❌ | ✅（风险度量清单 + solver 对照表） | 超重依赖 + 全 pandas |
| skfolio | ❌ | ✅✅ | **设计层借鉴价值最高**（CV、声明式约束、特征因子模型） |
| cvxportfolio | ❌ | ✅ | 多期 + 内生成本；细节未核实 |
| empyrical-reloaded | ⚠️（可选） | ✅ | 指标定义参考；口径需统一 |
| quantstats | ❌ | ✅（指标 checklist） | 拉入 seaborn/yfinance；绘图副作用 |
| ffn | — | — | **未核实** |
| barra-risk-model 等 | ❌ | ❌ | 无成熟项目，只借 A 股口径文档 |

---

## 附录 A：本报告的核实状态说明

**已逐字抓取源码/官方文档核对的内容**：
- alphalens `alphalens/utils.py`（全文）、`alphalens/performance.py`（全文）
- gplearn `SymbolicRegressor`/`SymbolicClassifier`/`SymbolicTransformer` 的官方 API 参数与默认值
- AlphaGen `README.md`、`alphagen/data/expression.py`、`alphagen/data/calculator.py`（全文）、仓库文件树
- PyPortfolioOpt `pypfopt/efficient_frontier/efficient_frontier.py`（全文）
- Riskfolio-Lib `README.md`（全文，含 26 风险度量清单与 solver 对照表）
- skfolio `README.rst`（全文，含全部模型清单与代码示例）
- quantstats `README.md`（全文，含函数清单与一个 `help()` 签名）
- `yli188/WorldQuant_alpha101_code/101Alpha_code_1.py`（全文）
- A 股多因子工作流 SKILL.md（全文）、GTJA191 公式表（全文）
- `Peimou/barra-risk-model` README

**明确标注「未核实」的内容**：
- DEAP 的具体 API 签名（`eaSimple`/`selNSGA2`/`cxTwoPoint` 等）—— 仅确认官方文档存在符号回归示例
- Riskfolio-Lib 的**方法签名**（`Portfolio(...)`/`optimization(rm=, obj=, ...)`/`hrp_optimization(linkage=, ...)`）—— 仅确认能力清单
- cvxportfolio 的类名与签名（`MultiPeriodOptimization` 等）
- empyrical / empyrical-reloaded 的函数签名
- ffn 的全部内容
- AutoAlpha / OpenEvolve 的源码细节
- 各项目的精确 star 数 / 最后提交日期（本报告用"活跃/归档/早期"等定性描述，依据是 release 号、README 声明与依赖现状，**未调用 GitHub API 取精确数值**）
- lquant 自身部分模块（`evaluate/*`、`ops/catalog.py`、`preprocess` 细节）的完整能力边界——仅核对了文件名与 `orthogonalize.py`/`dedup.py`/`weighting.py`/`optimizer.py`/`riskmodel.py`/`mining/gp.py`/`mining/fitness.py` 的实现

---

## 附录 B：给 lquant 的三条行动建议（按 ROI 排序）

1. **先做"零成本、高收益"的统计纪律项**（1–2 天量级）：alphalens 的丢样三分账 + `max_loss` 阈值、`zero_aware` 分箱、`group_adjust` 组相对 IC 口径显式化。这些不引入任何依赖，直接提升因子评价的可信度。
2. **再补 GP 搜索机制的三个短板**（1–2 周量级）：把 `fitness.py` 从单因子改成 **pool-based**（借 AlphaGen 的 `calc_pool_IC_ret` + `calc_mutual_IC`），加 **OOB 适应度**（借 gplearn `max_samples`）与 **parsimony 复杂度惩罚**，加 **表达式树前置校验**。保留 lquant 已有的 `sqrt(2·ln n_trials)` 与 decay hinge —— 这个组合目前没有开源项目做过。
3. **组合层按"风险度量 + 模型选择"两条线补**（2–4 周量级）：风险度量先只上 **CVaR / CDaR（LP，开源 solver 可用）**，配 **`efficient_risk` 的可控目标波动率**；模型选择上 **`WalkForward` + 换手/成本约束 + L2 正则**；协方差补 **EWM（半衰期 90 天）+ RMT 去噪/去模态**。**不要碰 EVaR/RLVaR/峰度（需 MOSEK）**。
