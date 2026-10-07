"""通知编排：env 驱动的通道链构建 + 并发发送。

配置契约（全部环境变量，密钥不入 config/仓库 —— 契合 SECRET_HYGIENE）::

    LQ_NOTIFY_CHANNELS=wecom,feishu,dingtalk,telegram,webhook   # 逗号分隔；空 = 功能关闭
    LQ_WECOM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..
    LQ_FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/..
    LQ_DINGTALK_WEBHOOK_URL=https://oapi.dingtalk.com/robot/send?access_token=..
    LQ_DINGTALK_SECRET=SEC..            # 可选，开了加签的机器人需要
    LQ_TELEGRAM_BOT_TOKEN=..            # 与 LQ_TELEGRAM_CHAT_ID 成对
    LQ_TELEGRAM_CHAT_ID=..
    LQ_GENERIC_WEBHOOK_URL=https://..   # 自建接收端
    LQ_NOTIFY_TIMEOUT=5                 # 秒，可选
    LQ_NOTIFY_TEXT_LIMIT=3500           # 字符，可选（低于企微 4096 字节上限）

语义对齐仓库既有的「缺依赖就降级」哲学：``LQ_NOTIFY_CHANNELS`` 缺省为空时，
``notify()`` 直接返回 skipped —— 没配置通知的部署行为与不引入本模块完全一致，
主链路（day_close / 定时任务）永远不被通知旁路拖住。
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

from lquant.notify.channels import (
    DEFAULT_TEXT_LIMIT,
    DEFAULT_TIMEOUT_S,
    Channel,
    DingTalkBot,
    FeishuBot,
    GenericWebhook,
    SendResult,
    TelegramBot,
    WecomBot,
)

__all__ = ["notify", "build_chain", "active_channels", "CHANNEL_FACTORY"]


# 通道名 -> 工厂。新增通道只需在 channels.py 加类并在此注册一行，
# env 名遵循 LQ_<CHANNEL 大写>_WEBHOOK_URL 的统一约定。
CHANNEL_FACTORY: dict[str, type[Channel]] = {
    "wecom": WecomBot,
    "feishu": FeishuBot,
    "dingtalk": DingTalkBot,
    "telegram": TelegramBot,
    "webhook": GenericWebhook,
}


def _timeout() -> float:
    try:
        return float(os.getenv("LQ_NOTIFY_TIMEOUT", "") or DEFAULT_TIMEOUT_S)
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _text_limit() -> int:
    try:
        return int(os.getenv("LQ_NOTIFY_TEXT_LIMIT", "") or DEFAULT_TEXT_LIMIT)
    except ValueError:
        return DEFAULT_TEXT_LIMIT


def build_chain(names: list[str] | None = None) -> list[Channel]:
    """按 env 配置构建通道实例。未知名跳过并留日志线索（由调用方决定是否告警）。"""
    if names is None:
        raw = os.getenv("LQ_NOTIFY_CHANNELS", "")
        names = [n.strip().lower() for n in raw.split(",") if n.strip()]
    chain: list[Channel] = []
    for n in names:
        cls = CHANNEL_FACTORY.get(n)
        if cls is None:
            # 未知通道名是配置错误：宁可 F-channel 报错也不静默吞掉 ——
            # 否则「拼错 feishu」会退化成单通道，故障面收窄却无人知晓。
            chain.append(_UnknownChannel(n, _timeout()))
            continue
        chain.append(cls(timeout=_timeout()))
    return chain


def active_channels() -> list[str]:
    """当前配置的通道名（构建链并过滤 skipped），供 CLI 展示与自检。"""
    return [c.name for c in build_chain()]


class _UnknownChannel(Channel):
    """占位通道：配置了未注册的通道名时，send 必须显式失败而不是消失。"""

    def __init__(self, name: str, timeout: float) -> None:
        super().__init__(timeout)
        self.name = f"{name}(unknown)"

    def send(self, title: str, text: str) -> SendResult:
        return SendResult(self.name, ok=False,
                          error=f"未注册的通道名，可用: "
                                f"{', '.join(CHANNEL_FACTORY)}")


def notify(title: str, text: str, channels: list[Channel] | None = None) -> list[SendResult]:
    """向所有已配置通道发送 ``title + text``。永不抛异常。

    - 无配置：返回 ``[SendResult(skipped=True)]``，主链路零感知
    - 多通道并发发送（每个通道都有独立超时，串行最坏 5s × N 会拖垮定时任务）
    - 超长文本截断（机器人协议有 4096 字节硬上限，超限整条消息被对端拒收）
    """
    chain = channels if channels is not None else build_chain()
    if not chain:
        return [SendResult("none", ok=False, error="未配置 LQ_NOTIFY_CHANNELS",
                           skipped=True)]
    limit = _text_limit()
    if len(text) > limit:
        text = text[: limit - 1] + "…"

    with ThreadPoolExecutor(max_workers=min(len(chain), 8)) as pool:
        results = list(pool.map(lambda c: c.send(title, text), chain))
    return results


def format_results(results: list[SendResult]) -> str:
    """CLI 友好的单行摘要：ok=2 feishu=errcode=31000:.. webhook=skipped"""
    parts = []
    for r in results:
        if r.skipped:
            parts.append(f"{r.channel}=skipped({r.error or ''})")
        elif r.ok:
            parts.append(f"{r.channel}=ok")
        else:
            parts.append(f"{r.channel}=FAIL({r.error or 'unknown'})")
    return " ".join(parts)
