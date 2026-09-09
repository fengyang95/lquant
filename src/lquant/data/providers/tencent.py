"""腾讯行情适配器（T0 源，ADR-05）。

qt.gtimg.cn HTTP 快照：不封 IP（simonlin1212/a-stock-data 实测表里
唯二标注"不封 IP"的源之一），延迟秒级 —— 看板热通路与自选股实时表首选。

用途（按设计文档 D4 / M1 / M2）：
- realtime：实时快照（与 market_snapshot 表对齐）
- reference 辅源：证券主数据补全（名称/上市状态），BaoStock 为主、腾讯对拍

实现约束：解析函数（_parse_quote）与网络请求严格分离 ——
解析可离线单测，网络薄壳 pragma: no cover。
"""
from __future__ import annotations

import re
from datetime import date, datetime

import polars as pl

from lquant.data.base import DataProvider
from lquant.data.capability import Capability
from lquant.data.providers import PROVIDERS

# v_sh600519="1~贵州茅台~600519~1700.00~..."; GBK 编码，~ 分隔
# 解析用带市场捕获的正则，~ 下标见下方常量
# ~ 分隔字段里我们关心的下标（0 起）
_IDX_NAME = 1
_IDX_PRICE = 3
_IDX_PRE_CLOSE = 4
_IDX_OPEN = 5
_IDX_VOLUME = 6          # 手
_IDX_TS = 30             # yyyymmddHHMMSS
_IDX_PCT_CHG = 32
_IDX_HIGH = 33
_IDX_LOW = 34
_IDX_AMOUNT = 37         # 万元
_IDX_TURNOVER = 38
_IDX_LIMIT_UP = 47
_IDX_LIMIT_DOWN = 48


def _to_float(s: str) -> float | None:
    # "0.00" 是腾讯对"停牌/无数据"价格的下发值 → 归一为 None（缺失）。
    # 仅用于价格/涨跌停字段；涨跌幅、换手率这些 0 是正常值，走 _to_num。
    if not s or s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_num(s: str) -> float | None:
    """同 _to_float 但保留 0 —— 供涨跌幅/换手率等 0 有意义的字段。"""
    if not s or s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _gtimg_code(symbol: str) -> str:
    """600519.SH -> sh600519；北交所 830799.BJ -> bj830799。"""
    code, ex = symbol.split(".")
    return f"{ex.lower()}{code}"


def parse_quotes(payload: str) -> pl.DataFrame:
    """解析 qt.gtimg.cn 批量快照文本 → realtime schema。

    v_sh600519 → 600519.SH。空报价（停牌/退市返回空串）整行跳过，
    不产出 NaN 垃圾。解析与网络分离，可离线单测。
    """
    pat = re.compile(r'v_(sh|sz|bj)(\d{6})="([^"]*)"')
    rows: list[dict] = []
    for m in pat.finditer(payload):
        mkt, code, body = m.group(1), m.group(2), m.group(3)
        f = body.split("~")
        # 只要求有价格；其余字段经 g()/len 防御 —— 腾讯短行情响应常见
        if len(f) <= _IDX_PRICE or not f[_IDX_PRICE]:
            continue
        ts_raw = f[_IDX_TS] if len(f) > _IDX_TS else ""
        try:
            ts = datetime.strptime(ts_raw, "%Y%m%d%H%M%S") if ts_raw else None
        except ValueError:
            ts = None

        def g(i: int) -> float | None:
            return _to_float(f[i]) if len(f) > i else None

        rows.append({
            "symbol": f"{code}.{mkt.upper()}",
            "name": f[_IDX_NAME],
            "last": g(_IDX_PRICE),
            "pre_close": g(_IDX_PRE_CLOSE),
            "open": g(_IDX_OPEN),
            "high": g(_IDX_HIGH),
            "low": g(_IDX_LOW),
            "volume": (g(_IDX_VOLUME) or 0.0) * 100.0,   # 手 → 股
            "amount": (g(_IDX_AMOUNT) or 0.0) * 1e4,     # 万元 → 元
            "pct_chg": _to_num(f[_IDX_PCT_CHG]) if len(f) > _IDX_PCT_CHG else None,
            "turnover_rate": _to_num(f[_IDX_TURNOVER]) if len(f) > _IDX_TURNOVER else None,
            "limit_up": g(_IDX_LIMIT_UP),
            "limit_down": g(_IDX_LIMIT_DOWN),
            "ts": ts,
            "is_stale": False,
            "source": "tencent",
        })
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows)


def _fetch_quotes(codes: list[str]) -> str:  # pragma: no cover - 网络
    import urllib.request

    url = f"https://qt.gtimg.cn/q={','.join(codes)}"
    req = urllib.request.Request(url, headers={"User-Agent": "lquant/0.1"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("gbk", errors="replace")


@PROVIDERS.register("tencent", {"free": True, "need_token": False, "note": "T0 源：不封 IP"})
class TencentProvider(DataProvider):
    name = "tencent"
    capability = frozenset({Capability.REALTIME, Capability.REFERENCE})

    def __init__(self, qps: float = 5, capability: frozenset[Capability] | None = None) -> None:
        self.capability = capability or self.capability

    def realtime(self, symbols: list[str]) -> pl.DataFrame:
        if not symbols:
            return pl.DataFrame()
        payload = _fetch_quotes([_gtimg_code(s) for s in symbols])
        return parse_quotes(payload)

    def securities(self) -> pl.DataFrame:
        """辅源清单：常见指数 + 无退市信息 —— 仅作 reference 兜底，
        退市/上市日以 BaoStock 为主源（幸存者偏差防护不能靠这里）。"""
        indices = ["sh000001", "sh000300", "sh000905", "sh000852",
                   "sz399001", "sz399006", "sh000688"]
        df = parse_quotes(_fetch_quotes(indices))
        if not len(df):
            return pl.DataFrame()
        return df.select(
            pl.col("symbol"),
            pl.col("name"),
        ).with_columns(
            sec_type=pl.lit("index"),
            board=pl.lit(None, dtype=pl.Utf8),
            list_date=pl.lit(None, dtype=pl.Date),
            delist_date=pl.lit(None, dtype=pl.Date),
            is_st=pl.lit(False),
            source=pl.lit("tencent"),
        )

    def trade_calendar(self, start: date, end: date) -> pl.DataFrame:
        self.require(Capability.CALENDAR)
        raise NotImplementedError

    def daily_bars(self, symbols, start, end):
        self.require(Capability.DAILY)
        raise NotImplementedError

    def minute_bars(self, symbols, start, end, freq):
        self.require(Capability.MINUTE_1)
        raise NotImplementedError

    def adj_factors(self, symbols, start, end):
        self.require(Capability.ADJ_FACTOR)
        raise NotImplementedError

    def financial_pit(self, symbols, start, end):
        self.require(Capability.FINANCIAL_PIT)
        raise NotImplementedError

    def health(self) -> bool:
        try:
            df = self.realtime(["600519.SH"])
            return len(df) == 1
        except Exception:  # noqa: BLE001
            return False
