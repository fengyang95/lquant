"""内部统一 schema（Polars）。

所有 Provider 的输出必须归一化到这里的列名与单位。
不同源列名/单位混用是静默错误的温床，所以在 adapter 层归一 + 断言。
"""
from __future__ import annotations

import polars as pl

# ---- 日线 ----
DAILY_BAR = {
    "symbol": pl.Utf8,        # 000001.SZ
    "trade_date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "pre_close": pl.Float64,
    "volume": pl.Float64,     # 股
    "amount": pl.Float64,     # 元（源站万元/亿元必须换算）
    "turnover_rate": pl.Float64,
    "adj_factor": pl.Float64,
    "pct_chg": pl.Float64,        # 当日涨跌幅%（源站 pctChg）
    "is_st": pl.Boolean,          # ST/*ST（源站 isST）
    "is_suspended": pl.Boolean,   # 停牌标记（行保留不再丢）
    "pe_ttm": pl.Float64,         # 滚动市盈率（peTTM）
    "pb_mrq": pl.Float64,         # 市净率（pbMRQ）
    "ps_ttm": pl.Float64,         # 滚动市销率（psTTM）
    "pcf_ncf_ttm": pl.Float64,    # 滚动市现率（pcfNcfTTM）
    # 总市值（元）——目标态：baostock 不出总股本，恒 NULL，
    # 待 tushare daily_basic 增强或 security join 填充
    "total_mv": pl.Float64,
    "float_mv": pl.Float64,       # 流通市值（元，close×volume/turn 推导）
    "sec_type": pl.Utf8,
    "quality_flags": pl.Int32,   # 位掩码；0 = 干净
    "source": pl.Utf8,
    "ingested_at": pl.Datetime,
    "data_version": pl.Utf8,
}

# ---- 分钟线 ----
MINUTE_BAR = {
    "symbol": pl.Utf8,
    "ts": pl.Datetime,
    "freq": pl.Utf8,          # 1min / 5min / ...
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
    "adj_factor": pl.Float64,
    "source": pl.Utf8,
    "ingested_at": pl.Datetime,
}

# ---- PIT 财务：双日期是防未来函数的数据层基础 ----
FINANCIAL_PIT = {
    "symbol": pl.Utf8,
    "stat_date": pl.Date,     # 报告期
    "pub_date": pl.Date,      # 公告日 —— 回测只能用 pub_date <= t
    "report_type": pl.Utf8,   # 带年季：2024Q1 / 2024Q2 / 2024Q3 / 2024Q4（年报）
    "item": pl.Utf8,
    "value": pl.Float64,
    "unit": pl.Utf8,
    "source": pl.Utf8,
    "ingested_at": pl.Datetime,
}

# ---- 参考数据 ----
SECURITY = {
    "symbol": pl.Utf8,
    "name": pl.Utf8,
    "sec_type": pl.Utf8,
    "board": pl.Utf8,
    "list_date": pl.Date,
    "delist_date": pl.Date,     # 幸存者偏差防护
    "is_st": pl.Boolean,
    "source": pl.Utf8,
}

# 行业分类带生效日：用今天的分类回测十年前 = 前视偏差
INDUSTRY = {
    "symbol": pl.Utf8,
    "std": pl.Utf8,           # sw1 / cics / em
    "code": pl.Utf8,
    "name": pl.Utf8,
    "std_date": pl.Date,
    "source": pl.Utf8,
}

ETF_META = {
    "symbol": pl.Utf8,
    "name": pl.Utf8,
    "track_index": pl.Utf8,
    "fund_type": pl.Utf8,        # qdii / commodity / bond / money / equity
    "is_cross_border": pl.Boolean,
    "sellable_after_days": pl.Int8,   # per-instrument，比按 sec_type 更细
    "management_fee": pl.Float64,
    "custody_fee": pl.Float64,
    "fund_size": pl.Float64,
    "share_outstanding": pl.Float64,
    "as_of": pl.Date,
    "source": pl.Utf8,
}

MARKET_SNAPSHOT = {
    "symbol": pl.Utf8,
    "ts": pl.Datetime,
    "last": pl.Float64,
    "pct_chg": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
    "iopv": pl.Float64,        # ETF 实时估值
    "discount": pl.Float64,    # 折溢价率
    "is_stale": pl.Boolean,    # 僵尸报价标志：停牌/未更新不能当真实价
    "source": pl.Utf8,
}

SCHEMAS = {
    "daily_bar": DAILY_BAR,
    "minute_bar": MINUTE_BAR,
    "financial_pit": FINANCIAL_PIT,
    "security": SECURITY,
    "industry_classify": INDUSTRY,
    "etf_meta": ETF_META,
    "market_snapshot": MARKET_SNAPSHOT,
}


def empty(name: str) -> pl.DataFrame:
    return pl.DataFrame(schema=SCHEMAS[name])


def coerce(df: pl.DataFrame, name: str) -> pl.DataFrame:
    """把 df 强转到目标 schema（缺列补空，类型统一）。"""
    target = SCHEMAS[name]
    cols = []
    for c, dt in target.items():
        if c in df.columns:
            cols.append(_cast_to_schema(df[c], dt))
        else:
            cols.append(pl.lit(None, dtype=dt).alias(c))
    return df.with_columns(cols).select(list(target))


def _cast_to_schema(s: pl.Series, dt: pl.DataType) -> pl.Series:
    """cast 到 schema dtype；Utf8 → Boolean 走 "1"/"true" 白名单（polars 不支持直转）。"""
    if dt == pl.Boolean and s.dtype == pl.Utf8:
        return (
            s.cast(pl.Utf8).str.to_lowercase().str.strip_chars()
            .is_in(["1", "true", "t", "yes"])
        )
    return s.cast(dt, strict=False)
