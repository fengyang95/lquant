"""响应封套：{ code, data, message, trace_id } 契约（设计文档 4.API 契约）。

封套只作用于新路由（EnvRoute 显式挂载）；既有裸返回路由不动，避免破坏既有测试。
错误经 EnvRoute 转成同构封套但保留 HTTP status_code —— 前端按 status 分支，body 走信封。
"""
from __future__ import annotations

import json

import pytest
from fastapi import APIRouter, FastAPI, HTTPException, Query
from fastapi.testclient import TestClient

from lquant.server.envelope import EnvRoute, err, ok


def _key_set(body: dict) -> set:
    return set(body)


def test_ok_contract():
    b = ok({"a": 1})
    assert _key_set(b) == {"code", "data", "message", "trace_id"}
    assert b["code"] == 0
    assert b["data"] == {"a": 1}
    assert b["message"] == "ok"
    assert isinstance(b["trace_id"], str) and b["trace_id"]


def test_ok_custom_message_and_trace_id():
    b = ok(42, message="计算完成", trace_id="T-1")
    assert b["data"] == 42 and b["message"] == "计算完成" and b["trace_id"] == "T-1"


def test_err_contract():
    b = err("出错了")
    assert _key_set(b) == {"code", "data", "message", "trace_id"}
    assert b["code"] == 1 and b["data"] is None and b["message"] == "出错了"


def test_trace_id_random():
    assert ok(1)["trace_id"] != ok(2)["trace_id"]


def _app() -> FastAPI:
    r = APIRouter(route_class=EnvRoute)

    @r.get("/plain")
    def plain() -> dict:
        return {"x": 1}

    @r.get("/none")
    def none() -> None:
        return None

    @r.get("/err")
    def boom() -> None:
        raise HTTPException(422, "参数不合法")

    @r.get("/checked")
    def checked(limit: int = Query(default=10, le=100)) -> dict:
        return {"limit": limit}

    app = FastAPI()
    app.include_router(r, prefix="/env")
    return app


def test_envroute_wraps_success():
    with TestClient(_app()) as c:
        body = c.get("/env/plain").json()
    assert body["code"] == 0 and body["data"] == {"x": 1}


def test_envroute_none_returns_data_null():
    with TestClient(_app()) as c:
        body = c.get("/env/none").json()
    assert body["code"] == 0 and body["data"] is None


def test_envroute_error_keeps_status_and_envelopes():
    with TestClient(_app(), raise_server_exceptions=False) as c:
        resp = c.get("/env/err")
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == 1 and body["message"] == "参数不合法"
    assert set(body) == {"code", "data", "message", "trace_id"}


def test_envroute_no_double_wrap():
    """已带 code 字段的 payload 不再二次包，避免嵌套信封。"""
    from lquant.server.envelope import make_router

    r = make_router()

    @r.get("/pre")
    def pre() -> dict:
        return ok(7)

    app = FastAPI()
    app.include_router(r)
    with TestClient(app) as c:
        body = c.get("/pre").json()
    assert body["code"] == 0 and body["data"] == 7
    # 未嵌套：message 保持 ok，data 没有「又包一层 code」
    assert isinstance(body["data"], int)


def test_envroute_no_stale_content_length():
    """封套后 body 变长，Content-Length 必须由 Starlette 重算，不得残留旧值
    （否则 uvicorn 下浏览器读到截断/错帧 body）。"""
    with TestClient(_app()) as c:
        resp = c.get("/env/plain")
        resp.read()
    assert resp.status_code == 200
    assert resp.headers.get("content-type", "").startswith("application/json")
    assert int(resp.headers["content-length"]) == len(resp.content)
    assert set(resp.json()) == {"code", "data", "message", "trace_id"}


def test_envroute_request_validation_error_keeps_422():
    """Query 越界 → FastAPI 的 RequestValidationError 须落成 422 封套（非 500），
    且不给客户端吐 pydantic 原始异常对象。"""
    with TestClient(_app(), raise_server_exceptions=False) as c:
        resp = c.get("/env/checked", params={"limit": 5000})
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == 1
    assert "RequestValidationError" not in resp.text         # 不泄内部异常类名
    assert set(body) == {"code", "data", "message", "trace_id"}