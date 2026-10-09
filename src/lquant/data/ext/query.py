"""扩展表的查询面：``rows``（分页/过滤/排序/选列）与 ``values``（取值枚举）。

每张扩展表天然可查 —— 用户不需要为「看一眼自己录的数据」再写脚本。
过滤语法刻意做成简单的字符串协议（``字段:值``、``字段!=值``、``字段~子串``），
因为它同时被 CLI 与 HTTP 复用，而 DSL 级别的表达式在这里是过度设计。
"""

from __future__ import annotations

import math
from datetime import date as _date
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

from lquant.data.ext.models import ExtConfig, ExtConfigError
from lquant.data.ext.storage import read_table


def _json_safe(value: Any) -> Any:
    """把 polars 值转成可 JSON 序列化的形态。

    NaN/Inf 必须转 None：JSON 标准没有它们，直接 dump 会产出非标 JSON，
    前端 JSON.parse 直接抛错。
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (_date, datetime)):
        return value.isoformat()
    return value


def apply_filters(df: pl.DataFrame, filters: list[str] | None) -> pl.DataFrame:
    """等值/不等式/包含过滤。

    每个条目内部 ``|`` 分隔的值是 OR，多条目之间是 AND。
    过滤一律按字符串口径比较 —— 用户填的是 URL/命令行里的文本，把 "1" 当整数
    还是文本去比会引入一类「明明有数据却筛不出来」的困惑。
    """
    if not filters:
        return df
    for entry in filters:
        if "~" in entry:
            field, _, needle = entry.partition("~")
            field = field.strip()
            if not field or not needle:
                raise ExtConfigError(f"过滤条件格式错误（应为 字段~子串）: {entry!r}")
            _require_column(df, field, entry)
            df = df.filter(
                pl.col(field).cast(pl.Utf8, strict=False).fill_null("").str.contains(
                    needle, literal=True
                )
            )
            continue
        if "!=" in entry:
            field, _, raw = entry.partition("!=")
            values = {v for v in raw.split("|") if v != ""}
            field = field.strip()
            if not field or not values:
                raise ExtConfigError(f"过滤条件格式错误（应为 字段!=值）: {entry!r}")
            _require_column(df, field, entry)
            df = df.filter(
                ~pl.col(field).cast(pl.Utf8, strict=False).fill_null("").is_in(values)
            )
            continue
        field, sep, raw = entry.partition(":")
        values = [v for v in raw.split("|") if v != ""] if sep else []
        field = field.strip()
        if not field or not values:
            raise ExtConfigError(f"过滤条件格式错误（应为 字段:值1|值2）: {entry!r}")
        _require_column(df, field, entry)
        df = df.filter(pl.col(field).cast(pl.Utf8, strict=False).is_in(values))
    return df


def apply_sort(df: pl.DataFrame, sort: str | None) -> pl.DataFrame:
    """排序：``字段`` 升序 / ``字段:desc`` 降序；未知字段显式报错。"""
    if not sort:
        return df
    field, _, direction = sort.partition(":")
    field = field.strip()
    _require_column(df, field, sort)
    return df.sort(field, descending=direction.strip().lower() == "desc", nulls_last=True)


def _require_column(df: pl.DataFrame, field: str, ctx: str) -> None:
    if field not in df.columns:
        raise ExtConfigError(f"字段 {field!r} 不存在（{ctx}），可用: {df.columns}")


def query_rows(
    config: ExtConfig,
    *,
    day: _date | str | None = None,
    start_date: _date | str | None = None,
    end_date: _date | str | None = None,
    filters: list[str] | None = None,
    sort: str | None = None,
    columns: list[str] | None = None,
    offset: int = 0,
    limit: int = 1000,
    data_root: str | Path | None = None,
) -> dict:
    """查明细行。``total`` 是**过滤后、分页前**的总数（前端分页依赖它）。"""
    if offset < 0:
        raise ExtConfigError("offset 不能为负")
    if limit < 1:
        raise ExtConfigError("limit 必须 >= 1")
    df, active = read_table(
        config, day=day, start_date=start_date, end_date=end_date, data_root=data_root
    )
    # 日期序保证分页稳定：polars 读多分区 concat 后顺序无契约，
    # 不排序时 offset/limit 在不同请求间可能取到不同行。
    order = [c for c in (config.symbol_field, config.date_field) if c in df.columns]
    if order and len(df):
        df = df.sort(order)
    df = apply_filters(df, filters)
    df = apply_sort(df, sort)
    total = df.height
    if columns:
        keep = [c for c in columns if c in df.columns]
        missing = [c for c in columns if c not in df.columns]
        if missing:
            raise ExtConfigError(f"列不存在: {missing}，可用: {df.columns}")
        df = df.select(keep)
    page = df.slice(offset, limit)
    return {
        "id": config.id, "label": config.label, "mode": config.mode,
        "date": active, "total": total, "offset": offset, "limit": limit,
        "fields": [f.to_dict() for f in config.fields],
        "rows": [{k: _json_safe(v) for k, v in row.items()} for row in page.to_dicts()],
    }


def query_values(
    config: ExtConfig,
    field: str,
    *,
    day: _date | str | None = None,
    start_date: _date | str | None = None,
    end_date: _date | str | None = None,
    limit: int = 200,
    data_root: str | Path | None = None,
) -> dict:
    """字段取值去重枚举（按出现次数降序）—— 过滤下拉的配套端点。"""
    df, active = read_table(
        config, day=day, start_date=start_date, end_date=end_date, data_root=data_root
    )
    if df.is_empty():
        return {"id": config.id, "field": field, "date": active,
                "total": 0, "distinct": 0, "values": []}
    _require_column(df, field, "values")
    counted = df.group_by(field).len().sort(["len", field], descending=[True, False]).head(limit)
    return {
        "id": config.id, "field": field, "date": active, "total": df.height,
        "distinct": df[field].n_unique(),
        "values": [
            {"value": _json_safe(r[field]), "count": r["len"]} for r in counted.to_dicts()
        ],
    }
