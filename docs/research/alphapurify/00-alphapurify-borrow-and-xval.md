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

同一病根还有第二处表现：`get_methods()` 列出的**参数名**（`neutralizer_cols`、`degree` 等）
根本无法作为关键字传入，因为链式方法签名是 `(self, method, *args)`——详见 §1.6④。

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

### 1.6 仓库级核对（clone 上游 main + 官方 examples/tests）

为区分「PyPI wheel」与「仓库真源」，`git clone --depth 1` 了上游仓库逐文件对照：

**① 上游 main == PyPI 1.0.6**（全部 7 个源文件，仅 2 处非功能性差异：一行版权注释、一个空行）。
→ 本报告基于 wheel 的所有结论对上游 main 同样成立，不存在「main 更新」的问题。

**② 仓库自带测试 2/3 失败**（实测 `pytest tests`，pytest 9.1.1）：

| 测试 | 结果 | 原因 |
|---|---|---|
| `tests/test_AlphaPurifier.py::test` | ✅ PASSED | — |
| `tests/test_Exposures.py::test_sheets` | ❌ FAILED | **测试自身写错**：模块内 `@pytest.fixture def df()` 与测试里的 `df()` 调用**同名冲突**，pytest 注入的是 DataFrame → `TypeError: 'DataFrame' object is not callable`。**该测试永远不可能通过。** |
| `tests/test_FctorAnalyzer.py::test_sheets` | ❌ FAILED | **API 与测试期望不符**：`create_single_fac_full_sheet(return_fig=True)` 方法体只调用四个 sheet 方法、**没有 return 语句**（源码核对），返回 `None` → `assert not res.empty` 必然失败。 |

附带：文件名拼写错误 `test_FctorAnalyzer.py`（Fctor）。

**③ 版本元数据三处不一致**：`pyproject.toml` = **1.0.6**，`setup.py` = **1.0.5**，`setup.ini` = **0.1.0**。
（wheel 里 `__version__ = "1.0.6"`，以 pyproject 为准。）

**④ 官方 examples 承诺的 keyword API 实际不可用**（实测）：

```python
# examples/AlphaPurifier.py 文档（L137-147）与 get_methods 都这样写：
AP.neutralize('multiOLS', neutralizer_cols=['ret_20','beta_60'], dummy_cols=['industry'])
# 实测：TypeError: AlphaPurifier.neutralize() got an unexpected keyword argument 'neutralizer_cols'

AP.winsorize('mad', n=3)     # TypeError: unexpected keyword argument 'n'
AP.winsorize('mad', 3)       # OK —— 必须按注册表顺序位置传参
```

三个链式方法签名都是 `(self, method, *args)`，**不接受 `**kwargs`**；而
`get_methods("neutralize","polynomial")` 却会列出 `neutralizer_cols` / `dummy_cols` /
`degree` / `interaction_only` / `include_bias` 等参数名。即
**自省暴露的参数名无法用于调用**——与 §0.3 的 `random_forest` 属于同一类病根：
**注册表/文档与分发实现不是单一真源**。

**⑤ Exposures 的设计语义（这部分值得借鉴）**：

- `PortfolioExposures`（分位组合 + `position="ls"`）：回答「**我的组合表现得像什么**」；
- `PureExposures`（因子加权组合 `w_i = f_i / Σ|f_i|`）：回答「**我的信号本身载荷在什么上**」；
- 两者合起来才是「alpha vs risk exposure」的完整分解——单看组合暴露会把
  「信号本身的暴露」与「组合构建引入的暴露」混在一起。

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

产物：`.xval-out/<factor>/{panel,lquant_ic,lquant_prep,ap_ic,ap_rank_ic,ap_prep,reference_ic}.parquet`
+ `xval_report.json` + `lquant_summary.json` + `ap_summary.json`。

### 3.5 扩展复跑（2026-10-04，Phase 4.4/4.6）：4 因子 × 9 变体 + 上游 bug 看门狗

方法与预处理清单改为单一真源 `scripts/xval/alphapurify/variants.py`，
`expect` 三态判定：`match` 必须一致、`diverge` 必须**仍然分歧**、
`upstream_broken` 必须**仍然坏**。规模：400 只 × 2023-01-01~2024-12-31。

**IC/RankIC**：4 个因子（mom20/mom5/ma20_ratio/std20_ratio）的逐日 IC 与 RankIC
三方（lquant / AlphaPurify / 独立 `pl.corr` 参考）`max|Δ| = 0.0`。

**预处理**（`max|Δ|`，四因子）：

| 变体 | mom20 | mom5 | ma20_ratio | std20_ratio |
|---|---|---|---|---|
| `winsor_mad_n3`（AP 默认口径） | 8.9e-16 | 8.9e-16 | 6.4e-15 | 1.1e-15 |
| `winsor_mad_n5_sigma`（lquant 默认口径） | 1.8e-15 | 1.8e-15 | 3.6e-15 | 1.8e-15 |
| `rolling_zscore_w20` | **0** | **0** | **0** | **0** |
| `rolling_robust_w20` | **0** | **0** | **0** | **0** |
| `volatility_scaling_w20_shift` | **0** | **0** | **0** | **0** |
| `yeo_johnson_l05` | 1.1e-7 | 1.9e-7 | 2.4e-7 | 3.7e-7 |
| `ewma_l094`（预期分歧） | 6.8e+6 | 1.1e+7 | 7.0e+6 | 7.0e+6 |
| `boxcox_l025`（预期分歧） | 7.0 | 8.1 | 8.7 | 4.3 |
| `rolling_minmax_w20` | 上游不可用 | 同左 | 同左 | 同左 |

→ 新增的滚动族标准化与 AP **逐位一致**；`yeo_johnson` 的 ~1e-7 只来自分母
（AP `σ+1e-9` vs 本仓 `σ≈0→1.0`）；`ewma`/`boxcox` 的大差值是本仓**修正上游
前视泄漏**的直接后果（见 `docs/research/00-borrow-and-port-plan.md` §1.7.2）。

**新发现的上游缺陷（3 个，均已原生绕开并留看门狗）**

1. `rolling_minmax_standardize` 的签名参数顺序与同族其它函数相反，而分发按统一
   顺序传参 → 它对 **`trade_date` 列**做滚动 Min-Max 并写回（dtype 变 `Float64`、
   值为 null），真正的因子列原封不动 —— 该方法**不可用**。
2. `EWMA_standardize` 把权重挂在**绝对时间下标**上再反向累加，等价于
   `σ²_t = Σ_{u≥t}(1−λ)λ^u x²_u`，**用到 t 之后的数据**（前视泄漏）。
3. `boxcox_standardize` 用**全样本**（含未来日期）最小值做平移，因子值随新数据
   整体漂移（同样是前视）。

**顺带修掉自己的 bug（教训比结论更值钱）**：`compare.py::_diff` 原来在两侧列名
相同时，join 后两侧取值表达式**都解析到左列** ⇒ `left-left ≡ 0`，任何同名比较都
「永远通过」。发现方式是给新变体两侧用同名列后 17 个变体齐刷刷 `0.000e+00`。
现在先 alias 成 `__left`/`__right` 再比，并让 `diverge`/`upstream_broken` 两类
判定去**要求分歧与缺陷仍然存在** —— 让「两边一样」有反证，而不是默认成立。
（§3.2 的 IC 结论未受此 bug 影响：两侧各自的汇总统计逐项相同，
且修正 harness 后复跑仍为 `max|Δ| = 0.0`。）

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
   借 `PureExposures` / `PortfolioExposures` 的**双视角**设计（`_cross_section_ols`）：
   - `PortfolioExposures` → 「组合表现得像什么」（分位组合的暴露归因）；
   - `PureExposures` → 「信号本身载荷在什么上」（因子加权组合 `w_i = f_i / Σ|f_i|`）。

   lquant 现在有分组归因（`attribution.py`）与风格相关（`style_corr.py`），但缺这个分解视角；
   尤其**「纯暴露」这一支**能回答「alpha 是不是只是换了皮的 beta/行业暴露」。

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
| 把上游 tests / examples 当 API 规格 | 实测自带测试 2/3 失败、examples 的 keyword 写法直接 `TypeError`（§1.6）；规格以「自己跑通的调用」为准 |

---

## 5. 对 lquant 的净结论

1. **因子分析内核可以放心**：IC/RankIC/IR/t 与独立第三方实现逐日位级一致（2 因子 × 463/478 天 × 26–28 万行，`max|Δ|=0`）。
2. **真正要补的是「约定」而非「算法」**：MAD 的 1.4826 与默认 n、零方差语义、`min_obs`、`-1` 收益处理，这四项应写进文档与自省输出。
3. **值得抄的是「产品化的三件套」**：截面快照 `trace()`、因子收益的纯暴露/纯收益分解、多 horizon 并行 IC。
4. **注册表设计上 lquant 反而更优**：单一真源避免了 AlphaPurify 的 `random_forest` 不一致。
5. **继续隔离**：AlphaPurify 作为「交叉验证器」而非运行时依赖——本次 harness 已经把这条路径固化成可重复的一键脚本。
6. **工程成熟度要打折，但算法可信**：上游 main == 1.0.6，自带测试 2/3 失败、版本元数据三处不一致、
   文档承诺的 keyword API 直接 `TypeError`（§1.6）。所以正确的用法是：
   **借算法语义与实现口径，不借 API 契约，也不把它的测试/文档当规格**——
   这也正是本次交叉验证要用「独立参考实现」做第三方的价值所在。
