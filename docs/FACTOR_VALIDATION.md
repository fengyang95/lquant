# 因子结果正确性验证（F1-F6 + N1-N6）

> 范式照抄 docs/BACKTEST_VALIDATION.md 六层验证。核心认知：因子错误的典型形态是
> **静默错误** —— 不报错，IC 虚高，分层漂亮，最后亏钱。
> 文档 = 测试 = 脚本，三层同源：本文每层的"怎么测"对应
> tests/unit/test_factor_accuracy.py 的断言与 scripts/validate_factor.py 的执行入口。

## F1 金标准手算

**抓什么**：因子值本身算错（窗口错位、方向反、归一化口径错）。
**怎么测**：构造已知输入（固定序列），手算期望输出，逐点对照。现有
tests/unit/test_factor_golden.py 与 test_ops_expanded.py 的手算断言。
**状态**：✅ 本轮

## F2 数学恒等式

**抓什么**：IC 口径错误、方向反、分组列写错、Pearson/Spearman 混用。
**怎么测**（tests/unit/test_factor_accuracy.py）：
- **完美因子 IC == 1.0**（test_f2_perfect_factor_ic_is_one）—— 端到端证真：
  构造与未来收益严格线性相关的因子，整条链路（engine→ic_series）错任何一环 IC 都到不了 1。
- IC(f - mean_d) == IC(f)（test_f2_ic_invariant_to_daily_demean）
- IC(-f) == -IC(f)（test test_f2_ic_antisymmetric）
- 严格单调变换下 RankIC 完全不变（test_f2_rankic_invariant_under_monotonic_transform）
**状态**：✅ 本轮

## F3 截断不变性

**T 日截断数据算出的历史因子值，与全窗数据算出的逐点一致。
不一致 = 未来函数或全样本统计量泄漏（全样本 zscore、全样本去极值）。
**怎么测**：同一表达式分别在全窗/截断窗计算后 join 对照（TS 算子 + CS 算子各一条）。
**状态**：F3/F3b ✅ 本轮

## F4 激标交叉核对
（test_f4_t_stat_identity）IC 序列 t 统计量必须等于 mean/std*sqrt(n)。
**状态**：✅ 本轮

## F5 交叉实现对照

MA20 / RSV10：qlib_alpha 内置实现 vs DSL 翻译版逐点对照（test_f5_cross_implementation_*）。
**状态**：✅ 本轮

## F6 可复现性

同输入两次计算逐位一致（test_f6_reproducible_double_compute）。**状态**：✅ 本轮

## N1-N6 中性化专项（M2.5）

每条一项断言，全部落在 `tests/unit/test_covariates.py`，并由
`scripts/validate_factor.py` 的 N 层按**显式 node id** 逐条执行（改名即报错，
不会静默跑空）：

- N1 去极值口径与手算一致：MAD = 中位数 ± n×1.4826×MAD
  （`test_n1_winsorize_mad_handcalc`）
- N2 自中性化归零：neutralize(x, [x]) == 0，一句话验证整条回归链路
  （`test_n2_self_neutralize_zero`）
- N3 行业均值剔除 = 组内去均值（手算对照）+ PIT as-of 关联
  （`test_n3_industry_mean_equals_within_group_demean` / `test_industry_pit_asof`）
- N4 残差与设计矩阵正交（OLS 一阶条件，`test_n4_residual_orthogonal`）
- N5 log 市值口径：`cov_market_cap == log1p(float_mv)`
  （`test_market_cap_is_log`）
- N6 协变量全缺失必须**报错**，不得静默去均值
  （`test_n6_all_covariates_missing_raises`）

**状态**：✅ 已落地并纳入 `validate_factor.py`（此前脚本只跑到 F6）

## 评估阶梯 L0-L3（因子筛选侧，与 F/N 互补）

F1-F6/N1-N6 验的是"**算得对不对**"；下面四层验的是"**这个因子值不值得留**"。
前者是平台单元测试，后者是 Agent 每次提交前的证据链。命令见 `docs/agent-skill/SKILL.md`，
细则与原因码见 `config/skills/factor-mining/`。

| 层 | 入口 | 判据 | 对应测试 |
|---|---|---|---|
| L0 静态 | `lq factor check` | 语法/算子/字段/最小窗口 ≥1；`#` 注释在词法层丢弃 | tests/unit/test_dsl.py |
| L1 快筛 | `lq factor eval` / `g1_fast_screen` | \|IC\| 过筛、中性化后不归零、过校正门槛 sqrt(2·ln n) | tests/unit/test_factor_accuracy.py（F2）、test_mining.py |
| L2 全量 | `lq factor audit` | 分层单调、IC 衰减、收益归因、**评级**、OOS 衰减 | tests/unit/test_factor_rating.py |
| L3 稳健 | `lq factor robust` | 参数扰动、分段稳定、起点敏感（变异系数）、月度剔除 | tests/unit/test_factor_robustness.py |

**评级口径**（`config/factors/rating.yaml`，缺失走内置兜底）：
Strong ⇐ |ICIR|≥0.5 且 |单调性|≥0.8 且 |L/S Sharpe|≥1.0 且过门槛；Moderate / Weak 依次降档。
多重检验：门槛随试验次数 n_trials 上浮，前 100 次试验的"显著"在后 500 次里自动失效。

**稳健性判据的三处反直觉点**（都是踩过的坑）：
- 起点敏感性用**变异系数** `std/|mean|` 而非绝对 std —— ICIR≈198 时绝对 std≈8 会误判失败。
- 样本太短（月数 < 2·top_n）时月度剔除返回 `insufficient`，记 skipped 而非 failed。
- 零方差完美因子 → IR 记 `inf`（而不是 NaN 被降档），否则最强的因子反而被评低。

**状态**：✅ 本轮（rating / robustness / audit / robust / report + 70/15/15 分段复用）

## 运行方式

    uv run python scripts/validate_factor.py   # F1-F6 + N1-N6 逐层 PASS/FAIL，失败退出 1
    PYTHONPATH=src python -m pytest tests/unit/test_dsl.py \
        tests/unit/test_factor_accuracy.py tests/unit/test_factor_rating.py \
        tests/unit/test_factor_robustness.py -q     # L0-L3 逐层

