# AlphaPurify x lquant 交叉验证

用 [eliasswu/AlphaPurify](https://github.com/eliasswu/AlphaPurify)（MIT，Polars 实现的
因子清洗/回测库）对 lquant 的因子分析模块做**独立第三方交叉验证**。

调研结论与完整结果见 [`docs/research/alphapurify/00-alphapurify-borrow-and-xval.md`](../../../docs/research/alphapurify/00-alphapurify-borrow-and-xval.md)。

## 设计：两个隔离环境，只通过 parquet 交换

```
lquant_side.py       主仓 .venv        （有 lquant，无 alphapurify）
   ├─ read_daily          走 lquant 自己的湖读取器
   ├─ FactorEngine        用 lquant DSL 算因子
   ├─ forward_return/ic_series/ic_summary   lquant 评价模块
   └─ pipeline_run        两个预处理变体
        ↓ panel.parquet / lquant_*.parquet|json
alphapurify_side.py  .venv-alphapurify （有 alphapurify，无 lquant）
   ├─ FactorAnalyzer.run()  rank_ic=True/False 各一次
   └─ AlphaPurifier        两个预处理变体
        ↓ ap_*.parquet|json
compare.py           .venv-alphapurify
   ├─ 三方对账（lquant / AlphaPurify / 独立参考实现）
   └─ 预处理两组对齐比较 + 约定差量化
```

两侧**互不 import**，避免依赖冲突，也让「输入是不是同一份」可验证。

## 用法

```bash
# 一次性建隔离环境
UV_CACHE_DIR=$PWD/.uv-cache uv venv .venv-alphapurify --python 3.12
UV_CACHE_DIR=$PWD/.uv-cache uv pip install --python .venv-alphapurify/bin/python alphapurify

# 一键跑（默认 mom20 / 2023-01-01~2024-12-31 / 800 只 / 输出 .xval-out）
bash scripts/xval/alphapurify/run_xval.sh

# 自定义
bash scripts/xval/alphapurify/run_xval.sh 2023-01-01 2024-12-31 800

# 仓库级核对（clone 上游 + 跑它的自带测试 + API 契约探针）
bash scripts/xval/alphapurify/repo_probe.sh
```

单跑某一侧：

```bash
# lquant 侧（必须在主仓根目录，LQ_DATA_DIR 指向数据湖）
cd /Users/lyp/code/lquant
LQ_DATA_DIR=$PWD/data .venv/bin/python \
  .claude/worktrees/qlibresearch/scripts/xval/alphapurify/lquant_side.py \
  --out .claude/worktrees/qlibresearch/.xval-out \
  --factor-expr 'Ts_Return($close, 20)' --factor-name mom20

# AlphaPurify 侧
.claude/worktrees/qlibresearch/.venv-alphapurify/bin/python \
  .claude/worktrees/qlibresearch/scripts/xval/alphapurify/alphapurify_side.py \
  --out .claude/worktrees/qlibresearch/.xval-out
```

## 实测结论（2026-10-03，alphapurify 1.0.6）

- **IC/RankIC 内核位级一致**：`mom20`（463 天）与 `mom5`（478 天）上，
  lquant / AlphaPurify / 独立参考实现三方逐日 `max|Δ| = 0.0`。
- **预处理内核一致、默认约定不同**：对齐口径后 `max|Δ| ≈ 1e-15`；
  各自出厂默认（MAD n=5×1.4826 vs n=3）`max|Δ| = 3.04`。
- 顺带发现 AlphaPurify 注册表/分发不一致：`neutralize("random_forest")` 报
  `NotImplementedError`，能用的名字是不在注册表里的 `randomforest`。

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
- `lquant_side.py` 需要**真实日线湖**（`data/parquet/daily`）；空湖会直接报错退出。
- `FactorAnalyzer` 用 `max_workers=1` 跑，避免 loky 多进程在沙箱环境下的不确定性；
  需要压测吞吐时再调大。
