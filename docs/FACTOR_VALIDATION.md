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

## N1-N6 中性化专项（M2.5 落地，先占位编号）

- N1 去极值/标准化口径与手算一致
- N2 自中性化归零：neutralize(x, [x]) == 0（一句话验证整条回归链路）
- N3 行业中性化 = 组内去均值（与手算一致）
- N4 残差与设计矩阵正交（OLS 一阶条件）
- N5 log 市值口径（市值必须 log 后进回归）
- N6 协变量全缺失必须上报 coverage=0 并告警，不得静默去均值

**状态**：⏳ M2.5 中性化里程碑

## 运行方式

    uv run python scripts/validate_factor.py   # F1-F4 逐层 PASS/FAIL，失败退出 1
