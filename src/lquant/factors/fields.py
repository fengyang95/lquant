"""日线面板字段白名单 —— DSL 中可被 $field 引用的字段，唯一真相源。

原先白名单硬编码在 `factors/mining/submit.py::_daily_fields()`，前端画布
如果自己再列一份，就会出现「画布能选到、引擎不认」的漂移。这里集中一份，
校验侧与 UI 侧都从这里取。
"""
from __future__ import annotations

#: 校验用白名单：与 covariates/provider 口径一致（含主键列）
DAILY_FIELDS: tuple[str, ...] = (
    "trade_date",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "pre_close",
    "turnover_rate",
    "adj_factor",
    "float_mv",
)

#: 主键列：可作为分组键，但不是因子可用的数值输入
_KEY_FIELDS: frozenset[str] = frozenset({"trade_date", "symbol"})

#: 因子编辑画布可选的数值字段（按面板习惯排序：价 → 量 → 基本面）
NUMERIC_FIELDS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "pre_close",
    "turnover_rate",
    "adj_factor",
    "float_mv",
)

#: 字段中文名（画布字段面板展示）
FIELD_LABELS: dict[str, str] = {
    "trade_date": "交易日期",
    "symbol": "标的代码",
    "open": "开盘价",
    "high": "最高价",
    "low": "最低价",
    "close": "收盘价",
    "volume": "成交量",
    "amount": "成交额",
    "pre_close": "前收盘价",
    "turnover_rate": "换手率",
    "adj_factor": "复权因子",
    "float_mv": "流通市值",
}


def daily_fields() -> set[str]:
    """白名单的可变副本（保持与既有调用点 `_daily_fields()` 的返回类型一致）。"""
    return set(DAILY_FIELDS)


def field_catalog() -> list[dict[str, str]]:
    """供前端画布枚举的字段清单（只含可参与计算的数值字段）。

    主键列（trade_date / symbol）不暴露 —— 它们是 over() 的分组键，
    当成因子输入没有意义，放出去只会诱导用户搭出错误画布。
    """
    assert _KEY_FIELDS.isdisjoint(NUMERIC_FIELDS), "数值字段不得与主键列重叠"
    return [
        {"name": name, "label": FIELD_LABELS.get(name, name)}
        for name in NUMERIC_FIELDS
    ]
