"""实时行情快照源（东财 push2，走 em_get 限流）。

设计要点：
- 输出 dict 与 PaperEngine.push 的 quote 参数严格同构
  （symbol/price/limit_up/limit_down），模拟盘引擎不感知行情来自
  历史回放还是实时快照 —— 这是「回测/模拟盘同一套撮合语义」的落点。
- 涨跌停价不自己拍脑定比例，复用 backtest.rules 的 PriceLimit
  （与回测引擎同一份 config/rules/cn_a_share.yaml）。
- 停牌股：最新价缺失/为 0 → suspended=True，price=0，
  由 service 层跳过撮合并保持盯市价不变。
- 交易时段判断只做「钟面检查」；是否交易日以 trade_calendar 为准，
  但 tick 路径故意不碰 DuckDB（reader 是读写打开，与常驻进程互撞），
  非交易日拉到的是收盘后静态快照，结果等价于多次重复 push，无害。
"""
from __future__ import annotations

from datetime import time
from functools import lru_cache

from lquant.core.types import parse_symbol

# 东财 ulist 批量上限取 50：够用且 URL 不会过长
_BATCH = 50

_ULIST_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"

# A 股连续竞价时段 + 尾盘集合竞价（钟面时间，Asia/Shanghai）
_SESSIONS = ((time(9, 30), time(11, 30)), (time(13, 0), time(15, 0)))
_CLOSE_AUCTION = (time(14, 57), time(15, 0))


def in_trading_hours(ts=None) -> bool:
    """钟面是否在连续竞价时段内（不判断交易日）。"""
    t = (ts or now_cn_sh()).time()
    return any(s <= t <= e for s, e in _SESSIONS)


def in_close_auction(ts=None) -> bool:
    """钟面是否在尾盘集合竞价（14:57-15:00）。回测 same_close 口径的实盘对应下单窗口。"""
    t = (ts or now_cn_sh()).time()
    return _CLOSE_AUCTION[0] <= t <= _CLOSE_AUCTION[1]


def now_cn_sh():
    from lquant.core.types import now_cn
    return now_cn()


@lru_cache(maxsize=1)
def _ruleset():
    from lquant.backtest.rules.loader import load_ruleset
    return load_ruleset()


def limit_prices(symbol: str, name: str, pre_close: float) -> tuple[float, float] | None:
    """官方涨跌停价：与回测 `InstrumentRules.limit_up/limit_down` 同源。

    必须走 limit_up/limit_down，而不是自己 `pre_close*(1±pct)` 再 `round(,2)`：
    后者漏掉 tick 取整（股票 0.01 / 基金 0.001）、ETF 跟踪指数涨跌幅、
    ST 分板（创业板/科创板 ST 仍 20%）以及 no_price_limit 豁免 —— 模拟盘的
    「涨停堵单」判定会和回测不一致，对账时无法归因。
    """
    if not pre_close or pre_close <= 0:
        return None
    sym = parse_symbol(symbol)
    is_st = "ST" in (name or "").upper()
    rules = _ruleset().for_symbol(symbol, sym.sec_type, sym.board, is_st=is_st)
    up, down = rules.limit_up(pre_close), rules.limit_down(pre_close)
    if up is None or down is None:
        return None                      # no_price_limit：不设涨跌停约束
    return up, down


def _secid(symbol: str) -> str:
    """东财 secid：沪 1、深 0（北交所在 push2 列表接口下同样挂 0）。"""
    sym = parse_symbol(symbol)
    market = 1 if sym.exchange == "SH" else 0
    return f"{market}.{sym.code}"


def _parse_item(item: dict) -> dict | None:
    """单只快照 → quote dict。停牌/缺昨收的标的仍返回（供策略感知），price=0。"""
    code = str(item.get("f12") or "")
    if not code:
        return None
    # push2 里 f13 只有 1(沪)/0(深)；北交所也挂 0，按代码段区分
    if item.get("f13") == 1:
        exchange = "SH"
    elif code.startswith(("4", "8", "92")):
        exchange = "BJ"
    else:
        exchange = "SZ"
    symbol = f"{code}.{exchange}"
    name = item.get("f14", "")
    price, pre_close = _num(item.get("f2")), _num(item.get("f18"))
    suspended = price is None or price <= 0
    px = price if price and price > 0 else 0.0
    limits = limit_prices(symbol, name, pre_close or 0.0)
    return {
        "symbol": symbol,
        "name": name,
        "price": px,
        "pre_close": pre_close or 0.0,
        "limit_up": limits[0] if limits else None,
        "limit_down": limits[1] if limits else None,
        "suspended": suspended,
    }


def _num(v) -> float | None:
    """fltt=2 下正常是 float；停牌等场景东财返回 '-'。"""
    if isinstance(v, (int, float)):
        return float(v)
    return None


def fetch_snapshot(symbols: list[str]) -> list[dict]:
    """批量拉实时快照。走 em_get 令牌桶限流，每批 50 只。

    返回顺序与入参不保证一致；单只失败不影响整批。
    """
    from lquant.market.em_client import em_get

    out: list[dict] = []
    uniq = list(dict.fromkeys(symbols))
    for i in range(0, len(uniq), _BATCH):
        batch = uniq[i:i + _BATCH]
        secids = ",".join(_secid(s) for s in batch)
        resp = em_get(_ULIST_URL, params={
            "fltt": 2, "invt": 2,
            "fields": "f12,f13,f14,f2,f18",
            "secids": secids,
        })
        data = (resp.json() or {}).get("data") or {}
        for item in data.get("diff") or []:
            q = _parse_item(item)
            if q:
                out.append(q)
    return out
