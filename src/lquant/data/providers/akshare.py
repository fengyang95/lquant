"""AkShare 适配器（补充源）。

东财系接口，覆盖最广但极易封 IP —— 所有请求走 TokenBucket 限流（qps 默认 3）。
能力集刻意收敛：不声明 FINANCIAL_PIT（新浪财务指标接口无 pub_date，未来函数风险），
不声明 CALENDAR（无干净接口）。

实现约束：
- akshare 一律**延迟导入**（方法内 import akshare as ak）；
  akshare 未安装时本模块 import 报 ImportError，由 providers.__init__._import_all
  吞掉不注册 —— 与 baostock 同机制。
- SDK 返回 pandas DataFrame，统一 pl.from_pandas 转 polars 后交引擎映射。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

import polars as pl

from lquant.core.types import parse_symbol
from lquant.data.capability import Capability
from lquant.data.normalize import normalize_60min_bounds, normalize_symbols
from lquant.data.providers import PROVIDERS
from lquant.data.providers._engine import MappingProvider
from lquant.data.ratelimit import TokenBucket

# freq（canonical）→ 东财 minute 接口 period 参数
_PERIOD_MAP = {"1min": "1", "5min": "5", "15min": "15", "30min": "30", "60min": "60"}

# stock_zh_a_hist / fund_etf_hist_em 的中文列
# 注：东财 daily 股票接口带「股票代码」，ETF 接口不带 —— symbol 缺列时
# 由 _select_daily 用循环变量注入，不依赖源返回该列。
_DAILY_COLS = ("日期", "股票代码", "开盘", "收盘", "最高", "最低",
               "成交量", "成交额")


def _select_daily(df: pl.DataFrame, symbol: str) -> pl.DataFrame:
    """选日线中文列；源未带「股票代码」列时注入（fund_etf_hist_em 的形态）。"""
    cols = [c for c in _DAILY_COLS if c in df.columns]
    out = df.select(cols)
    if "股票代码" not in out.columns:
        out = out.with_columns(pl.lit(symbol).alias("股票代码"))
    return out.with_columns(
        pl.col("日期").cast(pl.Utf8).str.to_date("%Y-%m-%d"),
        pl.col(["开盘", "收盘", "最高", "最低", "成交量", "成交额"])
        .cast(pl.Float64),
    )


def _to_code(symbol: str) -> str:
    """600000.SH → 600000（东财接口只吃 6 位代码）。"""
    return parse_symbol(symbol).code


def _from_pandas(df: Any) -> pl.DataFrame:
    """SDK pandas 结果 → polars；空/None 统一返回零列表。"""
    if df is None or len(df) == 0:
        return pl.DataFrame()
    return pl.from_pandas(df)


@PROVIDERS.register("akshare", {"free": True, "need_token": False})
class AkShareProvider(MappingProvider):
    name = "akshare"
    source = "akshare"
    capability = frozenset({
        Capability.DAILY, Capability.MINUTE_1, Capability.MINUTE_5,
        Capability.MINUTE_15, Capability.MINUTE_30, Capability.MINUTE_60,
        Capability.ADJ_FACTOR, Capability.REFERENCE, Capability.ETF_DAILY,
    })

    def __init__(self, qps: float = 3, capability: frozenset[Capability] | None = None) -> None:
        self._bucket = TokenBucket(qps)
        self.capability = capability or self.capability

    # ------------------------------------------------- MappingProvider 引擎
    def _fetch_raw(self, table: str, **params: Any) -> pl.DataFrame:
        if table == "daily_bar":
            return self._fetch_daily(**params)
        if table == "minute_bar":
            return self._fetch_minute(**params)
        raise NotImplementedError(f"akshare 不支持表 {table!r}")

    def _fetch_daily(
        self, symbols: list[str], start: date, end: date
    ) -> pl.DataFrame:
        """日线（不复权）：中文列名的源数据，映射交给 engine。"""
        import akshare as ak  # noqa: PLC0415  延迟导入：akshare 可选依赖

        frames = []
        for sym in symbols:
            self._bucket.acquire()
            raw = ak.stock_zh_a_hist(
                symbol=_to_code(sym),
                period="daily",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adjust="",
            )
            df = _from_pandas(raw)
            if df.is_empty():
                continue
            frames.append(_select_daily(df, sym))
        if not frames:
            return pl.DataFrame()
        return pl.concat(frames, how="diagonal")

    def _fetch_minute(
        self, symbols: list[str], start: date, end: date, freq: str = "1min"
    ) -> pl.DataFrame:
        """分钟线：时间列在本层解析为 ts（bar 结束时刻），映射交给 engine。"""
        period = _PERIOD_MAP.get(freq)
        if period is None:
            raise ValueError(f"akshare 分钟线不支持 {freq}")

        import akshare as ak  # noqa: PLC0415  延迟导入：akshare 可选依赖


        frames = []
        for sym in symbols:
            self._bucket.acquire()
            raw = ak.stock_zh_a_hist_min_em(
                symbol=_to_code(sym),
                period=period,
                start_date=f"{start} 09:30:00",
                end_date=f"{end} 15:00:00",
                adjust="",
            )
            df = _from_pandas(raw)
            if df.is_empty():
                continue
            frames.append(df.with_columns(
                pl.col("时间").cast(pl.Utf8).str.to_datetime("%Y-%m-%d %H:%M:%S").alias("ts"),
                pl.col(["开盘", "收盘", "最高", "最低", "成交量", "成交额"])
                .cast(pl.Float64),
            ).drop("时间"))
        if not frames:
            return pl.DataFrame()
        return pl.concat(frames, how="diagonal")

    def _post_normalize(self, df: pl.DataFrame, table: str) -> pl.DataFrame:
        """符号归一（600000 → 600000.SH）+ 分钟线 60min 边界归一 + ingested_at。

        60min 边界与 baostock 共用 normalize_60min_bounds：11:30 → 11:00。
        """
        df = normalize_symbols(df)
        if table == "minute_bar":
            df = normalize_60min_bounds(df)
            df = df.with_columns(
                ingested_at=pl.lit(datetime.now(), dtype=pl.Datetime),
            )
        return df

    # ------------------------------------------------------------ 核心方法
    def daily_bars(
        self, symbols: list[str], start: date, end: date
    ) -> pl.DataFrame:
        return self.request("daily_bar", symbols=symbols, start=start, end=end)

    def etf_daily_bars(
        self, symbols: list[str], start: date, end: date
    ) -> pl.DataFrame:
        """ETF 日线：fund_etf_hist_em，走 daily_bar 表 + sec_type=etf 覆盖。"""
        import akshare as ak  # noqa: PLC0415  延迟导入：akshare 可选依赖

        frames = []
        for sym in symbols:
            self._bucket.acquire()
            df = _from_pandas(ak.fund_etf_hist_em(
                symbol=_to_code(sym),
                period="daily",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adjust="",
            ))
            if df.is_empty():
                continue
            frames.append(_select_daily(df, sym))
        raw = pl.concat(frames, how="diagonal") if frames else pl.DataFrame()
        return self.request("daily_bar", _raw=raw, sec_type="etf")

    def minute_bars(
        self, symbols: list[str], start: date, end: date, freq: str = "1min"
    ) -> pl.DataFrame:
        return self.request(
            "minute_bar", symbols=symbols, start=start, end=end, freq=freq
        )

    def adj_factors(
        self, symbols: list[str], start: date, end: date
    ) -> pl.DataFrame:
        """复权因子 = 后复权收盘 / 不复权收盘（与 baostock 统一，factor ≥ 1）。

        akshare 不直接给因子，两次拉取相除（同 baostock 做法）；
        前复权在读取时用最新因子归一。
        """
        import akshare as ak  # noqa: PLC0415  延迟导入：akshare 可选依赖

        out = []
        for sym in symbols:
            self._bucket.acquire()
            hfq = _from_pandas(ak.stock_zh_a_hist(
                symbol=_to_code(sym), period="daily",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"), adjust="hfq",
            ))
            self._bucket.acquire()
            raw = _from_pandas(ak.stock_zh_a_hist(
                symbol=_to_code(sym), period="daily",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"), adjust="",
            ))
            if hfq.is_empty() or raw.is_empty():
                continue
            hfq_df = pl.DataFrame({
                "trade_date": hfq["日期"].cast(pl.Utf8).str.to_date("%Y-%m-%d"),
                "hfq_close": hfq["收盘"].cast(pl.Float64),
            })
            raw_df = pl.DataFrame({
                "trade_date": raw["日期"].cast(pl.Utf8).str.to_date("%Y-%m-%d"),
                "close": raw["收盘"].cast(pl.Float64),
            })
            # 按 trade_date inner join 对齐：两次拉取日期错位/缺行时不会
            # 位置错配产出错误因子，只丢对不上的日期。
            out.append(
                hfq_df.join(raw_df, on="trade_date", how="inner")
                      .with_columns(symbol=pl.lit(sym))
            )
        if not out:
            return pl.DataFrame()
        df = pl.concat(out).with_columns(
            # close==0（停牌等脏数据）回落 1.0：fail-soft，不因子化异常行
            factor=pl.when(pl.col("close") > 0)
                   .then(pl.col("hfq_close") / pl.col("close"))
                   .otherwise(1.0),
        )
        return normalize_symbols(df).select(
            "symbol", "trade_date", "factor", pl.lit("akshare").alias("source")
        )

    def securities(self) -> pl.DataFrame:
        """全市场 A 股代码-名称清单（仅 symbol/name，其余列由 parse_symbol 推断）。"""
        import akshare as ak  # noqa: PLC0415  延迟导入：akshare 可选依赖

        self._bucket.acquire()
        df = _from_pandas(ak.stock_info_a_code_name())
        if df.is_empty():
            return pl.DataFrame()
        df = df.with_columns(
            symbol=pl.col("code").cast(pl.Utf8),
            name=pl.col("name").cast(pl.Utf8),
        ).select("symbol", "name")
        df = normalize_symbols(df).with_columns(
            sec_type=pl.col("symbol").map_elements(
                lambda s: parse_symbol(s).sec_type.value, return_dtype=pl.Utf8),
            board=pl.col("symbol").map_elements(
                lambda s: parse_symbol(s).board.value, return_dtype=pl.Utf8),
            is_st=pl.col("name").cast(pl.Utf8).str.contains(r"ST"),
        )
        return df.select(
            "symbol", "name", "sec_type", "board", "is_st",
            pl.lit(None, dtype=pl.Date).alias("list_date"),
            pl.lit(None, dtype=pl.Date).alias("delist_date"),
            pl.lit("akshare").alias("source"),
        )
