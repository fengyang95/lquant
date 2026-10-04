# 交叉验证（xval）：用外部实现给 lquant 的核心口径当「陪考」

两套独立对拍，**都只借算法/口径，不引运行时**（ADR-11）：

| 目录 | 对拍对象 | 覆盖的能力 | 隔离环境 |
|---|---|---|---|
| [`alphapurify/`](alphapurify/README.md) | [eliasswu/AlphaPurify](https://github.com/eliasswu/AlphaPurify)（MIT，Polars） | 因子评价内核（IC/RankIC 逐日序列）、预处理方法口径 | `.venv-alphapurify` |
| [`qlib/`](qlib/README.md) | [microsoft/qlib](https://github.com/microsoft/qlib) | 基准口径（原生 `index_daily` ↔ qlib bin）、导出物能被 qlib 真跑 | `.venv-qlib` |

共同纪律：**两侧互不 import，只通过 parquet / JSON 交换**。这样「输入是不是同一份」
可验证，「谁的依赖把谁带崩」也不可能发生。

## 哨兵模式（Phase 4.6）

```bash
# 一键：AP 对拍（--check）+ qlib 基准对拍 + qlib workflow 真跑，任一项失败即非零退出
bash scripts/xval/sentinel.sh        # 或 make xval-sentinel
```

**它不是 CI job，也刻意不进 CI**：两套对拍都需要真实数据湖（7000+ 标的的 parquet）
与两个隔离 venv，CI runner 上都没有 —— 塞进去只会得到一个永远 skip 的绿灯，
比没有更糟。它的定位是「定期在开发机/预发布环境跑一次」。

哨兵只断言**口径**，不断言收益数字（收益会随窗口/股票池漂移）：

- AP 侧：`variants.py` 里 `expect="match"` 的每个变体 `max|Δ| ≤ tol`；
  `expect="diverge"` 的变体只要求「**确实仍然分歧**」（上游哪天修好了会提醒你，
  免得挂着过时的说明）；`expect="upstream_broken"` 的变体只验证「它仍然是坏的」。
- qlib 侧：基准日收益/区间收益在 float32 精度内一致；workflow 能真跑完，
  且 `ffr == 1.0`（基准序列覆盖整个回测窗口）、`excess_return_with_cost.mean` 有限。

## 加一个对拍对象要改哪里

清单是**唯一真源**，不在三个脚本里各写一遍：

| 目的 | 改哪里 |
|---|---|
| 加/改预处理变体、加对拍因子 | `alphapurify/variants.py`（纯数据，三个隔离环境都能 import） |
| 加 qlib 侧的算子/因子对拍 | `qlib/` 下新增脚本，并在 `sentinel.sh` 里加一段 |

> 反例警示：AlphaPurify 自己的注册表与分发就不一致（`neutralize("random_forest")`
> 报 `NotImplementedError`，能用的名字 `randomforest` 反而不在注册表里）。
> 单一真源不是洁癖，是这类「列得出、调不到」的唯一解药。
