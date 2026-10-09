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
| EP-9 看板→因子 | 入口 `src/lquant/factors/sources/board.py`（看板数据源） | 已交付（含入口） |
| EP-10 通知告警 | 入口 `src/lquant/notify/`（渠道 + 规则引擎 + CLI） | 已交付（含入口） |
| EP-11 LLM 挖因子 | 入口 `src/lquant/research/report_extract.py`（研报提案） | 已交付（含入口） |
| EP-12 多市场 | 三条硬约束先守 | 不做 |
| EP-13 分析角度 | 入口 `src/lquant/core/report.py`（报告契约原语）+ `security/contract.py` / `industry/contract.py` 的角度注册表 | 已交付（含入口） |
