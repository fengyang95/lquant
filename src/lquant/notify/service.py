"""通知编排：env 驱动的通道链构建 + 分类路由 + 降噪 + 并发发送。

配置契约（全部环境变量，密钥不入 config/仓库 —— 契合 SECRET_HYGIENE）::

    LQ_NOTIFY_CHANNELS=wecom,feishu,dingtalk,telegram,ntfy,pushplus,serverchan3,webhook
    #   ^ 逗号分隔；空 = 功能关闭
    LQ_WECOM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..
    LQ_FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/..
    LQ_FEISHU_WEBHOOK_SECRET=..         # 可选，机器人开了「签名校验」时需要
    LQ_DINGTALK_WEBHOOK_URL=https://oapi.dingtalk.com/robot/send?access_token=..
    LQ_DINGTALK_SECRET=SEC..            # 可选，开了加签的机器人需要
    LQ_TELEGRAM_BOT_TOKEN=..            # 与 LQ_TELEGRAM_CHAT_ID 成对
    LQ_TELEGRAM_CHAT_ID=..
    LQ_NTFY_URL=https://ntfy.sh/my-topic   # 手机推送最简路径，URL 含 topic
    LQ_NTFY_TOKEN=..                    # 可选
    LQ_PUSHPLUS_TOKEN=..
    LQ_SERVERCHAN3_SENDKEY=..
    LQ_GENERIC_WEBHOOK_URL=https://..   # 自建接收端
    LQ_NOTIFY_TIMEOUT=5                 # 秒，可选
    LQ_NOTIFY_TEXT_LIMIT=3500           # 兜底截断，可选

分类路由与降噪（全部默认关闭 —— 零配置部署行为与 v1 完全一致）::

    LQ_NOTIFY_REPORT_CHANNELS=feishu     # report 类消息只发这些渠道（收窄，不扩容）
    LQ_NOTIFY_ALERT_CHANNELS=wecom,ntfy  # alert 类（对账告警等）
    LQ_NOTIFY_ERROR_CHANNELS=webhook     # error 类（系统故障）
    LQ_NOTIFY_MIN_SEVERITY=warning       # info<warning<critical，低于该级不发
    LQ_NOTIFY_DEDUP_TTL_SECONDS=300      # 同内容消息 TTL 内只发一次（0/缺省=关）
    LQ_NOTIFY_COOLDOWN_SECONDS=60        # 全局冷却：上一次发送后 N 秒内静默
    LQ_NOTIFY_QUIET_HOURS=23:00-08:00    # 静默时段（Asia/Shanghai）；critical 豁免

降噪状态是**进程内**的（dict + 时间戳）：lquant 的通知发起点目前都是
单进程内的长任务（day_close / 定时 job），进程内状态已覆盖；跨进程
持久化留给规则引擎（rules.py 的 SQLite 状态）—— 不为用不到的场景先建库。
但同进程内已有多个并发入口（server API 线程池），降噪判定与登记由
``_SUPPRESS_LOCK`` 串行化，check-then-act 不做原子化会双发。

语义对齐仓库既有的「缺依赖就降级」哲学：``LQ_NOTIFY_CHANNELS`` 缺省为空时，
``notify()`` 直接返回 skipped —— 没配置通知的部署行为与不引入本模块完全一致，
主链路（day_close / 定时任务）永远不被通知旁路拖住。
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

from lquant.notify.channels import (
    DEFAULT_TEXT_LIMIT,
    DEFAULT_TIMEOUT_S,
    Channel,
    DingTalkBot,
    FeishuBot,
    GenericWebhook,
    NtfyChannel,
    PushPlusChannel,
    SendResult,
    ServerChan3Channel,
    TelegramBot,
    WecomBot,
)

__all__ = [
    "notify",
    "build_chain",
    "active_channels",
    "CHANNEL_FACTORY",
    "route_channels",
    "should_suppress",
    "reset_suppress_state",
    "format_results",
    "severity_rank",
]


# 通道名 -> 工厂。新增通道只需在 channels.py 加类并在此注册一行，
# env 名遵循 LQ_<CHANNEL 大写>_WEBHOOK_URL / _TOKEN / _SENDKEY 的统一约定。
CHANNEL_FACTORY: dict[str, type[Channel]] = {
    "wecom": WecomBot,
    "feishu": FeishuBot,
    "dingtalk": DingTalkBot,
    "telegram": TelegramBot,
    "ntfy": NtfyChannel,
    "pushplus": PushPlusChannel,
    "serverchan3": ServerChan3Channel,
    "webhook": GenericWebhook,
}

_CATEGORIES = ("report", "alert", "error")
# 每类消息允许的渠道收窄 env：只收窄已启用渠道，不会单独启用（对齐
# daily_stock_analysis 的路由语义 —— 路由不是第二个配置入口）。
_CATEGORY_ENV = {
    "report": "LQ_NOTIFY_REPORT_CHANNELS",
    "alert": "LQ_NOTIFY_ALERT_CHANNELS",
    "error": "LQ_NOTIFY_ERROR_CHANNELS",
}

_SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}

# 进程内降噪状态（见模块 docstring 的取舍说明）
_dedup_seen: dict[str, float] = {}
_last_sent: float = 0.0
_SUPPRESS_LOCK = threading.Lock()


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


def _float_env(key: str) -> float:
    try:
        return float(os.getenv(key, "") or 0)
    except ValueError:
        return 0.0


def severity_rank(severity: str) -> int:
    return _SEVERITY_RANK.get((severity or "info").lower(), 0)


def build_chain(names: list[str] | None = None) -> list[Channel]:
    """按 env 配置构建通道实例。未知名跳过并留日志线索（由调用方决定是否告警）。"""
    if names is None:
        raw = os.getenv("LQ_NOTIFY_CHANNELS", "")
        names = [n.strip().lower() for n in raw.split(",") if n.strip()]
    chain: list[Channel] = []
    for n in names:
        cls = CHANNEL_FACTORY.get(n)
        if cls is None:
            # 未知通道名是配置错误：宁可显式报错也不静默吞掉 ——
            # 否则「拼错 feishu」会退化成单通道，故障面收窄却无人知晓。
            chain.append(_UnknownChannel(n, _timeout()))
            continue
        chain.append(cls(timeout=_timeout()))
    return chain


def route_channels(chain: list[Channel], category: str) -> list[Channel]:
    """按消息类别收窄渠道。env 未配置该类 = 不收窄（沿用全部已启用渠道）。"""
    if category not in _CATEGORIES:
        category = "report"
    raw = os.getenv(_CATEGORY_ENV[category], "")
    allowed = {n.strip().lower() for n in raw.split(",") if n.strip()}
    if not allowed:
        return chain
    return [c for c in chain if c.name in allowed]


def _now_cn() -> datetime:
    """当前北京时间。lquant 硬约束之一：时间统一 ISO 8601 + Asia/Shanghai。"""
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def in_quiet_hours(now: datetime | None = None) -> bool:
    """静默时段判定。``LQ_NOTIFY_QUIET_HOURS="23:00-08:00"``，跨午夜区间合法。"""
    raw = os.getenv("LQ_NOTIFY_QUIET_HOURS", "").strip()
    if not raw or "-" not in raw:
        return False
    try:
        start_s, end_s = (p.strip() for p in raw.split("-", 1))
        h1, m1 = (int(x) for x in start_s.split(":"))
        h2, m2 = (int(x) for x in end_s.split(":"))
        # dtime() 也必须在 try 内：``25:00`` / ``23:70`` / ``23:00-24:00``
        # 这类很常见的手误会抛 ValueError("hour must be in 0..23")，而它在
        # 原实现里位于 try 之外 —— 一穿出去就把所有 info/warning 通知打挂
        # （paper 路径还被 except 吞成一条 warning，即告警静默丢失）。
        # 「配置错 → 不静默」与上面格式错的处理保持一致。
        start, end = dtime(h1, m1), dtime(h2, m2)
    except ValueError:
        return False
    t = (_now_cn() if now is None else now).time()
    if start <= end:  # 同日区间 09:00-12:00
        return start <= t <= end
    return t >= start or t <= end  # 跨午夜 23:00-08:00


def should_suppress(title: str, text: str, severity: str = "info") -> str | None:
    """降噪判定。返回压制原因（供日志/测试断言）；None = 放行。"""
    if severity_rank(severity) < severity_rank(os.getenv("LQ_NOTIFY_MIN_SEVERITY", "")):
        return "min_severity"
    # critical 永远豁免静默时段 —— 深夜对账背离恰恰是最该叫醒人的信号。
    if severity_rank(severity) < 2 and in_quiet_hours():
        return "quiet_hours"
    cooldown = _float_env("LQ_NOTIFY_COOLDOWN_SECONDS")
    if cooldown > 0 and _last_sent and time.time() - _last_sent < cooldown:
        return "cooldown"
    ttl = _float_env("LQ_NOTIFY_DEDUP_TTL_SECONDS")
    if ttl > 0:
        key = hashlib.sha1(f"{title}|{text}".encode()).hexdigest()
        seen = _dedup_seen.get(key)
        if seen and time.time() - seen < ttl:
            return "dedup"
    return None


def reset_suppress_state() -> None:
    """清空进程内降噪状态（测试隔离；生产长任务无需调用）。"""
    global _last_sent
    with _SUPPRESS_LOCK:
        _dedup_seen.clear()
        _last_sent = 0.0


def notify(
    title: str,
    text: str,
    *,
    category: str = "report",
    severity: str = "info",
    channels: list[Channel] | None = None,
) -> list[SendResult]:
    """向已配置渠道发送 ``title + text``。永不抛异常。

    - 无配置：返回 ``[SendResult(skipped=True)]``，主链路零感知
    - ``category``（report/alert/error）按 env 收窄渠道，见 ``_CATEGORY_ENV``
    - ``severity`` 参与 min_severity 与静默时段豁免判定（critical 豁免）
    - 降噪（dedup/cooldown/quiet）任一命中 → 返回带原因的 skipped 结果
    - 多通道并发发送；超长文本由各渠道 ``max_chars`` 分片续发
    """
    chain = channels if channels is not None else build_chain()
    if not chain:
        return [SendResult("none", ok=False, error="未配置 LQ_NOTIFY_CHANNELS", skipped=True)]
    chain = route_channels(chain, category)
    if not chain:
        return [
            SendResult(
                f"none({category})",
                ok=False,
                error=f"category={category} 路由后无渠道",
                skipped=True,
            )
        ]
    limit = _text_limit()
    if limit and len(text) > limit:
        text = text[: limit - 1] + "…"
    # check-then-act 原子化：server 线程池并发调 notify 时，两个相同请求
    # 不该同时过 dedup 判定造成双发。判定与登记共用同一份（截断后的）text，
    # 否则「查原文、记截断文」指纹对不上，超长消息去重失效。
    with _SUPPRESS_LOCK:
        reason = should_suppress(title, text, severity)
        if reason:
            return [SendResult("suppressed", ok=False, error=reason, skipped=True)]
        prev_last = _last_sent
        _mark_sent(title, text)

    with ThreadPoolExecutor(max_workers=min(len(chain), 8)) as pool:
        results = list(pool.map(lambda c: c.send(title, text), chain))
    delivered = sum(getattr(r, "sent_parts", 0) for r in results)
    if results and not any(r.ok for r in results) and delivered == 0:
        # 只有「一片都没送出去」才撤销登记允许重试 —— 告警丢失比重复告警更糟，
        # 「被 cooldown/dedup 压住的重试」会让故障看起来像已送达。
        # 部分送达（前几片成功、后面失败）**不能**撤销：重试会把已送达的
        # 前缀片再发一遍，群里出现重复内容。
        with _SUPPRESS_LOCK:
            _forget_sent(title, text, prev_last)
    return results


def _mark_sent(title: str, text: str) -> None:
    """登记降噪状态（dedup 指纹 + 全局冷却时间戳）。调用方须持 _SUPPRESS_LOCK。"""
    global _last_sent
    ttl = _float_env("LQ_NOTIFY_DEDUP_TTL_SECONDS")
    if ttl > 0:
        key = hashlib.sha1(f"{title}|{text}".encode()).hexdigest()
        _dedup_seen[key] = time.time()
        # 顺手清理过期指纹，长驻进程不积内存
        for k in [k for k, v in _dedup_seen.items() if time.time() - v >= ttl]:
            _dedup_seen.pop(k, None)
    if _float_env("LQ_NOTIFY_COOLDOWN_SECONDS") > 0:
        _last_sent = time.time()


def _forget_sent(title: str, text: str, prev_last: float) -> None:
    """全部通道发送失败时撤销登记。调用方须持 _SUPPRESS_LOCK。

    cooldown 时间戳恢复为发送前的值（此前从未发过则回 0.0）——
    否则网络故障期间的重试会一直被冷却窗口吞掉，告警永久丢失。
    """
    global _last_sent
    key = hashlib.sha1(f"{title}|{text}".encode()).hexdigest()
    _dedup_seen.pop(key, None)
    _last_sent = prev_last


def active_channels() -> list[str]:
    """当前**真正可用**的通道名（URL/凭证齐备），供 CLI 展示与自检。

    只看 ``LQ_NOTIFY_CHANNELS`` 会把未配 URL/token 的通道也算进来：用户以为
    配好了，直到 ``lq notify test`` 才发现全是 skipped。
    """
    return [c.name for c in build_chain() if c.configured()[0]]


def channel_status() -> list[tuple[str, bool, str]]:
    """``(通道名, 是否就绪, 说明)`` —— ``lq notify status`` 的真实自检数据源。

    与 ``active_channels`` 的分工：这个把**未就绪的也列出来并说明缺什么**，
    status 命令才能告诉用户「为什么这个通道不会发消息」。
    """
    out: list[tuple[str, bool, str]] = []
    for c in build_chain():
        ready, why = c.configured()
        out.append((c.name, ready, why))
    return out


class _UnknownChannel(Channel):
    """占位通道：配置了未注册的通道名时，send 必须显式失败而不是消失。"""

    def __init__(self, name: str, timeout: float) -> None:
        super().__init__(timeout)
        self.name = f"{name}(unknown)"

    def send(self, title: str, text: str) -> SendResult:
        return SendResult(
            self.name, ok=False, error=f"未注册的通道名，可用: {', '.join(CHANNEL_FACTORY)}"
        )


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
