"""A2A / JSON-RPC 2.0 错误码与异常。

码值取自 A2A 1.0 规范：JSON-RPC 标准段 -32700..-32603，A2A 语义段 -32001..-32009。
"""
from __future__ import annotations

# JSON-RPC 2.0 标准段
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# A2A 语义段
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002
UNSUPPORTED_OPERATION = -32004

#: 语义码 → A2A 错误类型名（放在 error.data.reason，供调用方按类型分支）
REASON_NAMES = {
    TASK_NOT_FOUND: "TaskNotFoundError",
    TASK_NOT_CANCELABLE: "TaskNotCancelableError",
    UNSUPPORTED_OPERATION: "UnsupportedOperationError",
}


class A2AError(Exception):
    """对应一个 JSON-RPC error 响应。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_error(self) -> dict:
        err: dict = {"code": self.code, "message": self.message}
        if reason := REASON_NAMES.get(self.code):
            err["data"] = {"reason": reason, "domain": "a2a-protocol.org"}
        return err


class SessionBusyError(A2AError):
    """同一 contextId 已有在跑的回答（映射自 AgentService 的 409）。"""

    def __init__(self, message: str) -> None:
        super().__init__(INVALID_REQUEST, message)
