# 研报复现工作流（对话式 skill）

> 方案第七节。复现 = 对话式 skill 工作流；研报是挖掘的输入，不是平行功能。

## 工作流（谁做什么）

```
读研报(PDF) → 抽 FactorSpec → 对话确认 assumptions → check → eval
→ 比对 claimed → 偏差归因（max 5 轮）→ lq backtest run → submit
```

- **LLM 做**：读研报、抽 FactorSpec、对话确认 assumptions；
  neutral_spec 研报没写就留 null，**严禁代猜**。
- **平台做**：check/eval/backtest/submit 全部平台算 —— LLM 不许算数。

## FactorSpec yaml 样例

```yaml
name: ep_reversal_1m
expr: "-Ts_Return($close, 20)"
claimed:
  ic_mean: 0.045          # 研报宣称的 IC —— ground truth
assumptions:
  - "研报未说明复权方式，假设后复权"
  - "持有期假设 5 日"
neutral_spec: null        # 研报没写中性化 → null，绝不代猜
window: {start: "2015-01-01", end: "2020-12-31"}
universe: all
rationale: "20 日反转因子复现（示例 spec）"
```

## 归因五类（复现失败时，说清楚差在哪）

| 码 | 含义 |
|---|---|
| EXPR_MISREAD | 表达式误读 |
| PARAM_ASSUMED | 参数是猜的（assumptions 非空时优先归因） |
| DATA_CALIBER | 数据口径差（vwap/市值/复权） |
| WINDOW_MISMATCH | 窗口/频率不一致 |
| REPORT_SUSPECT | 研报存疑（D 级：方向反/未来函数/幸存者偏差） |

分级：A 完全复现 / B 趋势一致 / C 无法复现 / **D 研报存疑**。
C/D 落 factor_replication 表归档 —— 研究资产，不是垃圾。
D 级最值钱：检出研报用了未来函数/幸存者偏差，手段复用 F3 截断不变性。
