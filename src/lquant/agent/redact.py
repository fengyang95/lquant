"""A2A 出站脱敏：黑名单键名 + 常见凭据形态 + 本机绝对路径。

为什么放在**出站层**（mapper）而不是解析层：内部事件要保真 —— ``/ask`` 页面与
其它本机消费方拿到的仍是未裁剪数据；只有跨信任边界的 A2A 帧需要脱敏。

口径是**黑名单**（2026-10-01 明确选择）：命中即整棵子树打码，其余原样透出。
安全性取决于黑名单覆盖是否完整 —— 新增敏感键名/凭据形态要同步这里。
"""
from __future__ import annotations

import re
from typing import Any

#: 统一打码占位符（键名命中 / 值形态命中）
MASK = "***"
#: 本机绝对路径的替换占位符
PATH_MASK = "<path>"

#: 键名黑名单：命中则整个值（含子树）打码。刻意不收裸 "auth"
#: —— 否则 "author"/"authority" 这类正常字段会被误伤。
_SENSITIVE_SUBSTR = (
    "token", "secret", "password", "passwd", "passphrase",
    "api_key", "api-key", "apikey",
    "credential", "private_key", "private-key",
    "access_key", "access-key", "authorization", "auth_token", "authtoken",
    "cookie",
)

#: 值里的凭据形态（即使键名看不出敏感，值本身也要打码）
_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{8,}\b"),
)

#: 本机绝对路径；``(?<![\w/])`` 避免在词中/协议斜杠后误匹配
_PATH_PATTERN = re.compile(
    r"(?<![\w/])(?:/(?:Users|home|root|var|tmp|opt|etc|private|Volumes|mnt|Applications)"
    r"(?:/[\w.@%+\-]+)+)")


def is_sensitive_key(key: str) -> bool:
    """键名是否命中黑名单（大小写不敏感的子串匹配）。"""
    k = str(key).lower()
    return any(s in k for s in _SENSITIVE_SUBSTR)


def redact_text(text: str) -> str:
    """脱敏一段自由文本：凭据形态 + 本机绝对路径。"""
    if not text:
        return text
    for rx in _VALUE_PATTERNS:
        text = rx.sub(MASK, text)
    return _PATH_PATTERN.sub(PATH_MASK, text)


def redact(value: Any, *, _key: str = "") -> Any:
    """递归脱敏任意 JSON 结构；``_key`` 为外层键名（命中则整棵打码）。"""
    if _key and is_sensitive_key(_key):
        return MASK
    if isinstance(value, dict):
        return {k: redact(v, _key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
