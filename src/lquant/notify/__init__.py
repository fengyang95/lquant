"""lquant 通知旁路：结果找人的最后一公里。

链路（数据 → 因子 → 回测 → 模拟盘）的结果目前只能「人找看板」；本模块把
关键结果主动推给用户 —— 优先覆盖「需要立刻知道」的场景（模拟盘对账
warning/critical 告警），而不是把每条日志都灌进 IM 群。

用法::

    from lquant.notify import notify
    notify("模拟盘对账告警", "demo: verdict=critical 偏差 12.3%",
           category="alert", severity="critical")

- 分类路由：``category`` ∈ report/alert/error，``LQ_NOTIFY_<类>_CHANNELS`` 收窄渠道
- 降噪：dedup TTL / 全局冷却 / 静默时段（critical 豁免）/ 最低严重度，env 见 service
- 渠道：企微 / 飞书(含加签) / 钉钉(含加签) / Telegram / ntfy / PushPlus / Server酱3 / 通用 webhook
- 长消息按渠道 ``max_chars`` 分片续发，宁可收前 N 片也不静默丢尾部

配置契约见 ``lquant/notify/service.py`` 模块 docstring（全 env 驱动，密钥不入库）。
"""
from lquant.notify.channels import SendResult
from lquant.notify.service import (
    CHANNEL_FACTORY,
    active_channels,
    build_chain,
    format_results,
    notify,
    reset_suppress_state,
    route_channels,
    severity_rank,
    should_suppress,
)

__all__ = ["notify", "SendResult", "build_chain", "active_channels",
           "format_results", "CHANNEL_FACTORY", "route_channels",
           "severity_rank", "should_suppress", "reset_suppress_state"]
