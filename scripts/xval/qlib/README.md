# qlib 基准口径交叉验证（Phase 1.3）

把 lquant 的**真实基准**（沪深300 = `000300.SH` / qlib `SH000300`）导出到 qlib，
并逐日对拍「原生 `index_daily`」与「qlib 二进制 bin」是不是同一条序列。

## 为什么需要它

Phase 1.3 之前，qlib 工作流的 `benchmark` 是 `SH600000`（招商银行）——
拿一只个股当大盘，算出来的「超额收益」没有解释力。换成真实指数后，
必须回答：两侧算超额收益用的基准序列是否一致？只要基准一致，剩下的差异
就纯粹来自组合构建与交易成本，而不是基准口径漂移。

## 用法

```bash
# 一键：导出 + 对拍（只需主 venv，不需要 pyqlib）
bash scripts/xval/qlib/run_xval.sh [REPO_ROOT] [QLIB_DIR] [START] [END]
# 默认 REPO_ROOT=/Users/lyp/code/lquant（读真实湖与真实 index_daily）
#      QLIB_DIR=<worktree>/data/qlib-xval
#      START=2026-01-01 END=2026-09-30

# 只跑对拍（已有导出物）
LQ_DUCKDB_PATH=$REPO_ROOT/data/duckdb/lquant.duckdb \
  python scripts/xval/qlib/benchmark_parity.py \
    --qlib-dir data/qlib-xval --symbol 000300.SH

# 真跑 qlib 工作流（需要 .venv-qlib）
$REPO_ROOT/.venv-qlib/bin/python src/lquant/qlib_io/runner.py \
    --provider data/qlib-xval \
    --config scripts/xval/qlib/workflow_xval_smoke.yaml \
    --market stocks60 --out data/qlib-xval/last_metrics.json
```

## 判据

`benchmark_parity.py` 检查三项：

| 项 | 口径 | 合格线 |
|---|---|---|
| 交易日集合 | 两侧共同交易日 | 差异只允许是「导出窗口之外」的日期 |
| 收盘点位 | **相对**误差 | ≤ `1e-4`（bin 是 **float32**，指数点位 ~4500 时绝对误差必然在 1e-3 量级） |
| 日收益 / 区间总收益 | **绝对**误差 | ≤ `1e-6`（收益率是小数，float32 的有效位数足够） |

注意点位用相对误差是**必须**的：qlib bin 是 float32，要求绝对相等是错的
（会永远失败）。这一点在首次对拍时就暴露出来了。

## 实测结果（2026-10-04，主仓真实湖）

```
日历 182 天 / 股票 7228 只 / 基准 SH000300
共同交易日 21（2026-09-02 ~ 2026-09-30）
close max|Δ| = 2.344e-04（相对 5.56e-08）
ret   max|Δ| = 7.438e-08
基准区间总收益：qlib +8.5244172%  vs  原生 +8.5244095%（差 7.7e-08）
→ 纯 float32 舍入，两侧基准是**同一条序列**
```

同期真跑 qlib 工作流（`workflow_xval_smoke.yaml`，60 只股票，Ridge 冒烟）：
`excess_return_with_cost.mean = -0.00584`、`ffr = 1.0` —— 基准被真正找到并使用
（`ffr=1.0` 表示基准序列完整覆盖回测窗口）。策略数值本身在 20 天 / 60 只 /
线性模型的条件下没有统计意义，这里只验证**链路**。

原生引擎侧的对应实现对拍在 `tests/unit/test_backtest_benchmark.py`：
`1 + 超额 = (1 + 组合总收益) / (1 + 基准区间收益)` 的定义式被断言。

## 作为定期哨兵

```bash
bash scripts/xval/sentinel.sh        # 或 make xval-sentinel
```

`sentinel.sh` 会把这一套（导出 → 基准对拍 → 真跑 workflow）与 AlphaPurify 侧
（见 [`../alphapurify/README.md`](../alphapurify/README.md)）串起来，任一失败即
非零退出。它**不进 CI**：需要真实数据湖与 `.venv-qlib` / `.venv-alphapurify`，
CI runner 上都没有 —— 塞进去只会得到一个永远 skip 的绿灯。

哨兵对 workflow 只断言两件事（收益数字会随窗口/股票池漂移，不能当判据）：

- `excess_return_with_cost.mean` 存在且有限 —— 基准链路活着；
- `ffr == 1.0` —— 基准序列覆盖整个回测窗口（`ffr < 1` 说明基准数据有缺口）。

## 边界

- 只对拍**基准腿**。两侧的组合腿（TopkDropout vs FactorTopN、成本模型）
  口径不同，不做数值对齐 —— 那是 `BACKTEST_ENGINES.md` 已明确的边界：
  qlib 回测只作对照，对外结论一律用原生引擎。
- `data/qlib-xval` 与 `data/qlib` 都在 `.gitignore` 的 `/data` 下，不入库。
- 导出物是**可重建的派生物**，口径以湖（股票）与 `index_daily`（指数）为唯一真源。
