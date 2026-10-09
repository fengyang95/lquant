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

# ---- daily_basic（tushare 每日指标，全市场一行/交易日）----
# 用途：补日线湖 total_mv/float_mv 空洞 + pe/pb/ps 备援；市值单位统一为**元**
# （tushare 源头是万元，入库前 ×1e4，与 DAILY_BAR 注释口径一致）。
DAILY_BASIC = {
    "symbol": pl.Utf8,
    "trade_date": pl.Date,
    "close": pl.Float64,
    "turnover_rate": pl.Float64,   # %，换手率（自由流通口径 turnover_rate_f 不存）
    "pe_ttm": pl.Float64,
    "pb_mrq": pl.Float64,          # 源字段 pb
    "ps_ttm": pl.Float64,
    "total_mv": pl.Float64,        # 元（源 total_mv 万元 ×1e4）
    "float_mv": pl.Float64,        # 元（源 circ_mv 万元 ×1e4）
    "dv_ttm": pl.Float64,          # %，滚动股息率
    "total_share": pl.Float64,     # 股（源万股 ×1e4）
    "float_share": pl.Float64,     # 股（源万股 ×1e4）
    "source": pl.Utf8,
}

SCHEMAS = {
    "daily_bar": DAILY_BAR,
    "daily_basic": DAILY_BASIC,
    "minute_bar": MINUTE_BAR,
    "financial_pit": FINANCIAL_PIT,
    "security": SECURITY,
    "industry_classify": INDUSTRY,
    "etf_meta": ETF_META,
    "market_snapshot": MARKET_SNAPSHOT,
}

# ---- curated schema 演进纪律 ----
#
# curated 列是**对外契约**：落湖的 parquet、`SCHEMAS`、字段映射 yaml、
# 因子 DSL 白名单、看板列全都在读它。经验是「悄悄删一列」比「悄悄加一列」
# 危险得多 —— 删列会让历史 parquet 与读取代码对不上，而加列基本无害。
#
# 所以：**只增不改**。任何一次改动（加/删/改类型）都必须同时
#   1) 调整 DATASET_SCHEMA_VERSION：加列 +1，删列/改类型 +1 并注明破坏性；
#   2) 更新 SCHEMA_FINGERPRINT（`schema_fingerprint()` 的输出）。
# `tests/unit/test_schema_contract.py` 会盯着这两者是否同步 —— 改列的人
# 一定会看到一条要求他显式确认的失败，而不是在 code review 里被漏掉。
DATASET_SCHEMA_VERSION = 1

# 最近一次「删列 / 改类型」的说明；只增列时保持上一版说明不动。
DATASET_SCHEMA_BREAKING_NOTE = "初始版本（建立指纹纪律时的基线）"


def schema_fingerprint() -> str:
    """curated schema 的稳定指纹（表 → 有序 (列, 类型) 的 sha256）。"""
    import hashlib

    payload = "\n".join(
        f"{table}:" + ",".join(f"{c}:{dt}" for c, dt in SCHEMAS[table].items())
        for table in sorted(SCHEMAS)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


SCHEMA_FINGERPRINT = "a4c35ef693b48fe133c713b2a7402bbaac651da5b5e65c4f79582939d05f7881"


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
