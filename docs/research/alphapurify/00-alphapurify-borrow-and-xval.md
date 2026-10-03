# AlphaPurify 调研 + 与 lquant 因子分析模块交叉验证

- 调研日期：2026-10-03
- 对象：[eliasswu/AlphaPurify](https://github.com/eliasswu/AlphaPurify)，本地实装 **alphapurify 1.0.6**（PyPI）
- 隔离环境：`.venv-alphapurify`（Python 3.12 / polars 1.44.2 / pandas 3.0.6 / scikit-learn 1.9.1）
- 许可证：**MIT**（`alphapurify-1.0.6.dist-info/licenses/LICENSE.txt`，Copyright (c) Elias Wu）
- 交叉验证脚本：`scripts/xval/alphapurify/`（`lquant_side.py` / `alphapurify_side.py` / `compare.py` / `run_xval.sh`）
- 一句话：**lquant 的因子评价内核（IC/RankIC/IR/t）与 AlphaPurify 位级一致；两者真正不同的是「预处理默认约定」，而这一点 lquant 需要显式说明。**

---

## 0. 结论摘要

### 0.1 交叉验证结论（本次最有价值的产出）

在**真实 A 股日线湖**上、用 lquant 的 DSL 算因子、三方对账（lquant / AlphaPurify / 独立参考实现）：

| 因子 | 窗口 | 股票数 | 行数 | IC 天数 | IC 均值（lquant） | IC 均值（AlphaPurify） | 逐日 max\|Δ\| |
|---|---|---|---|---|---|---|---|
| `mom20` = `Ts_Return($close,20)` | 2023-01-01~2024-12-31 | 800 | 265,257 | 463 | **0.009278548704917438** | **0.009278548704917438** | **0.0** |
| `mom5` = `Ts_Return($close,5)` | 同上 | 800 | 277,257 | 478 | **0.03127767811926659** | **0.03127767811926659** | **0.0** |

不只是均值相同——**逐日 IC 与 RankIC 序列的 `max|Δ| = 0.000e+00`**，且 lquant、AlphaPurify、独立参考实现三者互相都是 0。这意味着：

- lquant 的 IC/RankIC **计算内核正确**（与第三方实现逐日位级一致）；
- lquant 的 `t_stat` 公式 = 单样本 t 检验，与 `scipy.stats.ttest_1samp` **完全一致**（AP 用 scipy，数值位级相同）；
- lquant 的 IR = mean/std 与 AP 一致。

lquant 在此之上还**多给**了 AlphaPurify 没有的：Newey-West t 值、年化 IR、IC 正比例、`|IC|>0.02` 占比、IC 自相关（AP 只有 horizon autocorr）。

### 0.2 预处理：内核一致，但**默认约定不同**（必须写进文档）

MAD 去极值在两边**同名不同义**：

| 实现 | 公式 | 默认 n | 等效 σ 倍数 |
|---|---|---|---|
| lquant `winsorize(mad)` | `med ± n × 1.4826 × MAD` | **5.0** | 7.413 |
| AlphaPurify `winsorize("mad")` | `med ± n × MAD` | **3.0** | 3.0 |

实测（265,257 行）：

- 对齐口径后（lquant n=5 ↔ AP n=7.413，lquant n=2.0235 ↔ AP n=3）：`max|Δ| ≈ 1.3e-15` → **内核一致**（浮点误差级）。
- 各自出厂默认直接比：`max|Δ| = 3.04`、`mean|Δ| = 0.189` → **标准化后差 0.19 个标准差**，不是噪声，是真实口径差。

**结论**：内核没问题，风险在「同名不同义」。建议在 lquant 的预处理文档/UI 里显式写出 `n` 是「× 1.4826 × MAD」的口径，避免与 AlphaPurify/Alphalens 系使用者对不上。

### 0.3 顺带发现 AlphaPurify 的一个真 bug

`METHOD_REGISTRY["neutralize"]` 里注册的是 **`random_forest`**，但 `AlphaPurifier.neutralize()` 的分发分支写的是 **`randomforest`**。后果：

```python
AlphaPurifier.get_methods("neutralize")   # 列出 random_forest
p.neutralize("random_forest", [...])      # NotImplementedError: random_forest not found
p.neutralize("randomforest", [...])       # OK —— 但这个名字不在注册表里
```

即 **`get_methods()` 自省结果与可调用名不一致**。只有这一个键有此问题（winsorize 12/12、standardize 15/15 都对得上）。

反过来看：**lquant 的注册表设计在这点上更稳**——`METHODS` 同时是分发来源和自省来源（单一真源），不可能出现「列得出、调不到」。这条是「反向借鉴」：不要学 AlphaPurify 把注册表和 if/elif 分发分开写。

---

## 1. AlphaPurify 能力全景（1.0.6 源码核对）

### 1.1 模块构成

| 文件 | 行数 | 职责 |
|---|---|---|
| `AlphaPurifier.py` | 220 | 链式预处理 + 二维 `METHOD_REGISTRY` + `get_methods()` 自省 |
| `APr_utils.py` | 3726 | 全部预处理算子内核 |
| `FactorAnalyzer.py` | 4157 | IC/分层回测/换手/成本/归因/trace + Plotly 报告 |
| `Exposures.py` | 1426 | `PureExposures` / `PortfolioExposures`：截面 OLS 因子收益归因 + 相关性 |
| `BackTest.py` | 662 | `FactorBacktest`：逐笔 buy/sell/rebalance 的因子组合回测 |
| `Database.py` | 584 | `DataBase`：按 symbol 的 parquet 因子数据集并行读写/合并 |

> 仓库自带设计文档 `docs/量化平台设计文档.html` §3.3 写的是「alphapurify/ 只有 4 个文件」——那是早期版本；1.0.6 已扩到 7 个文件、约 1.08 万行，多了 `BackTest.py` 与 `APr_utils.py`。

### 1.2 预处理方法（42 个，3 类）

| 类别 | 数量 | 方法 |
|---|---|---|
| `winsorize` | 12 | mad、mean_std、volatility、iqr、quantile、rolling_quantile、boxcox_compress、zscore、rankgauss、tanh、huber、ransac |
| `neutralize` | 15 | multiOLS、lasso、ridge、elasticnet、polynomial、kernelridge、huber、rank、theilsen、random_forest、GBDT、ICA、PCA、bayesianridge、partialcorrelation |
| `standardize` | 15 | zscore、robust_zscore、minmax、rank、rank_gaussianize、rolling、rolling_robust、rolling_minmax、volatility_scaling、EWMA、normal_scores、quantile_binning、log_zscore、boxcox、yeo_johnson |

对照 lquant 现有 **18 个**（winsorize 5 / standardize 4 / neutralize 5 / orthogonalize 4）：lquant 取的是「实战高频子集」，方向正确；缺的主要是**稳健/非线性类**与**时序滚动类**。

### 1.3 FactorAnalyzer 能力

- **IC / RankIC**：逐日截面 `pl.corr`，多 horizon（默认 1/5/10）并行；`rank_ic` 开关决定出 RankIC 还是 IC（**一次只出一种**）。
- **分层回测**：多调仓周期（W/M/Q）、多头/空头/多空、分箱数可配、**动态权重漂移**、换手率。
- **成本内建**：`fee_rate` / `slippage_rate` / `tax_rate` / `tax_rate_direction` / `base_rate`。
- **`overnight`** 开关：把隔夜收益单独切出来（`on`/`off`/`only`）——少见且实用的细节。
- **前瞻偏差防护**：`fac_shift` 把信号整体后移，严格对齐可执行时点。
- **行业归因**：`group_by` + 行业 IC 按股票数加权贡献。
- **`trace(period, date, bins, position)`**：任意日期/方向/分箱的**截面快照**，看权重与收益明细——白盒排查利器。
- **工程**：joblib 多进程 + `pyarrow.memory_map` 零拷贝，宣称 400 万行 25 秒。

### 1.4 Exposures（因子收益归因）

- `PureExposures`：把目标因子对其它暴露做**截面 OLS**，分离「纯暴露」与「纯收益」，并给相关性矩阵。
- `PortfolioExposures`：对组合层做同样的暴露/收益分解。

### 1.5 依赖与工程

- 依赖：pandas、polars、duckdb、plotly、numpy、scipy、pyarrow、joblib、scikit-learn、tqdm（较重）。
- 全链路以 **pandas 作为对外 DataFrame 契约**（`AlphaPurifier.__init__` 收 pandas/polars，`to_result()` 出 pandas；`ics_dict` 也是 pandas）。
- 输出交互报告依赖 Plotly。

---

## 2. lquant vs AlphaPurify 能力对照

| 能力 | AlphaPurify | lquant | 判定 |
|---|---|---|---|
| IC / RankIC / IR / t | ✅ | ✅ **位级一致** | 已对齐，无需引入 |
| 评价扩展（NW-t、年化 IR、IC 自相关、正比例、衰减、评级、稳健性） | 部分 | ✅ 更全 | lquant 领先 |
| 预处理方法数 | 42 | 18 | 可选择性补（见 §4） |
| 预处理注册表自省 | ✅（但有 `random_forest` bug） | ✅ 单一真源，更稳 | lquant 设计更好 |
| 分层多空回测 | ✅ 含成本/滑点/税/换手 | ✅ 事件引擎 + 向量化快扫 | lquant 更强（A 股规则） |
| 因子收益归因 | ✅ 截面 OLS 纯暴露/纯收益 | 分组归因 + 风格相关 | **可借鉴**（见 §4） |
| 截面快照 `trace()` | ✅ | ❌ | **可借鉴** |
| 隔夜收益切分 `overnight` | ✅ | ❌ | 可选 |
| 多 horizon 并行 IC | ✅ | 循环 | 可选 |
| 数据接入 | `DataBase`（按 symbol parquet） | Parquet 湖 + DuckDB + 9 provider | lquant 领先 |
| 计算引擎 | Polars（对外 pandas） | 全 Polars + Rust | lquant 领先 |
| 报告 | Plotly 交互 | 自包含 HTML | 各自取舍 |

---

## 3. 交叉验证方法与结果

### 3.1 方法：两个隔离环境 + 三方对账

```
主仓 data/parquet/daily（真实 A 股日线）
        │
        ├─ [环境 A] 主 venv（有 lquant，无 alphapurify）
        │    read_daily → FactorEngine 算因子 → forward_return → ic_series / ic_summary
        │    pipeline_run（两个预处理变体）           → panel / lquant_ic / lquant_prep
        │
        ├─ [环境 B] .venv-alphapurify（有 alphapurify，无 lquant）
        │    读同一个 panel → FactorAnalyzer.run()（rank_ic=True/False 各一次）
        │    AlphaPurifier.winsorize+standardize      → ap_ic / ap_rank_ic / ap_prep
        │
        └─ [环境 B] compare.py：三方对账 + 独立参考实现（直接用 pl.corr 在 panel 上重算）
```

设计要点：

1. **两侧只通过 parquet 交换数据**，谁都不 import 谁——避免依赖冲突，也让「是不是同一份输入」可验证。
2. **独立参考实现**（第三方）用于在 A/B 不一致时判定谁偏离了数学定义。
3. 预处理给出**两个对齐变体**，把「内核差异」与「约定差异」分离：对齐口径一致 ⇒ 内核一致；出厂默认不一致 ⇒ 纯约定差。

### 3.2 结果

**IC / RankIC（逐日序列，三方）**

| 比较 | mom20（463 天） | mom5（478 天） |
|---|---|---|
| lquant vs 参考实现（IC / RankIC） | max\|Δ\|=0 / 0 | max\|Δ\|=0 / 0 |
| AlphaPurify vs 参考实现（IC / RankIC） | max\|Δ\|=0 / 0 | max\|Δ\|=0 / 0 |
| **lquant vs AlphaPurify（IC / RankIC）** | **max\|Δ\|=0 / 0** | **max\|Δ\|=0 / 0** |

**汇总统计（mom20，lquant 与 AlphaPurify 逐项对照）**

| 统计量 | lquant | AlphaPurify | 一致 |
|---|---|---|---|
| IC mean | 0.009278548704917438 | 0.009278548704917438 | ✅ |
| IC std | 0.18868786153540537 | 0.18868786153540537 | ✅ |
| IC IR | 0.04917406254655344 | 0.04917406254655344 | ✅ |
| IC t | 1.0580996842712307 | 1.0580996842712307 | ✅ |
| RankIC mean | -0.02407193926992184 | -0.02407193926992184 | ✅ |
| RankIC std | 0.19956904621017543 | 0.19956904621017543 | ✅ |
| RankIC IR | -0.12061960372637429 | -0.12061960372637429 | ✅ |
| RankIC t | -2.5954244577407377 | -2.595424457740737 | ✅ |
| 天数 | 463 | 463 | ✅ |

**预处理**

| 比较 | 结果 | 含义 |
|---|---|---|
| lquant 默认(n=5) vs AP(n=7.413) | max\|Δ\|=1.78e-15 | 内核一致 |
| lquant(n=2.0235) vs AP 默认(n=3) | max\|Δ\|=1.33e-15 | 内核一致（反向确认） |
| lquant 默认(n=5) vs AP 默认(n=3) | max\|Δ\|=3.04，mean\|Δ\|=0.189 | **约定差**（真实差异） |

### 3.3 差异归因（哪些是「约定」，哪些是「内核」）

| 差异点 | 性质 | 说明 |
|---|---|---|
| MAD 的 1.4826 一致性修正 | **约定** | lquant 有，AP 没有；默认 n 也不同（5 vs 3） |
| 零方差截面处理 | **约定** | lquant `_safe_std` 兜底为 1.0 → 返回 0；AP 直接返回 null |
| 前瞻收益 `fut_ret == -1` | **约定** | AP 把 -100% 收益当缺失丢弃；lquant 保留（极端退市样本） |
| `min_obs` 门槛 | **约定** | lquant 默认 `min_obs=5`；AP 无门槛（靠 `drop_nans` 兜） |
| IC/RankIC 数学定义 | **内核** | 逐日 `pl.corr` pearson/spearman —— **完全一致** |
| IR / t 公式 | **内核** | mean/std 与单样本 t —— **完全一致** |
| 收益对齐（`shift(-h)`） | **内核** | 两侧都是 `price.shift(-h)/price - 1` —— **完全一致** |

**结论**：内核无需改动；需要补的是**约定文档化**（MAD 口径、零方差语义、min_obs、-1 收益处理）。

### 3.4 复现

```bash
# 建隔离环境（一次）
cd <worktree>
UV_CACHE_DIR=$PWD/.uv-cache uv venv .venv-alphapurify --python 3.12
UV_CACHE_DIR=$PWD/.uv-cache uv pip install --python .venv-alphapurify/bin/python alphapurify

# 一键交叉验证（默认 mom20 / 2023-2024 / 800 只）
bash scripts/xval/alphapurify/run_xval.sh
# 换因子：
bash scripts/xval/alphapurify/run_xval.sh 2023-01-01 2024-12-31 800
```

产物：`.xval-out/{panel,lquant_ic,lquant_prep,ap_ic,ap_rank_ic,ap_prep,reference_ic}.parquet`
+ `xval_report.json` + `lquant_summary.json` + `ap_summary.json`。

---

## 4. 可借鉴清单（按优先级）

### P0 —— 建议直接补进 lquant

1. **MAD 口径文档化 + 显式参数**（成本：极低）
   在预处理文档、`describe()` 输出和前端 UI 上写明 `n` 是「× 1.4826 × MAD」。当前 `params={"n": 5.0}` 不解释这一点，与 AlphaPurify/Alphalens 系使用者对不上，是**静默的口径分歧**。

2. **零方差/退化截面的显式语义**（成本：低）
   lquant 现在静默返回 0（`_safe_std` 兜底），AP 返回 null。两者都不算错，但应在 `describe()` 里写清楚，避免「因子全市场同值」时被当成有效 0。

3. **稳健去极值补充：`huber` / `rankgauss`**（成本：低）
   A 股重尾 + 一字板场景下，Huber 回归清洗与 RankGauss（排名高斯化）比 MAD 更稳。AP 两个都有现成实现可对照口径。

### P1 —— 有明确收益，建议排期

4. **因子收益归因：截面 OLS「纯暴露 / 纯收益」分解**（成本：中）
   借 `PureExposures.calc_stats` 的设计（`_cross_section_ols`）：把目标因子对行业/市值/风格暴露回归，分离「因子自身收益」与「暴露带来的收益」。lquant 现在有分组归因（`attribution.py`）与风格相关（`style_corr.py`），但缺这个分解视角。

5. **截面快照 `trace()`**（成本：中）
   给定「日期 + 方向 + 分箱」，回看当期持仓权重与收益明细。这是排查「某天净值跳变到底是哪几只票」的最快路径，lquant 的 HTML 报告目前做不到这一粒度。

6. **多 horizon IC 的并行化**（成本：低）
   AP 对 (1,5,10) 多 horizon 并行；lquant 是循环。lquant 已有 `forward_return_matrix`，并行化收益直接。

7. **`overnight` 收益切分**（成本：低）
   把隔夜与日内收益分开统计，对判断「信号是不是靠跳空赚钱」很有用。

### P2 —— 可选

8. **预处理方法扩容**：`boxcox`/`yeo_johnson`（幂变换）、`rolling_*`（时序滚动标准化）、`EWMA`/`volatility_scaling`（波动率缩放）、`normal_scores`、`quantile_binning`。lquant 现在只有截面类，缺时序滚动类。
9. **`Database` 的按 symbol 因子存储**：lquant 已有 `write_factor`，可对照其并行读写与合并策略。

### 不借鉴

| 项 | 理由 |
|---|---|
| 把 AlphaPurify 引入主 venv | 依赖重（plotly/pandas/sklearn/scipy/duckdb）；与 ADR-11 的隔离思路一致，继续用隔离 venv 做交叉验证 |
| Plotly 交互报告 | lquant 已有自包含 HTML（零外部依赖），换 Plotly 是净增依赖 |
| 以 pandas 作为内部 DataFrame 契约 | lquant 全 Polars + Rust，回退 pandas 是倒退 |
| 把注册表与 if/elif 分发分开写 | 会重演 `random_forest` 这类「列得出、调不到」的 bug；lquant 的单一真源更好 |

---

## 5. 对 lquant 的净结论

1. **因子分析内核可以放心**：IC/RankIC/IR/t 与独立第三方实现逐日位级一致（2 因子 × 463/478 天 × 26–28 万行，`max|Δ|=0`）。
2. **真正要补的是「约定」而非「算法」**：MAD 的 1.4826 与默认 n、零方差语义、`min_obs`、`-1` 收益处理，这四项应写进文档与自省输出。
3. **值得抄的是「产品化的三件套」**：截面快照 `trace()`、因子收益的纯暴露/纯收益分解、多 horizon 并行 IC。
4. **注册表设计上 lquant 反而更优**：单一真源避免了 AlphaPurify 的 `random_forest` 不一致。
5. **继续隔离**：AlphaPurify 作为「交叉验证器」而非运行时依赖——本次 harness 已经把这条路径固化成可重复的一键脚本。
