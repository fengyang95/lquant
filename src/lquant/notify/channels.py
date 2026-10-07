"""通知通道实现：企微 / 飞书 / 钉钉 / Telegram / ntfy / PushPlus / Server酱3 / 通用 webhook。

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
- **分片续发优于截断丢弃**：长报告按渠道 ``max_chars`` 切片逐条发送
  （片间 ``slice_pause`` 秒防限流），中途某片失败即停 —— 宁可收到前 N 片
  也比静默丢弃尾部强；响应式改自 daily_stock_analysis 的 Discord 分片实践。

各通道 payload 口径（官方机器人协议）：

- 企微群机器人: ``{"msgtype": "text", "text": {"content": str}}``，
  content 上限 4096 字节（UTF-8），超限直接返回 errcode。
- 飞书自定义机器人: ``{"msg_type": "text", "content": {"text": str}}``；
  开启签名校验时 URL 追加 ``&timestamp=..&sign=..``
  （sign = base64(hmac_sha256(key=f"{ts}\\n{secret}", message=""))）。
- 钉钉自定义机器人: ``{"msgtype": "text", "text": {"content": str}}``；
  加签（``LQ_DINGTALK_SECRET``）：sign = base64(hmac_sha256(secret, f"{ts}\\n{secret}"))。
- Telegram Bot API: ``POST /bot<token>/sendMessage``
  ``{"chat_id": .., "text": str}``（text 上限 4096 字符）。
- ntfy: 纯文本 POST body + ``X-Title`` / ``Authorization: Bearer`` 头 ——
  URL 自带 topic（``https://ntfy.sh/my-topic``），手机推送最简路径。
- PushPlus: ``POST /send`` ``{token, title, content, template:"txt"}``。
- Server酱3: ``POST https://<sendkey>.push.ft07.com/send`` ``{title, desp}``。
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

__all__ = [
    "SendResult",
    "Channel",
    "WecomBot",
    "FeishuBot",
    "DingTalkBot",
    "TelegramBot",
    "NtfyChannel",
    "PushPlusChannel",
    "ServerChan3Channel",
    "GenericWebhook",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_TEXT_LIMIT",
    "slice_text",
]


DEFAULT_TIMEOUT_S = 5.0
# service 层的兜底截断上限（通知超长且有渠道不支持分片时的最后防线）。
DEFAULT_TEXT_LIMIT = 3500


@dataclass(frozen=True)
class SendResult:
    """单通道发送结果。skipped=True 表示通道未配置或被禁用，不算失败。"""

    channel: str
    ok: bool
    error: str | None = None
    skipped: bool = False


def slice_text(title: str, text: str, max_chars: int | None) -> list[tuple[str, str]]:
    """按渠道上限切片。返回 [(每片标题, 每片正文)]；首片带标题，续片带序号标记。"""
    if max_chars is None or len(title) + len(text) <= max_chars:
        return [(title, text)]
    # 首片给标题留位；续片给 "[续 i/N]" 头留位 —— 长度上限是硬约束，先扣头再装正文。
    cont_head = "[续 99/99]\n"
    body_cap = max_chars - max(len(title), len(cont_head)) - 1
    if body_cap < 1:
        # max_chars 小到装不下标题本身属配置错误：返回单片让平台报错（可见），
        # 而不是在 range(step=0) 上抛 ValueError 把发送线程炸掉。
        return [(title, text)]
    chunks = [text[i : i + body_cap] for i in range(0, len(text), body_cap)]
    n = len(chunks)
    out = [(title, chunks[0])]
    out += [(f"[续 {i}/{n}]", c) for i, c in enumerate(chunks[1:], start=1)]
    return out


class Channel:
    """通道基类：子类给 ``_url`` + ``_payload``（或覆写 ``_request``），基类管分片与错。"""

    name = "base"
    max_chars: int | None = 4000  # 单片正文上限；None = 不分片（自建接收端可不限）
    slice_pause = 0.2  # 片间停顿，防机器人限流

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self.timeout = timeout

    # -- 子类接口 ----------------------------------------------------------

    def _url(self) -> str | None:
        """通道请求 URL；返回 None 表示未配置（调用方按 skipped 处理）。"""
        raise NotImplementedError

    def _payload(self, title: str, text: str) -> dict[str, Any]:
        raise NotImplementedError

    def _request(self, title: str, text: str) -> tuple[bytes, dict[str, str]]:
        """构造请求体与头。JSON 协议通道用默认实现；ntfy 这类纯文本覆写。"""
        body = json.dumps(self._payload(title, text), ensure_ascii=False).encode("utf-8")
        return body, {"Content-Type": "application/json"}

    def _post_url(self, url: str) -> str:
        """加签类通道（钉钉/飞书）在此改写 URL；其余原样返回。"""
        return url

    # -- 发送主体 ----------------------------------------------------------

    def send(self, title: str, text: str) -> SendResult:
        """分片发送。全部片成功才 ok；任何一片失败即停并回报该错误。"""
        url = self._url()
        if not url:
            return SendResult(
                self.name, ok=False, error="未配置（缺 webhook URL / token）", skipped=True
            )
        slices = slice_text(title, text, self.max_chars)
        first_err: str | None = None
        for i, (t, chunk) in enumerate(slices):
            body, headers = self._request(t, chunk)
            req = urllib.request.Request(self._post_url(url), data=body, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                first_err = f"HTTP {e.code}: {e.reason[:200]}"
                break
            except Exception as e:  # noqa: BLE001  网络层失败只降级不外抛
                first_err = f"{type(e).__name__}: {e}"
                break

            # 机器人协议的「业务失败」也是 HTTP 200 + errcode != 0，必须解析：
            # 不看 errcode 的话「key 错了」会被当成发送成功，告警静默丢失。
            first_err = _business_error(self.name, raw)
            if first_err:
                break
            if i < len(slices) - 1:
                time.sleep(self.slice_pause)
        return SendResult(self.name, ok=first_err is None, error=first_err)


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
    if channel == "pushplus" and "errcode" not in data and "code" in data:
        # pushplus 官方成功响应是 {"code": 200}（企微/钉钉的 errcode 才是 0=成功），
        # 直接套通用口径会把每次成功发送误判为 FAIL 并中断分片续发。
        # 探测式判定：响应里真有 code 字段才按 pushplus 口径（测试 mock 走通用分支）。
        code = data.get("code", 0)
        if code != 200:
            return f"pushplus code={code}: {str(data.get('msg'))[:200]}"
        return None
    code = data.get("errcode", data.get("code", 0))
    if code:
        return f"{channel} errcode={code}: {str(data.get('errmsg'))[:200]}"
    return None


def _env(key: str) -> str | None:
    """函数内 import os：通道实例化发生在 build_chain（可能早于 env 就绪的测试）。"""
    import os

    return os.getenv(key) or None


class WecomBot(Channel):
    """企业微信群机器人。env: LQ_WECOM_WEBHOOK_URL"""

    name = "wecom"
    max_chars = 1200  # content 上限 4096 字节，UTF-8 中文 3 字节/字，留余量

    def _url(self) -> str | None:
        return _env("LQ_WECOM_WEBHOOK_URL")

    def _payload(self, title: str, text: str) -> dict[str, Any]:
        return {"msgtype": "text", "text": {"content": f"{title}\n{text}".strip()}}


class FeishuBot(Channel):
    """飞书自定义机器人。env: LQ_FEISHU_WEBHOOK_URL [+ LQ_FEISHU_WEBHOOK_SECRET 加签]"""

    name = "feishu"

    def _url(self) -> str | None:
        return _env("LQ_FEISHU_WEBHOOK_URL")

    def _post_url(self, url: str) -> str:
        """开了「签名校验」的机器人必须在 URL 上附 timestamp + sign。

        官方口径与钉钉不同：string_to_sign = f"{timestamp}\\n{secret}" 作为
        **HMAC key**，message 为空串，HmacSHA256 → base64。签名错返回的
        仍是 HTTP 200 + code != 0，靠 _business_error 兜底。
        """
        secret = _env("LQ_FEISHU_WEBHOOK_SECRET")
        if not secret:
            return url
        ts = str(int(time.time()))
        digest = hmac.new(f"{ts}\n{secret}".encode(), b"", digestmod=hashlib.sha256).digest()
        sign = base64.b64encode(digest).decode()
        sep = "&" if urllib.parse.urlparse(url).query else "?"
        return f"{url}{sep}timestamp={ts}&sign={urllib.parse.quote_plus(sign)}"

    def _payload(self, title: str, text: str) -> dict[str, Any]:
        return {"msg_type": "text", "content": {"text": f"{title}\n{text}".strip()}}


class DingTalkBot(Channel):
    """钉钉自定义机器人。env: LQ_DINGTALK_WEBHOOK_URL [+ LQ_DINGTALK_SECRET 加签]"""

    name = "dingtalk"
    max_chars = 1500

    def _url(self) -> str | None:
        return _env("LQ_DINGTALK_WEBHOOK_URL")

    def _post_url(self, url: str) -> str:
        """开了加签（密钥）的机器人必须在 URL 上附 timestamp + sign。

        官方口径：sign = base64(hmac_sha256(secret, f"{timestamp}\\n{secret}"))，
        timestamp 毫秒。签名错返回的也是 HTTP 200 + errcode，靠 _business_error 兜底。
        """
        secret = _env("LQ_DINGTALK_SECRET")
        if not secret:
            return url
        ts = str(int(time.time() * 1000))
        digest = hmac.new(
            secret.encode("utf-8"), f"{ts}\n{secret}".encode(), digestmod=hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest))
        sep = "&" if urllib.parse.urlparse(url).query else "?"
        return f"{url}{sep}timestamp={ts}&sign={sign}"

    def _payload(self, title: str, text: str) -> dict[str, Any]:
        return {"msgtype": "text", "text": {"content": f"{title}\n{text}".strip()}}


class TelegramBot(Channel):
    """Telegram Bot API。env: LQ_TELEGRAM_BOT_TOKEN + LQ_TELEGRAM_CHAT_ID"""

    name = "telegram"
    max_chars = 1500  # text 上限 4096 字符

    def _url(self) -> str | None:
        token = _env("LQ_TELEGRAM_BOT_TOKEN")
        if not token:
            return None
        return f"https://api.telegram.org/bot{token}/sendMessage"

    def _payload(self, title: str, text: str) -> dict[str, Any]:
        return {"chat_id": _env("LQ_TELEGRAM_CHAT_ID") or "", "text": f"{title}\n{text}".strip()}


class NtfyChannel(Channel):
    """ntfy 手机推送。env: LQ_NTFY_URL（含 topic，如 https://ntfy.sh/my-topic）
    [+ LQ_NTFY_TOKEN]。纯文本 POST + X-Title 头，零门槛的个人手机通知。"""

    name = "ntfy"

    def _url(self) -> str | None:
        return _env("LQ_NTFY_URL")

    def _request(self, title: str, text: str) -> tuple[bytes, dict[str, str]]:
        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "X-Title": title.encode("ascii", "ignore").decode() or "lquant",
        }
        token = _env("LQ_NTFY_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return text.encode("utf-8"), headers

    def _payload(self, title: str, text: str) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError("ntfy 走 _request，不用 JSON payload")


class PushPlusChannel(Channel):
    """PushPlus 微信推送。env: LQ_PUSHPLUS_TOKEN"""

    name = "pushplus"
    max_chars = 1800

    def _url(self) -> str | None:
        return "https://www.pushplus.plus/send"

    def _payload(self, title: str, text: str) -> dict[str, Any]:
        return {
            "token": _env("LQ_PUSHPLUS_TOKEN") or "",
            "title": title,
            "content": text,
            "template": "txt",
        }


class ServerChan3Channel(Channel):
    """Server酱3（方糖）App 推送。env: LQ_SERVERCHAN3_SENDKEY"""

    name = "serverchan3"
    max_chars = 1800

    def _url(self) -> str | None:
        key = _env("LQ_SERVERCHAN3_SENDKEY")
        if not key:
            return None
        return f"https://{key}.push.ft07.com/send"

    def _payload(self, title: str, text: str) -> dict[str, Any]:
        return {"title": title[:32], "desp": text}


class GenericWebhook(Channel):
    """通用 webhook（自建接收端）。env: LQ_GENERIC_WEBHOOK_URL

    payload 即 ``{"title": .., "text": ..}`` —— 不绑定任何 IM 协议，
    留给 n8n / 自建网关等做二次分发。
    """

    name = "webhook"
    max_chars = None  # 自建端点无协议上限，交由调用方与 service 兜底截断

    def _url(self) -> str | None:
        return _env("LQ_GENERIC_WEBHOOK_URL")

    def _payload(self, title: str, text: str) -> dict[str, Any]:
        return {"title": title, "text": text}
