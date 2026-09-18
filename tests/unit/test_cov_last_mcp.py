"""最后覆盖冲刺：mcp_server 工具处理器与 _handle_line 分支。"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import polars as pl

from lquant.agent import mcp_server as mcp


def test_tool_handlers_covered(monkeypatch):
    monkeypatch.setattr("lquant.market.ticks.fetch_quotes",
                        lambda symbols: [{"symbol": s} for s in symbols])
    df = pl.DataFrame({
        "symbol": ["600000.SH"],
        "trade_date": [__import__("datetime").date(2026, 1, 5)],
        "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
        "volume": [1.0], "amount": [1.0], "_factor": [0.5],
    })
    monkeypatch.setattr("lquant.data.store.parquet.read_daily",
                        lambda symbols, start, end: df.lazy())

    r = mcp.handle_request({"id": 1, "method": "tools/call", "params": {
        "name": "get_quotes", "arguments": {"symbols": ["600000.SH"]}}})
    assert r["result"]["isError"] is False

    r = mcp.handle_request({"id": 2, "method": "tools/call", "params": {
        "name": "get_daily", "arguments": {"symbol": "600000.SH", "days": 5}}})
    body = json.loads(r["result"]["content"][0]["text"])
    assert body and body[0]["trade_date"]

    r = mcp.handle_request({"id": 3, "method": "tools/call", "params": {
        "name": "list_factors", "arguments": {}}})
    assert r["result"]["isError"] is False

    r = mcp.handle_request({"id": 4, "method": "tools/call", "params": {
        "name": "get_factor_values",
        "arguments": {"symbol": "600000.SH", "names": ["KMID"]}}})
    out = json.loads(r["result"]["content"][0]["text"])
    assert out["KMID"][0]["_factor"] is not None


def test_call_tool_bad_args_and_unknown():
    # 参数名不匹配 → -32602
    r = mcp._call_tool("list_factors", {"bogus": 1})
    assert r["error"]["code"] == -32602
    # 未知工具 → -32602
    r = mcp._call_tool("nope", {})
    assert r["error"]["code"] == -32602


def test_handle_line_paths(capsys):
    # 空行 / 坏 JSON / 非对象 → 静默忽略
    for bad in ("", "   ", "not json", "[1, 2]"):
        mcp._handle_line(bad)
    # 合法请求 → stdout 写响应
    buf = io.StringIO()
    with redirect_stdout(buf):
        mcp._handle_line(json.dumps(
            {"id": 7, "method": "tools/list", "params": {}}))
    out = buf.getvalue().strip()
    resp = json.loads(out)
    assert resp["id"] == 7 and "tools" in resp["result"]
    capsys.readouterr()  # 排空 stderr
