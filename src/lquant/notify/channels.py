"""通知通道实现：企微 / 飞书 / 钉钉 / Telegram / 通用 webhook。

设计纪律（与仓库既有哲学对齐）：

- **零依赖**：全部走 stdlib ``urllib.request``。通知是可选旁路，不配 URL 就
  整个模块静默关闭 —— 没理由为它多拉一个 HTTP 客户端依赖。
- **永不抛异常**：通知失败只降级为 ``SendResult(ok=False, error=...)``。
  模拟盘对账告警发不出去不该把 day_close 主链路掀翻 —— 对齐「缓存写失败
  不中断计算」「日历不可用不阻断 tick」的既有取舍。
- **密钥只走 env**：webhook URL 本质是凭证（拿到就能往群里发消息），
  绝不落 config 文件与代码仓库，契合 ``docs/SECRET_HYGIENE.md``。
- **超时必有**：webhook 对端不可达时最多等 ``LQ_NOTIFY_TIMEOUT`` 秒，
  不允许通知旁路拖住定时任务。

各通道 payload 口径（实测过的官方机器人协议）：

- 企微群机器人: ``{"msgtype": "text", "text": {"content": str}}``，
  content 上限 4096 字节（UTF-8），超限直接返回 errcode。
- 飞书自定义机器人: ``{"msg_type": "text", "content": {"text": str}}``。
- 钉钉自定义机器人: ``{"msgtype": "text", "text": {"content": str}}``；
  开启加签时在 URL 上追加 ``&timestamp=..&sign=..``
  （sign = base64(hmac_sha256(secret, f"{ts}\\n{secret}"))）。
- Telegram Bot API: ``POST /bot<token>/sendMessage``
  ``{"chat_id": .., "text": str}``。
- 通用 webhook: POST JSON ``{"title": .., "text": ..}``，留给自建接收端。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

__all__ = ["SendResult", "Channel", "WecomBot", "FeishuBot", "DingTalkBot",
           "TelegramBot", "GenericWebhook", "DEFAULT_TIMEOUT_S",
           "DEFAULT_TEXT_LIMIT"]


DEFAULT_TIMEOUT_S = 5.0
# 企微 content 上限 4096 字节；UTF-8 中文 3 字节/字，留余量取 1300 字符级别。
# 钉钉/飞书同量级，统一按一个更低的保守值截断 —— 报警消息就该短。
DEFAULT_TEXT_LIMIT = 3500


@dataclass(frozen=True)
class SendResult:
    """单通道发送结果。skipped=True 表示通道未配置或被禁用，不算失败。"""

    channel: str
    ok: bool
    error: str | None = None
    skipped: bool = False


class Channel:
    """通道基类。子类实现 ``_payload`` 与 ``_url``，基类负责发与错。"""

    name = "base"

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self.timeout = timeout

    # -- 子类接口 ----------------------------------------------------------

    def _url(self) -> str | None:
        """通道请求 URL；返回 None 表示未配置（调用方按 skipped 处理）。"""
        raise NotImplementedError

    def _payload(self, title: str, text: str) -> dict[str, Any]:
        raise NotImplementedError

    def _post_url(self, url: str) -> str:
        """钉钉加签需要改写 URL；其余通道原样返回。"""
        return url

    # -- 发送主体 ----------------------------------------------------------

    def send(self, title: str, text: str) -> SendResult:
        url = self._url()
        if not url:
            return SendResult(self.name, ok=False,
                              error="未配置（缺 webhook URL / token）",
                              skipped=True)
        body = json.dumps(self._payload(title, text),
                          ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._post_url(url), data=body,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return SendResult(self.name, ok=False,
                              error=f"HTTP {e.code}: {e.reason[:200]}")
        except Exception as e:  # noqa: BLE001  任何网络层失败都只降级不外抛
            return SendResult(self.name, ok=False, error=f"{type(e).__name__}: {e}")

        # 机器人协议的「业务失败」也是 HTTP 200 + errcode != 0，必须解析：
        # 不看 errcode 的话「key 错了」会被当成发送成功，告警静默丢失。
        err = _business_error(self.name, raw)
        if err:
            return SendResult(self.name, ok=False, error=err)
        return SendResult(self.name, ok=True)


def _business_error(channel: str, raw: str) -> str | None:
    """解析机器人响应里的业务错误；无法解析时按 HTTP 成功放行。"""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if channel == "telegram":
        if not data.get("ok", True):
            return f"telegram: {str(data.get('description'))[:200]}"
        return None
    code = data.get("errcode", data.get("code", 0))
    if code:
        return f"{channel} errcode={code}: {str(data.get('errmsg'))[:200]}"
    return None


class WecomBot(Channel):
    """企业微信群机器人。env: LQ_WECOM_WEBHOOK_URL"""

    name = "wecom"

    def _url(self) -> str | None:
        import os
        return os.getenv("LQ_WECOM_WEBHOOK_URL") or None

    def _payload(self, title: str, text: str) -> dict[str, Any]:
        return {"msgtype": "text",
                "text": {"content": f"{title}\n{text}".strip()}}


class FeishuBot(Channel):
    """飞书自定义机器人。env: LQ_FEISHU_WEBHOOK_URL"""

    name = "feishu"

    def _url(self) -> str | None:
        import os
        return os.getenv("LQ_FEISHU_WEBHOOK_URL") or None

    def _payload(self, title: str, text: str) -> dict[str, Any]:
        return {"msg_type": "text",
                "content": {"text": f"{title}\n{text}".strip()}}


class DingTalkBot(Channel):
    """钉钉自定义机器人。env: LQ_DINGTALK_WEBHOOK_URL [+ LQ_DINGTALK_SECRET 加签]"""

    name = "dingtalk"

    def _url(self) -> str | None:
        import os
        return os.getenv("LQ_DINGTALK_WEBHOOK_URL") or None

    def _post_url(self, url: str) -> str:
        """开了加签（密钥）的机器人必须在 URL 上附 timestamp + sign。

        官方口径：sign = base64(hmac_sha256(secret, f"{timestamp}\\n{secret}"))，
        timestamp 毫秒。签名错返回的也是 HTTP 200 + errcode，靠 _business_error 兜底。
        """
        import os
        secret = os.getenv("LQ_DINGTALK_SECRET")
        if not secret:
            return url
        ts = str(int(time.time() * 1000))
        digest = hmac.new(secret.encode("utf-8"),
                          f"{ts}\n{secret}".encode(),
                          digestmod=hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest))
        sep = "&" if urllib.parse.urlparse(url).query else "?"
        return f"{url}{sep}timestamp={ts}&sign={sign}"

    def _payload(self, title: str, text: str) -> dict[str, Any]:
        return {"msgtype": "text",
                "text": {"content": f"{title}\n{text}".strip()}}


class TelegramBot(Channel):
    """Telegram Bot API。env: LQ_TELEGRAM_BOT_TOKEN + LQ_TELEGRAM_CHAT_ID"""

    name = "telegram"

    def _url(self) -> str | None:
        import os
        token = os.getenv("LQ_TELEGRAM_BOT_TOKEN")
        if not token:
            return None
        return f"https://api.telegram.org/bot{token}/sendMessage"

    def _payload(self, title: str, text: str) -> dict[str, Any]:
        import os
        return {"chat_id": os.getenv("LQ_TELEGRAM_CHAT_ID", ""),
                "text": f"{title}\n{text}".strip()}


class GenericWebhook(Channel):
    """通用 webhook（自建接收端）。env: LQ_GENERIC_WEBHOOK_URL

    payload 即 ``{"title": .., "text": ..}`` —— 不绑定任何 IM 协议，
   留给 n8n / 自建网关等做二次分发。
    """

    name = "webhook"

    def _url(self) -> str | None:
        import os
        return os.getenv("LQ_GENERIC_WEBHOOK_URL") or None

    def _payload(self, title: str, text: str) -> dict[str, Any]:
        return {"title": title, "text": text}
