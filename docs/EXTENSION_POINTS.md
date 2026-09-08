# 扩展点落地状态

| EP | 说明 | 档位 |
|---|---|---|
| EP-1 数据源 | 新写一个 Provider + yaml | 随功能实现 |
| EP-2 因子算子 | `@op` 注册，带 category/min_window | 随功能实现 |
| EP-3 预处理方法 | `METHODS` 注册 | 随功能实现 |
| EP-4 撮合规则 | 加 YAML 文件 | 随功能实现 |
| EP-5 策略方言 | JQ shim | M6b |
| EP-6 组合权重 | skfolio estimator | 随功能实现 |
| EP-7 看板采集器 | collector 注册 | 随功能实现 |
| EP-8 ML 模型 | Model 接口 | M6c |
| EP-9 看板→因子 | 表结构先建 | M7b 建表 |
| EP-10 通知告警 | 仅留接口 | 不做 |
| EP-11 LLM 挖因子 | 架构预留 | 不做 |
| EP-12 多市场 | 三条硬约束先守 | 不做 |
