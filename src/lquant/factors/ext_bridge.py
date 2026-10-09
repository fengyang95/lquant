"""扩展表 → 因子注册桥（数值字段进因子库，字符串字段进信号通道）。

口径与边界（这是扩展数据接入因子层的唯一入口，所有消费方共用）::

    数值字段 (int/float) → 因子 ext_{table}_{field}，注册进 factor_def 表
    字符串字段 (string)  → 信号条件通道（contains/==/!=），**不注册因子**
    bool 字段            → 不参与（既不能排序也不适合当条件）

为什么字符串不注册为因子：因子评价是 IC / 分组收益 / 排序口径，对「所属概念」
排大小没有金融含义。硬注册只会让因子库里多出一批永远无效的条目，稀释真正的
信号。字符串的价值在条件筛选，那是 signals.py 的职责。

为什么因子表达式是 ``$ext_{table}_{field}``：扩展数据经 ``attach_ext_columns``
注入面板后就是一个普通列，DSL 用 ``$列名`` 引用即可 —— 与 ``daily_bar`` 的
``$close`` 完全同一条执行路径，不引入第二套因子语义。
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from lquant.data.ext.pit import attach_ext_columns  # noqa: F401  对外转出
from lquant.data.ext.schema import ext_column_name
from lquant.data.ext.signals import signal_field_catalog
from lquant.data.ext.store import ExtConfigStore

__all__ = [
    "EXT_PREFIX",
    "attach_ext_columns",
    "ext_factor_ids",
    "ext_factor_specs",
    "ext_field_catalog",
    "ext_string_field_entries",
    "invalidate_ext_caches",
    "sync_ext_factors",
]

#: 扩展因子的统一前缀，便于在 factor_def 里识别/清理（绝不动用户自定义因子）。
EXT_PREFIX = "ext_"

#: 因子注册的 source 值。清理只删这个 source 的条目。
_SOURCE = "ext"

#: 上次同步的配置签名：签名没变就跳过（注册是 DB 写，不该每次读都跑）。
_SYNC_STATE: dict[str, tuple] = {}


def _config_signature(data_root: str | Path | None) -> tuple:
    """配置目录签名：任何一张表增删改都会改变它。"""
    base = ExtConfigStore(data_root).base
    try:
        sig = []
        for d in sorted(base.iterdir()):
            cfg = d / "config.json"
            if d.is_dir() and cfg.exists():
                st = cfg.stat()
                sig.append((d.name, st.st_mtime_ns, st.st_size))
        return tuple(sig)
    except OSError:
        return ()


def ext_factor_specs(data_root: str | Path | None = None) -> list[dict]:
    """当前全部扩展数值字段的因子条目（纯函数，不碰 DB）。"""
    out: list[dict] = []
    for cfg in ExtConfigStore(data_root).load_all():
        for f in cfg.numeric_fields:
            name = ext_column_name(cfg.id, f.name)
            out.append({
                "name": name,
                "expression": f"${name}",
                "description": (
                    f"扩展表「{cfg.label}」字段 {f.label or f.name}"
                    f"（{'时序·按交易日 PIT 对齐' if cfg.mode == 'timeseries' else '最新快照·仅当日帧'}）"
                ),
                "category": "扩展数据",
                "table": cfg.id,
                "field": f.name,
                "mode": cfg.mode,
            })
    return out


def ext_factor_ids(data_root: str | Path | None = None) -> frozenset[str]:
    return frozenset(s["name"] for s in ext_factor_specs(data_root))


def ext_string_field_entries(data_root: str | Path | None = None) -> list[dict]:
    """字符串字段条目（信号通道）—— 带所属表信息，供 UI/AI 枚举。"""
    out: list[dict] = []
    for cfg in ExtConfigStore(data_root).load_all():
        for entry in signal_field_catalog(cfg):
            out.append({**entry, "table": cfg.id, "table_label": cfg.label,
                        "mode": cfg.mode})
    return out


def ext_field_catalog(data_root: str | Path | None = None) -> dict:
    """扩展数据的一览：因子（数值）+ 信号条件（字符串）。

    给「因子库 / AI 提示词 / 前端画布」用的单一真相源 —— 与其让每个消费方
    各写一遍枚举逻辑（必然会漏掉字符串通道），不如只暴露这一个函数。
    """
    return {
        "factors": ext_factor_specs(data_root),
        "string_fields": ext_string_field_entries(data_root),
    }


def sync_ext_factors(data_root: str | Path | None = None) -> dict:
    """把扩展数值字段同步进 ``factor_def``（幂等，按配置签名跳过）。

    清理口径只针对 ``source='ext'``：同名冲突时以扩展表为准（扩展字段名带
    表 id 前缀，几乎不可能撞上用户自定义因子；真撞上说明用户在故意覆盖）。
    """
    from lquant.core.db import writer
    from lquant.data.store.catalog import upsert
    from lquant.data.store.ddl import DDL_STATEMENTS

    specs = ext_factor_specs(data_root)
    desired = {s["name"] for s in specs}
    key = (str(Path(data_root) if data_root is not None else ""), _config_signature(data_root))
    if _SYNC_STATE.get("key") == key:
        return {"registered": len(desired), "removed": 0, "skipped": True}

    with writer() as con:
        # factor_def 由 init_db 建；这里补一次 CREATE IF NOT EXISTS，让
        # 「全新数据根 + 第一次注册扩展因子」不必先跑 init_db。
        ddl = next(s for s in DDL_STATEMENTS if "CREATE TABLE IF NOT EXISTS factor_def" in s)
        con.execute(ddl)
        con.execute("DELETE FROM factor_def WHERE source = ?", [_SOURCE])

    if specs:
        from datetime import datetime

        df = pl.DataFrame([
            {
                "name": s["name"], "expression": s["expression"],
                "description": s["description"], "enabled": True,
                "created_at": datetime.now(), "source": _SOURCE,
                "source_ref": s["table"], "factor_id": None, "category": s["category"],
            }
            for s in specs
        ])
        upsert("factor_def", df)
    _SYNC_STATE["key"] = key
    return {"registered": len(desired), "removed": 0, "skipped": False}


def invalidate_ext_caches(data_root: str | Path | None = None) -> None:
    """写入/配置变更后清桥内状态（下次 sync 重新落库）。

    帧缓存由 ``data.ext.pit.invalidate_frame_cache`` 负责，配置缓存由
    ``data.ext.store`` 负责 —— 三处清理由 ``data.ext.store.invalidate_ext_caches``
    统一编排，这里只清自己的同步签名。
    """
    _SYNC_STATE.pop("key", None)


def ensure_synced(data_root: str | Path | None = None) -> dict:
    """``sync_ext_factors`` 的别名（供因子列表/评价入口按需触发）。"""
    return sync_ext_factors(data_root)
