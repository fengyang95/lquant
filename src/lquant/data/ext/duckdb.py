"""把扩展表挂进 DuckDB，让 `SELECT * FROM ext_<id>` 直接可查。

为什么需要：扩展数据落在独立 parquet 区域，如果不建视图，用户只能走
`/api/ext-data/{id}/rows`；SQL 研究者（``duckdb`` / notebook / MCP 工具）
就看不到这些表。视图是只读投影，不复制数据 —— parquet 仍是唯一真相源，
DuckDB 文件损坏也不会丢扩展数据。

命名：``ext_<table_id>``。与因子名 ``ext_<table_id>_<field>`` 同前缀，
一眼能看出「这条数据/这个因子来自自有扩展表」。
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from lquant.data.ext.models import ExtConfig
from lquant.data.ext.storage import existing_dates, snapshot_path
from lquant.data.ext.store import table_dir


def view_name(table_id: str) -> str:
    return f"ext_{table_id}"


def _quote(path: Path) -> str:
    """把路径包成 SQL 字符串字面量（单引号转义）。

    路径来自数据根配置，但仍按字面量转义 —— 这类拼字符串的 DDL 是最容易
    被路径里的引号截断的地方。
    """
    return "'" + str(path).replace("'", "''") + "'"


def sync_view(config: ExtConfig, data_root: str | Path | None = None) -> bool:
    """创建/刷新该表的 DuckDB 视图；无数据时删除残留视图并返回 False。

    duckdb 连接异常**向上抛**（fail-loud）：视图没建成功却让写入报 success，
    会让用户以为 SQL 侧已经能查到。
    """
    from lquant.core.db import writer

    name = view_name(config.id)
    with writer() as con:
        if config.mode == "snapshot":
            path = snapshot_path(config, data_root)
            if not path.exists():
                con.execute(f'DROP VIEW IF EXISTS "{name}"')
                return False
            source = _quote(path)
        else:
            if not existing_dates(config, data_root):
                con.execute(f'DROP VIEW IF EXISTS "{name}"')
                return False
            source = _quote(table_dir(config.id, data_root) / "date=*" / "part.parquet")
        # union_by_name：中途加字段后老分区少列，视图不该因此报 SchemaError。
        con.execute(
            f'CREATE OR REPLACE VIEW "{name}" AS '
            f"SELECT * FROM read_parquet({source}, union_by_name = true)"
        )
    return True


def drop_view(table_id: str) -> None:
    """删除视图（表被删除时调用）。"""
    from lquant.core.db import writer

    with writer() as con:
        con.execute(f'DROP VIEW IF EXISTS "{view_name(table_id)}"')


def sync_all(data_root: str | Path | None = None) -> list[str]:
    """刷新全部扩展表视图，返回成功挂载的表 id。

    单表失败只记日志继续：一张表的路径问题不该让其它扩展表在 SQL 侧全消失。
    """
    from lquant.data.ext.store import ExtConfigStore

    ok: list[str] = []
    for cfg in ExtConfigStore(data_root).load_all():
        try:
            if sync_view(cfg, data_root):
                ok.append(cfg.id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"扩展表 {cfg.id} DuckDB 视图刷新失败: {e}")
    return ok
