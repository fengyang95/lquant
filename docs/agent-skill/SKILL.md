# lquant 因子挖掘 Agent 操作手册（SKILL.md）

> 随仓库版本化。冻结快照含本文件 hash。Agent 只许说话，不许算数。

## 纪律（必须遵守）

1. **永不自算指标**：所有 IC/t/分层数字由平台算。你产字符串，平台当裁判。
2. **eval 前先 check**：`lq factor check "<expr>"`（毫秒级）永远第一步。
3. **盯校正门槛**：eval 响应里的门槛随 n_trials 上升（sqrt(2·ln n)）——
   你自己前 100 次试验的"显著"在后 500 次里就不显著了。
4. **提交前自查 corr**：入库前用 `lq factor corr`（或 /factors/analyze）确认与库内因子 |ρ|<0.7。
5. **rationale 必填**：submit 的 spec 里 rationale 为空会被拒。
6. **读淘汰原因码**：LOW_IC / REDUNDANT / SIZE_PROXY / OOS_FAIL 都带可操作提示 ——
   "与 mom_20 相关 0.82，建议换字段族"就是下一轮的方向。

## 操作面（CLI）

    lq factor check "<expr>"     # G0 静态校验（毫秒）
    lq factor eval "<expr>"      # 平台算 IC/ICIR/分层 + 中性化对照
    lq factor submit spec.yaml   # ★ 唯一入库通道，服务端重验（A/B 级入库）
    lq agent list / show / test  # 接入验收：lq agent test <name> 必须通过

## spec.yaml 最小样例

```yaml
name: my_factor_001
expr: "Rank(Ts_Mean($close,5)/$close-1)"
agent: claude-code
rationale: "5 日动量秩因子，假设：短动量在波动大的时候更强"
claimed:
  ic_mean: 0.03          # 你自己报的数字会被平台重算推翻或确认
```

## 三条硬护栏（为什么这些纪律存在）

1. **算归平台**：Agent 只编排。submit 服务端全量重算，谎报 IC 会被推翻并归档正确数字。
2. **预算内建**：eval/挖掘配额按 Agent 记账；校正门槛让"显著"标准水涨船高。
3. **submit 即重验**：G0–G3 重跑（含样本外），A/B 级入库；C/D 级落 factor_replication 归档。
