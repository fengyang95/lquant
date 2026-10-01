"""内置只读 stdio MCP server（零依赖，供 claude CLI 经 --mcp-config 拉起）。

协议：JSON-RPC 2.0，换行分隔（每行一个请求/响应），stdout 只输出响应，
日志一律走 stderr。仅实现 initialize / tools/list / tools/call。

工具**全部只读**，且都直接调函数、不走 HTTP（不要求 API 服务在跑）：

行情 / 日线 / 因子
- get_quotes(symbols)                        实时快照（lquant.market.ticks.fetch_quotes）
- get_daily(symbol, days=60)                 日线（lquant.data.store.parquet.read_daily）
- list_factors()                             内置 Alpha158 因子清单
- get_factor_values(symbol, names, days=60)  日线 + qlib_alpha.compute → _factor 列

大盘 / 板块 / 资金流（复用 server/api 的纯逻辑函数，避免两套口径）
- get_market_overview()                      情绪分 + 涨跌停 + 北向
- get_market_breadth(days=60)                涨跌家数（基于日线截面统计）
- get_sectors(kind="industry")               板块行情（industry/concept/area）
- get_money_flow(top=20, symbol="")          全市场资金流 Top / 单票历史
- get_limit_up(limit=50)                     涨停池
- get_dragon_tiger(limit=50)                 龙虎榜
- get_heat(top=15)                           热榜（涨/跌/放量/龙虎榜）
- get_index_quotes(days=20)                  主要指数行情 + 近 N 日收盘
- get_etf_list(limit=200)                    ETF 元数据（跟踪指数/规模/申赎 T+N）

⚠️ 这些函数内部用 ``reader()`` 打开 DuckDB，而 reader 是**读写模式**连接：
   主服务正在写库时可能撞锁（bounded retry 之后仍失败会退化成空数据）。
   读不到时按空态返回，不要把它当成「市场真的没有数据」。
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
    """end=today、start=today-days（read_daily 内部转 ISO 字符串即可）。

    用 today_cn()：读的是交易日数据，服务器时区非 Asia/Shanghai 时
    date.today() 会让窗口整体偏移一天（最近一根日线被切掉）。
    """
    from datetime import timedelta

    from lquant.core.types import today_cn

    end = today_cn()
    return end - timedelta(days=days), end


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


def _market():
    """延迟导入 market 路由模块，直接调它的纯逻辑函数（不经 HTTP）。

    刻意复用而不是另写一套：``/api/market/*`` 的口径（涨跌停阈值、空态处理、
    单位）只能有一处定义，否则 MCP 与 HTTP 会给出不一致的数字。

    注意**必须显式传参**：那些函数的默认值是 FastAPI 的 ``Query(...)`` 对象，
    漏传会把 Query 实例当业务值传下去。
    """
    from lquant.server.api import market

    return market


def _tool_market_overview() -> dict:
    return _market().overview()


def _tool_market_breadth(days: int = 60) -> dict:
    return _market().breadth(days=days)


def _tool_sectors(kind: str = "industry") -> list[dict]:
    return _market().sectors(kind=kind)


def _tool_money_flow(top: int = 20, symbol: str = "") -> list[dict]:
    return _market().money_flow(top=top, symbol=symbol.strip() or None)


def _tool_limit_up(limit: int = 50) -> list[dict]:
    return _market().limit_up(limit=limit)


def _tool_dragon_tiger(limit: int = 50) -> list[dict]:
    return _market().dragon_tiger(limit=limit)


def _tool_heat(top: int = 15) -> dict:
    return _market().heat(top=top)


def _tool_index_quotes(days: int = 20) -> list[dict]:
    return _market().index_quotes(days=days)


def _tool_etf_list(limit: int = 200) -> list[dict]:
    from lquant.server.api.etf import _meta_rows

    return _meta_rows(limit=limit)


TOOL_HANDLERS: dict[str, Callable[..., Any]] = {
    "get_quotes": _tool_get_quotes,
    "get_daily": _tool_get_daily,
    "list_factors": _tool_list_factors,
    "get_factor_values": _tool_get_factor_values,
    "get_market_overview": _tool_market_overview,
    "get_market_breadth": _tool_market_breadth,
    "get_sectors": _tool_sectors,
    "get_money_flow": _tool_money_flow,
    "get_limit_up": _tool_limit_up,
    "get_dragon_tiger": _tool_dragon_tiger,
    "get_heat": _tool_heat,
    "get_index_quotes": _tool_index_quotes,
    "get_etf_list": _tool_etf_list,
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
    {
        "name": "get_market_overview",
        "description": "大盘概览：情绪分 + 涨跌停家数 + 破板率 + 北向资金（看板首屏）",
        "inputSchema": _input_schema({}, []),
    },
    {
        "name": "get_market_breadth",
        "description": "市场宽度：逐日涨跌家数、涨跌停家数近似、中位涨跌幅、总成交额（基于日线截面）",
        "inputSchema": _input_schema(
            {"days": {"type": "integer", "default": 60, "minimum": 1,
                      "maximum": 250, "description": "回看的交易日数"}},
            [],
        ),
    },
    {
        "name": "get_sectors",
        "description": "板块行情（按涨跌幅降序），kind=industry 行业 / concept 概念 / area 地域",
        "inputSchema": _input_schema(
            {"kind": {"type": "string", "enum": ["industry", "concept", "area"],
                      "default": "industry", "description": "板块类型"}},
            [],
        ),
    },
    {
        "name": "get_money_flow",
        "description": "资金流：不传 symbol 返回最新交易日全市场主力净流入 Top；传 symbol 返回该票近 30 日历史",
        "inputSchema": _input_schema(
            {"top": {"type": "integer", "default": 20, "minimum": 1, "maximum": 200,
                     "description": "全市场榜单条数（传 symbol 时忽略）"},
             "symbol": {"type": "string", "default": "",
                        "description": "证券代码，如 600519；留空查全市场 Top"}},
            [],
        ),
    },
    {
        "name": "get_limit_up",
        "description": "涨停池：最新交易日涨停个股（含首次封板时间/连板数/所属行业）",
        "inputSchema": _input_schema(
            {"limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500}},
            [],
        ),
    },
    {
        "name": "get_dragon_tiger",
        "description": "龙虎榜：最新交易日上榜个股与上榜原因",
        "inputSchema": _input_schema(
            {"limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500}},
            [],
        ),
    },
    {
        "name": "get_heat",
        "description": "热榜：当日涨幅/跌幅/放量 Top 与龙虎榜要点（一句话答「今天盘面看点」）",
        "inputSchema": _input_schema(
            {"top": {"type": "integer", "default": 15, "minimum": 1, "maximum": 100}},
            [],
        ),
    },
    {
        "name": "get_index_quotes",
        "description": "主要指数最新行情（收盘/涨跌）与近 N 日收盘序列，用于回答大盘走势",
        "inputSchema": _input_schema(
            {"days": {"type": "integer", "default": 20, "minimum": 1, "maximum": 250}},
            [],
        ),
    },
    {
        "name": "get_etf_list",
        "description": "ETF 元数据列表：跟踪指数、类型、是否跨境、申赎 T+N、管理费、规模",
        "inputSchema": _input_schema(
            {"limit": {"type": "integer", "default": 200, "minimum": 1, "maximum": 1000}},
            [],
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
