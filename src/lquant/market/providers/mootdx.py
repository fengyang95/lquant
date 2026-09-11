"""通达信 TCP 适配器（ADR-05：唯二"不封 IP"的源之一）。

看板热通路 / 1 分钟线首选。走通达信标准行情协议：realtime 快照 + 分钟线。
解析与网络分离 —— 网络薄壳 pragma: no cover，解析纯函数可离线单测。

mootdx quotes() 返回 { market, code, price, last_close, open, high, low,
vol, amount, ... } 的 dict 列表；字段机车协议无常量契约，解析做防御。
"""
from __future__ import annotations

import polars as pl

from lquant.data.base import DataProvider
from lquant.data.capability import Capability
from lquant.data.providers import PROVIDERS


def _tdx(records: list | dict) -> list:
    """兼容 mootdx 返回：list[dict] 或 {data: [...]}。"""
    if isinstance(records, dict):
        data = records.get("data")
        return data if isinstance(data, list) else []
    return records if isinstance(records, list) else []


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _tdx_code(symbol: str) -> str:
    return symbol.split(".")[0]


def parse_quotes(records: list | dict) -> pl.DataFrame:
    """mootdx quotes 记录 → realtime schema（与 tencent 对齐后可 crosscheck）。

    basis（如无则跳过）. symbol 由 code + market 语义重建。
    """
    rows: list[dict] = []
    for r in _tdx(records):
        code = r.get("code")
        price = _f(r.get("price"))
        if not code or price is None or price <= 0:
            continue
        try:
            market = int(r.get("market"))
        except (TypeError, ValueError):
            market = 1   # 缺省视为沪市（与 mootdx 默认一致）
        mkt = "SH" if market == 1 else "SZ"
        mkt = "SH" if market == 1 else "SZ"
        volume = _f(r.get("vol"))   # 已经股
        amount = _f(r.get("amount"))  # 已元
        rows.append({
            "symbol": f"{code}.{mkt}",
            "name": None,
            "last": price,
            "pre_close": _f(r.get("last_close")),
            "open": _f(r.get("open")),
            "high": _f(r.get("high")),
            "low": _f(r.get("low")),
            "volume": volume or 0.0,
            "amount": amount or 0.0,
            "pct_chg": None,
            "turnover_rate": None,
            "limit_up": None,
            "limit_down": None,
            "ts": None,
            "is_stale": False,
            "source": "mootdx",
        })
    return pl.DataFrame(rows) if rows else pl.DataFrame()


@PROVIDERS.register("mootdx", {"free": True, "need_token": False, "note": "通达信 TCP，不封 IP"})
class MootdxProvider(DataProvider):
    name = "mootdx"
    capability = frozenset({Capability.REALTIME, Capability.MINUTE_1})

    def __init__(self, qps: float = 0, capability: frozenset[Capability] | None = None) -> None:
        self.capability = capability or self.capability

    @staticmethod
    def _client():  # pragma: no cover - 网络
        from mootdx.quotes import Quotes

        return Quotes.factory(market="std")

    def realtime(self, symbols: list[str]) -> pl.DataFrame:  # pragma: no cover - 网络
        if not symbols:
            return pl.DataFrame()
        records = self._client().quotes(symbol=[_tdx_code(s) for s in symbols])
        return parse_quotes(records)

    def minute_bars(self, symbols: list[str], start, end, freq="1min"):  # pragma: no cover - 网络
        if freq != "1min":
            raise ValueError(f"mootdx 分钟线仅支持 1min，got {freq}")
        frames = []
        for sym in symbols:
            # frequency=FREQUENCY.index('1m')=8；9 是 day（get_frequency 按下标查）。
            # offset=窗口条数，0 → count0 返回空；用最近 800 根 1 分钟线。
            df = self._client().bars(
                symbol=_tdx_code(sym), frequency=8, start=0, offset=800).iloc[::-1]
            if df is None or df.empty:
                continue
            df = df.reset_index().rename(columns={
                "datetime": "ts", "open": "open", "high": "high",
                "low": "low", "close": "close", "vol": "volume", "amount": "amount"})
            d = df[["ts", "open", "high", "low", "close", "volume", "amount"]].copy()
            d["symbol"] = sym
            d["freq"] = freq
            d["source"] = "mootdx"
            frames.append(pl.from_pandas(d))
        return pl.concat(frames, how="diagonal") if frames else pl.DataFrame()

    def daily_bars(self, symbols, start, end):
        self.require(Capability.DAILY)
        raise NotImplementedError

    def adj_factors(self, symbols, start, end):
        self.require(Capability.ADJ_FACTOR)
        raise NotImplementedError

    def financial_pit(self, symbols, start, end):
        self.require(Capability.FINANCIAL_PIT)
        raise NotImplementedError

    def securities(self):
        self.require(Capability.REFERENCE)
        raise NotImplementedError

    def trade_calendar(self, start, end):
        self.require(Capability.CALENDAR)
        raise NotImplementedError

    def health(self) -> bool:  # pragma: no cover - 网络
        try:
            df = self.realtime(["600519.SH"])
            return len(df) == 1
        except Exception:  # noqa: BLE001
            return False