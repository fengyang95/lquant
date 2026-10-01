"""内置 stdio MCP server 的 JSON-RPC 分发单测（不拉真数据）。"""
from __future__ import annotations

import json

import pytest

from lquant.agent import mcp_server


def _req(method: str, params: dict | None = None, _id: int | str | None = 1) -> dict:
    r: dict = {"jsonrpc": "2.0", "id": _id, "method": method}
    if params is not None:
        r["params"] = params
    return r


def test_initialize_returns_capabilities_and_server_info():
    resp = mcp_server.handle_request(_req("initialize"))
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert "error" not in resp
    res = resp["result"]
    assert res["protocolVersion"] == "2024-11-05"
    assert "tools" in res["capabilities"]
    assert res["serverInfo"]["name"] == "lquant-mcp"


def test_tools_list_covers_market_and_factor_tools_with_schema():
    resp = mcp_server.handle_request(_req("tools/list"))
    tools = resp["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == set(mcp_server.TOOL_HANDLERS)
    # a-stock-data skill 覆盖的大盘/板块/资金流/涨停/龙虎榜/热榜/指数/ETF 都要有对应工具
    assert {"get_market_overview", "get_market_breadth", "get_sectors", "get_money_flow",
            "get_limit_up", "get_dragon_tiger", "get_heat", "get_index_quotes",
            "get_etf_list"} <= names
    for t in tools:
        assert t["inputSchema"]["type"] == "object"
        assert "properties" in t["inputSchema"]


def test_market_tools_delegate_to_market_module_with_explicit_args(monkeypatch):
    """MCP 工具必须显式传参直调 market 路由函数。

    那些函数的默认值是 FastAPI 的 ``Query(...)`` 对象 —— 漏传就会把 Query 实例
    当业务值传下去（历史陷阱）。这里用哨兵断言每个工具都真的传了值。
    """
    from lquant.server.api import market

    calls: list[tuple] = []

    def spy(name):
        def inner(*a, **kw):
            calls.append((name, a, kw))
            return []
        return inner

    for fn in ("overview", "breadth", "sectors", "money_flow", "limit_up",
               "dragon_tiger", "heat", "index_quotes"):
        monkeypatch.setattr(market, fn, spy(fn))

    mcp_server._tool_market_overview()
    mcp_server._tool_market_breadth()
    mcp_server._tool_sectors()
    mcp_server._tool_money_flow()
    mcp_server._tool_limit_up()
    mcp_server._tool_dragon_tiger()
    mcp_server._tool_heat()
    mcp_server._tool_index_quotes()

    got = {name for name, _a, _kw in calls}
    assert got == {"overview", "breadth", "sectors", "money_flow", "limit_up",
                   "dragon_tiger", "heat", "index_quotes"}
    for name, args, kwargs in calls:
        assert not args, f"{name} 不应传位置参数"
        assert all(not hasattr(v, "default") for v in kwargs.values()), \
            f"{name} 传了 Query 默认值"
    assert dict((n, kw) for n, _a, kw in calls)["money_flow"] == {"top": 20, "symbol": None}
    assert dict((n, kw) for n, _a, kw in calls)["sectors"] == {"kind": "industry"}


def test_etf_list_uses_meta_rows(monkeypatch):
    from lquant.server.api import etf

    monkeypatch.setattr(etf, "_meta_rows", lambda **kw: [{"symbol": "510300.SH", **kw}])
    assert mcp_server._tool_etf_list(limit=5) == [{"symbol": "510300.SH", "limit": 5}]


def test_unknown_tool_returns_minus_32602():
    resp = mcp_server.handle_request(
        _req("tools/call", {"name": "nope", "arguments": {}}))
    assert resp["error"]["code"] == -32602


def test_unknown_method_returns_minus_32601():
    resp = mcp_server.handle_request(_req("resources/list"))
    assert resp["error"]["code"] == -32601


def test_get_quotes_stubbed(monkeypatch):
    def fake(symbols):
        return [{"symbol": s, "price": 10.0} for s in symbols]

    monkeypatch.setitem(mcp_server.TOOL_HANDLERS, "get_quotes", fake)
    resp = mcp_server.handle_request(
        _req("tools/call", {"name": "get_quotes",
                            "arguments": {"symbols": ["000001.SZ"]}}))
    content = resp["result"]["content"]
    assert content[0]["type"] == "text"
    data = json.loads(content[0]["text"])
    assert data == [{"symbol": "000001.SZ", "price": 10.0}]
    assert resp["result"]["isError"] is False


def test_tool_execution_error_returns_minus_32000(monkeypatch):
    def boom(symbols):
        raise RuntimeError("downstream down")

    monkeypatch.setitem(mcp_server.TOOL_HANDLERS, "get_quotes", boom)
    resp = mcp_server.handle_request(
        _req("tools/call", {"name": "get_quotes", "arguments": {"symbols": []}}))
    assert resp["error"]["code"] == -32000


def test_get_factor_values_stubbed(monkeypatch):
    """compute 返回带 _factor 列的 df → 序列化成 [{date, value}] 结构。"""
    import polars as pl

    def fake_read_daily(symbols, start=None, end=None):
        return pl.DataFrame({
            "trade_date": ["2026-01-05", "2026-01-06"],
            "symbol": symbols * 2,
            "open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0],
            "close": [1.0, 2.0], "volume": [1.0, 2.0], "amount": [1.0, 2.0],
        }).lazy()

    def fake_compute(df, name):
        assert name == "KMID"
        return df.with_columns((pl.col("close") - pl.col("open")).alias("_factor"))

    monkeypatch.setitem(mcp_server.TOOL_HANDLERS, "get_factor_values",
                        mcp_server._tool_get_factor_values)
    monkeypatch.setattr(mcp_server, "_read_daily", fake_read_daily)
    monkeypatch.setattr(mcp_server, "_compute", fake_compute)
    resp = mcp_server.handle_request(
        _req("tools/call", {"name": "get_factor_values",
                            "arguments": {"symbol": "000001.SZ",
                                          "names": ["KMID"]}}))
    data = json.loads(resp["result"]["content"][0]["text"])
    assert data["KMID"] == [
        {"trade_date": "2026-01-05", "_factor": 0.0},
        {"trade_date": "2026-01-06", "_factor": 0.0},
    ]


@pytest.mark.parametrize("bad", ['not json', '{"jsonrpc": "2.0"}', ""])
def test_main_loop_ignores_bad_lines(bad, capsys):
    """坏行（非 JSON / 缺 method / 空行）被忽略，不写 stdout。"""
    mcp_server._handle_line(bad)
    out = capsys.readouterr().out
    assert out == ""


def test_main_loop_responds_valid_line(capsys):
    mcp_server._handle_line(json.dumps(_req("initialize")))
    out = capsys.readouterr().out
    resp = json.loads(out.strip())
    assert resp["result"]["serverInfo"]["name"] == "lquant-mcp"
    assert out.endswith("\n")
