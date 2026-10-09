"""数据字典：SCHEMAS 各表的中文说明（/data/dictionary 静态来源）。

字段说明手写在 FIELD_NOTES（与 schema.py 的行内注释口径一致），
未覆盖字段回退「-」，新增 schema 字段时这里同步补一行。
"""
from __future__ import annotations

from lquant.data.schema import SCHEMAS

# 表名 → (中文名, 说明)
TABLE_META: dict[str, tuple[str, str]] = {
    "daily_bar": ("日线行情", "研究态事实源（Parquet 湖），逐标的逐交易日一行"),
    "daily_basic": ("每日指标", "tushare 每日估值指标，补市值/估值空洞"),
    "minute_bar": ("分钟线", "按 (freq, 年月) 分区的分钟行情"),
    "financial_pit": ("财务 PIT", "双日期防未来函数：回测只能用 pub_date <= t"),
    "security": ("标的主档", "含退市股，防幸存者偏差"),
    "industry_classify": ("行业分类", "带生效日的行业归属"),
    "etf_meta": ("ETF 主档", "基金类型/费用/跨境等元信息"),
    "market_snapshot": ("市场快照", "实时行情快照（东财）"),
}

# 表名 → {字段: 中文说明}
FIELD_NOTES: dict[str, dict[str, str]] = {
    "daily_bar": {
        "symbol": "标的代码（如 600519.SH）",
        "trade_date": "交易日",
        "open": "开盘价",
        "high": "最高价",
        "low": "最低价",
        "close": "收盘价",
        "pre_close": "前收盘价",
        "volume": "成交量（股）",
        "amount": "成交额（元）",
        "turnover_rate": "换手率（%）",
        "adj_factor": "复权因子",
        "pct_chg": "涨跌幅（%）",
        "is_st": "ST/*ST 标记",
        "is_suspended": "停牌标记",
        "pe_ttm": "滚动市盈率",
        "pb_mrq": "市净率",
        "ps_ttm": "滚动市销率",
        "pcf_ncf_ttm": "滚动市现率",
        "total_mv": "总市值（元）",
        "float_mv": "流通市值（元）",
        "sec_type": "证券类型（stock/etf）",
        "quality_flags": "质量位掩码（0 = 干净）",
        "source": "数据来源",
        "ingested_at": "入库时间",
        "quote_ts": "报价采集时刻（epoch 毫秒，上海墙钟；NULL = 盘后批量权威行，缺列 = 无法判定）",
        "data_version": "数据版本",
    },
    "daily_basic": {
        "symbol": "标的代码",
        "trade_date": "交易日",
        "close": "收盘价",
        "turnover_rate": "换手率（%）",
        "pe_ttm": "滚动市盈率",
        "pb_mrq": "市净率",
        "ps_ttm": "滚动市销率",
        "total_mv": "总市值（元）",
        "float_mv": "流通市值（元）",
        "dv_ttm": "滚动股息率（%）",
        "total_share": "总股本（股）",
        "float_share": "流通股本（股）",
        "source": "数据来源",
    },
    "minute_bar": {
        "symbol": "标的代码",
        "ts": "时间戳",
        "freq": "K 线频率（1min/5min/60min）",
        "open": "开盘价",
        "high": "最高价",
        "low": "最低价",
        "close": "收盘价",
        "volume": "成交量（股）",
        "amount": "成交额（元）",
        "adj_factor": "复权因子",
        "source": "数据来源",
        "ingested_at": "入库时间",
    },
    "financial_pit": {
        "symbol": "标的代码",
        "stat_date": "报告期",
        "pub_date": "公告日 —— 回测只能用 pub_date <= t",
        "report_type": "报告类型（如 2024Q4）",
        "item": "财务科目",
        "value": "值",
        "unit": "单位",
        "source": "数据来源",
        "ingested_at": "入库时间",
    },
    "security": {
        "symbol": "标的代码",
        "name": "证券简称",
        "sec_type": "证券类型",
        "board": "上市板块",
        "list_date": "上市日",
        "delist_date": "退市日（幸存者偏差防护）",
        "is_st": "ST/*ST 标记",
        "source": "数据来源",
    },
    "industry_classify": {
        "symbol": "标的代码",
        "std": "行业标准（sw1/cics/em）",
        "code": "行业代码",
        "name": "行业名称",
        "std_date": "生效日 —— 用今天的分类回测十年前 = 前视偏差",
        "source": "数据来源",
    },
    "etf_meta": {
        "symbol": "标的代码",
        "name": "基金简称",
        "track_index": "跟踪指数",
        "fund_type": "基金类型（qdii/commodity/bond/money/equity）",
        "is_cross_border": "是否跨境",
        "sellable_after_days": "跨境/ commodity T+n 可卖",
        "management_fee": "管理费率",
        "custody_fee": "托管费率",
        "fund_size": "基金规模",
        "share_outstanding": "场内流通份额",
        "as_of": "数据截至日",
        "source": "数据来源",
    },
    "market_snapshot": {
        "symbol": "标的代码",
        "ts": "时间戳",
        "last": "最新价",
        "pct_chg": "涨跌幅（%）",
        "volume": "成交量",
        "amount": "成交额（元）",
        "iopv": "ETF 实时估值（IOPV）",
        "discount": "折溢价率",
        "is_stale": "僵尸报价标志：停牌/未更新不能当真实价",
        "source": "数据来源",
    },
}


def dictionary() -> dict[str, dict]:
    """按表分组返回 {表: {label, description, fields: {字段: 说明}}}。"""
    out: dict[str, dict] = {}
    for name, schema in SCHEMAS.items():
        label, desc = TABLE_META.get(name, (name, ""))
        notes = FIELD_NOTES.get(name, {})
        out[name] = {
            "label": label,
            "description": desc,
            "fields": {c: notes.get(c, "-") for c in schema},
        }
    return out
