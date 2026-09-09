"""AkShare 适配器（东财系，封 IP 头号风险源）。

覆盖广但易封 —— 强制限流，且只用它做**独有数据**：
- minute_1：BaoStock 没有 1 分钟线（D1 补缺）
- ETF 元数据（fund_etf_spot_em 一次 1600+ 只含 IOPV，D9）
- 复权因子列（stock_zh_a_daily 的 hfq_factor/qfq_factor，D3 权威因子源）
- 日线/日历（BaoStock 的 fallback）

财务：akshare 财报无随公告的 pub_date 时序，无法满足 PIT ——
capability 不声明，financial_pit 直接报错，绝不伪造。
"""
from __future__ import annotations

from datetime import date

import polars as pl

from lquant.data.base import DataProvider
from lquant.data.capability import Capability
from lquant.data.normalize import normalize_symbols, rename_columns
from lquant.data.providers import PROVIDERS


def _ak_code(symbol: str) -> str:
    return symbol.split(".")[0]


@PROVIDERS.register("akshare", {"free": True, "need_token": False,
                                "note": "东财系，易封 IP，仅独有数据"})
class AkShareProvider(DataProvider):
    name = "akshare"
    capability = frozenset({
        Capability.DAILY, Capability.MINUTE_1, Capability.MINUTE_5,
        Capability.MINUTE_15, Capability.MINUTE_30, Capability.MINUTE_60,
        Capability.ETF_DAILY, Capability.ETF_META, Capability.ETF_SPOT,
        Capability.ETF_IOPV, Capability.ADJ_FACTOR, Capability.CALENDAR,
        Capability.REFERENCE, Capability.MONEY_FLOW, Capability.LIMIT_UP,
        Capability.DRAGON_TIGER, Capability.SECTOR,
    })

    def __init__(self, qps: float = 3, capability: frozenset[Capability] | None = None) -> None:
        self.capability = capability or self.capability

    @staticmethod
    def _ak():
        import akshare as ak
        return ak

    def daily_bars(self, symbols: list[str], start: date, end: date) -> pl.DataFrame:
        frames = []
        for sym in symbols:
            df = self._ak().stock_zh_a_hist(
                symbol=_ak_code(sym), period="daily",
                start_date=start.isoformat(), end_date=end.isoformat(), adjust="",
            )
            if df is None or df.empty:
                continue
            df = rename_columns(pl.from_pandas(df))
            if "volume" in df.columns:
                df = df.with_columns(pl.col("volume") * 100)   # 手 → 股，湖 schema 单位
            frames.append(df.with_columns(symbol=pl.lit(sym)))
        if not frames:
            return pl.DataFrame()
        return normalize_symbols(pl.concat(frames, how="diagonal"))

    def minute_bars(self, symbols: list[str], start: date, end: date,
                    freq: str) -> pl.DataFrame:
        period = {"1min": "1", "5min": "5", "15min": "15",
                  "30min": "30", "60min": "60"}.get(freq)
        if not period:
            raise ValueError(f"akshare 不支持 {freq}")
        frames = []
        for sym in symbols:
            df = self._ak().stock_zh_a_hist_min_em(
                symbol=_ak_code(sym), start_date=start.isoformat(),
                end_date=end.isoformat(), period=period, adjust="",
            )
            if df is None or df.empty:
                continue
            df = rename_columns(pl.from_pandas(df))
            if "volume" in df.columns:
                df = df.with_columns(pl.col("volume") * 100)   # 手 → 股
            frames.append(df.with_columns(symbol=pl.lit(sym), freq=pl.lit(freq)))
        if not frames:
            return pl.DataFrame()
        return normalize_symbols(pl.concat(frames, how="diagonal"))

    def adj_factors(self, symbols: list[str], start: date, end: date) -> pl.DataFrame:
        """复权因子列（stock_zh_a_daily, adjust='hfq-factor'）。

        返回 schema：symbol / trade_date / factor（后复权因子）。
        前复权因子 = factor / 最新 factor（设计文档 §3 归一约定）。
        """
        frames = []
        for sym in symbols:
            df = self._ak().stock_zh_a_daily(
                symbol=_ak_code(sym), start_date=start.isoformat(),
                end_date=end.isoformat(), adjust="hfq-factor",
            )
            if df is None or df.empty:
                continue
            df = pl.from_pandas(df).rename({"date": "trade_date", "hfq_factor": "factor"})
            # 防御：stock_zh_a_daily 的 df 是 DatetimeIndex reset，trade_date 是
            # Datetime 而非 String —— .str.to_date 会 SchemaError。cast 到 Date。
            frames.append(df.select(["trade_date", "factor"]).with_columns(
                pl.col("trade_date").cast(pl.Date),
                symbol=pl.lit(sym)))
        if not frames:
            return pl.DataFrame()
        return pl.concat(frames, how="diagonal")

    def etf_meta(self) -> pl.DataFrame:
        fp = self._ak().fund_etf_spot_em()
        if fp is None or fp.empty:
            return pl.DataFrame()
        df = pl.from_pandas(fp)
        # 幂等：换列名 → 归一 symbol → 数量级（symbol 单位是份？用盯盘口径）
        return df

    def securities(self) -> pl.DataFrame:
        df = self._ak().stock_info_a_code_name()
        if df is None or df.empty:
            return pl.DataFrame()
        out = pl.from_pandas(df).rename({"code": "symbol", "name": "name"})
        out = normalize_symbols(out)
        return out.select("symbol", "name").with_columns(
            sec_type=pl.lit("stock"),
            board=pl.lit(None, dtype=pl.Utf8),
            list_date=pl.lit(None, dtype=pl.Date),
            delist_date=pl.lit(None, dtype=pl.Date),
            is_st=pl.lit(False),
            source=pl.lit("akshare"),
        )

    def trade_calendar(self, start: date, end: date) -> pl.DataFrame:
        df = self._ak().tool_trade_date_hist_sina()
        if df is None or df.empty:
            return pl.DataFrame()
        out = pl.from_pandas(df).rename({"trade_date": "trade_date"}).select("trade_date")
        return out.with_columns(
            pl.col("trade_date").cast(pl.Date),
            is_open=pl.lit(True),
            exchange=pl.lit("SSE"),
        )

    # ---------------- 无能力：显式报错 ----------------
    def financial_pit(self, symbols, start, end):
        # akshare 财报缺公告日时序，无法满足 PIT —— 明说，不让上层拿有瑕疵的财务数
        self.require(Capability.FINANCIAL_PIT)
        raise NotImplementedError