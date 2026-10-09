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
  ``msgtype=text`` 的 content 上限是 **2048 字节**（UTF-8；4096 是
  ``markdown`` 类型的上限），超限直接返回 errcode。分片必须按**字节**算。
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
    # 本次**真的送达**的片数。用于区分「一片都没发出去」（可以安全重试）
    # 与「发了一半才失败」（重试会重复投递已送达的前缀）。见 service.notify。
    sent_parts: int = 0


def _safe_url(url: str) -> str:
    """URL 的脱敏摘要（只留 scheme+host），用于错误串与日志。

    webhook URL 本身就是凭证：path/query 里带企微 key、钉钉 access_token、
    Telegram bot token。任何可能外泄的字符串都只能带这个摘要。
    """
    try:
        p = urllib.parse.urlsplit(url if "://" in url else f"//{url}")
        if p.netloc:
            return f"{p.scheme or '?'}://{p.netloc}/…"
    except Exception:  # noqa: BLE001 - 脱敏失败也必须给占位符
        pass
    return "<webhook>"


def _redact(msg: str, *urls: str) -> str:
    """把错误串里出现的 URL / path / query 片段换成脱敏摘要。

    底层异常的消息形态不一致，两种都含凭证，必须都盖掉：
    ``ValueError: unknown url type`` 带**整串 URL**；``http.client.InvalidURL``
    只带 **path+query**（``'/cgi-bin/webhook/send?key=SECRET'``）。
    """
    for u in urls:
        if not u:
            continue
        p = urllib.parse.urlsplit(u if "://" in u else f"//{u}")
        cands = {u}
        if p.path and p.query:
            cands.add(f"{p.path}?{p.query}")
        if p.netloc and p.path:
            cands.add(f"{p.netloc}{p.path}")
            if p.query:
                cands.add(f"{p.netloc}{p.path}?{p.query}")
        # 只替换足够长的片段：短 path（"/send"）替换掉只会让报错更难读。
        for c in sorted((c for c in cands if c and len(c) > 8), key=len, reverse=True):
            msg = msg.replace(c, _safe_url(u) if c == u else "***")
    return msg


def _chunk_by_limits(text: str, char_cap: int | None, byte_cap: int | None) -> list[str]:
    """按「字符数 + UTF-8 字节数」双上限切分（中文 3 字节/字）。

    只按字符切会在 CJK 文本上超字节上限：企微 ``msgtype=text`` 是 2048
    **字节**，1200 个中文字 = 3600 字节，首片即被拒且后续片不再发 ——
    分片续发等于失效。按码点切分天然不会切断代理对。
    """
    chunks: list[str] = []
    cur: list[str] = []
    cur_bytes = 0
    for ch in text:
        b = len(ch.encode("utf-8"))
        if cur and ((char_cap is not None and len(cur) >= char_cap)
                    or (byte_cap is not None and cur_bytes + b > byte_cap)):
            chunks.append("".join(cur))
            cur, cur_bytes = [], 0
        cur.append(ch)
        cur_bytes += b
    if cur:
        chunks.append("".join(cur))
    return chunks or [""]


def slice_text(title: str, text: str, max_chars: int | None,
               max_bytes: int | None = None) -> list[tuple[str, str]]:
    """按渠道上限切片。返回 [(每片标题, 每片正文)]；首片带标题，续片带序号标记。

    ``max_chars`` / ``max_bytes`` 任一为 None 表示该维度不限；两个都给时
    必须同时满足才收片（CJK 文本下字节通常是更紧的那个约束）。
    """
    if max_chars is None and max_bytes is None:
        return [(title, text)]
    fits_chars = max_chars is None or len(title) + len(text) <= max_chars
    fits_bytes = max_bytes is None or (
        len(title.encode("utf-8")) + len(text.encode("utf-8")) <= max_bytes)
    if fits_chars and fits_bytes:
        return [(title, text)]
    # 首片给标题留位；续片给 "[续 i/N]" 头留位 —— 上限是硬约束，先扣头再装正文。
    cont_head = "[续 99/99]\n"
    head = max(len(title), len(cont_head)) + 1
    head_bytes = max(len(title.encode("utf-8")), len(cont_head.encode("utf-8"))) + 1
    body_cap = None if max_chars is None else max_chars - head
    body_bytes = None if max_bytes is None else max_bytes - head_bytes
    if (body_cap is not None and body_cap < 1) or (body_bytes is not None and body_bytes < 1):
        # 上限小到装不下标题本身属配置错误：返回单片让平台报错（可见），
        # 而不是在 range(step=0) 上抛 ValueError 把发送线程炸掉。
        return [(title, text)]
    chunks = _chunk_by_limits(text, body_cap, body_bytes)
    n = len(chunks)
    out = [(title, chunks[0])]
    out += [(f"[续 {i}/{n}]", c) for i, c in enumerate(chunks[1:], start=1)]
    return out


class Channel:
    """通道基类：子类给 ``_url`` + ``_payload``（或覆写 ``_request``），基类管分片与错。"""

    name = "base"
    max_chars: int | None = 4000  # 单片正文上限（字符）；None = 不限
    max_bytes: int | None = None  # 单片正文上限（UTF-8 字节）；None = 不限
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

    def configured(self) -> tuple[bool, str]:
        """(是否就绪, 说明)。供 ``lq notify status`` 做**真实**的自检。

        只看 ``_url()`` 不够：PushPlus 的 token 在 body 里（URL 恒定）、
        Telegram 还需要 chat_id —— 这类通道会把「未配置」误报成「已配置」，
        用户直到 ``lq notify test`` 才发现。子类按需覆写。
        """
        try:
            if not self._url():
                return False, "缺 webhook URL / token"
        except Exception as e:  # noqa: BLE001 - 自检本身不该抛
            return False, f"配置读取失败: {type(e).__name__}"
        return True, "ok"

    def send(self, title: str, text: str) -> SendResult:
        """分片发送。全部片成功才 ok；任何一片失败即停并回报该错误。

        两条硬契约：

        1. **永不抛异常** —— 所以 URL 拼接（含加签）与 ``Request`` 构造也
           必须在 try 内：URL 漏写 scheme 时 ``Request`` 会抛 ``ValueError``，
           穿出去就把 day_close / 日报主链路掀翻。
        2. **错误串不含凭证** —— URL 的 path/query 就是 key/token，而
           ``SendResult.error`` 会进 API 响应体、CLI 输出与持久化日志，
           所以一律过 ``_redact``。
        """
        # 统一走 configured() 自检，而不是只看 _url()：有些通道的「未配置」
        # 不体现在 URL 上（Telegram 的 chat_id 在 body、PushPlus 的 token 在
        # body），只看 url 会让它们发一次注定 400/903 的请求，把「没配」误报
        # 成「通道故障」（status 自检也跟着说谎）。子类覆写 configured() 即可。
        ready, why = self.configured()
        if not ready:
            return SendResult(self.name, ok=False, error=f"未配置（{why}）", skipped=True)
        url = self._url() or ""
        try:
            post_url = self._post_url(url)
            scheme = urllib.parse.urlparse(post_url).scheme
        except Exception as e:  # noqa: BLE001 - 加签/URL 解析失败也只是这条发不出去
            return SendResult(
                self.name, ok=False,
                error=f"webhook URL 非法: {_redact(str(e), url)}")
        if scheme not in ("http", "https"):
            return SendResult(
                self.name, ok=False,
                error=f"webhook URL 缺 scheme（需 http/https）: {_safe_url(url)}")
        slices = slice_text(title, text, self.max_chars, self.max_bytes)
        first_err: str | None = None
        delivered = 0
        for i, (t, chunk) in enumerate(slices):
            try:
                body, headers = self._request(t, chunk)
                req = urllib.request.Request(post_url, data=body, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                first_err = f"HTTP {e.code}: {str(e.reason)[:200]}"
                break
            except Exception as e:  # noqa: BLE001  网络层失败只降级不外抛
                first_err = _redact(f"{type(e).__name__}: {e}", url, post_url)
                break

            # 机器人协议的「业务失败」也是 HTTP 200 + errcode != 0，必须解析：
            # 不看 errcode 的话「key 错了」会被当成发送成功，告警静默丢失。
            first_err = _business_error(self.name, raw)
            if first_err:
                first_err = _redact(first_err, url, post_url)
                break
            delivered += 1
            if i < len(slices) - 1:
                time.sleep(self.slice_pause)
        return SendResult(self.name, ok=first_err is None, error=first_err,
                          sent_parts=delivered)


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
    max_chars = 1200
    # msgtype=text 的 content 上限是 2048 字节（4096 是 markdown 类型）。
    # 只按 1200 字符切，1185 个中文字就是 3585 字节，首片即被拒、后续片
    # 也不再发 —— 必须让字节上限参与切分。
    max_bytes = 2048

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

    def configured(self) -> tuple[bool, str]:
        """chat_id 缺了就发不出去（body 里会是空串 → 400），要能自检出来。"""
        ok, why = super().configured()
        if not ok:
            return ok, why
        if not _env("LQ_TELEGRAM_CHAT_ID"):
            return False, "缺 LQ_TELEGRAM_CHAT_ID"
        return True, "ok"

    def _url(self) -> str | None:
        token = _env("LQ_TELEGRAM_BOT_TOKEN")
        if not token:
            return None
        return f"https://api.telegram.org/bot{token}/sendMessage"

    def _payload(self, title: str, text: str) -> dict[str, Any]:
        return {"chat_id": _env("LQ_TELEGRAM_CHAT_ID") or "", "text": f"{title}\n{text}".strip()}


def _title_header(title: str) -> str:
    """把标题编码成可安全放进 HTTP header 的值（RFC 2047，非 ASCII 走 base64）。

    为什么不用 ``encode("ascii","ignore")``：那会把中文直接抹掉 —— 纯中文
    标题退化成 "lquant"，中英混排退化成残缺的空格串，用户端看到的标题是错的
    且无从察觉。ntfy 官方支持 UTF-8 标题，但 ``http.client`` 要求 header 值能
    用 latin-1 编码，中文裸塞会 ``UnicodeEncodeError`` 让整条通知发不出去；
    RFC 2047 的 ``=?UTF-8?B?..?=`` 既能承载 UTF-8 又可被 latin-1 编码，是
    header 里带非 ASCII 的标准做法（``email.header.decode_header`` 可逆）。

    自己拼 base64 而不用 ``email.header.Header``：Header 会按 maxlinelen
    折行并插入裸 ``\\n``，在 HTTP header 里非法，会被 ``http.client`` 拒绝。
    """
    # header 注入防线：CR/LF 一律折成空格，否则可以被用来伪造额外 header。
    flat = title.replace("\r", " ").replace("\n", " ").strip()
    if not flat:
        return "lquant"  # 空标题给个兜底，与旧行为一致
    if flat.isascii():
        return flat
    b64 = base64.b64encode(flat.encode("utf-8")).decode("ascii")
    return f"=?UTF-8?B?{b64}?="


class NtfyChannel(Channel):
    """ntfy 手机推送。env: LQ_NTFY_URL（含 topic，如 https://ntfy.sh/my-topic）
    [+ LQ_NTFY_TOKEN]。纯文本 POST + X-Title 头，零门槛的个人手机通知。"""

    name = "ntfy"
    # ntfy.sh 的消息体上限是 4096 字节（不是字符）：4000 个中文字 = 12000 字节，
    # 会被 413 拒掉，所以字节上限必须一起参与切分。
    max_bytes = 4096

    def _url(self) -> str | None:
        return _env("LQ_NTFY_URL")

    def _request(self, title: str, text: str) -> tuple[bytes, dict[str, str]]:
        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "X-Title": _title_header(title),
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
        # token 走 body 而不是 URL，所以「URL 恒定」不等于「已配置」：
        # 不检查 token 就会在未配置时也发一次注定 903 失败的请求，
        # 把「没配」误报成「通道故障」（status 自检也会跟着说谎）。
        if not _env("LQ_PUSHPLUS_TOKEN"):
            return None
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
