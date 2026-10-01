"""JSON-RPC 分发：方法别名、参数校验、错误码、流式信封。"""
from __future__ import annotations

import pytest

from lquant.agent.a2a.errors import A2AError
from lquant.agent.a2a.rpc import METHOD_ALIASES, dispatch
from lquant.agent.a2a.types import Message as A2AMessage
from lquant.agent.a2a.types import Part, Role, Task, TaskState, TaskStatus

_REQ = {"jsonrpc": "2.0", "id": 1, "method": "message/send",
        "params": {"message": {"messageId": "m1", "role": "ROLE_USER",
                               "parts": [{"text": "大盘怎么样"}]}}}


class _StubExecutor:
    """只记录调用、返回固定 Task 的执行体替身。"""

    def __init__(self, *, exc: Exception | None = None, frames=None) -> None:
        self.calls: list[tuple] = []
        self._exc = exc
        self._frames = frames or [{"statusUpdate": {"taskId": "t1",
                                                    "contextId": "c1",
                                                    "status": {"state": TaskState.WORKING},
                                                    "final": False}}]

    @staticmethod
    def _task(tid="t1", state=TaskState.COMPLETED) -> Task:
        return Task(id=tid, status=TaskStatus(state=state), contextId="c1")

    async def send(self, message: A2AMessage, context_id=None) -> Task:
        self.calls.append(("send", message.message_id, context_id))
        if self._exc:
            raise self._exc
        return self._task()

    async def open_stream(self, message: A2AMessage, context_id=None):
        self.calls.append(("open_stream", message.message_id, context_id))
        if self._exc:
            raise self._exc

        async def gen():
            for f in self._frames:
                yield f

        return gen()

    async def get(self, task_id: str, *, history_length=None) -> Task:
        self.calls.append(("get", task_id, history_length))
        if self._exc:
            raise self._exc
        return self._task(task_id)

    async def cancel(self, task_id: str) -> Task:
        self.calls.append(("cancel", task_id))
        if self._exc:
            raise self._exc
        return self._task(task_id, TaskState.CANCELED)


async def test_send_happy_path_returns_task_result():
    ex = _StubExecutor()
    out = await dispatch(_REQ, ex)
    assert out.stream is None
    assert out.body["jsonrpc"] == "2.0" and out.body["id"] == 1
    assert out.body["result"]["id"] == "t1"
    assert out.body["result"]["status"]["state"] == TaskState.COMPLETED
    assert ex.calls == [("send", "m1", None)]


async def test_context_id_taken_from_params():
    ex = _StubExecutor()
    req = {**_REQ, "params": {**_REQ["params"], "contextId": "ctx-1"}}
    await dispatch(req, ex)
    assert ex.calls == [("send", "m1", "ctx-1")]


_PARAMS_BY_METHOD = {
    "message/send": _REQ["params"],
    "message/stream": _REQ["params"],
    "tasks/get": {"id": "t1"},
    "tasks/cancel": {"id": "t1"},
}
_CALL_BY_METHOD = {"message/send": "send", "message/stream": "open_stream",
                   "tasks/get": "get", "tasks/cancel": "cancel"}


@pytest.mark.parametrize("method,canonical", sorted(METHOD_ALIASES.items()))
async def test_method_aliases_dispatch(method, canonical):
    """规范名与 SDK 的 PascalCase 名都要能路由到同一个实现。"""
    ex = _StubExecutor()
    req = {"jsonrpc": "2.0", "id": 1, "method": method,
           "params": _PARAMS_BY_METHOD[canonical]}
    out = await dispatch(req, ex)
    if canonical == "message/stream":
        assert out.stream is not None and out.body is None, method
    else:
        assert out.body.get("error") is None, method
    assert ex.calls[0][0] == _CALL_BY_METHOD[canonical]


async def test_stream_wraps_each_frame_in_jsonrpc_envelope():
    ex = _StubExecutor()
    req = {**_REQ, "id": "req-7", "method": "message/stream"}
    out = await dispatch(req, ex)
    assert out.body is None
    frames = [f async for f in out.stream]
    assert len(frames) == 1
    assert frames[0]["jsonrpc"] == "2.0" and frames[0]["id"] == "req-7"
    assert "statusUpdate" in frames[0]["result"]


async def test_stream_start_error_surfaces_before_sse_headers():
    """start 阶段的错误必须能作为 JSON-RPC error 返回（不能等到流里才发现）。"""
    ex = _StubExecutor(exc=A2AError(-32602, "contextId 不存在: x"))
    req = {**_REQ, "method": "message/stream"}
    out = await dispatch(req, ex)
    assert out.stream is None
    assert out.body["error"]["code"] == -32602


async def test_tasks_get_and_cancel():
    ex = _StubExecutor()
    out = await dispatch({"jsonrpc": "2.0", "id": 2, "method": "tasks/get",
                          "params": {"id": "t9", "historyLength": 3}}, ex)
    assert out.body["result"]["id"] == "t9"
    out = await dispatch({"jsonrpc": "2.0", "id": 3, "method": "CancelTask",
                          "params": {"taskId": "t9"}}, ex)
    assert out.body["result"]["status"]["state"] == TaskState.CANCELED
    assert ex.calls == [("get", "t9", 3), ("cancel", "t9")]


async def test_tasks_get_history_length_non_int_ignored():
    ex = _StubExecutor()
    await dispatch({"jsonrpc": "2.0", "id": 2, "method": "tasks/get",
                    "params": {"id": "t9", "historyLength": "5"}}, ex)
    assert ex.calls == [("get", "t9", None)]


@pytest.mark.parametrize("payload,code", [
    ([{"jsonrpc": "2.0"}], -32600),               # 批量请求不支持
    ("not-a-dict", -32600),
    ({"id": 1, "method": "message/send"}, -32600),          # 缺 jsonrpc
    ({"jsonrpc": "1.0", "id": 1, "method": "x"}, -32600),   # 版本不对
    ({"jsonrpc": "2.0", "id": 1}, -32600),                  # 缺 method
    ({"jsonrpc": "2.0", "id": 1, "method": "nope"}, -32601),
    ({"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": []}, -32602),
    ({"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {}}, -32602),
    ({"jsonrpc": "2.0", "id": 1, "method": "message/send",
      "params": {"message": {"role": "ROLE_USER", "parts": [{"text": "x"}]}}}, -32602),
    ({"jsonrpc": "2.0", "id": 1, "method": "tasks/cancel", "params": {}}, -32602),
])
async def test_protocol_errors(payload, code):
    out = await dispatch(payload, _StubExecutor())
    assert out.body["error"]["code"] == code
    assert out.stream is None


async def test_task_not_found_maps_to_minus_32001():
    ex = _StubExecutor(exc=A2AError(-32001, "任务不存在: x"))
    out = await dispatch({"jsonrpc": "2.0", "id": 1, "method": "tasks/get",
                          "params": {"id": "x"}}, ex)
    err = out.body["error"]
    assert err["code"] == -32001
    assert err["data"]["reason"] == "TaskNotFoundError"


async def test_unexpected_exception_becomes_internal_error():
    ex = _StubExecutor(exc=RuntimeError("boom"))
    out = await dispatch(_REQ, ex)
    assert out.body["error"]["code"] == -32603
    assert "boom" in out.body["error"]["message"]


async def test_null_params_defaults_to_empty_dict():
    ex = _StubExecutor()
    out = await dispatch({"jsonrpc": "2.0", "id": 1, "method": "message/send",
                          "params": None}, ex)
    assert out.body["error"]["code"] == -32602  # 缺 message


async def test_only_data_part_parses_at_rpc_layer_but_executor_rejects():
    """rpc 只做结构校验；「没有可用 text」的语义判断在 executor（见 test_a2a_executor）。"""
    ex = _StubExecutor()
    req = {**_REQ, "params": {"message": {"messageId": "m", "role": Role.USER,
                                          "parts": [{"data": {"k": 1}}]}}}
    out = await dispatch(req, ex)
    assert out.body["result"]["id"] == "t1"
    assert ex.calls == [("send", "m", None)]


async def test_part_validation_failure_maps_to_invalid_params():
    ex = _StubExecutor()
    req = {**_REQ, "params": {"message": {"messageId": "m", "role": Role.USER,
                                          "parts": [{"text": "a", "url": "http://x"}]}}}
    out = await dispatch(req, ex)
    assert out.body["error"]["code"] == -32602
    assert ex.calls == []


def test_parse_error_body():
    from lquant.agent.a2a.rpc import parse_error_body

    body = parse_error_body()
    assert body["error"]["code"] == -32700 and body["id"] is None


def test_a2a_message_constructible_from_wire():
    m = A2AMessage.model_validate({"messageId": "m", "role": Role.USER,
                                   "parts": [{"text": "x"}]})
    assert m.parts == [Part(text="x")]
