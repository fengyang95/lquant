# AlphaPurify x lquant 交叉验证

用 [eliasswu/AlphaPurify](https://github.com/eliasswu/AlphaPurify)（MIT，Polars 实现的
因子清洗/回测库）对 lquant 的因子分析模块做**独立第三方交叉验证**。

调研结论与完整结果见 [`docs/research/alphapurify/00-alphapurify-borrow-and-xval.md`](../../../docs/research/alphapurify/00-alphapurify-borrow-and-xval.md)。

## 设计：两个隔离环境，只通过 parquet 交换

```
lquant_side.py       主仓 .venv        （有 lquant，无 alphapurify）
   ├─ read_daily          走 lquant 自己的湖读取器（数据来自 LQ_DATA_DIR）
   ├─ FactorEngine        用 lquant DSL 算因子（代码来自本 worktree 的 src/）
   ├─ forward_return/ic_series/ic_summary   lquant 评价模块
   └─ pipeline_run        variants.py 的全部预处理变体
        ↓ panel.parquet / lquant_prep.parquet / lquant_ic.parquet / lquant_summary.json
alphapurify_side.py  .venv-alphapurify （有 alphapurify，无 lquant）
   ├─ FactorAnalyzer.run()  rank_ic=True/False 各一次
   └─ AlphaPurifier        同一批变体（位置参数按注册表顺序传）
        ↓ ap_prep.parquet / ap_ic.parquet / ap_rank_ic.parquet / ap_summary.json
compare.py           .venv-alphapurify
   ├─ 三方对账（lquant / AlphaPurify / 独立参考实现）
   └─ 预处理逐变体对齐 + 按 variants.py 声明的 expect 判定 + 退出码
```

两侧**互不 import**，避免依赖冲突，也让「输入是不是同一份」可验证。
`variants.py` 是唯一的例外：它**只有数据**，三个环境都 import 它，正是为了让
「跑了哪些、比了哪些」不可能在三侧之间漂移。

## 用法

**因子清单与方法清单的唯一真源是 [`variants.py`](variants.py)**（纯数据模块，
三个隔离环境都能 import）：加因子/加方法只改那一个文件，三个脚本不用动。

```bash
# 一次性建隔离环境
UV_CACHE_DIR=$PWD/.uv-cache uv venv .venv-alphapurify --python 3.12
UV_CACHE_DIR=$PWD/.uv-cache uv pip install --python .venv-alphapurify/bin/python alphapurify

# 一键跑：variants.py 的全部因子 / 2023-01-01~2024-12-31 / 400 只
#   输出 <worktree>/.xval-out/<factor>/，逐因子分子目录
#   默认 CHECK=1（哨兵）：任何 expect=match 的变体超差即退出码 1
bash scripts/xval/alphapurify/run_xval.sh

# 自定义窗口与股票池
bash scripts/xval/alphapurify/run_xval.sh 2023-01-01 2024-12-31 800

# 只跑一个因子 / 只看数字不判失败
FACTORS="mom20" CHECK=0 bash scripts/xval/alphapurify/run_xval.sh

# 仓库级核对（clone 上游 + 跑它的自带测试 + API 契约探针）
bash scripts/xval/alphapurify/repo_probe.sh
```

单跑某一侧：

```bash
# lquant 侧：数据来自主仓（LQ_DATA_DIR），**被测代码来自本 worktree**
# （脚本自动优先用自己所在 checkout 的 src/，可用 LQ_SRC 覆盖）
cd /Users/lyp/code/lquant
LQ_DATA_DIR=$PWD/data .venv/bin/python \
  .claude/worktrees/qlibresearch/scripts/xval/alphapurify/lquant_side.py \
  --out .claude/worktrees/qlibresearch/.xval-out/mom20 \
  --factor-expr 'Ts_Return($close, 20)' --factor-name mom20

# AlphaPurify 侧
.claude/worktrees/qlibresearch/.venv-alphapurify/bin/python \
  .claude/worktrees/qlibresearch/scripts/xval/alphapurify/alphapurify_side.py \
  --out .claude/worktrees/qlibresearch/.xval-out/mom20 --factor-name mom20
```

## 覆盖范围

| 维度 | 内容 |
|---|---|
| 因子 | `mom20`、`mom5`、`ma20_ratio`（水平类，强自相关）、`std20_ratio`（波动类，右偏重尾） |
| 评价 | 逐日 IC / RankIC 三方对账（lquant / AlphaPurify / 独立 `pl.corr` 参考实现） |
| 预处理 | `winsor_mad_n3`、`winsor_mad_n5_sigma`、`rolling_zscore_w20`、`rolling_robust_w20`、`rolling_minmax_w20`、`volatility_scaling_w20_shift`、`yeo_johnson_l05`、`ewma_l094`、`boxcox_l025` |

`expect` 三种取值：`match`（必须一致）/ `diverge`（已知且已解释的上游口径差）/
`upstream_broken`（上游实现不可用，只验证「它仍然是坏的」）。

## 实测结论（2026-10-04，alphapurify 1.0.6，400 只 × 2023-01-01~2024-12-31）

**IC/RankIC 逐日序列三方位级一致**（`max|Δ| = 0.0`，不是「接近」）：

| 因子 | 面板行数 | 交易日 | IC 天数 | lquant↔AP IC | lquant↔AP RankIC | ↔独立参考 |
|---|---|---|---|---|---|---|
| mom20 | 135,927 | 464 | 463 | 0.0 | 0.0 | 0.0 |
| mom5 | 141,927 | 479 | 478 | 0.0 | 0.0 | 0.0 |
| ma20_ratio | 136,327 | 465 | 464 | 0.0 | 0.0 | 0.0 |
| std20_ratio | 136,327 | 465 | 464 | 0.0 | 0.0 | 0.0 |

**预处理：7 个 `match` 变体全部在容差内**（`max|Δ|`）：

| 变体 | mom20 | mom5 | ma20_ratio | std20_ratio |
|---|---|---|---|---|
| `winsor_mad_n3`（AP 默认口径） | 8.9e-16 | 8.9e-16 | 6.4e-15 | 1.1e-15 |
| `winsor_mad_n5_sigma`（lquant 默认口径） | 1.8e-15 | 1.8e-15 | 3.6e-15 | 1.8e-15 |
| `rolling_zscore_w20` | **0** | **0** | **0** | **0** |
| `rolling_robust_w20` | **0** | **0** | **0** | **0** |
| `volatility_scaling_w20_shift` | **0** | **0** | **0** | **0** |
| `yeo_johnson_l05` | 1.1e-7 | 1.9e-7 | 2.4e-7 | 3.7e-7 |
| `ewma_l094`（**预期分歧**） | 6.8e+6 | 1.1e+7 | 7.0e+6 | 7.0e+6 |
| `boxcox_l025`（**预期分歧**） | 7.0 | 8.1 | 8.7 | 4.3 |
| `rolling_minmax_w20`（**上游不可用**） | — | — | — | — |

读法：

- `winsor_mad_*` 的 1e-15 量级是浮点结合律（两侧都是 Polars，但运算顺序不同）；
- `rolling_*`/`volatility_scaling` **逐位为 0** —— 滚动族口径完全对齐（都是
  pandas/polars 的 `ddof=1` + 满窗口）；
- `yeo_johnson` 的 ~1e-7 来自分母：AP 用 `σ+1e-9`，本仓用 `σ≈0→1.0` 兜底；
- `ewma`/`boxcox` 的巨大差值**不是 bug**，是刻意的：上游这两个实现有前视泄漏，
  本仓修正了（见 `docs/research/00-borrow-and-port-plan.md` 的 Phase 4.4 与
  `src/lquant/factors/preprocess/rolling.py`、`power.py` 的模块 docstring）。

## 上游 bug 看门狗

| 上游实现 | 问题 | 本仓处置 | 哨兵行为 |
|---|---|---|---|
| `rolling_minmax_standardize` | 签名是 `(df, factor_col, trade_date, symbol_col)`，同族其它函数都是 `(df, trade_date, symbol_col, factor_col)`，而 `AlphaPurifier.standardize` 按后者统一传参 → 它对 **`trade_date` 列**做滚动 Min-Max 并写回（该列变成 float/null），真正的因子列原封不动 | 原生实现（语义正确） | `expect=upstream_broken`：验证「它仍然坏」；哪天修好了会提醒改回 `match` |
| `EWMA_standardize` | 把 `(1−λ)λ^k` 权重挂在**绝对时间下标**上再反向累加，等价于 `σ²_t = Σ_{u≥t}(1−λ)λ^u x²_u` —— 用到 t 之后的数据（前视泄漏），且权重随绝对下标而非距离衰减 | 递归 `ewm_mean(adjust=False)` | `expect=diverge`：验证「它仍然分歧」 |
| `boxcox_standardize` | 用**全样本**（含未来日期）最小值做平移，因子值随新数据整体漂移 | 当日截面最小值平移 | 同上 |
| `neutralize("random_forest")` | 注册表/分发不一致：能用的名字是 `randomforest`，不在注册表里 | 不借鉴其契约 | `repo_probe.sh` |

## 自己家的 bug：xval harness 的自比较（2026-10-04 修）

`compare.py::_diff` 原来写成 `a.select([...col]).join(b.select([...col]))`，
两侧列名相同时右列被加 `_right` 后缀，而 `j[col_a]`/`j[col_b]` **都解析到左列**
→ `left - left ≡ 0`，**所有同名比较都「永远通过」**。发现方式是给新变体两侧用同名
列后，17 个变体齐刷刷 0.000e+00。现在先 alias 成 `__left`/`__right` 再比。

教训与上游那几条同源：**对拍工具本身必须先被怀疑**。所以现在 `expect=diverge`
的变体要求「确实仍然分歧」，`upstream_broken` 要求「确实仍然坏」 —— 让「两边一样」
这件事有反证，而不是默认成立。

### 仓库级核对（`repo_probe.sh`）

- **上游 main == PyPI 1.0.6**（仅 2 处非功能性差异），wheel 结论对 main 成立。
- **自带测试 2/3 失败**：`test_Exposures.py` 是测试自身 fixture 名冲突（永远不可能过）；
  `test_FctorAnalyzer.py` 断言了 `create_single_fac_full_sheet()` 的返回值，而该方法
  **没有 return 语句**。
- **版本元数据三处不一致**：pyproject 1.0.6 / setup.py 1.0.5 / setup.ini 0.1.0。
- **examples 文档的 keyword 写法直接 `TypeError`**：链式方法签名是
  `(self, method, *args)`，不接受关键字参数；参数必须按注册表顺序位置传入。

→ 结论：**算法与口径可信（已位级验证），但 API 契约与测试/文档不能当规格用。**

## 注意

- `.xval-out*/` 与 `.venv-alphapurify/` 已在 `.gitignore` 中；产物不入库。
- 输出布局：`<worktree>/.xval-out/<factor>/`（逐因子分子目录）；旧的单目录布局
  （`.xval-out/*.parquet`，列名 `lq_default`/`ap_default_aligned`）已废弃。
- `lquant_side.py` 需要**真实日线湖**（`data/parquet/daily`，由 `LQ_DATA_DIR` 指定）；
  空湖会直接报错退出。**数据来自主仓、被测代码来自本 worktree**，别把两者搞混。
- `FactorAnalyzer` 用 `max_workers=1` 跑，避免 loky 多进程在沙箱环境下的不确定性；
  需要压测吞吐时再调大。
