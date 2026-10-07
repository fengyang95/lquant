"""lquant 通知旁路：结果找人的最后一公里。

链路（数据 → 因子 → 回测 → 模拟盘）的结果目前只能「人找看板」；本模块把
关键结果主动推给用户 —— 优先覆盖「需要立刻知道」的场景（模拟盘对账
warning/critical 告警），而不是把每条日志都灌进 IM 群。

用法::

    from lquant.notify import notify
    notify("模拟盘对账告警", "demo: verdict=critical 偏差 12.3%")

配置见 ``lquant/notify/service.py`` 模块 docstring（全 env 驱动，密钥不入库）。
"""
from lquant.notify.channels import SendResult
from lquant.notify.service import (
    CHANNEL_FACTORY,
    active_channels,
    build_chain,
    format_results,
    notify,
)

__all__ = ["notify", "SendResult", "build_chain", "active_channels",
           "format_results", "CHANNEL_FACTORY"]
