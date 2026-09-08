"""JoinQuant API → 原生语义映射。

兼容性边界用「导入即失败」策略：不支持的 API 直接列出行号报错，
绝不静默返回错误结果。
"""
from __future__ import annotations

# JQ 函数名 → (原生函数, 注意事项)
MAPPING = {
    "attribute_history": ("lquant.research.dialect.jq_shim.attribute_history",
                          "返回 DataFrame，index 为日期"),
    "history": ("lquant.research.dialect.jq_shim.history", ""),
    "get_fundamentals": ("lquant.research.dialect.jq_shim.get_fundamentals",
                         "必须用 pub_date <= 当前日过滤（PIT）"),
    "order_target_value": ("lquant.research.dialect.jq_shim.order_target_value", ""),
    "order_target_percent": ("lquant.research.dialect.jq_shim.order_target_percent", ""),
    "order": ("lquant.research.dialect.jq_shim.order", ""),
    "get_all_securities": ("lquant.research.dialect.jq_shim.get_all_securities", ""),
    "get_index_stocks": ("lquant.research.dialect.jq_shim.get_index_stocks", ""),
    "get_trade_days": ("lquant.research.dialect.jq_shim.get_trade_days", ""),
    "log": ("lquant.research.dialect.jq_shim.log", ""),
}

# 支持但语义有坑，导入时告警
WARNINGS = {
    "attribute_history": "data[sec] 是【上一单位时间】的数据，JQ 老手也常踩",
    "set_option": "JQ 示例普遍写 close_tax=0.001（千一），当前实际万五，不改回测成本翻倍",
    "set_benchmark": "基准用于归因，不影响撮合",
}

UNSUPPORTED = {
    "run_daily", "run_interval", "run_monthly",  # 用原生 on_bar 调度
    "get_current_data", "get_security_info",     # 需要实时源
    "get_future_contracts", "get_dominant_future",
}
