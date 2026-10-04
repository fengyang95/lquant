# qlib 接入

把 lquant 日线湖接入 [microsoft/qlib](https://github.com/microsoft/qlib)，用它的
因子分析（IC/ICIR/RankIC、Alpha158）与组合回测（TopkDropout）做交叉验证。

## 架构

- **导出器** `src/lquant/qlib_io/export.py`（主 venv，纯 polars/pyarrow，
  不依赖 pyqlib）：湖 → qlib 二进制格式。
- **runner** `src/lquant/qlib_io/runner.py`（qlib 专用 venv）：独立脚本，
  执行 qrun 风格工作流，不 import lquant。
- **CLI** `lq qlib export / check / workflow`。

pyqlib 依赖树重，刻意不进主 venv（与 btval 同一隔离思路）：

```bash
python -m venv .venv-qlib
.venv-qlib/bin/pip install pyqlib lightgbm
# macOS 上 lightgbm 需要 OpenMP 运行时：
brew install libomp
```

`lq qlib workflow` 的解释器探测顺序：当前解释器可 import qlib → 进程内跑；
否则 `LQ_QLIB_PYTHON` / `--python` 指向的 venv → 子进程跑。

## 用法

```bash
# 0. 指数回填（基准的唯一来源；指数不入日线湖，走 DuckDB index_daily）
lq data index --start 2016-01-01

# 1. 导出（默认 stock、默认字段 OHLCV+amount+vwap+factor；--top 300 额外产出流动性池）
#    默认一并导出基准 000300.SH（qlib symbol SH000300）
lq qlib export --out data/qlib --top 300

# 2. 自检（日历有序、清单与 features 一致、基准就位、bin 抽样回读）
lq qlib check --dir data/qlib

# 3. 跑工作流：Alpha158 特征 + LGBM 训练 + IC 分析 + TopkDropout 回测
lq qlib workflow --config config/qlib/workflow_alpha158_lgbm.yaml \
    --provider data/qlib --market top300 \
    --out data/qlib/last_metrics.json
```

`--market` 覆盖股票池：`all`（默认，instruments/all.txt）/ `top300` 等。
metrics JSON 含 IC / ICIR / Rank IC / Rank ICIR 及回测年化、最大回撤、超额收益等。

## 基准口径（Phase 1.3）

qlib 回测的 `benchmark` 是 **`SH000300`（真实沪深300）**，不再是 `SH600000`
机械代理 —— 后者的「超额收益」是拿招商银行当大盘，数字没有解释力。

- 指数**不在 parquet 日线湖里**（点位不是价格，会触发价格护栏），所以
  `lq qlib export` 单独从 DuckDB `index_daily` 读基准，写成
  `features/SH000300/$close.day.bin` 并登记进 `instruments/all.txt`。
- 导出清单 `qlib_export_meta.json` 记录 `benchmark` / `benchmark_symbol_lquant`；
  `lq qlib check` 会校验「清单里的基准确实在 features 与 instruments 里」。
- `lq qlib workflow` 开跑前比对「yaml 的 benchmark」与「导出物的 benchmark」，
  不一致直接告警并给出修补命令（否则 qlib 找不到标的、超额收益静默为空）。
- `--no-benchmark` 可关闭基准导出；此时 yaml 里的 `benchmark` 也必须置 `null`。
- 指数**不复权**：`$factor = 1.0`，其余字段（volume/amount/turnover 等）补 NaN。

原生引擎侧对应实现是 `backtest/benchmark.py`（默认同为 `000300.SH`）；两侧
超额收益的**符号应一致、量级应接近**，这正是 qlib 交叉验证的用途。

## 价格口径（重要）

湖内 OHLC 是**不复权价**。导出时按 `adj_factor` 归一化：

- `$open/$high/$low/$close/$vwap` = 复权序列（归一化到最新一日 = 不复权）；
- `$factor` = adj_factor / adj_factor_latest，不复权价 = 复权价 / $factor；
- `$vwap` = amount/volume × f，停牌（volume≤0）为 NaN；
- volume/amount 及可选字段（pe_ttm/total_mv 等，`--field` 指定）为原始值。

收益率口径与真实一致，回测 `deal_price: close` 直接可用。

## 与仓库内原生实现的关系

`factors/qlib_alpha.py`（Alpha158 纯 Polars 重实现）与 `research/ml/`
（借 qlib 接口但不用其运行时）是**原生链路**；本模块是**真 qlib 链路**，
两者可互为交叉验证（qlib 源为 dump_bin 同构格式，公式口径一致）。

## 已知边界

- 基准来自 DuckDB `index_daily`；该表为空时导出物的 `benchmark` 为 `null`，
  此时 workflow 只能看绝对收益 —— `lq qlib export` 会显式告警并提示
  `lq data index`，不会静默退化。
- 导出不覆盖 DuckDB 参考表：股票走 parquet 湖、基准走 `index_daily` 表，
  两者是**各自唯一的真源**。
