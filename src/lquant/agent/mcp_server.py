"""内置只读 stdio MCP server（零依赖，供 claude CLI 经 --mcp-config 拉起）。

协议：JSON-RPC 2.0，换行分隔（每行一个请求/响应），stdout 只输出响应，
日志一律走 stderr。仅实现 initialize / tools/list / tools/call。

工具全部只读：
- get_quotes(symbols)                        实时快照（lquant.market.ticks.fetch_quotes）
- get_daily(symbol, days=60)                 日线（lquant.data.store.parquet.read_daily）
- list_factors()                             内置 Alpha158 因子清单
- get_factor_values(symbol, names, days=60)  日线 + qlib_alpha.compute → _factor 列
"""
from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("lquant.mcp")
logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "lquant-mcp"


def _tool_get_quotes(symbols: list[str]) -> list[dict]:
    from lquant.market.ticks import fetch_quotes

    return fetch_quotes(symbols)


def _read_daily(symbols: list[str], start=None, end=None):
    from lquant.data.store.parquet import read_daily

    return read_daily(symbols, start, end)


def _compute(df, name: str):
    from lquant.factors.qlib_alpha import compute

    return compute(df, name)


def _end_date(days: int):
    """end=today、start=today-days（read_daily 内部转 ISO 字符串即可）。"""
    from datetime import date, timedelta

    return date.today() - timedelta(days=days), date.today()


def _tool_get_daily(symbol: str, days: int = 60) -> list[dict]:
    start, end = _end_date(days)
    df = _read_daily([symbol], start, end).collect()
    df = df.sort("trade_date")
    return [
        {
            "trade_date": str(r["trade_date"]),
            "open": r["open"], "high": r["high"],
            "low": r["low"], "close": r["close"],
            "volume": r["volume"], "amount": r["amount"],
        }
        for r in df.iter_rows(named=True)
    ]


def _tool_list_factors() -> list[dict]:
    from lquant.factors.qlib_alpha import list_builtin

    return list_builtin()


def _tool_get_factor_values(symbol: str, names: list[str], days: int = 60) -> dict:
    start, end = _end_date(days)
    df = _read_daily([symbol], start, end).collect().sort("trade_date")
    out: dict[str, list[dict]] = {}
    for name in names:
        fdf = _compute(df, name)
        out[name] = [
            {"trade_date": str(r["trade_date"]), "_factor": r["_factor"]}
            for r in fdf.iter_rows(named=True)
        ]
    return out


TOOL_HANDLERS: dict[str, Callable[..., Any]] = {
    "get_quotes": _tool_get_quotes,
    "get_daily": _tool_get_daily,
    "list_factors": _tool_list_factors,
    "get_factor_values": _tool_get_factor_values,
}


def _input_schema(properties: dict[str, dict], required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required}


TOOLS_SPEC: list[dict] = [
    {
        "name": "get_quotes",
        "description": "获取一组 A 股标的的实时行情快照（价格/涨跌幅/时间戳）",
        "inputSchema": _input_schema(
            {"symbols": {"type": "array", "items": {"type": "string"},
                          "description": "证券代码列表，如 000001.SZ"}},
            ["symbols"],
        ),
    },
    {
        "name": "get_daily",
        "description": "获取单只标的最近 N 天日线（OHLCV+amount，按日期升序）",
        "inputSchema": _input_schema(
            {"symbol": {"type": "string", "description": "证券代码"},
             "days": {"type": "integer", "default": 60, "minimum": 1,
                      "maximum": 250}},
            ["symbol"],
        ),
    },
    {
        "name": "list_factors",
        "description": "列出全部内置 Alpha158 因子（name/family/window/formula）",
        "inputSchema": _input_schema({}, []),
    },
    {
        "name": "get_factor_values",
        "description": "计算单只标的近 N 天的若干内置因子值（按因子名分组）",
        "inputSchema": _input_schema(
            {"symbol": {"type": "string", "description": "证券代码"},
             "names": {"type": "array", "items": {"type": "string"},
                       "description": "因子名列表，如 MA20/KMID"},
             "days": {"type": "integer", "default": 60, "minimum": 1,
                      "maximum": 250}},
            ["symbol", "names"],
        ),
    },
]


def _json_text(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _call_tool(name: str, args: dict) -> dict:
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return {"error": {"code": -32602, "message": f"未知工具: {name}"}}
    try:
        data = handler(**args)
    except TypeError as e:
        # 参数名/个数不匹配也归入无效工具参数
        return {"error": {"code": -32602, "message": f"无效参数: {e}"}}
    except Exception as e:  # noqa: BLE001 - 统一转 -32000
        logger.exception("工具 %s 执行失败", name)
        return {"error": {"code": -32000, "message": f"工具执行失败: {e}"}}
    return {"result": {"content": [{"type": "text", "text": _json_text(data)}],
                       "isError": False}}


def handle_request(req: dict) -> dict:
    """单个 JSON-RPC 请求 → 响应 dict（纯函数，便于测试）。"""
    rid = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": rid,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": "0.1.0"},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS_SPEC}}
    if method == "tools/call":
        resp = _call_tool(params.get("name", ""), params.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": rid, **resp}

    return {
        "jsonrpc": "2.0", "id": rid,
        "error": {"code": -32601, "message": f"未知 method: {method}"},
    }


def _handle_line(line: str) -> None:
    """处理一行输入：坏行忽略（日志记 stderr），合法则写 stdout 并 flush。"""
    line = line.strip()
    if not line:
        return
    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        logger.warning("忽略非 JSON 行: %.80s", line)
        return
    if not isinstance(req, dict) or "method" not in req:
        logger.warning("忽略非法请求行: %.80s", line)
        return
    resp = handle_request(req)
    if resp.get("id") is None and "error" not in resp:
        return
    sys.stdout.write(json.dumps(resp, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def main() -> None:
    logger.info("lquant-mcp 启动（stdio，%d 个工具）", len(TOOL_HANDLERS))
    for raw in sys.stdin:
        _handle_line(raw)


if __name__ == "__main__":
    main()
