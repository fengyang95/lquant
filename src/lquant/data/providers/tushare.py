"""Tushare 适配器（pro 接口，需 token）。

积分档位决定可用接口：daily / stock_basic / trade_cal / adj_factor /
fund_daily 普通档即可；stk_mins（分钟线）需 2000 积分 —— 积分不足时
SDK 报权限类错误，本层捕获转成 SourceUnavailable，fallback 链可切源，
绝不报 CapabilityMissing（能力声明与账号档位是两回事）。

实现约束：
- tushare 一律**延迟导入**（方法内 import tushare as ts）；
  未安装时本模块 import 报 ImportError，由 providers.__init__._import_all
  吞掉不注册 —— 与 baostock / akshare 同机制。
- SDK 返回 pandas DataFrame，统一转 polars 后交引擎映射。
- 单位：daily / fund_daily 的 vol 是手（×100 → 股）、amount 是千元
  （×1000 → 元）；日线换算在本文件 fetch 侧用 normalize.scale_unit 完成，
  系数声明见 VOLUME_UNIT / AMOUNT_UNIT（此前在 mapping yaml derive 做）。
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from lquant.core.errors import SourceUnavailable
from lquant.core.types import now_cn_naive
from lquant.data.capability import Capability
from lquant.data.normalize import normalize_symbols, scale_unit
from lquant.data.providers import PROVIDERS
from lquant.data.providers._engine import MappingProvider
from lquant.data.ratelimit import TokenBucket

# 日线量纲考证（tushare pro 官方文档 daily/fund_daily 字段说明）：vol 单位是
# **手**（1 手 = 100 股）、amount 单位是**千元** —— 换算在 fetch 侧出帧处做，
# 入湖统一为 volume=股、amount=元（与 baostock/akshare 同口径）。
VOLUME_UNIT = "手"
AMOUNT_UNIT = "千元"

# stk_mins 的 freq 参数与 canonical freq 同名：1min/5min/15min/30min/60min
_FREQ_MAP = frozenset({"1min", "5min", "15min", "30min", "60min"})

# 权限类报错关键词（积分不足 / 未开通）：命中即转 SourceUnavailable 可切源
_PERMISSION_KEYWORDS = ("积分", "权限", "每天最多", "抱歉", "没有接口访问权限")

# 财务三大表：item 前缀用接口名，防不同表同名列混淆
_FINANCIAL_APIS = ("income", "balancesheet", "cashflow")

# 报告期月份 → 季度序号（1~4；Q4 即年报）
_REPORT_QUARTER = {3: 1, 6: 2, 9: 3, 12: 4}


def _from_pandas(df: Any) -> pl.DataFrame:
    """SDK pandas 结果 → polars；空/None 统一返回零列表。"""
    if df is None or len(df) == 0:
        return pl.DataFrame()
    return pl.from_pandas(df)


def _to_date_col(df: pl.DataFrame, col: str) -> pl.DataFrame:
    """tushare 日期列 YYYYMMDD → pl.Date。"""
    if col not in df.columns:
        return df
    return df.with_columns(
        pl.col(col).cast(pl.Utf8).str.to_date("%Y%m%d")
    )


def _prepare_daily(df: pl.DataFrame) -> pl.DataFrame:
    """daily / fund_daily 出帧：日期解析 + 量价 cast + 量纲归一。

    vol=手 → ×100 股、amount=千元 → ×1000 元（VOLUME_UNIT / AMOUNT_UNIT），
    入湖统一口径（schema: volume=股、amount=元）。
    """
    return scale_unit(
        scale_unit(
            _to_date_col(df, "trade_date").with_columns(
                pl.col(["open", "high", "low", "close", "pre_close",
                        "vol", "amount"]).cast(pl.Float64),
            ),
            col="vol", unit=VOLUME_UNIT,
        ),
        col="amount", unit=AMOUNT_UNIT,
    )


def _report_type_of(stat_date: date) -> str:
    """报告期 → 带年季的 report_type（如 2024Q1），与 baostock 统一。"""
    q = _REPORT_QUARTER.get(stat_date.month, 1)  # 非报告期末月按 Q1 fail-soft
    return f"{stat_date.year}Q{q}"


def _wide_to_long(df: pl.DataFrame, api: str) -> pl.DataFrame:
    """财务宽表 → FINANCIAL_PIT 长表。

    - end_date → stat_date、ann_date → pub_date
    - item = f"{api}.{列名}"
    - 无 ann_date 的行**整行丢弃**（未来函数防护，宁可丢数据）
    - 数值列逐列拆行；日期/标识列不拆
    """
    id_cols = {"ts_code", "end_date", "ann_date"}
    stat = df["end_date"]
    pub = df["ann_date"] if "ann_date" in df.columns else None
    recs: list[dict[str, Any]] = []
    for row_idx in range(df.height):
        stat_d, pub_d = stat[row_idx], (pub[row_idx] if pub is not None else None)
        if stat_d is None or pub_d is None:
            continue  # 无公告日 → 无法防未来函数 → 丢弃
        for col in df.columns:
            if col in id_cols:
                continue
            val = df[col][row_idx]
            if val is None:
                continue
            try:
                fv = float(val)
            except (TypeError, ValueError):
                continue
            recs.append({
                "symbol": df["ts_code"][row_idx],
                "stat_date": stat_d,
                "pub_date": pub_d,
                "report_type": _report_type_of(stat_d),
                "item": f"{api}.{col}",
                "value": fv,
                "unit": None,
                "source": "tushare",
                "ingested_at": now_cn_naive(),
            })
    return pl.DataFrame(recs) if recs else pl.DataFrame()


@PROVIDERS.register("tushare", {"free": False, "need_token": True})
class TushareProvider(MappingProvider):
    name = "tushare"
    source = "tushare"
    capability = frozenset({
        Capability.DAILY, Capability.MINUTE_1, Capability.MINUTE_5,
        Capability.MINUTE_15, Capability.MINUTE_30, Capability.MINUTE_60,
        Capability.ADJ_FACTOR, Capability.FINANCIAL_PIT, Capability.REFERENCE,
        Capability.CALENDAR, Capability.ETF_DAILY,
    })

    def __init__(
        self,
        token: str | None = None,
        qps: float = 1,
        capability: frozenset[Capability] | None = None,
    ) -> None:
        from lquant.core.env import load_env_files

        # 直接构造（绕过 build_chain）时也能找到 .env 里的 token：
        # 包目录 = providers.py 上三级（src/lquant/）
        load_env_files(Path.cwd() / ".env",
                       Path(__file__).resolve().parents[2] / ".env")
        self._token = token or os.environ.get("TUSHARE_TOKEN", "")
        self._bucket = TokenBucket(qps)
        self.capability = capability or self.capability
        self._client: Any = None

    def _pro(self) -> Any:
        """惰性创建 pro_api 客户端（tushare 延迟导入）。"""
        if self._client is None:
            import tushare as ts  # noqa: PLC0415  可选依赖，延迟导入

            self._client = ts.pro_api(self._token)
        return self._client

    # 瞬时错误关键词（小写匹配）：网络/超时/每分钟限流才重试，
    # 参数错、数据形状错（pandas→polars 转换失败）重试也不会好。
    _RETRY_KEYWORDS = (
        "timeout", "timed out", "connection", "reset", "eof", "ssl",
        "每分钟", "频率", "超过", "rate",
    )
    _RATE_LIMIT_KEYWORDS = ("每分钟", "频率", "rate")

    def _call(self, api_name: str, **kw: Any) -> pl.DataFrame:
        """限流 + 调用 + pandas→polars；权限类报错转 SourceUnavailable。

        瞬时错误重试：网络/超时退避 1-2-4s，每分钟限流类退避 20/60s
        （tushare 分钟级窗口短退避没用）。
        """
        import time

        from loguru import logger

        attempts = 3
        for attempt in range(attempts):
            self._bucket.acquire()
            try:
                api = getattr(self._pro(), api_name)
                return _from_pandas(api(**kw))
            except Exception as e:  # noqa: BLE001
                if any(k in str(e) for k in _PERMISSION_KEYWORDS):
                    raise SourceUnavailable(
                        self.name, f"{api_name} 权限不足（积分档位）: {e}"
                    ) from e
                msg = str(e).lower()
                retryable = any(k in msg for k in self._RETRY_KEYWORDS)
                if not retryable or attempt == attempts - 1:
                    raise
                delay = (20, 60)[attempt] if any(
                    k in str(e) for k in self._RATE_LIMIT_KEYWORDS) else 2**attempt
                logger.warning(
                    f"tushare {api_name} 第 {attempt + 1} 次失败"
                    f"（{type(e).__name__}: {e}），{delay}s 后重试"
                )
                time.sleep(delay)

    # ------------------------------------------------- MappingProvider 引擎
    def _fetch_raw(self, table: str, **params: Any) -> pl.DataFrame:
        if table == "daily_bar":
            return self._fetch_daily(**params)
        if table == "minute_bar":
            return self._fetch_minute(**params)
        raise NotImplementedError(f"tushare 不支持表 {table!r}")

    def _fetch_daily(
        self, symbols: list[str], start: date, end: date
    ) -> pl.DataFrame:
        """pro.daily：ts_code 与内部格式一致，源列名 df，映射交给 engine。"""
        frames = []
        for sym in symbols:
            df = self._call(
                "daily",
                ts_code=sym,
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
            )
            if df.is_empty():
                continue
            frames.append(_prepare_daily(df))
        if not frames:
            return pl.DataFrame()
        return pl.concat(frames, how="diagonal")

    def _fetch_minute(
        self, symbols: list[str], start: date, end: date, freq: str = "1min"
    ) -> pl.DataFrame:
        """pro.stk_mins：freq 校验先于 SDK import；trade_time → ts。"""
        if freq not in _FREQ_MAP:
            raise ValueError(f"tushare 分钟线不支持 {freq}")
        frames = []
        for sym in symbols:
            df = self._call(
                "stk_mins",
                ts_code=sym,
                freq=freq,
                start_date=f"{start} 09:00:00",
                end_date=f"{end} 16:00:00",
            )
            if df.is_empty():
                continue
            frames.append(df.with_columns(
                pl.col(["open", "high", "low", "close", "vol", "amount"])
                .cast(pl.Float64),
                ts=pl.col("trade_time").cast(pl.Utf8)
                   .str.to_datetime("%Y-%m-%d %H:%M:%S"),
            ).drop("trade_time"))
        if not frames:
            return pl.DataFrame()
        return pl.concat(frames, how="diagonal")

    def _post_normalize(self, df: pl.DataFrame, table: str) -> pl.DataFrame:
        """符号归一 + 分钟线 ingested_at。"""
        df = normalize_symbols(df)
        if table == "minute_bar":
            df = df.with_columns(
                ingested_at=pl.lit(now_cn_naive(), dtype=pl.Datetime),
            )
        return df

    # ------------------------------------------------------------ 核心方法
    def daily_bars(
        self, symbols: list[str], start: date, end: date
    ) -> pl.DataFrame:
        return self.request("daily_bar", symbols=symbols, start=start, end=end)

    def minute_bars(
        self, symbols: list[str], start: date, end: date, freq: str = "1min"
    ) -> pl.DataFrame:
        return self.request(
            "minute_bar", symbols=symbols, start=start, end=end, freq=freq
        )

    def etf_daily_bars(
        self, symbols: list[str], start: date, end: date
    ) -> pl.DataFrame:
        """ETF 日线：fund_daily，列结构同 daily，走 daily_bar + sec_type=etf。"""
        frames = []
        for sym in symbols:
            df = self._call(
                "fund_daily",
                ts_code=sym,
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
            )
            if df.is_empty():
                continue
            frames.append(_prepare_daily(df))
        raw = pl.concat(frames, how="diagonal") if frames else pl.DataFrame()
        return self.request("daily_bar", _raw=raw, sec_type="etf")

    def adj_factors(
        self, symbols: list[str], start: date, end: date
    ) -> pl.DataFrame:
        """pro.adj_factor 直出（ts_code/trade_date/adj_factor），无需相除。"""
        frames = []
        for sym in symbols:
            df = self._call(
                "adj_factor",
                ts_code=sym,
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
            )
            if df.is_empty():
                continue
            frames.append(
                _to_date_col(df, "trade_date")
                .with_columns(pl.col("adj_factor").cast(pl.Float64))
                .select(
                    pl.col("ts_code").alias("symbol"),
                    "trade_date",
                    pl.col("adj_factor").alias("factor"),
                )
            )
        if not frames:
            return pl.DataFrame()
        df = pl.concat(frames, how="diagonal")
        return normalize_symbols(df).select(
            "symbol", "trade_date", "factor", pl.lit("tushare").alias("source")
        )

    def daily_basic(self, trade_date: date) -> pl.DataFrame:
        """pro.daily_basic：一个交易日全市场一行（市值/估值/股本）。

        单位换算与 DAILY_BAR 注释口径一致：total_mv/circ_mv 万元 → 元，
        total_share/float_share 万股 → 股；pb → pb_mrq 对齐日线湖列名。
        """
        from lquant.data.schema import SCHEMAS

        df = self._call("daily_basic", trade_date=trade_date.strftime("%Y%m%d"))
        if df.is_empty():
            return pl.DataFrame(schema=SCHEMAS["daily_basic"])
        df = _to_date_col(df, "trade_date").with_columns(
            pl.col(["close", "turnover_rate", "pe_ttm", "pb", "ps_ttm", "dv_ttm",
                    "total_mv", "circ_mv", "total_share", "float_share"])
            .cast(pl.Float64),
        )
        out = df.select(
            pl.col("ts_code").alias("symbol"),
            "trade_date",
            "close",
            "turnover_rate",
            "pe_ttm",
            pl.col("pb").alias("pb_mrq"),
            "ps_ttm",
            (pl.col("total_mv") * 1e4).alias("total_mv"),
            (pl.col("circ_mv") * 1e4).alias("float_mv"),
            "dv_ttm",
            (pl.col("total_share") * 1e4).alias("total_share"),
            (pl.col("float_share") * 1e4).alias("float_share"),
            pl.lit("tushare").alias("source"),
        )
        return normalize_symbols(out)

    def financial_pit(
        self, symbols: list[str], start: date, end: date,
        kinds: tuple[str, ...] = ("profit", "balance", "cashflow"),
    ) -> pl.DataFrame:
        """PIT 财务：三大报表宽表 → 长表，item 带 f"{接口名}.{列名}" 前缀。

        end_date + ann_date 双日期天然 PIT；无 ann_date 的行丢弃
        （未来函数防护，宁可丢数据）。
        kinds 与 baostock 口径对齐：profit→income、balance→balancesheet、
        cashflow→cashflow；dupont/growth/operation/indicator 统一拉
        fina_indicator（140+ 财务指标，含 ann_date；去重后只拉一次）。
        item 前缀落 indicator.*（对齐 get_fundamentals DSL 口径；
        baostock 源的 dupont.*/growth.*/operation.* 前缀在本源不再保留，
        DSL 字段名以 _KIND_PREFIX 映射为准）。
        """
        kind_to_api = {
            "profit": "income",
            "balance": "balancesheet",
            "cashflow": "cashflow",
            "indicator": "fina_indicator",
            # baostock 口径的衍生季表在 tushare 统一并入 fina_indicator
            "dupont": "fina_indicator",
            "growth": "fina_indicator",
            "operation": "fina_indicator",
        }
        apis = tuple(dict.fromkeys(kind_to_api[k] for k in kinds if k in kind_to_api))
        if not apis:
            return pl.DataFrame()
        out: list[pl.DataFrame] = []
        for sym in symbols:
            for api in apis:
                df = self._call(
                    api,
                    ts_code=sym,
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                )
                if df.is_empty():
                    continue
                df = _to_date_col(df, "end_date")
                df = _to_date_col(df, "ann_date")
                # item 前缀对齐 get_fundamentals DSL 口径：fina_indicator
                # 落成 indicator.*（_KIND_PREFIX["indicator"]="indicator"）
                long = _wide_to_long(
                    df, "indicator" if api == "fina_indicator" else api)
                if not long.is_empty():
                    out.append(long)
        if not out:
            return pl.DataFrame()
        return normalize_symbols(pl.concat(out, how="diagonal"))

    # ------------------------------------------------------------ 参考数据
    def securities(self) -> pl.DataFrame:
        """stock_basic：L/D/P 三种 list_status 全拉，天然含退市（防幸存者偏差）。"""
        frames = []
        for status in ("L", "D", "P"):
            df = self._call(
                "stock_basic",
                exchange="",
                list_status=status,
                fields="ts_code,name,list_date,delist_date,list_status",
            )
            if df.is_empty():
                continue
            frames.append(df.with_columns(
                symbol=pl.col("ts_code").cast(pl.Utf8),
                name=pl.col("name").cast(pl.Utf8),
                list_date=pl.col("list_date").cast(pl.Utf8, strict=False)
                    .str.to_date("%Y%m%d"),
                delist_date=pl.col("delist_date").cast(pl.Utf8, strict=False)
                    .str.to_date("%Y%m%d"),
            ))
        if not frames:
            return pl.DataFrame()
        df = pl.concat(frames, how="diagonal")
        return df.select(
            "symbol", "name",
            pl.lit("stock", dtype=pl.Utf8).alias("sec_type"),
            pl.lit(None, dtype=pl.Utf8).alias("board"),
            pl.lit(False, dtype=pl.Boolean).alias("is_st"),
            "list_date", "delist_date",
            pl.lit("tushare").alias("source"),
        )

    def trade_calendar(self, start: date, end: date) -> pl.DataFrame:
        """pro.trade_cal（exchange=SSE）：cal_date/is_open → trade_date/is_open。"""
        df = self._call(
            "trade_cal",
            exchange="SSE",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        if df.is_empty():
            return pl.DataFrame()
        return df.with_columns(
            trade_date=pl.col("cal_date").cast(pl.Utf8).str.to_date("%Y%m%d"),
            is_open=pl.col("is_open").cast(pl.Utf8) == "1",
            exchange=pl.lit("SSE"),
        ).select("trade_date", "is_open", "exchange")
